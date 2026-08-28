from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests


SCHEMA_VERSION = "sec-finra-shadow-evidence-v1"
PASS_STATUS = "SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"
PARTIAL_STATUS = "SEC_FINRA_SHADOW_PARTIAL"
SCHEDULE13_TARGETED_SCHEMA_VERSION = "sec-schedule13-exact-family-reproof-v1"
SCHEDULE13_SOURCE_ID = "SEC_SCHEDULES_13D_13G"
SCHEDULE13_ALLOWED_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
SCHEDULE13_DISCOVERY_COUNT = 40
SCHEDULE13_PASS_STATUSES = {
    "SEC_SCHEDULE13_EXACT_FAMILY_PASS",
    "SEC_SCHEDULE13_NO_CURRENT_ALLOWED_FORM_PASS",
}
REQUEST_BUDGETS = {
    "secDiscovery": 3,
    "secSubmissions": 3,
    "secRawFiling": 3,
    "finraOauth": 1,
    "finraMetadata": 1,
    "finraData": 3,
}
SEC_FAMILIES = (
    (
        "SEC_SECTION16_FORMS_3_4_5",
        "INSIDER_OWNERSHIP_TRANSACTION",
        "4",
        "only",
        {"3", "3/A", "4", "4/A", "5", "5/A"},
        1,
    ),
    (
        "SEC_SCHEDULES_13D_13G",
        "BENEFICIAL_OWNERSHIP_POSITION_DISCLOSURE",
        "SC 13",
        "include",
        SCHEDULE13_ALLOWED_FORMS,
        SCHEDULE13_DISCOVERY_COUNT,
    ),
    (
        "SEC_FORM_13F",
        "INSTITUTIONAL_HOLDINGS_SNAPSHOT",
        "13F-HR",
        "include",
        {"13F-HR", "13F-HR/A"},
        1,
    ),
)
FINRA_DATASETS = (
    (
        "FINRA_CONSOLIDATED_SHORT_INTEREST",
        "consolidatedShortInterest",
        "SHORT_INTEREST_POSITION_SNAPSHOT",
        {"settlementDate", "currentShortPositionQuantity", "symbolCode"},
        "symbolCode",
        "settlementDate",
    ),
    (
        "FINRA_REG_SHO_DAILY_SHORT_VOLUME",
        "regShoDaily",
        "SHORT_SALE_ACTIVITY_VOLUME",
        {
            "tradeReportDate",
            "shortParQuantity",
            "totalParQuantity",
            "securitiesInformationProcessorSymbolIdentifier",
        },
        "securitiesInformationProcessorSymbolIdentifier",
        "tradeReportDate",
    ),
    (
        "FINRA_OTC_THRESHOLD_LIST",
        "thresholdList",
        "REGULATORY_THRESHOLD_STATUS_OTC_ONLY",
        {
            "tradeDate",
            "regShoThresholdFlag",
            "thresholdListFlag",
            "issueSymbolIdentifier",
        },
        "issueSymbolIdentifier",
        "tradeDate",
    ),
)
SOURCE_IDS = tuple(contract[0] for contract in SEC_FAMILIES) + tuple(
    contract[0] for contract in FINRA_DATASETS
)
SOURCE_WINDOW_BASES = {
    "SEC_SECTION16_FORMS_3_4_5": "SEC_CURRENT_FILINGS_PUBLICATION_DATE_ET",
    "SEC_SCHEDULES_13D_13G": "SEC_CURRENT_FILINGS_PUBLICATION_DATE_ET",
    "SEC_FORM_13F": "SEC_CURRENT_FILINGS_PUBLICATION_DATE_ET",
    "FINRA_CONSOLIDATED_SHORT_INTEREST": (
        "FINRA_SHORT_INTEREST_PUBLICATION_OBSERVATION_DATE_ET"
    ),
    "FINRA_REG_SHO_DAILY_SHORT_VOLUME": (
        "FINRA_DAILY_DATASET_PUBLICATION_OBSERVATION_DATE_ET"
    ),
    "FINRA_OTC_THRESHOLD_LIST": (
        "FINRA_DAILY_DATASET_PUBLICATION_OBSERVATION_DATE_ET"
    ),
}


