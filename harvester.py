import os
import json
import requests
import time
import datetime
import io
import urllib3
import random
import sys
import re
import math
import ssl
import traceback
import yfinance as yf
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request

# 로그 실시간 출력 설정
# 항상 line buffering을 켜서 GitHub Actions/터미널에 진행 로그가 즉시 보이게 한다.
if hasattr(sys.stdout, "reconfigure"):
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

# Raw-first policy:
# 1) Collect source fields directly whenever possible.
# 2) Avoid deriving core financial fields from unrelated proxies at collection time.
CORE_REQUIRED_KEYS = [
    "symbol", "name", "price", "currency", "marketCap", "updated", "Hist",
    "per", "pbr", "psr", "pegRatio", "targetMeanPrice",
    "roe", "roa", "eps", "operatingMargins", "debtToEquity",
    "totalDebt", "longTermDebt", "shortLongTermDebtTotal",
    "totalDebtAndCapitalLeaseObligation", "totalEquity", "totalStockholdersEquity",
    "revenueGrowth", "operatingCashflow",
    "dividendRate", "dividendYield",
    "volume", "beta", "heldPercentInstitutions", "shortRatio",
    "fiftyDayAverage", "twoHundredDayAverage",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "sector", "industry"
]

# Extendable bucket for future additions without destabilizing core pipeline.
# H11 prep: Distress-model inputs (Altman Z + financial safety model)
# Keep these optional first to avoid destabilizing existing coverage alarms.
DISTRESS_OPTIONAL_KEYS = [
    "totalAssets",
    "totalLiabilities",
    "currentAssets",
    "currentLiabilities",
    "workingCapital",
    "retainedEarnings",
    "ebit",
    "totalRevenue",
]

EXTENDED_OPTIONAL_KEYS = DISTRESS_OPTIONAL_KEYS[:]

STANDARD_KEYS = CORE_REQUIRED_KEYS + EXTENDED_OPTIONAL_KEYS

BENCHMARK_SPECS = [
    {"source": "^GSPC", "alias": "SP500_INDEX"},
    {"source": "^IXIC", "alias": "NASDAQ_INDEX"},
    {"source": "^VIX", "alias": "VIX_INDEX"},
]

MARKET_REGIME_FILENAME = "MARKET_REGIME_SNAPSHOT.json"
EARNINGS_EVENT_FILENAME = "EARNINGS_EVENT_MAP.json"
FMP_API_KEY = os.getenv("FMP_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_KEY") or os.getenv("FINNHUB_API_KEY")

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

DRIVE_RETRY_ATTEMPTS = 3
DRIVE_BACKOFF_BASE_SEC = 1.5

# Daily split optimization (balanced runtime)
DAILY_BATCH_FIRST_LABEL = "1차 (A-K)"
DAILY_BATCH_SECOND_LABEL = "2차 (L-Z & 기타)"
DAILY_BATCH_FIRST_CHARS = "ABCDEFGHIJK"
DAILY_BATCH_SECOND_CHARS = "LMNOPQRSTUVWXYZ0123456789"

# OHLCV retention policy for 5Y seasonality consumers
OHLCV_INITIAL_PERIOD = os.getenv("OHLCV_INITIAL_PERIOD", "5y")
OHLCV_INCREMENTAL_PERIOD = os.getenv("OHLCV_INCREMENTAL_PERIOD", "7d")
try:
    OHLCV_MAX_BARS = max(300, int(os.getenv("OHLCV_MAX_BARS", "1300")))
except Exception:
    OHLCV_MAX_BARS = 1300


def _extract_http_status(exc):
    return getattr(getattr(exc, "resp", None), "status", None)


def _is_transient_drive_error(exc):
    if isinstance(exc, HttpError):
        status = _extract_http_status(exc)
        return status in (429, 500, 502, 503, 504)

    if isinstance(exc, (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError)):
        return True

    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError)):
        return True

    error_name = type(exc).__name__
    error_msg = str(exc).lower()
    return ("ssleoferror" in error_name.lower()) or ("eof occurred in violation of protocol" in error_msg)


def _retry_backoff_sleep(attempt):
    base = DRIVE_BACKOFF_BASE_SEC * (2 ** attempt)
    delay_sec = base + random.uniform(0.0, 0.5)
    time.sleep(delay_sec)
    return delay_sec


def _rebuild_drive_service(context):
    global drive_service
    drive_service = get_drive_service()
    print(f"🔁 Drive client 재연결 완료 ({context})", flush=True)

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠️ Telegram 알림 실패: {type(e).__name__}: {e}", flush=True)

# --- [2. 드라이브 유틸리티] ---
def find_file_id(name, parent_id=None):
    query = f"name = '{name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"

    last_error = None
    for attempt in range(DRIVE_RETRY_ATTEMPTS): # 🎯 3번 재시도 (네트워크 지연으로 인한 중복 파일 생성 완벽 방지)
        try:
            results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
            return results[0]['id'] if results else None
        except HttpError as e:
            status = _extract_http_status(e)
            if status in (401, 403):
                print(f"⛔ Drive API 인증 오류(find_file_id:{name}): {status} {e}", flush=True)
                raise
            last_error = e
            if _is_transient_drive_error(e):
                if attempt < DRIVE_RETRY_ATTEMPTS - 1:
                    print(f"⚠️ Drive 파일 조회 실패(find_file_id:{name}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}] transient={status}", flush=True)
                    _rebuild_drive_service(f"find_file_id:{name}")
                    slept = _retry_backoff_sleep(attempt)
                    print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                    continue
                break
            print(f"⛔ Drive 파일 조회 비재시도 오류(find_file_id:{name}): {status} {e}", flush=True)
            raise
        except Exception as e:
            last_error = e
            if _is_transient_drive_error(e):
                if attempt < DRIVE_RETRY_ATTEMPTS - 1:
                    print(f"⚠️ Drive 파일 조회 예외(find_file_id:{name}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}]: {type(e).__name__}: {e}", flush=True)
                    _rebuild_drive_service(f"find_file_id:{name}")
                    slept = _retry_backoff_sleep(attempt)
                    print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                    continue
                break
            print(f"⛔ Drive 파일 조회 비재시도 예외(find_file_id:{name}): {type(e).__name__}: {e}", flush=True)
            raise

    raise RuntimeError(
        f"Drive 파일 조회 최종 실패(find_file_id:{name}, parent={parent_id}) "
        f"after {DRIVE_RETRY_ATTEMPTS} attempts: {type(last_error).__name__ if last_error else 'Unknown'}: {last_error}"
    )

