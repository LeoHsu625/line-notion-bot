import os
import json
import hashlib
import hmac
import base64
import concurrent.futures
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
import httpx
import gspread
from google.oauth2.service_account import Credentials
from groq import Groq
from upstash_redis import Redis

app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
CRON_SECRET               = os.environ.get('CRON_SECRET', '')
GROQ_API_KEY              = os.environ.get('GROQ_API_KEY', '')
SPREADSHEET_ID            = os.environ.get('SPREADSHEET_ID', '')
SERVICE_ACCOUNT_JSON      = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '{}')
ADMIN_USER_IDS            = set(os.environ.get('ADMIN_USER_IDS', '').split(',')) - {''}
NOTION_TOKEN              = os.environ.get('NOTION_TOKEN', '')
NOTION_TASK_DB_ID         = os.environ.get('NOTION_TASK_DB_ID', '55830834dfe647a8bb6d931660e9ae22')
UPSTASH_REDIS_REST_URL    = os.environ.get('UPSTASH_REDIS_REST_URL', '')
UPSTASH_REDIS_REST_TOKEN  = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')

MAX_USERS = 10

groq_client = Groq(api_key=GROQ_API_KEY)
TW_TZ = timezone(timedelta(hours=8))
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


# ── Session memory (Upstash Redis) ───────────────────────────────────────────
# Key:   session:{line_user_id}
# TTL:   86400s（24h 後自動清空，每天對話記憶重置）
# Shape: {
#   "last_tasks": [{"row": 1, "name": "任務名稱", "_id": "notion-page-id"}],
#   "history":    [{"role": "user"|"assistant", "content": "..."}]  ← 最多 12 筆（6 輪）
# }
# last_tasks：最近展示給用戶的任務清單（含 Notion page_id），
#             讓 delete/mark_done 在 today 沒有任務時仍能找到正確的 Notion 頁面
# history：傳給 Groq 的對話紀錄，解決「把那個刪掉」等需要前後文的模糊指令

_redis_instance = None

def _get_redis():
    global _redis_instance
    if _redis_instance is None and UPSTASH_REDIS_REST_URL:
        _redis_instance = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    return _redis_instance

def load_session(user_id: str) -> dict:
    try:
        r = _get_redis()
        data = r.get(f'session:{user_id}') if r else None
        return json.loads(data) if data else {'last_tasks': [], 'history': []}
    except Exception:
        return {'last_tasks': [], 'history': []}

def save_session(user_id: str, session: dict):
    try:
        r = _get_redis()
        if r:
            r.setex(f'session:{user_id}', 86400, json.dumps(session, ensure_ascii=False))
    except Exception:
        pass


def _gspread_client():
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )
    return gspread.authorize(creds)


def get_tasks_sheet():
    return _gspread_client().open_by_key(SPREADSHEET_ID).worksheet('Tasks')


def get_users_sheet():
    return _gspread_client().open_by_key(SPREADSHEET_ID).worksheet('Users')


def get_logical_date() -> str:
    now = datetime.now(TW_TZ)
    if now.hour < 6:
        now -= timedelta(days=1)
    return now.strftime('%Y-%m-%d')


# ── Notion helpers ────────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }


