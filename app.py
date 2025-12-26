import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    QuickReply, QuickReplyButton, MessageAction
)
from openai import OpenAI
from dotenv import load_dotenv

# 環境設定 & 金鑰
load_dotenv(override=True)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_API_KEY)

DB_NAME = 'health_assistant.db'

# Database
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS health_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            timestamp DATETIME,
            category TEXT,
            raw_text TEXT,
            structured_data TEXT,
            ai_advice TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            age INTEGER,
            height REAL,
            weight REAL,
            gender TEXT,
            updated_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def get_user_profile(user_id):
    """取得生理指標並在 Python 端預算 BMR/TDEE"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT age, height, weight, gender FROM user_profiles WHERE user_id = ?', (user_id,))
    profile = cursor.fetchone()
    conn.close()

    if profile:
        age, height, weight, gender = profile[0], profile[1], profile[2], profile[3]
        
        s = 5 if "男" in gender else -161
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + s
        tdee = bmr * 1.2
        
        return (f"用戶背景：{gender}性、{age}歲、{height}cm、{weight}kg。 "
                f"系統鎖定數值：BMR 為 {bmr:.0f} kcal，TDEE 為 {tdee:.0f} kcal。")
    return "用戶尚未建立個人生理指標資料。"

def save_user_profile(user_id, data):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_profiles (user_id, age, height, weight, gender, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            age=excluded.age, height=excluded.height, weight=excluded.weight, 
            gender=excluded.gender, updated_at=excluded.updated_at
    ''', (user_id, data.get('age'), data.get('height'), data.get('weight'), data.get('gender'), datetime.now()))
    conn.commit()
    conn.close()

init_db()

def get_today_stats(user_id, category):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    cursor.execute('''
        SELECT structured_data FROM health_logs 
        WHERE user_id = ? AND category = ? AND timestamp LIKE ?
    ''', (user_id, category, f"{today_str}%"))
    
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0, "今日尚無紀錄。"
    
    current_calories_sum = 0
    history_list = []
    
    for row in rows:
        try:
            log_data = json.loads(row[0])
            s_json = log_data.get('structured_json', {})
            
            if category == "飲食":
                current_calories_sum += s_json.get('calories', 0)
                
            history_list.append(s_json)
        except Exception as e:
            print(f"解析歷史紀錄出錯: {e}")

    return current_calories_sum, f"今日歷史明細：{json.dumps(history_list, ensure_ascii=False)}"


# RAG 知識檢索
def get_rag_context(user_text):
    base_path = os.path.dirname(os.path.abspath(__file__))
    
    keyword_map = {
        "diet_ref.json": ["飲食", "吃", "喝", "餐", "熱量", "飯", "麵"],
        "sleep_ref.json": ["睡眠", "睡", "醒", "品質", "累", "夢"],
        "chronic_ref.json": ["血壓", "血糖", "慢性病", "測量", "指數"]
    }
    
    selected_file = None
    for filename, keywords in keyword_map.items():
        if any(word in user_text for word in keywords):
            selected_file = filename
            break
            
    if not selected_file:
        print("--- RAG 系統：未匹配到關鍵字，未搜索知識庫 ---")
        return ""

    file_path = os.path.join(base_path, "rag_reference", selected_file)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            knowledge = json.load(f)
            print(f"--- RAG 系統：成功載入 {selected_file} ---")
            return f"參考之醫學指南標準：{json.dumps(knowledge, ensure_ascii=False)}"
    except Exception as e:
        print(f"RAG 讀取失敗: {e}")
        return ""


# AI prompt
def smart_ai_parser(user_input, user_id):
    category = "未知"
    if any(k in user_input for k in ["飲食", "吃", "餐", "喝"]): category = "飲食"
    elif any(k in user_input for k in ["睡眠", "睡"]): category = "睡眠"
    elif any(k in user_input for k in ["血壓", "血糖", "慢性病"]): category = "慢性病"

    current_sum, today_history = get_today_stats(user_id, category)

    rag_knowledge = get_rag_context(user_input)
    user_profile_context = get_user_profile(user_id)
    record_time = datetime.now().strftime('%Y-%m-%d %H:%M')

    diet_logic_prompt = ""
    if category == "飲食":
        diet_logic_prompt = f"""
        【飲食統計法律】
        - 系統已幫你算好，在你這筆紀錄之前，用戶今日已累計攝取：{current_sum} kcal。
        - 你的任務：計算「新總計 = {current_sum} + 本次食物熱量」。
        - 警告：禁止自行去重！即便本次輸入的食物與歷史明細重複，也必須視為新的一餐並累加熱量。
        """

    system_prompt = f"""
    你是一個整合了 RAG 系統並具備長期數據連貫性的專業健康管家。請分析輸入並輸出 JSON。

    【最高法律：RAG 與數據對齊】
    1. 絕對禁止記憶干擾：所有健康判定（如：睡眠建議、熱量估算、血壓分級）必須 100% 引用『知識庫內容』。
    2. 數據鎖定：嚴禁自行計算 BMR/TDEE。必須直接從『用戶基礎背景』讀取「系統鎖定基準值」。
    3. 術語在地化：TDEE 改稱為：『每日建議攝取總熱量』，BMR 改稱為：『基礎代謝率』
    4. 時間感知：現在是 {record_time}，請根據當前紀錄與今日歷史進行分析。
    5. 統計邏輯：
       - 「本次紀錄」：僅計算當下輸入的食物熱量。
       - 「今日統計」：必須將『用戶今日已紀錄歷史』中的熱量與「本次紀錄」相加得出總和。

    {diet_logic_prompt}

    【知識庫內容】
    {rag_knowledge}

    【用戶今日已紀錄歷史】
    {today_history}
    
    【用戶基礎背景】
    {user_profile_context}
    
    任務與輸出格式規範：
    1. 若意圖為 'update_profile'：輸出鍵 'intent', 'height', 'weight', 'age', 'gender'。
    
    2. 若意圖為 'health_record'：
       - 輸出鍵 'intent', 'category', 'structured_json', 'advice'。
       - 'advice' 模板（嚴格遵守，禁止開場白，使用 \\n 換行）：

       【紀錄日期】 {record_time}
       
       【 睡眠分析報告】
        ━━━━━━━━━━━━━━
        睡眠時數：[時數] 小時
        品質評估：[品質] [🟢/🟡/🔴] [達標判定]：對照您 [年齡] 歲標準，此時數 [充足/不足/過量]。
        ──────────────────
        專家分析：
        ● [結構提示]：(若用戶太晚睡或早醒，請務必引用 RAG 中的 N3 修復或 REM 記憶整合邏輯說明)。
        ● [風險提醒]：(若用戶提到打呼、酒精或咖啡因，請引用 knowledge 中的警示與 analysis_hint)。
        行動建議：
        1. [建議 1：環境改善，如溫度、光線]
        2. [建議 2：行為調整，如睡前儀式、咖啡因限制]

       2. 若為『飲食』：
          熱量推估：[食物名稱] = [本次數值]kcal
          今日統計：總累計(含本次) [今日總計]/ 每日建議攝取總熱量 [建議總量] kcal
          代謝建議：[分析佔比並告知剩餘配額建議]。

       3. 若為『慢性病』：
          測量狀態：[數值] -> [風險分級]
          判定標準：(直接引用 RAG 知識庫中的數值區間進行說明)
          行動指南：(具體的行動指引)

    字數限制：120 字以內，禁止贅字。
    格式要求：結尾空兩行加上官方免責聲明：『⚠️ 以上內容僅供參考，不構成醫療診斷。』
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            response_format={"type": "json_object"}
        )
        ai_res = json.loads(response.choices[0].message.content)
        print(f"--- 回傳 JSON 檢查 ---")
        print(json.dumps(ai_res, indent=2, ensure_ascii=False))
        return ai_res
    except Exception as e:
        print(f"AI 擷取錯誤: {e}")
        return None


