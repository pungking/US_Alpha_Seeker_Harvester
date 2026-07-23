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
import hashlib
from email.utils import parsedate_to_datetime
import yfinance as yf
from collections import Counter
from html.parser import HTMLParser
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request
from scripts.target_lineage import build_target_lineage, build_target_lineage_runtime_audit

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
TELEGRAM_SIMULATION_CHAT_ID = os.getenv('TELEGRAM_SIMULATION_CHAT_ID')
TELEGRAM_ALERT_CHAT_ID = os.getenv('TELEGRAM_ALERT_CHAT_ID')
GITHUB_EVENT_NAME = os.getenv('GITHUB_EVENT_NAME')
GITHUB_EVENT_PATH = os.getenv('GITHUB_EVENT_PATH')
DAILY_BATCH_MODE = (os.getenv('DAILY_BATCH_MODE') or 'auto').strip().lower()
HARVESTER_RUN_SUMMARY_PATH = (os.getenv("HARVESTER_RUN_SUMMARY_PATH") or "state/last-harvester-run.json").strip()
HARVESTER_FAILURE_REPORT_PATH = (os.getenv("HARVESTER_FAILURE_REPORT_PATH") or "state/harvester-failure-report.json").strip()
HARVESTER_EARNINGS_EVENT_COVERAGE_AUDIT_PATH = (
    os.getenv("HARVESTER_EARNINGS_EVENT_COVERAGE_AUDIT_PATH")
    or "state/stage4-earnings-event-coverage-audit.json"
).strip()
HARVESTER_MAPPING_FRESHNESS_AUDIT_PATH = (
    os.getenv("HARVESTER_MAPPING_FRESHNESS_AUDIT_PATH")
    or "state/harvester-mapping-freshness-audit.json"
).strip()
HARVESTER_MAPPING_FRESHNESS_AUDIT_MD_PATH = (
    os.getenv("HARVESTER_MAPPING_FRESHNESS_AUDIT_MD_PATH")
    or "state/harvester-mapping-freshness-audit.md"
).strip()
HARVESTER_TICKER_MAPPING_REFRESH_AUDIT_PATH = (
    os.getenv("HARVESTER_TICKER_MAPPING_REFRESH_AUDIT_PATH")
    or "state/ticker-mapping-refresh-audit.json"
).strip()
HARVESTER_TARGET_LINEAGE_RUNTIME_AUDIT_PATH = (
    os.getenv("HARVESTER_TARGET_LINEAGE_RUNTIME_AUDIT_PATH")
    or "state/target-lineage-runtime-audit.json"
).strip()
HARVESTER_CORPORATE_ACTION_RUNTIME_AUDIT_PATH = (
    os.getenv("HARVESTER_CORPORATE_ACTION_RUNTIME_AUDIT_PATH")
    or "state/corporate-action-lineage-runtime-audit.json"
).strip()

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

RAW_QUOTE_OPTIONAL_KEYS = [
    "previousClose",
    "regularMarketPreviousClose",
    "regularMarketChange",
    "regularMarketChangePercent",
]

RAW_FUNDAMENTAL_OPTIONAL_KEYS = [
    "netIncome",
    "netIncomeCommonStockholders",
]

RAW_TRACE_OPTIONAL_KEYS = [
    "instrumentType",
    "analysisEligible",
    "quoteTimestamp",
    "quoteSource",
    "netIncomeSource",
    "netIncomeAsOf",
    "targetMeanPriceSource",
    "targetMeanPriceRetrievedAt",
    "targetMeanPriceAsOf",
    "targetMeanPriceAsOfStatus",
]

STATE_TRACE_OPTIONAL_KEYS = [
    "historyPeriods",
    "historyTier",
    "symbolLifecycleState",
    "stateUpdatedAt",
    "historyMissingStreak",
    "quoteMissingStreak",
    "stateReason",
]

EXTENDED_OPTIONAL_KEYS = (
    DISTRESS_OPTIONAL_KEYS[:]
    + RAW_QUOTE_OPTIONAL_KEYS
    + RAW_FUNDAMENTAL_OPTIONAL_KEYS
    + RAW_TRACE_OPTIONAL_KEYS
    + STATE_TRACE_OPTIONAL_KEYS
)

STANDARD_KEYS = CORE_REQUIRED_KEYS + EXTENDED_OPTIONAL_KEYS

FIN_HISTORY_NET_INCOME_KEYS = [
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Including Noncontrolling Interests",
]

BENCHMARK_SPECS = [
    {"source": "^GSPC", "alias": "SP500_INDEX"},
    {"source": "^IXIC", "alias": "NASDAQ_INDEX"},
    {"source": "^VIX", "alias": "VIX_INDEX"},
]

MARKET_REGIME_FILENAME = "MARKET_REGIME_SNAPSHOT.json"
EARNINGS_EVENT_FILENAME = "EARNINGS_EVENT_MAP.json"
EARNINGS_EVENT_COVERAGE_AUDIT_FILENAME = "STAGE4_EARNINGS_EVENT_COVERAGE_AUDIT.json"
CORPORATE_ACTION_LINEAGE_AUDIT_FILENAME = "CORPORATE_ACTION_LINEAGE_RUNTIME_AUDIT.json"
HARVESTER_SYMBOL_STATE_FILENAME = "HARVESTER_SYMBOL_STATE.json"

VERIFIED_SYMBOL_CHANGE_STATUSES = {
    "VERIFIED_NO_SYMBOL_CHANGE_AS_OF_SOURCE",
    "VERIFIED_SYMBOL_CHANGE",
}
VERIFIED_DELISTING_STATUSES = {
    "VERIFIED_NOT_DELISTED_AS_OF_SOURCE",
    "VERIFIED_DELISTED",
}
VERIFIED_SUSPENSION_STATUSES = {
    "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE",
    "VERIFIED_SUSPENDED",
}
OHLCV_LINEAGE_MIN_BARS = 30  # Matches the existing Stage4 strict OHLCV minimum.
HARVESTER_MAPPING_FRESHNESS_AUDIT_FILENAME = "HARVESTER_MAPPING_FRESHNESS_AUDIT.json"
TICKER_MAPPING_REFRESH_AUDIT_FILENAME = "TICKER_MAPPING_REFRESH_AUDIT.json"
FMP_API_KEY = os.getenv("FMP_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_KEY") or os.getenv("FINNHUB_API_KEY")


def _read_bool_env(name, default=False):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_positive_int_env(name, default):
    try:
        raw = int(os.getenv(name, str(default)))
        if raw > 0:
            return raw
    except Exception:
        pass
    return default


SYMBOL_STATE_HISTORY_FULL_MIN_PERIODS = _read_positive_int_env("HARVESTER_HISTORY_FULL_MIN_PERIODS", 8)
SYMBOL_STATE_STALE_HISTORY_STREAK = _read_positive_int_env("HARVESTER_STALE_HISTORY_STREAK", 3)
SYMBOL_STATE_STALE_QUOTE_STREAK = _read_positive_int_env("HARVESTER_STALE_QUOTE_STREAK", 3)
SYMBOL_STATE_RETIRE_DAYS = _read_positive_int_env("HARVESTER_RETIRE_DAYS", 45)
HARVESTER_FAILURE_SAMPLE_LIMIT = _read_positive_int_env("HARVESTER_FAILURE_SAMPLE_LIMIT", 20)
HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT = _read_positive_int_env("HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT", 100)
HARVESTER_SKIP_RETIRED_SYMBOLS = _read_bool_env("HARVESTER_SKIP_RETIRED_SYMBOLS", True)
HARVESTER_SKIP_EXCLUDED_SYMBOLS = _read_bool_env("HARVESTER_SKIP_EXCLUDED_SYMBOLS", True)
HARVESTER_TICKER_MAPPING_REFRESH_ENABLED = _read_bool_env("HARVESTER_TICKER_MAPPING_REFRESH_ENABLED", True)
HARVESTER_TICKER_MAPPING_REFRESH_FAIL_OPEN = _read_bool_env("HARVESTER_TICKER_MAPPING_REFRESH_FAIL_OPEN", True)
HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON = _read_bool_env("HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON", False)
HARVESTER_TARGET_LINEAGE_MAX_AGE_HOURS = _read_positive_int_env("HARVESTER_TARGET_LINEAGE_MAX_AGE_HOURS", 48)
HARVESTER_EXTERNAL_CORPORATE_ACTION_ENABLED = _read_bool_env(
    "HARVESTER_EXTERNAL_CORPORATE_ACTION_ENABLED", True
)
HARVESTER_EXTERNAL_CORPORATE_ACTION_COVERAGE_YEARS = _read_positive_int_env(
    "HARVESTER_EXTERNAL_CORPORATE_ACTION_COVERAGE_YEARS", 5
)
HARVESTER_FMP_DELISTED_MAX_PAGES = _read_positive_int_env(
    "HARVESTER_FMP_DELISTED_MAX_PAGES", 50
)
HARVESTER_NASDAQ_HALT_RSS_MAX_AGE_HOURS = _read_positive_int_env(
    "HARVESTER_NASDAQ_HALT_RSS_MAX_AGE_HOURS", 120
)
HARVESTER_NASDAQ_HALT_RSS_RTH_MAX_AGE_MINUTES = _read_positive_int_env(
    "HARVESTER_NASDAQ_HALT_RSS_RTH_MAX_AGE_MINUTES", 15
)
HARVESTER_NASDAQ_HALT_RSS_MAX_FUTURE_SKEW_MINUTES = _read_positive_int_env(
    "HARVESTER_NASDAQ_HALT_RSS_MAX_FUTURE_SKEW_MINUTES", 15
)
RUN_FAILURE_DETAILS = []
RUN_SYMBOL_SKIP_DETAILS = []


def write_json_report(path, payload, label):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=True, indent=2)
        fp.write("\n")
    os.replace(tmp_path, path)
    print(f"🧾 {label} saved: {path}", flush=True)


def write_text_report(path, payload, label):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        fp.write(str(payload or ""))
    os.replace(tmp_path, path)
    print(f"🧾 {label} saved: {path}", flush=True)


def write_harvester_run_summary(payload):
    path = HARVESTER_RUN_SUMMARY_PATH or "state/last-harvester-run.json"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=True, indent=2)
        fp.write("\n")
    print(f"🧾 Harvester summary saved: {path}", flush=True)


def _short_failure_text(value, max_len=240):
    text = str(value if value is not None else "").replace("\n", " ").replace("`", "'").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def redact_secret_text(value):
    text = str(value if value is not None else "")
    text = re.sub(r"(?i)(apikey|api_key|token|key)=([^&\s]+)", r"\1=***", text)
    return text


def record_symbol_failure(symbol, stage, category, reason, **context):
    clean_context = {}
    for key, value in context.items():
        if value is None or value == "":
            continue
        clean_context[key] = _short_failure_text(value, 220)
    RUN_FAILURE_DETAILS.append(
        {
            "symbol": str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN",
            "stage": str(stage or "unknown").strip(),
            "category": str(category or "UNKNOWN").strip().upper(),
            "reason": _short_failure_text(reason or "unknown"),
            "context": clean_context,
        }
    )


def record_symbol_skip(symbol, stage, category, reason, **context):
    clean_context = {}
    for key, value in context.items():
        if value is None or value == "":
            continue
        clean_context[key] = _short_failure_text(value, 220)
    RUN_SYMBOL_SKIP_DETAILS.append(
        {
            "symbol": str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN",
            "stage": str(stage or "unknown").strip(),
            "category": str(category or "UNKNOWN").strip().upper(),
            "reason": _short_failure_text(reason or "unknown"),
            "context": clean_context,
        }
    )


def build_failure_snapshot():
    category_counts = Counter(item.get("category") or "UNKNOWN" for item in RUN_FAILURE_DETAILS)
    stage_counts = Counter(item.get("stage") or "unknown" for item in RUN_FAILURE_DETAILS)
    samples = RUN_FAILURE_DETAILS[:HARVESTER_FAILURE_SAMPLE_LIMIT]
    return {
        "total": len(RUN_FAILURE_DETAILS),
        "categoryCounts": dict(sorted(category_counts.items())),
        "stageCounts": dict(sorted(stage_counts.items())),
        "samples": samples,
    }


def build_skip_snapshot():
    category_counts = Counter(item.get("category") or "UNKNOWN" for item in RUN_SYMBOL_SKIP_DETAILS)
    stage_counts = Counter(item.get("stage") or "unknown" for item in RUN_SYMBOL_SKIP_DETAILS)
    samples = RUN_SYMBOL_SKIP_DETAILS[:HARVESTER_FAILURE_SAMPLE_LIMIT]
    return {
        "total": len(RUN_SYMBOL_SKIP_DETAILS),
        "categoryCounts": dict(sorted(category_counts.items())),
        "stageCounts": dict(sorted(stage_counts.items())),
        "samples": samples,
    }


def write_harvester_failure_report(payload):
    path = HARVESTER_FAILURE_REPORT_PATH or "state/harvester-failure-report.json"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=True, indent=2)
        fp.write("\n")
    print(f"🧾 Harvester failure report saved: {path}", flush=True)


def build_harvester_failure_report(summary):
    snapshot = build_failure_snapshot()
    skip_snapshot = build_skip_snapshot()
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "summary": summary,
        "failureSummary": {
            "total": snapshot["total"],
            "categoryCounts": snapshot["categoryCounts"],
            "stageCounts": snapshot["stageCounts"],
            "sampleLimit": HARVESTER_FAILURE_SAMPLE_LIMIT,
        },
        "skipSummary": {
            "total": skip_snapshot["total"],
            "categoryCounts": skip_snapshot["categoryCounts"],
            "stageCounts": skip_snapshot["stageCounts"],
            "sampleLimit": HARVESTER_FAILURE_SAMPLE_LIMIT,
        },
        "failures": RUN_FAILURE_DETAILS,
        "skips": RUN_SYMBOL_SKIP_DETAILS,
    }


def failure_telegram_summary():
    if not RUN_FAILURE_DETAILS:
        failure_text = "실패 상세: `none`"
    else:
        snapshot = build_failure_snapshot()
        count_preview = ", ".join(f"{k}:{v}" for k, v in snapshot["categoryCounts"].items()) or "unknown"
        sample_preview = ", ".join(
            f"{item.get('symbol')}:{item.get('category')}/{item.get('reason')}"
            for item in snapshot["samples"][:5]
        )
        failure_text = f"실패상세: `{count_preview}`\n샘플: `{_short_failure_text(sample_preview, 900)}`"

    if not RUN_SYMBOL_SKIP_DETAILS:
        return failure_text
    skip_snapshot = build_skip_snapshot()
    skip_preview = ", ".join(f"{k}:{v}" for k, v in skip_snapshot["categoryCounts"].items()) or "unknown"
    return f"{failure_text}\n라이프사이클 skip: `{skip_preview}`"

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
DAILY_BATCH_ALL_LABEL = "전체 (A-Z & 기타)"
DAILY_BATCH_FIRST_CHARS = "ABCDEFGHIJK"
DAILY_BATCH_SECOND_CHARS = "LMNOPQRSTUVWXYZ0123456789"
DAILY_BATCH_ALL_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

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


def resolve_daily_batch(now_kst):
    mode = DAILY_BATCH_MODE
    # workflow_dispatch 테스트 모드: 강제 1차/2차/전체 선택
    if mode in ("first", "1", "phase1", "batch1"):
        return DAILY_BATCH_FIRST_LABEL, DAILY_BATCH_FIRST_CHARS, "manual:first"
    if mode in ("second", "2", "phase2", "batch2"):
        return DAILY_BATCH_SECOND_LABEL, DAILY_BATCH_SECOND_CHARS, "manual:second"
    if mode in ("all", "full", "both"):
        return DAILY_BATCH_ALL_LABEL, DAILY_BATCH_ALL_CHARS, "manual:all"
    # 기본: 기존 로직 유지 (KST 시간대에 따른 자동 분할)
    current_hour = now_kst.hour
    if 6 <= current_hour <= 11:
        return DAILY_BATCH_FIRST_LABEL, DAILY_BATCH_FIRST_CHARS, "auto:hour_window_first"
    return DAILY_BATCH_SECOND_LABEL, DAILY_BATCH_SECOND_CHARS, "auto:hour_window_second"

def _resolve_telegram_chat_id(channel="ops"):
    if channel == "alert":
        return TELEGRAM_ALERT_CHAT_ID or TELEGRAM_SIMULATION_CHAT_ID
    return TELEGRAM_SIMULATION_CHAT_ID


def send_telegram(message, channel="ops"):
    chat_id = _resolve_telegram_chat_id(channel)
    if not TELEGRAM_TOKEN or not chat_id:
        print(f"ℹ️ Telegram skip: token/chat missing (channel={channel})", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    chat_mask = f"{str(chat_id)[:3]}***{str(chat_id)[-3:]}" if len(str(chat_id)) >= 7 else str(chat_id)
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"📨 Telegram sent (channel={channel}, chat={chat_mask})", flush=True)
    except requests.RequestException as e:
        print(f"⚠️ Telegram 알림 실패 (channel={channel}, chat={chat_mask}): {type(e).__name__}: {e}", flush=True)

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


TARGET_LINEAGE_KEYS = (
    "targetMeanPrice",
    "targetMeanPriceSource",
    "targetMeanPriceRetrievedAt",
    "targetMeanPriceAsOf",
    "targetMeanPriceAsOfStatus",
)


def _is_positive_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _has_complete_target_lineage(record):
    return (
        isinstance(record, dict)
        and _is_positive_finite(record.get("targetMeanPrice"))
        and bool(str(record.get("targetMeanPriceSource") or "").strip())
        and bool(str(record.get("targetMeanPriceRetrievedAt") or "").strip())
        and bool(str(record.get("targetMeanPriceAsOfStatus") or "").strip())
    )


def _merge_target_lineage_bundle(prev_record, raw_record):
    if _has_complete_target_lineage(raw_record):
        return {key: raw_record.get(key) for key in TARGET_LINEAGE_KEYS}
    if _is_positive_finite(raw_record.get("targetMeanPrice") if isinstance(raw_record, dict) else None):
        return {
            "targetMeanPrice": None,
            "targetMeanPriceSource": None,
            "targetMeanPriceRetrievedAt": None,
            "targetMeanPriceAsOf": None,
            "targetMeanPriceAsOfStatus": "TARGET_LINEAGE_INVALIDATED_MISSING_PROVENANCE",
        }
    if _has_complete_target_lineage(prev_record):
        return {key: prev_record.get(key) for key in TARGET_LINEAGE_KEYS}

    legacy_target_present = _is_positive_finite(
        prev_record.get("targetMeanPrice") if isinstance(prev_record, dict) else None
    )
    return {
        "targetMeanPrice": None,
        "targetMeanPriceSource": None,
        "targetMeanPriceRetrievedAt": None,
        "targetMeanPriceAsOf": None,
        "targetMeanPriceAsOfStatus": (
            "TARGET_LINEAGE_INVALIDATED_MISSING_PROVENANCE"
            if legacy_target_present
            else "TARGET_SOURCE_NOT_AVAILABLE"
        ),
    }


def merge_standard_record(prev_record, raw_record):
    merged = {}
    for key in STANDARD_KEYS:
        new_v = raw_record.get(key, None) if isinstance(raw_record, dict) else None
        old_v = prev_record.get(key, None) if isinstance(prev_record, dict) else None
        merged[key] = new_v if new_v is not None else old_v
    merged.update(_merge_target_lineage_bundle(prev_record, raw_record))
    return merged

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

