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

# 텔레그램 설정 (제공해주신 정보 반영)
TELEGRAM_TOKEN = "8468786480:AAFytUe-qHOfhsagEwTwDxn0l5vSxQbKmzs"
TELEGRAM_CHAT_ID = "1281749368"

if not RAW_SERVICE_ACCOUNT:
    raise ValueError("❌ GitHub Secrets에 GDRIVE_SERVICE_ACCOUNT가 설정되지 않았습니다.")

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 알림 및 유틸리티 함수] ---

def send_telegram(message):
    """텔레그램 메시지 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

def find_file_id(name, parent_id=None):
    """이름과 부모 ID를 기반으로 드라이브 ID 찾기"""
    query = f"name = '{name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    return results[0]['id'] if results else None

def download_json(file_id):
    """JSON 파일 다운로드"""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(fh.getvalue().decode())

def upload_json(filename, data, parent_id):
    """JSON 파일 업로드/업데이트"""
    file_id = find_file_id(filename, parent_id)
    fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
    media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
        print(f"✅ {filename} 업데이트 완료")
    else:
        meta = {'name': filename, 'parents': [parent_id]}
        drive_service.files().create(body=meta, media_body=media).execute()
        print(f"🆕 {filename} 신규 생성")

def slice_5y(data):
    """최근 5년치 재무제표 슬라이싱"""
    if not data: return {}
    for r in ['incomeStatement', 'balanceSheet', 'cashFlow']:
        if r in data:
            if 'annual' in data[r]: data[r]['annual'] = data[r]['annual'][:5]
            if 'interim' in data[r]: data[r]['interim'] = data[r]['interim'][:20]
    return data

def update_mapping_file(system_folder_id):
    """매핑 파일 최신화"""
    print("📡 MSN 사이트맵 스캔 중...")
    sitemap_url = "https://www.msn.com/staticsb/statics/latest/0/finance/sitemaps/stockdetails-en-us-sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=30)
        matches = re.findall(r'stockdetails/([^/]+)/fi-([a-z0-9]+)', resp.text)
        mapping = {t.upper(): i.lower() for t, i in matches}
        upload_json("Ticker_ID_Mapping_Final.json", mapping, system_folder_id)
        return mapping
    except Exception as e:
        print(f"⚠️ 매핑 업데이트 실패: {e}")
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", system_folder_id)
        return download_json(mapping_id) if mapping_id else {}

# --- [3. 메인 실행 로직] ---

def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    print("🔍 드라이브 경로 탐색 시작...")
    try:
        # 경로 추적: US_Alpha_Seeker > System_Identity_Maps > Financial_Data_5Y_Split
        root_id = find_file_id("US_Alpha_Seeker")
        if not root_id: raise Exception("'US_Alpha_Seeker' 폴더를 찾을 수 없습니다.")
        
        sys_id = find_file_id("System_Identity_Maps", root_id)
        if not sys_id: raise Exception("'System_Identity_Maps' 폴더를 찾을 수 없습니다.")
        
        data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
        if not data_id: raise Exception("'Financial_Data_5Y_Split' 폴더를 찾을 수 없습니다.")

        # 시간 및 업데이트 모드 설정 (KST 기준)
        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        current_hour = now_kst.hour
        day_of_week = now_kst.weekday() 
        day_of_month = now_kst.day
        
        update_mode = "DAILY" 
        if day_of_week == 5: update_mode = "WEEKLY"
        if day_of_month == 1 or day_of_month == 15: update_mode = "QUARTERLY"

        # 수집 그룹 결정 (7시 A-M, 10시 N-Z)
        if 6 <= current_hour <= 8:
            target_chars = "ABCDEFGHIJKLM"
            ticker_map = update_mapping_file(sys_id)
            send_telegram(f"🚀 *1차 수집 시작* (A-M)\n📅 모드: {update_mode}")
        else:
            target_chars = "NOPQRSTUVWXYZ0123456789"
            mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
            ticker_map = download_json(mapping_id) if mapping_id else {}
            send_telegram(f"🚀 *2차 수집 시작* (N-Z+)\n📅 모드: {update_mode}")

        storage = {}
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        for idx, (ticker, msn_id) in enumerate(ticker_map.items()):
            first_char = ticker[0].upper()
            # 그룹 필터링
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
                # 기본 정보 수집
                res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10)
                basic_data = res_a.json()[0] if res_a.status_code == 200 and res_a.json() else storage[filename].get(ticker, {}).get('basic', {})
                
                # 재무제표 수집 (QUARTERLY 모드일 때만)
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
                if success_count % 50 == 0: print(f"진행 중... {success_count}개 완료")
                time.sleep(random.uniform(0.6, 0.9))

            except Exception as e:
                print(f"⚠️ {ticker} 에러: {e}")
                error_count += 1

        # 드라이브 저장
        print("📤 드라이브 동기화 중...")
        for fname, content in storage.items():
            upload_json(fname, content, data_id)

        duration = (time.time() - start_time) / 60
        send_telegram(f"✨ *수집 완료 보고*\n- 성공: {success_count}\n- 에러: {error_count}\n- 소요: {duration:.1f}분")

    except Exception as e:
        send_telegram(f"🚨 *치명적 에러*: {str(e)}")

if __name__ == "__main__":
    run_harvester()
