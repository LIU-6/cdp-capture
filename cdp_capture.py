#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, TextIO
from urllib.parse import urljoin
from urllib.request import urlopen

import websocket


DEFAULT_CDP_BASE = "http://127.0.0.1:9222"
DEFAULT_OUTPUT_DIR = "capture_data"
DEFAULT_CAPTURE = "both"
STATUS_INTERVAL_SECONDS = 60

logger = logging.getLogger(__name__)
CONFIG_LIST_KEYS = {
    "ws_url_match",
    "http_url_match",
    "ws_match",
    "http_match",
}
CONFIG_BOOL_KEYS = {
    "reload",
    "list_pages",
    "overwrite",
    "no_raw",
    "print_match",
}
CONFIG_VALUE_KEYS = {
    "cdp_base",
    "capture",
    "page_id",
    "page_hint",
    "output_dir",
}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def log_status(message: str, *args) -> None:
    logger.info("[status] " + message, *args)


def log_action(message: str, *args) -> None:
    logger.info("[action] " + message, *args)


def log_error(message: str, *args) -> None:
    logger.error("[error] " + message, *args)


def log_done(message: str, *args, level: int = logging.INFO) -> None:
    logger.log(level, "[done] " + message, *args)


class MatchRule:
    def __init__(self, raw: str):
        self.raw = raw
        self.is_regex = raw.startswith("re:")
        self.pattern = raw[3:] if self.is_regex else raw
        self.regex = re.compile(self.pattern) if self.is_regex else None

    def matches(self, text: str) -> bool:
        if self.regex is not None:
            return bool(self.regex.search(text))
        return self.pattern in text


class CdpClient:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(
            ws_url,
            timeout=5,
            suppress_origin=True,
        )
        self.ws.settimeout(1.0)
        self.next_id = 1
        self.pending: Dict[int, dict] = {}
        self.lock = threading.Lock()

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))

        while True:
            raw = self.ws.recv()
            message = json.loads(raw)

            if "id" in message:
                if message["id"] == request_id:
                    if "error" in message:
                        raise RuntimeError(
                            f"CDP call failed for {method}: {message['error']}"
                        )
                    return message.get("result", {})
                self.pending[message["id"]] = message
                continue

            self.pending.setdefault(-1, []).append(message)

    def recv_event(self) -> dict:
        buffered = self.pending.get(-1)
        if buffered:
            return buffered.pop(0)
        while True:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                return {}
            message = json.loads(raw)
            if "method" in message:
                return message
            if "id" in message:
                self.pending[message["id"]] = message

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


@dataclass
class WsCaptureState:
    connections: Dict[str, str]
    raw_count: int = 0
    match_count: int = 0
    first_logged: bool = False

    def __init__(self) -> None:
        self.connections = {}
        self.raw_count = 0
        self.match_count = 0
        self.first_logged = False


@dataclass
class HttpCaptureState:
    requests: Dict[str, dict]
    raw_count: int = 0
    match_count: int = 0
    first_logged: bool = False

    def __init__(self) -> None:
        self.requests = {}
        self.raw_count = 0
        self.match_count = 0
        self.first_logged = False


@dataclass
class CaptureConfig:
    cdp_base: str
    capture: str
    reload: bool
    page_id: Optional[str]
    page_hint: Optional[str]
    output_dir: str
    overwrite: bool
    save_raw: bool
    print_match: bool
    ws_match_rules: List["MatchRule"]
    http_match_rules: List["MatchRule"]
    ws_url_match_rules: List["MatchRule"]
    http_url_match_rules: List["MatchRule"]


@dataclass
class OutputHandles:
    ws_match_fp: Optional[TextIO]
    ws_raw_fp: Optional[TextIO]
    http_match_fp: Optional[TextIO]
    http_raw_fp: Optional[TextIO]

    def close(self) -> None:
        for fp in (self.ws_match_fp, self.ws_raw_fp, self.http_match_fp, self.http_raw_fp):
            if fp:
                fp.close()


@dataclass
class CaptureRuntime:
    config: CaptureConfig
    page: dict
    paths: Dict[str, str]
    client: "CdpClient"
    ws_state: WsCaptureState
    http_state: HttpCaptureState
    outputs: OutputHandles
    last_status_at: float
    last_status_snapshot: tuple