def _classify_instrument_type(symbol, name, quote_type=None):
    s = str(symbol or '').strip().upper()
    n = str(name or '').strip().lower()
    q = str(quote_type or '').strip().upper()
    ordinary_share_name = bool(re.search(r"\bordinary shares?\b", n))

    if q == 'ETF' or n.endswith(' etf') or ' etf' in n or 'exchange traded fund' in n:
        return 'etf'
    if q == 'WARRANT' or s.endswith('.WS') or s.endswith('-WS') or s.endswith('.W') or s.endswith('-W') or ' warrant' in n:
        return 'warrant'
    if q == 'UNIT' or s.endswith('.U') or s.endswith('-U') or ' unit' in n:
        return 'unit'
    if q == 'RIGHT' or s.endswith('.R') or s.endswith('-R') or ' right' in n:
        return 'right'

    hybrid_keywords = [
        'preferred',
        'preference',
        ' pfd ',
        ' pfd',
        'depositary',
        ' dep shs',
        'subordinat',
        'capital security',
        'notes',
        'etn',
        'baby bond',
        'perpetual',
        'liquidation preference',
    ]
    if any(k in n for k in hybrid_keywords) and not (ordinary_share_name and q != 'PREFERRED'):
        return 'hybrid'
    if q in {'ETN', 'MUTUALFUND', 'PREFERRED', 'BOND', 'OPTION'}:
        return 'hybrid'

    return 'common'


def _collection_instrument_profile(ticker, listing_info, quote_info):
    listing_info = listing_info if isinstance(listing_info, dict) else {}
    quote_info = quote_info if isinstance(quote_info, dict) else {}
    listing_type = str(listing_info.get("instrumentType") or "").strip().lower()
    if listing_type:
        listing_eligible = listing_info.get("analysisEligible")
        return (
            listing_type,
            bool(listing_eligible) if listing_eligible is not None else listing_type == "common",
        )
    instrument_type = _classify_instrument_type(
        ticker,
        quote_info.get('shortName') or quote_info.get('longName'),
        quote_info.get('quoteType'),
    )
    return instrument_type, instrument_type == 'common'

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


LISTING_SOURCE_SPECS = [
    {
        "name": "nasdaqtrader_nasdaqlisted",
        "url": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "symbolField": "Symbol",
        "exchange": "NASDAQ",
    },
    {
        "name": "nasdaqtrader_otherlisted",
        "url": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
        "symbolField": "ACT Symbol",
        "exchange": None,
    },
]


EXCHANGE_CODE_LABELS = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "V": "IEX",
    "Z": "Cboe BZX",
}

EXTERNAL_CORPORATE_ACTION_SCHEMA_VERSION = "external-corporate-action-coverage-v1"
FMP_DELISTED_ENDPOINT = "https://financialmodelingprep.com/stable/delisted-companies"
NASDAQ_HALT_SEARCH_PAGE = "https://www.nasdaqtrader.com/Trader.aspx?id=TradingHaltSearch"
NASDAQ_HALT_RPC_ENDPOINT = "https://www.nasdaqtrader.com/RPCHandler.axd"
NASDAQ_HALT_RSS_ENDPOINT = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NASDAQ_HISTORICAL_SUSPENSION_CODES = {"H4", "H9", "H10", "H11", "M1", "T6", "T12"}
NASDAQ_HALT_REASON_LABELS = {
    "H4": "non_compliance",
    "H9": "filings_not_current",
    "H10": "sec_trading_suspension",
    "H11": "regulatory_concern",
    "M1": "corporate_action",
    "T6": "extraordinary_market_activity",
    "T12": "additional_information_requested",
}


def _canonical_sha256(payload) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_event_symbol(value) -> str:
    return _normalize_listing_symbol_for_yfinance(value)


def _parse_date(value) -> datetime.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _coverage_start_date(end_date: datetime.date) -> datetime.date:
    try:
        return end_date.replace(
            year=end_date.year - HARVESTER_EXTERNAL_CORPORATE_ACTION_COVERAGE_YEARS
        )
    except ValueError:
        return end_date.replace(
            year=end_date.year - HARVESTER_EXTERNAL_CORPORATE_ACTION_COVERAGE_YEARS,
            day=28,
        )


def _one_year_coverage_start(end_date: datetime.date) -> datetime.date:
    try:
        return end_date.replace(year=end_date.year - 1)
    except ValueError:
        return end_date.replace(year=end_date.year - 1, day=28)


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif lowered == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _parse_html_table_rows_with_contract(
    html_text: str,
) -> tuple[list[dict], int, bool]:
    parser = _HtmlTableParser()
    parser.feed(str(html_text or ""))
    if not parser.rows:
        return [], 0, False
    headers = [str(value).strip() for value in parser.rows[0]]
    raw_rows = parser.rows[1:]
    shape_valid = bool(
        headers
        and raw_rows
        and all(len(row) == len(headers) for row in raw_rows)
    )
    rows = [
        dict(zip(headers, row))
        for row in raw_rows
        if len(row) == len(headers)
    ]
    return rows, len(raw_rows), shape_valid


def _parse_html_table_rows(html_text: str) -> list[dict]:
    rows, _, _ = _parse_html_table_rows_with_contract(html_text)
    return rows


def _market_timestamp(date_value, time_value) -> str | None:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if not date_text or not time_text:
        return None
    try:
        parsed = datetime.datetime.strptime(
            f"{date_text[:10]} {time_text}",
            "%m/%d/%Y %H:%M:%S.%f" if "." in time_text else "%m/%d/%Y %H:%M:%S",
        )
        return parsed.replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
    except ValueError:
        return None


def _parse_fmp_delisted_rows(payload) -> list[dict]:
    rows = []
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, dict):
            continue
        symbol = _normalize_event_symbol(raw.get("symbol"))
        effective = _parse_date(raw.get("delistedDate"))
        if not symbol or not effective:
            continue
        rows.append(
            {
                "symbol": symbol,
                "companyName": raw.get("companyName") or None,
                "exchange": raw.get("exchange") or None,
                "ipoDate": str(raw.get("ipoDate") or "")[:10] or None,
                "eventEffectiveAt": effective.isoformat(),
            }
        )
    return sorted(rows, key=lambda row: (row["eventEffectiveAt"], row["symbol"]))


def _fmp_delisted_payload_contract_valid(payload) -> bool:
    return bool(
        isinstance(payload, list)
        and all(
            isinstance(row, dict)
            and _normalize_event_symbol(row.get("symbol"))
            and _parse_date(row.get("delistedDate"))
            for row in payload
        )
    )


def _events_within_coverage(
    rows: list[dict],
    coverage_start: datetime.date,
    coverage_end: datetime.date,
    *,
    retain_current_active: bool = False,
    source_as_of: str | None = None,
) -> list[dict]:
    as_of = _parse_iso_datetime(source_as_of)
    return [
        dict(row)
        for row in rows or []
        if (
            isinstance(row, dict)
            and (
                (
                    (effective := _parse_date(row.get("eventEffectiveAt")))
                    and coverage_start <= effective <= coverage_end
                )
                or (
                    retain_current_active
                    and row.get("currentFeedObserved") is True
                    and (
                        (resumed := _parse_iso_datetime(row.get("resumedAt"))) is None
                        or as_of is None
                        or resumed > as_of
                    )
                )
            )
        )
    ]


def _parse_nasdaq_halt_rows(
    html_text: str,
    *,
    allowed_codes: set[str] | None = None,
) -> list[dict]:
    allowed_codes = allowed_codes or NASDAQ_HISTORICAL_SUSPENSION_CODES
    rows = []
    for raw in _parse_html_table_rows(html_text):
        reason_code = str(raw.get("Reason Code") or "").strip().upper()
        if reason_code not in allowed_codes:
            continue
        symbol = _normalize_event_symbol(raw.get("Issue Symbol"))
        effective = _market_timestamp(raw.get("Halt Date"), raw.get("Halt Time"))
        if not symbol or not effective:
            continue
        resumed = _market_timestamp(
            raw.get("Resumption Date"),
            raw.get("Resumption Trade Time"),
        )
        rows.append(
            {
                "symbol": symbol,
                "eventEffectiveAt": effective,
                "resumedAt": resumed,
                "reasonCode": reason_code,
                "reason": NASDAQ_HALT_REASON_LABELS[reason_code],
                "market": raw.get("Market") or None,
                "currentFeedObserved": False,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["eventEffectiveAt"],
            row["symbol"],
            row["reasonCode"],
        ),
    )


def _parse_rfc2822_datetime(value: str | None) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _rss_publication_contract(
    published_at: str | None,
    retrieved_at: str,
) -> tuple[bool, str | None, str | None, float | None, str]:
    published = _parse_rfc2822_datetime(published_at)
    retrieved = _parse_iso_datetime(retrieved_at)
    if published is None or retrieved is None:
        return (
            False,
            "current_feed_published_at_invalid",
            None,
            None,
            "UNDETERMINED",
        )
    age_hours = (retrieved - published).total_seconds() / 3600
    max_future_skew_hours = (
        HARVESTER_NASDAQ_HALT_RSS_MAX_FUTURE_SKEW_MINUTES / 60
    )
    retrieved_market = retrieved.astimezone(ZoneInfo("America/New_York"))
    minute_of_day = retrieved_market.hour * 60 + retrieved_market.minute
    regular_session_window = bool(
        retrieved_market.weekday() < 5
        and (9 * 60 + 30) <= minute_of_day <= (16 * 60 + 15)
    )
    freshness_mode = (
        "WEEKDAY_REGULAR_SESSION_WINDOW"
        if regular_session_window
        else "OFF_SESSION_WEEKEND_OR_HOLIDAY_TOLERANCE"
    )
    max_age_hours = (
        HARVESTER_NASDAQ_HALT_RSS_RTH_MAX_AGE_MINUTES / 60
        if regular_session_window
        else HARVESTER_NASDAQ_HALT_RSS_MAX_AGE_HOURS
    )
    if age_hours < -max_future_skew_hours:
        return (
            False,
            "current_feed_published_at_future",
            published.isoformat().replace("+00:00", "Z"),
            age_hours,
            freshness_mode,
        )
    if age_hours > max_age_hours:
        return (
            False,
            "current_feed_published_at_stale",
            published.isoformat().replace("+00:00", "Z"),
            age_hours,
            freshness_mode,
        )
    return (
        True,
        None,
        published.isoformat().replace("+00:00", "Z"),
        age_hours,
        freshness_mode,
    )


def _parse_nasdaq_current_halt_rss(xml_payload: bytes) -> tuple[bool, str | None, list[dict]]:
    try:
        root = ElementTree.fromstring(xml_payload)
    except (ElementTree.ParseError, TypeError, ValueError):
        return False, None, []
    channel = root.find("./channel")
    if channel is None:
        return False, None, []
    namespace = {"ndaq": "http://www.nasdaqtrader.com/"}
    declared_count = channel.find("ndaq:numItems", namespace)
    items = channel.findall("./item")
    try:
        expected_count = int(str(declared_count.text or "").strip())
    except (AttributeError, TypeError, ValueError):
        return False, None, []
    if expected_count != len(items):
        return False, None, []
    rows = []
    for item in items:
        def field(name: str) -> str:
            node = item.find(f"ndaq:{name}", namespace)
            return str(node.text or "").strip() if node is not None else ""

        symbol = _normalize_event_symbol(field("IssueSymbol"))
        reason_code = field("ReasonCode").upper()
        effective = _market_timestamp(field("HaltDate"), field("HaltTime"))
        if not symbol or not reason_code or not effective:
            return False, None, []
        rows.append(
            {
                "symbol": symbol,
                "eventEffectiveAt": effective,
                "resumedAt": _market_timestamp(
                    field("ResumptionDate"),
                    field("ResumptionTradeTime"),
                ),
                "reasonCode": reason_code,
                "reason": NASDAQ_HALT_REASON_LABELS.get(
                    reason_code,
                    f"nasdaq_halt_{reason_code.lower()}",
                ),
                "market": field("Market") or None,
                "currentFeedObserved": True,
            }
        )
    return (
        True,
        str(channel.findtext("pubDate") or "").strip() or None,
        sorted(
            rows,
            key=lambda row: (
                row["eventEffectiveAt"],
                row["symbol"],
                row["reasonCode"],
            ),
        ),
    )


def _proof_from_source(
    source: dict,
    symbol: str,
    *,
    matched_symbol: str | None = None,
    match_status: str = "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE",
) -> dict:
    return {
        "source": source.get("source"),
        "sourceAsOf": source.get("sourceAsOf"),
        "sourceAsOfBasis": source.get("sourceAsOfBasis"),
        "retrievedAt": source.get("retrievedAt"),
        "requestStatus": "SUCCESS",
        "requestedSymbol": symbol,
        "matchedSymbol": matched_symbol,
        "symbolMatchStatus": match_status,
        "symbolMatchMethod": "DETERMINISTIC_EXACT_NORMALIZED_SYMBOL_LOOKUP",
        "sourceScopeComplete": (
            str(source.get("status") or "").upper() == "SUCCESS"
            and source.get("partialResponse") is False
        ),
        "coverageStart": source.get("coverageStart"),
        "coverageEnd": source.get("coverageEnd"),
        "partialResponse": False,
        "responseSha256": source.get("responseSha256"),
        "queryScope": source.get("queryScope") or "ALL_US_MARKETS",
        "requestScopeSymbolsSha256": source.get("requestScopeSymbolsSha256"),
    }


def _events_by_symbol(rows: list[dict], key: str = "symbol") -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        symbol = _normalize_event_symbol(row.get(key)) if isinstance(row, dict) else ""
        if symbol:
            grouped.setdefault(symbol, []).append(dict(row))
    for symbol in grouped:
        grouped[symbol].sort(
            key=lambda row: (
                str(row.get("eventEffectiveAt") or ""),
                str(row.get("reasonCode") or ""),
            )
        )
    return grouped


def _preserved_evidence_after_refresh_failure(
    previous_evidence,
    source: dict,
) -> dict | None:
    if not isinstance(previous_evidence, dict):
        return None
    preserved = dict(previous_evidence)
    preserved["preservedStatus"] = preserved.get("status")
    preserved["status"] = "UNVERIFIED_EXTERNAL_SOURCE_REFRESH_FAILED"
    preserved["reason"] = (
        source.get("reason")
        or source.get("status")
        or "external_source_refresh_failed"
    )
    preserved["refreshFailureAt"] = source.get("retrievedAt")
    return preserved


def _symbol_change_chain(events: dict, symbol: str) -> list[dict]:
    chain = []
    current = symbol
    visited = set()
    while current and current not in visited:
        visited.add(current)
        matches = [
            event
            for event in events.get(current, [])
            if _normalize_event_symbol(event.get("newSymbol")) == current
        ]
        if not matches:
            break
        selected = sorted(
            matches,
            key=lambda event: str(event.get("eventEffectiveAt") or ""),
        )[-1]
        chain.append(dict(selected))
        current = _normalize_event_symbol(selected.get("oldSymbol"))
    return sorted(chain, key=lambda event: str(event.get("eventEffectiveAt") or ""))


def _apply_symbol_change_evidence(row: dict, events: dict, source: dict, symbol: str) -> str:
    if str(source.get("status") or "").upper() != "SUCCESS":
        preserved = _preserved_evidence_after_refresh_failure(
            row.get("symbolChangeEvidence"),
            source,
        )
        if preserved is None:
            row.pop("symbolChangeEvidence", None)
        else:
            row["symbolChangeEvidence"] = preserved
        return "blocked"
    matching = _symbol_change_chain(events, symbol)
    proof = _proof_from_source(
        source,
        symbol,
        matched_symbol=symbol if matching else None,
        match_status=(
            "EXACT_EVENT_MATCH"
            if matching
            else "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE"
        ),
    )
    if matching:
        event = matching[-1]
        row["symbolChangeEvidence"] = {
            **proof,
            "status": "VERIFIED_SYMBOL_CHANGE",
            "oldSymbol": matching[0].get("oldSymbol"),
            "newSymbol": event.get("newSymbol"),
            "eventEffectiveAt": event.get("eventEffectiveAt"),
            "events": matching,
        }
        return "event"
    conflicts = [
        event
        for event in events.get(symbol, [])
        if _normalize_event_symbol(event.get("oldSymbol")) == symbol
    ]
    if conflicts:
        event = sorted(
            conflicts,
            key=lambda item: str(item.get("eventEffectiveAt") or ""),
        )[-1]
        row["symbolChangeEvidence"] = {
            **_proof_from_source(
                source,
                symbol,
                matched_symbol=symbol,
                match_status="EXACT_EVENT_MATCH",
            ),
            "status": "UNVERIFIED_SOURCE_CONFLICT",
            "reason": "active_symbol_matches_historical_old_symbol",
            "eventEffectiveAt": event.get("eventEffectiveAt"),
            "events": conflicts,
        }
        return "conflict"
    row["symbolChangeEvidence"] = {
        **proof,
        "status": "VERIFIED_NO_SYMBOL_CHANGE_AS_OF_SOURCE",
    }
    return "no_event"


def _apply_delisting_evidence(row: dict, events: dict, source: dict, symbol: str) -> str:
    if str(source.get("status") or "").upper() != "SUCCESS":
        preserved = _preserved_evidence_after_refresh_failure(
            row.get("delistingEvidence"),
            source,
        )
        if preserved is None:
            row.pop("delistingEvidence", None)
        else:
            row["delistingEvidence"] = preserved
        return "blocked"
    matching = events.get(symbol, [])
    proof = _proof_from_source(
        source,
        symbol,
        matched_symbol=symbol if matching else None,
        match_status=(
            "EXACT_EVENT_MATCH"
            if matching
            else "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE"
        ),
    )
    if matching:
        event = matching[-1]
        row["delistingEvidence"] = {
            **proof,
            "status": "UNVERIFIED_SOURCE_CONFLICT",
            "reason": "active_listing_conflicts_with_delisted_event",
            "eventEffectiveAt": event.get("eventEffectiveAt"),
            "events": matching,
        }
        return "conflict"
    row["delistingEvidence"] = {
        **proof,
        "status": "VERIFIED_NOT_DELISTED_AS_OF_SOURCE",
    }
    return "no_event"


def _apply_suspension_evidence(row: dict, events: dict, source: dict, symbol: str) -> str:
    if str(source.get("status") or "").upper() != "SUCCESS":
        preserved = _preserved_evidence_after_refresh_failure(
            row.get("suspensionEvidence"),
            source,
        )
        if preserved is None:
            row.pop("suspensionEvidence", None)
        else:
            row["suspensionEvidence"] = preserved
        return "blocked"
    matching = events.get(symbol, [])
    source_as_of = _parse_iso_datetime(source.get("sourceAsOf"))
    active = []
    for event in matching:
        resumed_at = _parse_iso_datetime(event.get("resumedAt"))
        if (
            event.get("currentFeedObserved") is True
            and (
                resumed_at is None
                or source_as_of is None
                or resumed_at > source_as_of
            )
        ):
            active.append(event)
    all_resumed = bool(
        matching
        and source_as_of
        and all(
            (resumed_at := _parse_iso_datetime(event.get("resumedAt")))
            and resumed_at <= source_as_of
            for event in matching
        )
    )
    proof = _proof_from_source(
        source,
        symbol,
        matched_symbol=symbol if matching else None,
        match_status=(
            "EXACT_HISTORICAL_EVENT_MATCH_CURRENTLY_RESUMED"
            if matching and not active and all_resumed
            else "EXACT_HISTORICAL_EVENT_MATCH_NOT_IN_CURRENT_FEED"
            if matching and not active
            else "EXACT_EVENT_MATCH"
            if matching
            else "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE"
        ),
    )
    if active:
        event = active[-1]
        row["suspensionEvidence"] = {
            **proof,
            "status": "VERIFIED_SUSPENDED",
            "eventEffectiveAt": event.get("eventEffectiveAt"),
            "reason": event.get("reason"),
            "events": matching,
        }
        return "active_event"
    row["suspensionEvidence"] = {
        **proof,
        "status": "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE",
        "events": matching,
    }
    return "resumed_event" if matching else "no_event"


