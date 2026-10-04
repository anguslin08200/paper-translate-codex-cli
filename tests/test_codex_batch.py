"""Offline scheduler checks; no Codex process or inference is started."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_batch as batch
from codex_batch_transport import TranslationError


class Monitor:
    def __init__(self):
        self.current = 0

    @contextmanager
    def stage_start(self, name, total):
        self.total = total
        yield self

    def advance(self):
        self.current += 1


class Translator:
    stage_name = "Translate Paragraphs"

    def __init__(self):
        self.translation_config = NS(pool_max_workers=4, debug=False,
            progress_monitor=Monitor(), raise_if_cancelled=lambda: None)
        self.outputs = []

    def pre_translate_paragraph(self, paragraph, tracker, fonts, xfonts):
        return paragraph.source, paragraph.source

    def post_translate_paragraph(self, paragraph, tracker, prepared, output):
        self.outputs.append((paragraph.source, output))


class BatchSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        work = Path(self.directory.name)
        self.args = NS(model="gpt-6.1-sol", effort="low", glossary=None,
            document="offline-test", cache=work / "cache.sqlite", status_file=work / "status.json")
        self.calls, self.created = [], []
        self.events = []

    def factory(self, model, effort, timeout, service_tier="standard"):
        owner = self

        class Session:
            def translate(self, items, glossary):
                owner.calls.append(items)
                return {item["id"]: item["source"] for item in items}

            def close(self):
                pass

        session = Session()
        self.created.append(session)
        return session

    def run_document(self, sources, factory=None):
        translator = Translator()
        docs = NS(page=[NS(pdf_font=[], pdf_xobject=[],
            pdf_paragraph=[NS(source=source) for source in sources])])
        batch.translate_document(translator, docs, self.args,
            lambda prefix, data: self.events.append((prefix, data)), factory or self.factory)
        return translator

    def test_batches_mapping_complete_and_cached_rerun(self):
        sources = [f"Unique paragraph {i}: <style id='1'>x</style> {{v1}}." for i in range(60)]
        translator = self.run_document(sources)
        self.assertEqual(len(self.calls), 3)
        self.assertLessEqual(len(self.created), 4)
        self.assertCountEqual(translator.outputs, [(s, s) for s in sources])
        self.assertEqual(translator.translation_config.progress_monitor.current, 60)
        self.assertTrue(batch.completion_path(self.args.status_file).exists())
        self.assertEqual(sum(data["delta"] for prefix, data in self.events if prefix == "PDF_ACTIVITY "), 0)
        self.calls.clear()
        self.run_document(sources)
        self.assertEqual(self.calls, [])

    def test_packing_bounds(self):
        segments = [batch.Segment(str(i), "x" * 3000, str(i), i) for i in range(17)]
        packed = batch.pack_batches(segments)
        self.assertEqual([len(group) for group in packed], [4, 4, 4, 4, 1])
        self.assertTrue(all(sum(len(s.source) for s in group) <= 12000 for group in packed))

    def test_fatal_error_not_retried_and_no_success_marker(self):
        class Failure:
            def translate(inner, items, glossary):
                self.calls.append(items)
                raise TranslationError("Codex subscription usage limit reached")

            def close(inner):
                pass

        # Simulate a prior success; it cannot survive a failed rerun.
        batch.completion_path(self.args.status_file).write_text('{"success":true}')
        with self.assertRaises(TranslationError):
            self.run_document(["Text"], lambda *a, **k: Failure())
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(batch.completion_path(self.args.status_file).exists())
        self.assertTrue(self.args.status_file.exists())

    def test_transient_retry_creates_new_session(self):
        def factory(*args, **kwargs):
            if self.created:
                return self.factory(*args, **kwargs)

            class Failure:
                def translate(inner, items, glossary):
                    raise TranslationError("connection closed", retryable=True)

                def close(inner):
                    pass

            session = Failure()
            self.created.append(session)
            return session

        translator = self.run_document(["Text"], factory)
        self.assertEqual(len(self.created), 2)
        self.assertEqual(translator.outputs, [("Text", "Text")])

    def test_changed_markers_block_cache_and_completion(self):
        class Broken:
            def translate(inner, items, glossary):
                return {item["id"]: "lost formula" for item in items}

            def close(inner):
                pass

        with self.assertRaises(TranslationError):
            self.run_document(["A <style id='1'>x</style>"], lambda *a, **k: Broken())
        self.assertFalse(batch.completion_path(self.args.status_file).exists())
        store = batch.Store(self.args.cache)
        self.assertEqual(store.db.execute("SELECT count(*) FROM translations").fetchone()[0], 0)
        store.db.close()

    def test_duplicate_source_is_requested_once_and_applied_everywhere(self):
        sources = ["Same source"] * 30
        translator = self.run_document(sources)
        self.assertEqual(sum(len(items) for items in self.calls), 1)
        self.assertEqual(len(translator.outputs), 30)
        event = [data for prefix, data in self.events if prefix == "PDF_BATCH "][-1]
        self.assertEqual(event["deduplicated"], 29)

    def test_balanced_validation_accepts_reordered_markers_without_retry(self):
        class Reordered:
            def translate(inner, items, glossary):
                self.calls.append(items)
                return {item["id"]: "B {v2}, A {v1}." for item in items}
            def close(inner):
                pass
        translator = self.run_document(["A {v1}, B {v2}."], lambda *a, **k: Reordered())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(translator.outputs[0][1], "B {v2}, A {v1}.")

    def test_balanced_validation_rejects_unpaired_tags_and_missing_markers(self):
        from codex_translate import validate
        with self.assertRaises(ValueError):
            validate("<b>x</b><i>y</i>", "<b><i>x</b>y</i>", require_order=False)
        with self.assertRaises(ValueError):
            validate("A {v1}", "A", require_order=False)

    def test_unknown_formula_marker_repair_preserves_real_constraints(self):
        # This reproduces the screenshot: an invented v3 has no corresponding formula object.
        result, removed = batch.validate_batch_output("A {v1}", "甲 {v1} {v3}")
        self.assertEqual(result, "甲 {v1} ")
        self.assertEqual(removed, ["{v3}"])
        for source, translated in (("A {v1}", "甲 {v3}"),
                                   ("A {v1}", "甲 {v1}{v1}"),
                                   ("<b>A</b>", "<b>甲"),
                                   ("A", "{v3}")):
            with self.subTest(source=source), self.assertRaises(ValueError):
                batch.validate_batch_output(source, translated)

    def test_invented_marker_is_repaired_without_another_model_call(self):
        class Invented:
            def translate(inner, items, glossary):
                self.calls.append(items)
                return {item["id"]: item["source"] + "{v3}" for item in items}
            def close(inner):
                pass
        self.args.status_file.write_text('{"previous_failure":"old attempt"}')
        translator = self.run_document(["A {v1}"], lambda *a, **k: Invented())
        self.assertEqual(translator.outputs, [("A {v1}", "A {v1}")])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.args.status_file.read_text(), "{}")

    def test_real_formula_definition_gets_targeted_recomposition(self):
        # Captured Luna failure repeats N in both the multiplier and its definition.
        class Definition:
            def translate(inner, items, glossary):
                self.calls.append(items)
                return {item["id"]: ("大 {v3} 倍，其中 {v3} 是維度" if len(self.calls) < 3
                                    else "大 {v3}（SSM 狀態維度）倍") for item in items}
            def close(inner):
                pass
        translator = self.run_document(["larger by a factor of {v3}, the SSM state dimension"],
                                       lambda *a, **k: Definition())
        self.assertEqual(len(self.calls), 3)
        self.assertIn("repair_instruction", self.calls[-1][0])
        self.assertEqual(translator.outputs[0][1], "大 {v3}（SSM 狀態維度）倍")

    def test_retry_only_broken_item_and_save_valid_item_on_final_failure(self):
        class PartiallyBroken:
            def translate(inner, items, glossary):
                self.calls.append(items)
                return {item["id"]: ("broken" if "style" in item["source"] else item["source"]) for item in items}
            def close(inner):
                pass
        good, bad = "Valid source", "A <style id='1'>formula</style>"
        with self.assertRaises(TranslationError):
            self.run_document([good, bad], lambda *a, **k: PartiallyBroken())
        self.assertEqual([len(items) for items in self.calls], [2, 1])
        self.assertEqual(self.calls[1][0]["source"], bad)
        store = batch.Store(self.args.cache)
        self.assertEqual(store.get(batch.segment_key(batch.scope_for(self.args, ""), good)), good)
        store.db.close()
        self.calls.clear()
        self.run_document([good, bad])
        self.assertEqual([item["source"] for items in self.calls for item in items], [bad])

    def test_failed_item_repair_and_actual_token_totals(self):
        class Repair:
            last_usage = None
            def translate(inner, items, glossary):
                self.calls.append(items)
                inner.last_usage = {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 50,
                                    "reasoningOutputTokens": 5, "totalTokens": 150}
                return {item["id"]: ("broken" if len(self.calls) == 1 and "style" in item["source"] else item["source"]) for item in items}
            def close(inner):
                pass
        translator = self.run_document(["Valid source", "A <style id='1'>formula</style>"], lambda *a, **k: Repair())
        self.assertEqual(len(translator.outputs), 2)
        event = [data for prefix, data in self.events if prefix == "PDF_BATCH "][-1]
        self.assertEqual(event["requests"], 2)
        self.assertEqual(event["usage_requests"], 2)
        self.assertEqual(event["tokens"]["inputTokens"], 200)

    def test_batch_initialization_skips_hello_model_call(self):
        import protected_translate
        import pdf2zh_next.translator.utils as utils
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
        old_translate, old_factory = ILTranslator.translate, utils._create_translator_instance
        self.addCleanup(setattr, ILTranslator, "translate", old_translate)
        self.addCleanup(setattr, utils, "_create_translator_instance", old_factory)
        settings = NS(model_copy=lambda: NS())
        config = NS(clitranslator_command="python codex_translate.py --model gpt-6.1-sol")
        with patch("pdf2zh_next.translator.translator_impl.clitranslator.CLITranslatorTranslator") as constructor:
            protected_translate.install_batching()
            translator, _, _ = utils._create_translator_instance(settings, config, None)
            self.assertIs(translator, constructor.return_value)
            translator.translate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
