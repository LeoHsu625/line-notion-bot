import os
import json
import hashlib
import hmac
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
import httpx
import gspread
from google.oauth2.service_account import Credentials
from groq import Groq

app = Flask(__name__)

LINE_CHANNEL_SECRET      = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_USER_ID             = os.environ.get('LINE_USER_ID', '')
CRON_SECRET              = os.environ.get('CRON_SECRET', '')
GROQ_API_KEY             = os.environ.get('GROQ_API_KEY', '')
SPREADSHEET_ID           = os.environ.get('SPREADSHEET_ID', '')
SERVICE_ACCOUNT_JSON     = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '{}')

groq_client = Groq(api_key=GROQ_API_KEY)
TW_TZ = timezone(timedelta(hours=8))

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def get_sheet():
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1


def get_logical_date() -> str:
    now = datetime.now(TW_TZ)
    if now.hour < 6:
        now -= timedelta(days=1)
    return now.strftime('%Y-%m-%d')


def get_today_tasks() -> list:
    today = get_logical_date()
    sheet = get_sheet()
    rows = sheet.get_all_records()
    tasks = []
    for i, row in enumerate(rows, start=2):
        if str(row.get('日期', '')) == today and str(row.get('完成', '')).upper() != 'TRUE':
            tasks.append({'row': i, 'name': str(row.get('任務名稱', ''))})
    return tasks


def get_tasks_for_date(date: str) -> dict:
    sheet = get_sheet()
    rows = sheet.get_all_records()
    done, not_done = [], []
    for row in rows:
        if str(row.get('日期', '')) == date:
            name = str(row.get('任務名稱', ''))
            if str(row.get('完成', '')).upper() == 'TRUE':
                done.append(name)
            else:
                not_done.append(name)
    return {'done': done, 'not_done': not_done}


def mark_task_done(row_index: int) -> bool:
    try:
        sheet = get_sheet()
        sheet.update_cell(row_index, 3, 'TRUE')
        return True
    except Exception:
        return False


def add_task(name: str, date: str = None) -> bool:
    try:
        sheet = get_sheet()
        task_date = date or get_logical_date()
        sheet.append_row([task_date, name, 'FALSE'])
        return True
    except Exception:
        return False


def ask_groq(user_message: str, tasks: list) -> dict:
    today = get_logical_date()
    if tasks:
        task_list_str = '\n'.join([f'- [ROW:{t["row"]}] {t["name"]}' for t in tasks])
    else:
        task_list_str = '（今天沒有未完成的任務）'

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
        model='llama-3.1-8b-instant',
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


def reply_to_line(reply_token: str, message: str):
    httpx.post(
        'https://api.line.me/v2/bot/message/reply',
        headers={
            'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        },
        json={'replyToken': reply_token, 'messages': [{'type': 'text', 'text': message}]},
        timeout=10,
    )


def push_to_line(user_id: str, message: str):
    httpx.post(
        'https://api.line.me/v2/bot/message/push',
        headers={
            'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        },
        json={'to': user_id, 'messages': [{'type': 'text', 'text': message}]},
        timeout=10,
    )


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

        if user_message == '/myid':
            uid = event.get('source', {}).get('userId', '找不到')
            reply_to_line(reply_token, f'你的 LINE User ID 是：\n{uid}')
            continue

        try:
            tasks  = get_today_tasks()
            result = ask_groq(user_message, tasks)
            action = result.get('action')
            reply_text = result.get('reply', '收到！')

            if action == 'mark_done':
                row = result.get('row')
                if row and not mark_task_done(int(row)):
                    reply_text = '標記失敗，請稍後再試 🙏'
            elif action == 'add_task':
                task_name = result.get('task_name', '')
                if task_name and not add_task(task_name, result.get('date')):
                    reply_text = '新增失敗，請稍後再試 🙏'
        except Exception as e:
            reply_text = f'發生錯誤，請稍後再試 🙏'

        reply_to_line(reply_token, reply_text)

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)
    if not LINE_USER_ID:
        return jsonify({'error': 'LINE_USER_ID not set'}), 400

    tasks = get_today_tasks()
    today = get_logical_date()

    if not tasks:
        message = f'早安！☀️\n{today} 今天沒有待辦事項，有需要新增嗎？'
    else:
        task_lines = '\n'.join([f'• {t["name"]}' for t in tasks])
        message = f'早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{task_lines}\n\n有需要調整的嗎？'

    push_to_line(LINE_USER_ID, message)
    return jsonify({'status': 'ok', 'tasks_count': len(tasks)})


@app.route('/api/night', methods=['GET'])
def night_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)
    if not LINE_USER_ID:
        return jsonify({'error': 'LINE_USER_ID not set'}), 400

    today    = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    today_tasks = get_tasks_for_date(today)
    done_count  = len(today_tasks['done'])
    total_count = done_count + len(today_tasks['not_done'])

    if total_count > 0:
        pct = int(done_count / total_count * 100)
        today_line = f'今日完成率：{done_count}/{total_count}（{pct}%）'
        if today_tasks['not_done']:
            undone = '\n'.join([f'  ❌ {t}' for t in today_tasks['not_done']])
            today_line += f'\n未完成：\n{undone}'
    else:
        today_line = '今日沒有任務紀錄'

    tomorrow_tasks = get_tasks_for_date(tomorrow)
    if tomorrow_tasks['not_done']:
        lines = '\n'.join([f'• {t}' for t in tomorrow_tasks['not_done']])
        tomorrow_section = f'明日預覽（{len(tomorrow_tasks["not_done"])} 件）：\n{lines}'
    else:
        tomorrow_section = '明天還沒有任務規劃，記得安排一下！'

    message = f'晚安！🌙 今天辛苦了。\n\n{today_line}\n\n{tomorrow_section}'
    push_to_line(LINE_USER_ID, message)
    return jsonify({'status': 'ok'})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'LINE AI Assistant is running!'})