def apply_external_corporate_action_coverage(
    mapping: dict,
    coverage: dict,
) -> tuple[dict, dict]:
    result = {
        symbol: dict(row)
        for symbol, row in mapping.items()
        if (
            isinstance(symbol, str)
            and not symbol.startswith("_")
            and isinstance(row, dict)
        )
    }
    sources = coverage.get("sources") if isinstance(coverage.get("sources"), dict) else {}
    event_rows = coverage.get("events") if isinstance(coverage.get("events"), dict) else {}
    symbol_change_events: dict[str, list[dict]] = {}
    for event in event_rows.get("symbolChanges") or []:
        if not isinstance(event, dict):
            continue
        for value in (event.get("oldSymbol"), event.get("newSymbol")):
            symbol = _normalize_event_symbol(value)
            if symbol:
                symbol_change_events.setdefault(symbol, []).append(dict(event))
    delisting_events = _events_by_symbol(event_rows.get("delistings") or [])
    suspension_events = _events_by_symbol(event_rows.get("suspensions") or [])
    counts = Counter()
    for symbol in sorted(result):
        row = result[symbol]
        counts[f"symbolChange:{_apply_symbol_change_evidence(row, symbol_change_events, sources.get('symbolChange') or {}, symbol)}"] += 1
        counts[f"delisting:{_apply_delisting_evidence(row, delisting_events, sources.get('delisting') or {}, symbol)}"] += 1
        counts[f"suspension:{_apply_suspension_evidence(row, suspension_events, sources.get('suspension') or {}, symbol)}"] += 1
    source_conflict_rows = sum(
        1
        for row in result.values()
        if any(
            str((row.get(key) or {}).get("status") or "").upper() == "UNVERIFIED_SOURCE_CONFLICT"
            for key in ("symbolChangeEvidence", "delistingEvidence", "suspensionEvidence")
        )
    )
    summary = {
        "totalRows": len(result),
        "symbolChangeBlockedRows": counts["symbolChange:blocked"],
        "delistingBlockedRows": counts["delisting:blocked"],
        "suspensionBlockedRows": counts["suspension:blocked"],
        "sourceConflictRows": source_conflict_rows,
        "unknownRows": 0,
        "statusCounts": dict(sorted(counts.items())),
    }
    return result, summary


def _normalize_listing_symbol_for_yfinance(symbol):
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    # Nasdaq Trader class-share symbols use dots; Yahoo/yfinance commonly
    # expects dashes for listed class shares.
    return text.replace("/", "-").replace(".", "-").replace("$", "-")


def _group_for_symbol(symbol):
    text = str(symbol or "").strip().upper()
    if not text:
        return "0"
    first = text[0]
    return first if first.isalpha() else "0"


def _parse_pipe_listing_text(text, spec):
    rows = []
    creation_time = None
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return rows, creation_time
    header = [part.strip() for part in lines[0].split("|")]
    for line in lines[1:]:
        if line.startswith("File Creation Time:"):
            creation_time = line.split(":", 1)[1].strip()
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < len(header):
            continue
        row = dict(zip(header, parts))
        raw_symbol = row.get(spec.get("symbolField")) or row.get("Symbol") or row.get("ACT Symbol")
        symbol = _normalize_listing_symbol_for_yfinance(raw_symbol)
        if not symbol:
            continue
        test_issue = str(row.get("Test Issue") or "").strip().upper()
        if test_issue and test_issue != "N":
            continue
        name = row.get("Security Name") or row.get("Security Name ") or ""
        etf_flag = str(row.get("ETF") or "").strip().upper() == "Y"
        ordinary_share_name = bool(re.search(r"(?i)\bordinary shares?\b", name))
        preferred_flag = "$" in str(raw_symbol or "") or bool(
            re.search(
                r"(?i)(preferred|preference|\bpfd\b|dep shs|depositary|perpetual|liquidation preference)",
                name,
            )
            and not ordinary_share_name
        )
        quote_type = "ETF" if etf_flag else ("PREFERRED" if preferred_flag else "EQUITY")
        instrument_type = _classify_instrument_type(symbol, name, quote_type)
        exchange_code = row.get("Exchange") or spec.get("exchange") or "UNKNOWN"
        exchange_label = EXCHANGE_CODE_LABELS.get(exchange_code, exchange_code)
        rows.append(
            {
                "symbol": symbol,
                "sourceSymbol": str(raw_symbol or "").strip().upper(),
                "name": name,
                "group": _group_for_symbol(symbol),
                "exchange": exchange_label,
                "exchangeCode": exchange_code,
                "listingSource": spec.get("name"),
                "listingStatus": "ACTIVE",
                "instrumentType": instrument_type,
                "analysisEligible": instrument_type == "common",
                "isEtf": etf_flag,
                "roundLotSize": row.get("Round Lot Size"),
                "nasdaqSymbol": row.get("NASDAQ Symbol"),
            }
        )
    return rows, creation_time


def fetch_authoritative_listing_rows():
    rows = {}
    source_summaries = []
    for spec in LISTING_SOURCE_SPECS:
        url = spec.get("url")
        name = spec.get("name")
        summary = {"name": name, "url": url, "status": "pending", "rowCount": 0}
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            parsed_rows, creation_time = _parse_pipe_listing_text(response.text, spec)
            for row in parsed_rows:
                rows[row["symbol"]] = row
            summary.update(
                {
                    "status": "ok",
                    "rowCount": len(parsed_rows),
                    "sourceCreationTime": creation_time,
                }
            )
        except Exception as e:
            summary.update(
                {
                    "status": "failed",
                    "errorType": type(e).__name__,
                    "errorMessage": _short_failure_text(e),
                }
            )
        source_summaries.append(summary)
    return rows, source_summaries


def _previous_external_coverage(previous_audit) -> dict:
    if not isinstance(previous_audit, dict):
        return {}
    coverage = previous_audit.get("externalCorporateActionCoverage")
    return coverage if isinstance(coverage, dict) else {}


def _merge_external_events(previous_rows, current_rows, key_fields) -> list[dict]:
    merged = {}
    for row in [*(previous_rows or []), *(current_rows or [])]:
        if not isinstance(row, dict):
            continue
        key = tuple(str(row.get(field) or "") for field in key_fields)
        if all(key):
            merged[key] = dict(row)
    return sorted(merged.values(), key=lambda row: tuple(str(row.get(field) or "") for field in key_fields))


def _source_failure(source, status, reason, *, retrieved_at, **extra) -> dict:
    return {
        "status": status,
        "source": source,
        "reason": reason,
        "retrievedAt": retrieved_at,
        "partialResponse": True,
        **extra,
    }


def _request_with_backoff(callable_request, attempts=3):
    last_response = None
    for attempt in range(attempts):
        response = callable_request()
        last_response = response
        if getattr(response, "status_code", None) != 429:
            return response
        if attempt < attempts - 1:
            time.sleep(1.0 * (2**attempt))
    return last_response


def _fetch_fmp_delisting_coverage(
    session,
    *,
    api_key,
    coverage_start,
    coverage_end,
    retrieved_at,
    previous_coverage,
) -> tuple[dict, list[dict]]:
    if not api_key:
        return (
            _source_failure(
                "FMP_DELISTED_COMPANIES",
                "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
                "credential_missing",
                retrieved_at=retrieved_at,
            ),
            previous_coverage.get("events", {}).get("delistings", []),
        )
    page_hashes = []
    fetched_rows = []
    completed = False
    for page in range(HARVESTER_FMP_DELISTED_MAX_PAGES):
        response = _request_with_backoff(
            lambda page=page: session.get(
                FMP_DELISTED_ENDPOINT,
                params={"page": page, "limit": 100},
                headers={"apikey": api_key, "User-Agent": "US-Alpha-Seeker-Harvester/1.0"},
                timeout=30,
            )
        )
        status_code = getattr(response, "status_code", None)
        if status_code in {401, 402, 403}:
            return (
                _source_failure(
                    "FMP_DELISTED_COMPANIES",
                    "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
                    f"entitlement_or_auth_http_{status_code}",
                    retrieved_at=retrieved_at,
                ),
                previous_coverage.get("events", {}).get("delistings", []),
            )
        if status_code != 200:
            return (
                _source_failure(
                    "FMP_DELISTED_COMPANIES",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"http_{status_code}",
                    retrieved_at=retrieved_at,
                ),
                previous_coverage.get("events", {}).get("delistings", []),
            )
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if not _fmp_delisted_payload_contract_valid(payload):
            return (
                _source_failure(
                    "FMP_DELISTED_COMPANIES",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    "response_schema_invalid",
                    retrieved_at=retrieved_at,
                ),
                previous_coverage.get("events", {}).get("delistings", []),
            )
        page_hashes.append(hashlib.sha256(response.content).hexdigest())
        if not payload:
            completed = bool(fetched_rows)
            break
        parsed = _parse_fmp_delisted_rows(payload)
        fetched_rows.extend(parsed)
        if len(payload) < 100:
            completed = True
            break
        time.sleep(0.2)
    current_rows = _events_within_coverage(
        fetched_rows,
        coverage_start,
        coverage_end,
    )
    preserved_rows = _events_within_coverage(
        previous_coverage.get("events", {}).get("delistings", []),
        coverage_start,
        coverage_end,
    )
    merged_rows = _merge_external_events(
        [],
        current_rows if completed else preserved_rows,
        ("symbol", "eventEffectiveAt"),
    )
    response_sha = _canonical_sha256(
        {
            "pageHashes": page_hashes,
            "events": merged_rows,
        }
    )
    summary = {
        "status": "SUCCESS" if completed else "UNVERIFIED_PARTIAL_RESPONSE",
        "source": "FMP_DELISTED_COMPANIES",
        "sourceAsOf": retrieved_at,
        "sourceAsOfBasis": "RETRIEVAL_TIME_NO_VENDOR_TIMESTAMP",
        "retrievedAt": retrieved_at,
        "coverageStart": coverage_start.isoformat(),
        "coverageEnd": coverage_end.isoformat(),
        "partialResponse": not completed,
        "responseSha256": response_sha,
        "queryScope": "US_DELISTED_COMPANIES_ALL",
        "requestCount": len(page_hashes),
        "eventCount": len(merged_rows),
        "partialObservedEventCount": 0 if completed else len(current_rows),
        "preservedEventCount": 0 if completed else len(preserved_rows),
    }
    return summary, merged_rows


