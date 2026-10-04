"""Regression tests for subscription billing, bounded context and GUI job snapshots."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_translate as bridge


class BridgeTests(unittest.TestCase):
    def test_preserve_formula_and_tags(self):
        # Layout markers must survive even when their surrounding prose changes.
        self.assertEqual(bridge.validate("gain {v0} <b>x</b>", "增益 {v0} <b>x</b>"),
                         "增益 {v0} <b>x</b>")
        with self.assertRaises(ValueError):
            bridge.validate("gain {v0}", "增益")
        with self.assertRaises(ValueError):
            bridge.validate("<b>x</b>", "<b>x")

    def test_rich_text_tag_order(self):
        # Equal tag counts are insufficient if their nesting changes.
        with self.assertRaises(ValueError):
            bridge.validate("<b><i>gain</i></b>", "<i><b>增益</b></i>")

    def test_failure_report_clears_only_recovered_segment(self):
        # A successful retry must not hide another paragraph's failure.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            bridge.update_status(path, "first", "quota error")
            bridge.update_status(path, "second", "placeholder error")
            bridge.update_status(path, "first", None)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(report), 1)
            self.assertEqual(next(iter(report.values())), "placeholder error")
            bridge.update_status(path, "second", None)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})

    def test_reject_empty_output(self):
        # Empty model replies cannot be accepted as completed translations.
        with self.assertRaises(ValueError):
            bridge.validate("text", " ")

    def test_chunking_keeps_source_and_markers(self):
        # Every source character must survive splitting, including XML attributes and whitespace.
        source = ("amplifier <span id='v1'> gain {v0} </span>\n" * 30)
        chunks = bridge.split_source(source, 256)
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(len(chunk) <= 256 for chunk in chunks))
        self.assertEqual(sum((bridge.Counter(bridge.TOKENS.findall(c)) for c in chunks),
                             bridge.Counter()), bridge.Counter(bridge.TOKENS.findall(source)))

    def test_reject_oversized_formula(self):
        # Never silently truncate an unbreakable formula or tag.
        with self.assertRaises(ValueError):
            bridge.split_source("x" * 257, 256)

    def test_context_isolation_and_budget(self):
        # Context from other PDF scopes must never appear in this document.
        with tempfile.TemporaryDirectory() as directory:
            store = bridge.Store(Path(directory) / "cache.sqlite3")
            try:
                store.put("a", "doc-a", "source-a" * 500, "translation-a" * 500)
                store.put("b", "doc-b", "private-source-b", "private-translation-b")
                store.put("c", "doc-a", "source-c" * 500, "translation-c" * 500)
                context = store.previous("doc-a", 2000)
                self.assertEqual(len(context), 2)
                self.assertLessEqual(sum(len(x["source"]) + len(x["translation"]) for x in context), 2000)
                self.assertNotIn("private", json.dumps(context))
                self.assertEqual(store.previous("doc-c", 2000), [])
                self.assertEqual(store.get("b"), "private-translation-b")
                store.put("b", "doc-b", "overwrite", "wrong")
                self.assertEqual(store.get("b"), "private-translation-b")
            finally:
                store.db.close()

    def test_remove_api_billing_environment(self):
        # Parent API credentials must not turn a subscription run into usage-based billing.
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "CODEX_API_KEY": "test-key",
                                     "OPENAI_BASE_URL": "https://example.invalid", "KEEP_ME": "yes"}):
            env = bridge.subscription_environment()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_api_login_is_rejected(self):
        # A valid API-key login still must fail this provider's subscription requirement.
        result = subprocess.CompletedProcess([], 0, "", "Logged in using an API key")
        with patch.object(bridge.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError):
                bridge.ensure_login(["codex"], {})

    def test_subscription_login_is_accepted(self):
        # The CLI's status channel can be stderr or stdout.
        result = subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT")
        with patch.object(bridge.subprocess, "run", return_value=result):
            bridge.ensure_login(["codex"], {})

    def test_cache_hit_skips_model_and_auth(self):
        # Rerunning a completed segment should not spend quota or require connectivity.
        with tempfile.TemporaryDirectory() as directory:
            argv = ["bridge", "--cache", str(Path(directory) / "cache.sqlite3"),
                    "--document", "pdf-hash"]
            def streams():
                return io.TextIOWrapper(io.BytesIO(b"source"), encoding="utf-8"), io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with patch.object(sys, "argv", argv), patch.object(bridge, "codex_command", return_value=["codex"]), patch.object(bridge, "ensure_login") as login, patch.object(bridge, "run_codex", return_value="譯文") as model:
                for _ in range(2):
                    stdin, stdout = streams()
                    with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
                        self.assertEqual(bridge.main(), 0)
                self.assertEqual(login.call_count, 1)
                self.assertEqual(model.call_count, 1)

    def test_schema_and_ephemeral_flags(self):
        # Only the schema's translation string may enter PDFMathTranslate stdout.
        args = type("Args", (), {"model": "", "effort": "low", "timeout": 1})()
        captured = {}
        class FakeProcess:
            returncode = 0
            def __init__(self, command, **kwargs):
                captured["command"] = command
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"translation": "增益 {v0}"}), encoding="utf-8")
            def communicate(self, prompt, timeout):
                captured["prompt"] = prompt
                return "", ""
        with patch.object(bridge.subprocess, "Popen", FakeProcess):
            self.assertEqual(bridge.run_codex("gain {v0}", [], "", args, ["codex"], {}),
                             "增益 {v0}")
        command = captured["command"]
        for flag in ("--ephemeral", "--output-schema", "--ignore-user-config"):
            self.assertIn(flag, command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn("features.shell_tool=false", command)


class GuiTests(unittest.TestCase):
    def test_both_entrypoints_use_job_snapshot_and_quoted_paths(self):
        # Exercise command building without creating a Tk window or starting a local model.
        # The public package has one maintained GUI, excluding old local backup copies.
        for number, relative in enumerate(("pdf_translate_gui.py",)):
            spec = importlib.util.spec_from_file_location("gui_test_" + str(number), ROOT / relative)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            gui = object.__new__(module.TranslatorGUI)
            gui.job = {"mode": "local", "model": "qwen3.8:27b-q4_K_M", "effort": "none",
                       "dual": False}
            gui._ensure_ollama = lambda: None
            with patch.object(module, "PYTHON", Path("C:/path with spaces/python.exe")):
                command = gui._build_command(Path("paper.pdf"), Path("output"))
            parsed = shlex.split(command[command.index("--clitranslator-command") + 1])
            self.assertEqual(parsed[0], "C:/path with spaces/python.exe")
            self.assertIn("--no-dual", command)

    def test_swallowed_failure_does_not_replace_outputs(self):
        # Simulate upstream returning code zero after a failed segment.
        spec = importlib.util.spec_from_file_location("gui_failure", ROOT / "pdf_translate_gui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gui = object.__new__(module.TranslatorGUI)
        gui.closing = False
        gui.stop_requested = False
        events = []
        gui._post = lambda *args: events.append(args)
        class FakeProcess:
            stdout = []
            def wait(self):
                return 0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"pdf")
            target = Path(directory) / "existing.pdf"
            target.write_bytes(b"existing output")
            gui.job = {"mono": target, "dual_target": Path(directory) / "dual.pdf", "dual": True}
            def build_command(source, temporary):
                (temporary / "codex-status.json").write_text('{"segment": "quota exhausted"}')
                (temporary / "source.mono.pdf").write_bytes(b"bad output")
                return ["fake"]
            gui._build_command = build_command
            with patch.object(module.subprocess, "Popen", return_value=FakeProcess()):
                gui._run_translation(source)
            self.assertEqual(target.read_bytes(), b"existing output")
            self.assertEqual(events[-1][0].__name__, "_failed")
            self.assertIn("quota exhausted", events[-1][1])
            self.assertFalse(list(Path(directory).glob(".pdf2zh-*")))

    def test_missing_batch_completion_does_not_replace_output(self):
        # An upstream zero exit and a PDF file alone cannot prove every model batch completed.
        spec = importlib.util.spec_from_file_location("gui_batch_missing", ROOT / "pdf_translate_gui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gui = object.__new__(module.TranslatorGUI)
        gui.closing, gui.stop_requested = False, False
        events = []
        gui._post = lambda *args: events.append(args)
        class FakeProcess:
            stdout = []
            def wait(self):
                return 0
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.pdf", Path(directory) / "target.pdf"
            source.write_bytes(b"source")
            target.write_bytes(b"existing")
            gui.job = {"mode": "codex", "dual": False, "mono": target, "dual_target": target}
            def build_command(source, temporary):
                (temporary / "source.mono.pdf").write_bytes(b"incomplete")
                return ["fake"]
            gui._build_command = build_command
            with patch.object(module.subprocess, "Popen", return_value=FakeProcess()), patch.dict(os.environ, {"PDF_TRANSLATE_LEGACY": "0"}):
                gui._run_translation(source)
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(events[-1][0].__name__, "_failed")
            self.assertIn("批次", events[-1][1])

    def test_codex_job_hash_and_glossary(self):
        # The same source bytes produce a document scope independent of filename.
        spec = importlib.util.spec_from_file_location("gui_codex", ROOT / "pdf_translate_gui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gui = object.__new__(module.TranslatorGUI)
        gui.job = {"mode": "codex", "model": "", "effort": "none", "dual": True,
                   "codex_model": "", "glossary": "C:/term files/glossary.txt"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paper.pdf"
            source.write_bytes(b"test pdf")
            with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                command = gui._build_command(source, Path(directory))
            parsed = shlex.split(command[command.index("--clitranslator-command") + 1])
            self.assertEqual(parsed[parsed.index("--document") + 1],
                             bridge.hashlib.sha256(b"test pdf").hexdigest())
            # The selected effort is forwarded exactly instead of silently rewritten.
            self.assertEqual(parsed[parsed.index("--effort") + 1], "none")
            self.assertEqual(parsed[parsed.index("--glossary") + 1], "C:/term files/glossary.txt")


if __name__ == "__main__":
    unittest.main()
