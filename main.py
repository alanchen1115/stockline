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
            question = event.message.text
            print(f"\n--- [開始處理查詢] 股票代碼: {question} ---")
            # 建立完整模擬真實瀏覽器的 Header
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://mops.twse.com.tw/"
            }

            pdf_content = None

            # --------------------------------------------------
            # 階段 1：嘗試上市 (TWSE) 快速下載
            # --------------------------------------------------
            twse_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            print(f"[上市 TWSE] 嘗試下載: {twse_url}")
            try:
                res = httpx.get(twse_url, headers=headers, follow_redirects=True, timeout=10.0)
                if res.status_code == 200 and res.content.startswith(b"%PDF"):
                    print(f"[上市 TWSE] 成功取得 PDF 檔案！")
                    pdf_content = res.content
                else:
                    print(f"[上市 TWSE] 查無 PDF (非 PDF 格式)")
            except Exception as e:
                print(f"[上市 TWSE] 發生錯誤: {e}")

            # --------------------------------------------------
            # 階段 2：嘗試上櫃 (TPEx) 歷史年度 API
            # --------------------------------------------------
            if not pdf_content:
                print(f"[上櫃 TPEx] 開始查詢 {question} 的簡報網址...")
                import datetime, re, urllib.parse
                current_roc_year = datetime.datetime.now().year - 1911
                
                for year in range(current_roc_year, current_roc_year - 4, -1):
                    api_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/regular_api.php?stk_code={question}&Syear={year}"
                    try:
                        api_res = httpx.get(api_url, headers=headers, timeout=10.0)
                        if api_res.status_code == 200:
                            api_json = api_res.json()
                            if "aaData" in api_json and len(api_json["aaData"]) > 0:
                                tpex_pdf_url = None
                                for row in api_json["aaData"]:
                                    row_str = str(row)
                                    pdf_match = re.search(r'(doc/[^\'"]+\.pdf|[^\'"]+\.pdf)', row_str, re.IGNORECASE)
                                    if pdf_match:
                                        raw_path = urllib.parse.unquote(pdf_match.group(1))
                                        if raw_path.startswith("http"):
                                            tpex_pdf_url = raw_path
                                        elif raw_path.startswith("doc/"):
                                            tpex_pdf_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/{raw_path}"
                                        else:
                                            tpex_pdf_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/doc/{raw_path}"
                                        break
                                
                                if tpex_pdf_url:
                                    print(f"[上櫃 TPEx] 找到 PDF 網址: {tpex_pdf_url}")
                                    tpex_res = httpx.get(tpex_pdf_url, headers=headers, follow_redirects=True, timeout=10.0)
                                    if tpex_res.status_code == 200 and tpex_res.content.startswith(b"%PDF"):
                                        print(f"[上櫃 TPEx] 成功取得 PDF 檔案！")
                                        pdf_content = tpex_res.content
                                        break
                    except Exception:
                        pass

            # --------------------------------------------------
            # 階段 3：前兩者皆失敗，調用公開資訊觀測站 (MOPS) API
            # --------------------------------------------------
            if not pdf_content:
                print(f"[MOPS 觀測站] 開始從公開資訊觀測站查詢 {question}...")
                import datetime, re
                mops_url = "https://mops.twse.com.tw/mops/web/ajax_t100sb07_1"
                current_roc_year = datetime.datetime.now().year - 1911
                
                for year in range(current_roc_year, current_roc_year - 3, -1):
                    payload = {"encodeRequestParam": "1", "step": "1", "firstin": "1", "co_id": question, "year": str(year)}
                    try:
                        mops_res = httpx.post(mops_url, data=payload, headers=headers, timeout=10.0)
                        if mops_res.status_code == 200:
                            pdf_matches = re.findall(r"([a-zA-Z0-9_\-]+\.pdf)", mops_res.text, re.IGNORECASE)
                            if pdf_matches:
                                for pdf_name in pdf_matches:
                                    if len(pdf_name) > 5 and not pdf_name.startswith("http"):
                                        possible_urls = [
                                            f"https://mops.twse.com.tw/nas/STR/{pdf_name}",
                                            f"https://mops.twse.com.tw/server-java/t57sb01?step=1&colorchg=1&co_id={question}&year={year}&mops_filename={pdf_name}"
                                        ]
                                        for test_url in possible_urls:
                                            mops_pdf_res = httpx.get(test_url, headers=headers, follow_redirects=True, timeout=10.0)
                                            if mops_pdf_res.status_code == 200 and mops_pdf_res.content.startswith(b"%PDF"):
                                                print(f"[MOPS 觀測站] 成功取得 PDF 檔案！")
                                                pdf_content = mops_pdf_res.content
                                                break
                                    if pdf_content:
                                        break
                            if pdf_content:
                                break
                    except Exception:
                        pass

            # --------------------------------------------------
            # 階段 4：全網搜尋 (修正正確名稱比對 + 支援 DiveInvest 等第三方平台)
            # --------------------------------------------------
            if not pdf_content:
                print(f"[全網搜尋] 開始搜尋 {question} (含達爾膚/美時等正確關鍵字) 法人說明會 PDF ...")
                try:
                    import urllib.parse, re
                    
                    # 帶入股票代號進行精確搜尋
                    search_query = f"{question} 法人說明會 filetype:pdf"
                    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(search_query)}"
                    
                    search_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
                    }
                    
                    search_res = httpx.get(search_url, headers=search_headers, timeout=10.0)
                    if search_res.status_code == 200:
                        # 擷取搜尋結果中真正的目標網址
                        raw_urls = re.findall(r'uddg=([^&"\']+)', search_res.text)
                        pdf_urls = [urllib.parse.unquote(u) for u in raw_urls if ".pdf" in u.lower()]
                        
                        print(f"[全網搜尋] 找到 {len(pdf_urls)} 個可能直連的 PDF 連結")
                        
                        for pdf_target in pdf_urls[:5]:
                            print(f"[全網搜尋] 嘗試下載: {pdf_target}")
                            try:
                                download_res = httpx.get(pdf_target, headers=headers, follow_redirects=True, timeout=10.0)
                                if download_res.status_code == 200 and download_res.content.startswith(b"%PDF"):
                                    print(f"[全網搜尋] 成功下載 PDF 檔案！")
                                    pdf_content = download_res.content
                                    break
                            except Exception as dl_err:
                                print(f"[全網搜尋] 下載失敗: {dl_err}")
                except Exception as search_err:
                    print(f"[全網搜尋] 搜尋過程發生錯誤: {search_err}")

            # 3. 檢查最終是否有取得合格的 PDF 內容
            if not pdf_content:
                print(f"[結果] 找不到合格的 PDF 檔案")
                completion = '查無此股票或該公司未上傳法說會簡報 PDF！請確認代碼後再試！'
            else:
                print(f"[結果] 成功取得合法 PDF，檔案大小: {len(pdf_content)} bytes")
                
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(pdf_content)
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
