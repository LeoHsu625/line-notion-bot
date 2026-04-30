import os
import json
import hashlib
import hmac
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
import httpx
import anthropic

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '')
NOTION_DATABASE_ID = os.environ.get('NOTION_DATABASE_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
LINE_USER_ID = os.environ.get('LINE_USER_ID', '')
CRON_SECRET = os.environ.get('CRON_SECRET', '')

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
TW_TZ = timezone(timedelta(hours=8))


def get_logical_date() -> str:
    """凌晨 6 點前仍視為前一天"""
    now = datetime.now(TW_TZ)
    if now.hour < 6:
        now = now - timedelta(days=1)
    return now.strftime('%Y-%m-%d')


def verify_line_signature(body: bytes, signature: str) -> bool:
    h = hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'), body, hashlib.sha256).digest()
    expected = base64.b64encode(h).decode('utf-8')
    return hmac.compare_digest(expected, signature)


def get_today_tasks() -> list:
    today = get_logical_date()
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "filter": {
            "and": [
                {"property": "Date", "date": {"equals": today}},
                {"property": "Status", "checkbox": {"equals": False}},
            ]
        }
    }
    resp = httpx.post(url, headers=headers, json=payload, timeout=10)
    tasks = []
    for result in resp.json().get('results', []):
        title = result['properties']['Name']['title']
        if title:
            tasks.append({"id": result['id'], "name": title[0]['plain_text']})
    return tasks


def get_tasks_for_date(date: str) -> dict:
    """回傳指定日期的所有任務，分為 done / not_done 兩組"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {"filter": {"property": "Date", "date": {"equals": date}}}
    resp = httpx.post(url, headers=headers, json=payload, timeout=10)
    done, not_done = [], []
    for result in resp.json().get('results', []):
        title = result['properties']['Name']['title']
        if not title:
            continue
        name = title[0]['plain_text']
        if result['properties']['Status']['checkbox']:
            done.append(name)
        else:
            not_done.append(name)
    return {'done': done, 'not_done': not_done}


def mark_task_done(task_id: str) -> bool:
    url = f"https://api.notion.com/v1/pages/{task_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    resp = httpx.patch(url, headers=headers, json={"properties": {"Status": {"checkbox": True}}}, timeout=10)
    return resp.status_code == 200


def add_task(name: str, date: str = None) -> bool:
    task_date = date or get_logical_date()
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": name}}]},
            "Date": {"date": {"start": task_date}},
        },
    }
    resp = httpx.post(url, headers=headers, json=payload, timeout=10)
    return resp.status_code == 200


def ask_claude(user_message: str, tasks: list) -> dict:
    today = get_logical_date()
    if tasks:
        task_list_str = "\n".join([f"- [ID:{t['id']}] {t['name']}" for t in tasks])
    else:
        task_list_str = "（今天沒有未完成的任務）"

    system_prompt = f"""你是 Leo 的 LINE 任務助理，幫他管理 Notion 待辦清單。
今天日期：{today}

今日未完成任務：
{task_list_str}

根據用戶訊息判斷意圖，只回傳 JSON，格式如下：
- 查詢任務：{{"action":"query_tasks","reply":"整理好的任務清單"}}
- 標記完成：{{"action":"mark_done","task_id":"對應ID","reply":"確認完成的回覆"}}
- 新增任務：{{"action":"add_task","task_name":"任務名稱","date":null或"YYYY-MM-DD","reply":"確認新增的回覆"}}
- 其他：{{"action":"reply","reply":"友善回覆"}}

