from __future__ import annotations

import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .ai import AiService
from .config import Settings
from .importer import ensure_imported
from .metrics import DateRange, MetricsService


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def create_handler(settings: Settings):
    metrics = MetricsService(settings.database_path)
    assistant = AiService(metrics, settings.deepseek_api_key, settings.deepseek_model)
    static_root = settings.static_dir.resolve()

    class MonekiHandler(BaseHTTPRequestHandler):
        server_version = "MonekiOps/1.0"

        def log_message(self, format_string, *args):
            print(f"{self.address_string()} - {format_string % args}")

        def _send_json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, json.JSONDecodeError) as error:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON") from error

        @staticmethod
        def _query(parsed):
            return parse_qs(parsed.query, keep_blank_values=False)

        def _period(self, query):
            bounds = metrics.date_bounds()
            start = query.get("start_date", [bounds["min_date"]])[0]
            end = query.get("end_date", [bounds["max_date"]])[0]
            return DateRange.validated(start, end)

        @staticmethod
        def _stores(query):
            values = query.get("store_ids", [])
            if not values:
                return []
            return [item for value in values for item in value.split(",") if item]

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    self._send_json({"status": "ok", "database": "ready", "ai_provider": assistant.provider})
                    return
                if parsed.path == "/api/meta":
                    self._send_json(
                        {
                            **metrics.date_bounds(),
                            "stores": metrics.stores(),
                            "products": metrics.products(),
                            "ai_provider": assistant.provider,
                            "currency": "CNY",
                            "timezone": "Asia/Shanghai",
                        }
                    )
                    return
                if parsed.path == "/api/data-quality":
                    self._send_json(metrics.quality())
                    return
                query = self._query(parsed)
                period = self._period(query)
                stores = self._stores(query)
                if parsed.path == "/api/metrics/daily":
                    self._send_json({"period": period.__dict__, "rows": metrics.daily(period, stores)})
                    return
                if parsed.path == "/api/products/top":
                    limit = int(query.get("limit", ["10"])[0])
                    self._send_json({"period": period.__dict__, "rows": metrics.top_products(period, stores, limit)})
                    return
                if parsed.path == "/api/stores/compare":
                    self._send_json({"period": period.__dict__, "rows": metrics.store_comparison(period)})
                    return
                if parsed.path == "/api/dashboard":
                    self._send_json(
                        {
                            "overview": metrics.overview(period, stores),
                            "daily": metrics.daily(period, stores),
                            "top_products": metrics.top_products(period, stores, 10),
                            "stores": metrics.store_comparison(period),
                        }
                    )
                    return
                self._serve_static(parsed.path)
            except ApiError as error:
                self._send_json({"error": str(error)}, error.status)
            except (ValueError, RuntimeError) as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                self._send_json({"error": "Internal server error", "detail": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path not in ("/api/chat", "/api/chat/stream"):
                    raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")
                payload = self._read_json()
                result = assistant.ask(payload.get("message", ""), payload.get("session_id"))
                if parsed.path == "/api/chat":
                    self._send_json(result)
                else:
                    self._stream(result)
            except ApiError as error:
                self._send_json({"error": str(error)}, error.status)
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                self._send_json({"error": "Internal server error", "detail": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _stream(self, result):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(event, payload):
                line = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()

            emit("tool", {"tool_call": result["tool_call"], "provider": result["provider"]})
            words = result["answer"]
            for start in range(0, len(words), 6):
                emit("delta", {"content": words[start : start + 6]})
                time.sleep(0.012)
            emit("done", result)

        def _serve_static(self, request_path):
            relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
            candidate = (static_root / relative).resolve()
            if static_root not in candidate.parents and candidate != static_root:
                raise ApiError(HTTPStatus.FORBIDDEN, "Invalid path")
            if not candidate.is_file():
                if "." not in Path(relative).name:
                    candidate = static_root / "index.html"
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, "File not found")
            content = candidate.read_bytes()
            content_type, _ = mimetypes.guess_type(candidate.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

    return MonekiHandler


def serve(settings: Settings) -> None:
    result = ensure_imported(settings.data_dir, settings.database_path)
    print(
        f"Data ready: {result['accepted_rows']}/{result['raw_rows']} accepted; "
        f"{result['rejected_rows']} quarantined"
    )
    server = ThreadingHTTPServer((settings.host, settings.port), create_handler(settings))
    print(f"Moneki Ops is running at http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