@dataclass
class WsFrameContext:
    request_id: str
    ws_url: str
    direction: str
    opcode: object
    payload: str


@dataclass
class HttpResponseContext:
    request_id: str
    entry: dict
    response_body: str
    body_base64: object
    body_error: Optional[str]


def fetch_json(url: str) -> object:
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def list_page_targets(cdp_base: str) -> List[dict]:
    targets = fetch_json(urljoin(cdp_base, "/json/list"))
    if not isinstance(targets, list):
        raise RuntimeError("Unexpected response format from CDP /json/list")
    return [target for target in targets if target.get("type") == "page"]


def choose_target(cdp_base: str, page_id: Optional[str], page_hint: Optional[str]) -> dict:
    pages = list_page_targets(cdp_base)

    if page_id:
        for page in pages:
            if page.get("id") == page_id:
                return page
        raise RuntimeError(f"Page with id={page_id} was not found")

    if page_hint:
        for page in pages:
            url = page.get("url", "")
            title = page.get("title", "")
            if page_hint in url or page_hint in title:
                return page
        raise RuntimeError(
            f"No page matched page_hint={page_hint!r}; check the keyword or use --page-id"
        )

    raise RuntimeError("Please provide --page-id or --page-hint to select a target page")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def truncate_file(path: str) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8"):
        pass


def safe_json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


def protocol_enabled(capture_mode: str, protocol: str) -> bool:
    return capture_mode in (protocol, "both")


def text_char_length(value: Optional[str]) -> int:
    return len(value or "")


def text_byte_length(value: Optional[str]) -> int:
    return len((value or "").encode("utf-8"))


def config_key_to_flag(key: str) -> str:
    return f"--{key.replace('_', '-')}"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise RuntimeError("Config file must contain a top-level JSON object")
    return data


def config_to_argv(config: dict) -> List[str]:
    argv: List[str] = []

    for key in CONFIG_VALUE_KEYS:
        if key in config and config[key] is not None:
            argv.extend([config_key_to_flag(key), str(config[key])])

    for key in CONFIG_LIST_KEYS:
        if key not in config or config[key] is None:
            continue
        value = config[key]
        values = value if isinstance(value, list) else [value]
        for item in values:
            argv.extend([config_key_to_flag(key), str(item)])

    for key in CONFIG_BOOL_KEYS:
        if config.get(key):
            argv.append(config_key_to_flag(key))

    return argv


def merge_config_args(argv: List[str]) -> List[str]:
    config_path = None
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            config_path = argv[index + 1]
            break
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            break

    if not config_path:
        return argv

    config = load_config(config_path)
    config_argv = config_to_argv(config)
    return ["--config", config_path, *config_argv, *argv]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture WebSocket frames and HTTP traffic from an already opened browser page via CDP."
    )
    parser.add_argument("--config", help="Path to a JSON config file")
    parser.add_argument("--cdp-base", default=DEFAULT_CDP_BASE, help="CDP base URL")
    parser.add_argument(
        "--capture",
        choices=("ws", "http", "both"),
        default=DEFAULT_CAPTURE,
        help="Traffic type to capture; defaults to both",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload the page after connecting to CDP to recapture websocket creation events and new HTTP traffic",
    )
    parser.add_argument(
        "--list-pages",
        action="store_true",
        help="List available page targets and exit",
    )
    parser.add_argument("--page-id", help="Target page id")
    parser.add_argument(
        "--page-hint",
        help="Keyword used to match page url/title when selecting a page automatically",
    )
    parser.add_argument(
        "--ws-url-match",
        action="append",
        default=[],
        help="WebSocket URL match rule, repeatable; substring match by default, use the re: prefix for regex",
    )
    parser.add_argument(
        "--http-url-match",
        action="append",
        default=[],
        help="HTTP URL match rule, repeatable; substring match by default, use the re: prefix for regex",
    )
    parser.add_argument(
        "--ws-match",
        action="append",
        default=[],
        help="WebSocket payload match rule, repeatable; substring match by default, use the re: prefix for regex",
    )
    parser.add_argument(
        "--http-match",
        action="append",
        default=[],
        help="HTTP response body match rule, repeatable; substring match by default, use the re: prefix for regex",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory; the script writes ws/http raw and match files into it",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Truncate output files on startup; append by default",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Do not write ws_raw.jsonl or http_raw.jsonl",
    )
    parser.add_argument(
        "--print-match",
        action="store_true",
        help="Print a short preview when a message or response matches",
    )
    return parser


