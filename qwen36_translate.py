"""Translate stdin to Traditional Chinese with a local Qwen Ollama model."""

import json
import argparse
import sys
import urllib.request
from terminology import ENGLISH_TERMS_POLICY


def parse_args() -> argparse.Namespace:
    # Keep model and reasoning controls explicit so the GUI can select them safely.
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.6:latest")
    parser.add_argument(
        "--effort", choices=("none", "low", "medium", "high"), default="none"
    )
    return parser.parse_args()


def main() -> int:
    # Force UTF-8 because Windows PowerShell may otherwise expose a Big5 console encoding.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    # PDFMathTranslate passes one source segment through standard input.
    source = sys.stdin.read()
    if not source.strip():
        return 0
    args = parse_args()

    # Ollama exposes a boolean thinking switch for these Qwen models. Effort labels above
    # "none" enable reasoning; the system instruction calibrates the requested depth.
    thinking_enabled = args.effort != "none"
    # Newer Qwen models accept a named reasoning level; False fully disables thinking.
    thinking_setting: bool | str = args.effort if thinking_enabled else False
    payload = {
        "model": args.model,
        "stream": False,
        "think": thinking_setting,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional scientific translation engine. "
                    # Explicitly request Taiwan Traditional Chinese terminology and script.
                    "Translate faithfully into Traditional Chinese as used in Taiwan. "
                    + ENGLISH_TERMS_POLICY +
                    "Preserve every formula, citation, XML/HTML "
                    "tag, and placeholder exactly. "
                    f"Use {args.effort} reasoning effort. Output only the translation."
                ),
            },
            {"role": "user", "content": source},
        ],
        "options": {"temperature": 0, "num_predict": 8192 if thinking_enabled else 4096},
    }

    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Match the GUI's long timeout; the user can still interrupt safely with its Stop button.
    with urllib.request.urlopen(request, timeout=1800) as response:
        result = json.load(response)

    # A missing translation is a hard failure so the caller can retry or report it.
    translation = result.get("message", {}).get("content", "").strip()
    if not translation:
        raise RuntimeError("Qwen returned an empty translation")
    sys.stdout.write(translation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