def _notion_query(body: dict) -> list:
    resp = httpx.post(
        f'https://api.notion.com/v1/databases/{NOTION_TASK_DB_ID}/query',
        headers=_notion_headers(),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()['results']


def _notion_page_name(page) -> str:
    titles = page['properties'].get('Name', {}).get('title', [])
    return titles[0]['plain_text'] if titles else ''


def get_today_tasks_notion() -> list:
    today = get_logical_date()
    results = _notion_query({
        'filter': {
            'and': [
                {'property': 'Date', 'date': {'equals': today}},
                {'property': 'Status', 'checkbox': {'equals': False}},
            ]
        }
    })
    return [{'id': p['id'], 'name': _notion_page_name(p)} for p in results]


def get_upcoming_tasks_notion() -> dict:
    today = get_logical_date()
    results = _notion_query({
        'filter': {
            'and': [
                {'property': 'Date', 'date': {'on_or_after': today}},
                {'property': 'Status', 'checkbox': {'equals': False}},
            ]
        },
        'sorts': [{'property': 'Date', 'direction': 'ascending'}]
    })
    grouped = {}
    for p in results:
        date = (p['properties'].get('Date', {}).get('date') or {}).get('start', '')
        if date:
            grouped.setdefault(date, []).append(_notion_page_name(p))
    return grouped


def get_tasks_for_date_notion(date: str) -> dict:
    results = _notion_query({'filter': {'property': 'Date', 'date': {'equals': date}}})
    done, not_done = [], []
    for p in results:
        name = _notion_page_name(p)
        if p['properties'].get('Status', {}).get('checkbox', False):
            done.append(name)
        else:
            not_done.append(name)
    return {'done': done, 'not_done': not_done}


def mark_task_done_notion(page_id: str) -> bool:
    try:
        resp = httpx.patch(
            f'https://api.notion.com/v1/pages/{page_id}',
            headers=_notion_headers(),
            json={'properties': {'Status': {'checkbox': True}}},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


def delete_task_notion(page_id: str) -> bool:
    try:
        resp = httpx.patch(
            f'https://api.notion.com/v1/pages/{page_id}',
            headers=_notion_headers(),
            json={'archived': True},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


def add_task_notion(name: str, date: str = None) -> bool:
    try:
        props = {'Name': {'title': [{'text': {'content': name}}]}}
        if date:
            props['Date'] = {'date': {'start': date}}
        resp = httpx.post(
            'https://api.notion.com/v1/pages',
            headers=_notion_headers(),
            json={'parent': {'database_id': NOTION_TASK_DB_ID}, 'properties': props},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


# ── User management ───────────────────────────────────────────────────────────

def get_user_status(user_id: str) -> str:
    """Returns 'active', 'waitlist', or 'new'."""
    rows = get_users_sheet().get_all_records()
    for row in rows:
        if str(row.get('user_id', '')) == user_id:
            return 'waitlist' if str(row.get('候補', '')).upper() == 'TRUE' else 'active'
    return 'new'


def register_user(user_id: str) -> str:
    """Returns 'registered', 'waitlist', or 'already_registered'."""
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    for row in rows:
        if str(row.get('user_id', '')) == user_id:
            return 'already_registered'
    active_count = sum(1 for r in rows if str(r.get('候補', '')).upper() != 'TRUE')
    now_str = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')
    if active_count < MAX_USERS:
        sheet.append_row([user_id, now_str, 'FALSE'])
        return 'registered'
    sheet.append_row([user_id, now_str, 'TRUE'])
    return 'waitlist'


def get_active_user_ids() -> list:
    rows = get_users_sheet().get_all_records()
    return [str(r['user_id']) for r in rows if str(r.get('候補', '')).upper() != 'TRUE']


def get_waitlist_count() -> int:
    rows = get_users_sheet().get_all_records()
    return sum(1 for r in rows if str(r.get('候補', '')).upper() == 'TRUE')


# ── Task operations (Google Sheets) ──────────────────────────────────────────

def get_today_tasks(user_id: str) -> list:
    today = get_logical_date()
    rows = get_tasks_sheet().get_all_records()
    tasks = []
    for i, row in enumerate(rows, start=2):
        if (str(row.get('日期', '')) == today
                and str(row.get('完成', '')).upper() != 'TRUE'
                and str(row.get('user_id', '')) == user_id):
            tasks.append({'row': i, 'name': str(row.get('任務名稱', ''))})
    return tasks


def get_upcoming_tasks(user_id: str) -> dict:
    """Return all undone tasks from today onwards, grouped by date."""
    today = get_logical_date()
    rows = get_tasks_sheet().get_all_records()
    grouped = {}
    for row in rows:
        date = str(row.get('日期', ''))
        if (date >= today
                and str(row.get('user_id', '')) == user_id
                and str(row.get('完成', '')).upper() != 'TRUE'):
            grouped.setdefault(date, []).append(str(row.get('任務名稱', '')))
    return dict(sorted(grouped.items()))


def get_tasks_for_date(date: str, user_id: str) -> dict:
    rows = get_tasks_sheet().get_all_records()
    done, not_done = [], []
    for row in rows:
        if str(row.get('日期', '')) == date and str(row.get('user_id', '')) == user_id:
            name = str(row.get('任務名稱', ''))
            (done if str(row.get('完成', '')).upper() == 'TRUE' else not_done).append(name)
    return {'done': done, 'not_done': not_done}


def get_all_today_tasks_bulk(user_ids: list) -> dict:
    """Read Tasks sheet once and return {user_id: [tasks]} for all users."""
    today = get_logical_date()
    rows = get_tasks_sheet().get_all_records()
    result = {uid: [] for uid in user_ids}
    for i, row in enumerate(rows, start=2):
        uid = str(row.get('user_id', ''))
        if uid in result and str(row.get('日期', '')) == today and str(row.get('完成', '')).upper() != 'TRUE':
            result[uid].append({'row': i, 'name': str(row.get('任務名稱', ''))})
    return result


def get_all_date_tasks_bulk(date: str, user_ids: list) -> dict:
    rows = get_tasks_sheet().get_all_records()
    result = {uid: {'done': [], 'not_done': []} for uid in user_ids}
    for row in rows:
        uid = str(row.get('user_id', ''))
        if uid in result and str(row.get('日期', '')) == date:
            name = str(row.get('任務名稱', ''))
            key = 'done' if str(row.get('完成', '')).upper() == 'TRUE' else 'not_done'
            result[uid][key].append(name)
    return result


def mark_task_done(row_index: int) -> bool:
    try:
        get_tasks_sheet().update_cell(row_index, 3, 'TRUE')
        return True
    except Exception:
        return False


def delete_task(row_index: int) -> bool:
    try:
        get_tasks_sheet().delete_rows(row_index)
        return True
    except Exception:
        return False


def add_task(name: str, user_id: str, date: str = None) -> bool:
    try:
        get_tasks_sheet().append_row([date or get_logical_date(), name, 'FALSE', user_id])
        return True
    except Exception:
        return False


# ── AI ────────────────────────────────────────────────────────────────────────

def ask_groq(user_message: str, tasks: list, history: list = None) -> dict:
    today = get_logical_date()
    task_list_str = (
        '\n'.join([f'- [ROW:{t["row"]}] {t["name"]}' for t in tasks])
        if tasks else '（今天沒有未完成的任務）'
    )
    system_prompt = f"""你是 LINE 任務助理，幫用戶管理待辦清單。
今天日期：{today}

今日未完成任務：
{task_list_str}

根據用戶訊息判斷意圖，只回傳 JSON，格式如下：
- 查詢今日任務：{{"action":"query_tasks","reply":"整理好的任務清單"}}
- 查詢指定日期：{{"action":"query_date","date":"YYYY-MM-DD","reply":""}}
- 查詢所有未來任務：{{"action":"query_upcoming","reply":""}}
- 標記完成：{{"action":"mark_done","row":<列號數字>,"reply":"確認完成的回覆"}}
- 刪除任務：{{"action":"delete_task","row":<列號數字>,"reply":"確認刪除的回覆"}}
- 新增單筆任務：{{"action":"add_task","task_name":"任務名稱","date":null或"YYYY-MM-DD","reply":"確認新增的回覆"}}
- 新增多筆任務：{{"action":"add_tasks","tasks":[{{"task_name":"任務1","date":null}},{{"task_name":"任務2","date":"YYYY-MM-DD"}}],"reply":"確認新增N筆任務的回覆"}}
- 其他：{{"action":"reply","reply":"友善回覆"}}

規則：
- 全部用繁體中文回覆
- 用戶詢問特定日期（非今天）時使用 query_date，date 填入該日期
- 用戶一次提到多個任務時，使用 add_tasks
- 標記完成或刪除時，從任務清單找最相符的任務並填入 ROW 數字；若對話紀錄中有提過任務清單，也可參考前文找到正確的 ROW
- 找不到對應任務時用 action: reply 告知
- 如果用戶在任務中指定時間，請將時間保留在 task_name 中（例如：task_name 為「21:00 打匹克球」）"""

    messages = [{'role': 'system', 'content': system_prompt}]
    if history:
        messages.extend(history[-12:])  # 最多帶入最近 6 輪對話
    messages.append({'role': 'user', 'content': user_message})

    resp = groq_client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        max_tokens=500,
        messages=messages,
    )
    text = resp.choices[0].message.content.strip()
    if '{' in text:
        text = text[text.index('{'):text.rindex('}') + 1]
    return json.loads(text)


# ── LINE messaging ────────────────────────────────────────────────────────────

def reply_to_line(reply_token: str, message: str):
    httpx.post(
        'https://api.line.me/v2/bot/message/reply',
        headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
        json={'replyToken': reply_token, 'messages': [{'type': 'text', 'text': message}]},
        timeout=10,
    )


def push_to_line(user_id: str, message: str):
    httpx.post(
        'https://api.line.me/v2/bot/message/push',
        headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
        json={'to': user_id, 'messages': [{'type': 'text', 'text': message}]},
        timeout=10,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(base64.b64encode(h).decode(), signature):
        abort(400)

    for event in json.loads(body).get('events', []):
        if event.get('type') != 'message' or event['message'].get('type') != 'text':
            continue

        user_message = event['message']['text'].strip()
        reply_token  = event['replyToken']
        user_id      = event.get('source', {}).get('userId', '')

        if user_message == '/myid':
            reply_to_line(reply_token, f'你的 LINE User ID：\n{user_id}')
            continue

        # ── Admin bypass ──
        is_admin = user_id in ADMIN_USER_IDS
        if is_admin:
            status = 'active'
        else:
            status = get_user_status(user_id)

        if status == 'new':
            result = register_user(user_id)
            if result == 'registered':
                active = len(get_active_user_ids())
                reply_to_line(reply_token,
                    f'嗨！我是 Aria 👋 你的 LINE 任務小幫手\n\n'
                    f'你是第 {active} 位 Beta 成員，名額還剩 {MAX_USERS - active} 個 🎉\n\n'
                    f'直接跟我說話就好，例如：\n\n'
                    f'📋 「今天有什麼事？」\n'
                    f'➕ 「幫我記：明天要繳費」\n'
                    f'➕ 「5/9 21:00 打匹克球」（支援時間和日期）\n'
                    f'✅ 「週報做完了」\n'
                    f'🗑️ 「刪除：打匹克球」\n'
                    f'📌 「我這週有什麼事？」\n\n'
                    f'有什麼想記的，直接說就好 😊\n\n'
                    f'📌 使用須知：你的任務資料會儲存於管理者的 Google Sheet，僅供本服務使用。')
            else:
                waitlist = get_waitlist_count()
                reply_to_line(reply_token,
                    f'感謝你的興趣！Beta 20 個名額已全數額滿 😔\n\n'
                    f'目前候補人數：{waitlist} 人\n\n'
                    f'已幫你登記候補，有空缺時會第一時間通知你！\n'
                    f'在此之前，歡迎追蹤 IG @leohsu625 了解更多 ✨')
            continue

        if status == 'waitlist':
            reply_to_line(reply_token,
                f'你目前在候補名單，有空缺時會主動通知你 🙏\n'
                f'歡迎追蹤 IG @leohsu625 了解最新動態')
            continue

        # ── Admin: Notion flow ──
        if is_admin:
            session = load_session(user_id)
            try:
                notion_tasks = get_today_tasks_notion()
                today_tasks = [{'row': i + 1, 'name': t['name'], '_id': t['id']}
                               for i, t in enumerate(notion_tasks)]
                # 今天有任務就用今天的，否則用 session 快取（例如昨晚提醒後隔天還沒刷新）
                context_tasks = today_tasks if today_tasks else session.get('last_tasks', [])
                result = ask_groq(user_message, context_tasks, session.get('history', []))
                action = result.get('action')
                reply_text = result.get('reply', '收到！')

                if action == 'query_tasks':
                    if not today_tasks:
                        reply_text = '今天沒有待辦事項 🎉'
                    else:
                        lines = [f'📋 今天有 {len(today_tasks)} 件待辦：\n']
                        lines += [f'・{t["name"]}' for t in today_tasks]
                        reply_text = '\n'.join(lines)
                    session['last_tasks'] = today_tasks
                elif action == 'query_upcoming':
                    upcoming = get_upcoming_tasks_notion()
                    if not upcoming:
                        reply_text = '目前沒有任何未完成的任務安排 🎉'
                    else:
                        lines = []
                        for date, names in upcoming.items():
                            lines.append(f'📅 {date}')
                            lines += [f'  ・{n}' for n in names]
                        reply_text = '📋 任務總覽：\n\n' + '\n'.join(lines)
                elif action == 'query_date':
                    date = result.get('date', '')
                    if date:
                        dt = get_tasks_for_date_notion(date)
                        not_done, done = dt['not_done'], dt['done']
                        if not not_done and not done:
                            reply_text = f'{date} 沒有任何任務紀錄。'
                        else:
                            lines = []
                            if not_done:
                                lines.append(f'未完成（{len(not_done)} 件）：')
                                lines += [f'・{t}' for t in not_done]
                            if done:
                                lines.append(f'\n已完成（{len(done)} 件）：')
                                lines += [f'・✅ {t}' for t in done]
                            reply_text = f'📅 {date} 的任務：\n\n' + '\n'.join(lines)
                elif action == 'mark_done':
                    row = result.get('row')
                    if row:
                        page_id = next((t['_id'] for t in context_tasks if t['row'] == int(row)), None)
                        task_name = next((t['name'] for t in context_tasks if t['row'] == int(row)), None)
                        if page_id and mark_task_done_notion(page_id):
                            if task_name:
                                reply_text = f'✅ 已完成：{task_name}\n\n如果標錯了，請告訴我！'
                        else:
                            reply_text = '標記失敗，請稍後再試 🙏'
                elif action == 'delete_task':
                    row = result.get('row')
                    if row:
                        page_id = next((t['_id'] for t in context_tasks if t['row'] == int(row)), None)
                        task_name = next((t['name'] for t in context_tasks if t['row'] == int(row)), None)
                        if page_id and delete_task_notion(page_id):
                            reply_text = f'🗑️ 已刪除：{task_name or "任務"}'
                        else:
                            reply_text = '刪除失敗，請稍後再試 🙏'
                elif action == 'add_task':
                    task_name = result.get('task_name', '')
                    if task_name and not add_task_notion(task_name, result.get('date')):
                        reply_text = '新增失敗，請稍後再試 🙏'
                elif action == 'add_tasks':
                    succeeded, failed = [], []
                    for t in result.get('tasks', []):
                        task_name = t.get('task_name', '')
                        if not task_name:
                            continue
                        date_val = t.get('date')
                        if add_task_notion(task_name, date_val):
                            succeeded.append(f'{date_val} {task_name}' if date_val else task_name)
                        else:
                            failed.append(task_name)
                    if succeeded:
                        lines = [f'✅ 新增了 {len(succeeded)} 個任務：']
                        lines += [f'- {s}' for s in succeeded]
                        if failed:
                            lines.append(f'\n⚠️ 以下任務新增失敗：{", ".join(failed)} 🙏')
                        reply_text = '\n'.join(lines)
                    elif failed:
                        reply_text = f'部分任務新增失敗：{", ".join(failed)} 🙏'
            except Exception:
                reply_text = '抱歉，剛才沒有反應過來 😅 可以再說一次，或換個方式試試？'

            history = session.get('history', [])
            history.append({'role': 'user', 'content': user_message})
            history.append({'role': 'assistant', 'content': reply_text})
            session['history'] = history[-12:]
            save_session(user_id, session)

            reply_to_line(reply_token, reply_text)
            continue

        # ── Active user: normal flow ──
        try:
            tasks  = get_today_tasks(user_id)
            result = ask_groq(user_message, tasks)
            action = result.get('action')
            reply_text = result.get('reply', '收到！')

            if action == 'query_tasks':
                if not tasks:
                    reply_text = '今天沒有待辦事項 🎉'
                else:
                    lines = [f'📋 今天有 {len(tasks)} 件待辦：\n']
                    lines += [f'・{t["name"]}' for t in tasks]
                    reply_text = '\n'.join(lines)
            elif action == 'query_upcoming':
                upcoming = get_upcoming_tasks(user_id)
                if not upcoming:
                    reply_text = '目前沒有任何未完成的任務安排 🎉'
                else:
                    lines = []
                    for date, names in upcoming.items():
                        lines.append(f'📅 {date}')
                        lines += [f'  ・{n}' for n in names]
                    reply_text = '📋 任務總覽：\n\n' + '\n'.join(lines)
            elif action == 'query_date':
                date = result.get('date', '')
                if date:
                    dt = get_tasks_for_date(date, user_id)
                    not_done, done = dt['not_done'], dt['done']
                    if not not_done and not done:
                        reply_text = f'{date} 沒有任何任務紀錄。'
                    else:
                        lines = []
                        if not_done:
                            lines.append(f'未完成（{len(not_done)} 件）：')
                            lines += [f'・{t}' for t in not_done]
                        if done:
                            lines.append(f'\n已完成（{len(done)} 件）：')
                            lines += [f'・✅ {t}' for t in done]
                        reply_text = f'📅 {date} 的任務：\n\n' + '\n'.join(lines)
            elif action == 'mark_done':
                row = result.get('row')
                if row:
                    if mark_task_done(int(row)):
                        task_name = next((t['name'] for t in tasks if t['row'] == int(row)), None)
                        if task_name:
                            reply_text = f'✅ 已完成：{task_name}\n\n如果標錯了，請告訴我！'
                    else:
                        reply_text = '標記失敗，請稍後再試 🙏'
            elif action == 'delete_task':
                row = result.get('row')
                if row:
                    task_name = next((t['name'] for t in tasks if t['row'] == int(row)), None)
                    if delete_task(int(row)):
                        reply_text = f'🗑️ 已刪除：{task_name or "任務"}'
                    else:
                        reply_text = '刪除失敗，請稍後再試 🙏'
            elif action == 'add_task':
                task_name = result.get('task_name', '')
                if task_name and not add_task(task_name, user_id, result.get('date')):
                    reply_text = '新增失敗，請稍後再試 🙏'
            elif action == 'add_tasks':
                succeeded = []
                failed = []
                for t in result.get('tasks', []):
                    task_name = t.get('task_name', '')
                    if not task_name:
                        continue
                    date_val = t.get('date')
                    if add_task(task_name, user_id, date_val):
                        label = f'{date_val} {task_name}' if date_val else task_name
                        succeeded.append(label)
                    else:
                        failed.append(task_name)
                if succeeded:
                    lines = [f'✅ 新增了 {len(succeeded)} 個任務：']
                    lines += [f'- {s}' for s in succeeded]
                    if failed:
                        lines.append(f'\n⚠️ 以下任務新增失敗：{", ".join(failed)} 🙏')
                    reply_text = '\n'.join(lines)
                elif failed:
                    reply_text = f'部分任務新增失敗：{", ".join(failed)} 🙏'
        except Exception:
            reply_text = '抱歉，剛才沒有反應過來 😅 可以再說一次，或換個方式試試？'

        reply_to_line(reply_token, reply_text)

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    today = get_logical_date()

    # Admin users: fetch from Notion
    admin_tasks = {}
    for uid in ADMIN_USER_IDS:
        try:
            admin_tasks[uid] = get_today_tasks_notion()
        except Exception:
            admin_tasks[uid] = []

    # Regular users: fetch from Google Sheets (best-effort)
    regular_ids = []
    all_tasks = {}
    try:
        regular_ids = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        all_tasks = get_all_today_tasks_bulk(regular_ids)
    except Exception:
        pass

    def send_morning(uid):
        if uid in ADMIN_USER_IDS:
            tasks = admin_tasks.get(uid, [])
        else:
            tasks = all_tasks.get(uid, [])
        if not tasks:
            msg = f'早安！☀️\n{today} 今天沒有待辦，有需要新增嗎？'
        else:
            lines = '\n'.join([f'• {t["name"]}' for t in tasks])
            msg = f'早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{lines}\n\n有需要調整的嗎？'
        push_to_line(uid, msg)

    all_ids = list(ADMIN_USER_IDS) + regular_ids
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_morning, all_ids))

    return jsonify({'status': 'ok', 'pushed': len(all_ids)})


@app.route('/api/lunch', methods=['GET'])
def lunch_cron():
    today = get_logical_date()

    # Admin users: fetch from Notion
    admin_tasks = {}
    for uid in ADMIN_USER_IDS:
        try:
            admin_tasks[uid] = get_today_tasks_notion()
        except Exception:
            admin_tasks[uid] = []

    # Regular users: best-effort
    regular_ids = []
    all_tasks = {}
    try:
        regular_ids = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        all_tasks = get_all_today_tasks_bulk(regular_ids)
    except Exception:
        pass

    def send_lunch(uid):
        if uid in ADMIN_USER_IDS:
            tasks = admin_tasks.get(uid, [])
        else:
            tasks = all_tasks.get(uid, [])
        if not tasks:
            msg = f'午休提醒 🌞\n今天待辦都清了，下午繼續加油！'
        else:
            lines = '\n'.join([f'• {t["name"]}' for t in tasks])
            msg = f'午休提醒 🌞 還有 {len(tasks)} 件待辦：\n\n{lines}'
        push_to_line(uid, msg)

    all_ids = list(ADMIN_USER_IDS) + regular_ids
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_lunch, all_ids))

    return jsonify({'status': 'ok', 'pushed': len(all_ids)})


