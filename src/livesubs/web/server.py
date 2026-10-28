"""Serves the phone page and pushes the translations to it with Server-Sent Events.

Runs in the UI process on its own threads (stdlib ``ThreadingHTTPServer``). Every change sends
the whole :class:`WebFeed` state, so a phone that reconnects (screen off, Wi-Fi hiccup) is right
again with its first message.
"""

from __future__ import annotations

import base64
import contextlib
import html
import json
import logging
import queue
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from livesubs.i18n import t
from livesubs.web.feed import WebFeed

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
FONT = Path(__file__).parents[1] / "assets" / "fonts" / "Inter-SemiBold.ttf"
KEEPALIVE_S = 15.0  # a comment line now and then keeps phones and routers from dropping us
CLIENT_BACKLOG = 4  # snapshots waiting for a slow client; only the newest matters
_PLACEHOLDER = re.compile(r"\{\{([\w.]+)\}\}")


def lan_address() -> str:
    """This PC's address on the local network (the interface used to reach other hosts)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))  # UDP: picks a route, sends nothing
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def render_page() -> bytes:
    """index.html with its ``{{web.*}}`` placeholders filled from the UI strings.

    ``{{awake_video}}`` becomes the silent keep-awake video as a data URI: inline, so Safari
    plays it without the byte-range requests it insists on for a video URL.
    """
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    video = base64.b64encode((STATIC / "awake.mp4").read_bytes()).decode("ascii")
    page = page.replace("{{awake_video}}", f"data:video/mp4;base64,{video}")
    return _PLACEHOLDER.sub(lambda m: html.escape(t(m.group(1))), page).encode("utf-8")


class WebServer:
    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.port = port
        self.host = host
        self._feed = WebFeed()
        self._lock = threading.Lock()
        self._clients: set[queue.Queue[str | None]] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._page = render_page()

    # --- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        """Bind and serve; raises ``OSError`` when the port is taken."""
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True  # event streams never end by themselves
        httpd.app = self  # type: ignore[attr-defined]
        self.port = httpd.server_address[1]  # port 0 (tests): the one the OS chose
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="web"
        )
        self._thread.start()
        log.info("Web output on port %d", self.port)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        with self._lock:
            for client in self._clients:
                _push(client, None)  # ends its stream
            self._clients.clear()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://{lan_address()}:{self.port}/"

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # --- content (UI thread) ---------------------------------------------------------------------

    def show(self, line_id: int, text: str) -> None:
        with self._lock:
            self._feed.show(line_id, text)
            self._publish()

    def clear(self) -> None:
        with self._lock:
            if self._feed.current is None:
                return
            self._feed.clear()
            self._publish()

    def new_session(self) -> None:
        with self._lock:
            self._feed.new_session()

    def _publish(self) -> None:
        message = _event(self._feed.snapshot())
        for client in self._clients:
            _push(client, message)

    # --- clients (handler threads) ---------------------------------------------------------------

    def _subscribe(self) -> queue.Queue[str | None]:
        client: queue.Queue[str | None] = queue.Queue(CLIENT_BACKLOG)
        with self._lock:
            client.put_nowait(_event(self._feed.snapshot()))
            self._clients.add(client)
        return client

    def _unsubscribe(self, client: queue.Queue[str | None]) -> None:
        with self._lock:
            self._clients.discard(client)


def _push(client: queue.Queue[str | None], message: str | None) -> None:
    """Queue for a client; a slow one loses its oldest snapshot (each one is the full state)."""
    if client.full():
        with contextlib.suppress(queue.Empty):
            client.get_nowait()
    client.put_nowait(message)


def _event(state: dict[str, object]) -> str:
    return f"data: {json.dumps(state, ensure_ascii=False)}\n\n"


class _Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    @property
    def app(self) -> WebServer:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(self.app._page, "text/html; charset=utf-8")
        elif path == "/font.ttf":
            self._send(FONT.read_bytes(), "font/ttf", cache=True)
        elif path == "/events":
            self._stream()
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str, *, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        client = self.app._subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\n")  # how soon the page reconnects
            while True:
                try:
                    message = client.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    message = ": ping\n\n"
                if message is None:
                    break
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
        except OSError:  # the phone went away
            pass
        finally:
            self.app._unsubscribe(client)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("web: " + format, *args)
