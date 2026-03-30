import datetime as dt
import json
import os
import sys

import requests

NOTION_VERSION = "2022-06-28"


def env(name, default=""):
    return str(os.getenv(name, default) or "").strip()


def bool_from_env(name, default=True):
    raw = env(name)
    if not raw:
        return default
    value = raw.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def short_text(value, max_len=1800):
    return str(value if value is not None else "").strip()[:max_len]


def notion_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(token, path, method="GET", payload=None):
    url = f"https://api.notion.com{path}"
    response = requests.request(
        method,
        url,
        headers=notion_headers(token),
        data=json.dumps(payload) if payload is not None else None,
        timeout=15,
    )
    text = response.text or ""
    try:
        data = json.loads(text) if text else {}
    except Exception:
        data = {}
    if not response.ok:
        raise RuntimeError(f"Notion {path} failed ({response.status_code}): {json.dumps(data)[:400]}")
    return data


def read_summary(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            value = json.load(fp)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def find_title_property(schema):
    for name, definition in (schema or {}).items():
        if str((definition or {}).get("type") or "") == "title":
            return name
    return None


def title_prop(value):
    return {"title": [{"text": {"content": short_text(value, 200)}}]}


def text_prop(value):
    return {"rich_text": [{"text": {"content": short_text(value, 1900)}}]}


def date_prop(value):
    try:
        date_text = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        date_text = dt.datetime.utcnow().date().isoformat()
    return {"date": {"start": date_text}}


def select_prop(value):
    return {"select": {"name": short_text(value, 100) or "Partial"}}


def query_existing_by_title(token, database_id, title_name, title_value):
    payload = {
        "filter": {
            "property": title_name,
            "title": {"equals": title_value},
        },
        "page_size": 1,
    }
    data = notion_request(token, f"/v1/databases/{database_id}/query", method="POST", payload=payload)
    results = data.get("results") if isinstance(data, dict) else []
    if isinstance(results, list) and results:
        return results[0]
    return None


def upsert_page(token, database_id, title_name, title_value, properties):
    existing = query_existing_by_title(token, database_id, title_name, title_value)
    if existing and existing.get("id"):
        notion_request(
            token,
            f"/v1/pages/{existing['id']}",
            method="PATCH",
            payload={"properties": properties},
        )
        return "updated"
    notion_request(
        token,
        "/v1/pages",
        method="POST",
        payload={
            "parent": {"database_id": database_id},
            "properties": properties,
        },
    )
    return "created"


def set_property_if_supported(target, schema, name, handlers):
    definition = (schema or {}).get(name) or {}
    prop_type = str(definition.get("type") or "")
    handler = handlers.get(prop_type)
    if handler is None:
        return
    target[name] = handler()


def main():
    if not bool_from_env("NOTION_HARVESTER_SYNC_ENABLED", True):
        print("[NOTION_HARVESTER_SYNC] skip: disabled_by_env")
        return 0

    token = env("NOTION_TOKEN")
    db_daily = env("NOTION_DB_DAILY_SNAPSHOT")
    required = bool_from_env("NOTION_HARVESTER_SYNC_REQUIRED", False)
    if not token or not db_daily:
        message = "[NOTION_HARVESTER_SYNC] skip: missing NOTION_TOKEN or NOTION_DB_DAILY_SNAPSHOT"
        if required:
            raise RuntimeError(message)
        print(message)
        return 0

    summary_path = env("HARVESTER_RUN_SUMMARY_PATH", "state/last-harvester-run.json")
    summary = read_summary(summary_path)

    run_id = env("GITHUB_RUN_ID", "local")
    run_attempt = env("GITHUB_RUN_ATTEMPT", "1")
    run_key = f"harvester-{run_id}-{run_attempt}"
    mode = short_text(summary.get("mode") or "N/A", 40)
    status_raw = env("GHA_JOB_STATUS", summary.get("status") or "success").lower()
    status = "Success" if status_raw == "success" else "Partial"
    summary_text = " ".join(
        [
            "source=harvester",
            f"status={status_raw}",
            f"event={summary.get('eventName') or env('GITHUB_EVENT_NAME', 'N/A')}",
            f"mode={mode}",
            f"batch={summary.get('batchMode') or 'N/A'}",
            f"targetSymbols={summary.get('targetSymbols', 'N/A')}",
            f"success={summary.get('successCount', 'N/A')}",
            f"errors={summary.get('errorCount', 'N/A')}",
            f"durationMin={summary.get('durationMinutes', 'N/A')}",
            f"triggerFile={summary.get('triggerFile') or 'N/A'}",
        ]
    )
    top_tickers = (
        f"mode={mode} target={summary.get('targetSymbols', 'N/A')} "
        f"ok={summary.get('successCount', 'N/A')} err={summary.get('errorCount', 'N/A')}"
    )

    db = notion_request(token, f"/v1/databases/{db_daily}", method="GET")
    schema = db.get("properties") if isinstance(db, dict) else {}
    title_name = find_title_property(schema) or "Run Date"

    properties = {title_name: title_prop(run_key)}
    set_property_if_supported(
        properties,
        schema,
        "Date",
        {
            "date": lambda: date_prop(summary.get("completedAt") or dt.datetime.utcnow().isoformat() + "Z"),
            "rich_text": lambda: text_prop(dt.datetime.utcnow().date().isoformat()),
        },
    )
    set_property_if_supported(
        properties,
        schema,
        "Status",
        {
            "select": lambda: select_prop(status),
            "rich_text": lambda: text_prop(status),
        },
    )
    set_property_if_supported(properties, schema, "Summary", {"rich_text": lambda: text_prop(summary_text)})
    set_property_if_supported(properties, schema, "Top Tickers", {"rich_text": lambda: text_prop(top_tickers)})
    set_property_if_supported(
        properties,
        schema,
        "Engine",
        {
            "select": lambda: select_prop("harvester"),
            "rich_text": lambda: text_prop("harvester"),
        },
    )

    upsert_status = upsert_page(token, db_daily, title_name, run_key, properties)
    print(
        f"[NOTION_HARVESTER_SYNC] {upsert_status} key={run_key} status={status_raw} "
        f"mode={mode} success={summary.get('successCount', 'N/A')} errors={summary.get('errorCount', 'N/A')}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[NOTION_HARVESTER_SYNC] failed: {error}", file=sys.stderr)
        raise
