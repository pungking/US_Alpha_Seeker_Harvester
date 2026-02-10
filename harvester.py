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

# 텔레그램 설정 (정확한지 다시 한 번 확인 필수)
TELEGRAM_TOKEN = "8468786480:AAFytUe-qHOfhsagEwTwDxn0l5vSxQbKmzs"
TELEGRAM_CHAT_ID = "1281749368"

def send_telegram(message):
    """텔레그램 메시지 전송 및 결과 반환"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"❌ 텔레그램 전송 실패 (상태 코드: {response.status_code}): {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"❌ 텔레그램 연결 에러: {e}")
        return False

# --- [인증 체크] ---
if not RAW_SERVICE_ACCOUNT:
    err_msg = "❌ GDRIVE_SERVICE_ACCOUNT가 설정되지 않았습니다."
    print(err_msg)
    send_telegram(err_msg)
    exit(1)

try:
    SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
    creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    err_msg = f"❌ 인증 정보 로드 실패 (JSON 형식 확인 필요): {e}"
    print(err_msg)
    send_telegram(err_msg)
    exit(1)

# --- [2. 유틸리티 함수] ---

def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    return results[0]['id'] if results else None

def download_json(file_id):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
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
    
    # 텔레그램 테스트용 시작 알림
    print("📡 텔레그램 연결 테스트 중...")
    if not send_telegram("🤖 수집기가 정상적으로 가동되었습니다. 경로 탐색을 시작합니다."):
        print("⚠️ 텔레그램 알림이 작동하지 않습니다. 로그를 확인하세요.")

    try:
        # 경로 탐색
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
        
        if not data_id: raise Exception("구글 드라이브 폴더 구조 확인 실패")

        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        current_hour = now_kst.hour
        update_mode = "DAILY" 
        if now_kst.weekday() == 5: update_mode = "WEEKLY"
        if now_kst.day in [1, 15]: update_mode = "QUARTERLY"

        # 수집 그룹 결정
        if 6 <= current_hour <= 8:
            target_chars = "ABCDEFGHIJKLM"
            ticker_map = {} # 매핑 업데이트 로직 필요 시 추가
        else:
            target_chars = "NOPQRSTUVWXYZ0123456789"
            mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
            ticker_map = download_json(mapping_id) if mapping_id else {}

        storage = {}
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        for ticker, msn_id in ticker_map.items():
            first_char = ticker[0].upper()
            if not first_char.isalpha():
                if "0123456789" not in target_chars: continue
            elif first_char not in target_chars:
                continue

            filename = f"{first_char if first_char.isalpha() else 'ETC'}_stocks.json"
            
            try:
                if filename not in storage:
                    fid = find_file_id(filename, data_id)
                    storage[filename] = download_json(fid) if fid else {}

                act_id = str(uuid.uuid4())
                res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10)
                basic_data = res_a.json()[0] if res_a.status_code == 200 and res_a.json() else storage[filename].get(ticker, {}).get('basic', {})
                
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
                time.sleep(random.uniform(0.6, 0.9))
                if success_count % 100 == 0: print(f"진행 중: {success_count}개 완료")

            except Exception as e:
                error_count += 1

        # 저장 및 최종 알림
        for fname, content in storage.items():
            upload_json(fname, content, data_id)

        duration = (time.time() - start_time) / 60
        send_telegram(f"✨ *수집 완료 보고*\n- 성공: {success_count}\n- 에러: {error_count}\n- 소요: {duration:.1f}분")

    except Exception as e:
        send_telegram(f"🚨 *치명적 에러*: {str(e)}")
        print(f"🚨 에러 상세: {e}")

if __name__ == "__main__":
    run_harvester()
