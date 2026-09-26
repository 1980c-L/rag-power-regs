# -*- coding: utf-8 -*-
"""批量提问验收专用本机桩 —— tools/batch_stub_server.py（**只用于验收，不是产品的一部分**）

与 `tools/llm_stub_server.py` 的分工（后者被 39/41/46 号证据复用，本轮一字未改）：
  - llm_stub_server.py：单题链路的桩，回答原文按 prompt 里真实看到的 chunk id 生成；
  - 本文件：只服务批量验收，额外提供三件单题桩没有的能力：

      1. **并发探针**：记录 `in_flight` 与 `max_in_flight` —— 用来证明批量生成"并发固定为 1"；
      2. **行为序列**：第 N 次请求可以指定返回 500 / 401 / 拒答文案 / 结构异常，
         用来验证"系统性错误后不再启动后续"与"非系统错误只记这一条失败"；
      3. **延迟注入**：让"取消正在运行的那一条"这条用例可稳定复现。

规矩：
  - 只绑 127.0.0.1，绝不联网、绝不转发、不接触任何真实供应商；
  - 只记录"有没有带凭据"（布尔），绝不记录凭据内容；
  - 桩回答引用的是它自己在 prompt 里真实看到的 chunk id，引用链路仍是真跑。

端点：
    POST /v1/chat/completions    生成（动作由 script 决定）
    GET  /__batch_stub__/state   计数 / 并发峰值 / 请求日志
    GET  /__batch_stub__/reset?script=ok,http500&delay=0.05   重置动作序列与计数

运行：
    python tools/batch_stub_server.py --port 5291 --log <jsonl> [--script ok,http500] [--delay 0.05]
script 语义：第 N 次请求取第 N 个动作；**超出后沿用最后一个动作**；空 = 全部 ok。
可用动作：ok / refuse / http500 / http401 / garbage
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
ID_RE = re.compile(r"\[([^\]\n]{1,120})\](?!\()")
BLOCK_HEAD_RE = re.compile(r"^\[([^\]\n]{1,120})\](?!\()")

ACTIONS = ("ok", "refuse", "http500", "http401", "garbage")
REFUSAL_TEXT = "资料中未找到相关依据：本机桩按脚本返回的拒答文案。"

STATE = {
    "count": 0,
    "in_flight": 0,
    "max_in_flight": 0,
    "script": [],
    "delay": 0.05,
    "log": None,
}
_LOCK = threading.Lock()


def _materials_of(prompt: str) -> str:
    for sep in ("\n\n【资料】", "【资料】"):
        if sep in prompt:
            return prompt.rsplit(sep, 1)[1].split("\n\n【问题】", 1)[0]
    return prompt


def _context_ids(prompt: str, limit: int = 3) -> list:
    out: list = []
    for block in _materials_of(prompt or "").split("\n\n"):
        line = block.strip()
        if not line.startswith("["):
            continue
        m = BLOCK_HEAD_RE.match(line)
        if not m:
            continue
        rid = re.sub(r"^(?:来源|出处)\s*[:：]?\s*", "", m.group(1).strip().strip("`").strip()).strip()
        if rid and rid not in out:
            out.append(rid)
            if limit and len(out) >= limit:
                break
    return out


def _answer_for(prompt: str) -> str:
    ids = _context_ids(prompt, limit=3)
    if not ids:
        return "【批量桩】资料里没有可引用的片段，因此不作答。"
    cited = " ".join(f"[{i}]" for i in ids)
    return (f"【批量桩回答·非真实模型】按资料复述线索：{cited}。"
            "本回答由本机桩生成，不代表任何真实模型输出。")


def _action_for(n: int) -> str:
    script = STATE["script"]
    if not script:
        return "ok"
    return script[min(n, len(script)) - 1]


class Handler(BaseHTTPRequestHandler):
    server_version = "batch-stub"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:            # noqa: A003
        sys.stderr.write("[batch-stub] %s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, entry: dict) -> None:
        log: Path = STATE["log"]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def do_GET(self) -> None:                                  # noqa: N802
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path.rstrip("/")
        params = urllib.parse.parse_qs(raw_query)
        if path == "/__batch_stub__/state":
            with _LOCK:
                self._json(200, {"count": STATE["count"], "in_flight": STATE["in_flight"],
                                 "max_in_flight": STATE["max_in_flight"],
                                 "script": STATE["script"], "delay": STATE["delay"]})
        elif path == "/__batch_stub__/reset":
            script = (params.get("script") or [""])[0].strip()
            delay = (params.get("delay") or [""])[0].strip()
            with _LOCK:
                STATE["count"] = 0
                STATE["in_flight"] = 0
                STATE["max_in_flight"] = 0
                STATE["script"] = [a for a in script.split(",") if a]
                if delay:
                    STATE["delay"] = float(delay)
            STATE["log"].write_text("", encoding="utf-8")
            self._json(200, {"reset": True, "script": STATE["script"], "delay": STATE["delay"]})
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:                                 # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:                                      # noqa: BLE001
            payload = {}
        prompt = ""
        msgs = payload.get("messages") or []
        if msgs and isinstance(msgs, list):
            prompt = str(msgs[0].get("content", ""))
        auth = self.headers.get("Authorization") or ""

        with _LOCK:
            STATE["count"] += 1
            n = STATE["count"]
            STATE["in_flight"] += 1
            STATE["max_in_flight"] = max(STATE["max_in_flight"], STATE["in_flight"])
            action = _action_for(n)
            delay = STATE["delay"]
        entered = time.time()
        time.sleep(max(delay, 0.0))

        if path != "/v1/chat/completions":
            with _LOCK:
                STATE["in_flight"] -= 1
            self._record({"n": n, "ts": entered, "path": path, "action": "not_found",
                          "auth_present": auth.lower().startswith("bearer ")})
            self._json(404, {"error": {"message": "只提供 /v1/chat/completions"}})
            return

        entry = {"n": n, "ts": entered, "ts_end": time.time(), "action": action,
                 "prompt_chars": len(prompt), "ids_in_prompt": _context_ids(prompt, limit=8),
                 "auth_present": auth.lower().startswith("bearer ")}
        try:
            if action == "http500":
                entry["status"] = 500
                self._record(entry)
                self._json(500, {"error": {"message": "批量桩故意失败（500）"}})
                return
            if action == "http401":
                entry["status"] = 401
                self._record(entry)
                self._json(401, {"error": {"message": "批量桩故意返回认证失败（401）"}})
                return
            if action == "garbage":
                entry["status"] = 200
                entry["answer"] = "[结构异常]"
                self._record(entry)
                self._json(200, {"id": "batch-stub", "choices": "not-a-list"})
                return
            answer = REFUSAL_TEXT if action == "refuse" else _answer_for(prompt)
            entry["status"] = 200
            entry["answer"] = answer
            self._record(entry)
            self._json(200, {"id": f"batch-stub-{n}", "object": "chat.completion",
                             "model": payload.get("model", "batch-stub"),
                             "choices": [{"index": 0,
                                          "message": {"role": "assistant", "content": answer},
                                          "finish_reason": "stop"}]})
        finally:
            with _LOCK:
                STATE["in_flight"] -= 1


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="批量验收专用本机桩（绝不联网）")
    ap.add_argument("--port", type=int, default=5291)
    ap.add_argument("--log", required=True)
    ap.add_argument("--script", default="", help="逗号分隔的动作序列：ok,refuse,http500,http401,garbage")
    ap.add_argument("--delay", type=float, default=0.05, help="每次响应的固定延迟（秒）")
    args = ap.parse_args(argv)

    for action in [a for a in args.script.split(",") if a]:
        if action not in ACTIONS:
            raise SystemExit(f"未知动作：{action}（可用：{', '.join(ACTIONS)}）")

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    STATE["log"] = log
    STATE["script"] = [a for a in args.script.split(",") if a]
    STATE["delay"] = args.delay

    print(f"[batch-stub] 绑定 http://{HOST}:{args.port}（只绑本机回环）"
          f"script={STATE['script'] or ['ok']} delay={args.delay}", flush=True)
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
