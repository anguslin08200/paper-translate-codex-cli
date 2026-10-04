"""Read Codex quota and model catalog through the official app-server protocol."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import queue
import subprocess
import threading
import time

from codex_translate import codex_command, subscription_environment


class AppServer:
    def __init__(self, timeout: float):
        # A single deadline bounds startup, both reads and every pagination request.
        self.deadline = time.monotonic() + timeout
        self.responses = queue.Queue()
        self.counter = 0
        self.process = subprocess.Popen(
            codex_command() + ["app-server", "--listen", "stdio://",
                               "-c", 'forced_login_method="chatgpt"'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=subscription_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        # Read on a worker so a stalled pipe cannot block the GUI beyond its timeout.
        try:
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    self.responses.put(RuntimeError("Codex app-server returned invalid JSON"))
        except (OSError, ValueError) as error:
            self.responses.put(RuntimeError(str(error)))
        finally:
            self.responses.put(RuntimeError("Codex app-server closed its response stream"))

    def send(self, message: dict):
        # Only initialize and the two read endpoints are called by this module.
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        request_id = self.counter
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex account status timed out")
            try:
                response = self.responses.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex account status timed out") from None
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict):
                raise RuntimeError("Codex app-server returned a non-object response")
            if response.get("id") != request_id:
                # Startup notifications and unrelated responses do not satisfy this request.
                continue
            if "error" in response:
                error = response["error"]
                detail = error.get("message", "request failed") if isinstance(error, dict) else str(error)
                raise RuntimeError(str(detail)[:500])
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex app-server response has no object result")
            return result

    def close(self):
        # EOF allows a healthy app-server to exit; kill its launcher and child on a stall.
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               capture_output=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=1)
            self.process.stdout.close()


def fetch_account_snapshot(timeout: float = 25) -> dict:
    """Return raw quota buckets, all picker models and explicit per-endpoint errors.

    This performs no inference, starts no translation and never reads auth tokens.
    Missing quotas remain None; missing/failed model discovery remains an empty list.
    """
    snapshot = {"rateLimits": None, "rateLimitsByLimitId": None, "models": [],
                "errors": {}, "fetchedAt": datetime.now(timezone.utc).isoformat()}
    server = None
    try:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        server = AppServer(timeout)
        server.request("initialize", {"clientInfo": {
            "name": "pdf_math_translate", "title": "PDFMathTranslate", "version": "1.0"}})
        server.send({"method": "initialized"})
        try:
            limits = server.request("account/rateLimits/read")
            snapshot["rateLimits"] = limits.get("rateLimits")
            snapshot["rateLimitsByLimitId"] = limits.get("rateLimitsByLimitId")
            if snapshot["rateLimits"] is None and not snapshot["rateLimitsByLimitId"]:
                snapshot["errors"]["account/rateLimits/read"] = "Codex returned no quota information"
        except (OSError, RuntimeError, TimeoutError) as error:
            snapshot["errors"]["account/rateLimits/read"] = str(error)
        try:
            cursor, seen = None, set()
            while True:
                # Preserve native model IDs and effort descriptors without guessing availability.
                params = {"limit": 100, "includeHidden": False}
                if cursor is not None:
                    params["cursor"] = cursor
                page = server.request("model/list", params)
                data = page.get("data")
                if not isinstance(data, list) or any(not isinstance(model, dict) for model in data):
                    raise RuntimeError("Codex model/list returned an invalid model catalog")
                snapshot["models"].extend(data)
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise RuntimeError("Codex model/list repeated or invalid pagination cursor")
                seen.add(cursor)
        except (OSError, RuntimeError, TimeoutError) as error:
            # Incomplete catalogs should not populate a misleading model selector.
            snapshot["models"] = []
            snapshot["errors"]["model/list"] = str(error)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        snapshot["errors"]["app-server"] = str(error)
    finally:
        if server is not None:
            try:
                server.close()
            except (OSError, subprocess.SubprocessError) as error:
                snapshot["errors"]["cleanup"] = str(error)
    return snapshot
