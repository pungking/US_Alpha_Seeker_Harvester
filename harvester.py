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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request

# 로그 실시간 출력 설정
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(line_buffering=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [1. 설정 및 본계정 인증] ---
CLIENT_ID = os.getenv('GDRIVE_CLIENT_ID')
CLIENT_SECRET = os.getenv('GDRIVE_CLIENT_SECRET')
REFRESH_TOKEN = os.getenv('GDRIVE_REFRESH_TOKEN')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GITHUB_EVENT_NAME = os.getenv('GITHUB_EVENT_NAME')
GITHUB_EVENT_PATH = os.getenv('GITHUB_EVENT_PATH')

STANDARD_KEYS = [
    "symbol", "name", "price", "currency", "marketCap", "updated", "Hist",
    "per", "pbr", "psr", "pegRatio", "targetMeanPrice",
    "roe", "roa", "eps", "operatingMargins", "debtToEquity",
    "revenueGrowth", "operatingCashflow",
    "dividendRate", "dividendYield",
    "volume", "beta", "heldPercentInstitutions", "shortRatio",
    "fiftyDayAverage", "twoHundredDayAverage", 
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "sector", "industry"
]

BENCHMARK_SPECS = [
    {"source": "^GSPC", "alias": "SP500_INDEX"},
    {"source": "^IXIC", "alias": "NASDAQ_INDEX"},
    {"source": "^VIX", "alias": "VIX_INDEX"},
]

MARKET_REGIME_FILENAME = "MARKET_REGIME_SNAPSHOT.json"
EARNINGS_EVENT_FILENAME = "EARNINGS_EVENT_MAP.json"
FMP_API_KEY = os.getenv("FMP_KEY")

def get_drive_service():
    creds_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "type": "authorized_user"
    }
    creds = Credentials.from_authorized_user_info(creds_data, ["https://www.googleapis.com/auth/drive"])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

drive_service = get_drive_service()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

# --- [2. 드라이브 유틸리티] ---
def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    
    for _ in range(3): # 🎯 3번 재시도 (네트워크 지연으로 인한 중복 파일 생성 완벽 방지)
        try:
            results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
            return results[0]['id'] if results else None
        except: 
            time.sleep(2)
    return None

def download_json(file_id):
    if not file_id: return None # 반환값을 None으로 명확히 하여 메인 로직에서 타입 캐스팅 유도
    for _ in range(3): # 다운로드도 3번 재시도
        try:
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            return json.loads(fh.getvalue().decode())
        except: 
            time.sleep(2)
    return None

def upload_json(filename, data, parent_id):
    print(f"📤 업로드 시도: {filename}...")
    for attempt in range(3): # 🎯 업로드 중 끊김 방지
        try:
            file_id = find_file_id(filename, parent_id)
            fh = io.BytesIO(json.dumps(data, indent=4, ensure_ascii=False).encode())
            media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
            
            if file_id:
                drive_service.files().update(fileId=file_id, media_body=media).execute()
            else:
                meta = {'name': filename, 'parents': [parent_id]}
                drive_service.files().create(body=meta, media_body=media).execute()
            print(f"✅ 완료: {filename}")
            return # 성공하면 함수 깔끔하게 종료
        except Exception as e:
            print(f"   ⚠️ 실패 ({attempt+1}/3): {str(e)}")
            time.sleep(3)

def get_dispatch_trigger_file():
    if not GITHUB_EVENT_PATH:
        return None
    try:
        with open(GITHUB_EVENT_PATH, 'r', encoding='utf-8') as f:
            event = json.load(f)
        return event.get('client_payload', {}).get('trigger_file')
    except Exception as e:
        print(f"⚠️ trigger_file 파싱 실패: {str(e)}")
        return None

