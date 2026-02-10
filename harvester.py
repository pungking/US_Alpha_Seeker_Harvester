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

if not RAW_SERVICE_ACCOUNT:
    raise ValueError("❌ GitHub Secrets에 GDRIVE_SERVICE_ACCOUNT가 설정되지 않았습니다.")

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 유틸리티 함수] ---

def send_telegram(message):
    """텔레그램 메시지 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

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
        print(f"✅ {filename} 업데이트 완료")
    else:
        meta = {'name': filename, 'parents': [parent_id]}
        drive_service.files().create(body=meta, media_body=media).execute()
        print(f"🆕 {filename} 신규 생성")

def slice_5y(data):
    if not data: return {}
    for r in ['incomeStatement', 'balanceSheet', 'cashFlow']:
        if r in data:
            if 'annual' in data[r]: data[r]['annual'] = data[r]['annual'][:5]
            if 'interim' in data[r]: data[r]['interim'] = data[r]['interim'][:20]
    return data

def update_mapping_file(system_folder_id):
    print("📡 MSN 사이트맵 스캔 중...")
    sitemap_url = "https://www.msn.com/staticsb/statics/latest/0/finance/sitemaps/stockdetails-en-us-sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=30)
        matches = re.findall(r'stockdetails/([^/]+)/fi-([a-z0-9]+)', resp.text)
        mapping = {t.upper(): i.lower() for t, i in matches}
        upload_json("Ticker_ID_Mapping_Final.json", mapping, system_folder_id)
        return mapping
    except Exception as e:
        send_telegram(f"⚠️ 매핑 최신화 실패: {str(e)}")
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", system_folder_id)
        return download_json(mapping_id) if mapping_id else {}

# --- [3. 메인 실행 로직] ---

def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    try:
        # 1. 드라이브 경로 탐색
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        data_id = find_file_id("Financial_Data_5Y_Split", sys_id)

        if not data_id:
            raise Exception("구글 드라이브 폴더 구조를 찾을 수 없습니다.")

        # 2. 시간 및 모드 설정
        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        current_hour = now_kst.hour
        update_mode = "DAILY" 
        if now_kst.weekday() == 5: update_mode = "WEEKLY"
        if now_kst.day in [1, 15]: update_mode = "QUARTERLY"

        # 3. 수집 그룹 판별
        if 6 <= current_hour <= 8:
            target_chars = "ABCDEFGHIJKLM"
            ticker_map = update_mapping_file(sys_id)
        else:
            target_chars = "NOPQRSTUVWXYZ0123456789"
            mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
            ticker_map = download_json(mapping_id) if mapping_id else {}

        # 시작 메시지는 이미 확인하셨으므로, 로깅으로 대체하거나 간소화
        print(f"🚀 수집 시작: {target_chars} (대상: {len(ticker_map)}개)")

        storage = {}
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        # 4. 데이터 수집 루프
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

            except Exception as e:
                print(f"⚠️ {ticker} 에러: {e}")
                error_count += 1

        # 5. 데이터 저장 (이 과정에서 알림 누락 방지를 위해 루프 밖 처리)
        print("📤 드라이브 저장 중...")
        for fname, content in storage.items():
            upload_json(fname, content, data_id)

        # 6. 최종 완료 보고서 발송
        duration = (time.time() - start_time) / 60
        summary = (
            f"🏁 *수집 완료 보고*\n"
            f"━━━━━━━━━━━━━━\n"
            f"📅 모드: {update_mode}\n"
            f"✅ 성공: {success_count} 건\n"
            f"❌ 에러: {error_count} 건\n"
            f"⏱️ 소요: {duration:.1f} 분\n"
            f"━━━━━━━━━━━━━━"
        )
        send_telegram(summary)

    except Exception as e:
        # 에러가 발생하면 무조건 텔레그램 발송
        send_telegram(f"🚨 *치명적 에러 발생*\n{str(e)}")
        raise e

if __name__ == "__main__":
    run_harvester()
