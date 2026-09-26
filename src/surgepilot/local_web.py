"""Loopback-only controller for the private, offline research report."""

import argparse
import ipaddress
import json
import secrets
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "local"
REPORT = ROOT / "reports/private/latest.html"
SUMMARY = ROOT / "reports/private/latest.json"
CREDENTIALS = Path.home() / "Downloads/토스 api key"
NY = ZoneInfo("America/New_York")
ALLOWED_FILES = {"/": (WEB / "index.html", "text/html; charset=utf-8"),
                 "/app.js": (WEB / "app.js", "text/javascript; charset=utf-8"),
                 "/style.css": (WEB / "style.css", "text/css; charset=utf-8"),
                 "/report": (REPORT, "text/html; charset=utf-8")}


def _now() -> str:
    return datetime.now(NY).isoformat(timespec="seconds")


def _parameters(payload: dict) -> dict:
    values = {}
    for name, maximum in (("lookback", 120), ("rise", 1000), ("trail", 100), ("stop", 100)):
        raw = payload.get(name, {"lookback": 5, "rise": 8, "trail": 7, "stop": 5}[name])
        try:
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name}: 숫자를 입력하세요") from None
        if not number.is_finite() or not 0 < number <= maximum:
            raise ValueError(f"{name}: 0보다 크고 {maximum} 이하여야 합니다")
        if name == "lookback":
            if number != int(number):
                raise ValueError("lookback: 정수를 입력하세요")
            values[name] = str(int(number))
        else:
            values[name] = str(number)
    return values


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.status = {"phase": "idle", "action": None, "message": "실행 대기 중", "started_at": None,
                       "finished_at": None, "report_version": self._report_version()}
        self.process = None

    @staticmethod
    def _report_version() -> int | None:
        return REPORT.stat().st_mtime_ns if REPORT.exists() else None

    def snapshot(self) -> dict:
        with self.lock:
            return self.status.copy()

    def start(self, action: str, values: dict):
        if action not in ("report", "premarket"):
            raise ValueError("알 수 없는 실행 작업입니다")
        if action == "premarket" and not CREDENTIALS.is_file():
            raise ValueError("다운로드 폴더에서 토스 api key 파일을 찾지 못했습니다")
        with self.lock:
            if self.status["phase"] == "running":
                raise RuntimeError("이미 실행 중인 작업이 있습니다")
            self.status = {"phase": "running", "action": action,
                           "message": "장전 분봉 수집 준비 중" if action == "premarket" else "보고서 계산 준비 중",
                           "started_at": _now(), "finished_at": None,
                           "report_version": self._report_version()}
        threading.Thread(target=self._run, args=(action, values), daemon=True).start()

    def _command(self, module: str, args: list[str]) -> int:
        with self.lock:
            if self.status["phase"] != "running":
                return -1
            self.process = subprocess.Popen([sys.executable, "-u", "-m", module, *args], cwd=ROOT,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1)
            process = self.process
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            # Keep credentials and unexpected upstream response bodies out of the UI.
            if line.startswith(("premarket pairs=", "premarket scanned", "premarket collection stopped",
                                "confirmed_recall=", "HTML:")):
                with self.lock:
                    if self.status["phase"] == "running":
                        self.status["message"] = line[:240]
        code = process.wait()
        with self.lock:
            self.process = None
        return code

    def _run(self, action: str, values: dict):
        try:
            if action == "premarket":
                code = self._command("surgepilot.scan", ["premarket", "--credentials-file", str(CREDENTIALS),
                                                          "--workers", "2", "--rate", "2"])
                if code != 0:
                    with self.lock:
                        detail = self.status["message"]
                    raise RuntimeError(detail if detail.startswith("premarket collection stopped")
                                       else "장전 수집이 실패했습니다. 토스 허용 IP와 API 상태를 확인하세요")
                with self.lock:
                    if self.status["phase"] != "running":
                        return
                    self.status["message"] = "장전 수집 종료 · 보고서 재계산 중"
            args = [option for name in ("lookback", "rise", "trail", "stop")
                    for option in (f"--{name}", values[name])]
            code = self._command("surgepilot.local_report", [*args, "--no-open"])
            if code != 0:
                raise RuntimeError("보고서 계산이 실패했습니다. 서버 터미널의 오류를 확인하세요")
            with self.lock:
                if self.status["phase"] == "running":
                    self.status.update(phase="completed", message="완료 · 새 보고서를 아래에서 확인하세요",
                                       finished_at=_now(), report_version=self._report_version())
        except Exception as error:
            with self.lock:
                if self.status["phase"] == "running":
                    self.status.update(phase="error", message=str(error), finished_at=_now())

    def cancel(self):
        with self.lock:
            if self.status["phase"] != "running":
                raise RuntimeError("실행 중인 작업이 없습니다")
            self.status.update(phase="canceled", message="사용자가 실행을 중단했습니다", finished_at=_now())
            process = self.process
        if process and process.poll() is None:
            process.terminate()


