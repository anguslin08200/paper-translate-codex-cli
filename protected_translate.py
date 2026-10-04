"""Run installed pdf2zh with original diagram/table glyphs kept out of typesetting."""
from __future__ import annotations
import json
import os
import sys
import threading
import logging
from traditional_chinese import to_traditional
from contextlib import nullcontext


PROTECTED_LABELS = {"figure", "chart", "table", "reference", "figure_text",
                    "figure_text_hybrid", "table_text", "table_cell", "table_cell_hybrid",
                    "wired_table_cell", "wireless_table_cell"}
OUTPUT_LOCK = threading.Lock()


def install_traditional_output() -> None:
    from pdf2zh_next.translator.base_translator import BaseTranslator
    # Include local/API responses and upstream cache hits before PDF typesetting.
    for method_name in ("translate", "llm_translate"):
        original = getattr(BaseTranslator, method_name)
        def traditional(self, *args, _original=original, **kwargs):
            result = _original(self, *args, **kwargs)
            return to_traditional(result) if isinstance(result, str) else result
        setattr(BaseTranslator, method_name, traditional)


def inject_api_secret() -> None:
    # Pass the key through the child environment; never expose it in OS command arguments.
    # Windows PDF workers inherit this environment so they install the same redactor.
    secret = os.environ.get("PDF_TRANSLATE_DEEPSEEK_API_KEY", "")
    if secret:
        if __name__ == "__main__":
            sys.argv.extend(["--deepseek-api-key", secret])
        original_factory = logging.getLogRecordFactory()
        def redacted_record(*args, **kwargs):
            record = original_factory(*args, **kwargs)
            record.msg = record.getMessage().replace(secret, "[已隱藏金鑰]")
            record.args = ()
            return record
        logging.setLogRecordFactory(redacted_record)


def emit(prefix, data):
    # One locked write avoids mixing JSON lines from concurrent paragraph workers.
    with OUTPUT_LOCK:
        sys.stdout.write(prefix + json.dumps(data) + "\n")
        sys.stdout.flush()


def contains_center(region, box) -> bool:
    # All BabelDOC IL boxes use the same PDF coordinate system.
    x, y = (box.x + box.x2) / 2, (box.y + box.y2) / 2
    return region.x <= x <= region.x2 and region.y <= y <= region.y2


def install_protection() -> None:
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    original = ParagraphFinder._group_characters_into_paragraphs

    def group(self, page, layout_index, layout_map):
        # Keep protected glyphs in page.pdf_character, where their original placement survives.
        # This acts before formula/style extraction; merely skipping the model would be too late.
        regions = [layout.box for layout in page.page_layout
                   if layout.class_name in PROTECTED_LABELS]
        regions.extend(figure.box for figure in page.pdf_figure)
        kept, prose = [], []
        for char in page.pdf_character:
            box = char.visual_bbox.box
            (kept if any(contains_center(region, box) for region in regions) else prose).append(char)
        page.pdf_character = prose
        try:
            return original(self, page, layout_index, layout_map)
        finally:
            page.pdf_character.extend(kept)

    # Patch only this isolated translation process; never modify installed third-party files.
    ParagraphFinder._group_characters_into_paragraphs = group


def install_progress() -> None:
    import pdf2zh_next.high_level as high_level
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    original_paragraph = ILTranslator.translate_paragraph

    def paragraph(self, *args, **kwargs):
        # Active tasks include any rate-limit wait and are not claimed to be completed translations.
        emit("PDF_ACTIVITY ", {"delta": 1})
        try:
            return original_paragraph(self, *args, **kwargs)
        finally:
            emit("PDF_ACTIVITY ", {"delta": -1})

    ILTranslator.translate_paragraph = paragraph

    def create_handler(config):
        # Forward the real progress IPC events as one JSON line, without Rich's wrapped output.
        def handler(event):
            if event.get("type", "").startswith("progress_"):
                fields = {key: event.get(key) for key in ("type", "stage", "stage_current",
                          "stage_total", "stage_progress", "overall_progress", "part_index", "total_parts")}
                emit("PDF_PROGRESS ", fields)
        return nullcontext(), handler

    high_level.create_progress_handler = create_handler


def install_batching() -> None:
    from types import SimpleNamespace
    import pdf2zh_next.translator.utils as translator_utils
    from pdf2zh_next.translator.translator_impl.clitranslator import CLITranslatorTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from codex_batch import options_for, translate_document
    original = ILTranslator.translate
    original_factory = translator_utils._create_translator_instance

    def create_translator(settings, translator_config, rate_limiter, enforce_glossary_support=True):
        command = getattr(translator_config, "clitranslator_command", "")
        if options_for(SimpleNamespace(command_string=command)) is None:
            return original_factory(settings, translator_config, rate_limiter, enforce_glossary_support)
        # The GUI checks login once; batch startup validates the service without a paid Hello turn.
        configured = settings.model_copy()
        configured.translate_engine_settings = translator_config
        return CLITranslatorTranslator(configured, rate_limiter), None, None

    translator_utils._create_translator_instance = create_translator

    def translate(self, docs):
        options = options_for(self.translate_engine)
        if options is None:
            return original(self, docs)
        # Prepare placeholders once, then bypass the per-paragraph external CLI adapter entirely.
        try:
            return translate_document(self, docs, options, emit)
        except Exception:
            # Setup errors can precede scheduler reporting; never allow a stale success marker.
            from codex_batch import completion_path
            marker = completion_path(options.status_file)
            if marker:
                marker.unlink(missing_ok=True)
            raise

    ILTranslator.translate = translate


# Windows multiprocessing reimports the entry point under __mp_main__.
# Install protection in that child too, where PDF processing actually happens.
if __name__ in ("__main__", "__mp_main__"):
    inject_api_secret()
    install_traditional_output()
    install_protection()
    install_progress()
    install_batching()

if __name__ == "__main__":
    # Children inherit UTF-8 and a sensible log width rather than legacy console encoding.
    os.environ["PYTHONUTF8"] = "1"
    os.environ["COLUMNS"] = "160"
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    from pdf2zh_next.main import cli
    cli()