# [추가됨] 실시간 진행 상태 기록 함수
def update_progress(current, total, ticker, sys_id, status="PROCESSING", trigger_file=None):
    progress_data = {
        "status": status,
        "current": current,
        "total": total,
        "last_ticker": ticker,
        "percentage": round((current / total) * 100, 1) if total > 0 else 0,
        "updated": (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
    }
    if trigger_file:
        progress_data["trigger_file"] = trigger_file
    upload_json("COLLECTION_PROGRESS.json", progress_data, sys_id)

# --- [OHLCV 누적 수집 로직] ---
def trim_zero_volume_flat_tail(records):
    trimmed = list(records)
    removed = 0
    while len(trimmed) >= 2:
        last = trimmed[-1]
        prev = trimmed[-2]
        is_flat_bar = last.get('open') == last.get('high') == last.get('low') == last.get('close')
        is_zero_volume = int(last.get('volume', 0) or 0) == 0
        if is_flat_bar and is_zero_volume and last.get('close') == prev.get('close'):
            trimmed.pop()
            removed += 1
        else:
            break
    return trimmed, removed


def sync_ohlcv_incremental(ticker, ohlcv_dir_id, source_symbol=None, record_symbol=None):
    source_symbol = source_symbol or ticker
    record_symbol = record_symbol or ticker
    file_name = f"{record_symbol}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    # OHLCV는 리스트 형태이므로 리스트로 변환 보장
    existing_data = download_json(file_id)
    if not isinstance(existing_data, list): existing_data = []
    
    try:
        stock = yf.Ticker(source_symbol)
        # 데이터가 없으면 2년(2y), 있으면 최근 7일(7d)만
        period = "7d" if existing_data else "2y"
        df = stock.history(period=period, interval="1d")
        
        if df.empty: return False
        
        # 각 레코드마다 symbol 필드를 강제로 추가
        new_recs = [
            {
                "symbol": record_symbol,
                "date": d.strftime('%Y-%m-%d'), 
                "open": round(r["Open"], 2), 
                "high": round(r["High"], 2), 
                "low": round(r["Low"], 2), 
                "close": round(r["Close"], 2), 
                "volume": int(r["Volume"])
            } for d, r in df.iterrows()
        ]
        
        # 날짜 기준 중복 제거 및 합치기
        combined = {item["date"]: item for item in (existing_data + new_recs)}
        # 최신 2년치(약 500거래일) 데이터만 유지하여 파일 크기 관리
        final_list = sorted(combined.values(), key=lambda x: x["date"])[-500:]
        final_list, removed_tail = trim_zero_volume_flat_tail(final_list)
        if removed_tail > 0:
            print(f"🧹 {file_name}: zero-volume flat tail {removed_tail}건 제거")
        if not final_list:
            return False
        
        upload_json(file_name, final_list, ohlcv_dir_id)
        return True
    except:
        return False

# --- [3. 시장 컨텍스트 스냅샷 생성] ---
def safe_sma(values, window):
    if len(values) < window:
        return None
    return round(sum(values[-window:]) / window, 2)


def safe_return_pct(values, lookback):
    if len(values) <= lookback:
        return None
    base = values[-lookback-1]
    if not base:
        return None
    return round(((values[-1] / base) - 1) * 100, 2)


def classify_vix_risk(vix_close):
    if vix_close is None:
        return "UNKNOWN"
    if vix_close >= 28:
        return "HIGH"
    if vix_close >= 20:
        return "ELEVATED"
    if vix_close >= 15:
        return "NORMAL"
    return "LOW"


def build_benchmark_snapshot(records, benchmark_alias):
    if not isinstance(records, list) or not records:
        return None
    closes = [float(item.get("close", 0) or 0) for item in records if item.get("close") is not None]
    if not closes:
        return None

    sma50 = safe_sma(closes, 50)
    sma200 = safe_sma(closes, 200)
    snapshot = {
        "close": round(closes[-1], 2),
        "return_20d": safe_return_pct(closes, 20),
        "above_sma50": bool(sma50 is not None and closes[-1] > sma50),
        "above_sma200": bool(sma200 is not None and closes[-1] > sma200),
    }
    if benchmark_alias == "VIX_INDEX":
        snapshot["risk_state"] = classify_vix_risk(snapshot["close"])
    return snapshot


def build_breadth_snapshot(tickers, ohlcv_dir_id):
    total = len(tickers)
    if total == 0:
        return {
            "source": "stage3_universe",
            "total": 0,
            "above_sma50_pct": 0.0,
            "above_sma200_pct": 0.0,
            "near_52w_high_pct": 0.0,
            "valid_count": 0
        }

    valid_count = 0
    above_sma50 = 0
    above_sma200 = 0
    near_52w_high = 0

    for ticker in tickers:
        file_id = find_file_id(f"{ticker}_OHLCV.json", ohlcv_dir_id)
        records = download_json(file_id)
        if not isinstance(records, list) or len(records) < 50:
            continue

        closes = [float(item.get("close", 0) or 0) for item in records if item.get("close") is not None]
        if len(closes) < 50:
            continue

        valid_count += 1
        last_close = closes[-1]
        sma50 = safe_sma(closes, 50)
        sma200 = safe_sma(closes, 200)
        high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)

        if sma50 is not None and last_close > sma50:
            above_sma50 += 1
        if sma200 is not None and last_close > sma200:
            above_sma200 += 1
        if high_52w and last_close >= high_52w * 0.9:
            near_52w_high += 1

    base_count = valid_count or total
    return {
        "source": "stage3_universe",
        "total": total,
        "valid_count": valid_count,
        "above_sma50_pct": round((above_sma50 / base_count) * 100, 1),
        "above_sma200_pct": round((above_sma200 / base_count) * 100, 1),
        "near_52w_high_pct": round((near_52w_high / base_count) * 100, 1)
    }


