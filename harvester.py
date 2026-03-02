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
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 로그 실시간 출력 설정
sys.stdout.reconfigure(line_buffering=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 인증] ---
RAW_SERVICE_ACCOUNT = os.getenv('GDRIVE_SERVICE_ACCOUNT')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# 🎯 깃허브로부터 실행 원인(Event)을 가져옵니다.
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
            results = drive_service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get('files', [])
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
    for attempt in range(5):
        try:
            file_id = find_file_id(filename, parent_id)
            fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
            media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
            if file_id:
                drive_service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
            else:
                meta = {'name': filename, 'parents': [parent_id]}
                drive_service.files().create(body=meta, media_body=media, supportsAllDrives=True).execute()
            print(f"✅ 업로드 완료: {filename}")
            return
        except Exception as e:
            print(f"   ⚠️ 업로드 실패 ({attempt+1}/5): {str(e)}")
            time.sleep(10)

# --- [신규 로직: OHLCV 수집 함수] ---
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
    total_success, total_error = 0, 0
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)

    try:
        print(f"🔍 시스템 가동: {today_str} (Event: {GITHUB_EVENT_NAME})")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)

        # 🎯 [게이트키퍼] 웹앱 신호(dispatch)가 있을 때만 OHLCV 수집 실행
        if GITHUB_EVENT_NAME == 'repository_dispatch':
            print("🚀 웹앱 신호 감지: OHLCV 특별 수집 모드 가동")
            ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
            s3_folder_id = find_file_id("Stage3_Fundamental_Data", root_id)
            if s3_folder_id:
                query = f"'{s3_folder_id}' in parents and name contains 'STAGE3_FUNDAMENTAL_FULL_' and trashed = false"
                s3_files = drive_service.files().list(q=query, fields="files(id, name)", orderBy="createdTime desc", supportsAllDrives=True).execute().get('files', [])
                if s3_files:
                    latest_s3 = s3_files[0]
                    # 💡 파일 내부 구조를 찾지 못할 경우를 대비해 예외 처리 강화
                    s3_data = download_json(latest_s3['id'])
                    # 웹앱 JSON 구조 유연하게 탐색
                    t_list = s3_data.get('fundamental_universe') or s3_data.get('stocks') or (s3_data if isinstance(s3_data, list) else [])
                    s3_tickers = [item['symbol'] for item in t_list if isinstance(item, dict) and 'symbol' in item]
                    
                    if s3_tickers:
                        send_telegram(f"🔍 *신호 확인:* `{len(s3_tickers)}`종목 OHLCV 수집")
                        for st in s3_tickers:
                            sync_ohlcv_incremental(st, ohlcv_dir_id)
                            time.sleep(random.uniform(1.3, 1.6))
                        upload_json("LATEST_STAGE4_READY.json", {"status": "COMPLETED", "trigger_file": latest_s3['name']}, sys_id)
                        send_telegram("✅ *OHLCV 준비 완료*")
        else:
            print("ℹ️ 일반 스케줄 실행: OHLCV 수집을 건너뛰고 정기 데일리 수집으로 진입합니다.")

        # 🎯 [원본 로직 100% 보존] 데일리/히스토리 수집 시작
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        current_hour = now_kst.hour
        if 6 <= current_hour <= 11:
            group_label, target_chars = "1차 (A-M)", "ABCDEFGHIJKLM"
        else:
            group_label, target_chars = "2차 (N-Z & 기타)", "NOPQRSTUVWXYZ0123456789"

        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *정기 데일리 수집:* `{group_label}`")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))
        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_total, g_success, g_error = len(group_tickers), 0, 0
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))

            for i, ticker in enumerate(group_tickers, 1):
                success_flag = False
                for attempt in range(3):
                    try:
                        time.sleep(random.uniform(1.3, 1.6))
                        stock = yf.Ticker(ticker)
                        hist_status = daily_data.get(ticker, {}).get('Hist', '❌')
                        if hist_status == '❌' or is_weekend_update:
                            f_data = stock.quarterly_financials
                            if not f_data.empty:
                                hist_data[ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                                hist_status = '✅'
                        
                        info = stock.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        if price:
                            raw_record = {
                                "symbol": ticker, "name": info.get('shortName') or info.get('longName'),
                                "price": price, "currency": info.get('currency', 'USD'),
                                "marketCap": info.get('marketCap'), "updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'), "Hist": hist_status,
                                "per": info.get('trailingPE'), "pbr": info.get('priceToBook'), "psr": info.get('priceToSalesTrailing12Months'),
                                "pegRatio": info.get('pegRatio'), "targetMeanPrice": info.get('targetMeanPrice'),
                                "roe": info.get('returnOnEquity'), "roa": info.get('returnOnAssets'), "eps": info.get('trailingEps'),
                                "operatingMargins": info.get('operatingMargins'), "debtToEquity": info.get('debtToEquity'),
                                "revenueGrowth": info.get('revenueGrowth'), "operatingCashflow": info.get('operatingCashflow'),
                                "dividendRate": info.get('dividendRate', 0), "dividendYield": info.get('dividendYield', 0),
                                "volume": info.get('regularMarketVolume'), "beta": info.get('beta'),
                                "heldPercentInstitutions": info.get('heldPercentInstitutions'), "shortRatio": info.get('shortRatio'),
                                "fiftyDayAverage": info.get('fiftyDayAverage'), "twoHundredDayAverage": info.get('twoHundredDayAverage'),
                                "fiftyTwoWeekHigh": info.get('fiftyTwoWeekHigh'), "fiftyTwoWeekLow": info.get('fiftyTwoWeekLow'),
                                "sector": info.get('sector'), "industry": info.get('industry')
                            }
                            daily_data[ticker] = {k: raw_record.get(k, None) for k in STANDARD_KEYS}
                            g_success += 1
                            success_flag = True
                            break
                    except: pass
                if not success_flag: g_error += 1

            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            total_success += g_success; total_error += g_error

        send_telegram(f"🏁 *데일리 수집 종료* | 성공: `{total_success}`")

    except Exception as e:
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