def matches_ws(
    payload: str,
    ws_url: str,
    payload_rules: List[MatchRule],
    ws_url_rules: List[MatchRule],
) -> bool:
    if ws_url_rules and not any(rule.matches(ws_url) for rule in ws_url_rules):
        return False
    if payload_rules and any(rule.matches(payload) for rule in payload_rules):
        return True
    return not payload_rules


def matches_http(
    url: str,
    body: str,
    body_rules: List[MatchRule],
    http_url_rules: List[MatchRule],
) -> bool:
    if http_url_rules and not any(rule.matches(url) for rule in http_url_rules):
        return False
    if body_rules and any(rule.matches(body) for rule in body_rules):
        return True
    return not body_rules


def build_output_paths(output_dir: str, overwrite: bool, capture_mode: str, save_raw: bool) -> Dict[str, str]:
    ensure_dir(output_dir)
    paths = {
        "ws_matches": os.path.join(output_dir, "ws_matches.jsonl"),
        "ws_raw": os.path.join(output_dir, "ws_raw.jsonl"),
        "http_matches": os.path.join(output_dir, "http_matches.jsonl"),
        "http_raw": os.path.join(output_dir, "http_raw.jsonl"),
    }
    if overwrite:
        for key, path in paths.items():
            protocol = "ws" if key.startswith("ws_") else "http"
            if protocol_enabled(capture_mode, protocol):
                if not save_raw and key.endswith("_raw"):
                    continue
                truncate_file(path)
    return paths


def build_capture_config(args: argparse.Namespace) -> CaptureConfig:
    return CaptureConfig(
        cdp_base=args.cdp_base,
        capture=args.capture,
        reload=args.reload,
        page_id=args.page_id,
        page_hint=args.page_hint,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
        save_raw=not args.no_raw,
        print_match=args.print_match,
        ws_match_rules=[MatchRule(raw) for raw in args.ws_match],
        http_match_rules=[MatchRule(raw) for raw in args.http_match],
        ws_url_match_rules=[MatchRule(raw) for raw in args.ws_url_match],
        http_url_match_rules=[MatchRule(raw) for raw in args.http_url_match],
    )


def open_output_handles(paths: Dict[str, str], config: CaptureConfig) -> OutputHandles:
    return OutputHandles(
        ws_match_fp=open(paths["ws_matches"], "a", encoding="utf-8")
        if protocol_enabled(config.capture, "ws")
        else None,
        ws_raw_fp=open(paths["ws_raw"], "a", encoding="utf-8")
        if protocol_enabled(config.capture, "ws") and config.save_raw
        else None,
        http_match_fp=open(paths["http_matches"], "a", encoding="utf-8")
        if protocol_enabled(config.capture, "http")
        else None,
        http_raw_fp=open(paths["http_raw"], "a", encoding="utf-8")
        if protocol_enabled(config.capture, "http") and config.save_raw
        else None,
    )


def build_runtime(config: CaptureConfig, page: dict) -> CaptureRuntime:
    ws_debugger_url = page.get("webSocketDebuggerUrl")
    if not ws_debugger_url:
        raise RuntimeError("target page does not expose webSocketDebuggerUrl; cannot connect to CDP")

    paths = build_output_paths(
        output_dir=config.output_dir,
        overwrite=config.overwrite,
        capture_mode=config.capture,
        save_raw=config.save_raw,
    )
    client = CdpClient(ws_debugger_url)
    runtime = CaptureRuntime(
        config=config,
        page=page,
        paths=paths,
        client=client,
        ws_state=WsCaptureState(),
        http_state=HttpCaptureState(),
        outputs=open_output_handles(paths, config),
        last_status_at=time.monotonic(),
        last_status_snapshot=(-1, -1, -1, -1, -1, -1),
    )
    return runtime


def write_jsonl(fp, record: dict) -> None:
    fp.write(safe_json_dumps(record))
    fp.flush()