def download_json(file_id):
    if not file_id: return None # 반환값을 None으로 명확히 하여 메인 로직에서 타입 캐스팅 유도
    last_error = None
    for attempt in range(DRIVE_RETRY_ATTEMPTS): # 다운로드도 3번 재시도
        try:
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            return json.loads(fh.getvalue().decode())
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON 파싱 오류(download_json:{file_id}): {e}", flush=True)
            return None
        except HttpError as e:
            status = _extract_http_status(e)
            if status in (401, 403):
                print(f"⛔ Drive API 인증 오류(download_json:{file_id}): {status} {e}", flush=True)
                raise
            last_error = e
            if _is_transient_drive_error(e):
                if attempt < DRIVE_RETRY_ATTEMPTS - 1:
                    print(f"⚠️ Drive 다운로드 실패(download_json:{file_id}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}] transient={status}", flush=True)
                    _rebuild_drive_service(f"download_json:{file_id}")
                    slept = _retry_backoff_sleep(attempt)
                    print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                    continue
                break
            print(f"⛔ Drive 다운로드 비재시도 오류(download_json:{file_id}): {status} {e}", flush=True)
            raise
        except Exception as e:
            last_error = e
            if _is_transient_drive_error(e):
                if attempt < DRIVE_RETRY_ATTEMPTS - 1:
                    print(f"⚠️ 다운로드 예외(download_json:{file_id}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}]: {type(e).__name__}: {e}", flush=True)
                    _rebuild_drive_service(f"download_json:{file_id}")
                    slept = _retry_backoff_sleep(attempt)
                    print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                    continue
                break
            print(f"⛔ 다운로드 비재시도 예외(download_json:{file_id}): {type(e).__name__}: {e}", flush=True)
            raise

    raise RuntimeError(
        f"Drive 다운로드 최종 실패(download_json:{file_id}) "
        f"after {DRIVE_RETRY_ATTEMPTS} attempts: {type(last_error).__name__ if last_error else 'Unknown'}: {last_error}"
    )

def upload_json(filename, data, parent_id):
    print(f"📤 업로드 시도: {filename}...")
    last_error = None
    for attempt in range(DRIVE_RETRY_ATTEMPTS): # 🎯 업로드 중 끊김 방지
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
        except HttpError as e:
            status = _extract_http_status(e)
            if status in (401, 403):
                print(f"⛔ Drive API 인증 오류(upload_json:{filename}): {status} {e}", flush=True)
                raise
            last_error = e
            if _is_transient_drive_error(e) and attempt < DRIVE_RETRY_ATTEMPTS - 1:
                print(f"   ⚠️ 업로드 실패(upload_json:{filename}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}] transient={status}", flush=True)
                _rebuild_drive_service(f"upload_json:{filename}")
                slept = _retry_backoff_sleep(attempt)
                print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                continue
            break
        except Exception as e:
            last_error = e
            if _is_transient_drive_error(e) and attempt < DRIVE_RETRY_ATTEMPTS - 1:
                print(f"   ⚠️ 업로드 예외(upload_json:{filename}) [{attempt + 1}/{DRIVE_RETRY_ATTEMPTS}]: {type(e).__name__}: {e}", flush=True)
                _rebuild_drive_service(f"upload_json:{filename}")
                slept = _retry_backoff_sleep(attempt)
                print(f"   ↳ backoff {slept:.2f}s 후 재시도", flush=True)
                continue
            break

    raise RuntimeError(
        f"Drive 업로드 최종 실패(upload_json:{filename}, parent={parent_id}) "
        f"after {DRIVE_RETRY_ATTEMPTS} attempts: {type(last_error).__name__ if last_error else 'Unknown'}: {last_error}"
    )

def summarize_key_coverage(records, keys):
    total = len(records)
    summary = {}
    for key in keys:
        missing = 0
        for rec in records.values():
            if not isinstance(rec, dict):
                missing += 1
                continue
            value = rec.get(key)
            if value is None or value == '':
                missing += 1
        present = total - missing
        coverage_pct = round((present / total) * 100, 1) if total > 0 else 0.0
        summary[key] = {"present": present, "missing": missing, "coveragePct": coverage_pct}
    return summary