規則：
- 全部用繁體中文回覆
- reply 是傳給用戶看的訊息，要自然
- 標記完成時，從清單找最相符的任務 ID
- 找不到對應任務時，action 用 reply，告訴用戶找不到"""

    resp = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    text = resp.content[0].text.strip()
    if '{' in text:
        text = text[text.index('{'):text.rindex('}') + 1]
    return json.loads(text)


def reply_to_line(reply_token: str, message: str):
    httpx.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": message}]},
        timeout=10,
    )


def push_to_line(user_id: str, message: str):
    httpx.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": [{"type": "text", "text": message}]},
        timeout=10,
    )


@app.route('/api/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    if not verify_line_signature(body, signature):
        abort(400)

    for event in json.loads(body).get('events', []):
        if event.get('type') != 'message' or event['message'].get('type') != 'text':
            continue

        user_message = event['message']['text'].strip()
        reply_token = event['replyToken']

        # 特殊指令：取得自己的 User ID
        if user_message == '/myid':
            user_id = event.get('source', {}).get('userId', '找不到')
            reply_to_line(reply_token, f"你的 LINE User ID 是：\n{user_id}")
            continue

        try:
            tasks = get_today_tasks()
            result = ask_claude(user_message, tasks)
            action = result.get('action')
            reply_text = result.get('reply', '收到！')

            if action == 'mark_done':
                task_id = result.get('task_id', '')
                if task_id and not mark_task_done(task_id):
                    reply_text = '標記失敗，請稍後再試 🙏'

            elif action == 'add_task':
                task_name = result.get('task_name', '')
                if task_name and not add_task(task_name, result.get('date')):
                    reply_text = '新增失敗，請稍後再試 🙏'

        except Exception:
            reply_text = '發生錯誤，請稍後再試 🙏'

        reply_to_line(reply_token, reply_text)

    return jsonify({'status': 'ok'})


@app.route('/api/cron', methods=['GET'])
def cron():
    # 驗證是 Vercel 發出的排程請求
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    if not LINE_USER_ID:
        return jsonify({'error': 'LINE_USER_ID not set'}), 400

    tasks = get_today_tasks()
    today = get_logical_date()

    if not tasks:
        message = f"早安！☀️\n{today} 今天沒有待辦事項，有需要新增嗎？"
    else:
        task_lines = "\n".join([f"• {t['name']}" for t in tasks])
        message = f"早安！☀️ 今天有 {len(tasks)} 件待辦：\n\n{task_lines}\n\n有需要調整的嗎？"

    push_to_line(LINE_USER_ID, message)
    return jsonify({'status': 'ok', 'tasks_count': len(tasks)})


@app.route('/api/night', methods=['GET'])
def night_cron():
    auth = request.headers.get('Authorization', '')
    if CRON_SECRET and auth != f'Bearer {CRON_SECRET}':
        abort(401)

    if not LINE_USER_ID:
        return jsonify({'error': 'LINE_USER_ID not set'}), 400

    today = get_logical_date()
    tomorrow = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    today_tasks = get_tasks_for_date(today)
    done_count = len(today_tasks['done'])
    total_count = done_count + len(today_tasks['not_done'])

    if total_count > 0:
        pct = int(done_count / total_count * 100)
        today_line = f"今日完成率：{done_count}/{total_count}（{pct}%）"
        if today_tasks['not_done']:
            undone = "\n".join([f"  ❌ {t}" for t in today_tasks['not_done']])
            today_line += f"\n未完成：\n{undone}"
    else:
        today_line = "今日沒有任務紀錄（可能還沒跑 /morning）"

    tomorrow_tasks = get_tasks_for_date(tomorrow)
    if tomorrow_tasks['not_done']:
        lines = "\n".join([f"• {t}" for t in tomorrow_tasks['not_done']])
        tomorrow_section = f"明日預覽（{len(tomorrow_tasks['not_done'])} 件）：\n{lines}"
    else:
        tomorrow_section = "明天還沒有任務規劃，記得安排一下！"

    message = f"晚安！🌙 今天辛苦了。\n\n{today_line}\n\n{tomorrow_section}"
    push_to_line(LINE_USER_ID, message)
    return jsonify({'status': 'ok', 'today_total': total_count, 'today_done': done_count, 'tomorrow_count': len(tomorrow_tasks['not_done'])})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'Leo LINE Bot is running!'})
