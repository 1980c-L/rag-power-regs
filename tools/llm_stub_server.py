# -*- coding: utf-8 -*-
"""本机 LLM 桩 —— **只用于验收，不是产品的一部分**。

存在的唯一目的：让「生成层成功」和「生成层失败」两条路径在**不调用任何真实模型**的前提下
可以被动真格地验证。它假装自己是 OpenAI 兼容接口的一个端点：

    POST /v1/chat/completions   → {"choices":[{"message":{"content": "<桩回答>"}}]}
    GET  /__stub__/count        → {"count": N}（累计收到的生成请求数）
    GET  /__stub__/log          → 每次请求的 JSONL（便于复核"到底调了几次、有没有带 Key"）

规则：
  - 只绑 127.0.0.1，绝不联网、绝不转发、不接触任何真实供应商；
  - 桩回答**引用的是它自己在 prompt 里真实看到的 chunk id**（形如 `[xxx#yyy]`），
    因此"引用解析"这条链路仍然是在真实输入上跑的，不是写死的假引用；
  - 每次请求把 `answer` 全文写进日志，验收脚本据此独立复算引用，避免"自证"；
  - `--mode fail` 时固定返回 500。

运行：
    python tools/llm_stub_server.py --port 5281 --log <日志文件> [--mode ok|fail]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
ID_RE = re.compile(r"\[([^\]\n]{1,120})\](?!\()")     # 与 rag_core 的引用 token 规则一致
BLOCK_HEAD_RE = re.compile(r"^\[([^\]\n]{1,120})\](?!\()")

STATE = {"count": 0, "mode": "ok", "log": None}


def _materials_of(prompt: str) -> str:
    """只看【资料】段落。

    ⚠️ 必须切**最后**一个 `【资料】`：prompt 的开头说明里也有「下面提供的【资料】回答问题」
    这句话，按第一次出现切会把整段说明（含 `[来源id]` 示例）当成资料。
    """
    for sep in ("\n\n【资料】", "【资料】"):
        if sep in prompt:
            return prompt.rsplit(sep, 1)[1].split("\n\n【问题】", 1)[0]
    return prompt


def _context_ids(prompt: str, limit: int = 0) -> list:
    """取出资料块的**块首标注** id（形如 `[air-evaluation#s2p2] 正文…`）。

    只认块首：正文里可能出现 `[^1]` 这类脚注标记、说明里还有 `[来源id]` 示例，
    它们都不是"这一段资料的 id"。
    """
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
        return "【本机桩】资料里没有可引用的片段，因此不作答。"
    cited = " ".join(f"[{i}]" for i in ids)
    return ("【本机桩回答·非真实模型】根据资料，这里只复述检索到的片段线索，用于验证"
            f"端到端链路：{cited}。本回答由本机桩生成，不代表任何真实模型输出。")


class Handler(BaseHTTPRequestHandler):
    server_version = "llm-stub"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # noqa: A003
        sys.stderr.write("[stub] %s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, entry: dict) -> None:
        log: Path = STATE["log"]
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def do_GET(self) -> None:                 # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/__stub__/count":
            self._json(200, {"count": STATE["count"], "mode": STATE["mode"]})
        elif path == "/__stub__/log":
            entries = []
            log: Path = STATE["log"]
            if log.exists():
                entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
            self._json(200, {"entries": entries})
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:                # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:                     # noqa: BLE001
            payload = {}
        prompt = ""
        msgs = payload.get("messages") or []
        if msgs and isinstance(msgs, list):
            prompt = str(msgs[0].get("content", ""))
        auth = self.headers.get("Authorization") or ""
        STATE["count"] += 1
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "path": path,
            "mode": STATE["mode"],
            "model": payload.get("model", ""),
            "prompt_chars": len(prompt),
            "ids_in_prompt": _context_ids(prompt, limit=8),
            # 只记录"有没有带凭据"，绝不记录凭据内容
            "auth_present": auth.lower().startswith("bearer "),
            "raw_body_chars": len(raw),
        }
        if path != "/v1/chat/completions":
            entry["status"] = 404
            self._record(entry)
            self._json(404, {"error": {"message": "只提供 /v1/chat/completions"}})
            return
        if STATE["mode"] == "fail":
            entry["status"] = 500
            entry["answer"] = ""
            self._record(entry)
            self._json(500, {"error": {"message": "stub 故意失败（测试用）"}})
            return
        answer = _answer_for(prompt)
        entry["status"] = 200
        entry["answer"] = answer          # 落盘原文，便于验收脚本独立复算引用
        self._record(entry)
        self._json(200, {
            "id": "stub-1",
            "object": "chat.completion",
            "model": payload.get("model", "stub"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
        })


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="本机 LLM 桩（仅供验收，绝不联网）")
    ap.add_argument("--port", type=int, default=5281)
    ap.add_argument("--log", required=True, help="每次请求写入的 JSONL 日志路径")
    ap.add_argument("--mode", choices=["ok", "fail"], default="ok")
    args = ap.parse_args(argv)

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    STATE["log"] = log
    STATE["mode"] = args.mode

    print(f"[stub] 绑定 http://{HOST}:{args.port}（只绑本机回环，不联网）mode={args.mode} log={log}", flush=True)
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
