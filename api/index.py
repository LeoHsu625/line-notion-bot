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

app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
CRON_SECRET               = os.environ.get('CRON_SECRET', '')
GROQ_API_KEY              = os.environ.get('GROQ_API_KEY', '')
SPREADSHEET_ID            = os.environ.get('SPREADSHEET_ID', '')
SERVICE_ACCOUNT_JSON      = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '{}')

MAX_USERS = 20

groq_client = Groq(api_key=GROQ_API_KEY)
TW_TZ = timezone(timedelta(hours=8))
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


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
- 查詢任務：{{"action":"query_tasks","reply":"整理好的任務清單"}}
- 標記完成：{{"action":"mark_done","row":<列號數字>,"reply":"確認完成的回覆"}}
- 新增任務：{{"action":"add_task","task_name":"任務名稱","date":null或"YYYY-MM-DD","reply":"確認新增的回覆"}}
- 其他：{{"action":"reply","reply":"友善回覆"}}

規則：
- 全部用繁體中文回覆
- 標記完成時，從清單找最相符的任務並填入 ROW 數字
- 找不到對應任務時用 action: reply 告知"""

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

        # ── Registration check ──
        status = get_user_status(user_id)

        if status == 'new':
            result = register_user(user_id)
            if result == 'registered':
                active = len(get_active_user_ids())
                reply_to_line(reply_token,
                    f'歡迎加入 AI 任務助理 Beta！🎉\n\n'
                    f'你是第 {active} 位成員，名額還剩 {MAX_USERS - active} 個。\n\n'
                    f'現在試著說：\n'
                    f'・「今天有什麼事？」\n'
                    f'・「幫我新增任務：讀一篇文章」\n'
                    f'・「週報做完了」')
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

        # ── Active user: normal flow ──
        try:
            tasks  = get_today_tasks(user_id)
            result = ask_groq(user_message, tasks)
            action = result.get('action')
            reply_text = result.get('reply', '收到！')

            if action == 'mark_done':
                row = result.get('row')
                if row and not mark_task_done(int(row)):
                    reply_text = '標記失敗，請稍後再試 🙏'
            elif action == 'add_task':
                task_name = result.get('task_name', '')
                if task_name and not add_task(task_name, user_id, result.get('date')):
                    reply_text = '新增失敗，請稍後再試 🙏'
        except Exception:
            reply_text = '發生錯誤，請稍後再試 🙏'

        reply_to_line(reply_token, reply_text)

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    today       = get_logical_date()
    active_ids  = get_active_user_ids()
    all_tasks   = get_all_today_tasks_bulk(active_ids)  # single sheet read

    def send_morning(uid):
        tasks = all_tasks.get(uid, [])
        if not tasks:
            msg = f'早安！☀️\n{today} 今天沒有待辦，有需要新增嗎？'
        else:
            lines = '\n'.join([f'• {t["name"]}' for t in tasks])
            msg = f'早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{lines}\n\n有需要調整的嗎？'
        push_to_line(uid, msg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_morning, active_ids))

    return jsonify({'status': 'ok', 'pushed': len(active_ids)})


@app.route('/api/night', methods=['GET'])
def night_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    today    = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    active_ids = get_active_user_ids()

    today_bulk    = get_all_date_tasks_bulk(today, active_ids)
    tomorrow_bulk = get_all_date_tasks_bulk(tomorrow, active_ids)

    def send_night(uid):
        t = today_bulk[uid]
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

        tm = tomorrow_bulk[uid]
        if tm['not_done']:
            lines = '\n'.join([f'• {x}' for x in tm['not_done']])
            tomorrow_section = f'明日預覽（{len(tm["not_done"])} 件）：\n{lines}'
        else:
            tomorrow_section = '明天還沒有任務規劃，記得安排一下！'

        push_to_line(uid, f'晚安！🌙 今天辛苦了。\n\n{today_line}\n\n{tomorrow_section}')

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(send_night, active_ids))

    return jsonify({'status': 'ok', 'pushed': len(active_ids)})


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
