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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request

# 로그 실시간 출력 설정 (GitHub Actions 환경 최적화)
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(line_buffering=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 본계정 인증] ---
CLIENT_ID = os.getenv('GDRIVE_CLIENT_ID')
CLIENT_SECRET = os.getenv('GDRIVE_CLIENT_SECRET')
REFRESH_TOKEN = os.getenv('GDRIVE_REFRESH_TOKEN')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
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

def get_drive_service():
    """본계정 Refresh Token을 사용하여 드라이브 서비스 빌드"""
    creds_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "type": "authorized_user"
    }
    creds = Credentials.from_authorized_user_info(creds_data, ["https://www.googleapis.com/auth/drive"])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

drive_service = get_drive_service()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

# --- [2. 드라이브 유틸리티] ---
def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    try:
        results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        return results[0]['id'] if results else None
    except: return None

def download_json(file_id):
    if not file_id: return []
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode())
    except: return []

def upload_json(filename, data, parent_id):
    print(f"📤 업로드 시도: {filename}...")
    file_id = find_file_id(filename, parent_id)
    fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
    media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
    
    try:
        if file_id:
            drive_service.files().update(fileId=file_id, media_body=media).execute()
        else:
            meta = {'name': filename, 'parents': [parent_id]}
            drive_service.files().create(body=meta, media_body=media).execute()
        print(f"✅ 완료: {filename}")
    except Exception as e:
        print(f"❌ 업로드 에러: {str(e)}")

# [ADDED] 실시간 진행률 기록 함수
def update_progress(current, total, ticker, sys_id, status="PROCESSING"):
    progress_data = {
        "status": status,
        "current": current,
        "total": total,
        "last_ticker": ticker,
        "percentage": round((current / total) * 100, 1) if total > 0 else 0,
        "updated": (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
    }
    # 웹앱 조회를 위해 고정된 파일명 사용
    upload_json("COLLECTION_PROGRESS.json", progress_data, sys_id)

# --- [OHLCV 누적 수집 로직] ---
def sync_ohlcv_incremental(ticker, ohlcv_dir_id):
    file_name = f"{ticker}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    existing_data = download_json(file_id) if file_id else []
    
    try:
        stock = yf.Ticker(ticker)
        period = "7d" if existing_data else "2y"
        df = stock.history(period=period, interval="1d")
        
        if df.empty: return False
        
        new_recs = [
            {
                "symbol": ticker,
                "date": d.strftime('%Y-%m-%d'), 
                "open": round(r['Open'], 2), 
                "high": round(r['High'], 2), 
                "low": round(r['Low'], 2), 
                "close": round(r['Close'], 2), 
                "volume": int(r['Volume'])
            } for d, r in df.iterrows()
        ]
        
        combined = {item['date']: item for item in (existing_data + new_recs)}
        final_list = sorted(combined.values(), key=lambda x: x['date'])[-500:]
        
        upload_json(file_name, final_list, ohlcv_dir_id)
        return True
    except: return False

# --- [3. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    total_success, total_error = 0, 0
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)

    try:
        print(f"🔍 시스템 가동: {today_str} (Event: {GITHUB_EVENT_NAME})")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)

        # 🎯 1. [특별 작업 모드] 웹앱 신호 시 OHLCV 300개 수집
        if GITHUB_EVENT_NAME == 'repository_dispatch':
            ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
            s3_folder_id = find_file_id("Stage3_Fundamental_Data", root_id)
            
            if s3_folder_id:
                query = f"'{s3_folder_id}' in parents and name contains 'STAGE3_FUNDAMENTAL_FULL_' and trashed = false"
                s3_files = drive_service.files().list(q=query, fields="files(id, name)", orderBy="createdTime desc").execute().get('files', [])
                
                if s3_files:
                    latest_s3 = s3_files[0]
                    s3_data = download_json(latest_s3['id'])
                    
                    t_list = s3_data.get('fundamental_universe') or s3_data.get('stocks') or (s3_data if isinstance(s3_data, list) else [])
                    s3_tickers = list(set([item['symbol'] for item in t_list if isinstance(item, dict) and 'symbol' in item]))
                    
                    if s3_tickers:
                        total_tickers = len(s3_tickers)
                        send_telegram(f"🚀 *수집 시작:* `{total_tickers}`종목 (2년치)")
                        
                        # [ADDED] 수집 시작 알림
                        update_progress(0, total_tickers, "STARTING...", sys_id, "PROCESSING")

                        for idx, st in enumerate(s3_tickers, 1):
                            if sync_ohlcv_incremental(st, ohlcv_dir_id):
                                total_success += 1
                            else:
                                total_error += 1
                            
                            # [ADDED] 10종목마다 또는 마지막에 드라이브 진행률 업데이트
                            if idx % 10 == 0 or idx == total_tickers:
                                update_progress(idx, total_tickers, st, sys_id, "PROCESSING")
                                print(f"📊 진행 상황: {idx}/{total_tickers} ({st})")

                            time.sleep(random.uniform(1.6, 2.3))
                        
                        # [ADDED] 수집 종료 상태 업데이트
                        update_progress(total_tickers, total_tickers, "FINISHED", sys_id, "COMPLETED")
                        
                        upload_json("LATEST_STAGE4_READY.json", {"status": "COMPLETED", "trigger_file": latest_s3['name'], "timestamp": today_str}, sys_id)
                        send_telegram(f"✅ *Stage 4 수집 완료!*\n성공: `{total_success}` | 실패: `{total_error}`")
            return 

        # 🎯 2. [데일리 수집 모드]
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        current_hour = now_kst.hour
        if 6 <= current_hour <= 11:
            group_label, target_chars = "1차 (A-M)", "ABCDEFGHIJKLM"
        else:
            group_label, target_chars = "2차 (N-Z & 기타)", "NOPQRSTUVWXYZ0123456789"

        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *[Daily] 본계정 가동*\n🎯 *타겟:* `{group_label}` | `{len(filtered_tickers)}`종목")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_success, g_error = 0, 0
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))

            for ticker in group_tickers:
                success_flag = False
                for _ in range(2):
                    try:
                        time.sleep(random.uniform(1.3, 1.8))
                        stock = yf.Ticker(ticker)
                        info = stock.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        
                        if price:
                            daily_data[ticker] = {k: info.get(k) for k in STANDARD_KEYS}
                            daily_data[ticker]["updated"] = today_str
                            daily_data[ticker]["symbol"] = ticker
                            g_success += 1
                            success_flag = True
                            break
                    except: pass
                if not success_flag: g_error += 1

            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            total_success += g_success; total_error += g_error

        duration = (time.time() - start_time) / 60
        send_telegram(f"🏁 *수집 종료*\n⏱️ `{duration:.1f}분` | 성공: `{total_success}`")

    except Exception as e:
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
