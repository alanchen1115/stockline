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
system_instruction = "你是投信分析師，請使用繁體中文2000字以內，分項說明公司股市價量表現、融資融卷、內外資進出及財務資訊，並分析近期公司股市展望給投資人具體的專業建議,包含股價支撐與壓力!"
thinking_config = genai.types.ThinkingConfig(thinking_budget=1000) # thinking_budget = 0,  turn off thinking mode
generation_config = genai.types.GenerateContentConfig(max_output_tokens=4000, temperature=0.1, top_p=0.2,
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
        # 回覆錯誤訊息
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Event type error:[No message or the message does not contain text]")
        )
        
    # 檢查使用者是否輸入 "再見"
    elif event.message.text == "再見":
        # 回覆 "Bye!"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Bye!")
        )
        return
       
    # 檢查是否正在與使用者交談
    elif working_status:
        try: 
            # 取得使用者輸入的股票代號（自動去除前後空白）
            question = event.message.text.strip()
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }

            pdf_content = None

            # --------------------------------------------------
            # 1. 僅嘗試下載上市 (TWSE) 簡報 PDF
            # --------------------------------------------------
            twse_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            try:
                res = httpx.get(twse_url, headers=headers, follow_redirects=True, timeout=8.0)
                # 驗證 HTTP 狀態碼為 200 且檔案開頭為 %PDF
                if res.status_code == 200 and res.content.startswith(b"%PDF"):
                    pdf_content = res.content
            except Exception:
                pdf_content = None

            # --------------------------------------------------
            # 2. 設定開啟 Google Search 實時網路查詢功能的 Config
            # --------------------------------------------------
            search_generation_config = genai.types.GenerateContentConfig(
                max_output_tokens=5000,
                temperature=0.1,
                top_p=0.2,
                thinking_config=thinking_config,
                system_instruction=system_instruction,
                tools=[{"google_search": {}}]  # <<-- 開啟 Google Search 實時連網搜尋
            )

            # --------------------------------------------------
            # 3. 呼叫 Gemini 生成分析報告 (PDF 模式 vs 上櫃/連網查詢模式)
            # --------------------------------------------------
            if pdf_content:
                # 模式 A：成功抓到上市 PDF 簡報
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(pdf_content)
                    temp_file_path = temp_file.name
                    sample_doc = client.files.upload(file=temp_file_path)
                    
                prompt = f"這是一份台灣上市股票『{question}』的法人說明會簡報，請結合這份簡報與最新網路資訊，依系統指令給予專業建議！"
                contents = [sample_doc, prompt]
            else:
                # 模式 B：無上市 PDF (包含上櫃股票 Https://tpex.org.tw 或未上傳簡報者)
                # 不下載 PDF，直接由 Gemini 連網搜尋櫃買中心與最新股價籌碼資訊
                prompt = (
                    f"請透過網路搜尋 https://www.tpex.org.tw/zh-tw/mainboard/listed/company-detail.html?{question} 最新的即時個股資訊!"
                    f"你是投信分析師，請使用繁體中文2000字以內，分項說明公司股市價量表現、融資融卷、內外資進出及財務資訊，並分析近期公司股市展望給投資人具體的專業建議,包含股價支撐與壓力!"
                )
                contents = [prompt]

            # 呼叫 Gemini 模型
            completion = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=search_generation_config
            ).text

            out = completion

        except Exception as e:
            out = f"Gemini執行出錯! 錯誤細節: {str(e)}"

        # 回覆 LINE 使用者
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=out))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
