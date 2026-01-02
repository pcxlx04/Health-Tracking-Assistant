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
            current_state TEXT,
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
        
        gender_str = gender if gender else "女"
        s = 5 if "男" in gender_str else -161
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
            s_json = json.loads(row[0])
            
            if category == "飲食":
                calories = s_json.get('calories', 0)
                try:
                    current_calories_sum += float(calories)
                except (ValueError, TypeError):
                    pass
                
            history_list.append(s_json)
        except Exception as e:
            print(f"解析歷史紀錄出錯: {e}")

    return current_calories_sum, f"今日歷史明細：{json.dumps(history_list, ensure_ascii=False)}"

def get_weekly_logs(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        SELECT category, structured_data, timestamp FROM health_logs 
        WHERE user_id = ? AND timestamp >= ?
        ORDER BY timestamp ASC
    ''', (user_id, seven_days_ago))
    
    rows = cursor.fetchall()
    conn.close()
    
    summary = {"飲食": [], "睡眠": [], "慢性病": []}
    for row in rows:
        category, data_str, time = row[0], row[1], row[2]
        try:
            summary[category].append({
                "時間": time,
                "數據": json.loads(data_str)
            })
        except:
            continue
            
    return summary

# RAG 知識檢索
def get_rag_context(user_text, category=None):
    base_path = os.path.dirname(os.path.abspath(__file__))

    category_map = {
        "飲食": "diet_ref.json",
        "睡眠": "sleep_ref.json",
        "慢性病": "chronic_ref.json"
    }
    
    selected_file = category_map.get(category)

    if not selected_file:
        keyword_map = {
            "diet_ref.json": ["飲食", "吃", "喝", "餐", "熱量", "飯", "麵"],
            "sleep_ref.json": ["睡眠", "睡", "醒", "品質", "累", "夢"],
            "chronic_ref.json": ["血壓", "血糖", "慢性病", "測量", "指數"]
        }
        for filename, keywords in keyword_map.items():
            if any(word in user_text for word in keywords):
                selected_file = filename
                break
            
    if not selected_file:
        print(f"--- RAG 系統：未匹配到類別 [{category}]，未搜索知識庫 ---")
        return ""

    file_path = os.path.join(base_path, "rag_reference", selected_file)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            knowledge = json.load(f)
            print(f"--- RAG 系統：成功根據類別 [{category}] 載入 {selected_file} ---")
            return f"參考之醫學指南標準：{json.dumps(knowledge, ensure_ascii=False)}"
    except Exception as e:
        print(f"RAG 讀取失敗: {e}")
        return ""

# 基於 LLM 的自然語言處理 (NLP) 與意圖識別
def smart_ai_parser(user_input, user_id, fixed_category=None):
    # 分類判定
    category = fixed_category
    if not category:
        if any(k in user_input for k in ["飲食", "吃", "餐", "喝"]): category = "飲食"
        elif any(k in user_input for k in ["睡眠", "睡"]): category = "睡眠"
        elif any(k in user_input for k in ["血壓", "血糖", "慢性病"]): category = "慢性病"
        else: category = "未知"

    rag_knowledge = get_rag_context(user_input, category)

    # 背景數據
    current_sum, today_history = get_today_stats(user_id, category)
    user_profile_context = get_user_profile(user_id)
    record_time = datetime.now().strftime('%Y-%m-%d %H:%M')

    # 定義 Advice 模板
    specific_logic_prompt = ""
    specific_advice_template = ""
    specific_json_format = ""
    
    if category == "睡眠":
        specific_advice_template = f"""
       【睡眠分析報告】
        睡眠時數：[時數] 小時
        品質評估：[品質] [🟢/🟡/🔴] 
        達標判定：對照您 [年齡] 歲標準，此時數 [充足/不足/過量]。
        
       【專家分析】
        ● [結構提示]：(若用戶太晚睡或早醒，請務必引用 RAG 中的 N3 修復或 REM 記憶整合邏輯說明)。
        ● [風險提醒]：(若用戶提到打呼、酒精或咖啡因，請引用 knowledge 中的警示與 analysis_hint)。
        
       【行動建議】
        1. [建議 1：環境改善，如溫度、光線]
        2. [建議 2：行為調整，如睡前儀式、咖啡因限制]
        """

        specific_json_format = """{
            "detected_metrics": {
                "hours": "睡眠總時數 (純數字)",
                "sleep_latency_min": "入睡耗時 (對比 10-20min 標準)",
                "waso_min": "醒後覺醒時間 (對比 < 20min 標準)",
                "efficiency_score": "睡眠效率百分比"
            },
            "quality_assessment": {
                "level": "良好/ 普通/ 極差",
                "primary_dimension": "受影響的主要維度 (Restoration / Emotional_Stability / Continuity)"
            },
            "feature_detection": {
                "snoring_osa_risk": "描述偵測到的症狀 (例如: 打呼且口乾) 或 null",
                "caffeine_impact": "偵測到的攝取行為與潛在影響描述 或 null",
                "alcohol_rebound": "偵測到的飲酒行為與反彈效應風險 或 null",
                "dreaming_stage": "描述 (Vivid / Vague / No_Dream) 並連結 REM 狀態"
            },
        }"""

    elif category == "飲食":
        specific_logic_prompt = f"""
       【飲食分析與法律】
        1. 熱量判定優先級：
            - 請先查閱知識庫 `calorie_estimation_reference` 中的 `common_items`。
            - **若名稱匹配**：必須強制使用該數值作為 `calories`，不得自行更改。
            - **若名稱未匹配**：由你根據內部醫學知識推估合理熱量。
        2. 營養素分析：
            - 透過你的內部知識，針對該食物拆解並估算：蛋白質(g)、碳水(g)、脂肪(g) 與 鈉(mg)。
        3. 統計法律：
            - 目前今日已累計：{current_sum} kcal。
            - 必須計算「新總計 = {current_sum} + 本次食物熱量」。
        """

        specific_advice_template = """
        【 飲食分析報告】
        熱量推估：[食物名稱] = [本次數值]kcal
        今日統計：總累計(含本次) [今日總計]/ 每日建議攝取總熱量 [建議總量] kcal
        ━━━━━━━━━━
        營養分析：
        ● 蛋白質估算：[克數]g / 碳水：[克數]g / 脂肪：[克數]g
        ● 鈉含量估算：[毫克]mg
        ● 代謝建議：[分別分析今日佔比，並告知熱量、蛋白質、鈉含量剩餘配額建議]。
        """

        specific_json_format = """{
            "items": "本次錄入的所有食物名稱，以、區隔",
            "calories": 本次錄入的熱量總和(純數字),
            "macros": {
                "carbs_g": "碳水估算(克)",
                "protein_g": "蛋白質估算(克)",
                "fat_g": "脂肪估算(克)"
            },
            "sodium_mg": "鈉含量估算(毫克)",
            "total_calories": "今日熱量加總"
        }"""

    elif category == "慢性病":
        specific_logic_prompt = """
        【慢性病處理演算法：嚴格執行路徑】

        STEP 1. 指標提取與隔離 (Metric Extraction)
        - 若用戶「未提及」某項指標：
            * value = "未紀錄", emoji = "⚪", status = "-", is_alert = false。
            * **禁止** 受其他異常指標影響而變色。

        STEP 2. BMI 計算邏輯 (BMI回溯法律)
        - 優先級：[本次輸入體重] > [用戶背景存檔體重]。
        - 只要「有體重」且「有存檔身高」：必須計算 BMI = kg / (m^2)。
        - 輸出規範：若使用存檔資料，status 必須加註「(存檔資料)」。

        STEP 3. dash_section 內容填充邏輯 (核心防線)
        - **CASE A [全綠標 🟢]**：若血壓、血糖、心率 全正常，填寫「🎯 行動計畫：繼續保持優良生活習慣！」。
        - **CASE B [DASH 觸發標紅 🟠/🔴]**：
            * 條件：僅當「血壓」或「血糖」任一項為 🟠 或 🔴 時，才可顯示。
            * 執行：從 chronic_ref.json 中「字面引用」對應等級的 `dash_diet` 建議與 `action_plans`。
            * 格式必須固定如下：
              🥗 DASH 飲食建議
              每日鈉攝取：{sodium}
              建議食物：{foods_eat}
              避免食物：{foods_avoid}
              範例菜單：{sample_menu}
          
              🎯 行動計畫
              立即：{immediate}
              本週：{weekly}
              每月：{monthly}
        - **CASE C [僅 BMI/心率異常]**：
            * 條件：血壓血糖正常，但 BMI 🟡/🔴 或心率 🟡/🔴。
            * 執行：**禁顯 DASH**，僅填寫 `action_plans` 中的「立即、本週、每月」內容。

        STEP 4. 代謝症候群判定 (邏輯閘)
        - 必須同時滿足：[血壓非🟢] 且 [血糖非🟢] 且 [BMI非🟢]。
        - 若三者缺一不可：metabolic_alert = "⚠️ 符合代謝症候群指標,心血管疾病風險大幅提升"。
        - 其餘任何情況（含資料不足）：metabolic_alert = "" (空字串)。

        【⚠️ 鋼鐵禁令】嚴禁編造知識庫中不存在的內容。若找不到對應內容，請填寫「請持續觀察並定期測量」。
        """

        specific_json_format = """{
            "blood_pressure": {"value": "收縮壓/舒張壓 或 未紀錄", "status": "分級", "emoji": "🟢/🟠/🔴/⚪", "is_alert": bool},
            "heart_rate": {"value": "數值 或 未紀錄", "status": "狀態", "emoji": "🟢/🟠/🔴/⚪", "is_alert": bool},
            "blood_sugar": {"value": "數值(狀態) 或 未紀錄", "status": "狀態", "emoji": "🟢/🟠/🔴/⚪", "is_alert": bool},
            "BMI": {"value": "數值 或 未紀錄", "status": "狀態", "emoji": "🟢/🟠/🔴/⚪", "is_alert": bool},
            "dash_section": "這裡存放根據邏輯拼好的字串",
            "metabolic_alert": "警示文字 或 無"
        }"""

        specific_advice_template = """
        【紀錄日期】 {record_time}
        📊 檢測結果
        {bp_emoji} 血壓：{bp_value} → {bp_status}
        {hr_emoji} 心率：{hr_value} → {hr_status}
        {bs_emoji} 血糖：{bs_value} → {bs_status}
        {bmi_emoji} BMI：{bmi_value} → {bmi_status}
        ━━━━━━━━━━━━━━
        {dash_section}

        {metabolic_alert_text}
        """
    
    # 組裝 System Prompt
    system_prompt = f"""
    你是一個整合了 RAG 系統並具備長期數據連貫性的專業健康管家。
    請針對【{category}】類別進行分析並輸出 JSON。

    【最高法律：RAG 與數據對齊】
    1. 絕對禁止記憶干擾：判定必須 100% 引用『知識庫內容』。
    2. 數據鎖定：必須直接從『用戶基礎背景』讀取「系統鎖定基準值」。
    3. 術語在地化：TDEE 改稱為：『每日建議攝取總熱量』，BMR 改稱為：『基礎代謝率』。
    4. 時間感知：現在是 {record_time}。
    5. 統計邏輯：
       - 「本次紀錄」：僅計算當下輸入的食物熱量。
       - 「今日統計」：必須將『用戶今日已紀錄歷史』中的熱量與「本次紀錄」相加。

    {specific_logic_prompt}

    【背景數據】
    - 知識庫：{rag_knowledge}
    - 今日歷史：{today_history}
    - 用戶背景：{user_profile_context}
    
    任務與輸出格式規範：
    1. 若意圖為 'update_profile'：必須輸出 JSON，且 Key 必須為 "intent", "height", "weight", "age", "gender" (數值為數字，性別為字串)。
    
    2. 若意圖為 'health_record'：
       - 輸出鍵 'intent', 'category', 'structured_json', 'advice'。
       - 【必要】'advice' 以『【紀錄日期】 {record_time}』開頭並嚴格套用：{specific_advice_template}
       - 【必要】'category' 欄位必須固定填入："{category}" (嚴禁更動名稱，確保資料庫對齊)。
       - 【必要】'structured_json' 的內容必須嚴格遵守此結構：{specific_json_format}，不得自行增減鍵值。

    一律用繁體字，嚴禁使用簡體字。
    400 字以內，禁止贅字。
    結尾空兩行加上官方免責聲明：『⚠️ 以上內容僅供參考，不構成醫療診斷。』
    """
    
    try:
        response = client.chat.completions.create(
            model="ft:gpt-4o-mini-2024-07-18:meetsure:health-assistant-v1:CtFWX1LW",
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


# 整理數據與生成週報
def generate_weekly_report(user_id):
    weekly_data = get_weekly_logs(user_id)
    user_profile = get_user_profile(user_id)
    
    if not any(weekly_data.values()):
        return "📊 您本週尚無任何健康紀錄喔！"

    stats = {
        "飲食": {"總熱量": 0, "平均熱量": 0, "天數": 0},
        "睡眠": {"總時數": 0, "平均時數": 0, "天數": 0},
        "慢性病": {
            "筆數": 0,
            "血壓紀錄": [],
            "心率紀錄": [],
            "血糖紀錄": [],
            "異常警告數": 0
        }
    }

    # 1. 解析飲食數據
    diet_days = set(log["時間"].split(' ')[0] for log in weekly_data["飲食"])
    stats["飲食"]["天數"] = len(diet_days)
    if stats["飲食"]["天數"] > 0:
        total_cal = sum(log["數據"].get("calories", 0) for log in weekly_data["飲食"])
        stats["飲食"]["總熱量"] = total_cal
        stats["飲食"]["平均熱量"] = round(total_cal / stats["飲食"]["天數"], 1)

    # 2. 解析睡眠數據
    sleep_days = set(log["時間"].split(' ')[0] for log in weekly_data["睡眠"])
    stats["睡眠"]["天數"] = len(sleep_days)
    if stats["睡眠"]["天數"] > 0:
        total_sleep = sum(log["數據"].get("hours", 0) for log in weekly_data["睡眠"])
        stats["睡眠"]["平均時數"] = round(total_sleep / stats["睡眠"]["天數"], 1)

    # 3. 解析慢性病數據
    for log in weekly_data["慢性病"]:
        items = log["數據"]
        if isinstance(items, list):
            stats["慢性病"]["筆數"] += 1
            for item in items:
                v_type = item.get("type")
                v_val = item.get("value")
                is_alert = item.get("is_alert", False)
                
                if is_alert:
                    stats["慢性病"]["異常警告數"] += 1
                
                if "血壓" in v_type:
                    stats["慢性病"]["血壓紀錄"].append(v_val)
                elif "心率" in v_type:
                    stats["慢性病"]["心率紀錄"].append(v_val)
                elif "血糖" in v_type:
                    stats["慢性病"]["血糖紀錄"].append(v_val)

    chronic_summary = (
        f"- 總測量筆數：{stats['慢性病']['筆數']} 筆\n"
        f"- 異常警告次數：{stats['慢性病']['異常警告數']} 次\n"
        f"- 本週血壓軌跡：{', '.join(stats['慢性病']['血壓紀錄']) if stats['慢性病']['血壓紀錄'] else '無'}\n"
        f"- 本週血糖軌跡：{', '.join(stats['慢性病']['血糖紀錄']) if stats['慢性病']['血糖紀錄'] else '無'}"
    )

    system_prompt = f"""
    你是一位專業的健康顧問。請根據以下【精確統計數據】為用戶撰寫週報。
    
    【用戶生理背景】
    {user_profile}

    【本週精確統計 (由系統計算，請直接引用)】
    - 飲食：總攝取 {stats['飲食']['總熱量']} kcal，實際紀錄 {stats['飲食']['天數']} 天，平均每日 {stats['飲食']['平均熱量']} kcal。
    - 睡眠：實際紀錄 {stats['睡眠']['天數']} 天，平均每日睡 {stats['睡眠']['平均時數']} 小時。
    - 慢性病趨勢： {chronic_summary}
    
    【詳細紀錄明細】
    {json.dumps(weekly_data, ensure_ascii=False)}

    【數值比較絕對準則】
    1. 判定能量攝取狀態：
       - 若 平均攝取 < TDEE：必須判定為「達標」或「低於建議量」，並給予正面鼓勵。
       - 若 平均攝取 > TDEE：才可判定為「過高」或「需調整」。
       - 絕對禁止將小於 TDEE 的數值描述為「略高」。
    2. 數值敏感度：1448.4 小於 1481，這在數學上是「減少」而非「增加」。

    撰寫要求：
    1. 嚴禁自行重新計算平均值，必須直接引用上方提供的【精確統計數據】。
    2. 嚴禁 Markdown 語法，改用實心圓點、方括號或分隔線。
    3. 分析重點：
       - 飲食：對照 TDEE 評價 {stats['飲食']['平均熱量']} kcal 是過高或過低。
       - 睡眠：分析時數是否穩定。
       - 慢性病：必須針對數值的軌跡進行點評（例如：您的血壓有上升趨勢，請注意）。
    4. 內容結構：
       [健康分析週報]
       ━━━━━━━━━━
      【飲食與營養 🍽️】
      【睡眠品質 💤】
      【慢性病追蹤 🩺】
      【綜合生活洞察 🧠】- 分析「睡眠、飲食、生理指標」三者間的交互影響。
       ━━━━━━━━━━
       ● 下週行動建議 📝
       1. ...
       2. ...
    5. 150-200 字內，繁體中文，保持簡潔。
    6. 結尾：⚠️ 以上內容僅供參考，不構成醫療診斷。
    """

    try:
        response = client.chat.completions.create(
            model="ft:gpt-4o-mini-2024-07-18:meetsure:health-assistant-v1:CtFWX1LW",
            messages=[{"role": "system", "content": system_prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"週報生成失敗: {e}")
        return "系統繁忙，週報生成失敗，請稍後再試。"

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

    reply = "抱歉，我無法分析這筆紀錄。請試著點選功能選單，並依照提示輸入喔！"
    
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
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES (?)", (user_id,))
        cursor.execute("UPDATE user_profiles SET current_state = ? WHERE user_id = ?", (category_name, user_id))
        conn.commit()
        conn.close()

        prompts = {
            "睡眠": (
            "已進入【睡眠紀錄】模式。\n\n"
            "請描述您昨晚的入睡/起床時間與品質（例如：昨晚12點躺下，大概30分鐘入睡，早上8點醒，精神很好）。\n\n"
            "💡 也可以輸入是否有打呼、攝取咖啡因、飲酒或做夢，這能幫助我更精準地分析您的睡眠品質喔！"
            ),
            "飲食": "已進入【飲食紀錄】模式。\n\n請描述您吃了什麼（例如：午餐吃了一個漢堡和一杯珍奶）。",
            "慢性病": (
                    "已進入【慢性病紀錄】模式。\n\n請提供測量數據，可包含血壓、心率或血糖，例如：\n"
                    "「血壓 135/85，心率 72，血糖 110 (飯後)。」\n\n"
                    "💡 若體重有變化也可以順便告訴我喔！"
                    )
        }
        reply = prompts.get(category_name, "請輸入您的健康日誌：")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    
    if user_text == "查看健康報告":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📊 正在彙整您過去 7 天的健康數據，請稍候..."))
        
        report = generate_weekly_report(user_id)
        
        line_bot_api.push_message(user_id, TextSendMessage(text=report))
        return


    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT current_state FROM user_profiles WHERE user_id = ?", (user_id,))
    state_row = cursor.fetchone()
    conn.close()
    
    pending_category = state_row[0] if (state_row and state_row[0]) else None

    # 呼叫 RAG Parser
    result = smart_ai_parser(user_text, user_id, fixed_category=pending_category)
    
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

        clean_structured_data = json.dumps(result.get('structured_json'), ensure_ascii=False)

        cursor.execute('''
            INSERT INTO health_logs (user_id, timestamp, raw_text, category, structured_data, ai_advice)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, 
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
              user_text, 
              result.get('category'), 
              clean_structured_data,
              result.get('advice')))
        
        cursor.execute("UPDATE user_profiles SET current_state = NULL WHERE user_id = ?", (user_id,))
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