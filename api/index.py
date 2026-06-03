import os
import json
import hashlib
import hmac
import base64
import concurrent.futures
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
import httpx
import anthropic
from upstash_redis import Redis

app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
ANTHROPIC_API_KEY         = os.environ.get('ANTHROPIC_API_KEY', '')
NOTION_TOKEN              = os.environ.get('NOTION_TOKEN', '')
NOTION_TASK_DB_ID         = os.environ.get('NOTION_TASK_DB_ID', '55830834dfe647a8bb6d931660e9ae22')
UPSTASH_REDIS_REST_URL    = os.environ.get('UPSTASH_REDIS_REST_URL', '')
UPSTASH_REDIS_REST_TOKEN  = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')
ADMIN_USER_IDS            = set(os.environ.get('ADMIN_USER_IDS', '').split(',')) - {''}

TW_TZ = timezone(timedelta(hours=8))

_anthropic_client = None
def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_API_KEY:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


# ── Session memory (Upstash Redis) ───────────────────────────────────────────
# Key:   session:{line_user_id}
# TTL:   86400s（24h 後自動清空）
# Shape: {
#   "last_tasks": [{"row": 1, "name": "任務名稱", "_id": "notion-page-id"}],
#   "history":    [{"role": "user"|"assistant", "content": "..."}]  ← 最多 12 筆（6 輪）
# }
# last_tasks：最近展示的任務清單（含 Notion page_id），供 delete/mark_done 跨輪查找
# history：傳給 Claude 的對話紀錄，解決「把那個刪掉」等模糊跨輪指令

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
        headers=_notion_headers(), json=body, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()['results']

def _notion_page_name(page) -> str:
    titles = page['properties'].get('Name', {}).get('title', [])
    return titles[0]['plain_text'] if titles else ''

def get_logical_date() -> str:
    now = datetime.now(TW_TZ)
    if now.hour < 6:
        now -= timedelta(days=1)
    return now.strftime('%Y-%m-%d')

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


# ── AI (Claude Haiku) ─────────────────────────────────────────────────────────

def ask_ai(user_message: str, tasks: list, history: list = None) -> dict:
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

    ai_messages = []
    if history:
        ai_messages.extend(history[-12:])
    ai_messages.append({'role': 'user', 'content': user_message})

    resp = _get_anthropic().messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=500,
        system=system_prompt,
        messages=ai_messages,
    )
    text = resp.content[0].text.strip()
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

        session = load_session(user_id)
        try:
            notion_tasks = get_today_tasks_notion()
            today_tasks = [{'row': i + 1, 'name': t['name'], '_id': t['id']}
                           for i, t in enumerate(notion_tasks)]
            context_tasks = today_tasks if today_tasks else session.get('last_tasks', [])
            result = ask_ai(user_message, context_tasks, session.get('history', []))
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

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    today = get_logical_date()
    for uid in ADMIN_USER_IDS:
        try:
            tasks = get_today_tasks_notion()
            if not tasks:
                msg = f'早安！☀️\n{today} 今天沒有待辦，有需要新增嗎？'
            else:
                lines = '\n'.join([f'• {t["name"]}' for t in tasks])
                msg = f'早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{lines}\n\n有需要調整的嗎？'
            push_to_line(uid, msg)
        except Exception:
            pass
    return jsonify({'status': 'ok'})


@app.route('/api/lunch', methods=['GET'])
def lunch_cron():
    for uid in ADMIN_USER_IDS:
        try:
            tasks = get_today_tasks_notion()
            if not tasks:
                msg = '午休提醒 🌞\n今天待辦都清了，下午繼續加油！'
            else:
                lines = '\n'.join([f'• {t["name"]}' for t in tasks])
                msg = f'午休提醒 🌞 還有 {len(tasks)} 件待辦：\n\n{lines}'
            push_to_line(uid, msg)
        except Exception:
            pass
    return jsonify({'status': 'ok'})


@app.route('/api/night', methods=['GET'])
def night_cron():
    today    = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    for uid in ADMIN_USER_IDS:
        try:
            t  = get_tasks_for_date_notion(today)
            tm = get_tasks_for_date_notion(tomorrow)
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
        except Exception:
            pass
    return jsonify({'status': 'ok'})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': '許小榮 personal assistant is running!'})
