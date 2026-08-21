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
  
    # elif working_status:
    #     try: 
    #         # 取得使用者輸入的文字
    #         question = event.message.text
    #         doc_url = "https://www.twse.com.tw/pdf/ch/"+question+"_ch.pdf"
    #         doc_data = httpx.get(doc_url)
    #         if doc_data.status_code != 200:
    #             completion = '查無股票代號！請輸入台灣上市股票代號！'
    #         else:
    #             with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
    #                 temp_file.write(doc_data.content)
    #                 temp_file_path = temp_file.name
    #                 sample_doc = client.files.upload(file=temp_file_path)
    #                 prompt = "請給專業建議!"               
    #             # gemini-2.5-flash
    #             completion = client.models.generate_content(
    #                                 model="gemini-2.5-flash",
    #                                 contents=[sample_doc, prompt],
    #                                 config=generation_config).text
    #         # 取得生成結果
    #         out = completion
    #     except:
    #         # 處理錯誤
    #         out = "Gemini執行出錯!請換個說法！" 
          
    elif working_status:
        try: 
            # 取得使用者輸入的文字
            question = event.message.text
            
            # 1. 先嘗試上市 (TWSE) 網址
            doc_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            doc_data = httpx.get(doc_url, follow_redirects=True)
            
            # 2. 若上市抓不到，嘗試上櫃 (TPEx) 網址
            if doc_data.status_code != 200:
                doc_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/doc/{question}_ch.pdf"
                doc_data = httpx.get(doc_url, follow_redirects=True)

            # 3. 兩者都抓不到才判定為無資料
            if doc_data.status_code != 200:
                completion = '查無股票代號或無簡報檔案！請輸入台灣上市或上櫃股票代號！'
            else:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(doc_data.content)
                    temp_file_path = temp_file.name
                    sample_doc = client.files.upload(file=temp_file_path)
                    prompt = "請給專業建議!"               
                
                # gemini-2.5-flash
                completion = client.models.generate_content(
                                    model="gemini-2.5-flash",
                                    contents=[sample_doc, prompt],
                                    config=generation_config).text
            # 取得生成結果
            out = completion
        except Exception as e:
            # 處理錯誤
            out = "Gemini執行出錯!請換個說法！"
          
        # 回覆生成結果
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=out))

if __name__ == "__main__":
    # 啟動 FastAPI 應用程式
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
