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
    # 네트워크 문제로 API 실패 시 최대 3번 시도
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
    # SSL 에러(EOF) 발생 지점을 방어하기 위한 5회 재시도
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
            time.sleep(10) # 실패 시 10초 대기 후 재시도

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
                for attempt in range(3): # 수집 재시도
                    try:
                        if i % 50 == 0: print(f"   > 진행 중: {group} {i}/{g_total}...")
                        time.sleep(random.uniform(1.3, 1.6))
                        stock = yf.Ticker(ticker)
                        
                        # 재무 데이터(History)
                        hist_status = daily_data.get(ticker, {}).get('Hist', '❌')
                        if hist_status == '❌' or is_weekend_update:
                            try:
                                f_data = stock.quarterly_financials
                                if not f_data.empty:
                                    hist_data[ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                                    hist_status = '✅'
                            except: pass

                        # 28개 필드 수집 (사용자님 원본 필드 그대로 유지)
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

            # 이 부분에서 에러가 나도 upload_json 내의 5회 재시도가 방어함
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