def build_ws_frame_context(event: dict, runtime: CaptureRuntime) -> WsFrameContext:
    params = event.get("params", {})
    response = params.get("response", {})
    request_id = params.get("requestId", "")
    return WsFrameContext(
        request_id=request_id,
        ws_url=runtime.ws_state.connections.get(request_id, ""),
        direction="recv" if event.get("method", "").endswith("Received") else "sent",
        opcode=response.get("opcode"),
        payload=response.get("payloadData", ""),
    )


def build_ws_record(runtime: CaptureRuntime, context: WsFrameContext) -> dict:
    return {
        "ts": now_iso(),
        "protocol": "ws",
        "direction": context.direction,
        "page_id": runtime.page.get("id"),
        "page_title": runtime.page.get("title"),
        "page_url": runtime.page.get("url"),
        "request_id": context.request_id,
        "ws_url": context.ws_url,
        "opcode": context.opcode,
        "payload": context.payload,
        "payload_char_length": text_char_length(context.payload),
        "payload_byte_length": text_byte_length(context.payload),
    }


def log_first_ws_frame_if_needed(state: WsCaptureState, context: WsFrameContext) -> None:
    if state.first_logged:
        return
    state.first_logged = True
    log_status(
        "first ws frame captured direction=%s opcode=%s ws=%s",
        context.direction,
        context.opcode,
        context.ws_url,
    )


def log_first_http_response_if_needed(state: HttpCaptureState, record: dict) -> None:
    if state.first_logged:
        return
    state.first_logged = True
    log_status(
        "first http response captured method=%s status=%s url=%s",
        record["request_method"],
        record["status"],
        record["url"],
    )


def record_ws_raw(runtime: CaptureRuntime, record: dict) -> None:
    if not runtime.outputs.ws_raw_fp:
        return
    write_jsonl(runtime.outputs.ws_raw_fp, record)
    runtime.ws_state.raw_count += 1


def record_ws_match(runtime: CaptureRuntime, record: dict, context: WsFrameContext) -> None:
    write_jsonl(runtime.outputs.ws_match_fp, record)
    runtime.ws_state.match_count += 1

    if runtime.config.print_match:
        preview = context.payload[:180].replace("\n", "\\n")
        log_action(
            "ws match count=%s direction=%s opcode=%s ws=%s payload=%s",
            runtime.ws_state.match_count,
            context.direction,
            context.opcode,
            context.ws_url,
            preview,
        )


def build_http_response_context(runtime: CaptureRuntime, request_id: str, entry: dict) -> HttpResponseContext:
    response_body = ""
    body_base64 = None
    body_error = None
    try:
        body_result = runtime.client.call("Network.getResponseBody", {"requestId": request_id})
        response_body = body_result.get("body", "")
        body_base64 = body_result.get("base64Encoded", False)
    except Exception as exc:
        body_error = str(exc)
    return HttpResponseContext(
        request_id=request_id,
        entry=entry,
        response_body=response_body,
        body_base64=body_base64,
        body_error=body_error,
    )


def build_http_record(runtime: CaptureRuntime, context: HttpResponseContext, params: dict) -> dict:
    entry = context.entry
    return {
        "ts": now_iso(),
        "protocol": "http",
        "page_id": runtime.page.get("id"),
        "page_title": runtime.page.get("title"),
        "page_url": runtime.page.get("url"),
        "request_id": context.request_id,
        "url": entry.get("response_url") or entry.get("request_url", ""),
        "request_method": entry.get("request_method"),
        "resource_type": entry.get("resource_type"),
        "status": entry.get("status"),
        "status_text": entry.get("status_text"),
        "mime_type": entry.get("mime_type"),
        "request_headers": entry.get("request_headers", {}),
        "response_headers": entry.get("response_headers", {}),
        "request_body": entry.get("request_body"),
        "request_body_char_length": text_char_length(entry.get("request_body")),
        "request_body_byte_length": text_byte_length(entry.get("request_body")),
        "response_body": context.response_body,
        "response_body_char_length": text_char_length(context.response_body),
        "response_body_byte_length": text_byte_length(context.response_body),
        "response_body_base64": context.body_base64,
        "response_body_error": context.body_error,
        "encoded_data_length": params.get("encodedDataLength"),
        "remote_ip": entry.get("remote_ip"),
        "remote_port": entry.get("remote_port"),
        "protocol_name": entry.get("protocol_name"),
        "from_disk_cache": entry.get("from_disk_cache"),
        "from_service_worker": entry.get("from_service_worker"),
    }


