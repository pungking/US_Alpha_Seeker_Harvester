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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [설정 환경 변수] ---
SERVICE_ACCOUNT_INFO = json.loads(os.getenv('GDRIVE_SERVICE_ACCOUNT'))
MSN_API_KEY = "0QfOX3Vn51YCzitbLaRkTTBadtWpgTN8NZLW0C1SEM"

creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO)
drive_service = build('drive', 'v3', credentials=creds)

# --- [유틸리티 함수: find_file_id, download_json, upload_json, slice_5y 등은 기존과 동일] ---

def run_harvester():
    # 1. 경로 파악
    sys_id = find_file_id("System_Identity_Maps")
    data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
    
    # 2. 시간 및 날짜 판별 (한국 시간 기준)
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    current_hour = now_kst.hour
    day_of_week = now_kst.weekday() # 0:월, 6:일
    day_of_month = now_kst.day
    
    # 3. 업데이트 모드 결정 로직
    # 기본적으로 매일 가격은 수집 (DAILY)
    # 매주 토요일(장 마감 후)에는 지표 업데이트 (WEEKLY)
    # 매월 1일 또는 분기 시작 시점에는 재무제표 업데이트 (QUARTERLY)
    update_mode = "DAILY" 
    if day_of_week == 5: # 토요일
        update_mode = "WEEKLY"
    if day_of_month == 1 or day_of_month == 15: # 매월 1일과 15일 (재무제표 갱신)
        update_mode = "QUARTERLY"

    # 4. 차수 판별 (7시: A-M / 10시: N-Z)
    if 6 <= current_hour <= 8:
        target_chars = "ABCDEFGHIJKLM"
        update_mapping_file(sys_id) # 1회차에 매핑 최신화
    else:
        target_chars = "NOPQRSTUVWXYZ0123456789"

    mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
    ticker_map = download_json(mapping_id)
    storage = {}
    session = requests.Session()

    for ticker, msn_id in ticker_map.items():
        first_char = ticker[0].upper()
        if first_char not in target_chars and not (target_chars == "NOPQRSTUVWXYZ0123456789" and not first_char.isalpha()):
            continue

        filename = f"{first_char if first_char.isalpha() else 'ETC'}_stocks.json"
        act_id = str(uuid.uuid4())
        
        try:
            # 기존 데이터 로드 (재무제표 보존용)
            if filename not in storage:
                fid = find_file_id(filename, data_id)
                storage[filename] = download_json(fid) if fid else {}

            # A. 매일 수집 (Price, PER, PBR 등)
            res_a = session.get(f"https://assets.msn.com/service/Finance/Equities?apikey={MSN_API_KEY}&activityId={act_id}&ids={msn_id}&wrapodata=false", timeout=10)
            basic_data = res_a.json()[0] if res_a.status_code == 200 else storage[filename].get(ticker, {}).get('basic', {})

            # B. 주기적 수집 (재무제표: 분기별 또는 특정일에만)
            history_data = storage[filename].get(ticker, {}).get('history_5y', {})
            if update_mode == "QUARTERLY":
                res_b = session.get(f"https://assets.msn.com/service/Finance/Equities/financialstatements?apikey={MSN_API_KEY}&activityId={act_id}&$filter=_p eq '{msn_id}'&wrapodata=false", timeout=15)
                if res_b.status_code == 200:
                    history_data = slice_5y(res_b.json())

            storage[filename][ticker] = {
                "msn_id": msn_id,
                "last_updated": now_kst.strftime('%Y-%m-%d %H:%M:%S'),
                "update_mode": update_mode,
                "basic": basic_data,
                "history_5y": history_data
            }
            
            # 수집 속도 조절 (데일리일 땐 빠르게, 쿼터리일 땐 안정적으로)
            time.sleep(0.5 if update_mode == "DAILY" else 1.0)

        except Exception as e:
            print(f"⚠️ {ticker} 에러: {e}")

    # 5. 최종 업로드
    for fname, content in storage.items():
        upload_json(fname, content, data_id)

if __name__ == "__main__":
    run_harvester()
