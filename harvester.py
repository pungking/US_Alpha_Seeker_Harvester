import os
import json
import requests
import time
import datetime
import io
import urllib3
import random
import yfinance as yf
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 인증] ---
RAW_SERVICE_ACCOUNT = os.getenv('GDRIVE_SERVICE_ACCOUNT')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

STANDARD_KEYS = ["symbol", "name", "price", "currency", "marketCap", "per", "pbr", "psr", "roe", "eps", "volume", "beta", "dividendYield", "sector", "industry", "updated", "Hist"]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

if not RAW_SERVICE_ACCOUNT:
    print("❌ 서비스 계정 설정 없음"); exit(1)

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 드라이브 유틸리티] ---
def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
    return results[0]['id'] if results else None

def download_json(file_id):
    if not file_id: return {}
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: _, done = downloader.next_chunk()
    return json.loads(fh.getvalue().decode())

def upload_json(filename, data, parent_id):
    file_id = find_file_id(filename, parent_id)
    fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
    media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': filename, 'parents': [parent_id]}
        drive_service.files().create(body=meta, media_body=media).execute()

# --- [3. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)
    
    # [A] 현재 타겟 그룹 판별 로직
    current_hour = now_kst.hour
    if 6 <= current_hour <= 9:
        target_group_label = "1차 수집 그룹 (A-M)"
        target_chars = "ABCDEFGHIJKLM"
    else:
        target_group_label = "2차 수집 그룹 (N-Z & 기타)"
        target_chars = "NOPQRSTUVWXYZ0123456789"

    try:
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(mapping_id)

        filtered_tickers = {t: info for t, info in full_map.items() 
                           if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        # [B] 시작 메시지 강화
        start_msg = (
            f"📡 *[US Alpha Seeker] 수집 가동*\n"
            f"📅 *일시:* `{today_str}`\n"
            f"🎯 *대상:* `{target_group_label}`\n"
            f"📊 *종목 수:* `{len(filtered_tickers)}개`\n"
            f"🔄 *히스토리 점검:* `{'매주 토요일 전체 갱신' if is_weekend_update else '누락분만 보완'}`"
        )
        send_telegram(start_msg)

        daily_storage = {}
        hist_storage = {}

        for ticker in filtered_tickers:
            group = filtered_tickers[ticker]['group']
            daily_name = f"{group}_stocks_daily.json"
            hist_name = f"{group}_stocks_history.json"

            if daily_name not in daily_storage:
                fid = find_file_id(daily_name, daily_dir_id)
                daily_storage[daily_name] = download_json(fid) if fid else {}
            if hist_name not in hist_storage:
                fid = find_file_id(hist_name, hist_dir_id)
                hist_storage[hist_name] = download_json(fid) if fid else {}

            try:
                time.sleep(random.uniform(1.2, 1.6))
                stock = yf.Ticker(ticker)
                
                # 히스토리 업데이트 체크
                curr_hist_status = daily_storage[daily_name].get(ticker, {}).get('Hist', '❌')
                if curr_hist_status == '❌' or is_weekend_update:
                    try:
                        f_data = stock.quarterly_financials
                        if not f_data.empty:
                            hist_storage[hist_name][ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                            curr_hist_status = '✅'
                    except: pass

                # 데일리 업데이트
                info = stock.info
                price = info.get('currentPrice') or info.get('regularMarketPrice')
                if price:
                    raw_info = {
                        "symbol": ticker,
                        "name": info.get('shortName') or info.get('longName'),
                        "price": price,
                        "marketCap": info.get('marketCap'),
                        "per": info.get('trailingPE'),
                        "pbr": info.get('priceToBook'),
                        "roe": info.get('returnOnEquity'),
                        "updated": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "Hist": curr_hist_status
                    }
                    daily_data_filled = {k: raw_info.get(k, info.get(k)) for k in STANDARD_KEYS}
                    daily_storage[daily_name][ticker] = daily_data_filled
                    success_count += 1
                
                if success_count % 300 == 0:
                    send_telegram(f"⏳ *진행 보고:* {success_count}개 수집 완료...")

            except Exception as e:
                error_count += 1

        # [C] 저장 단계 보고 강화
        send_telegram(f"📤 *데이터 저장 중...* (총 {len(daily_storage)}개 그룹 파일)")
        
        for d_name, d_content in daily_storage.items():
            upload_json(d_name, d_content, daily_dir_id)
            time.sleep(0.3)
        for h_name, h_content in hist_storage.items():
            upload_json(h_name, h_content, hist_dir_id)
            time.sleep(0.3)

        duration = (time.time() - start_time) / 60
        finish_msg = (
            f"✅ *하베스팅 완료 보고*\n"
            f"⏱️ *소요 시간:* `{duration:.1f}분`\n"
            f"📈 *성공:* `{success_count}종목` / ⚠️ *실패:* `{error_count}종목`"
        )
        send_telegram(finish_msg)

    except Exception as e:
        send_telegram(f"🚨 *치명적 에러 발생:*\n`{str(e)}`")

if __name__ == "__main__":
    run_harvester()
