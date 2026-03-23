# CDP Capture

`cdp_capture.py` 用来通过 Chrome DevTools Protocol（CDP）从一个已经打开的 Chromium 浏览器页面中抓取 WebSocket 流量、HTTP 流量，或同时抓取两者。

它会连接到已有的浏览器调试端口，监听 `Network` 事件，按可选规则过滤，并把结果写入 JSONL 文件。

## 用途

当浏览器网络流量本身才是真正的数据来源时，这个工具可以用很小的脚本成本抓取 WebSocket 帧或 HTTP 响应，而不需要先搭一个更重的 CDP 自动化系统。

## 运行要求

- Python 3.10+
- `websocket-client`
- 一个已经开启远程调试端口的 Chromium 浏览器

示例 CDP 地址：

```text
http://127.0.0.1:9222
```

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 快速开始

列出当前可用页面：

```bash
python3 cdp_capture.py --list-pages
```

同时抓取 WebSocket 和 HTTP：

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --reload
```

只抓 WebSocket：

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --capture ws
```

只抓 HTTP：

```bash
python3 cdp_capture.py \
  --page-hint example.com/app \
  --capture http \
  --reload
```

一个最小完整流程：

```bash
python3 cdp_capture.py --list-pages
python3 cdp_capture.py --page-hint example.com/app --capture both --reload
```

## 关键参数

- `--config`：JSON 配置文件
- `--cdp-base`：CDP 地址
- `--page-id`：目标页面 id
- `--page-hint`：按页面 URL 或标题匹配的关键字
- `--capture {ws,http,both}`：抓取模式
- `--reload`：附着后刷新页面
- `--output-dir`：输出目录
- `--overwrite`：启动前清空输出文件
- `--no-raw`：不写 raw JSONL 文件
- `--print-match`：命中记录时打印预览

精确默认值请看：

```bash
python3 cdp_capture.py --help
```

## 输出文件

工具会在输出目录下建立这些路径：

- `ws_matches.jsonl`
- `ws_raw.jsonl`
- `http_matches.jsonl`
- `http_raw.jsonl`

实际是否写入取决于运行参数：

- `--capture ws`：只写 WebSocket
- `--capture http`：只写 HTTP
- `--capture both`：两种都写
- `--no-raw`：不写 raw 文件

## 配置文件

可以通过 JSON 配置文件加载默认参数：

```bash
python3 cdp_capture.py --config config.example.json
```

显式传入的命令行参数会覆盖配置文件中的值。

## 说明

- 如果不使用 `--reload`，脚本只能看到它附着到页面之后产生的流量。
- 抓取结果里可能包含敏感请求头、Cookie、请求体或响应体。
- 浏览器必须已经开启远程调试端口。