def _fetch_nasdaq_suspension_coverage(
    session,
    *,
    coverage_start,
    coverage_end,
    retrieved_at,
    previous_coverage,
) -> tuple[dict, list[dict]]:
    headers = {"User-Agent": "US-Alpha-Seeker-Harvester/1.0"}
    history_coverage_start = max(
        coverage_start,
        _one_year_coverage_start(coverage_end),
    )
    try:
        landing = session.get(NASDAQ_HALT_SEARCH_PAGE, headers=headers, timeout=30)
        if landing.status_code != 200:
            raise RuntimeError(f"landing_http_{landing.status_code}")
    except (requests.RequestException, RuntimeError) as exc:
        return (
            _source_failure(
                "NASDAQ_TRADER_HALT_HISTORY",
                "UNVERIFIED_SOURCE_RESPONSE",
                f"{type(exc).__name__}:{_short_failure_text(exc, 180)}",
                retrieved_at=retrieved_at,
            ),
            previous_coverage.get("events", {}).get("suspensions", []),
        )
    try:
        current_feed = session.get(
            NASDAQ_HALT_RSS_ENDPOINT,
            headers=headers,
            timeout=30,
        )
        if current_feed.status_code != 200:
            raise RuntimeError(f"current_feed_http_{current_feed.status_code}")
    except (requests.RequestException, RuntimeError) as exc:
        return (
            _source_failure(
                "NASDAQ_TRADER_HALT_HISTORY_AND_CURRENT_FEED",
                "UNVERIFIED_SOURCE_RESPONSE",
                f"{type(exc).__name__}:{_short_failure_text(exc, 180)}",
                retrieved_at=retrieved_at,
            ),
            previous_coverage.get("events", {}).get("suspensions", []),
        )
    current_feed_valid, current_feed_pub_date, current_feed_rows = (
        _parse_nasdaq_current_halt_rss(current_feed.content)
    )
    if not current_feed_valid:
        return (
            _source_failure(
                "NASDAQ_TRADER_HALT_HISTORY_AND_CURRENT_FEED",
                "UNVERIFIED_SOURCE_RESPONSE",
                "current_feed_contract_invalid",
                retrieved_at=retrieved_at,
                responseSha256=hashlib.sha256(current_feed.content).hexdigest(),
            ),
            previous_coverage.get("events", {}).get("suspensions", []),
        )
    (
        publication_valid,
        publication_reason,
        current_feed_source_as_of,
        current_feed_age_hours,
        current_feed_freshness_mode,
    ) = _rss_publication_contract(current_feed_pub_date, retrieved_at)
    if not publication_valid:
        return (
            _source_failure(
                "NASDAQ_TRADER_HALT_HISTORY_AND_CURRENT_FEED",
                (
                    "UNVERIFIED_STALE_SOURCE"
                    if publication_reason == "current_feed_published_at_stale"
                    else "UNVERIFIED_SOURCE_RESPONSE"
                ),
                publication_reason or "current_feed_published_at_invalid",
                retrieved_at=retrieved_at,
                currentFeedPublishedAt=current_feed_source_as_of,
                currentFeedAgeHours=current_feed_age_hours,
                currentFeedFreshnessMode=current_feed_freshness_mode,
                responseSha256=hashlib.sha256(current_feed.content).hexdigest(),
            ),
            previous_coverage.get("events", {}).get("suspensions", []),
        )
    expected_headers = {"Halt Date", "Issue Symbol", "Reason Code", "Resumption Date"}
    response_hashes = [hashlib.sha256(current_feed.content).hexdigest()]
    historical_rows = []
    raw_halt_rows = 0
    for request_id, reason_code in enumerate(
        sorted(NASDAQ_HISTORICAL_SUSPENSION_CODES),
        1,
    ):
        args = [
            "",
            reason_code,
            "",
            history_coverage_start.strftime("%m/%d/%Y"),
            coverage_end.strftime("%m/%d/%Y"),
            "",
            "",
        ]
        request_payload = {
            "id": request_id,
            "method": "BL_TradeHalt.SearchTradeHaltsNEW",
            "params": json.dumps(args, separators=(",", ":")),
            "version": "1.1",
        }
        try:
            response = session.post(
                NASDAQ_HALT_RPC_ENDPOINT,
                data=json.dumps(request_payload, separators=(",", ":")),
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Referer": NASDAQ_HALT_SEARCH_PAGE,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"{reason_code}:{type(exc).__name__}:{_short_failure_text(exc, 160)}",
                    retrieved_at=retrieved_at,
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        if response.status_code != 200:
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"{reason_code}:http_{response.status_code}",
                    retrieved_at=retrieved_at,
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        payload_contract_valid = bool(
            isinstance(payload, dict)
            and str(payload.get("id") or "") == str(request_id)
            and str(payload.get("version") or "") == "1.1"
            and isinstance(payload.get("result"), str)
        )
        html_text = payload.get("result") if payload_contract_valid else None
        response_hashes.append(hashlib.sha256(response.content).hexdigest())
        if not payload_contract_valid:
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"{reason_code}:rpc_response_contract_invalid",
                    retrieved_at=retrieved_at,
                    responseSha256=response_hashes[-1],
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        if str(html_text or "").strip() == "No Data Found":
            continue
        (
            table_rows,
            raw_table_row_count,
            table_shape_valid,
        ) = _parse_html_table_rows_with_contract(html_text or "")
        headers_found = set(re.findall(r"<th[^>]*>([^<]+)</th>", html_text or ""))
        if not table_shape_valid or raw_table_row_count != len(table_rows):
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    (
                        f"{reason_code}:halt_table_row_shape_invalid:"
                        f"raw={raw_table_row_count}:parsed={len(table_rows)}"
                    ),
                    retrieved_at=retrieved_at,
                    responseSha256=response_hashes[-1],
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        if (
            not table_rows
            or not expected_headers.issubset(headers_found)
            or any(
                str(row.get("Reason Code") or "").strip().upper() != reason_code
                for row in table_rows
            )
        ):
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"{reason_code}:halt_table_contract_invalid",
                    retrieved_at=retrieved_at,
                    responseSha256=response_hashes[-1],
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        raw_halt_rows += raw_table_row_count
        parsed_halt_rows = _parse_nasdaq_halt_rows(html_text or "")
        if len(parsed_halt_rows) != len(table_rows):
            return (
                _source_failure(
                    "NASDAQ_TRADER_HALT_HISTORY",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    (
                        f"{reason_code}:halt_table_parse_loss:"
                        f"raw={len(table_rows)}:parsed={len(parsed_halt_rows)}"
                    ),
                    retrieved_at=retrieved_at,
                    responseSha256=response_hashes[-1],
                ),
                previous_coverage.get("events", {}).get("suspensions", []),
            )
        historical_rows.extend(parsed_halt_rows)
    previous_historical_rows = []
    for row in previous_coverage.get("events", {}).get("suspensions", []):
        if not isinstance(row, dict):
            continue
        effective = _parse_date(row.get("eventEffectiveAt"))
        if (
            effective
            and coverage_start <= effective < history_coverage_start
        ):
            previous_historical_rows.append(
                {
                    **row,
                    "currentFeedObserved": False,
                    "preservationStatus": (
                        "PRESERVED_POSITIVE_EVENT_OUTSIDE_CURRENT_QUERY_WINDOW"
                    ),
                }
            )
    merged_rows = _merge_external_events(
        previous_historical_rows,
        _events_within_coverage(
            historical_rows,
            history_coverage_start,
            coverage_end,
            retain_current_active=True,
            source_as_of=current_feed_source_as_of,
        ),
        ("symbol", "eventEffectiveAt", "reasonCode"),
    )
    merged_rows = _merge_external_events(
        merged_rows,
        _events_within_coverage(
            current_feed_rows,
            history_coverage_start,
            coverage_end,
            retain_current_active=True,
            source_as_of=current_feed_source_as_of,
        ),
        ("symbol", "eventEffectiveAt", "reasonCode"),
    )
    response_sha = _canonical_sha256(
        {
            "responseHashes": response_hashes,
            "reasonCodes": sorted(NASDAQ_HISTORICAL_SUSPENSION_CODES),
            "events": merged_rows,
        }
    )
    return (
        {
            "status": "SUCCESS",
            "source": "NASDAQ_TRADER_HALT_HISTORY_AND_CURRENT_FEED",
            "sourceAsOf": current_feed_source_as_of,
            "sourceAsOfBasis": "NASDAQ_RSS_PUBLICATION_TIME",
            "retrievedAt": retrieved_at,
            "coverageStart": history_coverage_start.isoformat(),
            "coverageEnd": coverage_end.isoformat(),
            "partialResponse": False,
            "responseSha256": response_sha,
            "queryScope": (
                "CURRENT_ALL_CODES_PLUS_LAST_YEAR_REGULATORY_EXTENDED_"
                "AND_CORPORATE_ACTION_HALT_CODES"
            ),
            "requestCount": len(response_hashes) + 1,
            "currentFeedRows": len(current_feed_rows),
            "currentFeedPublishedAt": current_feed_source_as_of,
            "currentFeedAgeHours": current_feed_age_hours,
            "currentFeedFreshnessMode": current_feed_freshness_mode,
            "currentFeedMaxAgeHours": HARVESTER_NASDAQ_HALT_RSS_MAX_AGE_HOURS,
            "currentFeedRthMaxAgeMinutes": (
                HARVESTER_NASDAQ_HALT_RSS_RTH_MAX_AGE_MINUTES
            ),
            "rawHaltRows": raw_halt_rows,
            "eventCount": len(merged_rows),
            "preservedHistoricalEventRows": len(previous_historical_rows),
            "includedHistoricalReasonCodes": sorted(
                NASDAQ_HISTORICAL_SUSPENSION_CODES
            ),
            "sourceCoverageLimit": "NASDAQ_HALT_SEARCH_LAST_YEAR",
            "requestedCoverageStart": coverage_start.isoformat(),
        },
        merged_rows,
    )


def fetch_external_corporate_action_coverage(
    active_symbols,
    *,
    previous_coverage=None,
    now_utc=None,
    session=None,
) -> dict:
    generated_at = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    coverage_end = _parse_iso_datetime(generated_at).date()
    coverage_start = _coverage_start_date(coverage_end)
    previous_coverage = previous_coverage if isinstance(previous_coverage, dict) else {}
    active = sorted({_normalize_event_symbol(symbol) for symbol in active_symbols if symbol})
    scope_hash = _canonical_sha256(active)
    if not HARVESTER_EXTERNAL_CORPORATE_ACTION_ENABLED:
        return {
            "schemaVersion": EXTERNAL_CORPORATE_ACTION_SCHEMA_VERSION,
            "generatedAt": generated_at,
            "overall": "disabled",
            "sources": {},
            "events": {"symbolChanges": [], "delistings": [], "suspensions": []},
            "requestScopeSymbolsSha256": scope_hash,
        }
    client = session or requests.Session()
    symbol_source = _source_failure(
        "FMP_OR_FINNHUB_SYMBOL_CHANGE",
        "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
        "entitlement_and_verified_response_fixture_required",
        retrieved_at=generated_at,
    )
    symbol_events = _events_within_coverage(
        previous_coverage.get("events", {}).get("symbolChanges", []),
        coverage_start,
        coverage_end,
    )
    delisting_source, delisting_events = _fetch_fmp_delisting_coverage(
        client,
        api_key=FMP_API_KEY,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        retrieved_at=generated_at,
        previous_coverage=previous_coverage,
    )
    suspension_source, suspension_events = _fetch_nasdaq_suspension_coverage(
        client,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        retrieved_at=generated_at,
        previous_coverage=previous_coverage,
    )
    delisting_events = _events_within_coverage(
        delisting_events,
        coverage_start,
        coverage_end,
    )
    suspension_events = _events_within_coverage(
        suspension_events,
        coverage_start,
        coverage_end,
        retain_current_active=True,
        source_as_of=generated_at,
    )
    for source in (symbol_source, delisting_source, suspension_source):
        if isinstance(source, dict):
            source["requestScopeSymbolsSha256"] = scope_hash
    sources = {
        "symbolChange": symbol_source,
        "delisting": delisting_source,
        "suspension": suspension_source,
    }
    source_statuses = {key: value.get("status") for key, value in sources.items()}
    overall = (
        "pass"
        if all(status == "SUCCESS" for status in source_statuses.values())
        else "blocked_external_source_contract"
    )
    return {
        "schemaVersion": EXTERNAL_CORPORATE_ACTION_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "overall": overall,
        "coverageStart": coverage_start.isoformat(),
        "coverageEnd": coverage_end.isoformat(),
        "requestScopeSymbolsSha256": scope_hash,
        "activeSymbolCount": len(active),
        "sources": sources,
        "events": {
            "symbolChanges": symbol_events,
            "delistings": delisting_events,
            "suspensions": suspension_events,
        },
        "summary": {
            "sourceStatuses": source_statuses,
            "symbolChangeEventRows": len(symbol_events),
            "delistingEventRows": len(delisting_events),
            "suspensionEventRows": len(suspension_events),
        },
    }


def refresh_ticker_mapping_from_authoritative_sources(
    existing_map,
    today_str,
    *,
    previous_mapping_audit=None,
):
    existing_map = existing_map if isinstance(existing_map, dict) else {}
    if not HARVESTER_TICKER_MAPPING_REFRESH_ENABLED:
        return existing_map, {
            "schemaVersion": 1,
            "generatedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "status": "disabled",
            "reason": "HARVESTER_TICKER_MAPPING_REFRESH_ENABLED=false",
        }

    listing_rows, source_summaries = fetch_authoritative_listing_rows()
    ok_sources = [s for s in source_summaries if s.get("status") == "ok"]
    if not listing_rows:
        audit = {
            "schemaVersion": 1,
            "generatedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "status": "source_failed_fail_open" if HARVESTER_TICKER_MAPPING_REFRESH_FAIL_OPEN else "source_failed",
            "sourceSummaries": source_summaries,
            "existingSymbols": len([k for k, v in existing_map.items() if isinstance(k, str) and isinstance(v, dict)]),
            "refreshedSymbols": 0,
            "addedSymbols": [],
            "removedSymbols": [],
            "reason": "No authoritative listing rows returned.",
        }
        if HARVESTER_TICKER_MAPPING_REFRESH_FAIL_OPEN:
            return existing_map, audit
        raise RuntimeError("Ticker mapping refresh failed: no authoritative listing rows")

    source_row_count = len(listing_rows)
    if not HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON:
        listing_rows = {
            symbol: row
            for symbol, row in listing_rows.items()
            if row.get("analysisEligible")
        }

    existing_symbols = {
        str(k).strip().upper()
        for k, v in existing_map.items()
        if isinstance(k, str) and k and isinstance(v, dict) and v.get("group")
    }
    refreshed_symbols = set(listing_rows.keys())
    added_symbols = sorted(refreshed_symbols - existing_symbols)
    removed_symbols = sorted(existing_symbols - refreshed_symbols)
    common_count = sum(1 for row in listing_rows.values() if row.get("analysisEligible"))
    non_common_count = len(listing_rows) - common_count

    refreshed_map = {}
    now_utc = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for symbol in sorted(listing_rows):
        row = dict(listing_rows[symbol])
        previous = existing_map.get(symbol) if isinstance(existing_map.get(symbol), dict) else {}
        row["firstMappedAt"] = previous.get("firstMappedAt") or now_utc
        row["lastMappedAt"] = now_utc
        # Preserve only explicit source-backed event evidence; refresh must not
        # silently erase a previously captured corporate-action lineage record.
        for evidence_key in ("symbolChangeEvidence", "delistingEvidence", "suspensionEvidence"):
            if isinstance(previous.get(evidence_key), dict):
                row[evidence_key] = dict(previous[evidence_key])
        refreshed_map[symbol] = row

    previous_external = _previous_external_coverage(previous_mapping_audit)
    try:
        external_coverage = fetch_external_corporate_action_coverage(
            refreshed_symbols,
            previous_coverage=previous_external,
            now_utc=now_utc,
        )
    except Exception as exc:
        external_coverage = {
            "schemaVersion": EXTERNAL_CORPORATE_ACTION_SCHEMA_VERSION,
            "generatedAt": now_utc,
            "overall": "unverified_source_response",
            "sources": {
                "symbolChange": _source_failure(
                    "FMP_OR_FINNHUB_SYMBOL_CHANGE",
                    "UNVERIFIED_SOURCE_RESPONSE",
                    f"{type(exc).__name__}:{_short_failure_text(exc, 180)}",
                    retrieved_at=now_utc,
                ),
                "delisting": {},
                "suspension": {},
            },
            "events": previous_external.get("events")
            or {"symbolChanges": [], "delistings": [], "suspensions": []},
        }
    refreshed_map, external_application_summary = apply_external_corporate_action_coverage(
        refreshed_map,
        external_coverage,
    )
    external_sources = external_coverage.get("sources") or {}
    refreshed_map["_meta"] = {
        "schemaVersion": 2,
        "generatedAt": now_utc,
        "generatedLocalTime": today_str,
        "source": "nasdaqtrader_symbol_directory",
        "sourceUrls": [spec.get("url") for spec in LISTING_SOURCE_SPECS],
        "activeListingCount": len(listing_rows),
        "sourceActiveListingCount": source_row_count,
        "commonStockEligibleCount": common_count,
        "nonCommonMonitoringCount": non_common_count,
        "addedCount": len(added_symbols),
        "removedCount": len(removed_symbols),
        "refreshPolicy": "authoritative_active_listing_replace",
        "includeNonCommon": HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON,
        "externalCorporateActionCoverage": {
            "schemaVersion": external_coverage.get("schemaVersion"),
            "generatedAt": external_coverage.get("generatedAt"),
            "overall": external_coverage.get("overall"),
            "coverageStart": external_coverage.get("coverageStart"),
            "coverageEnd": external_coverage.get("coverageEnd"),
            "requestScopeSymbolsSha256": external_coverage.get("requestScopeSymbolsSha256"),
            "sourceStatuses": {
                key: (value or {}).get("status")
                for key, value in external_sources.items()
            },
            "applicationSummary": external_application_summary,
        },
    }

    audit = {
        "schemaVersion": 1,
        "generatedAt": now_utc,
        "status": "refreshed",
        "sourceSummaries": source_summaries,
        "okSourceCount": len(ok_sources),
        "existingSymbols": len(existing_symbols),
        "sourceActiveListingCount": source_row_count,
        "refreshedSymbols": len(refreshed_symbols),
        "commonStockEligibleCount": common_count,
        "nonCommonMonitoringCount": non_common_count,
        "excludedFromMappingNonCommonCount": source_row_count - len(listing_rows),
        "includeNonCommon": HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON,
        "addedCount": len(added_symbols),
        "removedCount": len(removed_symbols),
        "addedSymbols": added_symbols[:HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT],
        "removedSymbols": removed_symbols[:HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT],
        "removedPolicy": "removed_from_Ticker_ID_Mapping_Final_when_absent_from_authoritative_active_listing_sources",
        "newListingPolicy": "added_to_Ticker_ID_Mapping_Final_when_present_in_authoritative_active_listing_sources",
        "externalCorporateActionCoverage": external_coverage,
        "externalCorporateActionApplication": external_application_summary,
    }
    return refreshed_map, audit

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

def _history_rows_from_entry(entry):
    if not isinstance(entry, dict):
        return []
    if isinstance(entry.get("financials"), list):
        return entry.get("financials") or []
    rows = []
    for k, v in entry.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            row = dict(v)
            row.setdefault("date", k)
            rows.append(row)
    return rows

def _extract_latest_financial_value(entry, candidate_keys):
    rows = _sort_financial_rows(_history_rows_from_entry(entry))
    if not rows:
        return None, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized_index = {_norm_label(k): k for k in row.keys()}
        for key in candidate_keys:
            raw = row.get(key)
            if raw in (None, ''):
                hit = normalized_index.get(_norm_label(key))
                raw = row.get(hit) if hit else None
            num = _to_finite_float(raw)
            if num is not None:
                return num, row.get("date")
    return None, None


def _parse_kst_datetime(text):
    if not text:
        return None
    if isinstance(text, datetime.datetime):
        return text
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(text), fmt)
        except Exception:
            continue
    return None


def _derive_history_tier(periods):
    if periods <= 0:
        return "ONBOARDING"
    if periods < SYMBOL_STATE_HISTORY_FULL_MIN_PERIODS:
        return "PROVISIONAL"
    return "FULL"


def _derive_symbol_lifecycle_state(prev_state, analysis_eligible, history_tier, missing_history_streak, missing_quote_streak):
    if not analysis_eligible:
        return "EXCLUDED", "instrument_type_ineligible"

    if missing_history_streak >= SYMBOL_STATE_STALE_HISTORY_STREAK:
        return "STALE", "history_missing_streak"
    if missing_quote_streak >= SYMBOL_STATE_STALE_QUOTE_STREAK:
        return "STALE", "quote_missing_streak"

    base_state = "ACTIVE"
    base_reason = "ready"
    if history_tier == "ONBOARDING":
        base_state = "ONBOARDING"
        base_reason = "history_missing"
    elif history_tier == "PROVISIONAL":
        base_state = "PROVISIONAL"
        base_reason = "history_partial"

    if prev_state in {"ONBOARDING", "PROVISIONAL", "STALE", "EXCLUDED", "RETIRED"} and base_state == "ACTIVE":
        return "RECOVERED", "history_recovered"
    if prev_state == "RECOVERED" and base_state == "ACTIVE":
        return "ACTIVE", "recovery_warmup_passed"

    return base_state, base_reason


def _update_symbol_state_entry(state_map, ticker, payload, touched_set, now_text):
    prev = state_map.get(ticker, {}) if isinstance(state_map.get(ticker), dict) else {}
    prev_state = str(prev.get("state") or "UNKNOWN").upper()
    missing_history_streak_prev = int(prev.get("missingHistoryStreak") or 0)
    missing_quote_streak_prev = int(prev.get("missingQuoteStreak") or 0)

    history_periods = max(0, int(payload.get("historyPeriods") or 0))
    history_tier = str(payload.get("historyTier") or "ONBOARDING").upper()
    analysis_eligible = bool(payload.get("analysisEligible"))
    quote_available = bool(payload.get("hasQuotePayload"))
    missing_history_now = history_periods <= 0
    missing_quote_now = not quote_available

    missing_history_streak = missing_history_streak_prev + 1 if missing_history_now else 0
    missing_quote_streak = missing_quote_streak_prev + 1 if missing_quote_now else 0

    lifecycle_state, reason = _derive_symbol_lifecycle_state(
        prev_state, analysis_eligible, history_tier, missing_history_streak, missing_quote_streak
    )

    recovered_at = prev.get("recoveredAt")
    if lifecycle_state == "RECOVERED":
        recovered_at = now_text

    first_seen_at = prev.get("firstSeenAt") or now_text
    state_map[ticker] = {
        "state": lifecycle_state,
        "reason": reason,
        "instrumentType": payload.get("instrumentType") or "unknown",
        "analysisEligible": analysis_eligible,
        "historyTier": history_tier,
        "historyPeriods": history_periods,
        "missingHistoryStreak": missing_history_streak,
        "missingQuoteStreak": missing_quote_streak,
        "lastSeenAt": now_text,
        "firstSeenAt": first_seen_at,
        "recoveredAt": recovered_at,
    }
    touched_set.add(ticker)
    return state_map[ticker]


def _apply_symbol_retire_policy(state_map, touched_set, now_text):
    now_dt = _parse_kst_datetime(now_text)
    if now_dt is None:
        return
    retire_cutoff = now_dt - datetime.timedelta(days=SYMBOL_STATE_RETIRE_DAYS)
    for ticker, entry in state_map.items():
        if not isinstance(entry, dict):
            continue
        if ticker in touched_set:
            continue
        last_seen_dt = _parse_kst_datetime(entry.get("lastSeenAt"))
        if last_seen_dt is None:
            continue
        if last_seen_dt <= retire_cutoff:
            entry["state"] = "RETIRED"
            entry["reason"] = f"retire_timeout_{SYMBOL_STATE_RETIRE_DAYS}d"


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def should_skip_symbol_for_collection(state_entry, authoritative_mapping_refreshed=False):
    if not isinstance(state_entry, dict):
        return False, None, None
    state = str(state_entry.get("state") or "").strip().upper()
    reason = str(state_entry.get("reason") or "unknown")
    if state == "RETIRED" and HARVESTER_SKIP_RETIRED_SYMBOLS and not authoritative_mapping_refreshed:
        return True, "SYMBOL_SKIPPED_RETIRED", reason
    if state == "EXCLUDED" and HARVESTER_SKIP_EXCLUDED_SYMBOLS and not authoritative_mapping_refreshed:
        return True, "SYMBOL_SKIPPED_EXCLUDED", reason
    return False, None, None


def _mapping_generated_at(full_map):
    if not isinstance(full_map, dict):
        return None
    for key in ("generatedAt", "generated_at", "updatedAt", "updated_at"):
        if full_map.get(key):
            return full_map.get(key)
    meta = full_map.get("_meta") or full_map.get("meta") or full_map.get("__meta__")
    if isinstance(meta, dict):
        for key in ("generatedAt", "generated_at", "updatedAt", "updated_at"):
            if meta.get(key):
                return meta.get(key)
    return None


def _state_counts_for_symbols(symbol_state, symbols):
    counts = Counter()
    for ticker in symbols:
        entry = symbol_state.get(ticker) if isinstance(symbol_state, dict) else None
        state = "UNSEEN"
        if isinstance(entry, dict):
            state = str(entry.get("state") or "UNKNOWN").strip().upper() or "UNKNOWN"
        counts[state] += 1
    return dict(sorted(counts.items()))


def _symbol_state_candidate(ticker, entry):
    entry = entry if isinstance(entry, dict) else {}
    analysis_eligible = entry.get("analysisEligible")
    return {
        "symbol": ticker,
        "state": str(entry.get("state") or "UNKNOWN").strip().upper() or "UNKNOWN",
        "reason": str(entry.get("reason") or "unknown"),
        "instrumentType": entry.get("instrumentType") or "unknown",
        "analysisEligible": bool(analysis_eligible) if analysis_eligible is not None else None,
        "historyTier": entry.get("historyTier") or "unknown",
        "historyPeriods": _safe_int(entry.get("historyPeriods")),
        "missingHistoryStreak": _safe_int(entry.get("missingHistoryStreak")),
        "missingQuoteStreak": _safe_int(entry.get("missingQuoteStreak")),
        "firstSeenAt": entry.get("firstSeenAt"),
        "lastSeenAt": entry.get("lastSeenAt"),
        "lastSkippedAt": entry.get("lastSkippedAt"),
    }


def _top_candidates(candidates, limit=None):
    if limit is None:
        limit = HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT
    return sorted(
        candidates,
        key=lambda x: (
            -_safe_int(x.get("missingQuoteStreak")),
            -_safe_int(x.get("missingHistoryStreak")),
            str(x.get("symbol") or ""),
        ),
    )[:limit]


def build_mapping_freshness_audit(full_map, filtered_tickers, symbol_state, today_str, batch_label, batch_mode):
    all_symbols = sorted(
        str(ticker).strip().upper()
        for ticker, info in (full_map or {}).items()
        if isinstance(ticker, str) and ticker and isinstance(info, dict) and info.get("group")
    )
    batch_symbols = sorted(str(ticker).strip().upper() for ticker in (filtered_tickers or {}).keys())
    generated_at = _mapping_generated_at(full_map)

    retired_candidates = []
    excluded_candidates = []
    stale_candidates = []
    quote_missing_persistent = []
    history_missing_persistent = []
    for ticker in all_symbols:
        entry = symbol_state.get(ticker) if isinstance(symbol_state, dict) else {}
        candidate = _symbol_state_candidate(ticker, entry)
        state = candidate["state"]
        if state == "RETIRED":
            retired_candidates.append(candidate)
        if state == "EXCLUDED" or candidate.get("analysisEligible") is False and state != "UNSEEN":
            excluded_candidates.append(candidate)
        if state == "STALE":
            stale_candidates.append(candidate)
        if candidate["missingQuoteStreak"] >= SYMBOL_STATE_STALE_QUOTE_STREAK:
            quote_missing_persistent.append(candidate)
        if candidate["missingHistoryStreak"] >= SYMBOL_STATE_STALE_HISTORY_STREAK:
            history_missing_persistent.append(candidate)

    skip_symbols = sorted({item.get("symbol") for item in RUN_SYMBOL_SKIP_DETAILS if item.get("symbol")})
    mapping_review_symbols = sorted(
        {
            *(item["symbol"] for item in retired_candidates),
            *(item["symbol"] for item in excluded_candidates),
            *(item["symbol"] for item in stale_candidates),
            *(item["symbol"] for item in quote_missing_persistent),
            *(item["symbol"] for item in history_missing_persistent),
        }
    )
    action_counts = {
        "skipCollection": len(skip_symbols),
        "mappingReview": len(mapping_review_symbols),
        "retired": len(retired_candidates),
        "excluded": len(excluded_candidates),
        "stale": len(stale_candidates),
        "persistentQuoteMissing": len(quote_missing_persistent),
        "persistentHistoryMissing": len(history_missing_persistent),
    }

    mapping_metadata_status = "present" if generated_at else "missing_generated_at"
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "batch": {
            "label": batch_label,
            "mode": batch_mode,
            "runLocalTime": today_str,
        },
        "mapping": {
            "sourceFile": "Ticker_ID_Mapping_Final.json",
            "mappingGeneratedAt": generated_at,
            "mappingMetadataStatus": mapping_metadata_status,
            "totalSymbols": len(all_symbols),
            "batchSymbols": len(batch_symbols),
            "stateCountsAll": _state_counts_for_symbols(symbol_state, all_symbols),
            "stateCountsBatch": _state_counts_for_symbols(symbol_state, batch_symbols),
            "newListingCoverageStatus": (
                "unverifiable_without_mapping_generated_at"
                if not generated_at
                else "mapping_timestamp_available"
            ),
        },
        "failureSummary": build_failure_snapshot(),
        "skipSummary": build_skip_snapshot(),
        "actionCounts": action_counts,
        "retiredCandidates": _top_candidates(retired_candidates),
        "instrumentIneligibleCandidates": _top_candidates(excluded_candidates),
        "staleMappingCandidates": _top_candidates(stale_candidates),
        "quoteMissingPersistentCandidates": _top_candidates(quote_missing_persistent),
        "historyMissingPersistentCandidates": _top_candidates(history_missing_persistent),
        "recommendedActions": {
            "skipCollectionSymbols": skip_symbols[:HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT],
            "mappingReviewSymbols": mapping_review_symbols[:HARVESTER_MAPPING_AUDIT_SAMPLE_LIMIT],
            "doNotRewriteMappingFromQuoteFailures": True,
            "requiresAuthoritativeListingRefreshForNewListings": False,
            "reason": (
                "Ticker_ID_Mapping_Final is refreshed from authoritative listing directories; "
                "quote failures only affect lifecycle skip/review classification."
            ),
        },
    }


