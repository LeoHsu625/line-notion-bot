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
from notion_client import Client as NotionClient

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

MAX_USERS = 10
INACTIVITY_DAYS = int(os.environ.get('INACTIVITY_DAYS', '7'))

groq_client = Groq(api_key=GROQ_API_KEY)
TW_TZ = timezone(timedelta(hours=8))
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


# ── Notion helpers ────────────────────────────────────────────────────────────

def _notion():
    return NotionClient(auth=NOTION_TOKEN)


def _notion_page_name(page) -> str:
    arr = page['properties'].get('Name', {}).get('title', [])
    return ''.join([t.get('plain_text', '') for t in arr]) or '(無名稱)'


def get_today_tasks_notion() -> list:
    today = get_logical_date()
    resp = _notion().databases.query(
        database_id=NOTION_TASK_DB_ID,
        filter={"and": [
            {"property": "Date", "date": {"equals": today}},
            {"property": "Status", "checkbox": {"equals": False}},
        ]},
        sorts=[{"property": "Date", "direction": "ascending"}],
    )
    return [{'row': p['id'], 'name': _notion_page_name(p)} for p in resp['results']]


def get_upcoming_tasks_notion() -> dict:
    today = get_logical_date()
    resp = _notion().databases.query(
        database_id=NOTION_TASK_DB_ID,
        filter={"and": [
            {"property": "Date", "date": {"on_or_after": today}},
            {"property": "Status", "checkbox": {"equals": False}},
        ]},
        sorts=[{"property": "Date", "direction": "ascending"}],
    )
    grouped = {}
    for p in resp['results']:
        date_obj = p['properties'].get('Date', {}).get('date')
        date = date_obj.get('start', '')[:10] if date_obj else ''
        if date:
            grouped.setdefault(date, []).append(_notion_page_name(p))
    return dict(sorted(grouped.items()))


def get_tasks_for_date_notion(date: str) -> dict:
    resp = _notion().databases.query(
        database_id=NOTION_TASK_DB_ID,
        filter={"property": "Date", "date": {"equals": date}},
    )
    done, not_done = [], []
    for p in resp['results']:
        name = _notion_page_name(p)
        checked = p['properties'].get('Status', {}).get('checkbox', False)
        (done if checked else not_done).append(name)
    return {'done': done, 'not_done': not_done}


def mark_task_done_notion(page_id: str) -> bool:
    try:
        _notion().pages.update(page_id=page_id, properties={"Status": {"checkbox": True}})
        return True
    except Exception:
        return False


def delete_task_notion(page_id: str) -> bool:
    try:
        _notion().pages.update(page_id=page_id, archived=True)
        return True
    except Exception:
        return False


def add_task_notion(name: str, date: str = None) -> bool:
    try:
        _notion().pages.create(
            parent={"database_id": NOTION_TASK_DB_ID},
            properties={
                "Name": {"title": [{"text": {"content": name}}]},
                "Date": {"date": {"start": date or get_logical_date()}},
                "Status": {"checkbox": False},
            },
        )
        return True
    except Exception:
        return False


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


# ── User management ──────────────────────────────────────────────────────────

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


def update_last_active(user_id: str):
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    now_str = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')
    for i, row in enumerate(rows, start=2):
        if str(row.get('user_id', '')) == user_id:
            sheet.update_cell(i, 4, now_str)
            return


def get_stale_active_users(days: int) -> list:
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    threshold = datetime.now(TW_TZ) - timedelta(days=days)
    stale = []
    for row in rows:
        if str(row.get('候補', '')).upper() == 'TRUE':
            continue
        uid = str(row.get('user_id', ''))
        if not uid or uid in ADMIN_USER_IDS:
            continue
        last = str(row.get('last_active', '') or row.get('加入時間', ''))
        if not last:
            continue
        try:
            last_dt = datetime.strptime(last[:16], '%Y-%m-%d %H:%M').replace(tzinfo=TW_TZ)
            if last_dt < threshold:
                stale.append(uid)
        except ValueError:
            pass
    return stale


def move_to_waitlist(user_id: str):
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        if str(row.get('user_id', '')) == user_id:
            sheet.update_cell(i, 3, 'TRUE')
            return


def promote_first_waitlist() -> str | None:
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        if str(row.get('候補', '')).upper() == 'TRUE':
            uid = str(row.get('user_id', ''))
            now_str = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')
            sheet.update_cell(i, 3, 'FALSE')
            sheet.update_cell(i, 4, now_str)
            return uid
    return None


# ── Task operations ───────────────────────────────────────────────────────────

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

