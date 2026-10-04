"""Protect scientific meaning and safe context boundaries without model requests."""
import sys
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_translate as bridge


def _status_worker(path, barrier, sources, error):
    # Spawn separate bridge processes; no inference, CLI or shared in-memory lock is involved.
    barrier.wait(timeout=20)
    for source in sources:
        bridge.update_status(Path(path), source, error)


class TranslationQualityTests(unittest.TestCase):
    def run_status_workers(self, path, batches, error):
        # A spawn barrier forces real cross-process contention on the same JSON report.
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(batches))
        workers = [context.Process(target=_status_worker,
                                   args=(str(path), barrier, batch, error)) for batch in batches]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)
                self.assertFalse(worker.is_alive(), "status writer did not finish within 30 seconds")
                self.assertEqual(worker.exitcode, 0)
        finally:
            # A failed assertion must not leave background test writers running.
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

    def test_concurrent_errors_and_recoveries_keep_other_segments(self):
        # Concurrent success removes only its own hash while every other failure survives.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            batches = [[f"worker-{worker}-segment-{segment}" for segment in range(10)]
                       for worker in range(4)]
            self.run_status_workers(path, batches, "quota exhausted")
            expected = {bridge.hashlib.sha256(source.encode()).hexdigest(): "quota exhausted"
                        for batch in batches for source in batch}
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
            recovery_batches = [batch[:5] for batch in batches]
            self.run_status_workers(path, recovery_batches, None)
            for batch in recovery_batches:
                for source in batch:
                    expected.pop(bridge.hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_disabled_context_does_not_query_database(self):
        # A zero budget is the parallel mode contract, including when no rows exist.
        store = object.__new__(bridge.Store)
        with patch.object(bridge, "sqlite3"):
            self.assertEqual(store.previous("document", 0), [])

    def test_status_lock_contention_is_bounded_and_releases(self):
        # A stuck writer must cause a bounded failure; released locks allow later recovery.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            with bridge._status_lock(path):
                with self.assertRaises(bridge.sqlite3.OperationalError):
                    with bridge._status_lock(path, timeout=0.05):
                        self.fail("a second connection acquired an already held status lock")
            bridge.update_status(path, "segment", "failed")
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def assert_safe_chunks(self, source, limit):
        # Exact reconstruction proves that chunk selection never drops source characters.
        chunks = bridge.split_source(source, limit)
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(0 < len(chunk) <= limit for chunk in chunks))
        self.assertEqual([marker for chunk in chunks for marker in bridge.TOKENS.findall(chunk)],
                         bridge.TOKENS.findall(source))
        return chunks

    def test_prefer_complete_sentence(self):
        # The second sentence should remain whole when the first ends below the budget.
        source = "First result. Second result with evidence."
        self.assertEqual(self.assert_safe_chunks(source, 30),
                         ["First result. ", "Second result with evidence."])

    def test_paragraph_and_unicode_boundaries(self):
        # Paragraph whitespace and Taiwan Chinese punctuation remain byte-for-byte in source.
        source = "First result.\n\nSecond result.\n第三個結果。 第四個結果。"
        chunks = self.assert_safe_chunks(source, 25)
        self.assertEqual(chunks[0], "First result.\n\n")

    def test_nested_adjacent_tags_remain_balanced(self):
        # A tag immediately after a word must not become part of an opaque word atom.
        source = "prefix <span id='v1'><b>gain {v0}</b> and <i>reuse</i></span> suffix"
        chunks = self.assert_safe_chunks(source, 58)
        for chunk in chunks:
            if "<span" in chunk:
                self.assertIn("</span>", chunk)
                self.assertIn("<b>gain {v0}</b>", chunk)
                self.assertIn("<i>reuse</i>", chunk)

    def test_atomic_placeholder_with_spaces(self):
        # Double-brace placeholders are markers even when their contents include spaces.
        source = "one {{formula with spaces}} two three"
        chunks = self.assert_safe_chunks(source, 25)
        self.assertTrue(any("{{formula with spaces}}" in chunk for chunk in chunks))

    def test_self_closing_and_html_void_tags(self):
        # Void tags do not require closing tags; attributed XML self-closing tags also work.
        self.assert_safe_chunks("<b>gain<br>more<img src='x'/><v0 /></b> end", 50)

    def test_oversized_balanced_region_rejected(self):
        # Splitting a large balanced region would destroy independent request validation.
        with self.assertRaisesRegex(ValueError, "balanced tag region"):
            bridge.split_source("<b>" + "word " * 60 + "</b>", 256)

    def test_malformed_nesting_rejected(self):
        # Invalid source nesting must fail explicitly before any request is made.
        for source in ("<b>x", "x</b>", "<b><i>x</b></i>"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Malformed"):
                bridge.split_source(source, 256)

    def test_marker_reordering_rejected(self):
        # Equal marker counts cannot detect formulas moved to a different clause.
        with self.assertRaisesRegex(ValueError, "marker order"):
            bridge.validate("gain {v0} exceeds {v1}", "{v1} 大於 {v0}")

    def test_numeric_prose_and_empty_source(self):
        # Decimal numbers and words remain atomic, and empty input produces no chunks.
        self.assert_safe_chunks("A 1.25 V result is not greater than 2.0 V.", 25)
        self.assertEqual(bridge.split_source("", 256), [])
        with self.assertRaises(ValueError):
            bridge.split_source("source", 0)

    def test_effort_values_are_parsed_without_model_assumptions(self):
        # Compatibility is delegated to account metadata rather than a fixed model list.
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort), patch.object(
                    sys, "argv", ["bridge", "--model", "future-model", "--effort", effort]):
                self.assertEqual(bridge.parse_args().effort, effort)


if __name__ == "__main__":
    unittest.main()