def _mapping_audit_markdown(audit):
    action = audit.get("actionCounts", {}) if isinstance(audit, dict) else {}
    mapping = audit.get("mapping", {}) if isinstance(audit, dict) else {}
    lines = [
        "# Harvester Mapping Freshness Audit",
        "",
        f"- Generated: `{audit.get('generatedAt')}`",
        f"- Source: `{mapping.get('sourceFile')}`",
        f"- Mapping metadata: `{mapping.get('mappingMetadataStatus')}`",
        f"- Symbols: total `{mapping.get('totalSymbols')}`, batch `{mapping.get('batchSymbols')}`",
        f"- Lifecycle skips: `{action.get('skipCollection', 0)}`",
        f"- Mapping review symbols: `{action.get('mappingReview', 0)}`",
        f"- Retired: `{action.get('retired', 0)}` | Excluded: `{action.get('excluded', 0)}` | Stale: `{action.get('stale', 0)}`",
        "",
        "## Recommended Policy",
        "",
        "- Do not repeatedly collect symbols already classified as `RETIRED` or `EXCLUDED`.",
        "- Keep stale/common symbols visible for review until the retire policy classifies them.",
        "- Do not rewrite `Ticker_ID_Mapping_Final.json` from quote failures alone.",
        "- Refresh new listings from authoritative listing directories before collection.",
        "",
    ]
    review = audit.get("recommendedActions", {}).get("mappingReviewSymbols", [])
    if review:
        lines.extend(["## Review Sample", "", ", ".join(f"`{s}`" for s in review[:40]), ""])
    return "\n".join(lines).rstrip() + "\n"

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


def _normalize_ohlcv_record(record):
    if not isinstance(record, dict):
        return None
    date_raw = str(record.get("date") or "").strip()
    if not date_raw:
        return None
    open_price = _to_finite_float(record.get("open"))
    high_price = _to_finite_float(record.get("high"))
    low_price = _to_finite_float(record.get("low"))
    close_price = _to_finite_float(record.get("close"))
    if (
        open_price is None
        or high_price is None
        or low_price is None
        or close_price is None
        or open_price <= 0
        or high_price <= 0
        or low_price <= 0
        or close_price <= 0
    ):
        return None
    if high_price < low_price:
        return None
    volume_raw = _to_finite_float(record.get("volume"))
    volume = int(max(0, round(volume_raw))) if volume_raw is not None else 0
    symbol = str(record.get("symbol") or "").strip()
    return {
        "symbol": symbol,
        "date": date_raw,
        "open": round(open_price, 2),
        "high": round(high_price, 2),
        "low": round(low_price, 2),
        "close": round(close_price, 2),
        "volume": volume,
    }


def sanitize_ohlcv_records(records):
    if not isinstance(records, list):
        return []
    cleaned = []
    removed = 0
    for row in records:
        normalized = _normalize_ohlcv_record(row)
        if normalized is None:
            removed += 1
            continue
        cleaned.append(normalized)
    return cleaned, removed


def _extract_ohlcv_payload(payload):
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("candles") or []
        lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else None
        return rows if isinstance(rows, list) else [], lineage
    return [], None


def _event_rows(frame, column, value_key):
    if column not in getattr(frame, "columns", []):
        return []
    rows = []
    for index, value in frame[column].items():
        number = _to_finite_float(value)
        if number is None or number == 0:
            continue
        rows.append({"eventEffectiveAt": index.strftime("%Y-%m-%d"), value_key: number})
    return rows


def _external_evidence_contract_valid(evidence, expected_symbol=None):
    if not isinstance(evidence, dict):
        return False
    requested_symbol = str(evidence.get("requestedSymbol") or "").strip().upper()
    matched_symbol = str(evidence.get("matchedSymbol") or "").strip().upper()
    match_status = str(evidence.get("symbolMatchStatus") or "").strip().upper()
    match_method = str(evidence.get("symbolMatchMethod") or "").strip().upper()
    expected = str(expected_symbol or requested_symbol).strip().upper()
    response_sha = str(evidence.get("responseSha256") or "").strip().lower()
    request_scope_sha = str(
        evidence.get("requestScopeSymbolsSha256") or ""
    ).strip().lower()
    source_as_of = _parse_iso_datetime(evidence.get("sourceAsOf"))
    retrieved_at = _parse_iso_datetime(evidence.get("retrievedAt"))
    coverage_start = _parse_iso_datetime(evidence.get("coverageStart"))
    coverage_end = _parse_iso_datetime(evidence.get("coverageEnd"), end_of_day=True)
    return bool(
        str(evidence.get("requestStatus") or "").strip().upper() == "SUCCESS"
        and str(evidence.get("source") or "").strip()
        and expected
        and requested_symbol == expected
        and (
            (
                match_status == "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE"
                and not matched_symbol
            )
            or (
                match_status
                in {
                    "EXACT_EVENT_MATCH",
                    "EXACT_HISTORICAL_EVENT_MATCH_CURRENTLY_RESUMED",
                    "EXACT_HISTORICAL_EVENT_MATCH_NOT_IN_CURRENT_FEED",
                }
                and matched_symbol == expected
            )
        )
        and match_method == "DETERMINISTIC_EXACT_NORMALIZED_SYMBOL_LOOKUP"
        and evidence.get("sourceScopeComplete") is True
        and str(evidence.get("queryScope") or "").strip()
        and re.fullmatch(r"[0-9a-f]{64}", request_scope_sha)
        and source_as_of
        and retrieved_at
        and coverage_start
        and coverage_end
        and coverage_start <= coverage_end
        and source_as_of <= retrieved_at
        and evidence.get("partialResponse") is False
        and re.fullmatch(r"[0-9a-f]{64}", response_sha)
    )


def _verified_evidence_status(evidence, allowed_statuses, fallback, expected_symbol=None):
    status = str((evidence or {}).get("status") or "").strip().upper()
    source = str((evidence or {}).get("source") or "").strip()
    source_as_of = str((evidence or {}).get("sourceAsOf") or "").strip()
    return (
        status
        if (
            status in allowed_statuses
            and source
            and _parse_iso_datetime(source_as_of)
            and _external_evidence_contract_valid(evidence, expected_symbol)
        )
        else fallback
    )


def _parse_iso_datetime(value, *, end_of_day=False):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed = datetime.datetime.fromisoformat(text)
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59)
        else:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _external_evidence_time_valid(
    evidence,
    source_as_of,
    retrieved_at,
    *,
    expected_symbol=None,
    lookback_start=None,
):
    if not isinstance(evidence, dict):
        return False
    evidence_as_of = _parse_iso_datetime(evidence.get("sourceAsOf"))
    evidence_as_of_end = _parse_iso_datetime(
        evidence.get("sourceAsOf"),
        end_of_day=True,
    )
    evidence_retrieved = _parse_iso_datetime(evidence.get("retrievedAt"))
    coverage_start = _parse_iso_datetime(evidence.get("coverageStart"))
    coverage_end = _parse_iso_datetime(evidence.get("coverageEnd"), end_of_day=True)
    history_as_of = _parse_iso_datetime(source_as_of)
    history_start = _parse_iso_datetime(lookback_start)
    retrieval = _parse_iso_datetime(retrieved_at)
    if not all(
        (
            evidence_as_of,
            evidence_as_of_end,
            evidence_retrieved,
            coverage_start,
            coverage_end,
            history_as_of,
            history_start,
            retrieval,
        )
    ):
        return False
    event_effective = _parse_iso_datetime(evidence.get("eventEffectiveAt") or evidence.get("effectiveAt"))
    return bool(
        _external_evidence_contract_valid(evidence, expected_symbol)
        and coverage_start <= history_start <= history_as_of <= coverage_end
        and history_as_of <= evidence_as_of_end
        and evidence_as_of <= evidence_retrieved <= retrieval
        and (event_effective is None or event_effective <= evidence_as_of_end)
    )


def _normalized_external_evidence(evidence):
    if not isinstance(evidence, dict):
        return None
    allowed = (
        "status",
        "source",
        "sourceAsOf",
        "sourceAsOfBasis",
        "retrievedAt",
        "requestStatus",
        "requestedSymbol",
        "matchedSymbol",
        "symbolMatchStatus",
        "symbolMatchMethod",
        "sourceScopeComplete",
        "coverageStart",
        "coverageEnd",
        "partialResponse",
        "responseSha256",
        "queryScope",
        "requestScopeSymbolsSha256",
        "eventEffectiveAt",
        "resumedAt",
        "oldSymbol",
        "newSymbol",
        "reason",
        "preservedStatus",
        "refreshFailureAt",
    )
    normalized = {key: evidence.get(key) for key in allowed if evidence.get(key) not in (None, "")}
    if evidence.get("partialResponse") is False:
        normalized["partialResponse"] = False
    if evidence.get("sourceScopeComplete") is True:
        normalized["sourceScopeComplete"] = True
    if isinstance(evidence.get("events"), list):
        normalized["events"] = sorted(
            [dict(row) for row in evidence["events"] if isinstance(row, dict)],
            key=lambda row: (
                str(row.get("eventEffectiveAt") or ""),
                str(row.get("symbol") or ""),
                str(row.get("reasonCode") or ""),
            ),
        )
    if "eventEffectiveAt" not in normalized and evidence.get("effectiveAt") not in (None, ""):
        normalized["eventEffectiveAt"] = evidence.get("effectiveAt")
    return normalized or None


def _corporate_lineage_is_comparable(lineage):
    source_as_of = lineage.get("sourceAsOf")
    retrieved_at = lineage.get("retrievedAt")
    source_time = _parse_iso_datetime(source_as_of)
    market_data_retrieval_time = _parse_iso_datetime(retrieved_at)
    evaluation_time = _parse_iso_datetime(
        lineage.get("lineageEvaluatedAt") or retrieved_at
    )
    return bool(
        lineage.get("vendor")
        and source_time
        and market_data_retrieval_time
        and evaluation_time
        and source_time <= market_data_retrieval_time <= evaluation_time
        and lineage.get("adjustmentType")
        and lineage.get("sourceFreshnessStatus") == "FRESH"
        and lineage.get("historyCoverageStatus") == "VERIFIED_OBSERVED_HISTORY"
        and lineage.get("splitAdjustmentStatus") == "VERIFIED_YFINANCE_AUTO_ADJUSTED"
        and lineage.get("dividendAdjustmentStatus") == "VERIFIED_YFINANCE_AUTO_ADJUSTED"
        and lineage.get("corporateActionStatus")
        in {"VERIFIED_SPLIT_DIVIDEND_EVENTS_IN_WINDOW", "VERIFIED_NO_SPLIT_OR_DIVIDEND_EVENT_IN_WINDOW"}
        and lineage.get("symbolChangeStatus") in VERIFIED_SYMBOL_CHANGE_STATUSES
        and lineage.get("delistingStatus") == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
        and lineage.get("suspensionStatus") == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"
        and all(
            _external_evidence_time_valid(
                lineage.get(key),
                source_as_of,
                lineage.get("lineageEvaluatedAt") or retrieved_at,
                expected_symbol=lineage.get("symbol"),
                lookback_start=lineage.get("lookbackStart"),
            )
            for key in ("symbolChangeEvidence", "delistingEvidence", "suspensionEvidence")
        )
    )


def _frame_has_unseen_adjustment_event(frame, previous_lineage):
    previous_lineage = previous_lineage if isinstance(previous_lineage, dict) else {}
    event_specs = (
        ("Stock Splits", "ratio", "splitEvents"),
        ("Dividends", "amount", "dividendEvents"),
    )
    for column, value_key, lineage_key in event_specs:
        previous_events = previous_lineage.get(lineage_key)
        previous_by_date = {
            str(row.get("eventEffectiveAt")): _to_finite_float(row.get(value_key))
            for row in previous_events or []
            if isinstance(row, dict) and row.get("eventEffectiveAt")
        }
        for row in _event_rows(frame, column, value_key):
            prior_value = previous_by_date.get(str(row["eventEffectiveAt"]))
            if prior_value is None or not math.isclose(
                prior_value,
                float(row[value_key]),
                rel_tol=1e-9,
                abs_tol=1e-12,
            ):
                return True
    return False


def _normalize_ohlcv_frame(frame, record_symbol):
    rows = []
    skipped = 0
    for index, raw in frame.iterrows():
        open_price = _to_finite_float(raw.get("Open"))
        high_price = _to_finite_float(raw.get("High"))
        low_price = _to_finite_float(raw.get("Low"))
        close_price = _to_finite_float(raw.get("Close"))
        if (
            open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
            or min(open_price, high_price, low_price, close_price) <= 0
            or high_price < low_price
        ):
            skipped += 1
            continue
        volume_raw = _to_finite_float(raw.get("Volume"))
        rows.append(
            {
                "symbol": record_symbol,
                "date": index.strftime("%Y-%m-%d"),
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "volume": int(max(0, round(volume_raw))) if volume_raw is not None else 0,
            }
        )
    return rows, skipped


def _adjusted_price_basis_changed(existing_rows, new_rows):
    existing_by_date = {row.get("date"): row for row in existing_rows if row.get("date")}
    for row in new_rows:
        previous = existing_by_date.get(row.get("date"))
        if not previous:
            continue
        if any(previous.get(key) != row.get(key) for key in ("open", "high", "low", "close")):
            return True
    return False


def _incremental_window_overlaps(existing_rows, new_rows):
    existing_dates = {row.get("date") for row in existing_rows if row.get("date")}
    return any(row.get("date") in existing_dates for row in new_rows)


def build_corporate_action_lineage(
    frame,
    *,
    record_symbol: str,
    source_symbol: str,
    requested_period: str,
    retrieved_at: str,
    listing_evidence: dict | None = None,
    expected_market_date: str | None = None,
    previous_lineage: dict | None = None,
    observation_count: int | None = None,
    stored_rows: list[dict] | None = None,
) -> dict:
    listing_evidence = listing_evidence if isinstance(listing_evidence, dict) else {}
    previous_lineage = previous_lineage if isinstance(previous_lineage, dict) else {}
    stored_rows = stored_rows if isinstance(stored_rows, list) else []
    source_as_of = (
        str(stored_rows[-1].get("date"))[:10]
        if stored_rows and stored_rows[-1].get("date")
        else frame.index.max().strftime("%Y-%m-%d") if len(frame.index) else None
    )
    lookback_start = (
        str(stored_rows[0].get("date"))[:10]
        if stored_rows and stored_rows[0].get("date")
        else frame.index.min().strftime("%Y-%m-%d") if len(frame.index) else None
    )
    has_split_column = "Stock Splits" in getattr(frame, "columns", [])
    has_dividend_column = "Dividends" in getattr(frame, "columns", [])
    split_events = _event_rows(frame, "Stock Splits", "ratio")
    dividend_events = _event_rows(frame, "Dividends", "amount")

    previous_splits = previous_lineage.get("splitEvents") if isinstance(previous_lineage.get("splitEvents"), list) else []
    previous_dividends = previous_lineage.get("dividendEvents") if isinstance(previous_lineage.get("dividendEvents"), list) else []
    split_events = list({row["eventEffectiveAt"]: row for row in [*previous_splits, *split_events]}.values())
    dividend_events = list({row["eventEffectiveAt"]: row for row in [*previous_dividends, *dividend_events]}.values())
    split_events.sort(key=lambda row: row["eventEffectiveAt"])
    dividend_events.sort(key=lambda row: row["eventEffectiveAt"])

    split_status = "VERIFIED_YFINANCE_AUTO_ADJUSTED" if has_split_column else "UNVERIFIED_SPLIT_ACTION_COLUMN_MISSING"
    dividend_status = "VERIFIED_YFINANCE_AUTO_ADJUSTED" if has_dividend_column else "UNVERIFIED_DIVIDEND_ACTION_COLUMN_MISSING"
    if has_split_column and has_dividend_column:
        corporate_status = (
            "VERIFIED_SPLIT_DIVIDEND_EVENTS_IN_WINDOW"
            if split_events or dividend_events
            else "VERIFIED_NO_SPLIT_OR_DIVIDEND_EVENT_IN_WINDOW"
        )
    else:
        corporate_status = "UNVERIFIED_ACTION_COLUMNS_INCOMPLETE"

    symbol_change_status = _verified_evidence_status(
        listing_evidence.get("symbolChangeEvidence"),
        VERIFIED_SYMBOL_CHANGE_STATUSES,
        "UNVERIFIED_HISTORICAL_SYMBOL_CHANGE_SOURCE_MISSING",
        record_symbol,
    )
    delisting_status = _verified_evidence_status(
        listing_evidence.get("delistingEvidence"),
        VERIFIED_DELISTING_STATUSES,
        "UNVERIFIED_DELISTING_EVENT_SOURCE_MISSING",
        record_symbol,
    )
    suspension_status = _verified_evidence_status(
        listing_evidence.get("suspensionEvidence"),
        VERIFIED_SUSPENSION_STATUSES,
        "UNVERIFIED_SUSPENSION_EVENT_SOURCE_MISSING",
        record_symbol,
    )
    source_freshness_status = (
        "FRESH"
        if source_as_of and (not expected_market_date or source_as_of >= expected_market_date)
        else "STALE_OR_UNVERIFIED"
    )
    effective_observation_count = observation_count if observation_count is not None else len(frame.index)
    history_coverage_status = (
        "VERIFIED_OBSERVED_HISTORY"
        if effective_observation_count >= OHLCV_LINEAGE_MIN_BARS
        else "UNVERIFIED_PARTIAL_HISTORY"
    )

    prior_start = previous_lineage.get("lookbackStart")
    prior_end = previous_lineage.get("lookbackEnd")
    effective_source_as_of = source_as_of if stored_rows else max(filter(None, [prior_end, source_as_of]), default=None)
    effective_lookback_start = lookback_start if stored_rows else min(filter(None, [prior_start, lookback_start]), default=None)
    lineage = {
        "schemaVersion": "corporate-action-lineage-v1",
        "lineageStatus": "PRESENT",
        "symbol": str(record_symbol or "").strip().upper(),
        "sourceSymbol": str(source_symbol or "").strip().upper(),
        "vendor": "YFINANCE_YAHOO",
        "retrievedAt": retrieved_at,
        "lineageEvaluatedAt": retrieved_at,
        "sourceAsOf": effective_source_as_of,
        "marketTimezone": "America/New_York",
        "adjustmentType": "YFINANCE_AUTO_ADJUSTED_OHLC",
        "splitAdjustmentStatus": split_status,
        "dividendAdjustmentStatus": dividend_status,
        "corporateActionStatus": corporate_status,
        "symbolChangeStatus": symbol_change_status,
        "delistingStatus": delisting_status,
        "suspensionStatus": suspension_status,
        "symbolChangeEvidence": _normalized_external_evidence(listing_evidence.get("symbolChangeEvidence")),
        "delistingEvidence": _normalized_external_evidence(listing_evidence.get("delistingEvidence")),
        "suspensionEvidence": _normalized_external_evidence(listing_evidence.get("suspensionEvidence")),
        "sourceFreshnessStatus": source_freshness_status,
        "historyCoverageStatus": history_coverage_status,
        "survivorshipBiasStatus": "UNVERIFIED_INCOMPLETE_CORPORATE_ACTION_COVERAGE",
        "returnBasis": "DIVIDEND_AND_SPLIT_ADJUSTED_PRICE_RETURN",
        "lookbackStart": effective_lookback_start,
        "lookbackEnd": effective_source_as_of,
        "observationCount": int(effective_observation_count),
        "splitEvents": split_events,
        "dividendEvents": dividend_events,
        "eventEffectiveAt": max(
            [row["eventEffectiveAt"] for row in [*split_events, *dividend_events]],
            default=None,
        ),
        "listingEvidence": {
            "listingSource": listing_evidence.get("listingSource"),
            "sourceAsOf": listing_evidence.get("listingSourceAsOf") or listing_evidence.get("lastMappedAt"),
            "listingStatus": listing_evidence.get("listingStatus"),
        },
        "vendorRequestLineage": {
            "method": "Ticker.history",
            "period": requested_period,
            "interval": "1d",
            "actions": True,
            "autoAdjust": True,
            "sourceSymbol": str(source_symbol or "").strip().upper(),
        },
    }
    if _corporate_lineage_is_comparable(lineage):
        lineage["survivorshipBiasStatus"] = "VERIFIED_CORPORATE_ACTION_LINEAGE"
        lineage["lineageVerifiedForComparison"] = True
    else:
        lineage["lineageVerifiedForComparison"] = False
    return lineage


