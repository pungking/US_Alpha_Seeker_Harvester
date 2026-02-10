import os
import json
import requests
import uuid
import time
import datetime
import io
import urllib3
import random
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 환경 변수] ---
SERVICE_ACCOUNT_INFO = json.loads(os.getenv('GDRIVE_SERVICE_ACCOUNT'))
# 하위 데이터 폴더 이름
DATA_FOLDER_NAME = "Financial_Data_5Y_Split"
MSN_API_KEY = "0QfOX3Vn51YCzitbLaRkTTBadtWpgTN8NZLW0C1SEM"

# 구글 인증 및 서비스 빌드
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 유틸리티 함수] ---

def find_file_id(name, parent_id=None):
    """이름과 부모 ID로 파일/폴더 ID 검색"""
    query = f"name = '{name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
    return results[0]['id'] if results else None

def download_json(file_id):
    """드라이브 JSON 다운로드"""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(fh.getvalue().decode())

def upload_json(filename, data, parent_id):
    """드라이브 JSON 업로드/업데이트"""
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
    """재무제표 최근 5년치 슬라이싱"""
    if not data: return {}
    for r in ['incomeStatement', 'balanceSheet', 'cashFlow']:
        if r in data:
            if 'annual' in data[r]: data[r]['annual'] = data[r]['annual'][:5]
            if 'interim' in data[r]: data[r]['interim'] = data[r]['interim'][:20]
    return data

# --- [3. 메인 실행 함수] ---

def run_harvester():
    # 1. 상위 폴더(System_Identity_Maps) ID 찾기
    system_folder_id = find_file_id("System_Identity_Maps")
    if not system_folder_id:
        print("❌ System_Identity_Maps 폴더를 찾을 수 없습니다.")
        return

    # 2. 하위 데이터 폴더(Financial_Data_5Y_Split) ID 찾기
    data_folder_id = find_file_id(DATA_FOLDER_NAME, system_folder_id)
    
    # 3. 매핑 파일 로드
    mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", system_folder_id)
    if not mapping_id:
        print("❌ 매핑 파일을 찾을 수 없습니다.")
        return
    ticker_id_map = download_json(mapping_id)

    print(f"📡 수집 시작 (대상: {len(ticker_id_map)} 종목)")
    
    storage = {}
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    for idx, (ticker, msn_id) in enumerate(ticker_id_map.items()):
        first_char = ticker[0].upper() if ticker[0].isalpha() else 'ETC'
        filename = f"{first_char}_stocks.json"
        act_id = str(uuid.uuid4())
        
        try:
            # A. 기본 지표 + B. 재무제표 동시 수집
            res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10, verify=False)
            res_b = session.get(f"https://assets.msn.com/service/Finance/Equities/financialstatements?apikey={MSN_API_KEY}&activityId={act_id}&$filter=_p eq '{msn_id}'&wrapodata=false", timeout=15, verify=False)

            if res_a.status_code == 200 and res_b.status_code == 200:
                # 데이터 병합 및 슬라이싱
                stock_entry = {
                    "msn_id": msn_id,
                    "last_updated": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "basic": res_a.json()[0] if res_a.json() else {},
                    "history_5y": slice_5y(res_b.json())
                }

                # 알파벳별 파일 로드 및 업데이트
                if filename not in storage:
                    file_id = find_file_id(filename, data_folder_id)
                    storage[filename] = download_json(file_id) if file_id else {}
                
                storage[filename][ticker] = stock_entry
                print(f"[{idx+1}/{len(ticker_id_map)}] {ticker} 완료")

            # 안정성을 위한 지연 (기존 속도 유지)
            time.sleep(random.uniform(0.7, 1.2))

        except Exception as e:
            print(f"⚠️ {ticker} 에러: {e}")

    # 4. 최종 업로드
    print("📤 드라이브 동기화 중...")
    for fname, content in storage.items():
        upload_json(fname, content, data_folder_id)

if __name__ == "__main__":
    run_harvester()