def derive_market_regime(benchmark_snapshots, breadth_snapshot):
    score = 50
    reasons = []

    sp500 = benchmark_snapshots.get("sp500") or {}
    nasdaq = benchmark_snapshots.get("nasdaq") or {}
    vix = benchmark_snapshots.get("vix") or {}

    if sp500.get("above_sma50"):
        score += 8
        reasons.append("SPX above 50DMA")
    else:
        score -= 8
        reasons.append("SPX below 50DMA")

    if sp500.get("above_sma200"):
        score += 10
        reasons.append("SPX above 200DMA")
    else:
        score -= 10
        reasons.append("SPX below 200DMA")

    if nasdaq.get("above_sma50"):
        score += 6
        reasons.append("NDX above 50DMA")
    else:
        score -= 6
        reasons.append("NDX below 50DMA")

    if nasdaq.get("above_sma200"):
        score += 6
        reasons.append("NDX above 200DMA")
    else:
        score -= 6
        reasons.append("NDX below 200DMA")

    breadth50 = breadth_snapshot.get("above_sma50_pct", 0)
    breadth200 = breadth_snapshot.get("above_sma200_pct", 0)
    highs = breadth_snapshot.get("near_52w_high_pct", 0)

    if breadth50 >= 60:
        score += 5
        reasons.append("Breadth50 healthy")
    elif breadth50 < 45:
        score -= 5
        reasons.append("Breadth50 weak")

    if breadth200 >= 55:
        score += 7
        reasons.append("Breadth200 healthy")
    elif breadth200 < 40:
        score -= 7
        reasons.append("Breadth200 weak")

    if highs >= 18:
        score += 4
        reasons.append("Leaders near highs")
    elif highs < 8:
        score -= 4
        reasons.append("Few leaders near highs")

    vix_close = vix.get("close")
    if vix_close is not None:
        if vix_close >= 28:
            score -= 16
            reasons.append("VIX stress")
        elif vix_close >= 20:
            score -= 8
            reasons.append("VIX elevated")
        elif vix_close < 15:
            score += 3
            reasons.append("VIX calm")

    score = max(0, min(100, int(round(score))))
    if score >= 70:
        state = "RISK_ON"
    elif score >= 45:
        state = "NEUTRAL"
    else:
        state = "RISK_OFF"

    return {
        "state": state,
        "score": score,
        "reasons": reasons
    }


def build_market_regime_snapshot(trigger_file, timestamp, tickers, ohlcv_dir_id):
    benchmark_snapshots = {}
    for benchmark in BENCHMARK_SPECS:
        alias = benchmark["alias"]
        file_id = find_file_id(f"{alias}_OHLCV.json", ohlcv_dir_id)
        records = download_json(file_id)
        snapshot = build_benchmark_snapshot(records, alias)
        if snapshot:
            if alias == "SP500_INDEX":
                benchmark_snapshots["sp500"] = snapshot
            elif alias == "NASDAQ_INDEX":
                benchmark_snapshots["nasdaq"] = snapshot
            elif alias == "VIX_INDEX":
                benchmark_snapshots["vix"] = snapshot

    breadth_snapshot = build_breadth_snapshot(tickers, ohlcv_dir_id)
    regime_snapshot = derive_market_regime(benchmark_snapshots, breadth_snapshot)

    return {
        "timestamp": timestamp,
        "trigger_file": trigger_file,
        "benchmarks": benchmark_snapshots,
        "breadth": breadth_snapshot,
        "regime": regime_snapshot
    }


def classify_event_risk(days_to_event):
    if days_to_event is None:
        return "NONE"
    if days_to_event <= 2:
        return "HIGH"
    if days_to_event <= 7:
        return "MEDIUM"
    return "NONE"


