from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
CI_WORKFLOW = (ROOT / ".github/workflows/telegram-routing-ci.yml").read_text(encoding="utf-8")
HARVESTER = (ROOT / "harvester.py").read_text(encoding="utf-8")


def main() -> int:
    assert "cron: '13 22 * * 1-5'" in WORKFLOW
    assert "cron: '13 3 * * 2-6'" in WORKFLOW
    assert "GITHUB_EVENT_SCHEDULE: ${{ github.event.schedule }}" in WORKFLOW
    assert "github.event.schedule == '13 22 * * 1-5'" in WORKFLOW
    assert "github.event.schedule == '13 3 * * 2-6'" in WORKFLOW
    assert "GITHUB_EVENT_SCHEDULE = os.getenv('GITHUB_EVENT_SCHEDULE')" in HARVESTER
    assert 'source = "schedule" if GITHUB_EVENT_NAME == "schedule"' in HARVESTER
    assert 'f"{source}:first"' in HARVESTER
    assert 'f"{source}:second"' in HARVESTER
    assert "scheduled_batch_mapping_missing" in HARVESTER
    assert '"scheduleExpression": GITHUB_EVENT_SCHEDULE' in HARVESTER
    assert "python scripts/check_harvester_schedule_slot_contract.py" in WORKFLOW
    assert "python scripts/check_harvester_schedule_slot_contract.py" in CI_WORKFLOW
    print("[HARVESTER_SCHEDULE_SLOT_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
