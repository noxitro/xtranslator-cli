"""Local OpenAI-compatible HTTP server that xTranslator talks to.

Two modes:
  capture - record every incoming source string, echo it back unchanged.
            Nothing is persisted by xTranslator (batch rules close files
            silently without saving dictionaries).
  serve   - answer with translations from a table; unknown strings are
            echoed back and recorded as missing.

The batch script ends with a sentinel rule whose only string is
wire.SENTINEL_TEXT; receiving it flags completion.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import wire


class ServerState:
    def __init__(self, mode: str, table: dict[str, str] | None = None):
        assert mode in ("capture", "serve")
        self.mode = mode
        self.table = table or {}
        self.lock = threading.Lock()
        self.captured: dict[str, None] = {}   # ordered set of source strings
        self.served: dict[str, str] = {}      # orig -> trans actually delivered
        self.missing: dict[str, None] = {}    # ordered set, serve mode only
        self.errors: list[str] = []
        self.n_requests = 0
        self.last_request = 0.0
        self.done = threading.Event()

    def handle(self, text: str) -> str:
        """Process one decoded source string, return the decoded reply text."""
        with self.lock:
            self.n_requests += 1
            self.last_request = time.monotonic()
            if text == wire.SENTINEL_TEXT:
                self.done.set()
                return text
            if self.mode == "capture":
                self.captured.setdefault(text, None)
                return text
            trans = self.table.get(text)
            if trans is None or trans == "":
                self.missing.setdefault(text, None)
                return text
            self.served[text] = trans
            return trans

    def record_error(self, msg: str) -> None:
        with self.lock:
            self.n_requests += 1
            self.last_request = time.monotonic()
            if len(self.errors) < 50:
                self.errors.append(msg)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    def _reply_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        state: ServerState = self.server.state  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body)
            content = req["messages"][0]["content"]
            _, payload = wire.split_request(content)
            text, style = wire.decode_payload(payload)
            reply = state.handle(text)
            content_out = wire.encode_reply(reply, style)
        except wire.WireError as e:
            state.record_error(str(e))
            self._reply_json(500, {"error": {"message": str(e)}})
            return
        except Exception as e:  # malformed request - report, don't crash
            state.record_error(f"bad request: {e!r}")
            self._reply_json(500, {"error": {"message": repr(e)}})
            return
        self._reply_json(200, {
            "id": "xtcli-local",
            "object": "chat.completion",
            "model": "xtcli",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content_out},
            }],
        })


class FakeApiServer:
    """Threaded local server; use as a context manager."""

    def __init__(self, port: int, mode: str, table: dict[str, str] | None = None):
        self.state = ServerState(mode, table)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeApiServer":
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]
