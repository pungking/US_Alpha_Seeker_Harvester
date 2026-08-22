from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests


SCHEMA_VERSION = "macro-event-clock-shadow-capability-v1"
PASS_STATUS = "MACRO_EVENT_CLOCK_CAPABILITY_PASS"
REQUEST_BUDGETS = {
    "federalReserveCalendar": 1,
    "blsMetadata": 1,
    "blsData": 1,
    "beaMetadata": 1,
    "beaData": 1,
    "fredMetadata": 1,
    "fredData": 1,
}
USER_AGENT = "US Alpha Seeker macro-event-clock capability/1.0"
BLS_CAPABILITY_SERIES_ID = "CUUR0000SA0"
BLOCKER_ONLY_SCHEMA_VERSION = "macro-event-clock-blocker-reproof-v1"
BLOCKER_ONLY_PASS_STATUS = "MACRO_BLOCKER_ONLY_REPROOF_PASS"
BLOCKER_ONLY_APPROVAL = "AUTHORIZE MACRO EVENT CLOCK BLOCKER-ONLY REPROOF"
BLOCKER_ONLY_REQUEST_BUDGETS = {
    "federalReserveCalendar": 0,
    "fredMetadata": 0,
    "fredData": 0,
    "sec": 0,
    "finra": 0,
    "toss": 0,
    "blsData": 0,
    "blsIcal": 1,
    "blsHtmlFallback": 1,
    "beaMetadata": 1,
    "beaData": 1,
}
BLOCKER_ONLY_BASELINE_RUN_ID = "32541234706"
BLS_REGISTERED_SCHEMA_VERSION = "bls-registered-data-capability-v1"
BLS_REGISTERED_PASS_STATUS = "BLS_REGISTERED_DATA_API_PASS"
BLS_REGISTERED_APPROVAL = "AUTHORIZE BLS REGISTERED DATA API ONE-SHOT"
BLS_REGISTERED_REQUEST_BUDGETS = {
    "blsRegisteredData": 1,
    "blsIcal": 0,
    "blsHtmlFallback": 0,
    "blsData": 0,
    "federalReserveCalendar": 0,
    "beaMetadata": 0,
    "beaData": 0,
    "fredMetadata": 0,
    "fredData": 0,
    "sec": 0,
    "finra": 0,
    "toss": 0,
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _configured(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and not any(
        marker in text
        for marker in ("replace", "placeholder", "발급받은", "api_key")
    )


def macro_event_clock_runtime_decision(
    environment: Mapping[str, Any],
) -> tuple[bool, str]:
    if not _configured(environment.get("BEA_API_KEY")):
        return False, "bea_api_key_missing"
    if not _configured(environment.get("FRED_API_KEY")):
        return False, "fred_api_key_missing"
    return True, "bounded_shadow_probe_enabled"


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _parse_timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _reserve_sentinel(
    path: Path,
    reserved_at: str,
    *,
    request_budgets: Mapping[str, int] = REQUEST_BUDGETS,
    schema_version: str = "macro-event-clock-probe-sentinel-v1",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schemaVersion": schema_version,
                "status": "IN_PROGRESS",
                "reservedAt": reserved_at,
                "requestBudgets": dict(request_budgets),
            },
            handle,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


class _BoundedClient:
    def __init__(
        self,
        session: Any,
        budgets: Mapping[str, int] = REQUEST_BUDGETS,
    ) -> None:
        self.session = session
        self.budgets = dict(budgets)
        self.counts = {key: 0 for key in self.budgets}

    def request(
        self,
        counter: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[Any | None, bytes, dict[str, Any]]:
        if self.counts[counter] >= self.budgets[counter]:
            raise ValueError(f"request_budget_exceeded_{counter}")
        self.counts[counter] += 1
        try:
            response = self.session.request(
                method,
                url,
                timeout=(10, 25),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            return None, b"", {
                "httpStatusCategory": None,
                "safeErrorCategory": type(exc).__name__,
                "responseSha256": None,
                "redirected": False,
            }
        body = bytes(getattr(response, "content", b""))
        headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        status_code = int(getattr(response, "status_code", 0) or 0)
        return response, body, {
            "httpStatusCategory": (
                f"{status_code // 100}xx" if 100 <= status_code <= 599 else "invalid"
            ),
            "responseSha256": _sha256(body),
            "responseBytes": len(body),
            "httpDatePresent": bool(headers.get("date")),
            "lastModifiedPresent": bool(headers.get("last-modified")),
            "rateLimitHeaderPresent": any(
                key.startswith("x-ratelimit") or key == "retry-after"
                for key in headers
            ),
            "redirected": bool(
                getattr(response, "is_redirect", False)
                or getattr(response, "is_permanent_redirect", False)
            ),
        }


def _http_failure(
    source_id: str,
    evidence: Mapping[str, Any],
    status_code: int | None,
    *,
    credentialed: bool = False,
) -> dict[str, Any]:
    if status_code in {401, 403} and credentialed:
        status = "AUTH_OR_ENTITLEMENT_BLOCKED"
        blocker = f"{source_id}_AUTH_OR_ENTITLEMENT_BLOCKED"
    elif status_code in {401, 403}:
        status = "SOURCE_ACCESS_BLOCKED"
        blocker = f"{source_id}_ACCESS_POLICY_OR_NETWORK_BLOCKED"
    elif status_code == 429:
        status = "RATE_LIMITED"
        blocker = f"{source_id}_RATE_LIMITED"
    else:
        status = "SOURCE_HTTP_FAILURE"
        blocker = f"{source_id}_HTTP_FAILURE"
    return {
        "sourceId": source_id,
        "status": status,
        "primaryBlocker": blocker,
        "publicationEffectiveSeparated": True,
        "response": dict(evidence),
    }


def _parse_bls_ical(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    event_rows = text.count("BEGIN:VEVENT")
    dtstart_lines = re.findall(
        r"^DTSTART(?:;[^:]*)?:(\S+)\s*$",
        text,
        flags=re.MULTILINE,
    )
    explicit_timezone_rows = sum(line.endswith("Z") for line in dtstart_lines)
    explicit_timezone_rows += len(
        re.findall(r"^DTSTART;TZID=", text, flags=re.MULTILINE)
    )
    return {
        "valid": (
            text.startswith("BEGIN:VCALENDAR")
            and event_rows > 0
            and len(dtstart_lines) == event_rows
            and explicit_timezone_rows >= event_rows
        ),
        "eventRows": event_rows,
        "parseablePublicationRows": len(dtstart_lines),
        "explicitEasternTimeRows": explicit_timezone_rows,
    }


class _BlsScheduleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.tags: set[str] = set()
        self.text: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        self.tags.add(tag)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.text.append(cleaned)
            if self._cell is not None:
                self._cell.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _parse_bls_schedule_html(body: bytes) -> dict[str, Any]:
    parser = _BlsScheduleHtmlParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    eastern_notice = "eastern time" in " ".join(parser.text).lower()
    time_pattern = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.IGNORECASE)
    date_pattern = re.compile(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"[A-Za-z]+\s+\d{1,2},\s+\d{4}\b|"
        r"\b[A-Za-z]+\s+\d{1,2},\s+\d{4}\b",
        re.IGNORECASE,
    )
    event_rows = 0
    publication_times: list[dt.datetime] = []
    for cells in parser.rows:
        row_text = " ".join(cells)
        time_match = time_pattern.search(row_text)
        if time_match is None:
            continue
        event_rows += 1
        date_match = date_pattern.search(row_text)
        if date_match is None:
            continue
        date_text = date_match.group(0)
        value = f"{date_text} {time_match.group(0).upper()}"
        for date_format in ("%A, %B %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
            try:
                parsed = dt.datetime.strptime(value, date_format)
            except ValueError:
                continue
            publication_times.append(
                parsed.replace(tzinfo=ZoneInfo("America/New_York"))
            )
            break
    shape_keys = sorted(
        {f"tag:{tag}" for tag in parser.tags}
        | {f"rowCells:{len(row)}" for row in parser.rows}
    )
    explicit_eastern_rows = len(publication_times) if eastern_notice else 0
    valid = (
        event_rows > 0
        and len(publication_times) == event_rows
        and explicit_eastern_rows == event_rows
    )
    return {
        "valid": valid,
        "eventRows": event_rows,
        "parseablePublicationRows": len(publication_times),
        "explicitEasternTimeRows": explicit_eastern_rows,
        "publicationDateMin": (
            min(publication_times).isoformat() if publication_times else None
        ),
        "publicationDateMax": (
            max(publication_times).isoformat() if publication_times else None
        ),
        "shapeKeySetSha256": _sha256("\n".join(shape_keys)) if shape_keys else None,
    }


def _fed_calendar(client: _BoundedClient) -> dict[str, Any]:
    source_id = "FEDERAL_RESERVE_FOMC_CALENDAR"
    response, body, evidence = client.request(
        "federalReserveCalendar",
        "GET",
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    if response is None or response.status_code != 200 or evidence["redirected"]:
        return _http_failure(
            source_id,
            evidence,
            response.status_code if response is not None else None,
        )
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()
    month_rows = len(
        re.findall(
            r"\b(?:january|march|april|june|july|september|october|december)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    valid = (
        "federal open market committee" in lowered
        and "meeting calendar" in lowered
        and month_rows > 0
    )
    return {
        "sourceId": source_id,
        "status": "SOURCE_CAPABILITY_PASS" if valid else "SOURCE_CONTRACT_INVALID",
        "primaryBlocker": None if valid else "FEDERAL_RESERVE_CALENDAR_SHAPE_INVALID",
        "response": evidence,
        "calendarEventShapeRows": month_rows,
        "publicationClockStatus": "SOURCE_PAGE_PUBLICATION_TIME_NOT_MACHINE_READABLE",
        "effectiveClockStatus": "MEETING_DATE_RANGE_AVAILABLE",
        "timezoneStatus": "OFFICIAL_FOMC_CALENDAR_DATE_BASIS",
        "revisionPolicy": "CURRENT_OFFICIAL_CALENDAR_WITH_TENTATIVE_FUTURE_DATES",
        "publicationEffectiveSeparated": True,
    }


def _bls(client: _BoundedClient) -> dict[str, Any]:
    source_id = "BLS_RELEASE_CALENDAR_AND_DATA"
    calendar_response, calendar_body, calendar_evidence = client.request(
        "blsMetadata",
        "GET",
        "https://www.bls.gov/schedule/news_release/bls.ics",
        headers={"User-Agent": USER_AGENT, "Accept": "text/calendar"},
    )
    calendar_failure = None
    event_rows = explicit_timezone_rows = 0
    calendar_valid = False
    if (
        calendar_response is None
        or calendar_response.status_code != 200
        or calendar_evidence["redirected"]
    ):
        calendar_failure = _http_failure(
            source_id,
            calendar_evidence,
            calendar_response.status_code if calendar_response is not None else None,
        )
    else:
        calendar_contract = _parse_bls_ical(calendar_body)
        event_rows = calendar_contract["eventRows"]
        explicit_timezone_rows = calendar_contract["explicitEasternTimeRows"]
        calendar_valid = calendar_contract["valid"]

    data_response, _, data_evidence = client.request(
        "blsData",
        "GET",
        f"https://api.bls.gov/publicAPI/v2/timeseries/data/{BLS_CAPABILITY_SERIES_ID}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if data_response is None or data_response.status_code != 200 or data_evidence["redirected"]:
        data_failure = _http_failure(
            source_id,
            data_evidence,
            data_response.status_code if data_response is not None else None,
        )
        return {
            **(calendar_failure or data_failure),
            "metadataResponse": calendar_evidence,
            "dataResponse": data_evidence,
            "dataContractStatus": "FAIL",
        }
    try:
        payload = data_response.json()
    except ValueError:
        payload = None
    series = (
        ((payload.get("Results") or {}).get("series") or [])
        if isinstance(payload, dict)
        else []
    )
    row = series[0] if len(series) == 1 and isinstance(series[0], dict) else {}
    observations = row.get("data") if isinstance(row.get("data"), list) else []
    effective_period_rows = sum(
        isinstance(item, dict) and bool(item.get("year")) and bool(item.get("period"))
        for item in observations
    )
    coded_footnote_rows = sum(
        isinstance(item, dict)
        and any(
            isinstance(note, dict) and bool(note.get("code"))
            for note in (item.get("footnotes") or [])
        )
        for item in observations
    )
    data_valid = (
        isinstance(payload, dict)
        and payload.get("status") == "REQUEST_SUCCEEDED"
        and len(series) == 1
        and bool(str(row.get("seriesID") or ""))
        and bool(observations)
        and effective_period_rows == len(observations)
    )
    if calendar_failure is not None:
        return {
            **calendar_failure,
            "metadataResponse": calendar_evidence,
            "dataResponse": data_evidence,
            "dataContractStatus": "PASS" if data_valid else "FAIL",
            "observationRows": len(observations),
            "effectivePeriodRows": effective_period_rows,
            "publicationEffectiveSeparated": True,
        }
    valid = calendar_valid and data_valid
    return {
        "sourceId": source_id,
        "status": "SOURCE_CAPABILITY_PASS" if valid else "SOURCE_CONTRACT_INVALID",
        "primaryBlocker": None if valid else "BLS_CALENDAR_OR_DATA_SHAPE_INVALID",
        "metadataResponse": calendar_evidence,
        "dataResponse": data_evidence,
        "dataContractStatus": "PASS" if data_valid else "FAIL",
        "calendarEventRows": event_rows,
        "explicitTimezoneRows": explicit_timezone_rows,
        "seriesIdSha256": _sha256(str(row.get("seriesID") or "")) if row else None,
        "observationRows": len(observations),
        "effectivePeriodRows": effective_period_rows,
        "codedFootnoteRows": coded_footnote_rows,
        "publicationClockStatus": "SCHEDULED_PUBLICATION_TIMESTAMP_AVAILABLE",
        "effectiveClockStatus": "OBSERVATION_PERIOD_AVAILABLE_SEPARATELY",
        "timezoneStatus": "EXPLICIT_ICS_TIMEZONE",
        "revisionPolicy": "FOOTNOTE_REVISION_STATUS_PRESERVED_AS_AGGREGATE",
        "publicationEffectiveSeparated": True,
    }


def _bea(client: _BoundedClient, api_key: str) -> dict[str, Any]:
    source_id = "BEA_RELEASE_SCHEDULE_AND_DATASET_METADATA"
    schedule_response, schedule_body, schedule_evidence = client.request(
        "beaMetadata",
        "GET",
        "https://www.bea.gov/news/schedule/",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    if (
        schedule_response is None
        or schedule_response.status_code != 200
        or schedule_evidence["redirected"]
    ):
        return _http_failure(
            source_id,
            schedule_evidence,
            schedule_response.status_code if schedule_response is not None else None,
        )
    schedule_text = schedule_body.decode("utf-8", errors="replace")
    schedule_rows = len(
        re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", schedule_text, re.IGNORECASE)
    )
    schedule_valid = "release schedule" in schedule_text.lower() and schedule_rows > 0

    data_response, _, data_evidence = client.request(
        "beaData",
        "GET",
        "https://apps.bea.gov/api/data",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        params={
            "UserID": api_key,
            "method": "GetDatasetList",
            "ResultFormat": "JSON",
        },
    )
    if data_response is None or data_response.status_code != 200 or data_evidence["redirected"]:
        return _http_failure(
            source_id,
            data_evidence,
            data_response.status_code if data_response is not None else None,
            credentialed=True,
        )
    try:
        payload = data_response.json()
    except ValueError:
        payload = None
    api_root = payload.get("BEAAPI") if isinstance(payload, dict) else None
    results = api_root.get("Results") if isinstance(api_root, dict) else None
    api_error = (
        api_root.get("Error")
        or (results.get("Error") if isinstance(results, dict) else None)
        if isinstance(api_root, dict)
        else None
    )
    datasets = results.get("Dataset") if isinstance(results, dict) else None
    dataset_rows = datasets if isinstance(datasets, list) else []
    api_valid = bool(dataset_rows) and all(
        isinstance(row, dict) and bool(row.get("DatasetName")) for row in dataset_rows
    )
    if api_error:
        error_keys = sorted(str(key) for key in api_error) if isinstance(api_error, dict) else []
        error_code = str(api_error.get("APIErrorCode") or "") if isinstance(api_error, dict) else ""
        user_id_rejected = error_code == "1"
        return {
            "sourceId": source_id,
            "status": (
                "AUTH_OR_ENTITLEMENT_BLOCKED"
                if user_id_rejected
                else "SOURCE_CONTRACT_INVALID"
            ),
            "primaryBlocker": (
                "BEA_API_USER_ID_REJECTED"
                if user_id_rejected
                else "BEA_API_ERROR_SUBTYPE_UNVERIFIED"
            ),
            "metadataResponse": schedule_evidence,
            "dataResponse": data_evidence,
            "scheduledPublicationRows": schedule_rows,
            "apiErrorPresent": True,
            "apiErrorCodeSha256": _sha256(error_code) if error_code else None,
            "apiErrorKeySetSha256": _sha256("\n".join(error_keys)) if error_keys else None,
            "publicationEffectiveSeparated": True,
        }
    valid = schedule_valid and api_valid
    key_scope = sorted(
        {str(key) for row in dataset_rows if isinstance(row, dict) for key in row}
    )
    return {
        "sourceId": source_id,
        "status": "SOURCE_CAPABILITY_PASS" if valid else "SOURCE_CONTRACT_INVALID",
        "primaryBlocker": None if valid else "BEA_SCHEDULE_OR_API_SHAPE_INVALID",
        "metadataResponse": schedule_evidence,
        "dataResponse": data_evidence,
        "scheduledPublicationRows": schedule_rows,
        "datasetRows": len(dataset_rows),
        "datasetKeySetSha256": _sha256("\n".join(key_scope)) if key_scope else None,
        "publicationClockStatus": "OFFICIAL_SCHEDULE_DATE_TIME_AVAILABLE",
        "effectiveClockStatus": "DATASET_METADATA_HAS_NO_EVENT_EFFECTIVE_TIMESTAMP",
        "timezoneStatus": "OFFICIAL_RELEASE_POLICY_AMERICA_NEW_YORK",
        "revisionPolicy": "CURRENT_RELEASE_SCHEDULE_MAY_BE_AMENDED",
        "publicationEffectiveSeparated": True,
    }


def _fred(client: _BoundedClient, api_key: str) -> dict[str, Any]:
    source_id = "FRED_RELEASE_METADATA_AND_SOURCE_DATES"
    common = {"api_key": api_key, "file_type": "json", "limit": "1"}
    metadata_response, _, metadata_evidence = client.request(
        "fredMetadata",
        "GET",
        "https://api.stlouisfed.org/fred/releases",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        params={**common, "order_by": "release_id", "sort_order": "asc"},
    )
    if (
        metadata_response is None
        or metadata_response.status_code != 200
        or metadata_evidence["redirected"]
    ):
        status_code = metadata_response.status_code if metadata_response is not None else None
        return _http_failure(
            source_id,
            metadata_evidence,
            403 if status_code == 400 else status_code,
            credentialed=True,
        )
    try:
        metadata_payload = metadata_response.json()
    except ValueError:
        metadata_payload = None
    releases = (
        metadata_payload.get("releases")
        if isinstance(metadata_payload, dict)
        and isinstance(metadata_payload.get("releases"), list)
        else []
    )
    metadata_valid = bool(releases) and all(
        isinstance(row, dict) and row.get("id") is not None and bool(row.get("name"))
        for row in releases
    )

    data_response, _, data_evidence = client.request(
        "fredData",
        "GET",
        "https://api.stlouisfed.org/fred/releases/dates",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        params={
            **common,
            "include_release_dates_with_no_data": "true",
            "sort_order": "desc",
        },
    )
    if data_response is None or data_response.status_code != 200 or data_evidence["redirected"]:
        return _http_failure(
            source_id,
            data_evidence,
            data_response.status_code if data_response is not None else None,
            credentialed=True,
        )
    try:
        data_payload = data_response.json()
    except ValueError:
        data_payload = None
    release_dates = (
        data_payload.get("release_dates")
        if isinstance(data_payload, dict)
        and isinstance(data_payload.get("release_dates"), list)
        else []
    )
    parseable_date_rows = sum(
        isinstance(row, dict)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date") or ""))
        is not None
        for row in release_dates
    )
    data_valid = bool(release_dates) and all(
        isinstance(row, dict)
        and row.get("release_id") is not None
        and bool(row.get("release_name"))
        for row in release_dates
    ) and parseable_date_rows == len(release_dates)
    valid = metadata_valid and data_valid
    metadata_keys = sorted(
        {str(key) for row in releases if isinstance(row, dict) for key in row}
    )
    data_keys = sorted(
        {str(key) for row in release_dates if isinstance(row, dict) for key in row}
    )
    return {
        "sourceId": source_id,
        "status": "SOURCE_CAPABILITY_PASS" if valid else "SOURCE_CONTRACT_INVALID",
        "primaryBlocker": None if valid else "FRED_RELEASE_CONTRACT_INVALID",
        "metadataResponse": metadata_evidence,
        "dataResponse": data_evidence,
        "releaseMetadataRows": len(releases),
        "releaseDateRows": len(release_dates),
        "parseableReleaseDateRows": parseable_date_rows,
        "metadataKeySetSha256": _sha256("\n".join(metadata_keys)) if metadata_keys else None,
        "releaseDateKeySetSha256": _sha256("\n".join(data_keys)) if data_keys else None,
        "publicationClockStatus": "SOURCE_PUBLISHED_RELEASE_DATE_NOT_FRED_AVAILABILITY",
        "effectiveClockStatus": "OBSERVATION_EFFECTIVE_PERIOD_NOT_PRESENT_IN_RELEASE_DATE_FEED",
        "timezoneStatus": "DATE_ONLY_SOURCE_CONTRACT",
        "revisionPolicy": "FRED_REALTIME_PERIOD_METADATA_NOT_AVAILABILITY_TIMESTAMP",
        "publicationEffectiveSeparated": True,
    }


def _overall_status(sources: list[Mapping[str, Any]]) -> tuple[str, str | None]:
    precedence = (
        ("AUTH_OR_ENTITLEMENT_BLOCKED", "MACRO_EVENT_CLOCK_AUTH_OR_ENTITLEMENT_BLOCKED"),
        ("SOURCE_ACCESS_BLOCKED", "MACRO_EVENT_CLOCK_SOURCE_ACCESS_BLOCKED"),
        ("RATE_LIMITED", "MACRO_EVENT_CLOCK_RATE_LIMITED"),
        ("SOURCE_HTTP_FAILURE", "MACRO_EVENT_CLOCK_TRANSIENT_FAILURE"),
        ("SOURCE_CONTRACT_INVALID", "MACRO_EVENT_CLOCK_RESPONSE_CONTRACT_INVALID"),
    )
    for source_status, overall_status in precedence:
        for source in sources:
            if source.get("status") == source_status:
                return overall_status, str(source.get("primaryBlocker") or source_status)
    return PASS_STATUS, None


def _bls_blocker_status(
    status_code: int | None,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if status_code in {401, 403}:
        status = "BLS_CALENDAR_SOURCE_ACCESS_BLOCKED"
    elif status_code == 429:
        status = "BLS_CALENDAR_RATE_LIMITED"
    elif status_code is None or status_code >= 500:
        status = "BLS_CALENDAR_TRANSIENT_FAILURE"
    else:
        status = "BLS_CALENDAR_CONTRACT_INVALID"
    return {
        "sourceId": "BLS_RELEASE_CALENDAR",
        "status": status,
        "primaryBlocker": status,
        "publicationEffectiveSeparated": True,
        "response": dict(evidence),
    }


def _bls_calendar_blocker(
    client: _BoundedClient,
    retrieved_at: str,
) -> dict[str, Any]:
    response, body, evidence = client.request(
        "blsIcal",
        "GET",
        "https://www.bls.gov/schedule/news_release/bls.ics",
        headers={"User-Agent": USER_AGENT, "Accept": "text/calendar"},
    )
    status_code = response.status_code if response is not None else None
    if (
        response is not None
        and status_code == 200
        and not evidence["redirected"]
    ):
        contract = _parse_bls_ical(body)
        return {
            "sourceId": "BLS_RELEASE_CALENDAR",
            "status": (
                "BLS_CALENDAR_ICAL_PASS"
                if contract["valid"]
                else "BLS_CALENDAR_CONTRACT_INVALID"
            ),
            "primaryBlocker": (
                None if contract["valid"] else "BLS_CALENDAR_ICAL_CONTRACT_INVALID"
            ),
            "eventRows": contract["eventRows"],
            "parseablePublicationRows": contract["parseablePublicationRows"],
            "explicitEasternTimeRows": contract["explicitEasternTimeRows"],
            "responseSha256": evidence["responseSha256"],
            "publicationClockStatus": "SCHEDULED_PUBLICATION_TIMESTAMP_AVAILABLE",
            "effectiveClockStatus": "OBSERVATION_PERIOD_NOT_PRESENT_IN_CALENDAR",
            "timezoneStatus": "EXPLICIT_ICS_TIMEZONE",
            "publicationEffectiveSeparated": True,
            "response": evidence,
        }
    if status_code not in {401, 403} or evidence["redirected"]:
        return _bls_blocker_status(status_code, evidence)

    parsed_retrieved_at = _parse_timestamp(retrieved_at)
    if parsed_retrieved_at is None:
        raise ValueError("invalid_retrieved_at")
    eastern = parsed_retrieved_at.astimezone(ZoneInfo("America/New_York"))
    fallback_url = (
        f"https://www.bls.gov/schedule/{eastern.year}/"
        f"{eastern.month:02d}_sched_list.htm"
    )
    fallback_response, fallback_body, fallback_evidence = client.request(
        "blsHtmlFallback",
        "GET",
        fallback_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    fallback_status = (
        fallback_response.status_code if fallback_response is not None else None
    )
    if (
        fallback_response is None
        or fallback_status != 200
        or fallback_evidence["redirected"]
    ):
        return _bls_blocker_status(fallback_status, fallback_evidence)
    contract = _parse_bls_schedule_html(fallback_body)
    return {
        "sourceId": "BLS_RELEASE_CALENDAR",
        "status": (
            "BLS_CALENDAR_HTML_FALLBACK_PASS"
            if contract["valid"]
            else "BLS_CALENDAR_CONTRACT_INVALID"
        ),
        "primaryBlocker": (
            None
            if contract["valid"]
            else "BLS_CALENDAR_HTML_CONTRACT_INVALID"
        ),
        "eventRows": contract["eventRows"],
        "parseablePublicationRows": contract["parseablePublicationRows"],
        "explicitEasternTimeRows": contract["explicitEasternTimeRows"],
        "publicationDateMin": contract["publicationDateMin"],
        "publicationDateMax": contract["publicationDateMax"],
        "responseSha256": fallback_evidence["responseSha256"],
        "shapeKeySetSha256": contract["shapeKeySetSha256"],
        "publicationClockStatus": "SCHEDULED_PUBLICATION_TIMESTAMP_AVAILABLE",
        "effectiveClockStatus": "OBSERVATION_PERIOD_NOT_PRESENT_IN_CALENDAR",
        "timezoneStatus": "AMERICA_NEW_YORK_FROM_OFFICIAL_EASTERN_TIME_NOTICE",
        "publicationEffectiveSeparated": True,
        "response": fallback_evidence,
    }


def _blocker_reproof_result(
    *,
    retrieved_at: str,
    client: _BoundedClient,
    status: str,
    primary_blocker: str | None,
    sources: list[Mapping[str, Any]],
) -> dict[str, Any]:
    result = {
        "schemaVersion": BLOCKER_ONLY_SCHEMA_VERSION,
        "status": status,
        "primaryBlocker": primary_blocker,
        "mode": "SHADOW_ONLY_BLOCKER_REPROOF",
        "preservedBaselineRunId": BLOCKER_ONLY_BASELINE_RUN_ID,
        "retrievedAt": retrieved_at,
        "requestBudgets": BLOCKER_ONLY_REQUEST_BUDGETS,
        "requestCounts": dict(client.counts),
        "externalRequestCount": sum(client.counts.values()),
        "requestBudgetCompliant": all(
            client.counts[key] <= BLOCKER_ONLY_REQUEST_BUDGETS[key]
            for key in BLOCKER_ONLY_REQUEST_BUDGETS
        ),
        "sources": [dict(source) for source in sources],
        "publicationEffectiveTimestampSeparated": all(
            source.get("publicationEffectiveSeparated") is True for source in sources
        ),
        "lookAheadViolationRows": 0,
        "unknownOrUnclassifiedRows": 0,
        "analysisEligible": False,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "rawResponseStored": False,
        "secretValuesStoredOrPrinted": False,
        "paginationUsed": False,
        "retryCount": 0,
        "recurringProviderEnabled": False,
        "accountHeaderUsed": False,
        "accountEndpointUsed": False,
        "orderEndpointUsed": False,
        "brokerOrSidecarStateMutation": False,
    }
    result["evidenceSha256"] = _canonical_sha256(result)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    return result


def collect_macro_event_clock_blocker_reproof(
    *,
    session: Any,
    environment: Mapping[str, Any],
    retrieved_at: str,
    bea_activation_confirmed: bool,
) -> dict[str, Any]:
    if _parse_timestamp(retrieved_at) is None:
        raise ValueError("invalid_retrieved_at")
    client = _BoundedClient(session, BLOCKER_ONLY_REQUEST_BUDGETS)
    if not bea_activation_confirmed or not _configured(environment.get("BEA_API_KEY")):
        return _blocker_reproof_result(
            retrieved_at=retrieved_at,
            client=client,
            status="BEA_KEY_ACTIVATION_REQUIRED",
            primary_blocker="BEA_KEY_ACTIVATION_REQUIRED",
            sources=[],
        )
    sources = [
        _bls_calendar_blocker(client, retrieved_at),
        _bea(client, str(environment["BEA_API_KEY"])),
    ]
    passing_bls = sources[0]["status"] in {
        "BLS_CALENDAR_ICAL_PASS",
        "BLS_CALENDAR_HTML_FALLBACK_PASS",
    }
    passing_bea = sources[1]["status"] == "SOURCE_CAPABILITY_PASS"
    primary_blocker = next(
        (
            str(source.get("primaryBlocker"))
            for source in sources
            if source.get("primaryBlocker")
        ),
        None,
    )
    return _blocker_reproof_result(
        retrieved_at=retrieved_at,
        client=client,
        status=(
            BLOCKER_ONLY_PASS_STATUS
            if passing_bls and passing_bea
            else "MACRO_BLOCKER_ONLY_REPROOF_SOURCE_BLOCKED"
        ),
        primary_blocker=primary_blocker,
        sources=sources,
    )


def _bls_registered_result(
    *,
    retrieved_at: str,
    client: _BoundedClient,
    status: str,
    primary_blocker: str | None,
    evidence: Mapping[str, Any] | None = None,
    series_rows: int = 0,
    observation_rows: int = 0,
    catalog_rows: int = 0,
    effective_period_rows: int = 0,
    registration_key_used: bool = False,
) -> dict[str, Any]:
    response = dict(evidence or {})
    result = {
        "schemaVersion": BLS_REGISTERED_SCHEMA_VERSION,
        "status": status,
        "primaryBlocker": primary_blocker,
        "mode": "SHADOW_ONLY_REGISTERED_DATA_PROBE",
        "retrievedAt": retrieved_at,
        "requestBudgets": BLS_REGISTERED_REQUEST_BUDGETS,
        "requestCounts": dict(client.counts),
        "externalRequestCount": sum(client.counts.values()),
        "requestBudgetCompliant": all(
            client.counts[key] <= BLS_REGISTERED_REQUEST_BUDGETS[key]
            for key in BLS_REGISTERED_REQUEST_BUDGETS
        ),
        "httpStatusCategory": response.get("httpStatusCategory"),
        "responseSha256": response.get("responseSha256"),
        "seriesRows": series_rows,
        "observationRows": observation_rows,
        "catalogPresentRows": catalog_rows,
        "effectivePeriodRows": effective_period_rows,
        "publicationTimestampAvailable": False,
        "registrationKeyUsed": registration_key_used,
        "calendarStatus": "BLS_CALENDAR_SOURCE_ACCESS_BLOCKED",
        "unknownOrUnclassifiedRows": 0,
        "analysisEligible": False,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "rawResponseStored": False,
        "secretStoredOrPrinted": False,
        "paginationUsed": False,
        "retryCount": 0,
        "recurringProviderEnabled": False,
        "accountHeaderUsed": False,
        "accountEndpointUsed": False,
        "orderEndpointUsed": False,
        "brokerOrSidecarStateMutation": False,
    }
    result["evidenceSha256"] = _canonical_sha256(result)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    return result


def collect_bls_registered_data_capability(
    *,
    session: Any,
    environment: Mapping[str, Any],
    retrieved_at: str,
) -> dict[str, Any]:
    if _parse_timestamp(retrieved_at) is None:
        raise ValueError("invalid_retrieved_at")
    client = _BoundedClient(session, BLS_REGISTERED_REQUEST_BUDGETS)
    if not _configured(environment.get("BLS_API_KEY")):
        return _bls_registered_result(
            retrieved_at=retrieved_at,
            client=client,
            status="BLS_REGISTRATION_KEY_NOT_VISIBLE_LOCALLY",
            primary_blocker="BLS_REGISTRATION_KEY_NOT_VISIBLE_LOCALLY",
        )
    response, _, evidence = client.request(
        "blsRegisteredData",
        "POST",
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        json={
            "seriesid": [BLS_CAPABILITY_SERIES_ID],
            "catalog": True,
            "registrationkey": str(environment["BLS_API_KEY"]),
        },
    )
    status_code = response.status_code if response is not None else None
    if status_code in {401, 403}:
        status = "BLS_AUTH_OR_REGISTRATION_BLOCKED"
    elif status_code == 429:
        status = "BLS_REGISTERED_API_RATE_LIMITED"
    elif response is None or (status_code is not None and status_code >= 500):
        status = "BLS_REGISTERED_API_TRANSIENT_FAILURE"
    elif status_code is None or not 200 <= status_code < 300 or evidence["redirected"]:
        status = "BLS_REGISTERED_API_SCHEMA_INVALID"
    else:
        status = ""
    if status:
        return _bls_registered_result(
            retrieved_at=retrieved_at,
            client=client,
            status=status,
            primary_blocker=status,
            evidence=evidence,
            registration_key_used=True,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    results = payload.get("Results") if isinstance(payload, dict) else None
    series = results.get("series") if isinstance(results, dict) else None
    rows = series if isinstance(series, list) else []
    matching_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("seriesID") == BLS_CAPABILITY_SERIES_ID
    ]
    observations = [
        item
        for row in matching_rows
        for item in (row.get("data") if isinstance(row.get("data"), list) else [])
        if isinstance(item, dict)
    ]
    catalog_rows = sum(
        isinstance(row.get("catalog"), dict) and bool(row["catalog"])
        for row in matching_rows
    )
    effective_period_rows = sum(
        bool(item.get("year")) and bool(item.get("period")) for item in observations
    )
    valid = (
        isinstance(payload, dict)
        and payload.get("status") == "REQUEST_SUCCEEDED"
        and len(matching_rows) == 1
        and catalog_rows == 1
        and bool(observations)
        and effective_period_rows == len(observations)
    )
    status = (
        BLS_REGISTERED_PASS_STATUS
        if valid
        else "BLS_REGISTERED_API_SCHEMA_INVALID"
    )
    return _bls_registered_result(
        retrieved_at=retrieved_at,
        client=client,
        status=status,
        primary_blocker=None if valid else status,
        evidence=evidence,
        series_rows=len(matching_rows),
        observation_rows=len(observations),
        catalog_rows=catalog_rows,
        effective_period_rows=effective_period_rows,
        registration_key_used=True,
    )


def collect_macro_event_clock_capability(
    *,
    session: Any,
    environment: Mapping[str, Any],
    retrieved_at: str,
) -> dict[str, Any]:
    if _parse_timestamp(retrieved_at) is None:
        raise ValueError("invalid_retrieved_at")
    enabled, reason = macro_event_clock_runtime_decision(environment)
    if not enabled:
        raise ValueError(reason)
    client = _BoundedClient(session)
    sources = [
        _fed_calendar(client),
        _bls(client),
        _bea(client, str(environment["BEA_API_KEY"])),
        _fred(client, str(environment["FRED_API_KEY"])),
    ]
    status, blocker = _overall_status(sources)
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "status": status,
        "primaryBlocker": blocker,
        "mode": "SHADOW_ONLY",
        "probeMode": "BOUNDED_MANUAL_READ_ONLY",
        "retrievedAt": retrieved_at,
        "requestBudgets": REQUEST_BUDGETS,
        "requestCounts": dict(client.counts),
        "externalRequestCount": sum(client.counts.values()),
        "requestBudgetCompliant": all(
            client.counts[key] <= REQUEST_BUDGETS[key] for key in REQUEST_BUDGETS
        ),
        "sources": sources,
        "publicationEffectiveTimestampSeparated": all(
            source.get("publicationEffectiveSeparated") is True for source in sources
        ),
        "lookAheadViolationRows": 0,
        "unknownOrUnclassifiedRows": 0,
        "analysisEligible": False,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "rawResponseStored": False,
        "secretValuesStoredOrPrinted": False,
        "paginationUsed": False,
        "retryCount": 0,
        "recurringProviderEnabled": False,
        "accountHeaderUsed": False,
        "accountEndpointUsed": False,
        "orderEndpointUsed": False,
        "brokerOrSidecarStateMutation": False,
    }
    result["evidenceSha256"] = _canonical_sha256(result)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    return result


def run_macro_event_clock_capability_probe(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_path: Path,
    retrieved_at: str,
) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(output_path)
    _reserve_sentinel(sentinel_path, retrieved_at)
    try:
        result = collect_macro_event_clock_capability(
            session=session,
            environment=environment,
            retrieved_at=retrieved_at,
        )
    except Exception as exc:
        _atomic_write_json(
            sentinel_path,
            {
                "schemaVersion": "macro-event-clock-probe-sentinel-v1",
                "status": "FAILED",
                "reservedAt": retrieved_at,
                "completedAt": _utc_now(),
                "safeErrorCategory": type(exc).__name__,
            },
        )
        raise
    _atomic_write_json(output_path, result)
    _atomic_write_json(
        sentinel_path,
        {
            "schemaVersion": "macro-event-clock-probe-sentinel-v1",
            "status": "COMPLETE",
            "reservedAt": retrieved_at,
            "completedAt": _utc_now(),
            "requestCounts": result["requestCounts"],
            "resultSha256": _sha256(output_path.read_bytes()),
        },
    )
    return result


def run_macro_event_clock_blocker_reproof(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_path: Path,
    retrieved_at: str,
    approval: str,
    bea_activation_confirmed: bool,
) -> dict[str, Any]:
    if approval != BLOCKER_ONLY_APPROVAL:
        raise ValueError("blocker_only_approval_required")
    if output_path.exists():
        raise FileExistsError(output_path)
    sentinel_schema = "macro-event-clock-blocker-reproof-sentinel-v1"
    _reserve_sentinel(
        sentinel_path,
        retrieved_at,
        request_budgets=BLOCKER_ONLY_REQUEST_BUDGETS,
        schema_version=sentinel_schema,
    )
    try:
        result = collect_macro_event_clock_blocker_reproof(
            session=session,
            environment=environment,
            retrieved_at=retrieved_at,
            bea_activation_confirmed=bea_activation_confirmed,
        )
    except Exception as exc:
        _atomic_write_json(
            sentinel_path,
            {
                "schemaVersion": sentinel_schema,
                "status": "FAILED",
                "reservedAt": retrieved_at,
                "completedAt": _utc_now(),
                "safeErrorCategory": type(exc).__name__,
            },
        )
        raise
    _atomic_write_json(output_path, result)
    _atomic_write_json(
        sentinel_path,
        {
            "schemaVersion": sentinel_schema,
            "status": "COMPLETE",
            "reservedAt": retrieved_at,
            "completedAt": _utc_now(),
            "requestCounts": result["requestCounts"],
            "resultSha256": _sha256(output_path.read_bytes()),
        },
    )
    return result


def run_bls_registered_data_capability_probe(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_path: Path,
    retrieved_at: str,
    approval: str,
) -> dict[str, Any]:
    if approval != BLS_REGISTERED_APPROVAL:
        raise ValueError("bls_registered_approval_required")
    if output_path.exists():
        raise FileExistsError(output_path)
    sentinel_schema = "bls-registered-data-capability-sentinel-v1"
    _reserve_sentinel(
        sentinel_path,
        retrieved_at,
        request_budgets=BLS_REGISTERED_REQUEST_BUDGETS,
        schema_version=sentinel_schema,
    )
    try:
        result = collect_bls_registered_data_capability(
            session=session,
            environment=environment,
            retrieved_at=retrieved_at,
        )
    except Exception as exc:
        _atomic_write_json(
            sentinel_path,
            {
                "schemaVersion": sentinel_schema,
                "status": "FAILED",
                "reservedAt": retrieved_at,
                "completedAt": _utc_now(),
                "safeErrorCategory": type(exc).__name__,
            },
        )
        raise
    _atomic_write_json(output_path, result)
    _atomic_write_json(
        sentinel_path,
        {
            "schemaVersion": sentinel_schema,
            "status": "COMPLETE",
            "reservedAt": retrieved_at,
            "completedAt": _utc_now(),
            "requestCounts": result["requestCounts"],
            "resultSha256": _sha256(output_path.read_bytes()),
        },
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sentinel", type=Path, required=True)
    parser.add_argument("--blocker-only", action="store_true")
    parser.add_argument("--bls-registered-only", action="store_true")
    parser.add_argument("--approval", default="")
    parser.add_argument("--bea-activation-confirmed", action="store_true")
    args = parser.parse_args()
    if args.blocker_only and args.bls_registered_only:
        parser.error("probe modes are mutually exclusive")
    if args.bls_registered_only:
        result = run_bls_registered_data_capability_probe(
            session=requests.Session(),
            environment=os.environ,
            output_path=args.output,
            sentinel_path=args.sentinel,
            retrieved_at=_utc_now(),
            approval=args.approval,
        )
        print(
            "[BLS_REGISTERED_DATA_CAPABILITY] "
            f"status={result['status']} requests={result['externalRequestCount']} "
            f"unknown={result['unknownOrUnclassifiedRows']} rawStored=false"
        )
        return 0 if result["status"] == BLS_REGISTERED_PASS_STATUS else 2
    if args.blocker_only:
        result = run_macro_event_clock_blocker_reproof(
            session=requests.Session(),
            environment=os.environ,
            output_path=args.output,
            sentinel_path=args.sentinel,
            retrieved_at=_utc_now(),
            approval=args.approval,
            bea_activation_confirmed=args.bea_activation_confirmed,
        )
        print(
            "[MACRO_BLOCKER_REPROOF] "
            f"status={result['status']} requests={result['externalRequestCount']} "
            f"unknown={result['unknownOrUnclassifiedRows']} rawStored=false"
        )
        return 0 if result["status"] == BLOCKER_ONLY_PASS_STATUS else 2
    result = run_macro_event_clock_capability_probe(
        session=requests.Session(),
        environment=os.environ,
        output_path=args.output,
        sentinel_path=args.sentinel,
        retrieved_at=_utc_now(),
    )
    print(
        "[MACRO_EVENT_CLOCK_CAPABILITY] "
        f"status={result['status']} requests={result['externalRequestCount']} "
        f"unknown={result['unknownOrUnclassifiedRows']} rawStored=false"
    )
    return 0 if result["status"] == PASS_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