def ask_groq(user_message: str, tasks: list) -> dict:
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
- 標記完成時，從清單找最相符的任務並填入 ROW 數字
- 找不到對應任務時用 action: reply 告知
- 如果用戶在任務中指定時間，請將時間保留在 task_name 中（例如：task_name 為「21:00 打匹克球」）"""

    resp = groq_client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        max_tokens=500,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
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


def _register_and_reply(user_id: str, reply_token: str):
    result = register_user(user_id)
    if result == 'registered':
        active = len(get_active_user_ids())
        remaining = MAX_USERS - active
        spots_line = f'名額還剩 {remaining} 個' if remaining > 0 else '你搶到最後一個名額'
        reply_to_line(reply_token,
            f'嗨！我是 Aria 👋 你的 LINE 任務小幫手\n\n'
            f'你是第 {active} 位 Beta 成員，{spots_line} 🎉\n\n'
            f'直接跟我說話就好，例如：\n\n'
            f'📋 「今天有什麼事？」\n'
            f'➕ 「幫我記：明天要繳費」\n'
            f'➕ 「5/9 21:00 打匹克球」（支援時間和日期）\n'
            f'✅ 「週報做完了」\n'
            f'🗑️ 「刪除：打匹克球」\n'
            f'📌 「我這週有什麼事？」\n\n'
            f'有什麼想記的，直接說就好 😊\n\n'
            f'📌 使用須知：你的任務資料會儲存於管理者的 Google Sheet，僅供本服務使用。\n'
            f'⏰ 提醒：{INACTIVITY_DAYS} 天內未使用，名額會自動釋出給候補用戶。')
    elif result == 'waitlist':
        waitlist = get_waitlist_count()
        reply_to_line(reply_token,
            f'感謝你的興趣！Beta {MAX_USERS} 個名額已全數額滿 😔\n\n'
            f'目前候補人數：{waitlist} 人\n\n'
            f'已幫你登記候補，有空缺時會第一時間通知你！\n'
            f'在此之前，歡迎追蹤 IG @leohsu625 了解更多 ✨')


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(base64.b64encode(h).decode(), signature):
        abort(400)

    for event in json.loads(body).get('events', []):
        event_type  = event.get('type')
        reply_token = event.get('replyToken', '')
        user_id     = event.get('source', {}).get('userId', '')

        if event_type == 'follow':
            if user_id:
                _register_and_reply(user_id, reply_token)
            continue

        if event_type != 'message' or event['message'].get('type') != 'text':
            continue

        user_message = event['message']['text'].strip()

        if user_message == '/myid':
            reply_to_line(reply_token, f'你的 LINE User ID：\n{user_id}')
            continue

        # ── Admin bypass ──
        if user_id in ADMIN_USER_IDS:
            status = 'active'
        else:
            status = get_user_status(user_id)

        if status == 'new':
            _register_and_reply(user_id, reply_token)
            continue

        if status == 'waitlist':
            reply_to_line(reply_token,
                f'你目前在候補名單，有空缺時會主動通知你 🙏\n'
                f'歡迎追蹤 IG @leohsu625 了解最新動態')
            continue

        # ── Active user: normal flow ──
        is_admin = user_id in ADMIN_USER_IDS
        try:
            tasks  = get_today_tasks_notion() if is_admin else get_today_tasks(user_id)
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
                upcoming = get_upcoming_tasks_notion() if is_admin else get_upcoming_tasks(user_id)
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
                    dt = get_tasks_for_date_notion(date) if is_admin else get_tasks_for_date(date, user_id)
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
                    ok = mark_task_done_notion(row) if is_admin else mark_task_done(int(row))
                    if ok:
                        match_row = row if is_admin else int(row)
                        task_name = next((t['name'] for t in tasks if t['row'] == match_row), None)
                        if task_name:
                            reply_text = f'✅ 已完成：{task_name}\n\n如果標錯了，請告訴我！'
                    else:
                        reply_text = '標記失敗，請稍後再試 🙏'
            elif action == 'delete_task':
                row = result.get('row')
                if row:
                    match_row = row if is_admin else int(row)
                    task_name = next((t['name'] for t in tasks if t['row'] == match_row), None)
                    ok = delete_task_notion(row) if is_admin else delete_task(int(row))
                    if ok:
                        reply_text = f'🗑️ 已刪除：{task_name or "任務"}'
                    else:
                        reply_text = '刪除失敗，請稍後再試 🙏'
            elif action == 'add_task':
                task_name = result.get('task_name', '')
                if task_name:
                    ok = add_task_notion(task_name, result.get('date')) if is_admin else add_task(task_name, user_id, result.get('date'))
                    if not ok:
                        reply_text = '新增失敗，請稍後再試 🙏'
            elif action == 'add_tasks':
                succeeded = []
                failed = []
                for t in result.get('tasks', []):
                    task_name = t.get('task_name', '')
                    if not task_name:
                        continue
                    date_val = t.get('date')
                    ok = add_task_notion(task_name, date_val) if is_admin else add_task(task_name, user_id, date_val)
                    if ok:
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
        try:
            update_last_active(user_id)
        except Exception:
            pass

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    today = get_logical_date()

    try:
        regular_ids = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        all_tasks   = get_all_today_tasks_bulk(regular_ids) if regular_ids else {}
    except Exception:
        regular_ids = []
        all_tasks   = {}

    all_ids = list(ADMIN_USER_IDS) + regular_ids

    def send_morning(uid):
        tasks = get_today_tasks_notion() if uid in ADMIN_USER_IDS else all_tasks.get(uid, [])
        if not tasks:
            msg = f'早安！☀️\n{today} 今天沒有待辦，有需要新增嗎？'
        else:
            lines = '\n'.join([f'• {t["name"]}' for t in tasks])
            msg = f'早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{lines}\n\n有需要調整的嗎？'
        push_to_line(uid, msg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_morning, all_ids))

    return jsonify({'status': 'ok', 'pushed': len(all_ids)})


@app.route('/api/night', methods=['GET'])
def night_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    today    = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        regular_ids = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        today_bulk    = get_all_date_tasks_bulk(today, regular_ids)    if regular_ids else {}
        tomorrow_bulk = get_all_date_tasks_bulk(tomorrow, regular_ids) if regular_ids else {}
    except Exception:
        regular_ids   = []
        today_bulk    = {}
        tomorrow_bulk = {}

    all_ids = list(ADMIN_USER_IDS) + regular_ids

    # Pre-fetch Notion data for admin users
    notion_today    = {uid: get_tasks_for_date_notion(today)    for uid in ADMIN_USER_IDS}
    notion_tomorrow = {uid: get_tasks_for_date_notion(tomorrow) for uid in ADMIN_USER_IDS}

    def send_night(uid):
        if uid in ADMIN_USER_IDS:
            t        = notion_today[uid]
            tm_tasks = notion_tomorrow[uid]['not_done']
        else:
            t        = today_bulk[uid]
            tm_tasks = tomorrow_bulk[uid]['not_done']

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

        if tm_tasks:
            lines = '\n'.join([f'• {x}' for x in tm_tasks])
            tomorrow_section = f'明日預覽（{len(tm_tasks)} 件）：\n{lines}'
        else:
            tomorrow_section = '明天還沒有任務規劃，記得安排一下！'

        push_to_line(uid, f'晚安！🌙 今天辛苦了。\n\n{today_line}\n\n{tomorrow_section}')

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_night, all_ids))

    return jsonify({'status': 'ok', 'pushed': len(all_ids)})


@app.route('/api/cleanup', methods=['GET'])
def cleanup_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    stale = get_stale_active_users(INACTIVITY_DAYS)
    deactivated, promoted = 0, 0

    for uid in stale:
        move_to_waitlist(uid)
        deactivated += 1
        push_to_line(uid,
            f'嗨！你已經 {INACTIVITY_DAYS} 天沒有使用 Aria，\n'
            f'名額已暫時釋出給候補的用戶。\n\n'
            f'想繼續使用的話，直接傳訊息給我，\n'
            f'有空缺時會主動通知你 🙏')

        new_uid = promote_first_waitlist()
        if new_uid:
            promoted += 1
            active_count = len(get_active_user_ids())
            push_to_line(new_uid,
                f'好消息！剛才有名額釋出，\n'
                f'你已從候補升格為正式成員 🎉\n\n'
                f'你是第 {active_count} 位 Beta 成員！\n\n'
                f'直接跟我說話就好，例如：\n\n'
                f'📋 「今天有什麼事？」\n'
                f'➕ 「幫我記：明天要繳費」\n'
                f'✅ 「週報做完了」\n\n'
                f'有什麼想記的，直接說就好 😊')

    return jsonify({'status': 'ok', 'deactivated': deactivated, 'promoted': promoted})


@app.route('/api/lunch', methods=['GET'])
def lunch_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    try:
        regular_ids = [uid for uid in get_active_user_ids() if uid not in ADMIN_USER_IDS]
        all_tasks   = get_all_today_tasks_bulk(regular_ids) if regular_ids else {}
    except Exception:
        regular_ids = []
        all_tasks   = {}

    all_ids = list(ADMIN_USER_IDS) + regular_ids

    def send_lunch(uid):
        tasks = get_today_tasks_notion() if uid in ADMIN_USER_IDS else all_tasks.get(uid, [])
        if not tasks:
            return
        lines = '\n'.join([f'• {t["name"]}' for t in tasks[:5]])
        more  = f'\n...還有 {len(tasks) - 5} 件' if len(tasks) > 5 else ''
        push_to_line(uid, f'📲 午休提醒 — 還有 {len(tasks)} 件待辦：\n\n{lines}{more}\n\n趁午休處理幾件？')

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_lunch, all_ids))

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
