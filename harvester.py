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
# cache_discovery=False로 네트워크 지연 최소화
drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)

# --- [2. 드라이브 유틸리티 - 에러 방어형] ---
def find_file_id(name, parent_id=None):
    for _ in range(3):
        try:
            query = f"name = '{name}' and trashed = false"
            if parent_id: query += f" and '{parent_id}' in parents"
            results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
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
                drive_service.files().update(fileId=file_id, media_body=media).execute()
            else:
                meta = {'name': filename, 'parents': [parent_id]}
                drive_service.files().create(body=meta, media_body=media).execute()
            print(f"✅ 업로드 완료: {filename}")
            return
        except Exception as e:
            print(f"   ⚠️ 업로드 실패 ({attempt+1}/5): {str(e)}")
            time.sleep(10)

# --- [신규 추가: OHLCV 누적 수집 및 병합 로직] ---
def sync_ohlcv_incremental(ticker, ohlcv_dir_id):
    """구글 드라이브의 기존 OHLCV 데이터를 찾아 신규 데이터만 추가합니다."""
    file_name = f"{ticker}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    existing_data = download_json(file_id) if file_id else []

    try:
        stock = yf.Ticker(ticker)
        # 데이터가 있으면 최근 7일치만(중복방지), 없으면 1년치를 가져옵니다.
        period = "7d" if existing_data else "1y"
        df = stock.history(period=period, interval="1d")
        
        if df.empty: return False

        new_records = []
        for date, row in df.iterrows():
            new_records.append({
                "date": date.strftime('%Y-%m-%d'),
                "open": round(row['Open'], 2),
                "high": round(row['High'], 2),
                "low": round(row['Low'], 2),
                "close": round(row['Close'], 2),
                "volume": int(row['Volume'])
            })

        if existing_data:
            # 날짜를 키로 사용하여 중복 제거 병합
            combined = {item['date']: item for item in existing_data + new_records}
            final_data = sorted(combined.values(), key=lambda x: x['date'])
        else:
            final_data = new_records

        upload_json(file_name, final_data, ohlcv_dir_id)
        return True
    except:
        return False

# --- [3. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    total_success, total_error = 0, 0
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)

    current_hour = now_kst.hour
    if 6 <= current_hour <= 11:
        group_label, target_chars = "1차 (A-M)", "ABCDEFGHIJKLM"
    else:
        group_label, target_chars = "2차 (N-Z & 기타)", "NOPQRSTUVWXYZ0123456789"

    try:
        print(f"🔍 시스템 가동: {today_str}")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        # OHLCV 폴더 확인 및 생성
        ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
        if not ohlcv_dir_id:
            meta = {'name': 'Financial_Data_OHLCV', 'parents': [sys_id], 'mimeType': 'application/vnd.google-apps.folder'}
            ohlcv_dir_id = drive_service.files().create(body=meta, fields='id').execute().get('id')

        # --- [추가 로직: Stage 3 기반 트리거 및 OHLCV 수집] ---
        # 드라이브에서 가장 최신 STAGE3 파일을 찾습니다.
        query = "name contains 'STAGE3_FUNDAMENTAL_FULL_' and trashed = false"
        s3_files = drive_service.files().list(q=query, fields="files(id, name, createdTime)", orderBy="createdTime desc").execute().get('files', [])
        
        if s3_files:
            latest_s3 = s3_files[0]
            # 처리 여부 확인 (신호 파일의 trigger_file 필드와 대조)
            ready_id = find_file_id("LATEST_STAGE4_READY.json", sys_id)
            ready_info = download_json(ready_id) if ready_id else {}
            
            if ready_info.get("trigger_file") != latest_s3['name']:
                print(f"💎 신규 Stage 3 탐지: {latest_s3['name']}")
                s3_content = download_json(latest_s3['id'])
                s3_tickers = [item['symbol'] for item in s3_content.get('fundamental_universe', [])]
                
                if s3_tickers:
                    send_telegram(f"🔍 *Stage 3 트리거 확인:* `{len(s3_tickers)}`종목 OHLCV 수집 개시")
                    s3_success = 0
                    for st in s3_tickers:
                        if sync_ohlcv_incremental(st, ohlcv_dir_id):
                            s3_success += 1
                        time.sleep(random.uniform(1.3, 1.6))
                    
                    # 수집 완료 신호 플래그 생성 (웹앱용)
                    signal = {
                        "status": "READY",
                        "stage": 4,
                        "trigger_file": latest_s3['name'],
                        "timestamp": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "count": s3_success
                    }
                    upload_json("LATEST_STAGE4_READY.json", signal, sys_id)
                    send_telegram(f"✅ *Stage 4 준비 완료*\n누적 수집 성공: `{s3_success}` 종목")
            else:
                print("ℹ️ 최신 Stage 3 파일이 이미 처리되었습니다.")

        # --- [기존 데일리 수집 로직] ---
        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *[US Alpha Seeker] 가동*\n🎯 *타겟:* `{group_label}`\n📊 *종목:* `{len(filtered_tickers)}` | `28필드` (HF Edition)")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_total, g_success, g_error = len(group_tickers), 0, 0
            print(f"\n--- 📦 그룹 [{group}] 작업 시작 ---")
            
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))

            for i, ticker in enumerate(group_tickers, 1):
                success_flag = False
                for attempt in range(3):
                    try:
                        if i % 50 == 0: print(f"   > 진행 중: {group} {i}/{g_total}...")
                        time.sleep(random.uniform(1.3, 1.6))
                        stock = yf.Ticker(ticker)
                        
                        hist_status = daily_data.get(ticker, {}).get('Hist', '❌')
                        if hist_status == '❌' or is_weekend_update:
                            try:
                                f_data = stock.quarterly_financials
                                if not f_data.empty:
                                    hist_data[ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                                    hist_status = '✅'
                            except: pass

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
                        else: break
                    except Exception as e:
                        if "SSL" in str(e) or "EOF" in str(e): time.sleep(5)
                
                if not success_flag: g_error += 1

            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            total_success += g_success; total_error += g_error
            send_telegram(f"📦 *그룹 [{group}] 완료*\n✅ 성공: `{g_success}` | ❌ 실패: `{g_error}`")

        duration = (time.time() - start_time) / 60
        send_telegram(f"🏁 *전체 수집 종료*\n⏱️ `{duration:.1f}분` | 성공: `{total_success}` | 실패: `{total_error}`")

    except Exception as e:
        send_telegram(f"🚨 *치명적 에러:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
