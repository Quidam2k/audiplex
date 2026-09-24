from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024
_RELAY_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "Last-Modified",
    "ETag",
    "Cache-Control",
)
_REQUEST_HEADERS = ("Range", "If-Range", "User-Agent")
_CLIENT_DISCONNECT_ERRORS = (
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True


class AuthProxy:
    def __init__(self, upstream_base: str, token: str):
        self._upstream_base = upstream_base.rstrip("/")
        self._token = token
        self._token_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

        self._client = self._new_client()
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._base_url: str | None = None

    @staticmethod
    def _new_client() -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(30, read=60),
            follow_redirects=True,
        )

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise RuntimeError("Auth proxy has not been started")
        return self._base_url

    def set_token(self, token: str) -> None:
        with self._token_lock:
            self._token = token

    def start(self) -> str:
        with self._lifecycle_lock:
            if self._server is not None:
                return self.base_url

            if self._client.is_closed:
                self._client = self._new_client()

            proxy = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.0"

                def do_GET(self) -> None:
                    proxy._handle_request(self, "GET")

                def do_HEAD(self) -> None:
                    proxy._handle_request(self, "HEAD")

                def log_message(self, format: str, *args: object) -> None:
                    logger.debug("%s - %s", self.address_string(), format % args)

            server = _ProxyServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever,
                name="audiplex-auth-proxy",
                daemon=True,
            )

            self._server = server
            self._thread = thread
            self._base_url = f"http://127.0.0.1:{port}"
            thread.start()
            return self._base_url

    def stop(self) -> None:
        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._base_url = None

        if server is not None:
            server.shutdown()
            server.server_close()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

        self._client.close()

    def _handle_request(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
    ) -> None:
        handler.close_connection = True

        path = urlsplit(handler.path).path
        if not path.startswith("/api/"):
            handler.send_error(403, "Forbidden")
            return

        with self._token_lock:
            token = self._token

        headers = {"Authorization": f"Bearer {token}"}
        for name in _REQUEST_HEADERS:
            value = handler.headers.get(name)
            if value is not None:
                headers[name] = value

        url = f"{self._upstream_base}{handler.path}"
        response_started = False

        try:
            with self._client.stream(method, url, headers=headers) as response:
                handler.send_response(response.status_code)
                for name in _RELAY_HEADERS:
                    value = response.headers.get(name)
                    if value is not None:
                        handler.send_header(name, value)
                handler.send_header("Connection", "close")
                handler.end_headers()
                response_started = True

                if method == "HEAD":
                    return

                for chunk in response.iter_raw(_CHUNK_SIZE):
                    if chunk:
                        handler.wfile.write(chunk)

        except _CLIENT_DISCONNECT_ERRORS:
            logger.debug("Proxy client disconnected while requesting %s", handler.path)
        except httpx.HTTPError as exc:
            logger.warning("Proxy upstream request failed for %s: %s", handler.path, exc)
            if not response_started:
                try:
                    handler.send_error(502, "Bad Gateway")
                except _CLIENT_DISCONNECT_ERRORS:
                    logger.debug(
                        "Proxy client disconnected before 502 response for %s",
                        handler.path,
                    )
