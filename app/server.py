"""A loopback-only HTTP application, using Python's standard library."""
import argparse
import hmac
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import read_config
from .domain import AppError, Store


STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_BODY = 65536


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "XAffiliateLocal/0.1"
    sys_version = ""

    def log_message(self, format, *args):
        # Do not log arbitrary URLs, form contents, or headers.
        pass

    def send_body(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {"127.0.0.1:%d" % port, "localhost:%d" % port}

    def authorized(self):
        if not self.valid_host():
            self.send_body(403, {"error": "ローカルアドレスからアクセスしてください。"})
            return False
        return True

    def state(self):
        state = self.server.store.state()
        state["csrfToken"] = self.server.csrf_token
        state["config"] = {"mode": "mock", "liveEnabled": False}
        return state

    def do_GET(self):
        if not self.authorized():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/state":
                self.send_body(200, self.state())
            elif path == "/health":
                self.send_body(200, {"status": "ok", "mode": "mock"})
            elif path in ASSETS:
                filename, mime = ASSETS[path]
                self.send_body(200, (STATIC / filename).read_bytes(), mime)
            else:
                self.send_body(404, {"error": "ページが見つかりません。"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_body(500, {"error": "読み込みに失敗しました。アプリを再起動して再度お試しください。"})

    def do_POST(self):
        if not self.authorized():
            return
        origin = self.headers.get("Origin")
        allowed_origins = {"http://127.0.0.1:%d" % self.server.server_address[1], "http://localhost:%d" % self.server.server_address[1]}
        token = self.headers.get("X-CSRF-Token", "")
        if (origin and origin not in allowed_origins) or not hmac.compare_digest(token.encode("utf-8"), self.server.csrf_token.encode("ascii")):
            self.send_body(403, {"error": "操作を確認できませんでした。ページを再読み込みしてください。"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_body(400, {"error": "この送信形式は利用できません。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.send_body(413, {"error": "送信データが大きすぎるか、サイズが不明です。"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.send_body(400, {"error": "JSON形式で送信してください。"})
            return
        try:
            def invalid_constant(value):
                raise ValueError("Invalid number")
            payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError("Not an object")
        except (UnicodeDecodeError, ValueError):
            self.send_body(400, {"error": "入力形式を確認してください。"})
            return
        try:
            path = urlsplit(self.path).path
            result = self.server.store.mutate(path, payload)
            if path != "/api/drafts/export":
                result["csrfToken"] = self.server.csrf_token
                result["config"] = {"mode": "mock", "liveEnabled": False}
            self.send_body(200, result)
        except AppError as exc:
            self.send_body(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_body(500, {"error": "保存に失敗しました。入力を確認して再度お試しください。"})

    def do_OPTIONS(self):
        self.send_body(405, {"error": "この操作は利用できません。"})


def create_server(db_path, port=8765):
    server = LocalServer(("127.0.0.1", port), Handler)
    server.store = Store(db_path, mode="mock")
    server.csrf_token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser(description="Xアフィリエイト運用システム（ローカル初期版）")
    parser.add_argument("--port", type=int, default=8765, help="ローカル接続ポート（既定: 8765）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port は 1〜65535 で指定してください")
    try:
        config = read_config()
        server = create_server(config["db_path"], args.port)
    except (ValueError, OSError) as exc:
        print("起動できませんでした: %s" % exc, file=sys.stderr)
        return 1
    print("X運用ワークスペース: http://127.0.0.1:%d" % args.port, flush=True)
    print("サンプルモード / データはこのMacに保存 / 停止は Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。保存データは次回も利用できます。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