def _configured(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and not any(
        marker in text
        for marker in ("replace", "placeholder", "발급받은", "실제_연락")
    )


def sec_finra_shadow_runtime_decision(
    environment: Mapping[str, Any],
) -> tuple[bool, str]:
    enabled = str(environment.get("SEC_FINRA_SHADOW_PROVIDER_ENABLED") or "").lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return False, "shadow_provider_disabled"
    if "@" not in str(environment.get("SEC_USER_AGENT") or "") or not _configured(
        environment.get("SEC_USER_AGENT")
    ):
        return False, "sec_fair_access_contact_missing"
    if not _configured(environment.get("FINRA_CLIENT_ID")):
        return False, "finra_client_id_missing"
    if not _configured(environment.get("FINRA_CLIENT_SECRET")):
        return False, "finra_client_secret_missing"
    return True, "server_side_shadow_enabled"


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _parse_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    try:
        parsed = (
            dt.datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
            if re.fullmatch(r"\d{14}", text)
            else dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _utc_iso(value: dt.datetime | None) -> str | None:
    return (
        value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        if value is not None
        else None
    )


class _BoundedClient:
    def __init__(
        self,
        session: Any,
        request_budgets: Mapping[str, int] | None = None,
    ) -> None:
        self.session = session
        self.request_budgets = dict(request_budgets or REQUEST_BUDGETS)
        self.counts = {key: 0 for key in self.request_budgets}

    def _reserve(self, counter: str) -> None:
        if counter not in self.request_budgets:
            raise ValueError(f"unknown_request_counter_{counter}")
        if self.counts[counter] >= self.request_budgets[counter]:
            raise ValueError(f"request_budget_exceeded_{counter}")
        self.counts[counter] += 1

    def request(
        self,
        counter: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[Any | None, bytes, dict[str, Any]]:
        self._reserve(counter)
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
            "rateLimitHeaderPresent": any(
                key.startswith("x-ratelimit") or key == "retry-after"
                for key in headers
            ),
            "redirected": bool(
                getattr(response, "is_redirect", False)
                or getattr(response, "is_permanent_redirect", False)
            ),
        }

    def stream_to_file(
        self,
        counter: str,
        method: str,
        url: str,
        path: Path,
        **kwargs: Any,
    ) -> tuple[Any | None, dict[str, Any]]:
        self._reserve(counter)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        response: Any | None = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            response = self.session.request(
                method,
                url,
                timeout=(10, 120),
                allow_redirects=False,
                stream=True,
                **kwargs,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                chunks = (
                    response.iter_content(chunk_size=1024 * 1024)
                    if hasattr(response, "iter_content")
                    else [bytes(getattr(response, "content", b""))]
                )
                for chunk in chunks:
                    if not chunk:
                        continue
                    raw = bytes(chunk)
                    handle.write(raw)
                    digest.update(raw)
                    byte_count += len(raw)
        except requests.RequestException as exc:
            if descriptor >= 0:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            return None, {
                "httpStatusCategory": None,
                "safeErrorCategory": type(exc).__name__,
                "responseSha256": None,
                "responseBytes": 0,
                "redirected": False,
            }
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()

        headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        status_code = int(getattr(response, "status_code", 0) or 0)
        return response, {
            "httpStatusCategory": (
                f"{status_code // 100}xx" if 100 <= status_code <= 599 else "invalid"
            ),
            "responseSha256": digest.hexdigest(),
            "responseBytes": byte_count,
            "httpDatePresent": bool(headers.get("date")),
            "rateLimitHeaderPresent": any(
                key.startswith("x-ratelimit") or key == "retry-after"
                for key in headers
            ),
            "redirected": bool(
                getattr(response, "is_redirect", False)
                or getattr(response, "is_permanent_redirect", False)
            ),
        }


def _atom_reference(
    body: bytes,
    allowed_forms: set[str],
) -> tuple[dict[str, str] | None, str, dict[str, Any]]:
    shape = {
        "discoveryEntryRows": 0,
        "machineReadableFormRows": 0,
        "allowedFormRows": 0,
        "rejectedSiblingFormRows": 0,
        "invalidReferenceRows": 0,
        "selectedAllowedFormStatus": "NO_FORM_SELECTED",
        "selectedFormFamily": None,
        "paginationUsed": False,
    }
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None, "DISCOVERY_XML_INVALID", shape
    namespace = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{namespace}entry")
    shape["discoveryEntryRows"] = len(entries)
    if not entries:
        return None, "NO_CURRENT_FILING_DISCOVERED", shape

    missing_form_metadata = 0
    selected_reference: dict[str, str] | None = None
    selected_form: str | None = None
    selected_reference_invalid = False
    for entry in entries:
        terms = [
            str(category.attrib.get("term") or "").strip()
            for category in entry.findall(f"{namespace}category")
            if str(category.attrib.get("term") or "").strip()
        ]
        if not terms:
            missing_form_metadata += 1
            continue
        shape["machineReadableFormRows"] += 1
        form = next((term for term in terms if term in allowed_forms), None)
        if form is None:
            shape["rejectedSiblingFormRows"] += 1
            continue
        shape["allowedFormRows"] += 1
        link = entry.find(f"{namespace}link")
        match = re.search(
            r"/Archives/edgar/data/(\d+)/(\d{18})(?:/|[-_])",
            urlparse(
                str(link.attrib.get("href") if link is not None else "")
            ).path,
        )
        if match is None:
            shape["invalidReferenceRows"] += 1
            if selected_form is None:
                selected_form = form
                selected_reference_invalid = True
            continue
        digits = match.group(2)
        if selected_form is None:
            selected_form = form
            selected_reference = {
                "cik": match.group(1),
                "accession": f"{digits[:10]}-{digits[10:12]}-{digits[12:]}",
                "form": form,
            }

    if selected_form is not None:
        shape["selectedFormFamily"] = selected_form
        if selected_reference_invalid:
            shape["selectedAllowedFormStatus"] = "ALLOWED_FORM_REFERENCE_INVALID"
            return None, "DISCOVERY_ALLOWED_FORM_REFERENCE_INVALID", shape
        shape["selectedAllowedFormStatus"] = "EXACT_ALLOWED_FORM_SELECTED"
        return selected_reference, "DISCOVERY_REFERENCE_VALID", shape

    if missing_form_metadata:
        shape["selectedAllowedFormStatus"] = "FORM_METADATA_INCOMPLETE"
        return None, "DISCOVERY_FORM_METADATA_MISSING_OR_INVALID", shape
    return None, "NO_CURRENT_ALLOWED_FORM_DISCOVERED", shape


def _submission_lineage(payload: Any, accession: str) -> dict[str, Any]:
    recent = (
        ((payload.get("filings") or {}).get("recent") or {})
        if isinstance(payload, dict)
        else {}
    )
    accessions = recent.get("accessionNumber") or []
    if not isinstance(recent, dict) or not isinstance(accessions, list):
        return {"status": "SUBMISSIONS_SCHEMA_INVALID"}
    try:
        index = accessions.index(accession)
    except ValueError:
        return {"status": "ACCESSION_NOT_OBSERVED"}

    def column(name: str) -> str:
        values = recent.get(name) or []
        return str(values[index]) if isinstance(values, list) and index < len(values) else ""

    return {
        "status": "ACCESSION_MATCHED",
        "observedForm": column("form"),
        "filingDate": column("filingDate") or None,
        "publishedAt": _utc_iso(_parse_timestamp(column("acceptanceDateTime"))),
    }


def _tag(block: str, name: str) -> str:
    match = re.search(rf"<{name}>\s*([^\r\n<]+)", block, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _raw_filing_shape(
    body: bytes,
    accession: str,
    allowed_forms: set[str],
) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    observed_accession = (
        re.search(r"ACCESSION NUMBER:\s*([0-9-]+)", text, flags=re.IGNORECASE)
        or [None, ""]
    )[1].strip()
    observed_form = (
        re.search(r"CONFORMED SUBMISSION TYPE:\s*([^\r\n]+)", text, flags=re.IGNORECASE)
        or [None, ""]
    )[1].strip()
    matching = parseable = wrappers = 0
    identifiers: set[str] = set()
    roots: set[str] = set()
    for block in re.findall(
        r"<DOCUMENT>(.*?)</DOCUMENT>", text, flags=re.IGNORECASE | re.DOTALL
    ):
        if _tag(block, "TYPE") not in allowed_forms or not _tag(
            block, "FILENAME"
        ).lower().endswith(".xml"):
            continue
        matching += 1
        start = re.search(r"<TEXT>", block, flags=re.IGNORECASE)
        if start is None:
            continue
        xml_text = re.sub(
            r"</TEXT>\s*$", "", block[start.end() :], flags=re.IGNORECASE
        ).strip()
        wrapper = re.fullmatch(
            r"<XML>\s*(.*?)\s*</XML>", xml_text, flags=re.IGNORECASE | re.DOTALL
        )
        if wrapper:
            xml_text = wrapper.group(1).strip()
            wrappers += 1
        try:
            root = ElementTree.fromstring(xml_text.encode("utf-8"))
        except ElementTree.ParseError:
            continue
        parseable += 1
        roots.add(root.tag.rsplit("}", 1)[-1])
        identifiers.update(
            _sha256(str(node.text or "").strip())
            for node in root.iter()
            if str(node.text or "").strip()
            and (
                node.tag.rsplit("}", 1)[-1].lower().endswith("cik")
                or node.tag.rsplit("}", 1)[-1].lower() == "cusip"
            )
        )
    valid = observed_accession == accession and observed_form in allowed_forms and parseable > 0
    return {
        "status": "RAW_FILING_SHAPE_VALID" if valid else "RAW_FILING_SHAPE_INVALID",
        "observedForm": observed_form or None,
        "accessionMatched": observed_accession == accession,
        "formFamilyMatched": observed_form in allowed_forms,
        "xmlShapeValid": parseable > 0,
        "matchingXmlDocumentCount": matching,
        "parseableMatchingXmlDocumentCount": parseable,
        "xmlWrapperDocumentCount": wrappers,
        "identifierHashCount": len(identifiers),
        "identifierScopeSha256": _sha256("\n".join(sorted(identifiers))) if identifiers else None,
        "rootElementScopeSha256": _sha256("\n".join(sorted(roots))) if roots else None,
    }


def _http_failure_class(response: Any | None) -> str:
    if response is None:
        return "TRANSIENT_FAILURE"
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {401, 403}:
        return "AUTH_OR_NETWORK_BLOCKED"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code >= 500:
        return "TRANSIENT_FAILURE"
    return "SOURCE_CONTRACT_INVALID"


def _sec_family(client: _BoundedClient, user_agent: str, contract: tuple[Any, ...]) -> dict[str, Any]:
    (
        source_id,
        evidence_class,
        form_filter,
        owner_mode,
        allowed_forms,
        discovery_count,
    ) = contract
    result = {
        "sourceId": source_id,
        "evidenceClass": evidence_class,
        "directSignalEligible": False,
        "historicalEvidenceCurrentSentimentEligible": False,
        "discoveryFormFilter": form_filter,
        "discoveryCountLimit": discovery_count,
    }
    url = "https://www.sec.gov/cgi-bin/browse-edgar?" + urlencode(
        {
            "action": "getcurrent",
            "type": form_filter,
            "company": "",
            "dateb": "",
            "owner": owner_mode,
            "start": "0",
            "count": str(discovery_count),
            "output": "atom",
        }
    )
    response, body, evidence = client.request(
        "secDiscovery",
        "GET",
        url,
        headers={"User-Agent": user_agent, "Accept": "application/atom+xml"},
    )
    result["discovery"] = evidence
    if response is None or response.status_code != 200 or evidence["redirected"]:
        result["discovery"]["status"] = "SEC_DISCOVERY_HTTP_FAILURE"
        return {
            **result,
            "status": "SOURCE_HTTP_FAILURE",
            "safeErrorCategory": "sec_discovery_failure",
            "httpFailureClass": _http_failure_class(response),
        }
    reference, status, discovery_shape = _atom_reference(body, allowed_forms)
    result["discovery"].update(
        discovery_shape,
        status=status,
        discoveryCountLimit=discovery_count,
    )
    if reference is None:
        return {
            **result,
            "status": status,
            "safeErrorCategory": (
                None
                if status
                in {
                    "NO_CURRENT_FILING_DISCOVERED",
                    "NO_CURRENT_ALLOWED_FORM_DISCOVERED",
                }
                else "sec_discovery_contract_invalid"
            ),
        }

    result["accessionSha256"] = _sha256(reference["accession"])
    result["archiveCikSha256"] = _sha256(reference["cik"].zfill(10))
    response, _, evidence = client.request(
        "secSubmissions",
        "GET",
        f"https://data.sec.gov/submissions/CIK{int(reference['cik']):010d}.json",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    result["submissions"] = evidence
    if response is None or response.status_code != 200 or evidence["redirected"]:
        return {
            **result,
            "status": "SOURCE_HTTP_FAILURE",
            "safeErrorCategory": "sec_submissions_failure",
            "httpFailureClass": _http_failure_class(response),
        }
    try:
        lineage = _submission_lineage(response.json(), reference["accession"])
    except ValueError:
        lineage = {"status": "SUBMISSIONS_JSON_INVALID"}
    result["submissionsLineage"] = lineage
    if lineage["status"] != "ACCESSION_MATCHED":
        return {
            **result,
            "status": "SOURCE_CONTRACT_INVALID",
            "safeErrorCategory": "sec_submissions_accession_lineage_invalid",
        }
    if lineage["observedForm"] not in allowed_forms:
        return {
            **result,
            "status": "SOURCE_CONTRACT_INVALID",
            "safeErrorCategory": "sec_submissions_form_family_mismatch",
        }

    response, body, evidence = client.request(
        "secRawFiling",
        "GET",
        (
            f"https://www.sec.gov/Archives/edgar/data/{int(reference['cik'])}/"
            f"{reference['accession'].replace('-', '')}/{reference['accession']}.txt"
        ),
        headers={"User-Agent": user_agent, "Accept": "text/plain, */*"},
    )
    result["rawFiling"] = evidence
    if response is None or response.status_code != 200 or evidence["redirected"]:
        return {
            **result,
            "status": "SOURCE_HTTP_FAILURE",
            "safeErrorCategory": "sec_raw_filing_failure",
            "httpFailureClass": _http_failure_class(response),
        }
    shape = _raw_filing_shape(body, reference["accession"], allowed_forms)
    result["rawFiling"].update(shape)
    if not shape["accessionMatched"]:
        return {
            **result,
            "status": "SOURCE_CONTRACT_INVALID",
            "safeErrorCategory": "sec_raw_filing_accession_lineage_invalid",
        }
    if not shape["formFamilyMatched"]:
        return {
            **result,
            "status": "SOURCE_CONTRACT_INVALID",
            "safeErrorCategory": "sec_raw_filing_form_family_mismatch",
        }
    if not shape["xmlShapeValid"]:
        return {
            **result,
            "status": "SOURCE_CONTRACT_INVALID",
            "safeErrorCategory": "sec_raw_filing_xml_shape_invalid",
        }
    return {
        **result,
        "status": "SOURCE_OBSERVATION_PASS",
        "safeErrorCategory": None,
        "observedForm": lineage["observedForm"],
        "filingDate": lineage["filingDate"],
        "publishedAt": lineage["publishedAt"],
    }


def collect_sec_schedule13_exact_family_evidence(
    *,
    session: Any,
    sec_user_agent: str,
    retrieved_at: str,
) -> dict[str, Any]:
    if (
        "@" not in sec_user_agent
        or not _configured(sec_user_agent)
        or _parse_timestamp(retrieved_at) is None
    ):
        raise ValueError("invalid_sec_contact_or_retrieval_timestamp")
    client = _BoundedClient(session)
    contract = next(row for row in SEC_FAMILIES if row[0] == SCHEDULE13_SOURCE_ID)
    schedule13 = _sec_family(client, sec_user_agent, contract)
    source_status = str(schedule13.get("status") or "")
    error_category = str(schedule13.get("safeErrorCategory") or "")
    http_failure = str(schedule13.get("httpFailureClass") or "")
    if source_status == "SOURCE_OBSERVATION_PASS":
        status = "SEC_SCHEDULE13_EXACT_FAMILY_PASS"
    elif source_status in {
        "NO_CURRENT_FILING_DISCOVERED",
        "NO_CURRENT_ALLOWED_FORM_DISCOVERED",
    }:
        status = "SEC_SCHEDULE13_NO_CURRENT_ALLOWED_FORM_PASS"
    elif source_status == "SOURCE_HTTP_FAILURE":
        status = {
            "AUTH_OR_NETWORK_BLOCKED": "SEC_SCHEDULE13_AUTH_OR_NETWORK_BLOCKED",
            "RATE_LIMITED": "SEC_SCHEDULE13_RATE_LIMITED",
            "SOURCE_CONTRACT_INVALID": "SEC_SCHEDULE13_DISCOVERY_METADATA_INVALID",
        }.get(http_failure, "SEC_SCHEDULE13_TRANSIENT_FAILURE")
    elif error_category.startswith("sec_submissions_"):
        status = "SEC_SCHEDULE13_SUBMISSIONS_LINEAGE_INVALID"
    elif error_category.startswith("sec_raw_filing_"):
        status = "SEC_SCHEDULE13_RAW_FILING_CONTRACT_INVALID"
    else:
        status = "SEC_SCHEDULE13_DISCOVERY_METADATA_INVALID"

    request_counts = {
        "secSchedule13Discovery": client.counts["secDiscovery"],
        "secSchedule13Submissions": client.counts["secSubmissions"],
        "secSchedule13RawFiling": client.counts["secRawFiling"],
        "section16": 0,
        "form13F": 0,
        "finraOauth": 0,
        "finraMetadata": 0,
        "finraData": 0,
        "federalReserve": 0,
        "fred": 0,
        "bea": 0,
        "bls": 0,
        "blsCalendar": 0,
        "toss": 0,
    }
    result = {
        "schemaVersion": SCHEDULE13_TARGETED_SCHEMA_VERSION,
        "mode": "SHADOW_ONLY_TARGETED_REPROOF",
        "status": status,
        "retrievedAt": retrieved_at,
        "schedule13": schedule13,
        "requestCounts": request_counts,
        "externalRequestCount": sum(request_counts.values()),
        "requestBudgetCompliant": (
            client.counts["secDiscovery"] <= 1
            and client.counts["secSubmissions"] <= 1
            and client.counts["secRawFiling"] <= 1
        ),
        "exactFamilyFilterApplied": True,
        "titleOrSummaryHeuristicUsed": False,
        "paginationUsed": False,
        "retryCount": 0,
        "rawResponseStored": False,
        "actualIdentifiersStoredOrPrinted": False,
        "secretValuesStoredOrPrinted": False,
        "googleDrivePublished": False,
        "recurringActivationAuthorized": False,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "brokerOrSidecarStateMutation": False,
        "unknownOrUnclassifiedRows": 0,
    }
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    result["evidenceSha256"] = _canonical_sha256(result)
    return result


def _finra(client: _BoundedClient, client_id: str, client_secret: str) -> dict[str, Any]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response, _, oauth = client.request(
        "finraOauth",
        "POST",
        "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token",
        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        params={"grant_type": "client_credentials"},
        data=b"",
    )
    oauth.pop("responseSha256", None)
    result: dict[str, Any] = {"oauth": oauth, "datasets": []}
    if response is None or response.status_code != 200 or oauth["redirected"]:
        return {
            **result,
            "status": "AUTH_OR_ENTITLEMENT_BLOCKED" if response is not None and response.status_code in {401, 403} else "SOURCE_HTTP_FAILURE",
            "safeErrorCategory": "finra_oauth_failure",
        }
    try:
        token = str((response.json() or {}).get("access_token") or "")
    except (AttributeError, ValueError):
        token = ""
    if not token:
        return {**result, "status": "SOURCE_CONTRACT_INVALID", "safeErrorCategory": "finra_oauth_contract_invalid"}
    result["oauth"]["accessTokenPresent"] = True
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response, _, evidence = client.request(
        "finraMetadata",
        "GET",
        "https://api.finra.org/metadata/group/otcMarket/name/consolidatedShortInterest",
        headers=headers,
    )
    result["metadata"] = evidence
    if response is None or response.status_code != 200 or evidence["redirected"]:
        return {**result, "status": "SOURCE_HTTP_FAILURE", "safeErrorCategory": "finra_metadata_failure"}
    try:
        metadata = response.json()
    except ValueError:
        metadata = None
    if not isinstance(metadata, (dict, list)):
        return {**result, "status": "SOURCE_CONTRACT_INVALID", "safeErrorCategory": "finra_metadata_contract_invalid"}
    result["metadata"]["shapeSha256"] = _canonical_sha256(metadata)

    for source_id, dataset, evidence_class, required, identifier_field, date_field in FINRA_DATASETS:
        response, _, evidence = client.request(
            "finraData",
            "GET",
            f"https://api.finra.org/data/group/otcMarket/name/{dataset}",
            headers=headers,
            params={"limit": "1"},
        )
        item = {
            "sourceId": source_id,
            "dataset": dataset,
            "evidenceClass": evidence_class,
            "directSignalEligible": False,
            "response": evidence,
        }
        result["datasets"].append(item)
        if response is None or response.status_code != 200 or evidence["redirected"]:
            item.update(
                status="SOURCE_HTTP_FAILURE",
                safeErrorCategory="finra_data_failure",
            )
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            item.update(status="SOURCE_CONTRACT_INVALID", safeErrorCategory="finra_data_contract_invalid")
            continue
        row = payload[0]
        identifier = str(row.get(identifier_field) or "").strip()
        missing = required - set(row)
        item.update(
            rowFieldCount=len(row),
            rowKeySetSha256=_sha256("\n".join(sorted(str(key) for key in row))),
            missingRequiredFieldCount=len(missing),
            identifierSha256=_sha256(identifier) if identifier else None,
            eventDate=str(row.get(date_field) or "") or None,
            status="SOURCE_OBSERVATION_PASS" if not missing and identifier else "SOURCE_CONTRACT_INVALID",
            safeErrorCategory=None if not missing and identifier else "finra_required_field_missing",
        )
    complete = len(result["datasets"]) == len(FINRA_DATASETS) and all(
        row["status"] == "SOURCE_OBSERVATION_PASS" for row in result["datasets"]
    )
    return {
        **result,
        "status": "SOURCE_OBSERVATION_PASS" if complete else "SOURCE_CONTRACT_INVALID",
        "safeErrorCategory": None if complete else "finra_dataset_incomplete",
    }


def build_sec_finra_shadow_not_run_result(reason: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "SEC_FINRA_SHADOW_NOT_RUN",
        "mode": "SHADOW_ONLY",
        "runtimeAction": "NOT_RUN",
        "runtimeReason": reason,
        "coverageMode": "LATEST_OFFICIAL_OBSERVATION_ONLY",
        "requestCounts": {key: 0 for key in REQUEST_BUDGETS},
        "externalRequestCount": 0,
        "analysisEligible": False,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "exchangeListedRegShoRequested": False,
        "exchangeListedRegShoStatus": "EXCLUDED_SOURCE_MATRIX_INCOMPLETE",
        "rawResponseStored": False,
        "secretValuesStoredOrPrinted": False,
        "actualIdentifiersStoredOrPrinted": False,
        "accountHeaderUsed": False,
        "accountEndpointUsed": False,
        "orderEndpointUsed": False,
        "brokerOrSidecarStateMutation": False,
        "unknownOrUnclassifiedRows": 0,
    }


def collect_sec_finra_shadow_evidence(
    *,
    session: Any,
    sec_user_agent: str,
    finra_client_id: str,
    finra_client_secret: str,
    retrieved_at: str,
) -> dict[str, Any]:
    if "@" not in sec_user_agent or not _configured(sec_user_agent) or _parse_timestamp(retrieved_at) is None:
        raise ValueError("invalid_sec_contact_or_retrieval_timestamp")
    if not _configured(finra_client_id) or not _configured(finra_client_secret):
        raise ValueError("finra_credentials_missing")
    client = _BoundedClient(session)
    sec_rows = [_sec_family(client, sec_user_agent, contract) for contract in SEC_FAMILIES]
    finra = _finra(client, finra_client_id, finra_client_secret)
    complete = all(
        row["status"]
        in {
            "SOURCE_OBSERVATION_PASS",
            "NO_CURRENT_FILING_DISCOVERED",
            "NO_CURRENT_ALLOWED_FORM_DISCOVERED",
        }
        for row in sec_rows
    ) and finra["status"] == "SOURCE_OBSERVATION_PASS"
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "status": PASS_STATUS if complete else PARTIAL_STATUS,
        "mode": "SHADOW_ONLY",
        "runtimeAction": "BOUNDED_SOURCE_OBSERVATION",
        "coverageMode": "LATEST_OFFICIAL_OBSERVATION_ONLY",
        "retrievedAt": retrieved_at,
        "requestBudgets": REQUEST_BUDGETS,
        "requestCounts": dict(client.counts),
        "externalRequestCount": sum(client.counts.values()),
        "requestBudgetCompliant": all(
            client.counts[key] <= REQUEST_BUDGETS[key] for key in REQUEST_BUDGETS
        ),
        "secFamilies": sec_rows,
        "finra": finra,
        "analysisEligible": False,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage4To7Impact": "NONE",
        "exchangeListedRegShoRequested": False,
        "exchangeListedRegShoStatus": "EXCLUDED_SOURCE_MATRIX_INCOMPLETE",
        "rawResponseStored": False,
        "secretValuesStoredOrPrinted": False,
        "actualIdentifiersStoredOrPrinted": False,
        "accountHeaderUsed": False,
        "accountEndpointUsed": False,
        "orderEndpointUsed": False,
        "brokerOrSidecarStateMutation": False,
        "semanticGuards": {
            "form13fIsRealtimeTradeEvidence": False,
            "historicalFilingIsCurrentSentiment": False,
            "regShoThresholdIsShortSignal": False,
            "shortInterestEquivalentToShortSaleVolume": False,
        },
        "unknownOrUnclassifiedRows": 0,
    }
    result["evidenceSha256"] = _canonical_sha256(result)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    return result