def refresh_corporate_action_lineage_evidence(
    existing_lineage: dict,
    listing_evidence: dict,
) -> dict:
    lineage = dict(existing_lineage)
    symbol = str(lineage.get("symbol") or "").strip().upper()
    evidence_specs = (
        (
            "symbolChangeEvidence",
            "symbolChangeStatus",
            VERIFIED_SYMBOL_CHANGE_STATUSES,
            "UNVERIFIED_HISTORICAL_SYMBOL_CHANGE_SOURCE_MISSING",
        ),
        (
            "delistingEvidence",
            "delistingStatus",
            VERIFIED_DELISTING_STATUSES,
            "UNVERIFIED_DELISTING_EVENT_SOURCE_MISSING",
        ),
        (
            "suspensionEvidence",
            "suspensionStatus",
            VERIFIED_SUSPENSION_STATUSES,
            "UNVERIFIED_SUSPENSION_EVENT_SOURCE_MISSING",
        ),
    )
    evaluation_times = [
        value
        for value in (
            lineage.get("lineageEvaluatedAt"),
            lineage.get("retrievedAt"),
        )
        if _parse_iso_datetime(value)
    ]
    for evidence_key, status_key, allowed_statuses, fallback in evidence_specs:
        evidence = listing_evidence.get(evidence_key)
        lineage[evidence_key] = _normalized_external_evidence(evidence)
        lineage[status_key] = _verified_evidence_status(
            evidence,
            allowed_statuses,
            fallback,
            symbol,
        )
        if isinstance(evidence, dict) and _parse_iso_datetime(evidence.get("retrievedAt")):
            evaluation_times.append(evidence["retrievedAt"])
    if evaluation_times:
        latest = max(
            evaluation_times,
            key=lambda value: _parse_iso_datetime(value),
        )
        lineage["lineageEvaluatedAt"] = latest
    lineage["listingEvidence"] = {
        "listingSource": listing_evidence.get("listingSource"),
        "sourceAsOf": (
            listing_evidence.get("listingSourceAsOf")
            or listing_evidence.get("lastMappedAt")
        ),
        "listingStatus": listing_evidence.get("listingStatus"),
    }
    if _corporate_lineage_is_comparable(lineage):
        lineage["survivorshipBiasStatus"] = "VERIFIED_CORPORATE_ACTION_LINEAGE"
        lineage["lineageVerifiedForComparison"] = True
    else:
        lineage["survivorshipBiasStatus"] = (
            "UNVERIFIED_INCOMPLETE_CORPORATE_ACTION_COVERAGE"
        )
        lineage["lineageVerifiedForComparison"] = False
    return lineage


def build_corporate_action_runtime_audit(
    rows: list[dict],
    *,
    trigger_file: str | None,
    expected_symbols: list[str] | None = None,
    generated_at: str | None = None,
    external_source_coverage: dict | None = None,
) -> dict:
    generated_at = generated_at or datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    lineage_rows = [row for row in rows if row.get("lineageStatus") == "PRESENT"]
    rejected_rows = [row for row in rows if str(row.get("lineageStatus") or "").startswith("REJECTED_")]
    verified_rows = [row for row in lineage_rows if row.get("lineageVerifiedForComparison") is True]
    unverified_rows = [row for row in lineage_rows if row.get("lineageVerifiedForComparison") is not True]
    expected = sorted({str(symbol or "").strip().upper() for symbol in (expected_symbols or []) if symbol})
    row_symbols = [str(row.get("symbol") or "").strip().upper() for row in rows if row.get("symbol")]
    row_counts = Counter(row_symbols)
    missing_symbols = [symbol for symbol in expected if row_counts.get(symbol, 0) == 0]
    duplicate_symbols = sorted(symbol for symbol, count in row_counts.items() if count > 1)
    structural_contract_ready = not missing_symbols and not duplicate_symbols
    external_source_coverage = (
        external_source_coverage if isinstance(external_source_coverage, dict) else {}
    )
    source_coverage_overall = external_source_coverage.get("overall")
    compact_external_coverage = {
        key: external_source_coverage.get(key)
        for key in (
            "schemaVersion",
            "generatedAt",
            "overall",
            "coverageStart",
            "coverageEnd",
            "requestScopeSymbolsSha256",
            "activeSymbolCount",
            "sources",
            "summary",
        )
        if external_source_coverage.get(key) is not None
    }
    if (
        structural_contract_ready
        and lineage_rows
        and not unverified_rows
        and not rejected_rows
    ):
        comparison_coverage_status = "verified_all_rows"
    elif verified_rows:
        comparison_coverage_status = "verified_rows_available_partial"
    elif source_coverage_overall == "blocked_external_source_contract":
        comparison_coverage_status = "blocked_external_source_contract"
    else:
        comparison_coverage_status = "unverified_external_event_source_coverage"
    if not structural_contract_ready:
        contract_status = "warn_coverage_mismatch"
    elif rejected_rows:
        contract_status = "warn_lineage_rejected"
    elif unverified_rows:
        contract_status = "warn_comparison_lineage_unverified"
    else:
        contract_status = "pass"
    retrieved = sorted(str(row.get("retrievedAt")) for row in lineage_rows if row.get("retrievedAt"))
    source_as_of = sorted(str(row.get("sourceAsOf")) for row in lineage_rows if row.get("sourceAsOf"))
    return {
        "schemaVersion": "corporate-action-lineage-runtime-audit-v1",
        "generatedAt": generated_at,
        "triggerFile": trigger_file,
        "overall": contract_status,
        "summary": {
            "targetRows": len(expected) if expected else len(rows),
            "lineageRows": len(lineage_rows),
            "verifiedForComparisonRows": len(verified_rows),
            "unverifiedRows": len(unverified_rows),
            "rejectedRows": len(rejected_rows),
            "missingRows": len(missing_symbols),
            "duplicateRows": len(duplicate_symbols),
            "structuralContractReady": structural_contract_ready,
            "lineageCoveragePct": (
                round((len(set(row_symbols).intersection(expected)) / len(expected)) * 100, 2)
                if expected
                else 100.0
            ),
            "comparisonCoverageStatus": comparison_coverage_status,
        },
        "sourceTimestamps": {
            "earliestRetrievedAt": retrieved[0] if retrieved else None,
            "latestRetrievedAt": retrieved[-1] if retrieved else None,
            "earliestSourceAsOf": source_as_of[0] if source_as_of else None,
            "latestSourceAsOf": source_as_of[-1] if source_as_of else None,
        },
        "externalSourceCoverage": compact_external_coverage,
        "missingSymbols": missing_symbols,
        "duplicateSymbols": duplicate_symbols,
        "rows": sorted(rows, key=lambda row: str(row.get("symbol") or "")),
    }


def get_latest_ohlcv_date(records):
    if not isinstance(records, list) or not records:
        return None
    try:
        latest = max(
            str(item.get("date", ""))
            for item in records
            if _normalize_ohlcv_record(item) is not None and item.get("date")
        )
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
def sync_ohlcv_incremental(
    ticker,
    ohlcv_dir_id,
    source_symbol=None,
    record_symbol=None,
    listing_evidence=None,
    lineage_sink=None,
):
    source_symbol = source_symbol or ticker
    record_symbol = record_symbol or ticker
    file_name = f"{record_symbol}_OHLCV.json"
    file_id = find_file_id(file_name, ohlcv_dir_id)
    existing_payload = download_json(file_id)
    existing_data, existing_lineage = _extract_ohlcv_payload(existing_payload)
    existing_data, removed_invalid_existing = sanitize_ohlcv_records(existing_data)
    if removed_invalid_existing > 0:
        print(f"🧹 {file_name}: invalid OHLCV rows {removed_invalid_existing}건 제거")

    # [최적화] 최신 거래일까지 이미 수집된 종목은 재호출 스킵
    if existing_data and is_ohlcv_fresh(existing_data) and existing_lineage:
        refreshed_lineage = existing_lineage
        if isinstance(listing_evidence, dict):
            refreshed_lineage = refresh_corporate_action_lineage_evidence(
                existing_lineage,
                listing_evidence,
            )
        lineage_changed = (
            _canonical_sha256(refreshed_lineage)
            != _canonical_sha256(existing_lineage)
        )
        if removed_invalid_existing > 0 or lineage_changed:
            upload_json(
                file_name,
                {
                    "schemaVersion": "ohlcv-lineage-v1",
                    "generatedAt": (
                        refreshed_lineage.get("lineageEvaluatedAt")
                        or refreshed_lineage.get("retrievedAt")
                    ),
                    "symbol": record_symbol,
                    "sourceSymbol": source_symbol,
                    "data": existing_data,
                    "lineage": refreshed_lineage,
                },
                ohlcv_dir_id,
            )
        if isinstance(lineage_sink, list):
            lineage_sink.append(refreshed_lineage)
        return "SKIPPED"

    try:
        stock = yf.Ticker(source_symbol)
        # Legacy arrays receive one full refresh so the initial lineage window is not fabricated.
        period = OHLCV_INCREMENTAL_PERIOD if existing_data and existing_lineage else OHLCV_INITIAL_PERIOD
        df = stock.history(period=period, interval="1d", actions=True, auto_adjust=True)

        if df.empty:
            record_symbol_failure(
                record_symbol,
                "ohlcv",
                "OHLCV_EMPTY",
                "yfinance_history_empty",
                sourceSymbol=source_symbol,
                requestedPeriod=period,
                existingRows=len(existing_data),
            )
            if isinstance(lineage_sink, list):
                lineage_sink.append(
                    {
                        "symbol": record_symbol,
                        "sourceSymbol": source_symbol,
                        "lineageStatus": "REJECTED_VENDOR_MISSING",
                        "reason": "yfinance_history_empty",
                    }
                )
            return "FAILED"

        new_recs, skipped_invalid_new = _normalize_ohlcv_frame(df, record_symbol)

        # Yahoo adjusted OHLC is retrospectively rebased after split/dividend
        # events. Never merge a rebased incremental window into an older basis.
        full_refresh_required = bool(
            existing_data
            and existing_lineage
            and (
                _frame_has_unseen_adjustment_event(df, existing_lineage)
                or _adjusted_price_basis_changed(existing_data, new_recs)
                or not _incremental_window_overlaps(existing_data, new_recs)
            )
        )
        if full_refresh_required:
            period = OHLCV_INITIAL_PERIOD
            df = stock.history(period=period, interval="1d", actions=True, auto_adjust=True)
            if df.empty:
                raise RuntimeError("full_history_refresh_required_but_vendor_returned_empty")
            new_recs, skipped_invalid_new = _normalize_ohlcv_frame(df, record_symbol)
            existing_data = []
            existing_lineage = None

        if skipped_invalid_new > 0:
            print(f"🧹 {file_name}: fetched invalid OHLCV rows {skipped_invalid_new}건 제외")

        # 날짜 기준 중복 제거 및 합치기
        combined = {item["date"]: item for item in (existing_data + new_recs)}
        # 최신 5년치(약 1,260거래일) 데이터 유지 (seasonality / regime 지표용)
        final_list = sorted(combined.values(), key=lambda x: x["date"])[-OHLCV_MAX_BARS:]
        final_list, removed_tail = trim_zero_volume_flat_tail(final_list)
        if removed_tail > 0:
            print(f"🧹 {file_name}: zero-volume flat tail {removed_tail}건 제거")
        if not final_list:
            record_symbol_failure(
                record_symbol,
                "ohlcv",
                "OHLCV_NO_VALID_ROWS",
                "no_valid_ohlcv_rows_after_sanitize",
                sourceSymbol=source_symbol,
                requestedPeriod=period,
                fetchedRows=len(new_recs),
                skippedInvalidRows=skipped_invalid_new,
            )
            if isinstance(lineage_sink, list):
                lineage_sink.append(
                    {
                        "symbol": record_symbol,
                        "sourceSymbol": source_symbol,
                        "lineageStatus": "REJECTED_PARTIAL_HISTORY",
                        "reason": "no_valid_ohlcv_rows_after_sanitize",
                    }
                )
            return "FAILED"

        retrieved_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        lineage = build_corporate_action_lineage(
            df,
            record_symbol=record_symbol,
            source_symbol=source_symbol,
            requested_period=period,
            retrieved_at=retrieved_at,
            listing_evidence=listing_evidence,
            expected_market_date=get_expected_market_date_str(),
            previous_lineage=existing_lineage,
            observation_count=len(final_list),
            stored_rows=final_list,
        )
        upload_json(
            file_name,
            {
                "schemaVersion": "ohlcv-lineage-v1",
                "generatedAt": retrieved_at,
                "symbol": record_symbol,
                "sourceSymbol": source_symbol,
                "data": final_list,
                "lineage": lineage,
            },
            ohlcv_dir_id,
        )
        if isinstance(lineage_sink, list):
            lineage_sink.append(lineage)
        return "UPDATED"
    except Exception as e:
        record_symbol_failure(
            record_symbol,
            "ohlcv",
            "OHLCV_EXCEPTION",
            f"{type(e).__name__}: {e}",
            sourceSymbol=source_symbol,
        )
        print(
            f"⚠️ OHLCV sync 실패 [{record_symbol}] source={source_symbol}: "
            f"{type(e).__name__}: {e}",
            flush=True
        )
        traceback.print_exc()
        if isinstance(lineage_sink, list):
            lineage_sink.append(
                {
                    "symbol": record_symbol,
                    "sourceSymbol": source_symbol,
                    "lineageStatus": "REJECTED_VENDOR_ERROR",
                    "reason": type(e).__name__,
                }
            )
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
        records, _ = _extract_ohlcv_payload(download_json(file_id))
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
        records, _ = _extract_ohlcv_payload(download_json(file_id))
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


def _count_event_field(events, key):
    return dict(Counter(str(event.get(key) or "unknown") for event in events.values()))


def build_earnings_event_coverage_audit(tickers, trigger_file, timestamp, event_payload):
    target_symbols = sorted({str(ticker).upper() for ticker in tickers if ticker})
    events = event_payload.get("events", {}) if isinstance(event_payload, dict) else {}
    matched_symbols = sorted(set(events.keys()) & set(target_symbols))
    missing_symbols = sorted(set(target_symbols) - set(events.keys()))
    event_count = len(events)
    target_count = len(target_symbols)
    coverage_pct = round((len(matched_symbols) / target_count) * 100, 2) if target_count else 0.0
    return {
        "generated_at_utc": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "scope": "stage4_earnings_event_map_coverage_audit",
        "trigger_file": trigger_file,
        "source_timestamp": timestamp,
        "event_map_source": event_payload.get("source", "unavailable") if isinstance(event_payload, dict) else "unavailable",
        "window": event_payload.get("window", {}) if isinstance(event_payload, dict) else {},
        "universe_count": target_count,
        "events_count": event_count,
        "matched_count": len(matched_symbols),
        "missing_count": len(missing_symbols),
        "coverage_pct": coverage_pct,
        "matched_symbols": matched_symbols,
        "missing_symbols": missing_symbols,
        "sample_missing_symbols": missing_symbols[:50],
        "source_counts": _count_event_field(events, "source"),
        "confidence_counts": _count_event_field(events, "confidence"),
        "event_risk_counts": _count_event_field(events, "event_risk"),
        "source_attempts": event_payload.get("source_attempts", []) if isinstance(event_payload, dict) else [],
        "done_when": {
            "eventsCountAvailable": event_count >= 0,
            "matchedSymbolsAvailable": True,
            "missingSymbolsAvailable": True,
            "triggerFileRecorded": bool(trigger_file),
            "sourceTimestampRecorded": bool(timestamp)
        },
        "safety": {
            "brokerMutationAuthorized": False,
            "executionPolicyChanged": False,
            "reason": "harvester report-only coverage audit"
        }
    }


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
        "source_timestamp": timestamp,
        "trigger_file": trigger_file,
        "source": "unavailable",
        "universe_count": len(tickers),
        "covered_count": 0,
        "missing_count": len(tickers),
        "matched_symbols": [],
        "missing_symbols": sorted({ticker.upper() for ticker in tickers if ticker}),
        "source_attempts": [],
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
    source_attempts = []

    # 1) FMP 캘린더 (단일 호출)
    if FMP_API_KEY:
        endpoint_candidates = [
            (
                "stable/earnings-calendar",
                f"https://financialmodelingprep.com/stable/earnings-calendar?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
            ),
            (
                "stable/earning-calendar",
                f"https://financialmodelingprep.com/stable/earning-calendar?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
            ),
            # Legacy fallback: keep for older plans only.
            (
                "api/v3/earning_calendar",
                f"https://financialmodelingprep.com/api/v3/earning_calendar?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
            ),
        ]

        calendar = None
        used_endpoint = None
        endpoint_errors = []
        fmp_before = len(event_map)
        for endpoint_name, url in endpoint_candidates:
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()
                payload_json = response.json() if response.content else []

                if isinstance(payload_json, dict) and payload_json.get("Error Message"):
                    endpoint_errors.append(f"{endpoint_name}: {redact_secret_text(payload_json.get('Error Message'))}")
                    continue

                calendar = payload_json if isinstance(payload_json, list) else []
                used_endpoint = endpoint_name
                break
            except Exception as e:
                endpoint_errors.append(f"{endpoint_name}: {type(e).__name__}: {redact_secret_text(e)}")

        if isinstance(calendar, list):
            for event in calendar:
                symbol = str(event.get('symbol') or '').upper()
                if symbol not in target_set:
                    continue
                date_str = normalize_event_date(event.get('date'))
                upsert_earnings_event(event_map, symbol, date_str, now_date, 'fmp', 'HIGH')

            if any(v.get('source') == 'fmp' for v in event_map.values()):
                source_labels.append('fmp')
                print(f"✅ FMP earnings calendar 사용: {used_endpoint}")
            source_attempts.append({
                "source": "fmp",
                "status": "ok",
                "endpoint": used_endpoint,
                "rows": len(calendar),
                "matched_delta": len(event_map) - fmp_before,
                "errors": endpoint_errors[:5]
            })
        else:
            print(f"⚠️ FMP earnings calendar 실패: {' | '.join(endpoint_errors) if endpoint_errors else 'unknown'}")
            source_attempts.append({
                "source": "fmp",
                "status": "failed",
                "endpoint": used_endpoint,
                "rows": 0,
                "matched_delta": 0,
                "errors": endpoint_errors[:5]
            })
    else:
        source_attempts.append({
            "source": "fmp",
            "status": "skipped_missing_key",
            "endpoint": None,
            "rows": 0,
            "matched_delta": 0,
            "errors": []
        })

    # 2) Finnhub 캘린더 (단일 호출)
    missing_symbols = sorted(target_set - set(event_map.keys()))
    if missing_symbols and FINNHUB_API_KEY:
        try:
            finnhub_before = len(event_map)
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
            source_attempts.append({
                "source": "finnhub",
                "status": "ok",
                "endpoint": "calendar/earnings",
                "rows": len(rows) if isinstance(rows, list) else 0,
                "matched_delta": len(event_map) - finnhub_before,
                "errors": []
            })
        except Exception as e:
            clean_error = redact_secret_text(e)
            print(f"⚠️ Finnhub earnings calendar 실패: {clean_error}")
            source_attempts.append({
                "source": "finnhub",
                "status": "failed",
                "endpoint": "calendar/earnings",
                "rows": 0,
                "matched_delta": 0,
                "errors": [clean_error]
            })
    elif missing_symbols:
        source_attempts.append({
            "source": "finnhub",
            "status": "skipped_missing_key",
            "endpoint": "calendar/earnings",
            "rows": 0,
            "matched_delta": 0,
            "errors": []
        })

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
        source_attempts.append({
            "source": "yfinance",
            "status": "ok",
            "endpoint": "Ticker.get_earnings_dates/calendar/info",
            "rows": len(missing_symbols),
            "matched_delta": yf_found,
            "errors": []
        })
    else:
        source_attempts.append({
            "source": "yfinance",
            "status": "skipped_no_missing_symbols",
            "endpoint": "Ticker.get_earnings_dates/calendar/info",
            "rows": 0,
            "matched_delta": 0,
            "errors": []
        })

    payload["events"] = event_map
    payload["covered_count"] = len(event_map)
    payload["missing_count"] = max(0, len(target_set) - len(event_map))
    payload["matched_symbols"] = sorted(set(event_map.keys()) & target_set)
    payload["missing_symbols"] = sorted(target_set - set(event_map.keys()))
    payload["source"] = '+'.join(source_labels) if source_labels else 'unavailable'
    payload["source_attempts"] = source_attempts
    payload["source_counts"] = _count_event_field(event_map, "source")
    payload["confidence_counts"] = _count_event_field(event_map, "confidence")
    payload["event_risk_counts"] = _count_event_field(event_map, "event_risk")
    payload["window"] = {
        "start_date": start_date,
        "end_date": end_date,
        "max_forward_days": 60
    }

    return payload


