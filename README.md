# CDP Capture

`cdp_capture.py` captures WebSocket traffic, HTTP traffic, or both from an already opened Chromium-based browser page through the Chrome DevTools Protocol (CDP).

It attaches to an existing browser debug port, listens to `Network` events, applies optional filters, and writes JSONL output files.

## Why

This tool is useful when browser traffic is the real source of truth and you need a small, scriptable way to capture WebSocket frames or HTTP responses without building a larger CDP automation stack.

## Requirements

- Python 3.10+
- `websocket-client`
- a Chromium-based browser started with remote debugging enabled

Example CDP endpoint:

```text
http://127.0.0.1:9222
```

## Installation

```bash
python3 -m pip install -r requirements.txt
```

## Quick Start

List available pages:

```bash
python3 cdp_capture.py --list-pages
```

Capture both WebSocket and HTTP traffic:

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --reload
```

Capture only WebSocket traffic:

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --capture ws
```

Capture only HTTP traffic:

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --capture http \
  --reload
```

Minimal full flow:

```bash
python3 cdp_capture.py --list-pages
python3 cdp_capture.py --page-hint example.com/app --capture both --reload
```

## Key Options

- `--config`: JSON config file
- `--cdp-base`: CDP base URL
- `--page-id`: target page id
- `--page-hint`: keyword used to match page URL or title
- `--capture {ws,http,both}`: capture mode
- `--reload`: reload the page after attaching
- `--output-dir`: output directory
- `--overwrite`: truncate output files before capture starts
- `--no-raw`: do not write raw JSONL files
- `--print-match`: print a preview for matched records

For exact defaults:

```bash
python3 cdp_capture.py --help
```

## Output Files

The tool builds these output paths under the output directory:

- `ws_matches.jsonl`
- `ws_raw.jsonl`
- `http_matches.jsonl`
- `http_raw.jsonl`

Actual writes depend on runtime options:

- `--capture ws`: only WebSocket records are written
- `--capture http`: only HTTP records are written
- `--capture both`: both protocols are written
- `--no-raw`: raw files are not written

## Config File

You can load defaults from a JSON config file:

```bash
python3 cdp_capture.py --config config.example.json
```

Explicit CLI arguments override config values.

## Notes

- If `--reload` is not used, only traffic that happens after the script attaches to the page is visible.
- Captured files may contain sensitive headers, cookies, request bodies, or response bodies.
- The browser must already be running with a remote debugging port enabled.
