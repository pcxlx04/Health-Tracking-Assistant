# Health Tracking Assistant

這是一個基於 **NLP** 與 **RAG** 技術的 **智慧健康助理 LineBot**。系統結合了個人生理指標計算、醫學知識庫檢索以及**微調（Fine-tuning)** 的大語言模型，為用戶提供精準的睡眠、飲食與慢性病追蹤建議。

## 核心功能 Core Features

**RAG 知識檢索**：根據用戶輸入自動檢索 JSON 指南，提供標準化的醫學建議（如血壓分級、睡眠時數標準）。
1. **睡眠管理 (Sleep)**：分析 N3 深層修復與 REM 記憶週期。系統會比對各年齡層標準，並針對咖啡因攝取或作息風險提供 AI 洞察，優化夜間修復效能。
2. **飲食與營養 (Diet & Nutrition)**：透過 Mifflin-St Jeor 公式計算基礎代謝率 (BMR) 與每日建議攝取總熱量 (TDEE)，並遵循 台灣衛福部 (MOHW) 建議之三大營養素比例、六大類食物分類法與新增「含糖飲料與甜點」類，即時加總當日攝取熱量並動態回報剩餘熱量配額，協助使用者在符合在地化營養標準的前提下，達成科學化的飲食監控與體重管理目標。
3. **慢性病追蹤 (Chronic Disease)**：分析血壓、血糖與 BMI 以早期偵測風險。 提供臨床風險分級與 DASH (得舒飲食) 指南，優化心血管健康。
4. **長期健康趨勢 (Analytical Reports)**：將零散的每日紀錄轉化為週報，識別長期健康規律與潛在風險預警。

## 系統運作流程 System Workflow

本專案採用 RAG (檢索增強生成) 與 Fine-tuning (模型微調) 的**混合式 AI 架構**，確保系統在具備醫學事實準確性的同時，擁有極高的輸出格式穩定性。 透過 SQLite 實現長效數據連貫性，賦予助理分析今日累計與長期趨勢的**時間感知**能力。

**Processing Pipeline**：
1. **NLP Intent Recognition**：精準分析用戶自然語言（如飲食、睡眠或生理指標紀錄），識別核心操作動機。
2. **RAG Retrieval Module**：從 rag_reference/ JSON 知識庫中即時檢索醫學參考數據，確保建議具備事實基礎。
3. **Data Integration & Prompting**：將用戶當前輸入、SQLite 歷史背景（如今日已攝取熱量）與 RAG 知識進行 Context 組裝，構建完整的推論環境。
4. **LLM Generation (Fine-tuned Model)**：調用經微調的 gpt-4o-mini 模型，利用其格式化直覺產生嚴格遵循 JSON 結構的回應，解決複雜格式遺漏問題。
5. **Response Parsing & Data Persistence** ：解析 AI 回傳的結構化數據並同步更新資料庫，最後透過 Line Messaging API 回傳個人化健康洞察。

## 系統架構 System Architecture

- **後端框架**: Flask
- **AI 模型**: OpenAI GPT-4o-mini
- **資料庫**: SQLite
- **核心技術**: 檢索增強生成 (RAG)
- **接入渠道**: Line Messaging API

## 檔案結構 Project Structure

- `app.py`: 主要執行程式與 AI 解析邏輯。
- `rag_reference/`: 存放睡眠、飲食、慢性病的 JSON 標準知識庫。
- `fine_tuning/`: 存放模型微調所需的訓練資料集 JSONL 與資料預處理腳本。
- `health_assistant.db`: SQLite 資料庫，儲存用戶生理參數、結構化健康日誌與對話上下文。
- `.env`: 存放 LINE 與 OpenAI 的 API 金鑰。
- `requirements.txt`: 專案所需的套件依賴清單。

## 安裝與執行 Installation & Setup

1. **安裝環境依賴**：
   ```bash
   pip install -r requirements.txt

2. **配置環境變數： 建立 .env 檔案並填入以下內容**：
   ```bash
   LINE_CHANNEL_ACCESS_TOKEN=你的TOKEN
   LINE_CHANNEL_SECRET=你的SECRET
   OPENAI_API_KEY=你的API金鑰

3. **啟動服務**：
   ```bash
   python app.py

## 使用範例 Usage Guide
- **更新個人檔案**: 點擊「更新個人檔案」，依提示提供性別、身高、體重、年齡以建立計算基準。
- **紀錄飲食**: 輸入飲食項目，系統會顯示該餐熱量、今日累計及建議。
- **紀錄健康數據**: 輸入「血壓 135/85」，系統會回饋對應的風險等級與標準。

## 免責聲明 Disclaimer
本系統提供之所有建議與數據分析僅供參考，不構成醫療診斷或專業醫療建議。如有健康疑慮請務必諮詢專業醫療人員。
