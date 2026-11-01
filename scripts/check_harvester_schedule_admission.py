"""Offline scheduling contracts. No provider imports or network access."""
import datetime as dt
import hashlib
import json
import socket
import base64
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import harvester_schedule_contract as c
import harvester_schedule_receipts as r

REPO = "fixture-owner/fixture-repo"
HEAD = "a" * 40
NOW = dt.datetime(2026, 10, 30, 0, 0, tzinfo=dt.timezone.utc)


def control_bytes() -> bytes:
    return c.encode({"schemaVersion": "harvester-schedule-control.v1", "repository": REPO,
                     "receiptBranch": c.RECEIPT_BRANCH, "activationAt": "2026-10-01T00:00:00Z",
                     "timezone": "Asia/Seoul", "startWindowSeconds": 10800, "pathVersion": 1})


class MemoryTransport:
    def __init__(self):
        self.repository = REPO
        self.files = {"control.json": control_bytes()}
        self.calls = []
        self.write_status = 201
        self.corrupt_readback = False

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        name = path.split("/contents/", 1)[-1].split("?", 1)[0]
        if method == "GET":
            if name not in self.files: return r.HttpResult(404, {}, b"{}")
            raw = self.files[name]
            if self.corrupt_readback and name != "control.json": raw += b" "
            return r.HttpResult(200, {}, c.encode({"type": "file", "encoding": "base64", "size": len(raw),
                                      "content": base64.b64encode(raw).decode()}))
        if name in self.files: return r.HttpResult(422, {}, b"{}")
        self.files[name] = base64.b64decode(body["content"])
        if self.write_status == 0: raise c.ContractError("TRANSPORT_UNCERTAIN")
        return r.HttpResult(self.write_status, {}, b"{}")


