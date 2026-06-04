#!/usr/bin/env python3
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel: str) -> str:
    path = ROOT / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""

checks = []

def add(check_id: str, status: str, detail: str) -> None:
    checks.append({"id": check_id, "status": status, "detail": detail})

workflow = read(".github/workflows/main.yml")
harvester = read("harvester.py")

add(
    "workflow_no_primary_chat_fallback",
    "PASS" if workflow and "secrets.TELEGRAM_CHAT_ID" not in workflow and "vars.TELEGRAM_CHAT_ID" not in workflow else "FAIL",
    "Harvester workflow must not route collection, test, or monitor notifications to the primary analysis chat.",
)
add(
    "workflow_uses_simulation_and_alert_routes",
    "PASS" if "TELEGRAM_SIMULATION_CHAT_ID" in workflow and "TELEGRAM_ALERT_CHAT_ID" in workflow else "FAIL",
    "Harvester workflow must expose simulation and alert routes explicitly.",
)
add(
    "harvester_ops_no_primary_fallback",
    "PASS" if "TELEGRAM_SIMULATION_CHAT_ID" in harvester and not re.search(r"return\s+TELEGRAM_CHAT_ID\b", harvester) else "FAIL",
    "Normal Harvester collection notifications must use simulation route only.",
)
add(
    "harvester_alert_no_primary_fallback",
    "PASS" if not re.search(r"TELEGRAM_ALERT_CHAT_ID\s+or\s+TELEGRAM_CHAT_ID", harvester) else "FAIL",
    "Harvester errors may use alert then simulation, but never primary.",
)

fail = sum(1 for check in checks if check["status"] == "FAIL")
warn = sum(1 for check in checks if check["status"] == "WARN")
report = {
    "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "overall": "fail" if fail else "warn" if warn else "pass",
    "policy": "primary_analysis_only_simulation_for_monitoring_alert_for_errors",
    "checks": checks,
}
state_dir = ROOT / "state"
state_dir.mkdir(exist_ok=True)
(state_dir / "telegram-routing-policy-audit.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
rows = [
    "# Telegram Routing Policy Audit",
    "",
    f"- overall: **{report['overall']}**",
    f"- policy: {report['policy']}",
    "",
    "| Check | Status | Detail |",
    "| --- | --- | --- |",
]
rows.extend(f"| {check['id']} | {check['status']} | {check['detail']} |" for check in checks)
(state_dir / "telegram-routing-policy-audit.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
print(f"[TELEGRAM_ROUTING_AUDIT] overall={report['overall']} checks={len(checks)} json=state/telegram-routing-policy-audit.json")
if fail:
    raise SystemExit(1)