@app.route('/api/night', methods=['GET'])
def night_cron():
    today    = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    # Admin users: fetch from Notion
    admin_today = {}
    admin_tomorrow = {}
    for uid in ADMIN_USER_IDS:
        try:
            admin_today[uid]    = get_tasks_for_date_notion(today)
            admin_tomorrow[uid] = get_tasks_for_date_notion(tomorrow)
        except Exception:
            admin_today[uid]    = {'done': [], 'not_done': []}
            admin_tomorrow[uid] = {'done': [], 'not_done': []}

    # Regular users: best-effort
    regular_ids = []
    today_bulk = {}
    tomorrow_bulk = {}
    try:
        regular_ids   = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        today_bulk    = get_all_date_tasks_bulk(today, regular_ids)
        tomorrow_bulk = get_all_date_tasks_bulk(tomorrow, regular_ids)
    except Exception:
        pass

    def send_night(uid):
        if uid in ADMIN_USER_IDS:
            t  = admin_today.get(uid, {'done': [], 'not_done': []})
            tm = admin_tomorrow.get(uid, {'done': [], 'not_done': []})
        else:
            t  = today_bulk.get(uid, {'done': [], 'not_done': []})
            tm = tomorrow_bulk.get(uid, {'done': [], 'not_done': []})

        done_count  = len(t['done'])
        total_count = done_count + len(t['not_done'])

        if total_count > 0:
            pct = int(done_count / total_count * 100)
            today_line = f'今日完成率：{done_count}/{total_count}（{pct}%）'
            if t['not_done']:
                undone = '\n'.join([f'  ❌ {x}' for x in t['not_done']])
                today_line += f'\n未完成：\n{undone}'
        else:
            today_line = '今日沒有任務紀錄'

        if tm['not_done']:
            lines = '\n'.join([f'• {x}' for x in tm['not_done']])
            tomorrow_section = f'明日預覽（{len(tm["not_done"])} 件）：\n{lines}'
        else:
            tomorrow_section = '明天還沒有任務規劃，記得安排一下！'

        push_to_line(uid, f'晚安！🌙 今天辛苦了。\n\n{today_line}\n\n{tomorrow_section}')

    all_ids = list(ADMIN_USER_IDS) + regular_ids
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_night, all_ids))

    return jsonify({'status': 'ok', 'pushed': len(all_ids)})


@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)
    rows = get_users_sheet().get_all_records()
    active   = [r for r in rows if str(r.get('候補', '')).upper() != 'TRUE']
    waitlist = [r for r in rows if str(r.get('候補', '')).upper() == 'TRUE']
    return jsonify({
        'active': len(active),
        'waitlist': len(waitlist),
        'slots_remaining': max(0, MAX_USERS - len(active)),
    })


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'LINE AI Task Assistant Beta is running!'})
