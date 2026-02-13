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

# 사용자님이 정하신 데이터 규격 그대로 유지
STANDARD_KEYS = ["symbol", "name", "price", "currency", "marketCap", "per", "pbr", "psr", "roe", "eps", "volume", "beta", "dividendYield", "sector", "industry", "updated", "Hist"]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

if not RAW_SERVICE_ACCOUNT:
    print("❌ 서비스 계정 설정 없음")
    exit(1)

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

# --- [3. 메인 하베스터 로직] ---
def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d')
    
    send_telegram(f"🤖 *Alpha Seeker v2 가동* ({today_str})")

    try:
        # 폴더 구조 확인
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        # 매핑 로드
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(mapping_id)

        # 수집 부하 분산을 위한 시간별 필터링 (기존 로직 유지)
        current_hour = now_kst.hour
        target_chars = "ABCDEFGHIJKLM" if 6 <= current_hour <= 9 else "NOPQRSTUVWXYZ0123456789"
        
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}
        
        send_telegram(f"🚀 *수집 시작*: {len(filtered_tickers)} 종목 대상")

        # 메모리 내 저장소 (그룹별 캐싱)
        daily_storage = {}
        hist_storage = {}

        for ticker in filtered_tickers:
            group = filtered_tickers[ticker]['group']
            daily_name = f"{group}_stocks_daily.json"
            hist_name = f"{group}_stocks_history.json"

            # 1. 파일 캐싱 (없을 때만 다운로드)
            if daily_name not in daily_storage:
                fid = find_file_id(daily_name, daily_dir_id)
                daily_storage[daily_name] = download_json(fid) if fid else {}
            if hist_name not in hist_storage:
                fid = find_file_id(hist_name, hist_dir_id)
                hist_storage[hist_name] = download_json(fid) if fid else {}

            # 2. Yahoo Finance 데이터 수집
            try:
                time.sleep(random.uniform(1.2, 1.6)) # 안전한 딜레이
                stock = yf.Ticker(ticker)
                
                # [히스토리 보완 체크]
                hist_status = daily_storage[daily_name].get(ticker, {}).get('Hist', '❌')
                if hist_status == '❌':
                    try:
                        f_data = stock.quarterly_financials
                        if not f_data.empty:
                            hist_storage[hist_name][ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                            hist_status = '✅'
                    except: pass

                # [데일리 업데이트]
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
                
                if success_count % 200 == 0:
                    print(f"🔄 진행 중: {success_count}개 수집 완료...")

            except Exception as e:
                print(f"⚠️ {ticker} 에러: {e}")
                error_count += 1

        # 3. 드라이브 업데이트 (Daily & History)
        send_telegram(f"📤 *저장 단계*: 최신 데이터 업로드 중...")
        
        for d_name, d_content in daily_storage.items():
            upload_json(d_name, d_content, daily_dir_id)
            time.sleep(0.5)
            
        for h_name, h_content in hist_storage.items():
            upload_json(h_name, h_content, hist_dir_id)
            time.sleep(0.5)

        duration = (time.time() - start_time) / 60
        send_telegram(f"✨ *최종 완료 보고*\n✅ 성공: {success_count}\n❌ 실패: {error_count}\n⏱️ 소요: {duration:.1f}분\n모든 지표가 최신으로 유지되었습니다.")

    except Exception as e:
        send_telegram(f"🚨 하베스터 치명적 에러: {str(e)}")

if __name__ == "__main__":
    run_harvester()
