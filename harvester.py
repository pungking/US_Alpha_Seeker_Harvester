import os
import json
import requests
import uuid
import time
import datetime
import io
import urllib3
import random
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 인증] ---
RAW_SERVICE_ACCOUNT = os.getenv('GDRIVE_SERVICE_ACCOUNT')
MSN_API_KEY = "0QfOX3Vn51YCzitbLaRkTTBadtWpgTN8NZLW0C1SEM"

# 텔레그램 설정
TELEGRAM_TOKEN = "8468786480:AAFytUe-qHOfhsagEwTwDxn0l5vSxQbKmzs"
TELEGRAM_CHAT_ID = "1281749368"

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

# --- [2. 유틸리티 함수] ---

def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
    return results[0]['id'] if results else None

def download_json(file_id):
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

def slice_5y(data):
    if not data: return {}
    for r in ['incomeStatement', 'balanceSheet', 'cashFlow']:
        if r in data:
            if 'annual' in data[r]: data[r]['annual'] = data[r]['annual'][:5]
            if 'interim' in data[r]: data[r]['interim'] = data[r]['interim'][:20]
    return data

# --- [3. 메인 로직] ---

def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    send_telegram("🤖 *수집기 가동 시작*")

    try:
        # 경로 탐색
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
        
        # 시간 및 모드 설정
        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        update_mode = "DAILY" 
        if now_kst.weekday() == 5: update_mode = "WEEKLY"
        if now_kst.day in [1, 15]: update_mode = "QUARTERLY"

        # 그룹 판별 및 필터링
        current_hour = now_kst.hour
        target_chars = "ABCDEFGHIJKLM" if 6 <= current_hour <= 8 else "NOPQRSTUVWXYZ0123456789"
        
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(mapping_id) if mapping_id else {}
        
        filtered_map = {t: i for t, i in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"🚀 *수집 프로세스 시작*\n- 그룹: {target_chars}\n- 모드: {update_mode}\n- 대상: {len(filtered_map)} 종목")

        storage = {}
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})

        # 데이터 수집 루프
        for ticker, msn_id in filtered_map.items():
            first_char = ticker[0].upper()
            filename = f"{first_char if first_char.isalpha() else 'ETC'}_stocks.json"
            
            try:
                if filename not in storage:
                    fid = find_file_id(filename, data_id)
                    storage[filename] = download_json(fid) if fid else {}

                act_id = str(uuid.uuid4())
                # MSN 기본 데이터 수집
                res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10)
                
                if res_a.status_code == 200:
                    basic_data = res_a.json()[0]
                    history_data = storage[filename].get(ticker, {}).get('history_5y', {})
                    
                    if update_mode == "QUARTERLY":
                        res_b = session.get(f"https://assets.msn.com/service/Finance/Equities/financialstatements?apikey={MSN_API_KEY}&activityId={act_id}&$filter=_p eq '{msn_id}'&wrapodata=false", timeout=15)
                        if res_b.status_code == 200:
                            history_data = slice_5y(res_b.json())

                    storage[filename][ticker] = {
                        "msn_id": msn_id,
                        "last_updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'),
                        "basic": basic_data,
                        "history_5y": history_data
                    }
                    success_count += 1
                
                # 🛡️ 밴 방지를 위해 안전한 슬립 유지 (0.7 ~ 1.1초)
                time.sleep(random.uniform(0.7, 1.1))

                if success_count % 500 == 0:
                    print(f"🔄 현재 {success_count}개 수집 중...")

            except Exception:
                error_count += 1

        # --- [저장 로직] ---
        send_telegram(f"📤 *저장 단계 진입*: {success_count}개 데이터를 드라이브에 기록합니다.")
        
        for fname, content in storage.items():
            try:
                upload_json(fname, content, data_id)
                send_telegram(f"✅ 파일 저장 완료: `{fname}`")
                time.sleep(2) # 구글 API 과부하 방지
            except Exception as e:
                send_telegram(f"⚠️ `{fname}` 저장 중 오류 발생: {str(e)}")

        # 최종 보고
        duration = (time.time() - start_time) / 60
        summary = (
            f"✨ *최종 수집 완료 보고*\n"
            f"━━━━━━━━━━━━━━\n"
            f"✅ 성공: {success_count}\n"
            f"❌ 에러: {error_count}\n"
            f"⏱️ 소요: {duration:.1f}분\n"
            f"━━━━━━━━━━━━━━"
        )
        send_telegram(summary)

    except Exception as e:
        send_telegram(f"🚨 *시스템 중단 에러*: {str(e)}")

if __name__ == "__main__":
    run_harvester()