def record_http_raw(runtime: CaptureRuntime, record: dict) -> None:
    if not runtime.outputs.http_raw_fp:
        return
    write_jsonl(runtime.outputs.http_raw_fp, record)
    runtime.http_state.raw_count += 1


def record_http_match(runtime: CaptureRuntime, record: dict) -> None:
    write_jsonl(runtime.outputs.http_match_fp, record)
    runtime.http_state.match_count += 1

    if runtime.config.print_match:
        preview = (record["response_body"] or "")[:180].replace("\n", "\\n")
        log_action(
            "http match count=%s method=%s status=%s url=%s body=%s",
            runtime.http_state.match_count,
            record["request_method"],
            record["status"],
            record["url"],
            preview,
        )


def build_status_snapshot(runtime: CaptureRuntime) -> tuple:
    return (
        len(runtime.ws_state.connections),
        runtime.ws_state.raw_count,
        runtime.ws_state.match_count,
        len(runtime.http_state.requests),
        runtime.http_state.raw_count,
        runtime.http_state.match_count,
    )


def handle_ws_event(event: dict, runtime: CaptureRuntime) -> bool:
    method = event.get("method")
    params = event.get("params", {})
    state = runtime.ws_state

    if method == "Network.webSocketCreated":
        request_id = params.get("requestId", "")
        ws_url = params.get("url", "")
        state.connections[request_id] = ws_url
        log_status("ws created request_id=%s url=%s", request_id, ws_url)
        return False

    if method == "Network.webSocketClosed":
        request_id = params.get("requestId", "")
        ws_url = state.connections.pop(request_id, "")
        log_status("ws closed request_id=%s url=%s", request_id, ws_url)
        return False

    if method not in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
        return False

    context = build_ws_frame_context(event, runtime)
    record = build_ws_record(runtime, context)
    record_ws_raw(runtime, record)

    log_first_ws_frame_if_needed(state, context)

    if not matches_ws(
        context.payload,
        context.ws_url,
        runtime.config.ws_match_rules,
        runtime.config.ws_url_match_rules,
    ):
        return True

    record_ws_match(runtime, record, context)
    return True


def handle_http_event(event: dict, runtime: CaptureRuntime) -> bool:
    method = event.get("method")
    params = event.get("params", {})
    state = runtime.http_state

    if method == "Network.requestWillBeSent":
        request = params.get("request", {})
        request_id = params.get("requestId", "")
        state.requests[request_id] = {
            "request_id": request_id,
            "loader_id": params.get("loaderId"),
            "document_url": params.get("documentURL"),
            "request_url": request.get("url", ""),
            "request_method": request.get("method"),
            "request_headers": request.get("headers", {}),
            "request_body": request.get("postData"),
            "resource_type": params.get("type"),
            "request_ts": params.get("timestamp"),
            "wall_time": params.get("wallTime"),
        }
        return False

    if method == "Network.responseReceived":
        request_id = params.get("requestId", "")
        response = params.get("response", {})
        entry = state.requests.setdefault(
            request_id,
            {
                "request_id": request_id,
            },
        )
        entry.update(
            {
                "response_url": response.get("url", entry.get("request_url", "")),
                "status": response.get("status"),
                "status_text": response.get("statusText"),
                "mime_type": response.get("mimeType"),
                "response_headers": response.get("headers", {}),
                "remote_ip": response.get("remoteIPAddress"),
                "remote_port": response.get("remotePort"),
                "protocol_name": response.get("protocol"),
                "from_disk_cache": response.get("fromDiskCache"),
                "from_service_worker": response.get("fromServiceWorker"),
                "encoded_data_length": response.get("encodedDataLength"),
                "resource_type": params.get("type", entry.get("resource_type")),
                "response_ts": params.get("timestamp"),
            }
        )
        return False

    if method == "Network.loadingFailed":
        request_id = params.get("requestId", "")
        state.requests.pop(request_id, None)
        return False

    if method != "Network.loadingFinished":
        return False

    request_id = params.get("requestId", "")
    entry = state.requests.pop(request_id, None)
    if not entry:
        return False

    context = build_http_response_context(runtime, request_id, entry)
    record = build_http_record(runtime, context, params)
    record_http_raw(runtime, record)

    log_first_http_response_if_needed(state, record)

    if not matches_http(
        record["url"],
        context.response_body,
        runtime.config.http_match_rules,
        runtime.config.http_url_match_rules,
    ):
        return True

    record_http_match(runtime, record)
    return True


