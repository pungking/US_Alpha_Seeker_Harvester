"""Offline upload/exit-code regressions; never import credential-loading harvester."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import re
import ssl
import subprocess
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import httplib2
import requests
from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.errors import HttpError
from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC, HttpRequest, MediaIoBaseUpload

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "harvester.py").read_text()
NAMES = {
    "_extract_http_status", "_is_transient_drive_error", "DriveUploadError",
    "_find_unique_upload_file", "_verify_drive_upload", "upload_json",
    "harvester_error_alert",
    "_execute_drive_upload_request",
}
PRIVATE = "private-fixture-filename-parent-response"
PAYLOAD = {"fixture": "value"}
RAW = json.dumps(PAYLOAD, indent=4, ensure_ascii=False).encode()


def load_upload() -> dict:
    nodes = [node for node in ast.parse(SOURCE).body if getattr(node, "name", None) in NAMES]
    scope = dict(io=io, json=json, hashlib=hashlib, re=re, ssl=ssl, requests=requests,
                 HttpError=HttpError, IncompleteRead=IncompleteRead, Any=object,
                 httplib2=httplib2, SimpleNamespace=SimpleNamespace,
                 AuthorizedSession=AuthorizedSession, DEFAULT_HTTP_TIMEOUT_SEC=DEFAULT_HTTP_TIMEOUT_SEC,
                 DRIVE_RETRY_ATTEMPTS=3, MediaIoBaseUpload=Mock(),
                 _rebuild_drive_service=Mock(), _retry_backoff_sleep=Mock(return_value=0))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "harvester.py", "exec"), scope)
    return scope


class UploadContract(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = load_upload()
        self.drive = Mock()
        self.files = self.drive.files.return_value
        self.files.list.return_value.execute.return_value = {"files": [{"id": "fixture-id"}]}
        self.files.get_media.return_value.execute.return_value = RAW
        self.scope["drive_service"] = self.drive
        # Old implementation intentionally remains runnable to demonstrate RED.
        self.scope["find_file_id"] = Mock(return_value="fixture-id")
        self.output = io.StringIO()

    def invoke(self) -> Exception | None:
        with redirect_stdout(self.output):
            try:
                self.assertIsNone(self.scope["upload_json"](PRIVATE, PAYLOAD, PRIVATE))
            except Exception as error:
                self.output.write("".join(traceback.format_exception(error)))
                return error
        return None

    def writes(self) -> int:
        return self.files.update.call_count + self.files.create.call_count

    def assert_safe(self) -> None:
        self.assertNotIn(PRIVATE, self.output.getvalue())

    def test_normal_write(self) -> None:
        self.assertIsNone(self.invoke())
        self.assertEqual(self.writes(), 1)
        self.files.update.return_value.execute.assert_called_once_with(http=ANY, num_retries=0)
        self.files.get_media.assert_not_called()
        self.assert_safe()

    def test_lost_update_response_reconciles_exact_bytes(self) -> None:
        self.files.update.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        self.assertIsNone(self.invoke())
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.files.list.call_count, 2)
        self.files.get_media.return_value.execute.assert_called_once_with(http=ANY, num_retries=0)
        self.assert_safe()

    def test_lost_create_response_never_creates_twice(self) -> None:
        self.scope["find_file_id"].return_value = None
        self.files.list.return_value.execute.side_effect = [
            {"files": []}, {"files": [{"id": "fixture-new-id"}]}]
        self.files.create.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        self.assertIsNone(self.invoke())
        self.assertEqual(self.writes(), 1)
        self.assert_safe()

    def test_unverified_write_fails_without_retry(self) -> None:
        self.files.update.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        for raw in (b"old bytes", b"", None):
            with self.subTest(raw=type(raw).__name__):
                self.files.get_media.return_value.execute.return_value = raw
                error = self.invoke()
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("writeAttempts=1", str(error))
                self.assertIn("cause=BrokenPipeError", str(error))
        self.assertEqual(self.writes(), 3)  # Three independent calls, each one write.
        self.assert_safe()

    def test_ambiguous_preflight_never_writes(self) -> None:
        for result in ({"files": [{"id": "one"}, {"id": "two"}]},
                       {"files": [], "nextPageToken": PRIVATE},
                       {"incompleteSearch": True, "files": []}, {}, {"files": [{}]}):
            with self.subTest(result=result):
                self.files.list.return_value.execute.return_value = result
                self.assertIsInstance(self.invoke(), RuntimeError)
        self.assertEqual(self.writes(), 0)
        self.assert_safe()

    def test_postwrite_identity_drift_or_missing_blocks(self) -> None:
        self.files.update.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        for rows in ([], [{"id": "different"}], [{"id": "one"}, {"id": "two"}]):
            self.files.list.return_value.execute.side_effect = [
                {"files": [{"id": "fixture-id"}]}, {"files": rows}]
            self.assertIsInstance(self.invoke(), RuntimeError)
        self.assertEqual(self.writes(), 3)
        self.files.get_media.assert_not_called()
        self.assert_safe()

    def test_prewrite_transient_retry_is_read_only(self) -> None:
        self.files.list.return_value.execute.side_effect = [
            BrokenPipeError(PRIVATE), {"files": [{"id": "fixture-id"}]}]
        self.assertIsNone(self.invoke())
        self.assertEqual(self.files.list.call_count, 2)
        self.assertEqual(self.writes(), 1)
        self.assert_safe()

    def test_prewrite_exhaustion_reports_actual_counts(self) -> None:
        self.files.list.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        error = self.invoke()
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("lookupAttempts=3", str(error))
        self.assertIn("writeAttempts=0", str(error))
        self.assertEqual(self.writes(), 0)
        self.assert_safe()

    def test_verification_failure_never_replays_upload(self) -> None:
        self.files.update.return_value.execute.side_effect = TimeoutError(PRIVATE)
        self.files.get_media.return_value.execute.side_effect = IncompleteRead(PRIVATE.encode(), 10)
        error = self.invoke()
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("verification=READ_FAILED", str(error))
        self.assertEqual(self.writes(), 1)
        self.assert_safe()

    def test_auth_failure_is_not_retried_or_reconciled(self) -> None:
        self.files.update.return_value.execute.side_effect = HttpError(
            SimpleNamespace(status=403, reason="Forbidden"), PRIVATE.encode())
        error = self.invoke()
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("httpStatus=403", str(error))
        self.assertEqual(self.writes(), 1)
        self.files.get_media.assert_not_called()
        self.scope["_rebuild_drive_service"].assert_not_called()
        self.assert_safe()

    def test_safe_fatal_alert(self) -> None:
        self.assertIn("harvester_error_alert", self.scope)
        self.files.update.return_value.execute.side_effect = BrokenPipeError(PRIVATE)
        self.files.get_media.return_value.execute.return_value = b"mismatch"
        error = self.invoke()
        alert = self.scope["harvester_error_alert"](error)
        self.assertIn("BrokenPipeError", alert)
        self.assertNotIn(PRIVATE, alert)
        self.assertNotIn(PRIVATE, self.scope["harvester_error_alert"](RuntimeError(PRIVATE)))
        self.assertIn("harvester_error_alert(e)", SOURCE)

    def test_other_uncertain_write_failures_reconcile_without_replay(self) -> None:
        errors = [TimeoutError(PRIVATE), ConnectionResetError(PRIVATE),
                  IncompleteRead(PRIVATE.encode(), 10),
                  requests.exceptions.ConnectionError(PRIVATE),
                  HttpError(SimpleNamespace(status=503, reason="Unavailable"), PRIVATE.encode())]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.files.update.return_value.execute.side_effect = error
                self.assertIsNone(self.invoke())
        self.assertEqual(self.writes(), len(errors))
        self.assertEqual(self.files.get_media.call_count, len(errors))
        self.assert_safe()

    def test_prewrite_permanent_failure_reports_one_lookup_zero_writes(self) -> None:
        self.files.list.return_value.execute.side_effect = ValueError(PRIVATE)
        error = self.invoke()
        self.assertIn("lookupAttempts=1", str(error))
        self.assertIn("writeAttempts=0", str(error))
        self.assertEqual(self.writes(), 0)
        self.assert_safe()

    def test_serialization_failure_does_not_touch_drive(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            self.scope["upload_json"](PRIVATE, {"value": object()}, PRIVATE)
        self.assertIn("phase=serialize", str(raised.exception))
        self.drive.files.assert_not_called()

    def test_exact_query_escapes_name_and_parent(self) -> None:
        self.scope["_find_unique_upload_file"]("a'b\\c", "d'e\\f")
        query = self.files.list.call_args.kwargs
        self.assertIn("name = 'a\\'b\\\\c'", query["q"])
        self.assertIn("'d\\'e\\\\f' in parents", query["q"])
        self.assertEqual(query["pageSize"], 2)
        self.assertIn("nextPageToken", query["fields"])
        self.assertIn("incompleteSearch", query["fields"])

    def test_workflow_propagates_pipeline_failure(self) -> None:
        workflow = (ROOT / ".github/workflows/main.yml").read_text()
        step = workflow.split("- name: Run Master Harvester", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("shell: bash", step)
        command = step.split("run: |", 1)[1]
        command = "\n".join(line.strip() for line in command.strip().splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            for code in (0, 7):
                fixture = command.replace("python harvester.py", f"(exit {code})")
                result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", fixture], cwd=tmp, capture_output=True)
                self.assertEqual(result.returncode, code)
        self.assertIn("check_drive_upload_contract.py", (ROOT / ".github/workflows/telegram-routing-ci.yml").read_text())

    def sdk_transport(self, handler: object) -> object:
        """Real SDK/Auth/Requests; replace only the wire with a synthetic response."""
        self.scope["MediaIoBaseUpload"] = MediaIoBaseUpload
        self.drive._http.credentials = AnonymousCredentials()

        def wire(_adapter: object, prepared: object, **_kwargs: object) -> requests.Response:
            status, headers, body = handler(prepared.method, prepared.body)
            response = requests.Response()
            response.status_code, response.headers, response._content = status, headers, body
            response.request = prepared
            return response

        # Assert the underlying adapter disables retries, not only execute().
        with AuthorizedSession(AnonymousCredentials(), max_refresh_attempts=0) as session:
            self.assertEqual(session.get_adapter("https://fixture.invalid").max_retries.total, 0)
        return patch.object(requests.adapters.HTTPAdapter, "send", wire)

    def test_real_sdk_lost_response_has_no_hidden_second_put(self) -> None:
        puts = []

        def handler(method: str, body: object) -> tuple:
            if method == "PATCH":
                return 200, {"location": "https://fixture.invalid/upload"}, b""
            self.assertEqual(method, "PUT")
            puts.append(body.read() if hasattr(body, "read") else body)
            raise RemoteDisconnected(PRIVATE)

        class Connection:
            sock = object()
            def request(self, method: str, uri: str, body: object, headers: object) -> None:
                puts.append(body.read() if hasattr(body, "read") else body)
            def getresponse(self) -> None:
                raise RemoteDisconnected(PRIVATE)
            def connect(self) -> None:
                self.sock = object()
            def close(self) -> None:
                self.sock = None

        connection = Connection()
        def old_wire(uri: str, method: str = "GET", body: object = None, headers: object = None) -> tuple:
            if method == "PATCH":
                return httplib2.Response({"status": "200", "location": "https://fixture.invalid/upload"}), b""
            return httplib2.Http()._conn_request(connection, "/upload", method, body, headers)

        self.files.update.side_effect = lambda **kwargs: HttpRequest(
            http=SimpleNamespace(request=old_wire), postproc=lambda *_: {},
            uri="https://fixture.invalid/files", method="PATCH", body="", headers={},
            resumable=kwargs["media_body"])
        with self.sdk_transport(handler):
            self.assertIsNone(self.invoke())
        self.assertEqual(len(puts), 1, "uncertain PUT must not replay inside the SDK transport")
        self.assertEqual(puts[0], RAW)
        self.assert_safe()

    def test_real_sdk_error_body_does_not_leak_in_logging(self) -> None:
        for body in (PRIVATE.encode(), json.dumps({"error": {"message": PRIVATE}}).encode()):
            old_http = Mock()
            old_http.request.return_value = httplib2.Response({"status": "403"}), body
            self.files.list.return_value = HttpRequest(
                http=old_http, postproc=lambda *_: {}, uri="https://fixture.invalid/files", method="GET")
            logger = logging.getLogger("googleapiclient.http")
            capture = logging.StreamHandler(self.output)
            logger.addHandler(capture)
            try:
                with self.sdk_transport(lambda *_: (403, {}, body)):
                    self.assertIsInstance(self.invoke(), RuntimeError)
            finally:
                logger.removeHandler(capture)
            self.assert_safe()

    def test_real_sdk_successful_resumable_upload(self) -> None:
        methods = []
        def handler(method: str, body: object) -> tuple:
            methods.append(method)
            if method == "PATCH":
                return 200, {"Location": "https://fixture.invalid/upload"}, b""
            self.assertEqual(body.read(), RAW)
            return 200, {}, b'{"id":"fixture-id"}'
        self.files.update.side_effect = lambda **kwargs: HttpRequest(
            http=Mock(), postproc=lambda *_: {}, uri="https://fixture.invalid/files",
            method="PATCH", body="", headers={}, resumable=kwargs["media_body"])
        with self.sdk_transport(handler):
            self.assertIsNone(self.invoke())
        self.assertEqual(methods, ["PATCH", "PUT"])
        self.files.get_media.assert_not_called()
        self.assert_safe()

    def test_real_sdk_auth_and_redirect_never_replay(self) -> None:
        for status in (401, 302, 307):
            methods = []
            def handler(method: str, body: object) -> tuple:
                methods.append(method)
                return status, {"location": "https://fixture.invalid/other"}, PRIVATE.encode()
            self.files.list.return_value = HttpRequest(
                http=Mock(), postproc=lambda *_: {}, uri="https://fixture.invalid/files", method="GET")
            with self.sdk_transport(handler):
                self.assertIsInstance(self.invoke(), RuntimeError)
            self.assertEqual(methods, ["GET"])
            self.assert_safe()


if __name__ == "__main__":
    unittest.main()