class ReceiptTests(unittest.TestCase):
    def reserve(self, transport):
        return r.reserve_slot(c.due_slot(NOW, REPO), c.digest(control_bytes()), "123", HEAD, lambda: NOW, transport)

    def test_create_201_readback_required(self):
        t = MemoryTransport(); result = self.reserve(t)
        self.assertTrue(result.may_collect)
        self.assertEqual([m for m, _ in t.calls], ["GET", "GET", "PUT", "GET"])
        self.assertTrue(r.recheck_admission(result.proof, lambda: NOW, t).may_collect)
        before = len(t.calls)
        done = r.finish_slot(result.proof, {"collectorExit": "success", "workflowJobResult": "success", "secPhase": "skipped"}, lambda: NOW+dt.timedelta(days=1), t)
        self.assertEqual(done.status, "RESULT_PERSISTED")
        self.assertFalse(done.may_collect)
        self.assertEqual(len(t.calls)-before, 5)
        result_raw = c.decode(t.files[result.proof.slot.attempt_path.replace("attempt", "result")])
        self.assertFalse(result_raw["sourceCoverageVerified"])

    def test_existing_any_receipt_blocks(self):
        for raw in (b"bad", b"{}", b'{"status":"failed"}'):
            t = MemoryTransport(); t.files[c.due_slot(NOW, REPO).attempt_path] = raw
            with self.assertRaises(c.ContractError): self.reserve(t)
            self.assertNotIn("PUT", [m for m, _ in t.calls])

    def test_branch_control_missing_blocks(self):
        t = MemoryTransport(); t.files.clear()
        with self.assertRaises(c.ContractError): self.reserve(t)
        self.assertEqual(len(t.calls), 1)

    def test_control_digest_mismatch(self):
        t = MemoryTransport(); t.files["control.json"] += b" "
        with self.assertRaises(c.ContractError): self.reserve(t)

    def test_create_timeout_no_replay(self):
        for status in (0, 409, 422, 200, 302):
            t = MemoryTransport(); t.write_status = status
            with self.assertRaises(c.ContractError): self.reserve(t)
            self.assertEqual([m for m, _ in t.calls].count("PUT"), 1)
            with self.assertRaises(c.ContractError): self.reserve(t)
            self.assertEqual([m for m, _ in t.calls].count("PUT"), 1)

    def test_different_run_cannot_adopt_reservation(self):
        t = MemoryTransport(); proof = self.reserve(t).proof
        bad = c.AdmissionProof(proof.slot, proof.control_sha256, "456", HEAD, proof.attempt_sha256)
        with self.assertRaises(c.ContractError): r.recheck_admission(bad, lambda: NOW, t)
        t.files[proof.slot.attempt_path] += b" "
        with self.assertRaises(c.ContractError): r.recheck_admission(proof, lambda: NOW, t)

    def test_concurrent_requests_use_one_attempt(self):
        t = MemoryTransport(); self.assertTrue(self.reserve(t).may_collect)
        with self.assertRaises(c.ContractError): self.reserve(t)
        self.assertEqual([m for m, _ in t.calls].count("PUT"), 1)

    def test_redaction_allowlist_and_budget(self):
        t = MemoryTransport(); proof = self.reserve(t).proof
        with self.assertRaises(c.ContractError):
            r.finish_slot(proof, {"secret": "fixture-bearer-secret"}, lambda: NOW, t)
        for path in ("https://evil.invalid", f"/repos/{REPO}/contents/../secret", f"/repos/{REPO}/contents/control.json"):
            with self.assertRaises(c.ContractError): r.GitHubTransport(REPO, "fixture-token", "sender").request("PUT", path, {})

    def test_transport_no_redirect_retry_and_size_bound(self):
        seen = []
        class Response:
            status_code = 302
            headers = {"Location": "https://evil.invalid"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def iter_content(self, size): yield b"{}"
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def request(self, method, url, **kwargs):
                seen.append((method, url, kwargs, self.trust_env)); return Response()
        fake = SimpleNamespace(Session=Session, RequestException=OSError)
        t = r.GitHubTransport(REPO, "fixture-token", "resolve")
        with patch.dict(sys.modules, {"requests": fake}):
            with self.assertRaises(c.ContractError): r.read_run_metadata(t, REPO, "123", HEAD)
            with self.assertRaises(c.ContractError): r.read_run_metadata(t, REPO, "123", HEAD)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0][2]["allow_redirects"]); self.assertFalse(seen[0][3])
        self.assertEqual(seen[0][2]["timeout"], 15)
        t = r.GitHubTransport(REPO, "fixture-token", "sender")
        with patch.dict(sys.modules, {"requests": fake}), patch.object(Response, "iter_content", return_value=[b"x"*65537]):
            with self.assertRaises(c.ContractError): t.request("GET", r.content_path(REPO, "control.json"))

    def test_corrupt_readback_and_expiry_block(self):
        t = MemoryTransport(); t.corrupt_readback = True
        with self.assertRaises(c.ContractError): self.reserve(t)
        self.assertEqual([m for m, _ in t.calls].count("PUT"), 1)
        t = MemoryTransport(); proof = self.reserve(t).proof
        with self.assertRaises(c.ContractError): r.recheck_admission(proof, lambda: NOW+dt.timedelta(days=1), t)


