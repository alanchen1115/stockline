import json, os, glob, pathlib
import tempfile
import urllib.parse
import re
import datetime
import httpx
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Header, BackgroundTasks, HTTPException
from google import genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 1. 設定 Google AI API 金鑰與模型設定
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

system_instruction = (
    "你是專業投信分析師，請使用繁體中文（1500字以內），分項說明該公司股市價量表現、"
    "融資融券、內外資籌碼進出及財務資訊，並分析近期公司股市展望，給予投資人具體的專業建議，例如股價支撐或壓力！如果查不到資訊，就回答查無相關資訊"
)

thinking_config = genai.types.ThinkingConfig(thinking_budget=1000)
generation_config = genai.types.GenerateContentConfig(
    max_output_tokens=4000,
    temperature=0.1,
    top_p=0.2,
    thinking_config=thinking_config,
    system_instruction=system_instruction
)

# 2. 設定 Line Bot API
line_bot_api = LineBotApi(os.environ["CHANNEL_ACCESS_TOKEN"])
line_handler = WebhookHandler(os.environ["CHANNEL_SECRET"])

working_status = os.getenv("DEFALUT_TALKING", default="true").lower() == "true"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"title": "Line Bot"}

@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature=Header(None),
):
    body = await request.body()
    try:
        background_tasks.add_task(
            line_handler.handle, body.decode("utf-8"), x_line_signature
        )
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "ok"


@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global working_status
    
    if event.type != "message" or event.message.type != "text":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Event type error:[No message or text]")
        )
        return
        
    user_input = event.message.text
    if user_input == "再見":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Bye!"))
        return
       
    if working_status:
        try: 
            question = user_input
            print(f"\n==========================================")
            print(f"[開始處理查詢] 股票代碼/名稱: {question}")
            print(f"==========================================")
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.tpex.org.tw/"
            }

            pdf_content = None

            # --------------------------------------------------
            # 階段 1：嘗試上市 (TWSE) 檔案
            # --------------------------------------------------
            twse_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            print(f"[階段 1 - 上市 TWSE] 嘗試下載: {twse_url}")
            try:
                res = httpx.get(twse_url, headers=headers, follow_redirects=True, timeout=8.0)
                if res.status_code == 200 and res.content.startswith(b"%PDF"):
                    print(f"[階段 1 - 上市 TWSE] 成功取得 PDF！")
                    pdf_content = res.content
                else:
                    print(f"[階段 1 - 上市 TWSE] 無直連 PDF 檔")
            except Exception as e:
                print(f"[階段 1 - 上市 TWSE] 請求失敗: {e}")

            # --------------------------------------------------
            # 階段 2：嘗試上櫃 (TPEx) 歷史 API
            # --------------------------------------------------
            if not pdf_content:
                print(f"[階段 2 - 上櫃 TPEx] 開始查詢 {question} 簡報 API...")
                current_roc_year = datetime.datetime.now().year - 1911
                for year in range(current_roc_year, current_roc_year - 3, -1):
                    api_url = f"https://www.tpex.org.tw/web/regular_emerging/corporate_info/regular/regular_api.php?stk_code={question}&Syear={year}"
                    try:
                        api_res = httpx.get(api_url, headers=headers, timeout=8.0)
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
                                    print(f"[階段 2 - 上櫃 TPEx] 找到 PDF: {tpex_pdf_url}")
                                    tpex_res = httpx.get(tpex_pdf_url, headers=headers, follow_redirects=True, timeout=8.0)
                                    if tpex_res.status_code == 200 and tpex_res.content.startswith(b"%PDF"):
                                        print(f"[階段 2 - 上櫃 TPEx] 成功取得 PDF！")
                                        pdf_content = tpex_res.content
                                        break
                    except Exception:
                        pass

            # --------------------------------------------------
            # 階段 3：嘗試全網 PDF 搜尋
            # --------------------------------------------------
            if not pdf_content:
                print(f"[階段 3 - 全網搜尋] 開始全網搜尋 {question} 法人說明會 PDF...")
                try:
                    search_query = f"{question} 法人說明會 filetype:pdf"
                    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(search_query)}"
                    
                    search_res = httpx.get(search_url, headers=headers, timeout=8.0)
                    if search_res.status_code == 200:
                        raw_urls = re.findall(r'uddg=([^&"\']+)', search_res.text)
                        pdf_urls = [urllib.parse.unquote(u) for u in raw_urls if ".pdf" in u.lower()]
                        
                        for pdf_target in pdf_urls[:3]:
                            print(f"[階段 3 - 全網搜尋] 嘗試下載: {pdf_target}")
                            try:
                                download_res = httpx.get(pdf_target, headers=headers, follow_redirects=True, timeout=8.0)
                                if download_res.status_code == 200 and download_res.content.startswith(b"%PDF"):
                                    print(f"[階段 3 - 全網搜尋] 成功取得全網 PDF！")
                                    pdf_content = download_res.content
                                    break
                            except Exception:
                                pass
                except Exception as search_err:
                    print(f"[階段 3 - 全網搜尋] 搜尋跳過: {search_err}")

            # --------------------------------------------------
            # 階段 4：Gemini 智慧生成 (PDF 增強 vs DuckDuckGo 實時搜尋)
            # --------------------------------------------------
            if pdf_content:
                print(f"[Gemini 分析] 模式: 【PDF 簡報增強分析】")
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(pdf_content)
                    temp_file_path = temp_file.name
                    sample_doc = client.files.upload(file=temp_file_path)
                    
                prompt = f"這是一份台股『{question}』的法人說明會簡報，請協助依據系統指令做專業建議！"
                
                completion = client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=[sample_doc, prompt],
                    config=generation_config
                ).text
            else:
                print(f"[Gemini 分析] 模式: 【DuckDuckGo 搜尋最新個股資訊】 (查無官方 PDF)")
                
                search_context = ""
                try:
                    from duckduckgo_search import DDGS
                    query = f"{question} 股票 最新股價 營收 三大法人 新聞 資料日期"
                    print(f"[DuckDuckGo] 開始搜尋: {query}")
                    
                    results = list(DDGS().text(query, max_results=5))
                    if results:
                        items = [f"【新聞標題】{r.get('title')}\n【摘要內容】{r.get('body')}" for r in results]
                        search_context = "\n\n".join(items)
                        print(f"[DuckDuckGo] 成功擷取 {len(results)} 筆最新網路資訊！")
                except Exception as ddg_err:
                    print(f"[DuckDuckGo] 搜尋過程發生錯誤: {ddg_err}")

                if search_context:
                    prompt = (
                        f"以下是從網路搜尋到的台灣股票『{question}』最新即時新聞與數據：\n\n"
                        f"{search_context}\n\n"
                        f"請結合上述最新資料，嚴格依照系統指令（分項說明價量表現、籌碼面、財務資訊、未來展望與投資建議）產出專業分析報告！"
                    )
                else:
                    prompt = (
                        f"請針對台灣股票代號/公司名稱『{question}』，"
                        f"依據系統指令（價量表現、籌碼面、財務資訊、未來展望與投資建議）進行專業分析報告！"
                    )

                completion = client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=[prompt],
                    config=generation_config
                ).text

            print("[Gemini] 分析報告生成完成！")
            out = completion

        except Exception as e:
            print(f"[錯誤] 執行發生例外: {str(e)}")
            out = f"Gemini 執行出錯，請稍後再試！錯誤細節: {str(e)}" 

        # 回覆 LINE 使用者
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=out)
        )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
