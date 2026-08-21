import json, os, glob, pathlib
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request,  Header, BackgroundTasks, HTTPException, status
from google import genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, AudioMessage
import httpx
import tempfile

# 設定 Google AI API 金鑰
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
system_instruction = "你是投信分析師，請使用繁體中文2000字以內，分項說明公司股市價量表現、融資融卷、內外資進出及財務資訊，並分析近期公司股市展望給投資人具體的專業建議!"
thinking_config = genai.types.ThinkingConfig(thinking_budget=0) # thinking_budget = 0,  turn off thinking mode
generation_config = genai.types.GenerateContentConfig(max_output_tokens=5000, temperature=0.1, top_p=0.2,
                                                      thinking_config=thinking_config,
                                                      system_instruction=system_instruction)
# 設定 Line Bot 的 API 金鑰和秘密金鑰
line_bot_api = LineBotApi(os.environ["CHANNEL_ACCESS_TOKEN"])
line_handler = WebhookHandler(os.environ["CHANNEL_SECRET"])

# 設定是否正在與使用者交談
working_status = os.getenv("DEFALUT_TALKING", default = "true").lower() == "true"

# 建立 FastAPI 應用程式
app = FastAPI()

# 設定 CORS，允許跨域請求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 處理根路徑請求
@app.get("/")
def root():
    return {"title": "Line Bot"}

# 處理 Line Webhook 請求
@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature=Header(None),
):
    # 取得請求內容
    body = await request.body()
    try:
        # 將處理 Line 事件的任務加入背景工作
        background_tasks.add_task(
            line_handler.handle, body.decode("utf-8"), x_line_signature
        )
    except InvalidSignatureError:
        # 處理無效的簽章錯誤
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "ok"

# 處理文字訊息事件
@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global working_status
    
    # 檢查事件類型和訊息類型
    if event.type != "message" or event.message.type != "text":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Event type error:[No message or the message does not contain text]")
        )
        return
        
    # 檢查使用者是否輸入 "再見"
    elif event.message.text == "再見":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Bye!")
        )
        return
       
    # 檢查是否正在與使用者交談
    elif working_status:
        try: 
            question = event.message.text.strip()
            print(f"\n--- [開始處理查詢] 股票代碼: {question} ---")
# 建立完整模擬真實瀏覽器的 Header
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.tpex.org.tw/"
            }

            # 1. 先嘗試上市 (TWSE)
            twse_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            print(f"[上市 TWSE] 嘗試下載: {twse_url}")
            doc_data = httpx.get(twse_url, headers=headers, follow_redirects=True, timeout=10.0)
            print(f"[上市 TWSE] 回傳狀態碼: {doc_data.status_code}")

            # 2. 若上市抓不到 (status != 200)，動態查詢上櫃 (TPEx) 的簡報 API
            if doc_data.status_code != 200:
                print(f"[上櫃 TPEx] 開始透過 API 查詢 {question} 的簡報網址...")
                api_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/regular_api.php?stk_code={question}"
                
                api_res = httpx.get(api_url, headers=headers, timeout=10.0)
                
                if api_res.status_code == 200:
                    try:
                        api_json = api_res.json()
                        # 檢查 API 是否有回傳簡報資料
                        if "aaData" in api_json and len(api_json["aaData"]) > 0:
                            # 取得最新一次法說會的 PDF 檔名或相對路徑
                            # 櫃買中心 API 的 JSON 中通常會在第 5 或第 6 個欄位包含 PDF 連結檔名
                            pdf_file_info = api_json["aaData"][0] 
                            
                            # 抓取 API 回傳的 PDF 檔名 (例如 "1130315_8069.pdf")
                            # 透過 parse 或正規表達式提取 .pdf 網址
                            import re
                            pdf_match = re.search(r'doc/([^\'"]+\.pdf)', str(pdf_file_info))
                            
                            if pdf_match:
                                pdf_filename = pdf_match.group(1)
                                tpex_pdf_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/doc/{pdf_filename}"
                                print(f"[上櫃 TPEx] 成功找到 PDF 網址: {tpex_pdf_url}")
                                
                                # 下載真正的上櫃 PDF
                                doc_data = httpx.get(tpex_pdf_url, headers=headers, follow_redirects=True, timeout=10.0)
                                print(f"[上櫃 TPEx] PDF 下載狀態碼: {doc_data.status_code}")
                            else:
                                print("[上櫃 TPEx] 找到法說會紀錄，但未包含 PDF 檔案連結")
                        else:
                            print("[上櫃 TPEx] 該公司未在上櫃系統找到法說會簡報紀錄")
                    except Exception as json_err:
                        print(f"[上櫃 TPEx] 解析 API JSON 失敗: {json_err}")

            # 3. 檢查最終抓取結果
            if doc_data.status_code != 200:
                print(f"[結果] 上市與上櫃均抓取失敗 (最後 Status Code: {doc_data.status_code})")
                completion = '查無股票代號或該公司未上傳法說會簡報 PDF！請確認代碼後再試！'
            else:
                print(f"[結果] 成功取得 PDF，檔案大小: {len(doc_data.content)} bytes")
                
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(doc_data.content)
                    temp_file_path = temp_file.name
                    
                    print("[Gemini] 開始上傳 PDF 至 Google API...")
                    sample_doc = client.files.upload(file=temp_file_path)
                    prompt = "請給專業建議!"               
                    
                    print("[Gemini] 開始生成分析報告...")
                    completion = client.models.generate_content(
                                        model="gemini-2.5-flash",
                                        contents=[sample_doc, prompt],
                                        config=generation_config).text
                    print("[Gemini] 報告生成完成！")
                    
            out = completion
            
        except Exception as e:
            print(f"[錯誤] 程式執行發生例外狀況: {str(e)}")
            out = f"Gemini執行出錯! 錯誤訊息: {str(e)}" 
  
        # 回覆生成結果
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=out))
        return

if __name__ == "__main__":
    # 啟動 FastAPI 應用程式
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
