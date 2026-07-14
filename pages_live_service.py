# -*- coding: utf-8 -*-
"""GitHub Pages 风格静态站点服务，每 5 分钟刷新 rqdata 行情并重建 dist/。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from online_refresh import now_beijing_iso, now_beijing_label, poll_remote_opportunities

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
LOG_PATH = BASE_DIR / "pages_live_service.log"
CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PYTHON = Path(r"D:\veighna_studio\python.exe")
REFRESH_SECONDS = 300

SELL_PUT_CACHE: dict[str, dict] = {}
SELL_PUT_CACHE_LOCK = threading.Lock()
SELL_PUT_SCANNING: set[str] = set()


def _load_sell_put_cache_from_disk(mode: str) -> dict | None:
    path = DIST_DIR / "sell-put-monitor" / f"board-quotes-{mode}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _start_sell_put_scan(mode: str) -> None:
    with SELL_PUT_CACHE_LOCK:
        if mode in SELL_PUT_SCANNING:
            return
        SELL_PUT_SCANNING.add(mode)

    def worker() -> None:
        try:
            from sell_put_app import fetch_live_board_quotes

            payload = fetch_live_board_quotes(mode)
            with SELL_PUT_CACHE_LOCK:
                SELL_PUT_CACHE[mode] = payload
            static_path = DIST_DIR / "sell-put-monitor" / f"board-quotes-{payload['selection_mode']}.json"
            static_path.parent.mkdir(parents=True, exist_ok=True)
            static_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"Put/Call 板块后台扫描完成: {mode}")
        except Exception as exc:
            log(f"Put/Call 板块后台扫描失败: {exc}\n{traceback.format_exc()}")
        finally:
            with SELL_PUT_CACHE_LOCK:
                SELL_PUT_SCANNING.discard(mode)

    threading.Thread(target=worker, daemon=True).start()


def _sell_put_cached_payload(mode: str) -> tuple[dict | None, bool]:
    with SELL_PUT_CACHE_LOCK:
        if mode not in SELL_PUT_CACHE:
            disk_payload = _load_sell_put_cache_from_disk(mode)
            if disk_payload:
                SELL_PUT_CACHE[mode] = disk_payload
        cached = SELL_PUT_CACHE.get(mode)
        scanning = mode in SELL_PUT_SCANNING
    return cached, scanning


def now_label() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def log(message: str) -> None:
    line = f"[{now_label()}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def python_exe() -> str:
    if DEFAULT_PYTHON.exists():
        return str(DEFAULT_PYTHON)
    return sys.executable


def build_all() -> dict[str, object]:
    py = python_exe()
    steps = [
        ([py, "monitor_server.py", "--export-static", "--source", "auto", "--funding-rate", "0.03"], "convertible-spread"),
        ([py, "option_vertical_app.py", "--export-static"], "option-vertical-spread"),
        ([py, "sell_put_app.py", "--export-static"], "sell-put-monitor"),
    ]
    summary: dict[str, object] = {"steps": {}, "updated_at_label": now_label()}
    for cmd, name in steps:
        log(f"开始构建 {name} ...")
        try:
            proc = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                summary["steps"][name] = {
                    "ok": False,
                    "error": (proc.stderr or proc.stdout or "").strip()[-500:],
                }
                log(f"构建 {name} 失败 (exit {proc.returncode})")
                continue
            payload = parse_json_output(proc.stdout)
            summary["steps"][name] = payload
            log(f"完成构建 {name}")
            if name == "convertible-spread":
                try:
                    from deploy_files_to_ghpages import deploy_file

                    deploy_result = deploy_file("data/latest_monitor.json")
                    summary["steps"][name]["deploy"] = deploy_result
                    log(f"可转抛数据已同步到 gh-pages: {deploy_result.get('commit')}")
                except Exception as exc:
                    summary["steps"][name]["deploy_error"] = str(exc)
                    log(f"可转抛数据同步到 gh-pages 失败: {exc}")
        except Exception as exc:
            summary["steps"][name] = {"ok": False, "error": str(exc)}
            log(f"构建 {name} 异常: {exc}")
    return summary


def parse_json_output(output: str) -> dict:
    text = output.strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if text[index + end :].strip():
            continue
        return payload if isinstance(payload, dict) else {"value": payload}
    return {"raw_output": text[-500:]}


class PagesHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST_DIR), **kwargs)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/option-vertical-spread/opportunities":
            self._handle_option_opportunities(parsed.query)
            return
        if parsed.path == "/api/github/refresh/option-vertical-spread":
            self._handle_github_refresh(parsed.query)
            return
        if parsed.path == "/api/sell-put-monitor/board-quotes":
            self._handle_sell_put_board_quotes(parsed.query)
            return
        if parsed.path == "/api/sell-put-monitor/progress":
            self._handle_sell_put_progress()
            return
        return super().do_GET()

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle_github_refresh(self, query: str) -> None:
        params = parse_qs(query)
        previous = params.get("since", [None])[0]
        try:
            payload = poll_remote_opportunities(previous_updated_at=previous, timeout=240)
            self._send_json(payload)
        except Exception as exc:
            log(f"GitHub 刷新轮询失败: {exc}\n{traceback.format_exc()}")
            self._send_json(
                {
                    "ok": False,
                    "message": str(exc),
                    "updated_at": now_beijing_iso(),
                    "updated_at_label": now_beijing_label(),
                    "data_mode": "github_refresh",
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_option_opportunities(self, query: str) -> None:
        try:
            from option_vertical_app import RqdataAdapter, all_option_symbols, load_config

            config = load_config()
            adapter = RqdataAdapter(config)
            params = parse_qs(query)
            symbols_param = params.get("symbols", params.get("symbol", ["ALL"]))[0].strip().upper()
            if symbols_param in {"", "ALL", "__ALL__"}:
                symbols = all_option_symbols(config)
            else:
                symbols = [item.strip().upper() for item in re.split(r"[,，\s]+", symbols_param) if item.strip()]
            payload = adapter.scan_many_arbitrage(symbols)
            payload["data_mode"] = "live"
            payload["updated_at"] = now_beijing_iso()
            payload["updated_at_label"] = now_beijing_label()
            payload["refreshed_at_label"] = now_beijing_label()
            static_path = DIST_DIR / "option-vertical-spread" / "opportunities.json"
            static_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json(payload)
        except Exception as exc:
            log(f"期权垂直价差实时刷新失败: {exc}\n{traceback.format_exc()}")
            self._send_json(
                {
                    "ok": False,
                    "message": str(exc),
                    "updated_at": now_beijing_iso(),
                    "updated_at_label": now_beijing_label(),
                    "data_mode": "live",
                    "puts": [],
                    "calls": [],
                    "put_opportunities": [],
                    "call_opportunities": [],
                    "opportunities": [],
                    "errors": [str(exc)],
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_sell_put_progress(self) -> None:
        try:
            from sell_put_app import _get_progress

            payload = {"ok": True, **_get_progress()}
            self._send_json(payload)
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_sell_put_board_quotes(self, query: str) -> None:
        params = parse_qs(query)
        mode = params.get("mode", ["dominant"])[0]
        if mode not in {"dominant", "option_volume"}:
            mode = "dominant"
        quick = params.get("quick", ["1"])[0] != "0"
        wait = params.get("wait", ["0"])[0] == "1"

        if wait and not quick:
            try:
                from sell_put_app import fetch_live_board_quotes

                payload = fetch_live_board_quotes(mode)
                with SELL_PUT_CACHE_LOCK:
                    SELL_PUT_CACHE[mode] = payload
                static_path = DIST_DIR / "sell-put-monitor" / f"board-quotes-{payload['selection_mode']}.json"
                static_path.parent.mkdir(parents=True, exist_ok=True)
                static_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send_json(payload)
                return
            except Exception as exc:
                log(f"Put/Call 板块实时刷新失败: {exc}\n{traceback.format_exc()}")
                self._send_json(
                    {
                        "ok": False,
                        "message": str(exc),
                        "updated_at": now_beijing_iso(),
                        "updated_at_label": now_beijing_label(),
                        "data_mode": "live",
                        "sections": {"put": {"groups": [], "errors": []}, "call": {"groups": [], "errors": []}},
                        "errors": [str(exc)],
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        cached, scanning = _sell_put_cached_payload(mode)
        if not scanning:
            _start_sell_put_scan(mode)
            cached, scanning = _sell_put_cached_payload(mode)

        if cached:
            response = dict(cached)
            response["scan_in_progress"] = scanning
            response["data_mode"] = "live_refreshing" if scanning else "live_cached"
            if scanning:
                response["message"] = "后台正在扫描最新行情，当前显示缓存数据"
            self._send_json(response)
            return

        self._send_json(
            {
                "ok": True,
                "sections": {"put": {"groups": [], "errors": []}, "call": {"groups": [], "errors": []}},
                "errors": [],
                "selection_mode": mode,
                "option_side": "all",
                "poll_seconds": 300,
                "updated_at": now_beijing_iso(),
                "updated_at_label": now_beijing_label(),
                "data_mode": "live_refreshing",
                "scan_in_progress": True,
                "message": "首次加载，后台扫描进行中，请稍后再次刷新",
            }
        )

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def refresh_loop(stop_event: threading.Event, interval: int) -> None:
    while not stop_event.wait(interval):
        try:
            summary = build_all()
            log(f"定时刷新完成: {json.dumps(summary.get('steps', {}), ensure_ascii=False)}")
        except Exception as exc:
            log(f"定时刷新失败: {exc}\n{traceback.format_exc()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Pages 风格本地站点 + rqdata 定时刷新")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--interval", type=int, default=REFRESH_SECONDS, help="刷新间隔秒数，默认 300")
    parser.add_argument("--skip-initial-build", action="store_true")
    args = parser.parse_args()

    if not args.skip_initial_build:
        log("首次构建静态站点 ...")
        build_all()

    stop_event = threading.Event()
    worker = threading.Thread(target=refresh_loop, args=(stop_event, args.interval), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.host, args.port), PagesHandler)
    log(f"Pages 服务已启动: http://127.0.0.1:{args.port}/")
    log(f"可转抛: http://127.0.0.1:{args.port}/convertible-spread/")
    log(f"期权垂直价差: http://127.0.0.1:{args.port}/option-vertical-spread/")
    log(f"Sell Put: http://127.0.0.1:{args.port}/sell-put-monitor/")
    log(f"rqdata 自动刷新间隔: {args.interval} 秒")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        log("Pages 服务已停止")


if __name__ == "__main__":
    main()