def _summary() -> dict | None:
    if not SUMMARY.exists():
        return None
    data = json.loads(SUMMARY.read_text(encoding="utf-8"))
    return {"generated_at": data["generated_at"], "period": data["period"],
            "strategy": {key: data["strategy"][key] for key in
                         ("lookback_minutes", "rise_pct", "trail_pct", "stop_pct")},
            "coverage": data["recall"]["coverage"], "selected": data["recall"]["selected"],
            "top100": data["top_prior"]["summary"]}


def make_handler(controller: Controller, port: int):
    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self) -> bool:
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code: int, content: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def _json(self, code: int, value: dict | None):
            self._send(code, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "허용되지 않은 호스트입니다"})
            path = self.path.partition("?")[0]
            if path == "/api/status":
                return self._json(200, controller.snapshot())
            if path == "/api/summary":
                return self._json(200, _summary())
            if path == "/api/ip":
                try:
                    with urllib.request.urlopen("https://api.ipify.org", timeout=6) as response:
                        address = response.read(64).decode().strip()
                    ipaddress.ip_address(address)
                    return self._json(200, {"ip": address})
                except Exception:
                    return self._json(503, {"error": "외부 IP 조회에 실패했습니다"})
            if path in ALLOWED_FILES:
                file, kind = ALLOWED_FILES[path]
                if not file.exists():
                    return self._json(404, {"error": "보고서가 없습니다. 분석을 실행하세요"})
                content = file.read_bytes()
                if path == "/":
                    content = content.replace(b"__CSRF_TOKEN__", controller.token.encode())
                return self._send(200, content, kind)
            return self._json(404, {"error": "찾을 수 없습니다"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "허용되지 않은 호스트입니다"})
            if self.headers.get("Origin") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self._json(403, {"error": "허용되지 않은 출처입니다"})
            if self.headers.get("X-SurgePilot-Token") != controller.token:
                return self._json(403, {"error": "요청 토큰이 유효하지 않습니다"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "JSON 요청만 허용합니다"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 4096:
                    return self._json(413, {"error": "요청이 너무 큽니다"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON 객체를 보내세요")
                if self.path == "/api/run":
                    controller.start(payload.get("action"), _parameters(payload))
                    return self._json(202, controller.snapshot())
                if self.path == "/api/cancel":
                    controller.cancel()
                    return self._json(200, controller.snapshot())
                return self._json(404, {"error": "찾을 수 없습니다"})
            except (ValueError, TypeError, KeyError) as error:
                return self._json(400, {"error": str(error)})
            except RuntimeError as error:
                return self._json(409, {"error": str(error)})

        def log_message(self, format, *args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Local-only SurgePilot research app")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024-65535")
    url = f"http://127.0.0.1:{args.port}/"
    controller = Controller()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller, args.port))
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    print(f"SurgePilot 로컬 앱: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if controller.snapshot()["phase"] == "running":
            controller.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