# --- [4. 메인 엔진] ---
def run_harvester():
    start_time = time.time()
    started_at_utc = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    total_success, total_error = 0, 0
    total_lifecycle_skipped = 0
    total_mapping_pruned = 0
    target_lineage_observations = []
    completed_lineage_groups = []
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
    is_weekend_update = (now_kst.weekday() in (5, 6))
    run_mode = "dispatch" if GITHUB_EVENT_NAME == 'repository_dispatch' else "daily"
    dispatch_trigger_file = None
    dispatch_total_symbols = 0
    dispatch_benchmark_success = 0
    dispatch_benchmark_fail = 0
    dispatch_benchmark_skipped = 0
    dispatch_market_regime_ready = False
    dispatch_earnings_event_ready = False
    dispatch_earnings_event_count = 0
    dispatch_earnings_event_missing = 0
    dispatch_earnings_event_source = "unavailable"
    dispatch_corporate_action_ready = False
    dispatch_corporate_action_summary = {}
    dispatch_corporate_action_comparison_status = "unverified_external_event_source_coverage"
    daily_group_label = "N/A"
    daily_batch_mode = DAILY_BATCH_MODE or "auto"
    daily_target_count = 0
    mapping_refresh_audit = {"status": "not_run"}

    try:
        print(f"🔍 시스템 가동: {today_str} (Event: {GITHUB_EVENT_NAME})")
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id) # 변수명 오류 수정 (find_id_map 제거)

        # 🎯 1. [특별 작업 모드] 웹앱 신호 시 OHLCV 300개 수집
        if GITHUB_EVENT_NAME == 'repository_dispatch':
            ohlcv_dir_id = find_file_id("Financial_Data_OHLCV", sys_id)
            s3_folder_id = find_file_id("Stage3_Fundamental_Data", root_id)
            dispatch_trigger_file = get_dispatch_trigger_file()
            ticker_mapping = {}
            external_source_coverage = {}
            try:
                ticker_mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
                candidate_mapping = download_json(ticker_mapping_id)
                if isinstance(candidate_mapping, dict):
                    ticker_mapping = candidate_mapping
                mapping_audit_id = find_file_id(TICKER_MAPPING_REFRESH_AUDIT_FILENAME, sys_id)
                candidate_audit = download_json(mapping_audit_id)
                if isinstance(candidate_audit, dict):
                    candidate_coverage = candidate_audit.get("externalCorporateActionCoverage")
                    if isinstance(candidate_coverage, dict):
                        external_source_coverage = candidate_coverage
            except Exception as e:
                print(f"⚠️ Corporate-action listing lineage unavailable: {type(e).__name__}: {e}", flush=True)
            
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
                    dispatch_trigger_file = current_trigger_file
                    
                    # 티커 리스트 추출 및 중복 제거 (순서 보존: 재현성 유지)
                    t_list = s3_data.get('fundamental_universe') or s3_data.get('stocks') or (s3_data if isinstance(s3_data, list) else [])
                    s3_tickers = list(dict.fromkeys(
                        item['symbol'] for item in t_list if isinstance(item, dict) and 'symbol' in item
                    ))
                    
                    if s3_tickers:
                        total_count = len(s3_tickers)
                        dispatch_total_symbols = total_count
                        send_telegram(f"🚀 *수집 시작:* `{total_count}`종목 (OHLCV {OHLCV_INITIAL_PERIOD})")
                        
                        update_progress(0, total_count, "STARTING...", sys_id, "PROCESSING", current_trigger_file)

                        ohlcv_skipped = 0
                        corporate_action_lineage_rows = []
                        for idx, st in enumerate(s3_tickers, 1):
                            listing_evidence = dict(ticker_mapping.get(st) or {})
                            mapping_meta = ticker_mapping.get("_meta") if isinstance(ticker_mapping.get("_meta"), dict) else {}
                            listing_evidence["listingSourceAsOf"] = (
                                listing_evidence.get("lastMappedAt") or mapping_meta.get("generatedAt")
                            )
                            sync_status = sync_ohlcv_incremental(
                                st,
                                ohlcv_dir_id,
                                listing_evidence=listing_evidence,
                                lineage_sink=corporate_action_lineage_rows,
                            )
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

                        try:
                            corporate_action_audit = build_corporate_action_runtime_audit(
                                corporate_action_lineage_rows,
                                trigger_file=current_trigger_file,
                                expected_symbols=s3_tickers,
                                external_source_coverage=external_source_coverage,
                            )
                            write_json_report(
                                HARVESTER_CORPORATE_ACTION_RUNTIME_AUDIT_PATH,
                                corporate_action_audit,
                                "Corporate-action lineage runtime audit",
                            )
                            upload_json(
                                CORPORATE_ACTION_LINEAGE_AUDIT_FILENAME,
                                corporate_action_audit,
                                sys_id,
                            )
                            dispatch_corporate_action_summary = corporate_action_audit.get("summary", {})
                            dispatch_corporate_action_ready = bool(
                                dispatch_corporate_action_summary.get(
                                    "structuralContractReady"
                                )
                            )
                            dispatch_corporate_action_comparison_status = (
                                dispatch_corporate_action_summary.get(
                                    "comparisonCoverageStatus"
                                )
                                or "unverified_external_event_source_coverage"
                            )
                        except Exception as e:
                            print(f"⚠️ Corporate-action lineage audit 생성 실패: {type(e).__name__}: {e}", flush=True)

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
                        dispatch_benchmark_success = benchmark_success
                        dispatch_benchmark_fail = benchmark_fail
                        dispatch_benchmark_skipped = benchmark_skipped

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
                        dispatch_market_regime_ready = market_regime_ready

                        earnings_event_ready = False
                        earnings_event_count = 0
                        earnings_event_source = "unavailable"
                        earnings_event_missing = total_count
                        try:
                            earnings_event_map = fetch_earnings_event_map(s3_tickers, current_trigger_file, today_str)
                            earnings_event_audit = build_earnings_event_coverage_audit(
                                s3_tickers,
                                current_trigger_file,
                                today_str,
                                earnings_event_map
                            )
                            earnings_event_count = len(earnings_event_map.get('events', {}))
                            earnings_event_source = earnings_event_map.get('source', 'unavailable')
                            earnings_event_missing = int(earnings_event_map.get('missing_count', max(0, total_count - earnings_event_count)))
                            write_json_report(
                                HARVESTER_EARNINGS_EVENT_COVERAGE_AUDIT_PATH,
                                earnings_event_audit,
                                "Stage4 earnings event coverage audit"
                            )
                            upload_json(EARNINGS_EVENT_FILENAME, earnings_event_map, sys_id)
                            upload_json(EARNINGS_EVENT_COVERAGE_AUDIT_FILENAME, earnings_event_audit, sys_id)
                            earnings_event_ready = True
                            print(
                                "📅 실적 이벤트 맵 완료: "
                                f"{earnings_event_count}건 "
                                f"(source: {earnings_event_source}, missing: {earnings_event_missing}, "
                                f"audit={EARNINGS_EVENT_COVERAGE_AUDIT_FILENAME})"
                            )
                        except Exception as e:
                            print(f"⚠️ 실적 이벤트 맵 업로드 실패: {str(e)}")
                        dispatch_earnings_event_ready = earnings_event_ready
                        dispatch_earnings_event_count = earnings_event_count
                        dispatch_earnings_event_missing = earnings_event_missing
                        dispatch_earnings_event_source = earnings_event_source

                        update_progress(total_count, total_count, "FINISHED", sys_id, "COMPLETED", current_trigger_file)

                        upload_json(
                            "LATEST_STAGE4_READY.json",
                            {
                                "status": "COMPLETED",
                                "trigger_file": current_trigger_file,
                                "timestamp": today_str,
                                "corporateActionLineageStatus": (
                                    (
                                        "CONTRACT_READY_OOS_VERIFIED"
                                        if dispatch_corporate_action_comparison_status
                                        == "verified_all_rows"
                                        else "CONTRACT_READY_OOS_BLOCKED"
                                    )
                                    if dispatch_corporate_action_ready
                                    else "COVERAGE_MISMATCH"
                                ),
                                "corporateActionLineageAudit": CORPORATE_ACTION_LINEAGE_AUDIT_FILENAME,
                                "corporateActionOosComparisonStatus": (
                                    dispatch_corporate_action_comparison_status
                                ),
                            },
                            sys_id,
                        )
                        regime_status = "READY" if market_regime_ready else "SKIPPED"
                        earnings_status = "READY" if earnings_event_ready else "SKIPPED"
                        failure_line = failure_telegram_summary()
                        send_telegram(f"✅ *Stage 4 수집 완료!*\n성공: `{total_success}` (skip `{ohlcv_skipped}`) | 실패: `{total_error}`\n벤치마크: `{benchmark_success}` 성공 (skip `{benchmark_skipped}`) / `{benchmark_fail}` 실패\n시장국면: `{regime_status}`\n실적이벤트: `{earnings_status}` ({earnings_event_count})\n{failure_line}")
            duration = (time.time() - start_time) / 60
            summary_payload = {
                "status": "success",
                "startedAt": started_at_utc,
                "completedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                "eventName": GITHUB_EVENT_NAME or "unknown",
                "mode": run_mode,
                "triggerFile": dispatch_trigger_file,
                "targetSymbols": dispatch_total_symbols,
                "batchLabel": "dispatch_stage4",
                "batchMode": "repository_dispatch",
                "marketRegimeReady": dispatch_market_regime_ready,
                "earningsEventReady": dispatch_earnings_event_ready,
                "earningsEventCount": dispatch_earnings_event_count,
                "earningsEventMissing": dispatch_earnings_event_missing,
                "earningsEventSource": dispatch_earnings_event_source,
                "earningsEventCoverageAudit": EARNINGS_EVENT_COVERAGE_AUDIT_FILENAME,
                "earningsEventCoverageAuditPath": HARVESTER_EARNINGS_EVENT_COVERAGE_AUDIT_PATH,
                "corporateActionLineageReady": dispatch_corporate_action_ready,
                "corporateActionLineageSummary": dispatch_corporate_action_summary,
                "corporateActionOosComparisonStatus": (
                    dispatch_corporate_action_comparison_status
                ),
                "corporateActionLineageAudit": CORPORATE_ACTION_LINEAGE_AUDIT_FILENAME,
                "corporateActionLineageAuditPath": HARVESTER_CORPORATE_ACTION_RUNTIME_AUDIT_PATH,
                "corporateActionExternalCoverageOverall": external_source_coverage.get("overall"),
                "corporateActionExternalSourceStatuses": (
                    (external_source_coverage.get("summary") or {}).get("sourceStatuses")
                    or {
                        key: (value or {}).get("status")
                        for key, value in (external_source_coverage.get("sources") or {}).items()
                    }
                ),
                "benchmarkSuccess": dispatch_benchmark_success,
                "benchmarkSkipped": dispatch_benchmark_skipped,
                "benchmarkFailed": dispatch_benchmark_fail,
                "successCount": total_success,
                "errorCount": total_error,
                "durationMinutes": round(duration, 2),
            }
            failure_report = build_harvester_failure_report(summary_payload)
            write_harvester_failure_report(failure_report)
            summary_payload["failureReportPath"] = HARVESTER_FAILURE_REPORT_PATH
            summary_payload["failureCategoryCounts"] = failure_report.get("failureSummary", {}).get("categoryCounts", {})
            summary_payload["failureSamples"] = failure_report.get("failures", [])[:HARVESTER_FAILURE_SAMPLE_LIMIT]
            write_harvester_run_summary(summary_payload)
            return # dispatch 작업이 끝났으므로 여기서 명시적으로 종료

        # 🎯 2. [데일리 수집 모드] (스케줄러로 실행될 때 여기로 옴)
        daily_dir_id = find_file_id("Financial_Data_Daily", sys_id)
        hist_dir_id = find_file_id("Financial_Data_History_5Y", sys_id)
        symbol_state_file_id = find_file_id(HARVESTER_SYMBOL_STATE_FILENAME, sys_id)
        symbol_state = download_json(symbol_state_file_id) if symbol_state_file_id else {}
        if not isinstance(symbol_state, dict):
            symbol_state = {}
        symbol_state_touched = set()
        group_label, target_chars, batch_mode_source = resolve_daily_batch(now_kst)
        daily_group_label = group_label
        daily_batch_mode = batch_mode_source
        print(f"🧩 데일리 배치 선택: {group_label} (mode={batch_mode_source})")

        full_map_file_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(full_map_file_id)
        
        # 딕셔너리가 아닌 경우 빈 딕셔너리로 초기화 방어
        if not isinstance(full_map, dict):
            full_map = {}

        previous_mapping_refresh_audit = {}
        try:
            previous_mapping_audit_id = find_file_id(
                TICKER_MAPPING_REFRESH_AUDIT_FILENAME,
                sys_id,
            )
            previous_mapping_refresh_audit = (
                download_json(previous_mapping_audit_id)
                if previous_mapping_audit_id
                else {}
            )
        except Exception as exc:
            print(
                "⚠️ Previous external corporate-action coverage unavailable: "
                f"{type(exc).__name__}: {_short_failure_text(exc, 180)}",
                flush=True,
            )
        full_map, mapping_refresh_audit = refresh_ticker_mapping_from_authoritative_sources(
            full_map,
            today_str,
            previous_mapping_audit=previous_mapping_refresh_audit,
        )
        write_json_report(
            HARVESTER_TICKER_MAPPING_REFRESH_AUDIT_PATH,
            mapping_refresh_audit,
            "Ticker mapping refresh audit",
        )
        upload_json(TICKER_MAPPING_REFRESH_AUDIT_FILENAME, mapping_refresh_audit, sys_id)
        if mapping_refresh_audit.get("status") == "refreshed":
            upload_json("Ticker_ID_Mapping_Final.json", full_map, sys_id)
            print(
                "🗺️ Ticker mapping refreshed from authoritative listings: "
                f"active={mapping_refresh_audit.get('refreshedSymbols')} "
                f"added={mapping_refresh_audit.get('addedCount')} "
                f"removed={mapping_refresh_audit.get('removedCount')}",
                flush=True,
            )
        else:
            print(f"🗺️ Ticker mapping refresh status={mapping_refresh_audit.get('status')}", flush=True)
            
        filtered_tickers = {
            t: info
            for t, info in full_map.items()
            if isinstance(t, str)
            and t
            and isinstance(info, dict)
            and info.get("group")
            and ((t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars))
        }
        daily_target_count = len(filtered_tickers)

        send_telegram(
            f"📡 *[Daily] 본계정 가동*\n"
            f"🎯 *타겟:* `{group_label}` | `{len(filtered_tickers)}`종목\n"
            f"🧩 mode: `{batch_mode_source}`"
        )

        groups = sorted(list(set(info['group'] for info in filtered_tickers.values())))

        for group in groups:
            group_tickers = {t: info for t, info in filtered_tickers.items() if info['group'] == group}
            g_total = len(group_tickers)
            g_success, g_error, g_lifecycle_skipped, g_mapping_pruned = 0, 0, 0, 0
            print(f"\n--- 📦 그룹 [{group}] 작업 시작 ---")
            daily_name, hist_name = f"{group}_stocks_daily.json", f"{group}_stocks_history.json"
            
            daily_data = download_json(find_file_id(daily_name, daily_dir_id))
            hist_data = download_json(find_file_id(hist_name, hist_dir_id))
            
            if not isinstance(daily_data, dict): daily_data = {}
            if not isinstance(hist_data, dict): hist_data = {}

            active_group_symbols = set(group_tickers.keys())
            stale_daily_symbols = sorted(
                str(symbol).strip()
                for symbol in daily_data.keys()
                if isinstance(symbol, str)
                and symbol
                and not symbol.startswith("_")
                and symbol.strip().upper() not in active_group_symbols
            )
            for stale_symbol in stale_daily_symbols:
                daily_data.pop(stale_symbol, None)
                hist_data.pop(stale_symbol, None)
                g_mapping_pruned += 1
                record_symbol_skip(
                    stale_symbol,
                    "daily_mapping_prune",
                    "SYMBOL_PRUNED_MAPPING_ABSENT",
                    "absent_from_refreshed_Ticker_ID_Mapping_Final",
                    group=group,
                )

            for i, ticker in enumerate(group_tickers, 1):
                success_flag = False
                failure_recorded = False
                last_attempt_error = None
                listing_info = group_tickers.get(ticker) if isinstance(group_tickers.get(ticker), dict) else {}
                previous_state_entry = symbol_state.get(ticker) if isinstance(symbol_state, dict) else None
                should_skip, skip_category, skip_reason = should_skip_symbol_for_collection(
                    previous_state_entry,
                    authoritative_mapping_refreshed=mapping_refresh_audit.get("status") == "refreshed",
                )
                if should_skip:
                    g_lifecycle_skipped += 1
                    if isinstance(previous_state_entry, dict):
                        previous_state_entry["lastSkippedAt"] = today_str
                        previous_state_entry["lastSkipReason"] = skip_reason
                    record_symbol_skip(
                        ticker,
                        "daily_lifecycle",
                        skip_category,
                        skip_reason,
                        group=group,
                        state=(previous_state_entry or {}).get("state"),
                        instrumentType=(previous_state_entry or {}).get("instrumentType"),
                        missingQuoteStreak=(previous_state_entry or {}).get("missingQuoteStreak"),
                        missingHistoryStreak=(previous_state_entry or {}).get("missingHistoryStreak"),
                    )
                    print(
                        f"⏭️ LIFECYCLE_SKIP [{ticker}] category={skip_category} reason={skip_reason}",
                        flush=True
                    )
                    continue
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
                            info_previous_close = _first_present(info, [
                                'regularMarketPreviousClose',
                                'previousClose',
                            ])
                            info_regular_market_change = _first_present(info, [
                                'regularMarketChange',
                            ])
                            info_regular_market_change_pct = _first_present(info, [
                                'regularMarketChangePercent',
                            ])
                            info_quote_timestamp = _first_present(info, [
                                'regularMarketTime',
                                'postMarketTime',
                                'preMarketTime',
                            ])
                            info_net_income = _first_present(info, [
                                'netIncome',
                                'netIncomeToCommon',
                            ])
                            info_net_income_common = _first_present(info, [
                                'netIncomeCommonStockholders',
                                'netIncomeToCommon',
                                'netIncome',
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
                            history_entry = hist_data.get(ticker)
                            history_net_income, history_net_income_asof = _extract_latest_financial_value(
                                history_entry,
                                FIN_HISTORY_NET_INCOME_KEYS
                            )
                            net_income_value = info_net_income if info_net_income not in (None, '') else history_net_income
                            net_income_common_value = (
                                info_net_income_common
                                if info_net_income_common not in (None, '')
                                else history_net_income
                            )
                            net_income_source = (
                                'INFO'
                                if info_net_income not in (None, '') or info_net_income_common not in (None, '')
                                else ('HISTORY' if history_net_income is not None else 'MISSING')
                            )
                            has_quote_payload = any(
                                value not in (None, '')
                                for value in [
                                    info_previous_close,
                                    info_regular_market_change,
                                    info_regular_market_change_pct,
                                    info_quote_timestamp
                                ]
                            )
                            quote_source = 'YFINANCE_INFO' if has_quote_payload else 'MISSING'
                            instrument_type, analysis_eligible = _collection_instrument_profile(
                                ticker,
                                listing_info,
                                info,
                            )
                            net_income_asof = (
                                today_str
                                if net_income_source == 'INFO'
                                else (history_net_income_asof or None)
                            )
                            history_rows = _history_rows_from_entry(history_entry)
                            history_periods = len(history_rows)
                            history_tier = _derive_history_tier(history_periods)
                            symbol_state_entry = _update_symbol_state_entry(
                                symbol_state,
                                ticker,
                                {
                                    "analysisEligible": analysis_eligible,
                                    "instrumentType": instrument_type,
                                    "historyTier": history_tier,
                                    "historyPeriods": history_periods,
                                    "hasQuotePayload": has_quote_payload,
                                },
                                symbol_state_touched,
                                today_str
                            )
                            symbol_lifecycle_state = str(symbol_state_entry.get("state") or "UNKNOWN").upper()
                            symbol_state_reason = str(symbol_state_entry.get("reason") or "unknown")
                            history_missing_streak = int(symbol_state_entry.get("missingHistoryStreak") or 0)
                            quote_missing_streak = int(symbol_state_entry.get("missingQuoteStreak") or 0)
                            current_assets_raw = info_current_assets if info_current_assets not in (None, '') else distress_fields.get("currentAssets")
                            current_liabilities_raw = info_current_liabilities if info_current_liabilities not in (None, '') else distress_fields.get("currentLiabilities")
                            current_assets_num = _to_finite_float(current_assets_raw)
                            current_liabilities_num = _to_finite_float(current_liabilities_raw)
                            working_capital = None
                            if current_assets_num is not None and current_liabilities_num is not None:
                                working_capital = current_assets_num - current_liabilities_num

                            # [FIX] Restore legacy raw-record mapping so STANDARD_KEYS are filled with
                            # Yahoo source keys (trailingPE, priceToBook, returnOnEquity, etc).
                            target_mean_price = info.get('targetMeanPrice')
                            target_lineage = build_target_lineage(
                                target_mean_price,
                                datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                            )
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
                                "targetMeanPrice": target_mean_price,
                                **target_lineage,
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
                                "previousClose": info_previous_close,
                                "regularMarketPreviousClose": info_previous_close,
                                "regularMarketChange": info_regular_market_change,
                                "regularMarketChangePercent": info_regular_market_change_pct,
                                "netIncome": net_income_value,
                                "netIncomeCommonStockholders": net_income_common_value,
                                "quoteTimestamp": info_quote_timestamp,
                                "quoteSource": quote_source,
                                "netIncomeSource": net_income_source,
                                "netIncomeAsOf": net_income_asof,
                                "instrumentType": instrument_type,
                                "analysisEligible": analysis_eligible,
                                "historyPeriods": history_periods,
                                "historyTier": history_tier,
                                "symbolLifecycleState": symbol_lifecycle_state,
                                "stateUpdatedAt": today_str,
                                "historyMissingStreak": history_missing_streak,
                                "quoteMissingStreak": quote_missing_streak,
                                "stateReason": symbol_state_reason,
                                "fiftyDayAverage": info.get('fiftyDayAverage'),
                                "twoHundredDayAverage": info.get('twoHundredDayAverage'),
                                "fiftyTwoWeekHigh": info.get('fiftyTwoWeekHigh'),
                                "fiftyTwoWeekLow": info.get('fiftyTwoWeekLow'),
                                "sector": info.get('sector'),
                                "industry": info.get('industry')
                            }

                            prev_record = daily_data.get(ticker, {}) if isinstance(daily_data.get(ticker), dict) else {}
                            daily_data[ticker] = merge_standard_record(prev_record, raw_record)
                            
                            g_success += 1
                            success_flag = True
                            break
                        else:
                            history_entry = hist_data.get(ticker)
                            history_rows = _history_rows_from_entry(history_entry)
                            history_periods = len(history_rows)
                            history_tier = _derive_history_tier(history_periods)
                            instrument_type, analysis_eligible = _collection_instrument_profile(
                                ticker,
                                listing_info,
                                info,
                            )
                            symbol_state_entry = _update_symbol_state_entry(
                                symbol_state,
                                ticker,
                                {
                                    "analysisEligible": analysis_eligible,
                                    "instrumentType": instrument_type,
                                    "historyTier": history_tier,
                                    "historyPeriods": history_periods,
                                    "hasQuotePayload": False,
                                },
                                symbol_state_touched,
                                today_str
                            )
                            symbol_lifecycle_state = str(symbol_state_entry.get("state") or "UNKNOWN").upper()
                            symbol_state_reason = str(symbol_state_entry.get("reason") or "unknown")
                            history_missing_streak = int(symbol_state_entry.get("missingHistoryStreak") or 0)
                            quote_missing_streak = int(symbol_state_entry.get("missingQuoteStreak") or 0)

                            prev_record = daily_data.get(ticker, {}) if isinstance(daily_data.get(ticker), dict) else {}
                            quote_missing_record = {
                                "symbol": ticker,
                                "name": info.get('shortName') or info.get('longName') or prev_record.get("name"),
                                "updated": today_str,
                                "Hist": hist_status,
                                "instrumentType": instrument_type,
                                "analysisEligible": analysis_eligible,
                                "historyPeriods": history_periods,
                                "historyTier": history_tier,
                                "symbolLifecycleState": symbol_lifecycle_state,
                                "stateUpdatedAt": today_str,
                                "historyMissingStreak": history_missing_streak,
                                "quoteMissingStreak": quote_missing_streak,
                                "stateReason": symbol_state_reason,
                                "quoteSource": "MISSING",
                            }
                            daily_data[ticker] = merge_standard_record(prev_record, quote_missing_record)
                            record_symbol_failure(
                                ticker,
                                "daily_quote",
                                "QUOTE_MISSING",
                                symbol_state_reason,
                                group=group,
                                instrumentType=instrument_type,
                                historyTier=history_tier,
                                historyPeriods=history_periods,
                                quoteMissingStreak=quote_missing_streak,
                                historyMissingStreak=history_missing_streak,
                            )
                            failure_recorded = True

                            print(
                                f"⚠️ QUOTE_MISSING [{ticker}] price unavailable | "
                                f"state={symbol_lifecycle_state} reason={symbol_state_reason} "
                                f"quoteMissingStreak={quote_missing_streak} "
                                f"historyTier={history_tier} periods={history_periods}",
                                flush=True
                            )
                            break
                    except Exception as e:
                        last_attempt_error = {
                            "type": type(e).__name__,
                            "message": _short_failure_text(e),
                            "attempt": attempt + 1,
                        }
                        if "SSL" in str(e) or "EOF" in str(e):
                            time.sleep(5)
                        elif attempt == 2:
                            print(f"⚠️ 수집 재시도 소진 [{ticker}]: {type(e).__name__}: {e}", flush=True)
                
                if not success_flag:
                    g_error += 1
                    if not failure_recorded:
                        if last_attempt_error:
                            record_symbol_failure(
                                ticker,
                                "daily_quote",
                                "DAILY_EXCEPTION_RETRY_EXHAUSTED",
                                f"{last_attempt_error.get('type')}: {last_attempt_error.get('message')}",
                                group=group,
                                attempts=last_attempt_error.get("attempt"),
                            )
                        else:
                            record_symbol_failure(
                                ticker,
                                "daily_quote",
                                "DAILY_NO_SUCCESS",
                                "quote_or_history_unclassified_failure",
                                group=group,
                            )

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

            requested_raw_keys = RAW_QUOTE_OPTIONAL_KEYS + RAW_FUNDAMENTAL_OPTIONAL_KEYS
            raw_cov = summarize_key_coverage(group_records, requested_raw_keys)
            target_lineage_observations.extend(
                {
                    "targetMeanPrice": record.get("targetMeanPrice"),
                    "targetMeanPriceSource": record.get("targetMeanPriceSource"),
                    "targetMeanPriceRetrievedAt": record.get("targetMeanPriceRetrievedAt"),
                    "targetMeanPriceAsOfStatus": record.get("targetMeanPriceAsOfStatus"),
                }
                for record in group_records.values()
                if isinstance(record, dict)
            )
            raw_status_preview = []
            for key in requested_raw_keys:
                pct = raw_cov.get(key, {}).get("coveragePct", 0.0)
                if pct >= 95:
                    status = "RECEIVED"
                elif pct > 0:
                    status = "REQUESTED_BUT_PARTIAL"
                else:
                    status = "REQUESTED_BUT_MISSING"
                raw_status_preview.append(f"{key}:{status}({pct}%)")
            print(f"   🔎 [{group}] Raw request audit: {', '.join(raw_status_preview)}")

            type_counts = {}
            eligible_count = 0
            for rec in group_records.values():
                if not isinstance(rec, dict):
                    continue
                t = str(rec.get("instrumentType") or "unknown").strip().lower() or "unknown"
                type_counts[t] = type_counts.get(t, 0) + 1
                if bool(rec.get("analysisEligible")):
                    eligible_count += 1
            type_preview = ", ".join(
                [f"{k}:{v}" for k, v in sorted(type_counts.items(), key=lambda x: x[0])]
            ) or "none"
            excluded_count = max(0, len(group_records) - eligible_count)
            print(
                f"   🧭 [{group}] Instrument profile: {type_preview} | eligible(common)={eligible_count} excluded={excluded_count}"
            )
            lifecycle_counts = {}
            for rec in group_records.values():
                if not isinstance(rec, dict):
                    continue
                state = str(rec.get("symbolLifecycleState") or "UNKNOWN").strip().upper() or "UNKNOWN"
                lifecycle_counts[state] = lifecycle_counts.get(state, 0) + 1
            lifecycle_preview = ", ".join(
                [f"{k}:{v}" for k, v in sorted(lifecycle_counts.items(), key=lambda x: x[0])]
            ) or "none"
            print(f"   🛡️ [{group}] Symbol lifecycle: {lifecycle_preview}")

            # 데일리 데이터와 히스토리 데이터 모두 업로드
            upload_json(daily_name, daily_data, daily_dir_id)
            upload_json(hist_name, hist_data, hist_dir_id)
            
            total_success += g_success
            total_error += g_error
            total_lifecycle_skipped += g_lifecycle_skipped
            total_mapping_pruned += g_mapping_pruned
            completed_lineage_groups.append(group)

            target_lineage_generated_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            target_lineage_runtime = build_target_lineage_runtime_audit(
                target_lineage_observations,
                reference_time=target_lineage_generated_at,
                freshness_max_hours=HARVESTER_TARGET_LINEAGE_MAX_AGE_HOURS,
                batch_mode=daily_batch_mode,
                target_symbols=daily_target_count,
                completed_groups=completed_lineage_groups,
                collection_status="partial_checkpoint",
            )
            write_json_report(
                HARVESTER_TARGET_LINEAGE_RUNTIME_AUDIT_PATH,
                target_lineage_runtime,
                "Target lineage runtime audit checkpoint",
            )
            
            print(
                f"📦 그룹 [{group}] 완료: 성공 {g_success} / 실패 {g_error} / "
                f"lifecycle skip {g_lifecycle_skipped} / mapping prune {g_mapping_pruned}"
            )
            send_telegram(
                f"📦 *그룹 [{group}] 완료*\n"
                f"✅ 성공: `{g_success}` | ❌ 실패: `{g_error}` | "
                f"⏭️ lifecycle skip: `{g_lifecycle_skipped}` | 🧹 mapping prune: `{g_mapping_pruned}`"
            )

        _apply_symbol_retire_policy(symbol_state, symbol_state_touched, today_str)
        upload_json(HARVESTER_SYMBOL_STATE_FILENAME, symbol_state, sys_id)

        duration = (time.time() - start_time) / 60
        mapping_audit = build_mapping_freshness_audit(
            full_map,
            filtered_tickers,
            symbol_state,
            today_str,
            daily_group_label,
            daily_batch_mode,
        )
        write_json_report(
            HARVESTER_MAPPING_FRESHNESS_AUDIT_PATH,
            mapping_audit,
            "Harvester mapping freshness audit",
        )
        write_text_report(
            HARVESTER_MAPPING_FRESHNESS_AUDIT_MD_PATH,
            _mapping_audit_markdown(mapping_audit),
            "Harvester mapping freshness audit markdown payload",
        )
        upload_json(HARVESTER_MAPPING_FRESHNESS_AUDIT_FILENAME, mapping_audit, sys_id)
        target_lineage_generated_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        target_lineage_runtime = build_target_lineage_runtime_audit(
            target_lineage_observations,
            reference_time=target_lineage_generated_at,
            freshness_max_hours=HARVESTER_TARGET_LINEAGE_MAX_AGE_HOURS,
            batch_mode=daily_batch_mode,
            target_symbols=daily_target_count,
            completed_groups=completed_lineage_groups,
            collection_status="completed",
        )
        write_json_report(
            HARVESTER_TARGET_LINEAGE_RUNTIME_AUDIT_PATH,
            target_lineage_runtime,
            "Target lineage runtime audit",
        )
        failure_line = failure_telegram_summary()
        send_telegram(
            f"🏁 *수집 종료*\n"
            f"⏱️ `{duration:.1f}분` | 성공: `{total_success}` | 실패: `{total_error}` | "
            f"lifecycle skip: `{total_lifecycle_skipped}` | mapping prune: `{total_mapping_pruned}`\n{failure_line}"
        )
        summary_payload = {
            "status": "success",
            "startedAt": started_at_utc,
            "completedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "eventName": GITHUB_EVENT_NAME or "unknown",
            "mode": run_mode,
            "batchLabel": daily_group_label,
            "batchMode": daily_batch_mode,
            "targetSymbols": daily_target_count,
            "weekendUpdate": bool(is_weekend_update),
            "successCount": total_success,
            "errorCount": total_error,
            "lifecycleSkipCount": total_lifecycle_skipped,
            "mappingPruneCount": total_mapping_pruned,
            "durationMinutes": round(duration, 2),
        }
        failure_report = build_harvester_failure_report(summary_payload)
        write_harvester_failure_report(failure_report)
        summary_payload["failureReportPath"] = HARVESTER_FAILURE_REPORT_PATH
        summary_payload["tickerMappingRefreshAuditPath"] = HARVESTER_TICKER_MAPPING_REFRESH_AUDIT_PATH
        summary_payload["tickerMappingRefreshStatus"] = mapping_refresh_audit.get("status")
        summary_payload["tickerMappingAddedCount"] = mapping_refresh_audit.get("addedCount", 0)
        summary_payload["tickerMappingRemovedCount"] = mapping_refresh_audit.get("removedCount", 0)
        external_coverage = mapping_refresh_audit.get("externalCorporateActionCoverage") or {}
        summary_payload["corporateActionExternalCoverageOverall"] = external_coverage.get("overall")
        summary_payload["corporateActionExternalSourceStatuses"] = (
            (external_coverage.get("summary") or {}).get("sourceStatuses")
            or {
                key: (value or {}).get("status")
                for key, value in (external_coverage.get("sources") or {}).items()
            }
        )
        summary_payload["corporateActionExternalApplication"] = (
            mapping_refresh_audit.get("externalCorporateActionApplication") or {}
        )
        summary_payload["mappingFreshnessAuditPath"] = HARVESTER_MAPPING_FRESHNESS_AUDIT_PATH
        summary_payload["failureCategoryCounts"] = failure_report.get("failureSummary", {}).get("categoryCounts", {})
        summary_payload["skipCategoryCounts"] = failure_report.get("skipSummary", {}).get("categoryCounts", {})
        summary_payload["mappingFreshnessActionCounts"] = mapping_audit.get("actionCounts", {})
        summary_payload["targetLineageRuntimeAuditPath"] = HARVESTER_TARGET_LINEAGE_RUNTIME_AUDIT_PATH
        summary_payload["targetLineageRuntime"] = target_lineage_runtime
        summary_payload["failureSamples"] = failure_report.get("failures", [])[:HARVESTER_FAILURE_SAMPLE_LIMIT]
        write_harvester_run_summary(summary_payload)

    except Exception as e:
        record_symbol_failure("RUN", "fatal", "RUN_FATAL", f"{type(e).__name__}: {e}")
        send_telegram(f"🚨 *에러 발생:* `{str(e)}` ", channel="alert")
        duration = (time.time() - start_time) / 60
        summary_payload = {
            "status": "failed",
            "startedAt": started_at_utc,
            "completedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "eventName": GITHUB_EVENT_NAME or "unknown",
            "mode": run_mode,
            "batchLabel": daily_group_label,
            "batchMode": daily_batch_mode,
            "targetSymbols": daily_target_count,
            "successCount": total_success,
            "errorCount": total_error,
            "lifecycleSkipCount": total_lifecycle_skipped,
            "mappingPruneCount": total_mapping_pruned,
            "durationMinutes": round(duration, 2),
            "errorType": type(e).__name__,
            "errorMessage": str(e),
        }
        failure_report = build_harvester_failure_report(summary_payload)
        write_harvester_failure_report(failure_report)
        summary_payload["failureReportPath"] = HARVESTER_FAILURE_REPORT_PATH
        summary_payload["tickerMappingRefreshAuditPath"] = HARVESTER_TICKER_MAPPING_REFRESH_AUDIT_PATH
        summary_payload["tickerMappingRefreshStatus"] = mapping_refresh_audit.get("status")
        summary_payload["failureCategoryCounts"] = failure_report.get("failureSummary", {}).get("categoryCounts", {})
        summary_payload["skipCategoryCounts"] = failure_report.get("skipSummary", {}).get("categoryCounts", {})
        summary_payload["failureSamples"] = failure_report.get("failures", [])[:HARVESTER_FAILURE_SAMPLE_LIMIT]
        write_harvester_run_summary(summary_payload)
        print(f"⛔ run_harvester fatal: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        raise

if __name__ == "__main__":
    run_harvester()
