"""Desktop GUI for layout-preserving PDF translation."""

from __future__ import annotations

import os
import base64
import ctypes
import ctypes.wintypes
import json
import shutil
import subprocess
import threading
import queue
import shlex
import hashlib
import tkinter as tk
import uuid
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Query official account metadata without reading login credentials.
from codex_account import fetch_account_snapshot
from traditional_chinese import to_traditional
from terminology import ENGLISH_TERMS_POLICY


APP_DIR = Path(__file__).resolve().parent
PDF2ZH = APP_DIR / ".venv" / "Scripts" / "pdf2zh_next.exe"
PYTHON = APP_DIR / ".venv" / "Scripts" / "python.exe"
QWEN_BRIDGE = APP_DIR / "qwen36_translate.py"
# Resolve a standard Ollama installation instead of depending on the author's folder layout.
OLLAMA = Path(shutil.which("ollama") or shutil.which("ollama.exe") or "ollama.exe")
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "PDFMathTranslate"
CONFIG_FILE = CONFIG_DIR / "gui_config.json"

# Codex uses the official CLI and existing ChatGPT subscription login.
CODEX_BRIDGE = APP_DIR / "codex_translate.py"
PROTECTED_RUNNER = APP_DIR / "protected_translate.py"

MODEL_OPTIONS = {
    "Codex（ChatGPT 訂閱額度）": ("codex", ""),
    "Qwen 3.8 27B Q4_K_M（本地，推薦）": ("local", "qwen3.8:27b-q4_K_M"),
    "Qwen 3.6 36B（本地，較慢）": ("local", "qwen3.6:latest"),
    "Qwen 3.5 9B（本地，較快）": ("local", "qwen3.5:9b"),
    "DeepSeek API": ("deepseek", "deepseek-chat"),
}
EFFORT_OPTIONS = {
    "低（推薦論文翻譯）": "low",
    "無（速度優先）": "none",
    "最小": "minimal",
    "中": "medium",
    "高": "high",
    "極高": "xhigh",
    "最大": "max",
}


def enable_high_dpi() -> None:
    # Set awareness before creating Tk so Windows does not bitmap-stretch text.
    if os.name == "nt":
        try:
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def quota_rows(snapshot: dict) -> list[tuple[str, float | None, str]]:
    # A missing window is unknown, never an invented 100% remaining quota.
    buckets = snapshot.get("rateLimitsByLimitId") or {}
    if not buckets and snapshot.get("rateLimits"):
        buckets = {"codex": snapshot["rateLimits"]}
    rows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limitName") or bucket.get("limitId") or bucket_id
        for key, title in (("primary", "主要"), ("secondary", "次要")):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            duration = window.get("windowDurationMins")
            label = f"{name} · {duration / 60:g} 小時" if isinstance(duration, (int, float)) else f"{name} · {title}視窗"
            used = window.get("usedPercent")
            remaining = max(0.0, min(100.0, 100.0 - used)) if isinstance(used, (int, float)) else None
            reset = window.get("resetsAt")
            # Convert the service timestamp to the user's Windows local time.
            try:
                reset_text = datetime.fromtimestamp(reset).strftime("%m/%d %H:%M") if reset is not None else "未知"
            except (TypeError, ValueError, OSError, OverflowError):
                reset_text = "未知"
            rows.append((label, remaining, reset_text))
    return rows

# Keep technical terminology recognizable while translating prose for Taiwan readers.
TRADITIONAL_TECHNICAL_PROMPT = (
    "Translate the prose into Traditional Chinese as used in Taiwan. "
    + ENGLISH_TERMS_POLICY +
    "Preserve formulas, symbols, citations, "
    "tags, and placeholders exactly. Output only the translation."
)


