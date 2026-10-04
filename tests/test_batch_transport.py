"""Protocol-only tests: the fake app-server never invokes a model."""
from __future__ import annotations

import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

import codex_batch_transport as batch


class _Output:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                break
            yield line

    def put(self, message):
        self.lines.put(json.dumps(message) + "\n")

    def close(self):
        self.lines.put(None)


class _Input:
    def __init__(self, process):
        self.process = process
        self.buffer = ""

    def write(self, value):
        self.buffer += value

    def flush(self):
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.process.receive(json.loads(line))

    def close(self):
        self.process.stdout.close()


class _Process:
    def __init__(self, *, outcome="success", ids=None):
        self.stdout = _Output()
        self.stdin = _Input(self)
        self.messages = []
        self.outcome = outcome
        self.ids = ids
        self.threads = 0
        self.pid = 123

    def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            self.stdout.put({"id": message["id"], "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            self.threads += 1
            self.stdout.put({"method": "thread/started", "params": {
                "thread": {"id": f"thread-{self.threads}"}}})
            self.stdout.put({"id": message["id"], "result": {
                "thread": {"id": f"thread-{self.threads}"}}})
        elif method == "turn/start":
            params = message["params"]
            thread = params["threadId"]
            turn = f"turn-{self.threads}"
            # Notifications can precede the request response on the same pipe.
            if self.outcome == "tool":
                self.stdout.put({"id": 901, "method": "item/commandExecution/requestApproval",
                                 "params": {"threadId": thread, "turnId": turn}})
            elif self.outcome == "timeout":
                pass
            elif self.outcome in ("failed", "quota"):
                info = {"type": "UsageLimitExceeded"} if self.outcome == "quota" else {
                    "type": "ResponseStreamDisconnected"}
                self.stdout.put({"method": "turn/completed", "params": {
                    "threadId": thread, "turn": {"id": turn, "status": "failed",
                                                "error": {"codexErrorInfo": info,
                                                          "message": "sensitive source text"}}}})
            else:
                keys = self.ids if self.ids is not None else params["outputSchema"]["required"]
                result = {key: f"譯文-{key}" for key in keys}
                self.stdout.put({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn, "tokenUsage": {"total": {
                        "inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 50,
                        "reasoningOutputTokens": 5, "totalTokens": 150}}}})
                self.stdout.put({"method": "item/completed", "params": {
                    "threadId": thread, "turnId": turn, "item": {
                        "type": "agentMessage", "phase": "final_answer",
                        "text": json.dumps(result)}}})
                self.stdout.put({"method": "turn/completed", "params": {
                    "threadId": thread, "turn": {"id": turn, "status": "completed", "items": []}}})
            self.stdout.put({"id": message["id"], "result": {
                "turn": {"id": turn, "status": "inProgress"}}})

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.stdout.close()


class BatchTransportTests(unittest.TestCase):
    def setUp(self):
        # 離線協定測試不依賴開發者電腦的 Codex 安裝，也不需要 CI 登入。
        command = patch.object(batch, "codex_command", return_value=["synthetic-codex"])
        command.start()
        self.addCleanup(command.stop)

    def test_explicit_speed_tier_overrides_host_settings(self):
        # Verify both directions so a globally enabled Fast setting cannot leak into Standard.
        for selected, native, enabled in (("standard", "default", "false"), ("fast", "priority", "true")):
            with self.subTest(tier=selected), patch.object(batch.subprocess, "Popen", return_value=_Process()) as launch:
                with batch.BatchSession("gpt-6.1-sol", "low", timeout=1, service_tier=selected):
                    command = launch.call_args.args[0]
                    self.assertIn('service_tier="' + native + '"', command)
                    self.assertIn("features.fast_mode=" + enabled, command)

    def session(self, fake, timeout=1):
        patcher = patch.object(batch.subprocess, "Popen", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return batch.BatchSession("gpt-6.1-sol", "low", timeout)

    def test_framing_schema_reuse_and_fresh_threads(self):
        fake = _Process()
        with self.session(fake) as session:
            one = session.translate([{"id": "a", "source": "A <style id='1'>x</style>"},
                                     {"id": "b", "source": "B"}], "throughput=吞吐量")
            two = session.translate([{"id": "c", "source": "C"}])
            self.assertEqual(session.last_usage["inputTokens"], 100)
        self.assertEqual(one, {"a": "譯文-a", "b": "譯文-b"})
        self.assertEqual(two, {"c": "譯文-c"})
        self.assertEqual([m.get("method") for m in fake.messages].count("initialize"), 1)
        starts = [m["params"] for m in fake.messages if m.get("method") == "thread/start"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(s["ephemeral"] and s["model"] == "gpt-6.1-sol" for s in starts))
        self.assertTrue(all(s["sandbox"] == "read-only" for s in starts))
        turns = [m["params"] for m in fake.messages if m.get("method") == "turn/start"]
        self.assertEqual([t["threadId"] for t in turns], ["thread-1", "thread-2"])
        self.assertEqual(turns[0]["outputSchema"]["required"], ["a", "b"])
        self.assertFalse(turns[0]["outputSchema"]["additionalProperties"])
        self.assertEqual(turns[0]["effort"], "low")

    def test_missing_id_is_rejected(self):
        fake = _Process(ids=["a"])
        with self.session(fake) as session:
            with self.assertRaisesRegex(batch.TranslationError, "IDs") as caught:
                session.translate([{"id": "a", "source": "A"}, {"id": "b", "source": "B"}])
        self.assertTrue(caught.exception.retryable)

    def test_transient_failed_turn_is_retryable_and_sanitized(self):
        with self.session(_Process(outcome="failed")) as session:
            with self.assertRaises(batch.TranslationError) as caught:
                session.translate([{"id": "a", "source": "sensitive source text"}])
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("sensitive", str(caught.exception))

    def test_usage_limit_is_fatal(self):
        with self.session(_Process(outcome="quota")) as session:
            with self.assertRaises(batch.TranslationError) as caught:
                session.translate([{"id": "a", "source": "A"}])
        self.assertFalse(caught.exception.retryable)
        self.assertIn("usage limit", str(caught.exception))

    def test_timeout_and_tool_refusal(self):
        with self.session(_Process(outcome="timeout"), timeout=0.02) as session:
            with self.assertRaisesRegex(batch.TranslationError, "timed out") as caught:
                session.translate([{"id": "a", "source": "A"}])
        self.assertTrue(caught.exception.retryable)
        fake = _Process(outcome="tool")
        with self.session(fake) as session:
            with self.assertRaisesRegex(batch.TranslationError, "requested a tool"):
                session.translate([{"id": "a", "source": "A"}])
        self.assertIn({"id": 901, "result": {"decision": "decline"}}, fake.messages)

    def test_external_close_aborts_waiting_translation(self):
        fake = _Process(outcome="timeout")
        session = self.session(fake, timeout=5)
        result = []

        def waiting_translate():
            try:
                session.translate([{"id": "a", "source": "A"}])
            except batch.TranslationError as exc:
                result.append(exc)

        worker = threading.Thread(target=waiting_translate)
        worker.start()
        # Wait for the fake turn/start request, then close the session externally.
        until = time.monotonic() + 1
        while not any(m.get("method") == "turn/start" for m in fake.messages):
            if time.monotonic() > until:
                self.fail("fake turn did not start")
            time.sleep(0.001)
        session.close()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result and result[0].retryable)


if __name__ == "__main__":
    unittest.main()
