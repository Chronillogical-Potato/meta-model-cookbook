from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .session import DemoSession, SessionBusyError

MAX_REQUEST_BYTES = 8 * 1024 * 1024
TOKEN_HEADER = "X-Muse-Kiosk-Token"


def make_handler(
    session: DemoSession,
    static_root: Path,
    *,
    access_token: str,
    qr_url: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; frame-ancestors 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _json(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, indent=2).encode()
            self._send(status, body, "application/json; charset=utf-8")

        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _host_is_valid(self) -> bool:
            return self.headers.get("Host", "") in self._allowed_hosts()

        def _origin_is_valid(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return origin == f"http://{self.headers.get('Host', '')}"

        def _token_is_valid(self) -> bool:
            supplied = self.headers.get(TOKEN_HEADER)
            if supplied is None:
                query = parse_qs(urlparse(self.path).query)
                supplied = query.get("token", [""])[0]
            return hmac.compare_digest(supplied, access_token)

        def _authorize(self, *, require_token: bool) -> bool:
            if not self._host_is_valid():
                self._json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "invalid host"})
                return False
            if not self._origin_is_valid():
                self._json(
                    HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"}
                )
                return False
            if require_token and not self._token_is_valid():
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid kiosk token"})
                return False
            return True

        def _asset_is_allowed(self, name: str) -> bool:
            return name in {
                "mujoco_scene.png",
                "muse_robotics_demo.mp4",
                "muse_robotics_hero.png",
                "reachy_mini_card.png",
            }

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            require_token = path not in {"/", "/index.html"} and not path.startswith(
                "/asset/"
            )
            if not self._authorize(require_token=require_token):
                return
            if path == "/state":
                self._json(HTTPStatus.OK, session.snapshot().public_dict())
                return
            if path in {"/", "/index.html"}:
                template = (static_root / "index.html").read_text()
                body = template.replace("__MUSE_KIOSK_TOKEN__", access_token).encode()
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return
            if path.startswith("/asset/"):
                name = unquote(path.removeprefix("/asset/"))
                asset_root = static_root.parents[1] / "assets"
                target = asset_root / name
                if (
                    Path(name).name != name
                    or not self._asset_is_allowed(name)
                    or not target.is_file()
                    or target.is_symlink()
                ):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                content_type = (
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                self._send(HTTPStatus.OK, target.read_bytes(), content_type)
                return
            if path.startswith("/output/"):
                name = unquote(path.removeprefix("/output/"))
                if Path(name).name != name or name not in session.allowed_outputs():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                target = session.output_dir / name
                content_type = (
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                self._send(HTTPStatus.OK, target.read_bytes(), content_type)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorize(require_token=True):
                self.close_connection = True
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json":
                self.close_connection = True
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "Content-Type must be application/json"},
                )
                return
            path = urlparse(self.path).path
            if path not in {"/action", "/reset"}:
                self.close_connection = True
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("request size is invalid")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise TypeError("body must be a JSON object")
                if path == "/reset":
                    self._json(HTTPStatus.OK, session.reset().public_dict())
                    return
                request = payload.get("request")
                if not isinstance(request, str):
                    raise TypeError("request must be a string")
                encoded_image = payload.get("image")
                image = None
                if encoded_image is not None:
                    if not isinstance(encoded_image, str):
                        raise ValueError("image must be base64 text")
                    image = base64.b64decode(encoded_image, validate=True)
                include_qr = payload.get("include_qr")
                if include_qr is not None and not isinstance(include_qr, bool):
                    raise ValueError("include_qr must be a boolean")
                state = session.handle(
                    request,
                    image=image,
                    qr_url=qr_url,
                    include_qr=include_qr,
                )
            except SessionBusyError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.close_connection = True
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, state.public_dict())

    return Handler


def serve(
    session: DemoSession,
    *,
    host: str = "127.0.0.1",
    port: int = 8800,
    qr_url: str | None = None,
    access_token: str | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The cookbook kiosk binds to loopback only.")
    token = access_token or secrets.token_urlsafe(24)
    static_root = Path(__file__).with_name("static")
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(session, static_root, access_token=token, qr_url=qr_url),
    )
    print(f"Kiosk: http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