class DataBlob(ctypes.Structure):
    """Windows DATA_BLOB used by the current-user DPAPI."""

    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _protect_secret(secret: str) -> str:
    # Encrypt API keys with the signed-in Windows account before writing configuration.
    raw = secret.encode("utf-8")
    source = ctypes.create_string_buffer(raw)
    source_blob = DataBlob(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    output_blob = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _unprotect_secret(encoded: str) -> str:
    # Only the same Windows user account can decrypt this DPAPI payload.
    encrypted = base64.b64decode(encoded)
    source = ctypes.create_string_buffer(encrypted)
    source_blob = DataBlob(len(encrypted), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    output_blob = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


class TranslatorGUI(tk.Tk):
    def __init__(self) -> None:
        enable_high_dpi()
        super().__init__()
        self.title("論文 PDF 繁體中文翻譯")
        # Scale the layout as well as text on high-DPI laptop and external displays.
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry(f"{int(900 * scale)}x{min(int(780 * scale), self.winfo_screenheight() - 80)}")
        self.minsize(int(760 * scale), min(int(640 * scale), self.winfo_screenheight() - 80))
        self.process: subprocess.Popen[str] | None = None
        self.ollama_process: subprocess.Popen[bytes] | None = None
        self.stop_requested = False
        self.closing = False
        # Dispatch worker events on Tk's main thread without calling Tk from workers.
        self.events = queue.Queue()
        self.job = {}
        self.codex_models = {}
        self.account_refreshing = False
        self.account_snapshot = None
        self.started_at = None
        self.phase_text = ""
        self.phase_detail = ""
        self.progress_status_path = None
        self.active_count = 0
        self.after(100, self._drain_events)
        # Closing the window must also stop background translation/model processes.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.file_var = tk.StringVar()
        self.output_var = tk.StringVar(value="請選擇 PDF")
        self.model_var = tk.StringVar(value=next(iter(MODEL_OPTIONS)))
        self.effort_var = tk.StringVar(value=next(iter(EFFORT_OPTIONS)))
        self.deepseek_model_var = tk.StringVar(value="deepseek-chat")
        self.api_key_var = tk.StringVar()
        # An empty model uses the official CLI default rather than assuming account access.
        self.codex_model_var = tk.StringVar()
        self.glossary_var = tk.StringVar()
        self.dual_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就緒")
        self.progress_var = tk.DoubleVar(value=0)
        self.parallel_var = tk.StringVar(value="8")
        # Keep speed tier separate from reasoning effort and persist it for the next job.
        self.speed_var = tk.StringVar(value="標準（節省額度）")
        self.batch_detail = ""
        self.token_detail = ""
        self.account_var = tk.StringVar(value="額度尚未讀取")
        self.effort_note_var = tk.StringVar()

        self._load_config()
        self._build_ui()
        self.model_var.trace_add("write", lambda *_: self._update_model_fields())
        self.file_var.trace_add("write", lambda *_: self._update_output_preview())
        self._update_model_fields()
        self.codex_model_var.trace_add("write", lambda *_: self._update_effort_options())
        self.after(300, self._refresh_account)
        self.after(1000, self._tick_elapsed)

    def _build_ui(self) -> None:
        # Use a compact grid so the controls remain readable on laptop displays.
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(9, weight=1)

        ttk.Label(root, text="論文 PDF 繁體中文翻譯", font=("Microsoft JhengHei UI", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
        )
        ttk.Button(root, text="高畫質 PDF 預覽…", command=self._preview_pdf).grid(row=0, column=2, sticky="e")
        ttk.Label(root, text="PDF 檔案").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(root, textvariable=self.file_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(root, text="選擇…", command=self._choose_file).grid(row=1, column=2)

        ttk.Label(root, text="輸出位置").grid(row=2, column=0, sticky="nw", pady=5)
        ttk.Label(root, textvariable=self.output_var, wraplength=620).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=8, pady=5
        )

        ttk.Label(root, text="模型").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(
            root, textvariable=self.model_var, values=list(MODEL_OPTIONS), state="readonly"
        ).grid(row=3, column=1, columnspan=2, sticky="ew", padx=8)

        ttk.Label(root, text="Effort").grid(row=4, column=0, sticky="w", pady=5)
        self.effort_combo = ttk.Combobox(
            root, textvariable=self.effort_var, values=list(EFFORT_OPTIONS), state="readonly"
        )
        self.effort_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=8)

        self.deepseek_frame = ttk.LabelFrame(root, text="DeepSeek API", padding=10)
        self.deepseek_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        self.deepseek_frame.columnconfigure(1, weight=1)
        ttk.Label(self.deepseek_frame, text="模型名稱").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.deepseek_frame, textvariable=self.deepseek_model_var).grid(
            row=0, column=1, sticky="ew", padx=8, pady=3
        )
        ttk.Label(self.deepseek_frame, text="API Key").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.deepseek_frame, textvariable=self.api_key_var, show="●").grid(
            row=1, column=1, sticky="ew", padx=8, pady=3
        )

        # Reuse the provider options row; only the selected provider's frame is visible.
        self.codex_frame = ttk.LabelFrame(root, text="Codex 訂閱翻譯", padding=10)
        self.codex_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        self.codex_frame.columnconfigure(1, weight=1)
        ttk.Label(self.codex_frame, text="帳號可用模型").grid(row=0, column=0)
        self.codex_model_combo = ttk.Combobox(self.codex_frame, textvariable=self.codex_model_var,
                                             state="readonly")
        self.codex_model_combo.grid(row=0, column=1, sticky="ew", padx=8)
        self.account_button = ttk.Button(self.codex_frame, text="重新整理模型／額度", command=self._refresh_account)
        self.account_button.grid(row=0, column=2)
        ttk.Label(self.codex_frame, text="術語表 TXT（選填）").grid(row=1, column=0)
        ttk.Entry(self.codex_frame, textvariable=self.glossary_var).grid(row=1, column=1, sticky="ew")
        # Match the actual GUI command limits rather than the low-level bridge defaults.
        ttk.Label(self.codex_frame, text="批次翻譯：最多 24 段／12000 字元；每路複用程序，各批次獨立，不累積全文歷史。").grid(
            row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(self.codex_frame, textvariable=self.effort_note_var, wraplength=650).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=4)
        self.quota_frame = ttk.Frame(self.codex_frame)
        self.quota_frame.grid(row=4, column=0, columnspan=3, sticky="ew")
        ttk.Label(self.codex_frame, textvariable=self.account_var, wraplength=650).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(self.codex_frame, text="批次併發路數").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Combobox(self.codex_frame, textvariable=self.parallel_var, values=("1", "2", "3", "4", "6", "8"),
                     width=5, state="readonly").grid(row=6, column=1, sticky="w", padx=8)
        ttk.Label(self.codex_frame, text="加速預設 low＋8 路＋標準檔位；保留標記數量與配對檢查，允許中文語序調整。").grid(
            row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(self.codex_frame, text="速度檔位").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Combobox(self.codex_frame, textvariable=self.speed_var,
                     values=("標準（節省額度）", "Fast（速度優先）"), state="readonly", width=24).grid(
            row=8, column=1, sticky="w", padx=8)
        ttk.Label(self.codex_frame, text="Fast：訂閱額度消耗約為標準的 2.5 倍；僅支援的模型可用，影響下次翻譯。").grid(
            row=9, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(root, text="同時輸出中英雙語版", variable=self.dual_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=5
        )
        self.start_button = ttk.Button(root, text="開始翻譯", command=self._start)
        self.start_button.grid(row=6, column=2, sticky="e")
        self.stop_button = ttk.Button(root, text="停止", command=self._request_stop, state="disabled")
        self.stop_button.grid(row=6, column=2, sticky="e", padx=(0, 92))

        ttk.Label(root, textvariable=self.status_var, wraplength=850).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 4)
        )
        self.progress = ttk.Progressbar(root, variable=self.progress_var, maximum=100)
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.log = tk.Text(root, height=16, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")

    def _load_config(self) -> None:
        # Restore user choices and decrypt a saved API key when configuration exists.
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if config.get("model") in MODEL_OPTIONS:
                self.model_var.set(config["model"])
            if config.get("effort") in EFFORT_OPTIONS:
                self.effort_var.set(config["effort"])
            elif config.get("effort") in ("低", "無（最快，推薦翻譯）"):
                # Migrate the old misleading Codex 'none' label to its actual low behavior.
                self.effort_var.set(next(iter(EFFORT_OPTIONS)))
            self.deepseek_model_var.set(config.get("deepseek_model", "deepseek-chat"))
            if config.get("api_key"):
                self.api_key_var.set(_unprotect_secret(config["api_key"]))
            # Restore Codex preferences without persisting any account credentials.
            self.codex_model_var.set(config.get("codex_model", ""))
            self.speed_var.set("Fast（速度優先）" if config.get("service_tier") == "fast" else "標準（節省額度）")
            self.glossary_var.set(config.get("glossary", ""))
            self.dual_var.set(bool(config.get("dual", True)))
            # Bound concurrency to avoid accidental bursts against shared subscription limits.
            # Migrate the old per-paragraph worker setting to the faster batch default.
            saved = str(config.get("parallel", 8)) if config.get("batch_version") == 5 else "8"
            self.parallel_var.set(saved if saved in ("1", "2", "3", "4", "6", "8") else "8")
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        # Store preferences locally; DPAPI keeps the API key unreadable to other accounts.
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config = {
            "model": self.model_var.get(),
            "effort": self.effort_var.get(),
            "deepseek_model": self.deepseek_model_var.get().strip(),
            "api_key": _protect_secret(self.api_key_var.get().strip()) if self.api_key_var.get().strip() else "",
            # Only model names and a glossary path are persisted for Codex.
            "codex_model": self.codex_model_var.get().strip(),
            "service_tier": "fast" if self.speed_var.get().startswith("Fast") else "standard",
            "glossary": self.glossary_var.get().strip(),
            "dual": self.dual_var.get(),
            "parallel": int(self.parallel_var.get()),
            "batch_version": 5,
        }
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="選擇論文 PDF", filetypes=(("PDF 檔案", "*.pdf"), ("所有檔案", "*.*"))
        )
        if selected:
            self.file_var.set(selected)

    def _preview_pdf(self) -> None:
        # The same viewer handles original, translated and double-width bilingual PDFs.
        initial = Path(self.file_var.get()).parent if self.file_var.get() else APP_DIR
        selected = filedialog.askopenfilename(title="選擇要預覽的 PDF", initialdir=initial,
                                              filetypes=(("PDF 檔案", "*.pdf"),))
        if selected:
            try:
                from pdf_preview import PDFPreview
                PDFPreview(self, Path(selected))
            except Exception as exc:
                messagebox.showerror("無法預覽", str(exc))

    def _output_paths(self) -> tuple[Path, Path]:
        # Resolve typed relative paths before handing them to a child with a different cwd.
        source = Path(self.file_var.get()).expanduser().resolve()
        return (
            source.with_name(f"{source.stem}_翻譯.pdf"),
            source.with_name(f"{source.stem}_翻譯_雙語.pdf"),
        )

    def _update_output_preview(self) -> None:
        if not self.file_var.get():
            self.output_var.set("請選擇 PDF")
            return
        mono, dual = self._output_paths()
        self.output_var.set(f"中文版：{mono}\n雙語版：{dual}")

    def _update_model_fields(self) -> None:
        # DeepSeek credentials are shown only when the remote API is selected.
        mode, _ = MODEL_OPTIONS[self.model_var.get()]
        # Keep provider controls mutually exclusive.
        if mode == "codex":
            self.codex_frame.grid()
        else:
            self.codex_frame.grid_remove()
        if mode == "deepseek":
            self.deepseek_frame.grid()
        else:
            self.deepseek_frame.grid_remove()
        self._update_effort_options()

    def _update_effort_options(self) -> None:
        # Account model metadata, rather than a fixed list, defines legal reasoning settings.
        mode, _ = MODEL_OPTIONS[self.model_var.get()]
        model = self.codex_models.get(self.codex_model_var.get(), {})
        supported = {item.get("reasoningEffort") for item in model.get("supportedReasoningEfforts", [])}
        options = [label for label, value in EFFORT_OPTIONS.items()
                   if mode != "codex" or value in supported]
        if mode != "codex":
            options = [label for label, value in EFFORT_OPTIONS.items() if value in ("none", "low", "medium", "high")]
        self.effort_combo.configure(values=options, state="readonly" if options else "disabled")
        if options and self.effort_var.get() not in options:
            preferred = "low" if "low" in supported else model.get("defaultReasoningEffort")
            self.effort_var.set(next((label for label in options if EFFORT_OPTIONS[label] == preferred), options[0]))
        self.effort_note_var.set("論文翻譯建議先用 low；複雜推導可選 medium。模型／effort／併發更改隻影響下次工作。"
                                 if supported else "請重新整理以讀取模型及支援的 effort；不會把 none 暗中轉為 low。")

    def _refresh_account(self) -> None:
        # One bounded worker reads metadata; it performs no model inference.
        if self.account_refreshing or self.closing:
            return
        self.account_refreshing = True
        self.account_button.configure(state="disabled")
        self.account_var.set("正在讀取帳號模型與額度…")
        def fetch():
            try:
                snapshot = fetch_account_snapshot()
            except Exception as exc:
                snapshot = {"errors": {"account": str(exc)}, "models": []}
            self._post(self._show_account, snapshot)
        threading.Thread(target=fetch, daemon=True).start()

    def _show_account(self, snapshot: dict) -> None:
        # Apply every widget change on Tk's thread; failed refreshes clear stale quota bars.
        self.account_refreshing = False
        self.account_button.configure(state="normal")
        self.account_snapshot = snapshot
        for widget in self.quota_frame.winfo_children():
            widget.destroy()
        for label, remaining, reset in quota_rows(snapshot):
            text = f"{label}：剩餘 {remaining:g}%" if remaining is not None else f"{label}：剩餘額度未知"
            ttk.Label(self.quota_frame, text=f"{text} · 重置 {reset}").pack(anchor="w")
            if remaining is not None:
                ttk.Progressbar(self.quota_frame, value=remaining, maximum=100).pack(fill="x", pady=(0, 4))
        models = snapshot.get("models") or []
        if models:
            self.codex_models = {item["model"]: item for item in models if item.get("model") and not item.get("hidden")}
            self.codex_model_combo.configure(values=list(self.codex_models))
            if self.codex_model_var.get() not in self.codex_models:
                default = next((key for key, value in self.codex_models.items() if value.get("isDefault")), next(iter(self.codex_models), ""))
                self.codex_model_var.set(default)
        self._update_effort_options()
        errors = snapshot.get("errors") or {}
        stamp = datetime.now().strftime("%H:%M:%S")
        self.account_var.set("讀取失敗／部分不可用：" + "; ".join(str(value)[:180] for value in errors.values())
                             if errors else f"更新於 {stamp} · 帳號共享額度；翻譯與其他 Codex 工作會共同使用。")
        if not quota_rows(snapshot) and not errors:
            self.account_var.set(f"更新於 {stamp} · 服務未提供額度視窗，剩餘額度未知。")

    def _post(self, callback, *args) -> None:
        # Queue UI updates; worker threads never access Tcl/Tk directly.
        self.events.put((callback, args))

    def _drain_events(self) -> None:
        # Bound per-tick work so fast CLI logging cannot starve the window.
        for _ in range(200):
            try:
                callback, args = self.events.get_nowait()
            except queue.Empty:
                break
            if not self.closing:
                callback(*args)
        if not self.closing:
            self.after(100, self._drain_events)

    def _append_log(self, text: str) -> None:
        # Hide any echoed key before displaying diagnostics, and normalize Chinese log text.
        secret = self.job.get("api_key", "")
        if secret:
            text = text.replace(secret, "[已隱藏金鑰]")
        text = to_traditional(text)
        self.log.configure(state="normal")
        self.log.insert("end", text)
        # Retain recent diagnostics without accumulating an entire book's logs in memory.
        if int(self.log.index("end-1c").split(".")[0]) > 2500:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        # Resolve typed relative paths before handing them to a child with a different cwd.
        source = Path(self.file_var.get()).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            messagebox.showerror("無法開始", "請選擇有效的 PDF 檔案。")
            return
        mode, model = MODEL_OPTIONS[self.model_var.get()]
        if mode == "codex" and self.codex_model_var.get() not in self.codex_models:
            messagebox.showerror("無法開始", "請先重新整理並選擇帳號提供的模型。")
            return
        # Refuse unsupported Fast selections before parsing the PDF or consuming inference quota.
        if mode == "codex" and self.speed_var.get().startswith("Fast"):
            catalog = self.codex_models[self.codex_model_var.get()]
            tiers = {tier.get("id") for tier in catalog.get("serviceTiers", [])}
            if not (tiers & {"fast", "priority"} or "fast" in catalog.get("additionalSpeedTiers", [])):
                messagebox.showerror("Fast 不可用", "此帳號模型未提供 Fast；請重新整理模型或選擇標準檔位。")
                return
        if mode == "deepseek" and not self.api_key_var.get().strip():
            messagebox.showerror("無法開始", "請輸入 DeepSeek API Key。")
            return

        mono, dual = self._output_paths()
        existing = [path for path in (mono, dual if self.dual_var.get() else None) if path and path.exists()]
        if existing and not messagebox.askyesno("覆蓋檔案", "輸出檔案已存在，是否覆蓋？"):
            return

        # Validate runtime inputs before starting a worker.
        if not PDF2ZH.is_file() or not PYTHON.is_file():
            messagebox.showerror("無法開始", "找不到 PDFMathTranslate 虛擬環境。")
            return
        glossary = self.glossary_var.get().strip()
        if mode == "codex" and glossary and not Path(glossary).is_file():
            messagebox.showerror("無法開始", "術語表檔案不存在。")
            return
        # Snapshot all Tk values so edits during execution affect only the next job.
        self.job = dict(mode=mode, model=model, effort=EFFORT_OPTIONS[self.effort_var.get()],
                        dual=self.dual_var.get(), api_key=self.api_key_var.get().strip(),
                        deepseek_model=self.deepseek_model_var.get().strip(),
                        codex_model=self.codex_model_var.get().strip(), glossary=glossary,
                        service_tier="fast" if self.speed_var.get().startswith("Fast") else "standard",
                        mono=mono, dual_target=dual, parallel=int(self.parallel_var.get()) if mode == "codex" else 1)
        self.started_at = time.monotonic()
        self.phase_text = "正在解析 PDF"
        self.phase_detail = "等待版面與段落總數"
        self.progress_status_path = None
        self.active_count = 0
        self.batch_detail = ""
        self.token_detail = ""
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.stop_requested = False
        self.progress_var.set(0)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set("正在解析 PDF…")
        try:
            self._save_config()
        except OSError as exc:
            messagebox.showwarning("無法儲存設定", f"API Key 與設定未能儲存：{exc}")
        self._append_log("\n開始翻譯：" + str(source) + "\n")
        threading.Thread(target=self._run_translation, args=(source,), daemon=True).start()

    def _ensure_ollama(self) -> None:
        # Start the local server in the background when the GUI is the first Ollama client.
        try:
            urllib_request = __import__("urllib.request", fromlist=["urlopen"])
            urllib_request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).close()
        except Exception:
            # Remember only the server started by this GUI so an existing shared server stays alive.
            self.ollama_process = subprocess.Popen(
                [str(OLLAMA), "serve"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _build_command(self, source: Path, temp_dir: Path) -> list[str]:
        # Read the immutable job snapshot instead of accessing Tk from a background thread.
        mode, model = self.job["mode"], self.job["model"]
        effort = self.job["effort"]
        command = [
            str(PYTHON), str(PROTECTED_RUNNER), str(source), "--output", str(temp_dir), "--lang-in", "en",
            # Use Taiwan Traditional Chinese for both local and API translation engines.
            "--lang-out", "zh-TW", "--qps", str(self.job.get("parallel", 1)),
            "--pool-max-workers", str(self.job.get("parallel", 1)),
            "--no-auto-extract-glossary", "--watermark-output-mode", "no_watermark",
            # Preserve diagram/table strokes as well as their protected original glyphs.
            "--no-remove-non-formula-lines", "--disable-config-auto-save",
        ]
        if not self.job["dual"]:
            command.append("--no-dual")

        if mode == "local":
            self._ensure_ollama()
            # Forward slashes prevent the CLI command parser from treating backslashes as escapes.
            # pdf2zh parses this string with POSIX shlex, including paths with spaces.
            bridge_command = shlex.join(
                (PYTHON.as_posix(), QWEN_BRIDGE.as_posix(), "--model", model, "--effort", effort)
            )
            command.extend(
                [
                    "--clitranslator",
                    "--clitranslator-command",
                    bridge_command,
                    # A partly CPU-offloaded 27B model can need over five minutes for long blocks.
                    "--clitranslator-timeout",
                    "1800",
                ]
            )
        elif mode == "codex":
            # The bridge owns versioned/model-specific caching; upstream cache could reuse old prompts.
            command.append("--ignore-cache")
            # Hash PDF content to isolate context and cache even for identical filenames.
            digest = hashlib.sha256()
            with source.open("rb") as document:
                for block in iter(lambda: document.read(1024 * 1024), b""):
                    digest.update(block)
            bridge = [PYTHON.as_posix(), CODEX_BRIDGE.as_posix(),
                      "--status-file", (temp_dir / "codex-status.json").as_posix(),
                      "--document", digest.hexdigest(), "--effort",
                      effort]
            # Parallel turns are independent; avoid completion-order-dependent translated context.
            bridge += ["--max-chars", "4000", "--context-chars",
                       "0"]
            if self.job["codex_model"]:
                bridge += ["--model", self.job["codex_model"]]
            # Forward the immutable job choice to every persistent batch worker.
            bridge += ["--service-tier", self.job.get("service_tier", "standard")]
            if self.job["glossary"]:
                bridge += ["--glossary", Path(self.job["glossary"]).as_posix()]
            # Fail before expensive PDF parsing when subscription login is unavailable.
            check = subprocess.run([str(PYTHON), str(CODEX_BRIDGE), "--check-login"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=40)
            if check.returncode:
                raise RuntimeError(check.stderr.strip() or "請先執行 codex login")
            command.extend(["--clitranslator", "--clitranslator-command", shlex.join(bridge),
                            "--clitranslator-timeout", "1800"])
        else:
            # DeepSeek receives the same Traditional-Chinese and English-term policy as local Qwen.
            command.extend(["--custom-system-prompt", TRADITIONAL_TECHNICAL_PROMPT])
            command.extend(
                [
                    "--deepseek", "--deepseek-model", self.job["deepseek_model"],
                    "--deepseek-thinking-mode", "disabled" if effort == "none" else "enabled",
                ]
            )
            if effort != "none":
                command.extend(["--deepseek-reasoning-effort", "high" if effort != "high" else "max"])
        return command

    def _run_translation(self, source: Path) -> None:
        # Publish terminal UI events only after cleanup, so a new job cannot race the old one.
        outcome = None
        temp_dir = source.parent / f".pdf2zh-{uuid.uuid4().hex}"
        try:
            # Include directory creation in error handling to avoid a stuck busy UI.
            temp_dir.mkdir()
            self.progress_status_path = temp_dir / "codex-status.json"
            command = self._build_command(source, temp_dir)
            # Honor stop requests received during model/login initialization.
            if self.stop_requested:
                outcome = (self._stopped,)
                return
            # Keep the API key out of the visible log while streaming useful progress messages.
            self.process = subprocess.Popen(
                command,
                cwd=APP_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Give the child predictable Unicode logging and readable console line widths.
                env={**os.environ, "PYTHONUTF8": "1", "COLUMNS": "160",
                     "PDF_TRANSLATE_DEEPSEEK_API_KEY": self.job.get("api_key", "")
                     if self.job.get("mode") == "deepseek" else ""},
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Close the small stop/start race after the subprocess is created.
            if self.stop_requested:
                self._terminate_process_tree(self.process)
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if not self.closing:
                    if not line.startswith(("PDF_PROGRESS ", "PDF_ACTIVITY ", "PDF_BATCH ")):
                        self._post(self._append_log, line)
                    self._post(self._update_progress, line)
            return_code = self.process.wait()
            if self.stop_requested:
                if not self.closing:
                    outcome = (self._stopped,)
                return
            if return_code != 0:
                raise RuntimeError("PDFMathTranslate 返回錯誤，請檢視上方日誌。")

            # BabelDOC can swallow segment failures and still exit successfully.
            status_file = temp_dir / "codex-status.json"
            if status_file.exists():
                failures = json.loads(status_file.read_text(encoding="utf-8"))
                if failures:
                    raise RuntimeError(f"Codex 有 {len(failures)} 段翻譯未完成，未覆寫輸出。\n"
                                       + next(iter(failures.values())))
            if self.job.get("mode") == "codex" and os.environ.get("PDF_TRANSLATE_LEGACY") != "1":
                # Missing positive completion also blocks silent upstream fallback to source text.
                from codex_batch import completion_path
                marker = completion_path(status_file)
                if not marker.exists() or json.loads(marker.read_text(encoding="utf-8")).get("success") is not True:
                    raise RuntimeError("批次翻譯未全部完成，未覆寫輸出；請檢視上方錯誤日誌。")
            mono_source = next(temp_dir.glob("*.mono.pdf"), None)
            dual_source = next(temp_dir.glob("*.dual.pdf"), None)
            mono_target, dual_target = self.job["mono"], self.job["dual_target"]
            if mono_source is None:
                raise RuntimeError("沒有找到純中文輸出 PDF。")
            # Verify both requested outputs before replacing existing user files.
            if self.job["dual"] and dual_source is None:
                raise RuntimeError("沒有找到雙語輸出 PDF。")
            os.replace(mono_source, mono_target)
            if self.job["dual"] and dual_source is not None:
                os.replace(dual_source, dual_target)
            if not self.closing:
                outcome = (self._completed, mono_target, dual_target if self.job["dual"] and dual_source else None)
        except Exception as exc:
            if not self.closing:
                outcome = (self._failed, str(exc))
        finally:
            # Cleanup is restricted to the generated directory beneath the selected PDF folder.
            if temp_dir.resolve().parent != source.parent.resolve() or not temp_dir.name.startswith(".pdf2zh-"):
                raise RuntimeError("Unsafe temporary directory")
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.process = None
            if outcome and not self.closing:
                self._post(*outcome)

    def _terminate_process_tree(self, process: subprocess.Popen[str] | subprocess.Popen[bytes] | None) -> None:
        # taskkill /T also closes the CLI translator child currently waiting on Ollama.
        if process is None or process.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Fall back to the direct child if Windows cannot enumerate its process tree.
            try:
                process.terminate()
            except OSError:
                pass

    def _request_stop(self) -> None:
        # Kill in a worker thread so the window remains responsive while Windows ends children.
        # Record cancellation even while login or model startup is still in progress.
        if str(self.start_button["state"]) != "disabled":
            return
        self.stop_requested = True
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止…")
        threading.Thread(target=self._terminate_process_tree, args=(self.process,), daemon=True).start()

    def _stopped(self) -> None:
        self.started_at = None
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress_var.set(0)
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("已停止")
        self._append_log("\n翻譯已由使用者停止。\n")

    def _on_close(self) -> None:
        # Do not leave translation children or a GUI-owned Ollama server after the window closes.
        self.closing = True
        self.stop_requested = True
        self._terminate_process_tree(self.process)
        self._terminate_process_tree(self.ollama_process)
        self.destroy()

    def _update_progress(self, line: str) -> None:
        # Read structured IPC progress instead of guessing percentages from wrapped console logs.
        if line.startswith("PDF_BATCH "):
            try:
                event = json.loads(line[10:])
                self.batch_detail = (f"批次 {event['completed']}/{event['total']} · 快取 {event['cache_hits']} 段 · "
                                     f"去重 {event.get('deduplicated', 0)} 段 · 請求 {event.get('requests', 0)} 次 · {event['workers']} 路")
                # Show server-reported token use; cached input is already included in input.
                tokens = event.get("tokens")
                self.token_detail = (f"Token 已回報 {event.get('usage_requests', 0)}/{event.get('requests', 0)} 次："
                    f"輸入 {tokens['inputTokens']:,}（含快取 {tokens['cachedInputTokens']:,}）· "
                    f"輸出 {tokens['outputTokens']:,} · 推理 {tokens['reasoningOutputTokens']:,}"
                    if tokens else "Token：服務尚未回報；字元數不當作 token 或額度估計。")
                self._show_elapsed()
            except (ValueError, KeyError, TypeError):
                pass
            return
        if line.startswith("PDF_ACTIVITY "):
            try:
                self.active_count = max(0, self.active_count + int(json.loads(line[13:])["delta"]))
            except (ValueError, KeyError, TypeError):
                pass
            return
        if line.startswith("PDF_PROGRESS "):
            try:
                event = json.loads(line[13:])
                stage = str(event.get("stage") or "處理 PDF")
                current, total = int(event.get("stage_current") or 0), int(event.get("stage_total") or 0)
                percent = float(event.get("stage_progress") or 0)
                overall = float(event.get("overall_progress") or 0)
                self.progress.stop()
                self.progress.configure(mode="determinate")
                # Upstream weights provide an estimate; reserve 100% for verified publication.
                self.progress_var.set(max(0, min(99, overall)))
                self.phase_text = "翻譯段落" if "translate" in stage.lower() else stage
                self.phase_detail = f"整體估計 {min(99, overall):.0f}% · 已處理 {current}/{total} {'段' if 'translate' in stage.lower() else '項'} · 本階段 {percent:.0f}%"
                self._show_elapsed()
            except (ValueError, TypeError):
                pass
            return
        lowered = line.lower()
        if "loading onnx model" in lowered or "start to translate" in lowered:
            self.phase_text = "分析 PDF 版面"
        elif "found title paragraph" in lowered or "using deepseek" in lowered:
            self.phase_text = "翻譯段落"
        elif any(keyword in lowered for keyword in ("typeset", "render", "save pdf")):
            self.phase_text = "生成 PDF"

    def _show_elapsed(self) -> None:
        if self.started_at is None:
            return
        # Atomic reports can be read while bridge workers update failures under their shared lock.
        failures = 0
        try:
            if self.progress_status_path and self.progress_status_path.exists():
                failures = len(json.loads(self.progress_status_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        seconds = int(time.monotonic() - self.started_at)
        self.status_var.set(f"{self.phase_text} · {self.phase_detail}\n"
                            f"{self.batch_detail} · 進行中 {self.active_count} 段 · 失敗 {failures} 項 · 耗時 {seconds // 60}:{seconds % 60:02d}"
                            "（完成全部階段並驗證輸出後才算完成）"
                            + ("\n" + self.token_detail if self.token_detail else ""))

    def _tick_elapsed(self) -> None:
        # Keep elapsed time visible even while a slow paragraph has no new console messages.
        self._show_elapsed()
        if not self.closing:
            self.after(1000, self._tick_elapsed)

    def _completed(self, mono: Path, dual: Path | None) -> None:
        # Keep the final usage summary in the visible log after clearing the live status.
        self._append_log("\n" + self.batch_detail + "\n" + self.token_detail + "\n")
        self.started_at = None
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress_var.set(100)
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("翻譯完成")
        self._refresh_account()
        details = f"中文版：\n{mono}"
        if dual:
            details += f"\n\n雙語版：\n{dual}"
        messagebox.showinfo("完成", details)

    def _failed(self, error: str) -> None:
        self.started_at = None
        self.progress.stop()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("翻譯失敗")
        messagebox.showerror("翻譯失敗", error)


if __name__ == "__main__":
    TranslatorGUI().mainloop()
