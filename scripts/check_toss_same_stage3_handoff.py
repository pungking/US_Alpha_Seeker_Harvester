from __future__ import annotations

import json
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.toss_shadow_market_data import (
    build_same_stage3_readiness,
    build_stage3_request_scope,
    finish_same_stage3_sentinel,
    publish_toss_shadow_with_archive,
    reserve_same_stage3_sentinel,
    same_stage3_idempotency_key,
    same_stage3_scope_matches,
    toss_shadow_matches_stage3,
)
import harvester as harvester_module


def _payload(*symbols: str) -> dict[str, object]:
    return {
        "manifest": {"timestamp": "2026-08-13T00:00:00Z"},
        "fundamental_universe": [{"symbol": symbol} for symbol in symbols],
    }


def _passing_shadow(source_artifact: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": "toss-market-data-shadow-v1",
        "status": "TOSS_SHADOW_PASS",
        "mode": "SHADOW_ONLY",
        "provider": "TOSS_OPEN_API",
        "eligible": True,
        "tossEvidenceExcluded": False,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "accountHeaderUsed": False,
        "orderEndpointUsed": False,
        "rateLimitHeadersComplete": True,
        "requestCounts": {"oauth": 1, "marketCalendar": 1, "prices": 1},
        "responseSha256": {
            "oauth": "1" * 64,
            "marketCalendar": "2" * 64,
            "prices": ["3" * 64],
        },
        "summary": {
            "requestedRows": 2,
            "matchedRows": 2,
            "missingRows": 0,
            "invalidRows": 0,
            "duplicateRows": 0,
        },
        "prices": [{"providerSymbolSha256": "4" * 64}] * 2,
        "requestLineage": {
            "status": "VERIFIED_STAGE3_REQUEST_SCOPE",
            "requestScopeSha256": source_artifact["requestScopeSha256"],
            "requestSourceArtifact": source_artifact,
        },
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    harvester_source = (root / "harvester.py").read_text(encoding="utf-8")
    workflow_source = (root / ".github/workflows/main.yml").read_text(
        encoding="utf-8"
    )

    scope = build_stage3_request_scope(
        file_name="STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
        payload=_payload("FIXTURE.A", "FIXTURE-B"),
        drive_created_at="2026-08-13T00:00:01Z",
    )
    assert scope["status"] == "VERIFIED_STAGE3_REQUEST_SCOPE"
    assert scope["normalizationCollisionRows"] == 0
    assert scope["symbols"] == ["FIXTURE-B", "FIXTURE.A"]
    source_artifact = scope["sourceArtifact"]
    assert source_artifact["hashBasis"] == "CANONICAL_JSON"
    assert source_artifact["generatedAtSource"] == "ARTIFACT_FIELD"

    exact = build_stage3_request_scope(
        file_name=str(source_artifact["file"]),
        payload=_payload("FIXTURE.A", "FIXTURE-B"),
        expected_sha256=str(source_artifact["sha256"]),
        drive_created_at="2026-08-13T00:00:01Z",
    )
    assert exact == scope

    hash_mismatch = build_stage3_request_scope(
        file_name=str(source_artifact["file"]),
        payload=_payload("FIXTURE.A", "FIXTURE-B"),
        expected_sha256="0" * 64,
        drive_created_at="2026-08-13T00:00:01Z",
    )
    assert hash_mismatch["status"] == "REQUEST_SOURCE_HASH_MISMATCH"
    assert hash_mismatch["symbols"] == []

    collision = build_stage3_request_scope(
        file_name="STAGE3_FUNDAMENTAL_FULL_COLLISION.json",
        payload=_payload("fixture", "FIXTURE"),
        drive_created_at="2026-08-13T00:00:01Z",
    )
    assert collision["status"] == "NORMALIZATION_COLLISION"
    assert collision["normalizationCollisionRows"] == 1

    key = same_stage3_idempotency_key(source_artifact)
    assert len(key) == 64
    assert key == same_stage3_idempotency_key(source_artifact)
    same_identity_different_time_source = {
        **source_artifact,
        "generatedAt": "2026-08-13T00:00:01Z",
        "generatedAtSource": "GOOGLE_DRIVE_CREATED_TIME",
    }
    assert key == same_stage3_idempotency_key(same_identity_different_time_source)
    shadow = _passing_shadow(source_artifact)
    assert toss_shadow_matches_stage3(shadow, source_artifact) is True
    assert same_stage3_scope_matches(scope, source_artifact) is True
    assert toss_shadow_matches_stage3(
        shadow, same_identity_different_time_source
    ) is True
    stale_source = {**source_artifact, "file": "STAGE3_FUNDAMENTAL_FULL_STALE.json"}
    assert toss_shadow_matches_stage3(shadow, stale_source) is False
    assert toss_shadow_matches_stage3({**shadow, "status": "TOSS_SHADOW_TRANSIENT_FAILURE"}, source_artifact) is False
    assert toss_shadow_matches_stage3({**shadow, "prices": []}, source_artifact) is False
    assert same_stage3_scope_matches(
        {**scope, "status": "STAGE4_ORDERING_WINDOW_CLOSED"}, source_artifact
    ) is False

    readiness = build_same_stage3_readiness(
        scope=scope,
        existing_shadow=shadow,
        collector_enabled=False,
    )
    assert readiness["status"] == "EXISTING_MATCHED_SHADOW_REUSABLE"
    assert readiness["collectorRequired"] is False
    assert readiness["requestBudget"] == {
        "oauth": 0,
        "marketCalendar": 0,
        "pricesMax": 0,
    }
    activation = build_same_stage3_readiness(
        scope=scope,
        existing_shadow=None,
        collector_enabled=False,
    )
    assert activation["status"] == "SAME_STAGE3_HANDOFF_READY_FOR_ACTIVATION"
    assert activation["collectorRequired"] is True
    assert activation["canonicalAnalysisCanContinue"] is True
    malformed_existing = build_same_stage3_readiness(
        scope=scope,
        existing_shadow={"requestLineage": ["invalid"]},
        collector_enabled=False,
    )
    assert malformed_existing["status"] == (
        "SAME_STAGE3_HANDOFF_READY_FOR_ACTIVATION"
    )

    class _Files:
        def get(self, **_kwargs: object) -> "_Files":
            return self

        def execute(self) -> dict[str, str]:
            return {
                "id": "stage3-id",
                "name": str(source_artifact["file"]),
                "createdTime": "2026-08-13T00:00:01Z",
            }

    class _Drive:
        def files(self) -> _Files:
            return _Files()

    progress = {
        "status": "PROCESSING",
        "trigger_file": source_artifact["file"],
        "trigger_sha256": source_artifact["sha256"],
        "trigger_hash_basis": "CANONICAL_JSON",
        "trigger_request_scope_sha256": source_artifact["requestScopeSha256"],
    }
    original_find = harvester_module.find_file_id
    original_download = harvester_module.download_json
    original_drive = harvester_module.drive_service
    original_runtime_decision = harvester_module.toss_shadow_runtime_decision
    original_window_inspector = harvester_module.inspect_same_stage3_handoff_window
    original_write = harvester_module.write_json_report
    original_send = harvester_module.send_telegram
    try:
        harvester_module.find_file_id = lambda name, _parent=None: {
            "COLLECTION_PROGRESS.json": "progress-id",
            "Stage3_Fundamental_Data": "stage3-folder-id",
            str(source_artifact["file"]): "stage3-id",
        }.get(name)
        harvester_module.download_json = lambda file_id: (
            progress if file_id == "progress-id" else _payload("FIXTURE.A", "FIXTURE-B")
        )
        harvester_module.drive_service = _Drive()
        exact_handoff = harvester_module.load_stage3_shadow_handoff_scope(
            "root-id", "system-id"
        )
        assert exact_handoff["status"] == "VERIFIED_STAGE3_REQUEST_SCOPE"
        assert exact_handoff["handoffStatus"] == (
            "VERIFIED_COLLECTION_PROGRESS_HANDOFF"
        )

        progress["status"] = "COMPLETED"
        late = harvester_module.load_stage3_shadow_handoff_scope(
            "root-id", "system-id"
        )
        assert late["status"] == "STAGE4_ORDERING_WINDOW_CLOSED"
        assert late["symbols"] == []
        progress["status"] = "PROCESSING"

        progress["trigger_sha256"] = "0" * 64
        rejected = harvester_module.load_stage3_shadow_handoff_scope(
            "root-id", "system-id"
        )
        assert rejected["status"] == "REQUEST_SOURCE_HASH_MISMATCH"
        progress["trigger_sha256"] = source_artifact["sha256"]

        harvester_module.toss_shadow_runtime_decision = lambda _env: (
            True,
            "registered_server_runtime_enabled",
        )
        harvester_module.find_file_id = lambda name, _parent=None: (
            "shadow-id" if name == "TOSS_MARKET_DATA_SHADOW.json" else None
        )
        harvester_module.download_json = lambda _file_id: shadow
        harvester_module.write_json_report = lambda *_args, **_kwargs: None
        harvester_module.inspect_same_stage3_handoff_window = (
            lambda *_args, **_kwargs: {
                "status": "HANDOFF_WINDOW_OPEN",
                "open": True,
                "sourceArtifactMatches": True,
                "safeErrorCategory": None,
            }
        )
        reused = harvester_module.ensure_toss_shadow_market_data(
            "root-id",
            "system-id",
            ["FIXTURE.A", "FIXTURE-B"],
            session=object(),
            request_source_artifact=source_artifact,
        )
        assert reused["runtimeAction"] == "EXISTING_MATCHED_SHADOW_REUSED"
        assert reused["thisRunRequestCounts"] == {
            "oauth": 0,
            "marketCalendar": 0,
            "prices": 0,
        }

        harvester_module.find_file_id = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("offline private detail")
        )
        harvester_module.send_telegram = lambda *_args, **_kwargs: {
            "attempted": False,
            "delivered": False,
            "safeErrorCategory": "config_missing",
        }
        offline = harvester_module.run_toss_same_stage3_collector()
        assert offline["status"] == "TOSS_SHADOW_TRANSIENT_FAILURE"
        assert offline["requestCounts"] == {
            "oauth": 0,
            "marketCalendar": 0,
            "prices": 0,
        }
        assert "offline private detail" not in json.dumps(offline)
    finally:
        harvester_module.find_file_id = original_find
        harvester_module.download_json = original_download
        harvester_module.drive_service = original_drive
        harvester_module.toss_shadow_runtime_decision = original_runtime_decision
        harvester_module.inspect_same_stage3_handoff_window = original_window_inspector
        harvester_module.write_json_report = original_write
        harvester_module.send_telegram = original_send

    with tempfile.TemporaryDirectory() as temp_dir:
        first = reserve_same_stage3_sentinel(
            Path(temp_dir), key, source_artifact, "2026-08-13T00:00:02Z"
        )
        assert first["status"] == "RESERVED"
        duplicate = reserve_same_stage3_sentinel(
            Path(temp_dir), key, source_artifact, "2026-08-13T00:00:03Z"
        )
        assert duplicate["status"] == "IN_PROGRESS"
        finish_same_stage3_sentinel(
            Path(first["path"]),
            status="SUCCESS",
            completed_at="2026-08-13T00:00:04Z",
            artifact_sha256="c" * 64,
        )
        completed = reserve_same_stage3_sentinel(
            Path(temp_dir), key, source_artifact, "2026-08-13T00:00:05Z"
        )
        assert completed["status"] == "SUCCESS"
        completed_payload = json.loads(Path(first["path"]).read_text())
        assert completed_payload["artifactFile"] == "TOSS_MARKET_DATA_SHADOW.json"
        assert completed_payload["artifactHashBasis"] == "CANONICAL_JSON"

        failed_key = "d" * 64
        failed = reserve_same_stage3_sentinel(
            Path(temp_dir), failed_key, source_artifact, "2026-08-13T00:00:06Z"
        )
        finish_same_stage3_sentinel(
            Path(failed["path"]),
            status="FAILED",
            completed_at="2026-08-13T00:00:07Z",
        )
        preserved = reserve_same_stage3_sentinel(
            Path(temp_dir), failed_key, source_artifact, "2026-08-13T00:00:08Z"
        )
        assert preserved["status"] == "FAILED"

    uploads: list[str] = []
    persistence = publish_toss_shadow_with_archive(
        previous_shadow={**shadow, "retrievedAt": "2026-08-12T00:00:00Z"},
        current_shadow={**shadow, "retrievedAt": "2026-08-13T00:00:00Z"},
        uploader=lambda name, _payload: uploads.append(name),
    )
    assert persistence["status"] == "ARCHIVE_AND_CANONICAL_COMPLETE"
    assert uploads[-1] == "TOSS_MARKET_DATA_SHADOW.json"
    assert len(uploads) == 3
    assert persistence["canonicalArtifactSha256"] == hashlib.sha256(
        json.dumps(
            {**shadow, "retrievedAt": "2026-08-13T00:00:00Z"},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert uploads[0] != uploads[1]

    failed_uploads: list[str] = []

    def fail_current_archive(name: str, _payload: dict[str, object]) -> None:
        failed_uploads.append(name)
        if len(failed_uploads) == 2:
            raise RuntimeError("private detail")

    failed_publish = publish_toss_shadow_with_archive(
        previous_shadow=shadow,
        current_shadow=shadow,
        uploader=fail_current_archive,
    )
    assert failed_publish["status"] == "ARCHIVE_OR_CANONICAL_FAILED"
    assert failed_publish["safeErrorCategory"] == "RuntimeError"
    assert "private detail" not in json.dumps(failed_publish)
    assert "TOSS_MARKET_DATA_SHADOW.json" not in failed_uploads

    assert "load_stage3_shadow_handoff_scope" in harvester_source
    assert "toss_shadow_scope = load_latest_stage3_shadow_scope(root_id)" not in (
        harvester_source
    )
    assert "--toss-shadow-collector" in harvester_source
    assert "trigger_sha256" in harvester_source
    assert "trigger_request_scope_sha256" in harvester_source
    assert "EXISTING_MATCHED_SHADOW_REUSED" in harvester_source
    assert "specified_stage3_trigger_missing" in harvester_source
    assert "지정된 trigger_file 미발견" not in harvester_source
    assert "TOSS_SHADOW_PROVIDER_ENABLED: 'false'" in workflow_source
    assert "TOSS_READ_ONLY_CAPABILITY_PROBE_ENABLED: 'false'" in workflow_source
    assert "X-Tossinvest-Account" not in harvester_source
    assert "/api/v1/orders" not in harvester_source

    renamed = build_stage3_request_scope(
        file_name="STAGE3_FUNDAMENTAL_FULL_RENAMED.json",
        payload=_payload("RENAMED.A"),
        drive_created_at="2026-08-13T00:00:01Z",
    )
    assert renamed["status"] == scope["status"]

    print("[TOSS_SAME_STAGE3_HANDOFF] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