def _first_present(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return value
    return None

def _norm_label(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())

def _to_finite_float(value):
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num

def _first_finite_from_values(values):
    if values is None:
        return None
    for value in values:
        num = _to_finite_float(value)
        if num is not None:
            return num
    return None

def _safe_statement_value(df, candidate_rows):
    if df is None or getattr(df, "empty", True):
        return None
    try:
        normalized_index = {_norm_label(idx): idx for idx in df.index}
        for row_name in candidate_rows:
            hit = normalized_index.get(_norm_label(row_name))
            if hit is None:
                continue
            selected = df.loc[hit]
            if getattr(selected, "empty", True):
                continue

            # pandas.Series path
            if hasattr(selected, "values") and not hasattr(selected, "iterrows"):
                val = _first_finite_from_values(selected.values)
                if val is not None:
                    return val
                continue

            # pandas.DataFrame (duplicated labels) path
            if hasattr(selected, "iterrows"):
                for _, row in selected.iterrows():
                    val = _first_finite_from_values(getattr(row, "values", row))
                    if val is not None:
                        return val
    except Exception:
        return None
    return None

def _get_balance_sheet_fields(stock):
    result = {
        "totalDebt": None,
        "longTermDebt": None,
        "shortLongTermDebtTotal": None,
        "totalDebtAndCapitalLeaseObligation": None,
        "totalEquity": None,
        "totalStockholdersEquity": None,
    }

    statements = []
    for getter in ("quarterly_balance_sheet", "balance_sheet"):
        try:
            df = getattr(stock, getter)
            if df is not None and not df.empty:
                statements.append(df)
        except Exception:
            continue

    # yfinance 버전/엔드포인트별 차이를 흡수하기 위해 함수형 getter도 병행한다.
    for kwargs in ({"freq": "quarterly"}, {"freq": "yearly"}):
        try:
            df = stock.get_balance_sheet(pretty=True, **kwargs)
            if df is not None and not df.empty:
                statements.append(df)
        except Exception:
            continue

    if not statements:
        return result

    for df in statements:
        if result["totalDebt"] is None:
            result["totalDebt"] = _safe_statement_value(df, [
                "Total Debt",
                "TotalDebt",
                "Total Debt And Capital Lease Obligation",
                "TotalDebtAndCapitalLeaseObligation",
            ])
        if result["longTermDebt"] is None:
            result["longTermDebt"] = _safe_statement_value(df, [
                "Long Term Debt",
                "LongTermDebt",
                "Long Term Debt And Capital Lease Obligation",
                "LongTermDebtAndCapitalLeaseObligation",
            ])
        if result["shortLongTermDebtTotal"] is None:
            result["shortLongTermDebtTotal"] = _safe_statement_value(df, [
                "Current Debt",
                "CurrentDebt",
                "Current Debt And Capital Lease Obligation",
                "CurrentDebtAndCapitalLeaseObligation",
                "Short Long Term Debt",
                "ShortLongTermDebt",
            ])
        if result["totalDebtAndCapitalLeaseObligation"] is None:
            result["totalDebtAndCapitalLeaseObligation"] = _safe_statement_value(df, [
                "Total Debt And Capital Lease Obligation",
                "TotalDebtAndCapitalLeaseObligation",
                "Total Debt",
                "TotalDebt",
            ])
        if result["totalEquity"] is None:
            result["totalEquity"] = _safe_statement_value(df, [
                "Stockholders Equity",
                "StockholdersEquity",
                "Total Equity Gross Minority Interest",
                "TotalEquityGrossMinorityInterest",
                "Common Stock Equity",
                "CommonStockEquity",
                "Total Stockholder Equity",
                "TotalStockholderEquity",
                "Total Stockholders Equity",
                "TotalStockholdersEquity",
            ])
        if result["totalStockholdersEquity"] is None:
            result["totalStockholdersEquity"] = _safe_statement_value(df, [
                "Stockholders Equity",
                "StockholdersEquity",
                "Total Stockholder Equity",
                "TotalStockholderEquity",
                "Total Stockholders Equity",
                "TotalStockholdersEquity",
                "Common Stock Equity",
                "CommonStockEquity",
            ])

    return result

def _get_distress_statement_fields(stock):
    """
    Collect statement-level raw inputs required for Altman-style distress models.
    Raw-first: no proxy derivation except workingCapital=currentAssets-currentLiabilities.
    """
    result = {
        "totalAssets": None,
        "totalLiabilities": None,
        "currentAssets": None,
        "currentLiabilities": None,
        "retainedEarnings": None,
        "ebit": None,
        "totalRevenue": None,
    }

    # Balance Sheet fields
    bs_frames = []
    for attr_name, method_name, freq in (
        ("quarterly_balance_sheet", "get_balance_sheet", "quarterly"),
        ("balance_sheet", "get_balance_sheet", "yearly"),
    ):
        df = _get_statement_df(stock, attr_name, method_name, freq)
        if df is not None and not getattr(df, "empty", True):
            bs_frames.append(df)

    for df in bs_frames:
        if result["totalAssets"] is None:
            result["totalAssets"] = _safe_statement_value(df, [
                "Total Assets",
                "TotalAssets",
            ])
        if result["totalLiabilities"] is None:
            result["totalLiabilities"] = _safe_statement_value(df, [
                "Total Liabilities Net Minority Interest",
                "TotalLiabilitiesNetMinorityInterest",
                "Total Liabilities",
                "TotalLiabilities",
                "Total Liab",
                "TotalLiab",
            ])
        if result["currentAssets"] is None:
            result["currentAssets"] = _safe_statement_value(df, [
                "Current Assets",
                "CurrentAssets",
                "Total Current Assets",
                "TotalCurrentAssets",
            ])
        if result["currentLiabilities"] is None:
            result["currentLiabilities"] = _safe_statement_value(df, [
                "Current Liabilities",
                "CurrentLiabilities",
                "Total Current Liabilities",
                "TotalCurrentLiabilities",
            ])
        if result["retainedEarnings"] is None:
            result["retainedEarnings"] = _safe_statement_value(df, [
                "Retained Earnings",
                "RetainedEarnings",
            ])

    # Income Statement fields
    is_frames = []
    for attr_name, method_name, freq in (
        ("quarterly_financials", "get_income_stmt", "quarterly"),
        ("financials", "get_income_stmt", "yearly"),
    ):
        df = _get_statement_df(stock, attr_name, method_name, freq)
        if df is not None and not getattr(df, "empty", True):
            is_frames.append(df)

    for df in is_frames:
        if result["ebit"] is None:
            result["ebit"] = _safe_statement_value(df, [
                "EBIT",
                "Ebit",
                "Operating Income",
                "Operating Income Loss",
                "OperatingIncome",
            ])
        if result["totalRevenue"] is None:
            result["totalRevenue"] = _safe_statement_value(df, [
                "Total Revenue",
                "TotalRevenue",
                "Revenue",
                "Operating Revenue",
                "Net Sales",
                "Sales",
            ])

    return result

def _normalize_period_label(value):
    """Normalize yfinance period labels to YYYY-MM-DD."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    text = str(value)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else text

def _get_statement_df(stock, attr_name, method_name, freq):
    """Prefer attribute access, then fallback to method-based yfinance API."""
    try:
        df = getattr(stock, attr_name)
        if df is not None and not getattr(df, "empty", True):
            return df
    except Exception:
        pass

    method = getattr(stock, method_name, None)
    if not callable(method):
        return None

    for kwargs in ({"freq": freq, "pretty": True}, {"freq": freq}):
        try:
            df = method(**kwargs)
            if df is not None and not getattr(df, "empty", True):
                return df
        except Exception:
            continue
    return None

def _merge_statement_df(rows_map, df, period_type, statement_tag):
    """Merge statement dataframe into period-keyed rows."""
    if df is None or getattr(df, "empty", True):
        return
    try:
        for col in getattr(df, "columns", []):
            date_key = _normalize_period_label(col)
            if not date_key:
                continue
            map_key = f"{period_type}:{date_key}"
            row = rows_map.setdefault(map_key, {
                "date": date_key,
                "_periodType": period_type,
                "_sources": []
            })
            if statement_tag not in row["_sources"]:
                row["_sources"].append(statement_tag)

            for idx in getattr(df, "index", []):
                try:
                    val = _to_finite_float(df.at[idx, col])
                except Exception:
                    val = None
                if val is None:
                    continue
                row[str(idx)] = val
    except Exception:
        return

def _sort_financial_rows(rows):
    """Sort by date descending, prefer quarterly when same date exists."""
    def _key(row):
        date_key = str(row.get("date", ""))
        period_weight = 1 if row.get("_periodType") == "QUARTERLY" else 0
        return (date_key, period_weight)
    return sorted(rows, key=_key, reverse=True)

def _build_financial_history_payload(stock, updated_at):
    """
    Build 5-year financial history payload (quarterly + annual, multi-statement).
    Output is compatible with Stage 3/Stage 2 consumers via `financials` array.
    """
    rows_map = {}
    statement_specs = [
        ("quarterly_financials", "get_income_stmt", "quarterly", "QUARTERLY", "INCOME"),
        ("financials", "get_income_stmt", "yearly", "ANNUAL", "INCOME"),
        ("quarterly_balance_sheet", "get_balance_sheet", "quarterly", "QUARTERLY", "BALANCE"),
        ("balance_sheet", "get_balance_sheet", "yearly", "ANNUAL", "BALANCE"),
        ("quarterly_cashflow", "get_cash_flow", "quarterly", "QUARTERLY", "CASHFLOW"),
        ("cashflow", "get_cash_flow", "yearly", "ANNUAL", "CASHFLOW"),
    ]

    for attr_name, method_name, freq, period_type, statement_tag in statement_specs:
        df = _get_statement_df(stock, attr_name, method_name, freq)
        _merge_statement_df(rows_map, df, period_type, statement_tag)

    if not rows_map:
        return None

    merged_rows = _sort_financial_rows(list(rows_map.values()))
    quarterly_rows = [r for r in merged_rows if r.get("_periodType") == "QUARTERLY"][:20]
    annual_rows = [r for r in merged_rows if r.get("_periodType") == "ANNUAL"][:5]
    financials = _sort_financial_rows(quarterly_rows + annual_rows)

    return {
        "financials": financials,
        "quarterlyFinancials": quarterly_rows,
        "annualFinancials": annual_rows,
        "_meta": {
            "schemaVersion": "v2_5y_multi_statement",
            "quarterlyCount": len(quarterly_rows),
            "annualCount": len(annual_rows),
            "totalPeriods": len(financials),
            "updatedAt": updated_at
        }
    }

def _history_has_financials(entry):
    if isinstance(entry, dict):
        f = entry.get("financials")
        if isinstance(f, list) and len(f) > 0:
            return True
        # Legacy shape: { "2025-12-31 ...": {...}, ... }
        if any(isinstance(v, dict) for v in entry.values()):
            return True
    elif isinstance(entry, list) and len(entry) > 0:
        return True
    return False

def _needs_financial_history_refresh(entry):
    """Refresh when payload is missing/legacy/non-5Y schema."""
    if not isinstance(entry, dict):
        return True
    meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
    schema = str(meta.get("schemaVersion") or "")
    if schema == "v2_5y_multi_statement" and isinstance(entry.get("financials"), list):
        return False
    return True

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


def get_latest_ohlcv_date(records):
    if not isinstance(records, list) or not records:
        return None
    try:
        latest = max(str(item.get("date", "")) for item in records if isinstance(item, dict) and item.get("date"))
        return latest if latest else None
    except Exception:
        return None


def get_expected_market_date_str():
    """
    미국(뉴욕) 기준으로 일봉이 확정되어 있어야 하는 최신 거래일(YYYY-MM-DD)을 계산한다.
    - 장 마감(보수적으로 18:00 ET) 이전 실행: 직전 거래일
    - 주말 실행: 직전 금요일
    """
    try:
        from zoneinfo import ZoneInfo
        ny_now = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # zoneinfo 사용 불가 환경 fallback (DST 미반영)
        ny_now = datetime.datetime.utcnow() - datetime.timedelta(hours=5)

    ref_date = ny_now.date()
    if getattr(ny_now, 'hour', 0) < 18:
        ref_date -= datetime.timedelta(days=1)

    while ref_date.weekday() >= 5:
        ref_date -= datetime.timedelta(days=1)

    return ref_date.strftime('%Y-%m-%d')


def is_ohlcv_fresh(existing_records):
    latest = get_latest_ohlcv_date(existing_records)
    if not latest:
        return False
    expected = get_expected_market_date_str()
    return latest >= expected


# --- [OHLCV 누적 수집 로직] ---
def sync_ohlcv_incremental(ticker, ohlcv_dir_id, source_symbol=None, record_symbol=None):
    source_symbol = source_symbol or ticker
    record_symbol = record_symbol or ticker
    file_name = f"{record_symbol}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    # OHLCV는 리스트 형태이므로 리스트로 변환 보장
    existing_data = download_json(file_id)
    if not isinstance(existing_data, list): existing_data = []

    # [최적화] 최신 거래일까지 이미 수집된 종목은 재호출 스킵
    if existing_data and is_ohlcv_fresh(existing_data):
        return "SKIPPED"

    try:
        stock = yf.Ticker(source_symbol)
        # 데이터가 없으면 5년(5y), 있으면 최근 7일(7d)만
        period = OHLCV_INCREMENTAL_PERIOD if existing_data else OHLCV_INITIAL_PERIOD
        df = stock.history(period=period, interval="1d")

        if df.empty:
            return "FAILED"

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
        # 최신 5년치(약 1,260거래일) 데이터 유지 (seasonality / regime 지표용)
        final_list = sorted(combined.values(), key=lambda x: x["date"])[-OHLCV_MAX_BARS:]
        final_list, removed_tail = trim_zero_volume_flat_tail(final_list)
        if removed_tail > 0:
            print(f"🧹 {file_name}: zero-volume flat tail {removed_tail}건 제거")
        if not final_list:
            return "FAILED"

        upload_json(file_name, final_list, ohlcv_dir_id)
        return "UPDATED"
    except Exception as e:
        print(
            f"⚠️ OHLCV sync 실패 [{record_symbol}] source={source_symbol}: "
            f"{type(e).__name__}: {e}",
            flush=True
        )
        traceback.print_exc()
        return "FAILED"

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


def normalize_event_date(raw_value):
    if raw_value is None:
        return None

    # datetime/date
    if isinstance(raw_value, datetime.datetime):
        return raw_value.date().strftime('%Y-%m-%d')
    if isinstance(raw_value, datetime.date):
        return raw_value.strftime('%Y-%m-%d')

    # unix timestamp (sec or ms)
    if isinstance(raw_value, (int, float)):
        ts = float(raw_value)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts > 0:
            try:
                dt = datetime.datetime.utcfromtimestamp(ts)
                return dt.strftime('%Y-%m-%d')
            except Exception:
                return None

    # string-like
    value = str(raw_value).strip()
    if not value:
        return None

    # ISO / pandas Timestamp string 등은 앞 10자리(YYYY-MM-DD) 우선 사용
    if len(value) >= 10 and value[4] == '-' and value[7] == '-':
        return value[:10]

    # 기타 포맷 파싱 시도
    for fmt in ('%Y/%m/%d', '%m/%d/%Y', '%Y%m%d'):
        try:
            return datetime.datetime.strptime(value, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def upsert_earnings_event(event_map, symbol, date_str, now_date, source, confidence):
    if not symbol or not date_str:
        return

    try:
        event_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return

    days_to_event = (event_date - now_date).days
    if days_to_event < 0 or days_to_event > 60:
        return

    new_payload = {
        "earnings_date": date_str,
        "days_to_event": days_to_event,
        "event_risk": classify_event_risk(days_to_event),
        "source": source,
        "confidence": confidence
    }

    current = event_map.get(symbol)
    if not current:
        event_map[symbol] = new_payload
        return

    # 더 가까운 이벤트 우선. 동일 거리면 confidence 높은 소스 우선.
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
    current_days = current.get('days_to_event', 9999)
    current_conf = rank.get(current.get('confidence', 'UNKNOWN'), 0)
    new_conf = rank.get(confidence, 0)

    if days_to_event < current_days or (days_to_event == current_days and new_conf > current_conf):
        event_map[symbol] = new_payload


def extract_yf_earnings_date(stock):
    # 1) get_earnings_dates (가장 신뢰도 높음)
    try:
        df = stock.get_earnings_dates(limit=1)
        if df is not None and hasattr(df, 'index') and len(df.index) > 0:
            return normalize_event_date(df.index[0])
    except Exception:
        pass

    # 2) calendar 구조 파싱
    try:
        cal = stock.calendar
        if isinstance(cal, dict):
            for key in ('Earnings Date', 'Earnings Date Start', 'earningsDate'):
                if key in cal:
                    val = cal.get(key)
                    if isinstance(val, (list, tuple)) and val:
                        val = val[0]
                    date_str = normalize_event_date(val)
                    if date_str:
                        return date_str
        elif hasattr(cal, 'to_dict'):
            cdict = cal.to_dict()
            if isinstance(cdict, dict):
                for _, v in cdict.items():
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if 'earn' in str(kk).lower():
                                date_str = normalize_event_date(vv)
                                if date_str:
                                    return date_str
    except Exception:
        pass

    # 3) info timestamp fallback
    try:
        info = stock.info if isinstance(stock.info, dict) else {}
        for key in ('earningsTimestamp', 'earningsTimestampStart', 'earningsTimestampEnd'):
            date_str = normalize_event_date(info.get(key))
            if date_str:
                return date_str
    except Exception:
        pass

    return None


def fetch_earnings_event_map(tickers, trigger_file, timestamp):
    payload = {
        "timestamp": timestamp,
        "trigger_file": trigger_file,
        "source": "unavailable",
        "universe_count": len(tickers),
        "covered_count": 0,
        "missing_count": len(tickers),
        "events": {}
    }

    if not tickers:
        return payload

    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    start_date = now_kst.strftime('%Y-%m-%d')
    end_date = (now_kst + datetime.timedelta(days=45)).strftime('%Y-%m-%d')
    now_date = now_kst.date()

    target_set = {ticker.upper() for ticker in tickers if ticker}
    event_map = {}
    source_labels = []

    # 1) FMP 캘린더 (단일 호출)
    if FMP_API_KEY:
        try:
            url = f"https://financialmodelingprep.com/api/v3/earning_calendar?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            calendar = response.json()

            for event in calendar if isinstance(calendar, list) else []:
                symbol = str(event.get('symbol') or '').upper()
                if symbol not in target_set:
                    continue
                date_str = normalize_event_date(event.get('date'))
                upsert_earnings_event(event_map, symbol, date_str, now_date, 'fmp', 'HIGH')

            if event_map:
                source_labels.append('fmp')
        except Exception as e:
            print(f"⚠️ FMP earnings calendar 실패: {str(e)}")

    # 2) Finnhub 캘린더 (단일 호출)
    missing_symbols = sorted(target_set - set(event_map.keys()))
    if missing_symbols and FINNHUB_API_KEY:
        try:
            url = f"https://finnhub.io/api/v1/calendar/earnings?from={start_date}&to={end_date}&token={FINNHUB_API_KEY}"
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            payload_json = response.json() if response.content else {}

            rows = []
            if isinstance(payload_json, dict):
                rows = payload_json.get('earningsCalendar') or payload_json.get('earnings') or []
            elif isinstance(payload_json, list):
                rows = payload_json

            for event in rows if isinstance(rows, list) else []:
                symbol = str(event.get('symbol') or '').upper()
                if symbol not in target_set:
                    continue
                date_str = normalize_event_date(event.get('date'))
                upsert_earnings_event(event_map, symbol, date_str, now_date, 'finnhub', 'HIGH')

            if any(v.get('source') == 'finnhub' for v in event_map.values()):
                source_labels.append('finnhub')
        except Exception as e:
            print(f"⚠️ Finnhub earnings calendar 실패: {str(e)}")

    # 3) yfinance fallback (누락 티커만)
    missing_symbols = sorted(target_set - set(event_map.keys()))
    if missing_symbols:
        print(f"ℹ️ Earnings fallback(yfinance) 시작: {len(missing_symbols)} symbols")
        yf_found = 0
        for idx, symbol in enumerate(missing_symbols, 1):
            try:
                stock = yf.Ticker(symbol)
                date_str = extract_yf_earnings_date(stock)
                upsert_earnings_event(event_map, symbol, date_str, now_date, 'yfinance', 'MEDIUM')
                if symbol in event_map and event_map[symbol].get('source') == 'yfinance':
                    yf_found += 1
            except Exception:
                pass

            if idx % 50 == 0 or idx == len(missing_symbols):
                print(f"   > yfinance earnings fallback {idx}/{len(missing_symbols)}")
            time.sleep(random.uniform(0.05, 0.12))

        if yf_found > 0:
            source_labels.append('yfinance')

    payload["events"] = event_map
    payload["covered_count"] = len(event_map)
    payload["missing_count"] = max(0, len(target_set) - len(event_map))
    payload["source"] = '+'.join(source_labels) if source_labels else 'unavailable'

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
                        send_telegram(f"🚀 *수집 시작:* `{total_count}`종목 (OHLCV {OHLCV_INITIAL_PERIOD})")
                        
                        update_progress(0, total_count, "STARTING...", sys_id, "PROCESSING", current_trigger_file)

                        ohlcv_skipped = 0
                        for idx, st in enumerate(s3_tickers, 1):
                            sync_status = sync_ohlcv_incremental(st, ohlcv_dir_id)
                            if sync_status == "UPDATED":
                                total_success += 1
                            elif sync_status == "SKIPPED":
                                total_success += 1
                                ohlcv_skipped += 1
                            else:
                                total_error += 1

                            # 최신 데이터가 이미 있는 종목은 짧게 대기하여 전체 테스트 시간을 절감
                            if sync_status == "SKIPPED":
                                time.sleep(random.uniform(0.05, 0.15))
                            else:
                                time.sleep(random.uniform(1.6, 2.3))

                            if idx % 10 == 0 or idx == total_count:
                                print(f"📊 진행 중... {idx}/{total_count} (skip {ohlcv_skipped})")
                                update_progress(idx, total_count, st, sys_id, "PROCESSING", current_trigger_file)

                        benchmark_success = 0
                        benchmark_fail = 0
                        benchmark_skipped = 0
                        for benchmark in BENCHMARK_SPECS:
                            alias = benchmark["alias"]
                            source = benchmark["source"]
                            print(f"📈 벤치마크 수집: {alias} <- {source}")
                            benchmark_status = sync_ohlcv_incremental(alias, ohlcv_dir_id, source_symbol=source, record_symbol=alias)
                            if benchmark_status == "UPDATED":
                                benchmark_success += 1
                            elif benchmark_status == "SKIPPED":
                                benchmark_success += 1
                                benchmark_skipped += 1
                            else:
                                benchmark_fail += 1
                                print(f"⚠️ 벤치마크 수집 실패: {alias}")

                        market_regime_ready = False
                        try:
                            regime_snapshot = build_market_regime_snapshot(current_trigger_file, today_str, s3_tickers, ohlcv_dir_id)
                            upload_json(MARKET_REGIME_FILENAME, regime_snapshot, sys_id)
                            market_regime_ready = True
                            print(
                                f"🧭 시장 국면 스냅샷 완료: "
                                f"{regime_snapshot.get('regime', {}).get('state', 'UNKNOWN')} "
                                f"(score={regime_snapshot.get('regime', {}).get('score', 0)})"
                            )
                        except Exception as e:
                            print(f"⚠️ 시장 국면 스냅샷 생성 실패: {str(e)}")

                        earnings_event_ready = False
                        earnings_event_count = 0
                        earnings_event_source = "unavailable"
                        earnings_event_missing = total_count
                        try:
                            earnings_event_map = fetch_earnings_event_map(s3_tickers, current_trigger_file, today_str)
                            earnings_event_count = len(earnings_event_map.get('events', {}))
                            earnings_event_source = earnings_event_map.get('source', 'unavailable')
                            earnings_event_missing = int(earnings_event_map.get('missing_count', max(0, total_count - earnings_event_count)))
                            upload_json(EARNINGS_EVENT_FILENAME, earnings_event_map, sys_id)
                            earnings_event_ready = True
                            print(f"📅 실적 이벤트 맵 완료: {earnings_event_count}건 (source: {earnings_event_source}, missing: {earnings_event_missing})")
                        except Exception as e:
                            print(f"⚠️ 실적 이벤트 맵 업로드 실패: {str(e)}")

                        update_progress(total_count, total_count, "FINISHED", sys_id, "COMPLETED", current_trigger_file)

                        upload_json("LATEST_STAGE4_READY.json", {"status": "COMPLETED", "trigger_file": current_trigger_file, "timestamp": today_str}, sys_id)
                        regime_status = "READY" if market_regime_ready else "SKIPPED"
                        earnings_status = "READY" if earnings_event_ready else "SKIPPED"
                        send_telegram(f"✅ *Stage 4 수집 완료!*\n성공: `{total_success}` (skip `{ohlcv_skipped}`) | 실패: `{total_error}`\n벤치마크: `{benchmark_success}` 성공 (skip `{benchmark_skipped}`) / `{benchmark_fail}` 실패\n시장국면: `{regime_status}`\n실적이벤트: `{earnings_status}` ({earnings_event_count})")
            return # dispatch 작업이 끝났으므로 여기서 명시적으로 종료

        # 🎯 2. [데일리 수집 모드] (스케줄러로 실행될 때 여기로 옴)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        
        current_hour = now_kst.hour
        if 6 <= current_hour <= 11:
            group_label, target_chars = DAILY_BATCH_FIRST_LABEL, DAILY_BATCH_FIRST_CHARS
        else:
            group_label, target_chars = DAILY_BATCH_SECOND_LABEL, DAILY_BATCH_SECOND_CHARS

        full_map = download_json(find_file_id("Ticker_ID_Mapping_Final.json", sys_id))
        
        # 딕셔너리가 아닌 경우 빈 딕셔너리로 초기화 방어
        if not isinstance(full_map, dict):
            full_map = {}
            
        filtered_tickers = {t: info for t, info in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"📡 *[Daily] 본계정 가동*\n🎯 *타겟:* `{group_label}` | `{len(filtered_tickers)}`종목")

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_total = len(group_tickers)
            g_success, g_error = 0, 0
            print(f"\n--- 📦 그룹 [{group}] 작업 시작 ---")
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))
            
            if not isinstance(daily_data, dict): daily_data = {}
            if not isinstance(hist_data, dict): hist_data = {}

            for i, ticker in enumerate(group_tickers, 1):
                success_flag = False
                for attempt in range(3): # 수집 재시도
                    try:
                        if i % 50 == 0:
                            print(f"   > 진행 중: {group} {i}/{g_total}...")
                        time.sleep(random.uniform(1.3, 1.8))
                        stock = yf.Ticker(ticker)
                        
                        # [중요 보완] 5Y financial history (income/balance/cashflow, quarterly+annual)
                        prev_hist = daily_data.get(ticker, {}).get('Hist')
                        hist_status = prev_hist if prev_hist in ('✅', '❌') else '❌'
                        existing_hist_entry = hist_data.get(ticker)
                        history_refresh_required = _needs_financial_history_refresh(existing_hist_entry)
                        if hist_status == '❌' or is_weekend_update or history_refresh_required:
                            try:
                                history_payload = _build_financial_history_payload(stock, today_str)
                                if history_payload and history_payload.get("financials"):
                                    hist_data[ticker] = history_payload
                                    hist_status = '✅'
                                elif hist_status != '✅':
                                    hist_status = '❌'
                            except Exception as e:
                                print(f"⚠️ 재무제표 수집 실패 [{ticker}]: {type(e).__name__}: {e}", flush=True)
                                if hist_status != '✅' and not _history_has_financials(existing_hist_entry):
                                    hist_status = '❌'

                        info = stock.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        
                        if price:
                            info_total_debt = _first_present(info, [
                                'totalDebt',
                                'totalDebtAndCapitalLeaseObligation',
                            ])
                            info_long_term_debt = _first_present(info, [
                                'longTermDebt',
                                'longTermDebtAndCapitalLeaseObligation',
                            ])
                            info_short_long_debt = _first_present(info, [
                                'shortLongTermDebt',
                                'currentDebt',
                                'currentDebtAndCapitalLeaseObligation',
                            ])
                            info_total_debt_lease = _first_present(info, [
                                'totalDebtAndCapitalLeaseObligation',
                                'totalDebt',
                            ])
                            info_total_equity = _first_present(info, [
                                'totalEquity',
                                'totalStockholdersEquity',
                                'totalStockholderEquity',
                                'stockholdersEquity',
                                'commonStockEquity',
                            ])
                            info_total_stockholders_equity = _first_present(info, [
                                'totalStockholdersEquity',
                                'totalStockholderEquity',
                                'stockholdersEquity',
                                'commonStockEquity',
                            ])
                            info_total_assets = _first_present(info, [
                                'totalAssets',
                            ])
                            info_total_liabilities = _first_present(info, [
                                'totalLiabilitiesNetMinorityInterest',
                                'totalLiabilities',
                                'totalLiab',
                            ])
                            info_current_assets = _first_present(info, [
                                'currentAssets',
                            ])
                            info_current_liabilities = _first_present(info, [
                                'currentLiabilities',
                            ])
                            info_retained_earnings = _first_present(info, [
                                'retainedEarnings',
                            ])
                            info_ebit = _first_present(info, [
                                'ebit',
                                'operatingIncome',
                            ])
                            info_total_revenue = _first_present(info, [
                                'totalRevenue',
                                'revenue',
                            ])
                            info_peg_ratio = _first_present(info, [
                                'pegRatio',
                                'trailingPegRatio',
                            ])

                            needs_balance_sheet = any(
                                x is None or x == ''
                                for x in [
                                    info_total_debt,
                                    info_long_term_debt,
                                    info_short_long_debt,
                                    info_total_debt_lease,
                                    info_total_equity,
                                    info_total_stockholders_equity,
                                ]
                            )
                            needs_distress_fields = any(
                                x is None or x == ''
                                for x in [
                                    info_total_assets,
                                    info_total_liabilities,
                                    info_current_assets,
                                    info_current_liabilities,
                                    info_retained_earnings,
                                    info_ebit,
                                    info_total_revenue,
                                ]
                            )
                            bs_fields = _get_balance_sheet_fields(stock) if needs_balance_sheet else {}
                            distress_fields = _get_distress_statement_fields(stock) if needs_distress_fields else {}
                            current_assets_raw = info_current_assets if info_current_assets not in (None, '') else distress_fields.get("currentAssets")
                            current_liabilities_raw = info_current_liabilities if info_current_liabilities not in (None, '') else distress_fields.get("currentLiabilities")
                            current_assets_num = _to_finite_float(current_assets_raw)
                            current_liabilities_num = _to_finite_float(current_liabilities_raw)
                            working_capital = None
                            if current_assets_num is not None and current_liabilities_num is not None:
                                working_capital = current_assets_num - current_liabilities_num

                            # [FIX] Restore legacy raw-record mapping so STANDARD_KEYS are filled with
                            # Yahoo source keys (trailingPE, priceToBook, returnOnEquity, etc).
                            raw_record = {
                                "symbol": ticker,
                                "name": info.get('shortName') or info.get('longName'),
                                "price": price,
                                "currency": info.get('currency', 'USD'),
                                "marketCap": info.get('marketCap'),
                                "updated": today_str,
                                "Hist": hist_status,
                                "per": info.get('trailingPE'),
                                "pbr": info.get('priceToBook'),
                                "psr": info.get('priceToSalesTrailing12Months'),
                                "pegRatio": info_peg_ratio,
                                "targetMeanPrice": info.get('targetMeanPrice'),
                                "roe": info.get('returnOnEquity'),
                                "roa": info.get('returnOnAssets'),
                                "eps": info.get('trailingEps'),
                                "operatingMargins": info.get('operatingMargins'),
                                "debtToEquity": info.get('debtToEquity'),
                                "totalDebt": info_total_debt if info_total_debt not in (None, '') else bs_fields.get("totalDebt"),
                                "longTermDebt": info_long_term_debt if info_long_term_debt not in (None, '') else bs_fields.get("longTermDebt"),
                                "shortLongTermDebtTotal": info_short_long_debt if info_short_long_debt not in (None, '') else bs_fields.get("shortLongTermDebtTotal"),
                                "totalDebtAndCapitalLeaseObligation": info_total_debt_lease if info_total_debt_lease not in (None, '') else bs_fields.get("totalDebtAndCapitalLeaseObligation"),
                                "totalEquity": info_total_equity if info_total_equity not in (None, '') else bs_fields.get("totalEquity"),
                                "totalStockholdersEquity": info_total_stockholders_equity if info_total_stockholders_equity not in (None, '') else bs_fields.get("totalStockholdersEquity"),
                                "totalAssets": info_total_assets if info_total_assets not in (None, '') else distress_fields.get("totalAssets"),
                                "totalLiabilities": info_total_liabilities if info_total_liabilities not in (None, '') else distress_fields.get("totalLiabilities"),
                                "currentAssets": current_assets_raw,
                                "currentLiabilities": current_liabilities_raw,
                                "workingCapital": working_capital,
                                "retainedEarnings": info_retained_earnings if info_retained_earnings not in (None, '') else distress_fields.get("retainedEarnings"),
                                "ebit": info_ebit if info_ebit not in (None, '') else distress_fields.get("ebit"),
                                "totalRevenue": info_total_revenue if info_total_revenue not in (None, '') else distress_fields.get("totalRevenue"),
                                "revenueGrowth": info.get('revenueGrowth'),
                                "operatingCashflow": info.get('operatingCashflow'),
                                "dividendRate": info.get('dividendRate', 0),
                                "dividendYield": info.get('dividendYield', 0),
                                "volume": info.get('regularMarketVolume'),
                                "beta": info.get('beta'),
                                "heldPercentInstitutions": info.get('heldPercentInstitutions'),
                                "shortRatio": info.get('shortRatio'),
                                "fiftyDayAverage": info.get('fiftyDayAverage'),
                                "twoHundredDayAverage": info.get('twoHundredDayAverage'),
                                "fiftyTwoWeekHigh": info.get('fiftyTwoWeekHigh'),
                                "fiftyTwoWeekLow": info.get('fiftyTwoWeekLow'),
                                "sector": info.get('sector'),
                                "industry": info.get('industry')
                            }

                            # 신규 값이 None일 때 기존 값을 덮어쓰지 않도록 보존
                            prev_record = daily_data.get(ticker, {}) if isinstance(daily_data.get(ticker), dict) else {}
                            merged_record = {}
                            for k in STANDARD_KEYS:
                                new_v = raw_record.get(k, None)
                                old_v = prev_record.get(k, None)
                                merged_record[k] = new_v if new_v is not None else old_v
                            daily_data[ticker] = merged_record
                            
                            g_success += 1
                            success_flag = True
                            break
                        else:
                            break
                    except Exception as e:
                        if "SSL" in str(e) or "EOF" in str(e):
                            time.sleep(5)
                
                if not success_flag:
                    g_error += 1

            # Core key coverage sanity summary (raw-first policy visibility)
            group_records = {t: daily_data.get(t, {}) for t in group_tickers.keys()}
            coverage = summarize_key_coverage(group_records, CORE_REQUIRED_KEYS)
            weak_keys = sorted(
                [(k, v["coveragePct"]) for k, v in coverage.items() if v["coveragePct"] < 80],
                key=lambda x: x[1]
            )
            if weak_keys:
                preview = ", ".join([f"{k}:{pct}%" for k, pct in weak_keys[:5]])
                print(f"   ⚠️ [{group}] Core key coverage<80%: {preview}")

            # Distress-model input coverage visibility (Altman + financial safety model prep)
            distress_cov = summarize_key_coverage(group_records, DISTRESS_OPTIONAL_KEYS)
            weak_distress = sorted(
                [(k, v["coveragePct"]) for k, v in distress_cov.items() if v["coveragePct"] < 70],
                key=lambda x: x[1]
            )
            if weak_distress:
                preview = ", ".join([f"{k}:{pct}%" for k, pct in weak_distress[:8]])
                print(f"   ⚠️ [{group}] Distress key coverage<70%: {preview}")
            else:
                print(f"   ✅ [{group}] Distress key coverage>=70% for all tracked fields")

            # 데일리 데이터와 히스토리 데이터 모두 업로드
            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            
            total_success += g_success
            total_error += g_error
            
            print(f"📦 그룹 [{group}] 완료: 성공 {g_success} / 실패 {g_error}")
            send_telegram(f"📦 *그룹 [{group}] 완료*\n✅ 성공: `{g_success}` | ❌ 실패: `{g_error}`")

        duration = (time.time() - start_time) / 60
        send_telegram(f"🏁 *수집 종료*\n⏱️ `{duration:.1f}분` | 성공: `{total_success}` | 실패: `{total_error}`")

    except Exception as e:
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ")
        print(f"⛔ run_harvester fatal: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        raise

if __name__ == "__main__":
    run_harvester()
