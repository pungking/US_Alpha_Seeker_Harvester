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

# [최종 확정] 해지펀드급 28개 분석 필드 (Quantum Jump)
STANDARD_KEYS = [
    # 1. 기본 정보 & 가격
    "symbol", "name", "price", "currency", "marketCap", "updated", "Hist",
    # 2. 밸류에이션 (Value)
    "per", "pbr", "psr", "pegRatio", "targetMeanPrice",
    # 3. 수익성 & 효율성 (Quality)
    "roe", "roa", "eps", "operatingMargins", "debtToEquity",
    # 4. 성장성 & 현금흐름 (Growth & Cash)
    "revenueGrowth", "operatingCashflow",
    # 5. 주주 환원 (Dividend)
    "dividendRate", "dividendYield",
    # 6. 수급 & 추세 (Momentum & Sentiment)
    "volume", "beta", "heldPercentInstitutions", "shortRatio",
    "fiftyDayAverage", "twoHundredDayAverage", 
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    # 7. 메타 데이터
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
    print(f"📤 업로드 중: {filename}...")
    file_id = find_file_id(filename, parent_id)
    fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
    media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': filename, 'parents': [parent_id]}
        drive_service.files().create(body=meta, media_body=media).execute()
    print(f"✅ 업로드 완료: {filename}")

# --- [3. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    total_success, total_error = 0, 0
    
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)
    
    current_hour = now_kst.hour
    if 6 <= current_hour <= 11:
        group_label = "1차 수집 (A-M)"
        target_chars = "ABCDEFGHIJKLM"
    else:
        group_label = "2차 수집 (N-Z & 기타)"
        target_chars = "NOPQRSTUVWXYZ0123456789"

    try:
        print(f"🔍 시스템 가동: {today_str}")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *[US Alpha Seeker] 엔진 가동*\n🎯 *타겟:* `{group_label}`\n📊 *종목:* `{len(filtered_tickers)}` | *필드:* `28개` (Hedge Fund Edition)")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_total, g_success, g_error = len(group_tickers), 0, 0
            
            print(f"\n--- 📦 그룹 [{group}] 시작 ({g_total}개) ---")
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))

            for i, ticker in enumerate(group_tickers, 1):
                try:
                    if i % 50 == 0: print(f"   > 진행 중: {group} {i}/{g_total}...")
                    time.sleep(random.uniform(1.1, 1.3))
                    stock = yf.Ticker(ticker)
                    
                    # History 수집
                    hist_status = daily_data.get(ticker, {}).get('Hist', '❌')
                    if hist_status == '❌' or is_weekend_update:
                        try:
                            f_data = stock.quarterly_financials
                            if not f_data.empty:
                                hist_data[ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                                hist_status = '✅'
                        except: pass

                    # Daily 수집 (28개 필드 - Hedge Fund Logic)
                    info = stock.info
                    price = info.get('currentPrice') or info.get('regularMarketPrice')
                    
                    if price:
                        raw_record = {
                            # 1. 기본
                            "symbol": ticker,
                            "name": info.get('shortName') or info.get('longName'),
                            "price": price,
                            "currency": info.get('currency', 'USD'),
                            "marketCap": info.get('marketCap'),
                            "updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'),
                            "Hist": hist_status,
                            
                            # 2. 밸류에이션
                            "per": info.get('trailingPE'),
                            "pbr": info.get('priceToBook'),
                            "psr": info.get('priceToSalesTrailing12Months'),
                            "pegRatio": info.get('pegRatio'), # New: 성장성 대비 가치
                            "targetMeanPrice": info.get('targetMeanPrice'),
                            
                            # 3. 수익성 & 효율성
                            "roe": info.get('returnOnEquity'),
                            "roa": info.get('returnOnAssets'), # New: 자산 효율성
                            "eps": info.get('trailingEps'),
                            "operatingMargins": info.get('operatingMargins'), # New: 영업이익률
                            "debtToEquity": info.get('debtToEquity'),
                            
                            # 4. 성장성 & 현금
                            "revenueGrowth": info.get('revenueGrowth'), # New: 매출 성장률
                            "operatingCashflow": info.get('operatingCashflow'), # New: 영업 현금흐름
                            
                            # 5. 배당
                            "dividendRate": info.get('dividendRate', 0),
                            "dividendYield": info.get('dividendYield', 0),
                            
                            # 6. 수급 & 추세
                            "volume": info.get('regularMarketVolume'),
                            "beta": info.get('beta'),
                            "heldPercentInstitutions": info.get('heldPercentInstitutions'),
                            "shortRatio": info.get('shortRatio'), # New: 공매도 비율
                            "fiftyDayAverage": info.get('fiftyDayAverage'), # New: 50일 이평선 (단기 추세)
                            "twoHundredDayAverage": info.get('twoHundredDayAverage'), # New: 200일 이평선 (장기 추세)
                            "fiftyTwoWeekHigh": info.get('fiftyTwoWeekHigh'),
                            "fiftyTwoWeekLow": info.get('fiftyTwoWeekLow'),
                            
                            # 7. 메타
                            "sector": info.get('sector'),
                            "industry": info.get('industry')
                        }
                        
                        # 28개 필드 매핑 및 저장
                        daily_data[ticker] = {k: raw_record.get(k, None) for k in STANDARD_KEYS}
                        g_success += 1
                    else: g_error += 1
                except: g_error += 1

            # 업로드
            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            total_success += g_success; total_error += g_error
            
            send_telegram(f"📦 *그룹 [{group}] 완료*\n✅ 성공: `{g_success}` | ❌ 실패: `{g_error}`\n📊 합계: `{g_total}` (Sync OK)")

        duration = (time.time() - start_time) / 60
        send_telegram(f"🏁 *전체 수집 종료*\n⏱️ `{duration:.1f}분` | 성공: `{total_success}` | 실패: `{total_error}`")

    except Exception as e:
        print(f"🚨 에러: {str(e)}")
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