def log_status_if_needed(runtime: CaptureRuntime, now: float) -> None:
    snapshot = build_status_snapshot(runtime)
    if now - runtime.last_status_at < STATUS_INTERVAL_SECONDS or snapshot == runtime.last_status_snapshot:
        return

    log_capture_status(
        ws_state=runtime.ws_state,
        http_state=runtime.http_state,
    )
    runtime.last_status_at = now
    runtime.last_status_snapshot = snapshot


def log_capture_status(
    ws_state: WsCaptureState,
    http_state: HttpCaptureState,
) -> None:
    log_status(
        "ws_connections=%s ws_raw=%s ws_matches=%s http_inflight=%s http_raw=%s http_matches=%s",
        len(ws_state.connections),
        ws_state.raw_count,
        ws_state.match_count,
        len(http_state.requests),
        http_state.raw_count,
        http_state.match_count,
    )


def log_startup(runtime: CaptureRuntime) -> None:
    log_status(
        "startup ready id=%s title=%r url=%s capture=%s dir=%s save_raw=%s ws_matches=%s ws_raw=%s http_matches=%s http_raw=%s",
        runtime.page.get("id"),
        runtime.page.get("title"),
        runtime.page.get("url"),
        runtime.config.capture,
        runtime.config.output_dir,
        runtime.config.save_raw,
        runtime.paths["ws_matches"],
        runtime.paths["ws_raw"],
        runtime.paths["http_matches"],
        runtime.paths["http_raw"],
    )


def enable_capture(runtime: CaptureRuntime) -> None:
    runtime.client.call("Network.enable")
    log_status("cdp connected waiting for network events")
    if runtime.config.reload:
        log_action("reload page to recapture network traffic")
        runtime.client.call("Page.reload", {"ignoreCache": False})


def process_event(runtime: CaptureRuntime, event: dict) -> None:
    if protocol_enabled(runtime.config.capture, "ws") and runtime.outputs.ws_match_fp:
        handle_ws_event(event, runtime)
    if protocol_enabled(runtime.config.capture, "http") and runtime.outputs.http_match_fp:
        handle_http_event(event, runtime)


def run_capture_loop(runtime: CaptureRuntime) -> None:
    while True:
        event = runtime.client.recv_event()
        now = time.monotonic()

        if not event:
            log_status_if_needed(runtime, now)
            continue

        process_event(runtime, event)
        log_status_if_needed(runtime, now)


def main() -> int:
    configure_logging()
    parser = build_parser()
    try:
        args = parser.parse_args(merge_config_args(sys.argv[1:]))
    except Exception as exc:
        log_error("%s", exc)
        return 2

    if args.list_pages:
        try:
            pages = list_page_targets(args.cdp_base)
        except Exception as exc:
            log_error("%s", exc)
            return 1

        for page in pages:
            print(
                json.dumps(
                    {
                        "id": page.get("id"),
                        "title": page.get("title"),
                        "url": page.get("url"),
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    try:
        config = build_capture_config(args)
    except re.error as exc:
        log_error("regex compilation failed err=%s", exc)
        return 2

    try:
        page = choose_target(config.cdp_base, config.page_id, config.page_hint)
    except Exception as exc:
        log_error("%s", exc)
        return 1

    try:
        runtime = build_runtime(config, page)
    except Exception as exc:
        log_error("%s", exc)
        return 1

    log_startup(runtime)

    try:
        try:
            enable_capture(runtime)
            run_capture_loop(runtime)
        except KeyboardInterrupt:
            log_done("interrupted by Ctrl+C")
        except websocket.WebSocketConnectionClosedException:
            log_done("cdp websocket connection was closed", level=logging.WARNING)
    finally:
        runtime.outputs.close()
        runtime.client.close()

    log_capture_status(
        ws_state=runtime.ws_state,
        http_state=runtime.http_state,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
