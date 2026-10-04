"""Batch prepared BabelDOC paragraphs through reusable, isolated Codex workers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import threading
import time

from codex_translate import Store, TOKENS, split_source, update_status, validate

BATCH_POLICY = "taiwan-science-batch-v5-english-terms"


def validate_batch_output(source, output):
    # Paragraph-local formula IDs have no PDF object when absent from this source.
    # Remove only those invented IDs, then require every real marker and balanced tag.
    # Existing duplicate IDs, missing formulas and altered style tags are never guessed.
    allowed = set(TOKENS.findall(source))
    removed = []

    def remove_unknown(match):
        token = match.group()
        if token in allowed:
            return token
        removed.append(token)
        return ""

    cleaned = re.sub(r"\{v\d+\}|\[v\d+\]", remove_unknown, output)
    return validate(source, cleaned, require_order=False), removed


def only_duplicate_formulas(source, translated):
    # A last repair is warranted only for repeated real formulas, never missing content or tags.
    expected, actual = Counter(TOKENS.findall(source)), Counter(TOKENS.findall(translated))
    extra = actual - expected
    return bool(extra) and not (expected - actual) and all(
        token in expected and re.fullmatch(r"\{v\d+\}|\[v\d+\]", token) for token in extra)


def completion_path(status_file):
    return Path(status_file).with_suffix(".complete.json") if status_file else None


@dataclass
class Segment:
    id: str
    source: str
    key: str
    paragraph_index: int


@dataclass
class BatchResult:
    accepted: dict[str, str]
    errors: dict[str, str]
    requests: int
    tokens: dict[str, int] = field(default_factory=dict)
    usage_requests: int = 0


def options_for(engine):
    # Activate only for the installed Codex bridge, keeping local/API providers unchanged.
    parts = shlex.split(getattr(engine, "command_string", ""))
    bridge = next((i for i, part in enumerate(parts) if Path(part).name == "codex_translate.py"), None)
    if bridge is None or os.environ.get("PDF_TRANSLATE_LEGACY") == "1":
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="low")
    # Speed tier changes scheduling, so it deliberately stays outside translation cache keys.
    parser.add_argument("--service-tier", choices=("standard", "fast"), default="standard")
    parser.add_argument("--document", default="standalone")
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                        / "PDFMathTranslate/codex-cache.sqlite3")
    return parser.parse_known_args(parts[bridge + 1:])[0]


def pack_batches(segments, max_chars=12000, max_items=24):
    # Combine nearby short paragraphs while bounding both output schema size and input volume.
    result, current, characters = [], [], 0
    for segment in segments:
        if len(segment.source) > max_chars:
            raise ValueError("Segment exceeds batch input limit")
        if current and (len(current) >= max_items or characters + len(segment.source) > max_chars):
            result.append(current)
            current, characters = [], 0
        current.append(segment)
        characters += len(segment.source)
    if current:
        result.append(current)
    return result


def scope_for(args, glossary, policy=BATCH_POLICY):
    # Worker count does not change cache identity; every batch uses a fresh model thread.
    fields = [policy, args.document, args.model, args.effort, glossary]
    if policy == "taiwan-science-v2":
        fields.extend([4000, 0])
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def segment_key(scope, source):
    return hashlib.sha256((scope + source).encode("utf-8")).hexdigest()


def translate_document(translator, docs, args, emit, session_factory=None):
    from babeldoc.format.pdf.document_il.midend.il_translator import DocumentTranslateTracker
    from codex_batch_transport import BatchSession, TranslationError
    factory = session_factory or BatchSession
    marker = completion_path(args.status_file)
    # A previous success must never authorize publishing this job after an interrupted rerun.
    if marker:
        marker.unlink(missing_ok=True)
    if not args.model:
        raise ValueError("Batch translation requires an explicit account model")
    glossary = args.glossary.read_text(encoding="utf-8") if args.glossary else ""
    if len(glossary) > 4000:
        raise ValueError("Glossary exceeds 4000 characters")
    translator.docs = docs
    tracker = DocumentTranslateTracker()
    total = sum(len(page.pdf_paragraph) for page in docs.page)
    workers = min(8, max(1, int(translator.translation_config.pool_max_workers)))
    started = time.monotonic()
    sessions, local, session_lock = [], threading.local(), threading.Lock()
    abort = threading.Event()
    fatal_reason = []
    prepared, pending, values, aliases = [], [], {}, {}
    store = Store(args.cache)
    scope = scope_for(args, glossary)
    # 術語政策改變後不可搬移舊中文術語快取；僅重用同一新政策的結果。
    old_scopes = []
    completed_batches, cache_hits, deduplicated, requests = 0, 0, 0, 0
    token_totals, usage_requests = {}, 0

    def report(batch_count):
        # Partial usage is labelled with its reported-request count, including on failed jobs.
        emit("PDF_BATCH ", {"completed": completed_batches, "total": batch_count,
            "cache_hits": cache_hits, "deduplicated": deduplicated, "requests": requests,
            "tokens": token_totals or None, "usage_requests": usage_requests,
            "workers": workers, "elapsed": round(time.monotonic() - started, 2)})

    def request(batch):
        # A thread keeps one process, but the transport creates a fresh ephemeral thread per batch.
        if abort.is_set():
            raise TranslationError(fatal_reason[0] if fatal_reason else "Translation canceled")
        if not hasattr(local, "session"):
            # Real long batches exceeded two minutes; avoid canceling and paying for a full retry.
            session = factory(args.model, args.effort, timeout=300,
                              service_tier=getattr(args, "service_tier", "standard"))
            with session_lock:
                sessions.append(session)
            local.session = session
        remaining, accepted, errors, calls = list(batch), {}, {}, 0
        rejected = {}
        tokens, reported = {}, 0
        emit("PDF_ACTIVITY ", {"delta": len(batch)})
        try:
            for attempt in range(3):
                translator.translation_config.raise_if_cancelled()
                if abort.is_set():
                    raise TranslationError("Translation canceled")
                payload = [{"id": segment.id, "source": segment.source} for segment in remaining]
                # Supply an explicit per-item namespace before the first call as well as repair.
                for item in payload:
                    item["required_markers"] = TOKENS.findall(item["source"])
                if attempt:
                    # Explicit repair constraints avoid redoing accepted text or guessing marker order.
                    for item in payload:
                        item["required_markers"] = TOKENS.findall(item["source"])
                        # Show the defective result and exact constraint failure for targeted repair.
                        item["previous_translation"] = rejected.get(item["id"], "")
                        item["validation_error"] = errors.get(item["id"], "")
                        if attempt == 2:
                            # Definitions can induce repeated formulas; request clause recomposition.
                            repeated = next(iter(Counter(TOKENS.findall(rejected[item["id"]]))
                                                 - Counter(item["required_markers"])))
                            item["repair_instruction"] = (
                                "Recompose the clause that repeats a formula. Merge the definition into "
                                f"one occurrence of {repeated} with its actual source definition adjacent "
                                "or in parentheses. Do not invent a definition. Use each marker exactly "
                                "as many times as required_markers lists it. Refer back using words "
                                "if needed; do not repeat its marker to explain it. Preserve all meaning.")
                try:
                    calls += 1
                    call_started = time.monotonic()
                    emit("PDF_REQUEST ", {"batch": batch[0].id, "attempt": attempt + 1, "state": "start",
                        "items": len(payload), "input_chars": sum(len(item["source"]) for item in payload)})
                    try:
                        output = local.session.translate(payload, glossary)
                    finally:
                        # Include failed requests if the server reports their token use too.
                        usage = getattr(local.session, "last_usage", None)
                        if isinstance(usage, dict) and usage:
                            reported += 1
                            for key, number in usage.items():
                                tokens[key] = tokens.get(key, 0) + number
                        emit("PDF_REQUEST ", {"batch": batch[0].id, "attempt": attempt + 1, "state": "end",
                            "elapsed": round(time.monotonic() - call_started, 2), "tokens": usage})
                    if set(output) != {segment.id for segment in remaining}:
                        raise TranslationError("Batch response has missing or unexpected paragraph IDs")
                    failed = []
                    errors = {}
                    for segment in remaining:
                        try:
                            accepted[segment.id], removed = validate_batch_output(segment.source, output[segment.id])
                            if removed:
                                # Emit marker-only diagnostics without logging document content.
                                emit("PDF_REPAIR ", {"id": segment.id, "removed_invented_markers": removed})
                        except ValueError as exc:
                            rejected[segment.id] = output[segment.id]
                            errors[segment.id] = str(exc)
                            failed.append(segment)
                            diagnostics = os.environ.get("PDF_TRANSLATE_DIAGNOSTICS_DIR")
                            if diagnostics:
                                # Only an explicit local trial saves rejected text for root-cause review.
                                path = Path(diagnostics) / "rejected-translations.jsonl"
                                with session_lock:
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    with path.open("a", encoding="utf-8") as stream:
                                        stream.write(json.dumps({"id": segment.id, "attempt": attempt + 1,
                                            "source": segment.source, "output": output[segment.id], "error": str(exc)},
                                            ensure_ascii=False) + "\n")
                    # Keep accepted text and spend the second request only on broken markers.
                    remaining = failed
                    if not remaining:
                        return BatchResult(accepted, {}, calls, tokens, reported)
                    if attempt == 1 and not all(only_duplicate_formulas(s.source, rejected[s.id]) for s in remaining):
                        # Other damage retains the existing two-attempt limit and blocks publication.
                        break
                except TranslationError as exc:
                    errors = {segment.id: str(exc) for segment in remaining}
                    if not exc.retryable or attempt or "timed out" in str(exc).lower():
                        # Stop queued work immediately on fatal service/protocol errors.
                        # Preserve its diagnostic even if a canceled future reaches the controller first.
                        with session_lock:
                            if not fatal_reason:
                                fatal_reason.append(str(exc))
                        abort.set()
                        return BatchResult(accepted, errors, calls, tokens, reported)
                    # A single bounded retry is reserved for transport/service transient errors.
                    if abort.wait(0.5):
                        raise TranslationError("Translation canceled")
                    local.session.close()
                    local.session = factory(args.model, args.effort, timeout=300,
                                            service_tier=getattr(args, "service_tier", "standard"))
                    with session_lock:
                        sessions.append(local.session)
            return BatchResult(accepted, errors, calls, tokens, reported)
        finally:
            emit("PDF_ACTIVITY ", {"delta": -len(batch)})

    def apply_ready(pbar):
        # PDF mutation stays on the controller thread and only starts after all chunks are valid.
        for item in prepared:
            if item["applied"] or not all(segment.id in values for segment in item["segments"]):
                continue
            translated = validate(item["source"], "\n".join(values[segment.id] for segment in item["segments"]), require_order=False)
            llm_tracker = item["tracker"].new_llm_translate_tracker()
            llm_tracker.set_input(item["source"])
            llm_tracker.set_output(translated)
            translator.post_translate_paragraph(item["paragraph"], item["tracker"], item["input"], translated)
            item["applied"] = True
            pbar.advance()

    try:
        with translator.translation_config.progress_monitor.stage_start(translator.stage_name, total) as pbar:
            for page in docs.page:
                page_tracker = tracker.new_page()
                fonts = {font.font_id: font for font in page.pdf_font}
                xfonts = {obj.xobj_id: {**fonts, **{font.font_id: font for font in obj.pdf_font}}
                          for obj in page.pdf_xobject}
                for paragraph in page.pdf_paragraph:
                    translator.translation_config.raise_if_cancelled()
                    paragraph_tracker = page_tracker.new_paragraph()
                    source, translation_input = translator.pre_translate_paragraph(paragraph, paragraph_tracker, fonts, xfonts)
                    if source is None:
                        pbar.advance()
                        continue
                    index = len(prepared)
                    chunks = split_source(source, 4000)
                    segments = []
                    for chunk_index, chunk in enumerate(chunks):
                        segment = Segment(f"p{index:05d}c{chunk_index:03d}", chunk, segment_key(scope, chunk), index)
                        segments.append(segment)
                        cached = store.get(segment.key)
                        if cached is None:
                            cached = next((value for old_scope in old_scopes
                                if (value := store.get(segment_key(old_scope, chunk))) is not None), None)
                        if cached is not None:
                            try:
                                values[segment.id] = validate(chunk, cached, require_order=False)
                                cache_hits += 1
                                continue
                            except ValueError:
                                # Remove only this invalid batch cache entry so a valid replacement can persist.
                                store.db.execute("DELETE FROM translations WHERE key=?", (segment.key,))
                                store.db.commit()
                        # Exact duplicate source in this document shares one validated translation.
                        # Preserve each paragraph's own PDF input, styles and destination object.
                        if segment.key in aliases:
                            aliases[segment.key].append(segment)
                            deduplicated += 1
                        else:
                            aliases[segment.key] = [segment]
                            pending.append(segment)
                    prepared.append({"paragraph": paragraph, "tracker": paragraph_tracker, "input": translation_input,
                                     "source": source, "segments": segments, "applied": False})
            apply_ready(pbar)
            batches = pack_batches(pending)
            report(len(batches))
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(request, batch): batch for batch in batches}
            # Independent valid batches must persist even when marker repair fails elsewhere.
            validation_failures = []
            try:
                for future in as_completed(futures):
                    translator.translation_config.raise_if_cancelled()
                    batch = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        abort.set()
                        # Failure reporting occurs before exiting, so swallowed upstream errors cannot publish.
                        update_status(args.status_file, batch[0].source, str(exc)[:500])
                        raise
                    rows = []
                    for segment in batch:
                        if segment.id not in result.accepted:
                            continue
                        output = result.accepted[segment.id]
                        for alias in aliases[segment.key]:
                            values[alias.id] = output
                        rows.append((segment.key, scope, segment.source, output))
                    # Commit one accepted batch instead of doing a filesystem sync for every segment.
                    store.db.executemany("INSERT OR IGNORE INTO translations VALUES (?,?,?,?)", rows)
                    store.db.commit()
                    requests += result.requests
                    usage_requests += result.usage_requests
                    for key, number in result.tokens.items():
                        token_totals[key] = token_totals.get(key, 0) + number
                    # Save successful items even if a second attempt still fails on another item.
                    # A later rerun will only request the unaccepted source instead of paying twice.
                    if result.errors:
                        report(len(batches))
                        error = next(iter(result.errors.values()))
                        for segment in batch:
                            if segment.id in result.errors:
                                update_status(args.status_file, segment.source, result.errors[segment.id])
                        if abort.is_set():
                            raise TranslationError(error)
                        validation_failures.append(error)
                        continue
                    completed_batches += 1
                    apply_ready(pbar)
                    report(len(batches))
                if validation_failures:
                    raise TranslationError(validation_failures[0])
            except BaseException:
                # Also cancel pending requests when PDF application or cancellation fails.
                abort.set()
                raise
            finally:
                if abort.is_set():
                    for future in futures:
                        future.cancel()
                    for session in sessions:
                        try:
                            session.close()
                        except Exception:
                            pass
                executor.shutdown(wait=True, cancel_futures=True)
        if translator.translation_config.debug:
            path = translator.translation_config.get_working_file_path("translate_tracking.json")
            Path(path).write_text(tracker.to_json(), encoding="utf-8")
        if not all(item["applied"] for item in prepared):
            raise RuntimeError("Not all prepared paragraphs were applied")
        if marker:
            # All prepared paragraphs are validated now; discard earlier attempts' stale errors.
            # Atomically replace the report before writing the affirmative completion marker.
            status = Path(args.status_file)
            temporary = status.with_name(status.name + ".success.tmp")
            temporary.write_text("{}", encoding="utf-8")
            os.replace(temporary, status)
            # The GUI requires affirmative completion as well as validated PDF output files.
            marker.write_text(json.dumps({"success": True, "paragraphs": total,
                "batches": completed_batches, "cache_hits": cache_hits,
                "deduplicated": deduplicated, "requests": requests,
                "tokens": token_totals or None, "usage_requests": usage_requests,
                "elapsed": round(time.monotonic() - started, 2)}), encoding="utf-8")
    except Exception as exc:
        # Cover preparation, cache and protocol failures as well as completed model requests.
        update_status(args.status_file, "batch-document", str(exc)[:500])
        raise
    finally:
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        store.db.close()
