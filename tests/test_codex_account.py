"""Protocol regressions for the read-only Codex status integration."""
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_account as account


class AccountTests(unittest.TestCase):
    def server(self, responses):
        # Fake only the transport, retaining real request IDs and response validation.
        server = account.AppServer.__new__(account.AppServer)
        server.counter = 0
        server.deadline = time.monotonic() + 2
        server.responses = queue.Queue()
        server.process = Mock()
        server.process.stdin = io.StringIO()
        server.process.stdout = io.StringIO()
        server.reader = Mock()
        for response in responses:
            server.responses.put(response)
        return server

    def fetch(self, responses):
        server = self.server(responses)
        with patch.object(account, "AppServer", return_value=server), patch.object(server, "close"):
            result = account.fetch_account_snapshot()
        sent = [json.loads(line) for line in server.process.stdin.getvalue().splitlines()]
        return result, sent

    def test_pagination_preserves_model_efforts_and_quota_buckets(self):
        # Returned capabilities and native IDs must survive two pages without hardcoded choices.
        model = {"id": "native-id", "model": "native-model", "isDefault": True,
                 "supportedReasoningEfforts": [{"reasoningEffort": "none", "description": "fast"}]}
        bucket = {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 42}}
        result, sent = self.fetch([
            {"id": 1, "result": {}}, {"method": "account/updated", "params": {}},
            {"id": 2, "result": {"rateLimits": bucket, "rateLimitsByLimitId": {"codex": bucket}}},
            {"id": 3, "result": {"data": [model], "nextCursor": "next"}},
            {"id": 4, "result": {"data": [{"id": "second"}], "nextCursor": None}}])
        self.assertEqual(result["models"], [model, {"id": "second"}])
        self.assertEqual(result["rateLimitsByLimitId"], {"codex": bucket})
        self.assertEqual(result["errors"], {})
        self.assertEqual(sent[1], {"method": "initialized"})
        self.assertEqual(sent[-1]["params"]["cursor"], "next")
        self.assertEqual({message["method"] for message in sent},
                         {"initialize", "initialized", "account/rateLimits/read", "model/list"})

    def test_rate_limit_error_does_not_hide_model_catalog(self):
        # Login or quota read failures are explicit while independent model discovery survives.
        result, _ = self.fetch([{ "id": 1, "result": {}},
                               {"id": 2, "error": {"code": -1, "message": "Login required"}},
                               {"id": 3, "result": {"data": [{"id": "native"}]}}])
        self.assertIsNone(result["rateLimits"])
        self.assertEqual(result["errors"]["account/rateLimits/read"], "Login required")
        self.assertEqual(result["models"], [{"id": "native"}])

    def test_repeated_cursor_rejects_incomplete_catalog(self):
        # A server pagination bug must not loop indefinitely or publish only some choices.
        result, _ = self.fetch([{ "id": 1, "result": {}},
                               {"id": 2, "result": {"rateLimits": {}}},
                               {"id": 3, "result": {"data": [], "nextCursor": "again"}},
                               {"id": 4, "result": {"data": [], "nextCursor": "again"}}])
        self.assertEqual(result["models"], [])
        self.assertIn("pagination", result["errors"]["model/list"])

    def test_invalid_result_and_eof_are_explicit(self):
        # Malformed responses and EOF are actionable protocol errors, never zero usage.
        for response in ({"id": 1, "result": []}, RuntimeError("closed stream")):
            with self.assertRaises(RuntimeError):
                self.server([response]).request("initialize")

    def test_timeout_cleans_up_server(self):
        # Initialization timeout still invokes cleanup and returns a readable failure.
        server = self.server([])
        server.deadline = time.monotonic() + 0.01
        with patch.object(account, "AppServer", return_value=server), patch.object(server, "close") as close:
            result = account.fetch_account_snapshot()
        close.assert_called_once()
        self.assertIn("timed out", result["errors"]["app-server"])

    def test_stalled_process_cleanup_kills_tree_on_windows(self):
        # npm's launcher can spawn a native child, so Windows cleanup must kill the tree.
        server = self.server([])
        server.process.pid = 123
        server.process.wait.side_effect = [subprocess.TimeoutExpired("codex", 1), 0]
        with patch.object(account.os, "name", "nt"), patch.object(account.subprocess, "run") as run:
            server.close()
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"])
        self.assertTrue(server.process.stdin.closed)
        self.assertTrue(server.process.stdout.closed)

    def test_transport_strips_api_billing_environment(self):
        # Use the existing subscription guard, never an inherited API key or auth file parser.
        process = Mock()
        process.stdout = io.StringIO("")
        with patch.object(account, "codex_command", return_value=["codex"]), \
                patch.object(account, "subscription_environment", return_value={"SAFE": "1"}), \
                patch.object(account.subprocess, "Popen", return_value=process) as popen:
            server = account.AppServer(2)
            server.reader.join(1)
        self.assertEqual(popen.call_args.kwargs["env"], {"SAFE": "1"})
        self.assertEqual(popen.call_args.args[0][1:4], ["app-server", "--listen", "stdio://"])


if __name__ == "__main__":
    unittest.main()
