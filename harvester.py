import os
import json
import requests
import time
import datetime
import io
import urllib3
import random
import sys
import yfinance as yf
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaBytesUpload

# 로그 및 경고 무시 설정
sys.stdout.reconfigure(line_buffering=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 인증] ---
RAW_SERVICE_ACCOUNT = os.getenv('GDRIVE_SERVICE_ACCOUNT')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# 🎯 GitHub Action이 웹앱 신호(dispatch)로 실행되었는지 확인하는 환경변수
GITHUB_EVENT_NAME = os.getenv('GITHUB_EVENT_NAME')

STANDARD_KEYS = [
    "symbol", "name", "price", "currency", "marketCap", "updated", "Hist",
    "per", "pbr", "psr", "pegRatio", "targetMeanPrice",
    "roe", "roa", "eps", "operatingMargins", "debtToEquity",
    "revenueGrowth", "operatingCashflow",
    "dividendRate", "dividendYield",
    "volume", "beta", "heldPercentInstitutions", "shortRatio",
    "fiftyDayAverage", "twoHundredDayAverage", 
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "sector", "industry"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

if not RAW_SERVICE_ACCOUNT:
    print("❌ 에러: 서비스 계정 설정이 없습니다."); sys.exit(1)

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)

# --- [2. 드라이브 유틸리티] ---
def find_file_id(name, parent_id=None):
    for _ in range(3):
        try:
            query = f"name = '{name}' and trashed = false"
            if parent_id: query += f" and '{parent_id}' in parents"
            results = drive_service.files().list(
                q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute().get('files', [])
            return results[0]['id'] if results else None
        except: time.sleep(2)
    return None

def download_json(file_id):
    if not file_id: return {}
    for _ in range(3):
        try:
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            return json.loads(fh.getvalue().decode())
        except: time.sleep(2)
    return {}

def upload_json(filename, data, parent_id):
    print(f"📤 업로드 시도: {filename}...")
    json_content = json.dumps(data, indent=4, ensure_ascii=False).encode('utf-8')
    media = MediaBytesUpload(json_content, mimetype='application/json')
    for attempt in range(5):
        try:
            file_id = find_file_id(filename, parent_id)
            if file_id:
                drive_service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
            else:
                meta = {'name': filename, 'parents': [parent_id]}
                drive_service.files().create(body=meta, media_body=media, supportsAllDrives=True).execute()
            return
        except Exception as e:
            print(f"⚠️ 업로드 에러: {str(e)}"); time.sleep(10)

def sync_ohlcv_incremental(ticker, ohlcv_dir_id):
    file_name = f"{ticker}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    existing_data = download_json(file_id) if file_id else []
    try:
        stock = yf.Ticker(ticker)
        period = "7d" if existing_data else "1y"
        df = stock.history(period=period, interval="1d")
        if df.empty: return False
        new_recs = [{"date": d.strftime('%Y-%m-%d'), "open": round(r['Open'], 2), "high": round(r['High'], 2), "low": round(r['Low'], 2), "close": round(r['Close'], 2), "volume": int(r['Volume'])} for d, r in df.iterrows()]
        combined = {item['date']: item for item in (existing_data + new_recs)}
        upload_json(file_name, sorted(combined.values(), key=lambda x: x['date']), ohlcv_dir_id)
        return True
    except: return False

# --- [3. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    total_success = 0
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')

    try:
        print(f"🔍 시스템 가동: {today_str} (Event: {GITHUB_EVENT_NAME})")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        
        # 🎯 [핵심] 신호가 있을 때만 OHLCV 수집 진행
        # repository_dispatch 이거나 사용자가 직접 누른 workflow_dispatch 일 때만 체크
        if GITHUB_EVENT_NAME in ['repository_dispatch', 'workflow_dispatch']:
            ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
            s3_folder_id = find_file_id("Stage3_Fundamental_Data", root_id)
            
            if s3_folder_id:
                query = f"'{s3_folder_id}' in parents and name contains 'STAGE3_FUNDAMENTAL_FULL_' and trashed = false"
                s3_files = drive_service.files().list(q=query, fields="files(id, name)", orderBy="createdTime desc", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get('files', [])
                
                if s3_files:
                    latest_s3 = s3_files[0]
                    ready_id = find_file_id("LATEST_STAGE4_READY.json", sys_id)
                    ready_info = download_json(ready_id) if ready_id else {}
                    
                    if ready_info.get("trigger_file") != latest_s3['name']:
                        print(f"💎 신규 신호 확인: {latest_s3['name']}")
                        s3_data = download_json(latest_s3['id'])
                        
                        # 데이터 리스트 추출 로직 (유연하게 대응)
                        target_list = s3_data.get('fundamental_universe') or s3_data.get('stocks') or (s3_data if isinstance(s3_data, list) else None)
                        
                        if isinstance(target_list, list):
                            s3_tickers = [item['symbol'] for item in target_list if 'symbol' in item]
                            if s3_tickers:
                                send_telegram(f"🚀 *신호 감지:* `{len(s3_tickers)}`종목 OHLCV 수집 시작")
                                for st in s3_tickers:
                                    sync_ohlcv_incremental(st, ohlcv_dir_id)
                                    time.sleep(random.uniform(1.3, 1.6))
                                
                                signal = {"status": "COMPLETED", "stage": 4, "trigger_file": latest_s3['name'], "timestamp": today_str}
                                upload_json("LATEST_STAGE4_READY.json", signal, sys_id)
                                send_telegram(f"✅ *Stage 4 준비 완료!*")
        
        # --- [공통 업무: 데일리 정기 수집] ---
        # 신호 유무와 상관없이 스케줄 시간이면 항상 실행됩니다.
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        # 한국 시간 기준 수집 그룹 결정
        target_chars = "ABCDEFGHIJKLM" if 6 <= now_kst.hour <= 11 else "NOPQRSTUVWXYZ0123456789"
        
        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        if full_map:
            filtered = {t: i for t, i in full_map.items() if (t[0].upper() in target_chars)}
            send_telegram(f"📡 *정기 데일리 수집:* `{len(filtered)}`종목 시작")
            
            groups = sorted(list(set(i['group'] for i in filtered.values())))
            for group in groups:
                group_tickers = {t: i for t, i in filtered.items() if i['group'] == group}
                daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
                daily_data = download_json(find_file_id(daily_name, daily_dir_id))
                hist_data = download_json(find_file_id(hist_name, hist_dir_id))

                for ticker in group_tickers:
                    try:
                        time.sleep(random.uniform(1.3, 1.6))
                        stock = yf.Ticker(ticker)
                        info = stock.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        if price:
                            raw = {"symbol": ticker, "name": info.get('shortName'), "price": price, "updated": today_str, "Hist": "✅"}
                            daily_data[ticker] = {k: raw.get(k, None) for k in STANDARD_KEYS}
                            total_success += 1
                    except: continue
                
                upload_json(daily_name, daily_data, daily_dir_id)
                upload_json(hist_name, hist_data, hist_dir_id)

        send_telegram(f"🏁 *수집 종료* | 총 성공: `{total_success}`")

    except Exception as e:
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
