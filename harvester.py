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

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 인증] ---
RAW_SERVICE_ACCOUNT = os.getenv('GDRIVE_SERVICE_ACCOUNT')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# 데이터 표준 규격 (17개 핵심 필드)
STANDARD_KEYS = [
    "symbol", "name", "price", "currency", "marketCap", "per", "pbr", 
    "psr", "roe", "eps", "volume", "beta", "dividendYield", 
    "sector", "industry", "updated", "Hist"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

if not RAW_SERVICE_ACCOUNT:
    print("❌ 서비스 계정 설정이 없습니다."); exit(1)

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 구글 드라이브 유틸리티 함수] ---
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

# --- [3. 메인 하베스터 엔진] ---
def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    # 시간 및 모드 설정
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5) # 토요일 정기 점검 여부
    
    # 수집 그룹 판별
    current_hour = now_kst.hour
    if 6 <= current_hour <= 9:
        group_label = "1차 그룹 (A-M)"
        target_chars = "ABCDEFGHIJKLM"
    else:
        group_label = "2차 그룹 (N-Z & 기타)"
        target_chars = "NOPQRSTUVWXYZ0123456789"

    try:
        # 폴더 ID 확보
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(mapping_id)

        # 수집 대상 필터링
        filtered_tickers = {t: info for t, info in full_map.items() 
                           if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        # 시작 메시지 전송
        start_msg = (
            f"📡 *[US Alpha Seeker] 수집 가동*\n"
            f"📅 *일시:* `{today_str}`\n"
            f"🎯 *대상:* `{group_label}`\n"
            f"📊 *규모:* `{len(filtered_tickers)}개 종목`\n"
            f"🛠️ *모드:* `가격/지표 전수 최신화` + `재무 {'전수 갱신' if is_weekend_update else '누락 보완'}`"
        )
        send_telegram(start_msg)

        daily_storage = {}
        hist_storage = {}

        for ticker in filtered_tickers:
            group = filtered_tickers[ticker]['group']
            daily_name = f"{group}_stocks_daily.json"
            hist_name = f"{group}_stocks_history.json"

            # 메모리 로드
            if daily_name not in daily_storage:
                fid = find_file_id(daily_name, daily_dir_id)
                daily_storage[daily_name] = download_json(fid) if fid else {}
            if hist_name not in hist_storage:
                fid = find_file_id(hist_name, hist_dir_id)
                hist_storage[hist_name] = download_json(fid) if fid else {}

            # 수집 실행
            try:
                time.sleep(random.uniform(1.2, 1.6))
                stock = yf.Ticker(ticker)
                
                # History (재무제표) 보완/갱신
                hist_status = daily_storage[daily_name].get(ticker, {}).get('Hist', '❌')
                if hist_status == '❌' or is_weekend_update:
                    try:
                        f_data = stock.quarterly_financials
                        if not f_data.empty:
                            hist_storage[hist_name][ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                            hist_status = '✅'
                    except: pass

                # Daily (실시간 가격 및 지표) 수집
                info = stock.info
                price = info.get('currentPrice') or info.get('regularMarketPrice')
                
                if price:
                    raw_info = {
                        "symbol": ticker,
                        "name": info.get('shortName') or info.get('longName'),
                        "price": price,
                        "currency": info.get('currency', 'USD'),
                        "marketCap": info.get('marketCap'),
                        "per": info.get('trailingPE'),
                        "pbr": info.get('priceToBook'),
                        "psr": info.get('priceToSalesTrailing12Months'),
                        "roe": info.get('returnOnEquity'),
                        "eps": info.get('trailingEps'),
                        "volume": info.get('regularMarketVolume'),
                        "beta": info.get('beta'),
                        "dividendYield": info.get('dividendYield', 0),
                        "sector": info.get('sector'),
                        "industry": info.get('industry'),
                        "updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'),
                        "Hist": hist_status
                    }
                    daily_storage[daily_name][ticker] = {k: raw_info.get(k, None) for k in STANDARD_KEYS}
                    success_count += 1
                
                if success_count % 300 == 0:
                    print(f"🔄 진행 중: {success_count}개 완료...")

            except Exception as e:
                error_count += 1

        # 저장 단계
        send_telegram(f"📤 *데이터 업로드:* `{len(daily_storage)}개` 그룹 파일 갱신 중...")
        
        for d_name, d_content in daily_storage.items():
            upload_json(d_name, d_content, daily_dir_id)
            time.sleep(0.5)
        for h_name, h_content in hist_storage.items():
            upload_json(h_name, h_content, hist_dir_id)
            time.sleep(0.5)

        # 종료 보고
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
