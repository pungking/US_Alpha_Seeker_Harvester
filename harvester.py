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

if not RAW_SERVICE_ACCOUNT:
    raise ValueError("❌ GitHub Secrets에 GDRIVE_SERVICE_ACCOUNT가 설정되지 않았습니다.")

SERVICE_ACCOUNT_INFO = json.loads(RAW_SERVICE_ACCOUNT)
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [2. 필수 유틸리티 함수] ---

def find_file_id(name, parent_id=None):
    """이름과 부모 ID를 기반으로 ID 찾기"""
    query = f"name = '{name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    results = drive_service.files().list(
        q=query, 
        fields="files(id, name)",
        spaces='drive'
    ).execute().get('files', [])
    
    return results[0]['id'] if results else None

def download_json(file_id):
    """구글 드라이브에서 JSON 파일 다운로드"""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(fh.getvalue().decode())

def upload_json(filename, data, parent_id):
    """구글 드라이브에 JSON 파일 업로드/업데이트"""
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
    """재무제표 데이터 최근 5년치로 슬라이싱"""
    if not data: return {}
    for r in ['incomeStatement', 'balanceSheet', 'cashFlow']:
        if r in data:
            if 'annual' in data[r]: data[r]['annual'] = data[r]['annual'][:5]
            if 'interim' in data[r]: data[r]['interim'] = data[r]['interim'][:20]
    return data

def update_mapping_file(system_folder_id):
    """MSN 사이트맵을 스캔하여 티커 매핑 파일 최신화"""
    print("📡 MSN 사이트맵 스캔 중... (매핑 최신화)")
    sitemap_url = "https://www.msn.com/staticsb/statics/latest/0/finance/sitemaps/stockdetails-en-us-sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=30)
        matches = re.findall(r'stockdetails/([^/]+)/fi-([a-z0-9]+)', resp.text)
        mapping = {t.upper(): i.lower() for t, i in matches}
        upload_json("Ticker_ID_Mapping_Final.json", mapping, system_folder_id)
        return mapping
    except Exception as e:
        print(f"⚠️ 매핑 업데이트 실패: {e}")
        # 실패 시 기존 파일 로드 시도
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", system_folder_id)
        return download_json(mapping_id) if mapping_id else {}

# --- [3. 메인 실행 로직] ---

def run_harvester():
    print("🔍 드라이브 경로 탐색 시작...")
    
    # 계층 구조 탐색: US_Alpha_Seeker > System_Identity_Maps > Financial_Data_5Y_Split
    root_id = find_file_id("US_Alpha_Seeker")
    if not root_id:
        print("❌ 'US_Alpha_Seeker' 폴더를 찾을 수 없습니다. 공유 설정을 확인하세요.")
        return
        
    sys_id = find_file_id("System_Identity_Maps", root_id)
    if not sys_id:
        print("❌ 'System_Identity_Maps' 폴더를 찾을 수 없습니다.")
        return
        
    data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
    if not data_id:
        print("❌ 'Financial_Data_5Y_Split' 폴더를 찾을 수 없습니다.")
        return

    print(f"✅ 경로 확인 완료! (Data Folder ID: {data_id})")

    # 시간 및 주기 설정
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    current_hour = now_kst.hour
    day_of_week = now_kst.weekday() 
    day_of_month = now_kst.day
    
    # 업데이트 모드 결정
    update_mode = "DAILY" 
    if day_of_week == 5: update_mode = "WEEKLY" # 토요일: 지표 위주
    if day_of_month == 1 or day_of_month == 15: update_mode = "QUARTERLY" # 재무제표 갱신

    # 차수 판별 (7시: A-M / 10시: N-Z)
    if 6 <= current_hour <= 8:
        target_chars = "ABCDEFGHIJKLM"
        print(f"⏰ 1차 업데이트 시작 (그룹: {target_chars})")
        ticker_map = update_mapping_file(sys_id)
    else:
        target_chars = "NOPQRSTUVWXYZ0123456789"
        print(f"⏰ 2차 업데이트 시작 (그룹: {target_chars})")
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        ticker_map = download_json(mapping_id) if mapping_id else {}

    storage = {}
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    for idx, (ticker, msn_id) in enumerate(ticker_map.items()):
        first_char = ticker[0].upper()
        # 해당 회차 타겟이 아니면 패스
        is_etc = not first_char.isalpha()
        if is_etc:
            if "0123456789" not in target_chars: continue
        else:
            if first_char not in target_chars: continue

        filename = f"{first_char if first_char.isalpha() else 'ETC'}_stocks.json"
        
        try:
            # 1. 기존 데이터 로드 (재무제표 보존용)
            if filename not in storage:
                fid = find_file_id(filename, data_id)
                storage[filename] = download_json(fid) if fid else {}

            # 2. 데이터 수집 (Basic + Financials)
            act_id = str(uuid.uuid4())
            res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10)
            
            # 기본 데이터 업데이트 (실패 시 기존 데이터 유지)
            basic_data = res_a.json()[0] if res_a.status_code == 200 and res_a.json() else storage[filename].get(ticker, {}).get('basic', {})
            history_data = storage[filename].get(ticker, {}).get('history_5y', {})

            # 특정 주기에만 재무제표(손익/대차/현금) 수집
            if update_mode == "QUARTERLY":
                res_b = session.get(f"https://assets.msn.com/service/Finance/Equities/financialstatements?apikey={MSN_API_KEY}&activityId={act_id}&$filter=_p eq '{msn_id}'&wrapodata=false", timeout=15)
                if res_b.status_code == 200:
                    history_data = slice_5y(res_b.json())

            # 3. 데이터 병합
            storage[filename][ticker] = {
                "msn_id": msn_id,
                "last_updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'),
                "update_mode": update_mode,
                "basic": basic_data,
                "history_5y": history_data
            }
            
            if (idx + 1) % 10 == 0:
                print(f" 진행 중... ({idx+1}/{len(ticker_map)}) 현재: {ticker}")
                
            time.sleep(random.uniform(0.6, 0.9)) # 안전 속도 유지

        except Exception as e:
            print(f"⚠️ {ticker} 수집 중 에러: {e}")

    # 4. 최종 파일 저장
    print("📤 드라이브 동기화 시작...")
    for fname, content in storage.items():
        upload_json(fname, content, data_id)
    print("✨ 모든 작업이 완료되었습니다.")

if __name__ == "__main__":
    run_harvester()
