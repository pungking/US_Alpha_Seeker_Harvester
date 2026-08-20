from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests


SCHEMA_VERSION = "sec-finra-shadow-evidence-v1"
PASS_STATUS = "SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"
PARTIAL_STATUS = "SEC_FINRA_SHADOW_PARTIAL"
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
    ),
    (
        "SEC_SCHEDULES_13D_13G",
        "BENEFICIAL_OWNERSHIP_POSITION_DISCLOSURE",
        "SC 13",
        "include",
        {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"},
    ),
    (
        "SEC_FORM_13F",
        "INSTITUTIONAL_HOLDINGS_SNAPSHOT",
        "13F-HR",
        "include",
        {"13F-HR", "13F-HR/A"},
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
    def __init__(self, session: Any) -> None:
        self.session = session
        self.counts = {key: 0 for key in REQUEST_BUDGETS}

    def request(
        self,
        counter: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[Any | None, bytes, dict[str, Any]]:
        if self.counts[counter] >= REQUEST_BUDGETS[counter]:
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
            "rateLimitHeaderPresent": any(
                key.startswith("x-ratelimit") or key == "retry-after"
                for key in headers
            ),
            "redirected": bool(
                getattr(response, "is_redirect", False)
                or getattr(response, "is_permanent_redirect", False)
            ),
        }


def _atom_reference(body: bytes) -> tuple[dict[str, str] | None, str]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None, "DISCOVERY_XML_INVALID"
    entry = root.find("{http://www.w3.org/2005/Atom}entry")
    if entry is None:
        return None, "NO_CURRENT_FILING_DISCOVERED"
    link = entry.find("{http://www.w3.org/2005/Atom}link")
    match = re.search(
        r"/Archives/edgar/data/(\d+)/(\d{18})(?:/|[-_])",
        urlparse(str(link.attrib.get("href") if link is not None else "")).path,
    )
    if match is None:
        return None, "DISCOVERY_REFERENCE_INVALID"
    digits = match.group(2)
    return {
        "cik": match.group(1),
        "accession": f"{digits[:10]}-{digits[10:12]}-{digits[12:]}",
    }, "DISCOVERY_REFERENCE_VALID"


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
        "matchingXmlDocumentCount": matching,
        "parseableMatchingXmlDocumentCount": parseable,
        "xmlWrapperDocumentCount": wrappers,
        "identifierHashCount": len(identifiers),
        "identifierScopeSha256": _sha256("\n".join(sorted(identifiers))) if identifiers else None,
        "rootElementScopeSha256": _sha256("\n".join(sorted(roots))) if roots else None,
    }


def _sec_family(client: _BoundedClient, user_agent: str, contract: tuple[Any, ...]) -> dict[str, Any]:
    source_id, evidence_class, form_filter, owner_mode, allowed_forms = contract
    result = {
        "sourceId": source_id,
        "evidenceClass": evidence_class,
        "directSignalEligible": False,
        "historicalEvidenceCurrentSentimentEligible": False,
        "discoveryFormFilter": form_filter,
    }
    url = "https://www.sec.gov/cgi-bin/browse-edgar?" + urlencode(
        {
            "action": "getcurrent",
            "type": form_filter,
            "company": "",
            "dateb": "",
            "owner": owner_mode,
            "start": "0",
            "count": "1",
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
        return {**result, "status": "SOURCE_HTTP_FAILURE", "safeErrorCategory": "sec_discovery_failure"}
    reference, status = _atom_reference(body)
    result["discovery"]["status"] = status
    if reference is None:
        return {
            **result,
            "status": status,
            "safeErrorCategory": None if status == "NO_CURRENT_FILING_DISCOVERED" else "sec_discovery_contract_invalid",
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
        return {**result, "status": "SOURCE_HTTP_FAILURE", "safeErrorCategory": "sec_submissions_failure"}
    try:
        lineage = _submission_lineage(response.json(), reference["accession"])
    except ValueError:
        lineage = {"status": "SUBMISSIONS_JSON_INVALID"}
    result["submissionsLineage"] = lineage

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
        return {**result, "status": "SOURCE_HTTP_FAILURE", "safeErrorCategory": "sec_raw_filing_failure"}
    shape = _raw_filing_shape(body, reference["accession"], allowed_forms)
    result["rawFiling"].update(shape)
    if lineage["status"] != "ACCESSION_MATCHED" or shape["status"] != "RAW_FILING_SHAPE_VALID":
        return {**result, "status": "SOURCE_CONTRACT_INVALID", "safeErrorCategory": "sec_lineage_or_xml_invalid"}
    return {
        **result,
        "status": "SOURCE_OBSERVATION_PASS",
        "safeErrorCategory": None,
        "observedForm": lineage["observedForm"],
        "filingDate": lineage["filingDate"],
        "publishedAt": lineage["publishedAt"],
    }


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
        row["status"] in {"SOURCE_OBSERVATION_PASS", "NO_CURRENT_FILING_DISCOVERED"}
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
