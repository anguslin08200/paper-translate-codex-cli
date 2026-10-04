"""Check quota semantics, dynamic effort selection and diagram glyph protection."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_translate_gui as gui
import protected_translate as protection
import codex_translate as bridge


class Value:
    def __init__(self, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class QualityTests(unittest.TestCase):
    def test_structured_progress_is_phase_completion(self):
        # A translation phase percentage is never presented as final file completion.
        app = object.__new__(gui.TranslatorGUI)
        app.active_count = 0
        app.progress = NS(stop=lambda: None, configure=lambda **values: None)
        app.progress_var = Value()
        app._show_elapsed = lambda: None
        app._update_progress('PDF_ACTIVITY {"delta": 1}')
        app._update_progress('PDF_ACTIVITY {"delta": 1}')
        self.assertEqual(app.active_count, 2)
        app._update_progress('PDF_PROGRESS {"stage":"Translate Paragraphs", "stage_current":5,"stage_total":10,"stage_progress":50,"overall_progress":22}')
        self.assertEqual(app.progress_var.get(), 22)
        self.assertIn("5/10", app.phase_detail)
        self.assertIn("本階段", app.phase_detail)

    def test_equivalent_style_syntax_restores_original(self):
        # Equivalent quote syntax is safe to repair, while semantic tag changes are rejected.
        self.assertEqual(bridge.validate("<style id='1'>gain</style>", '<style id="1">增益</style>'),
                         "<style id='1'>增益</style>")
        with self.assertRaises(ValueError):
            bridge.validate("<style id='1'>gain</style>", '<style id="2">增益</style>')
    def test_quota_remaining_and_unknown(self):
        # Missing limits cannot appear as a full allowance; out-of-range values are clamped.
        self.assertEqual(gui.quota_rows({}), [])
        rows = gui.quota_rows({"rateLimits": {"primary": {"usedPercent": 82, "windowDurationMins": 300},
                                             "secondary": {"usedPercent": None}}})
        self.assertEqual(rows[0][1], 18)
        self.assertIn("5 小時", rows[0][0])
        self.assertIsNone(rows[1][1])
        self.assertEqual(gui.quota_rows({"rateLimitsByLimitId": {"reserve": {
            "primary": {"usedPercent": 120, "resetsAt": "invalid"}}}})[0][1:], (0, "未知"))

    def test_effort_uses_model_metadata(self):
        # Unsupported none is visibly corrected in the selector before a job is started.
        app = object.__new__(gui.TranslatorGUI)
        app.model_var = Value(next(iter(gui.MODEL_OPTIONS)))
        app.codex_model_var = Value("native-test-model")
        app.codex_models = {"native-test-model": {"supportedReasoningEfforts": [
            {"reasoningEffort": "low"}, {"reasoningEffort": "medium"}], "defaultReasoningEffort": "medium"}}
        app.effort_var = Value("無（速度優先）")
        app.effort_note_var = Value()
        options = {}
        app.effort_combo = NS(configure=lambda **values: options.update(values))
        app._update_effort_options()
        self.assertNotIn("無（速度優先）", options["values"])
        self.assertEqual(gui.EFFORT_OPTIONS[app.effort_var.get()], "low")

    def test_original_diagram_glyphs_bypass_grouping(self):
        # Protected source glyphs must retain object identity and original position.
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
        box = lambda a,b,c,d: NS(x=a,y=b,x2=c,y2=d)
        diagram = NS(visual_bbox=NS(box=box(2,2,4,4)), char_unicode="R")
        prose = NS(visual_bbox=NS(box=box(20,20,22,22)), char_unicode="text")
        page = NS(page_layout=[NS(box=box(0,0,10,10), class_name="figure")],
                  pdf_figure=[], pdf_character=[diagram, prose])
        seen = []
        def group(instance, page, *args):
            seen.extend(page.pdf_character)
            page.pdf_character = []
            return ["paragraph"]
        with patch.object(ParagraphFinder, "_group_characters_into_paragraphs", group):
            protection.install_protection()
            result = ParagraphFinder._group_characters_into_paragraphs(None, page, None, None)
        self.assertEqual(seen, [prose])
        self.assertEqual(page.pdf_character, [diagram])
        self.assertEqual(result, ["paragraph"])


if __name__ == "__main__":
    unittest.main()
