"""Offline scheduling contracts. No provider imports or network access."""
import datetime as dt
import hashlib
import json
import socket
import unittest
from unittest.mock import patch

import harvester_schedule_contract as c

REPO = "fixture-owner/fixture-repo"
HEAD = "a" * 40
NOW = dt.datetime(2026, 10, 30, 0, 0, tzinfo=dt.timezone.utc)


def control_bytes() -> bytes:
    return c.encode({"schemaVersion": "harvester-schedule-control.v1", "repository": REPO,
                     "receiptBranch": c.RECEIPT_BRANCH, "activationAt": "2026-10-01T00:00:00Z",
                     "timezone": "Asia/Seoul", "startWindowSeconds": 10800, "pathVersion": 1})


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