def fetch_earnings_event_map(tickers, trigger_file, timestamp):
    payload = {
        "timestamp": timestamp,
        "trigger_file": trigger_file,
        "source": "unavailable",
        "universe_count": len(tickers),
        "events": {}
    }

    if not tickers or not FMP_API_KEY:
        return payload

    try:
        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        start_date = now_kst.strftime('%Y-%m-%d')
        end_date = (now_kst + datetime.timedelta(days=45)).strftime('%Y-%m-%d')
        url = f"https://financialmodelingprep.com/api/v3/earning_calendar?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        calendar = response.json()

        event_map = {}
        target_set = {ticker.upper() for ticker in tickers}

        for event in calendar if isinstance(calendar, list) else []:
            symbol = str(event.get('symbol') or '').upper()
            date_str = str(event.get('date') or '')[:10]
            if not symbol or not date_str or symbol not in target_set:
                continue

            try:
                event_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                days_to_event = (event_date - now_kst.date()).days
            except ValueError:
                continue

            current = event_map.get(symbol)
            if current and current['days_to_event'] <= days_to_event:
                continue

            event_map[symbol] = {
                "earnings_date": date_str,
                "days_to_event": days_to_event,
                "event_risk": classify_event_risk(days_to_event)
            }

        payload["source"] = "fmp"
        payload["events"] = event_map
        return payload
    except Exception as e:
        print(f"⚠️ Earnings event map 생성 실패: {str(e)}")
        return payload