# LINE Webhook & 訊息處理
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    
    if user_text == "更新個人檔案":
        reply = (
            "【個人身體基準：為什麼這很重要？】\n\n"
              "為了提供更精準的科學建議，系統建議您提供您的基礎生理指標，這些資料將用於以下分析：\n\n"
                "🛌 睡眠：年齡是判斷睡眠結構與所需時數的關鍵變數。\n\n" 
                "🥗 飲食：身高與體重可用來估算基礎代謝率（BMR），作為熱量與營養建議的依據。\n\n" 
                "🩺 慢性病：基本生理特徵能幫助系統更準確辨識異常狀況，降低個體差異造成的誤判。\n\n"
              "請輸入您的「身高、體重、年齡、性別」\n" "（範例：165公分、50公斤、25歲、女）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if user_text == "我要紀錄":
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="睡眠追蹤", text="【紀錄】睡眠")),
            QuickReplyButton(action=MessageAction(label="飲食與營養", text="【紀錄】飲食")),
            QuickReplyButton(action=MessageAction(label="慢性病紀錄", text="【紀錄】慢性病"))
        ])
        line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(text="請選擇紀錄類別：", quick_reply=quick_reply)
        )
        return
    
    if user_text.startswith("【紀錄】"):
        category_name = user_text.replace("【紀錄】", "")
        prompts = {
            "睡眠": "已進入【睡眠紀錄】模式。\n\n請描述您昨晚的入睡/起床時間與品質（例如：昨晚12點睡，早上8點醒，精神很好）。",
            "飲食": "已進入【飲食紀錄】模式。\n\n請描述您吃了什麼（例如：午餐吃了一個漢堡和一杯珍奶）。",
            "慢性病": "已進入【慢性病紀錄】模式。\n\n請提供測量數據（例如：血壓 135/85，心率 75）。"
        }
        reply = prompts.get(category_name, "請輸入您的健康日誌：")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 呼叫 RAG Parser
    result = smart_ai_parser(user_text, user_id)
    
    if not result:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統繁忙，請稍後再試。"))
        return

    if result.get('intent') == 'update_profile':
        save_user_profile(user_id, result)
        reply = (
            f"✅ 檔案已更新：\n"
            f"身高：{result.get('height')}cm\n"
            f"體重：{result.get('weight')}kg\n"
            f"年齡：{result.get('age')}歲\n"
            f"性別：{result.get('gender')}"
        )
    
    elif result.get('intent') == 'health_record':
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        structured_json = json.dumps(result, ensure_ascii=False)

        cursor.execute('''
            INSERT INTO health_logs (user_id, timestamp, raw_text, category, structured_data, ai_advice)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, 
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
              user_text, 
              result.get('category'), 
              structured_json, 
              result.get('advice')))
        conn.commit()
        conn.close()

        reply = (
            f"{result.get('category', '紀錄')} 紀錄成功！\n"
            f"━━━━━━━━━━━━━━\n"
            f"{result.get('advice')}"
        )
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(port=5000)