class ContractTests(unittest.TestCase):
    def route(self, event="schedule", body=None, **kwargs):
        return c.resolve_route(event, body or {"schedule": c.CRONS["first"]},
                               kwargs.get("ref", "refs/heads/main"), kwargs.get("attempt", 1),
                               kwargs.get("created", NOW), kwargs.get("now", NOW),
                               kwargs.get("mode", "hybrid"), REPO)

    def test_first_second_utc_mapping(self):
        first = c.due_slot(NOW, REPO)
        self.assertEqual(c.stamp(first.scheduled_for), "2026-10-29T22:13:00Z")
        second = c.due_slot(c.parse_time("2026-10-30T03:13:00Z"), REPO)
        self.assertEqual(second.batch, "second")

    def test_half_open_window(self):
        for time, expected in [("2026-10-29T23:00:00Z", True), ("2026-10-30T01:13:00Z", False),
                               ("2026-10-30T03:13:00Z", True), ("2026-10-30T06:13:00Z", False)]:
            self.assertEqual(c.due_slot(c.parse_time(time), REPO) is not None, expected)
        self.assertIsNone(c.due_slot(c.parse_time("2026-11-01T00:00:00Z"), REPO))
        self.assertIsNone(c.due_slot(c.parse_time("2026-11-02T00:00:00Z"), REPO))

    def test_native_wake_basis(self):
        self.assertEqual(self.route().slot.basis, "RECEIVER_SELECTED_DUE_SLOT")
        for body in ({"schedule": "bad"}, {"schedule": c.CRONS["second"]}):
            with self.assertRaises(c.ContractError): self.route(body=body)
        with self.assertRaises(c.ContractError): self.route(created=NOW+dt.timedelta(seconds=1))

    def test_assist_main_attempt_and_payload(self):
        body = {"action": c.ASSIST_ACTION, "client_payload": {"schemaVersion": c.REQUEST_SCHEMA,
                "scheduledFor": "2026-10-29T22:13:00Z", "batch": "first"}}
        self.assertEqual(self.route("repository_dispatch", body).kind, "hybrid_daily")
        for kw in ({"ref": "refs/heads/other"}, {"attempt": 2}, {"attempt": True}):
            with self.assertRaises(c.ContractError): self.route("repository_dispatch", body, **kw)
        body["client_payload"]["repository"] = "attacker/repo"
        with self.assertRaises(c.ContractError): self.route("repository_dispatch", body)

    def test_manual_no_bypass(self):
        for mode in ("all", "auto", "first"):
            with self.assertRaises(c.ContractError): self.route("workflow_dispatch", {"inputs": {"batch_mode": mode}})
        r = self.route("workflow_dispatch", {"inputs": {"batch_mode": "first", "scheduled_for": "2026-10-29T22:13:00Z"}})
        self.assertEqual(r.slot.slot_sha256, self.route().slot.slot_sha256)

    def test_legacy_defaults(self):
        self.assertEqual(self.route(mode="legacy").kind, "legacy_daily")
        with self.assertRaises(c.ContractError): self.route("repository_dispatch", {"action": c.ASSIST_ACTION}, mode="legacy")
        with self.assertRaises(c.ContractError): self.route(mode="unknown")

    def test_slot_hash_host_invariance(self):
        self.assertEqual(c.due_slot(NOW, REPO).slot_sha256, self.route().slot.slot_sha256)

    def test_control_and_proof_schema(self):
        raw = control_bytes()
        control = c.validate_control(raw, hashlib.sha256(raw).hexdigest(), REPO)
        proof = c.AdmissionProof(self.route().slot, control.sha256, "123", HEAD, "b"*64)
        self.assertEqual(c.validate_proof(c.encode(proof.wire()), control, "123", HEAD, NOW), proof)
        for raw in (raw+b" ", b"{}", b'{"a":1,"a":2}'):
            with self.assertRaises(c.ContractError): c.validate_control(raw, control.sha256, REPO)
        bad = proof.wire(); bad["runAttempt"] = True
        with self.assertRaises(c.ContractError): c.validate_proof(c.encode(bad), control, "123", HEAD, NOW)

    def test_stage3_does_not_claim_daily_slot(self):
        r = self.route("repository_dispatch", {"action": "stage3_completed"})
        self.assertEqual(r.kind, "stage3"); self.assertIsNone(r.slot)
        with self.assertRaises(c.ContractError): self.route("repository_dispatch", {"action": "unknown"})

    def test_sec_gate_matrix(self):
        for enabled in (False, True):
            for success in (False, True):
                self.assertFalse(c.sec_phase_allowed(self.route(), success, enabled))
                t = c.parse_time("2026-10-30T03:13:00Z")
                r = self.route(body={"schedule": c.CRONS["second"]}, created=t, now=t)
                self.assertEqual(c.sec_phase_allowed(r, success, enabled), success and enabled)


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
        unittest.main()