# --- [4. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    total_success, total_error = 0, 0
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() == 5)

    try:
        print(f"🔍 시스템 가동: {today_str} (Event: {GITHUB_EVENT_NAME})")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id) # 변수명 오류 수정 (find_id_map 제거)

        # 🎯 1. [특별 작업 모드] 웹앱 신호 시 OHLCV 300개 수집
        if GITHUB_EVENT_NAME == 'repository_dispatch':
            ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
            s3_folder_id = find_file_id("Stage3_Fundamental_Data", root_id)
            dispatch_trigger_file = get_dispatch_trigger_file()
            
            if s3_folder_id:
                query = f"'{s3_folder_id}' in parents and name contains 'STAGE3_FUNDAMENTAL_FULL_' and trashed = false"
                s3_files = drive_service.files().list(q=query, fields="files(id, name)", orderBy="createdTime desc").execute().get('files', [])
                
                if s3_files:
                    target_s3 = None
                    if dispatch_trigger_file:
                        target_s3 = next((f for f in s3_files if f.get('name') == dispatch_trigger_file), None)
                        if target_s3:
                            print(f"🎯 지정된 Stage 3 파일 사용: {dispatch_trigger_file}")
                        else:
                            print(f"⚠️ 지정된 trigger_file 미발견: {dispatch_trigger_file} → 최신 파일로 대체")
                    
                    if not target_s3:
                        target_s3 = s3_files[0]
                    
                    s3_data = download_json(target_s3['id'])
                    current_trigger_file = target_s3['name']
                    
                    # 티커 리스트 추출 및 중복 제거 (무한루프 방지)
                    t_list = s3_data.get('fundamental_universe') or s3_data.get('stocks') or (s3_data if isinstance(s3_data, list) else [])
                    s3_tickers = list(set([item['symbol'] for item in t_list if isinstance(item, dict) and 'symbol' in item]))
                    
                    if s3_tickers:
                        total_count = len(s3_tickers)
                        send_telegram(f"🚀 *수집 시작:* `{total_count}`종목 (2년치)")
                        
                        update_progress(0, total_count, "STARTING...", sys_id, "PROCESSING", current_trigger_file)

                        for idx, st in enumerate(s3_tickers, 1):
                            if sync_ohlcv_incremental(st, ohlcv_dir_id):
                                total_success += 1
                            else:
                                total_error += 1
                            
                            time.sleep(random.uniform(1.6, 2.3))
                            
                            if idx % 10 == 0 or idx == total_count:
                                print(f"📊 진행 중... {idx}/{total_count}")
                                update_progress(idx, total_count, st, sys_id, "PROCESSING", current_trigger_file)
                                
                        benchmark_success = 0
                        benchmark_fail = 0
                        for benchmark in BENCHMARK_SPECS:
                            alias = benchmark["alias"]
                            source = benchmark["source"]
                            print(f"📈 벤치마크 수집: {alias} <- {source}")
                            if sync_ohlcv_incremental(alias, ohlcv_dir_id, source_symbol=source, record_symbol=alias):
                                benchmark_success += 1
                            else:
                                benchmark_fail += 1
                                print(f"⚠️ 벤치마크 수집 실패: {alias}")

                        market_regime_ready = False
                        try:
                            regime_snapshot = build_market_regime_snapshot(current_trigger_file, today_str, s3_tickers, ohlcv_dir_id)
                            upload_json(MARKET_REGIME_FILENAME, regime_snapshot, sys_id)
                            market_regime_ready = True
                            print(f"🧭 시장 국면 스냅샷 완료: {regime_snapshot.get('regime', {}).get('state', 'UNKNOWN')} ({regime_snapshot.get('regime', {}).get('score', 0)})")
                        except Exception as e:
                            print(f"⚠️ 시장 국면 스냅샷 생성 실패: {str(e)}")

                        earnings_event_ready = False
                        earnings_event_count = 0
                        try:
                            earnings_event_map = fetch_earnings_event_map(s3_tickers, current_trigger_file, today_str)
                            earnings_event_count = len(earnings_event_map.get('events', {}))
                            upload_json(EARNINGS_EVENT_FILENAME, earnings_event_map, sys_id)
                            earnings_event_ready = True
                            print(f"📅 실적 이벤트 맵 완료: {earnings_event_count}건")
                        except Exception as e:
                            print(f"⚠️ 실적 이벤트 맵 업로드 실패: {str(e)}")

                        update_progress(total_count, total_count, "FINISHED", sys_id, "COMPLETED", current_trigger_file)

                        upload_json("LATEST_STAGE4_READY.json", {"status": "COMPLETED", "trigger_file": current_trigger_file, "timestamp": today_str}, sys_id)
                        regime_status = "READY" if market_regime_ready else "SKIPPED"
                        earnings_status = "READY" if earnings_event_ready else "SKIPPED"
                        send_telegram(f"✅ *Stage 4 수집 완료!*\n성공: `{total_success}` | 실패: `{total_error}`\n벤치마크: `{benchmark_success}` 성공 / `{benchmark_fail}` 실패\n시장국면: `{regime_status}`\n실적이벤트: `{earnings_status}` ({earnings_event_count})")
            return # dispatch 작업이 끝났으므로 여기서 명시적으로 종료

        # 🎯 2. [데일리 수집 모드] (스케줄러로 실행될 때 여기로 옴)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        current_hour = now_kst.hour
        if 6 <= current_hour <= 11:
            group_label, target_chars = "1차 (A-M)", "ABCDEFGHIJKLM"
        else:
            group_label, target_chars = "2차 (N-Z & 기타)", "NOPQRSTUVWXYZ0123456789"

        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        
        # 딕셔너리가 아닌 경우 빈 딕셔너리로 초기화 방어
        if not isinstance(full_map, dict):
            full_map = {}
            
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *[Daily] 본계정 가동*\n🎯 *타겟:* `{group_label}` | `{len(filtered_tickers)}`종목")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_success, g_error = 0, 0
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))
            
            if not isinstance(daily_data, dict): daily_data = {}
            if not isinstance(hist_data, dict): hist_data = {}

            for ticker in group_tickers:
                success_flag = False
                for _ in range(2):
                    try:
                        time.sleep(random.uniform(1.3, 1.8))
                        stock = yf.Ticker(ticker)
                        
                        # [중요 보완] 히스토리(재무제표) 데이터 수집 로직 복구
                        hist_status = daily_data.get(ticker, {}).get('Hist', '❌')
                        if hist_status == '❌' or is_weekend_update:
                            try:
                                f_data = stock.quarterly_financials
                                if not f_data.empty:
                                    hist_data[ticker] = {str(k): v for k, v in f_data.to_dict().items()}
                                    hist_status = '✅'
                            except: pass

                        info = stock.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        
                        if price:
                            # STANDARD_KEYS에 맞게 데이터 추출
                            daily_data[ticker] = {k: info.get(k) for k in STANDARD_KEYS}
                            daily_data[ticker]["updated"] = today_str
                            daily_data[ticker]["symbol"] = ticker
                            daily_data[ticker]["Hist"] = hist_status # 히스토리 상태 업데이트
                            
                            g_success += 1
                            success_flag = True
                            break
                    except: pass
                
                if not success_flag: g_error += 1

            # 데일리 데이터와 히스토리 데이터 모두 업로드
            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id) # 누락되었던 히스토리 업로드 복구
            
            total_success += g_success
            total_error += g_error
            
            # 그룹별 완료 알림 (선택사항, 너무 많으면 주석처리 가능)
            print(f"📦 그룹 [{group}] 완료: 성공 {g_success} / 실패 {g_error}")

        duration = (time.time() - start_time) / 60
        send_telegram(f"🏁 *수집 종료*\n⏱️ `{duration:.1f}분` | 성공: `{total_success}` | 실패: `{total_error}`")

    except Exception as e:
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")

if __name__ == "__main__":
    run_harvester()
