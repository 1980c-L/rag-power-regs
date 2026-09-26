# -*- coding: utf-8 -*-
"""api_v3（第二阶段 A 本机薄接口）的接口层独立验收。

验的是**接口**，不是页面（页面在 `verify_frontend_v3.py --backend api`）：
  1. /api/meta 的字段、真实统计（与 rag_core 现算逐字段比对）、Key 只返回布尔；
  2. /api/query 的真实检索结果与**直接调用 rag_core.py** 逐字段一致（命中 id/顺序/分数、
     同节补充、上下文统计、引用解析）；
  3. 错误输入一律 fail closed（状态码 + 错误码），并覆盖未知路径 / 错方法 / CORS / 体积上限；
  4. 三条"不该调用模型"的路径（仅检索、零结果、无 Key）确实没有发出生成请求；
  5. 生成层成功 / 失败两条路径都走**本机桩**（tools/llm_stub_server.py），
     **真实模型请求为 0**：api_v3 的 base_url 被显式指到桩上，桩日志即请求台账。

运行：
    python verify_api_v3.py [--out <证据输出目录>]
本文件内不写任何个人绝对路径；不读也不打印任何 API Key 内容（只用一个哨兵值验证"没泄漏"）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import rag_core as rc                                          # noqa: E402

API_PORT = 5280
STUB_PORT = 5281
STUB_BASE = f"http://127.0.0.1:{STUB_PORT}/v1"
ORIGIN_OK = "http://127.0.0.1:5273"
ORIGIN_BAD = "http://evil.example.com"
# 哨兵 Key：只用来证明"服务端用到了它、但响应里绝不出现它"
SENTINEL_KEY = "sk-STUB-SENTINEL-DO-NOT-LEAK-9f3a"
Q_TRIAD = "RAG 评估三元组包含哪三个维度？"
Q_GROUND = "装设接地线的顺序是什么？"
Q_ZERO = "红烧肉怎么做？"

FIELDS = ("id", "origin", "rank", "score", "source_id", "source_title",
          "source_org", "source_url", "section", "text")

PASSED: list = []
FAILED: list = []
SEEN_BODIES: list = []          # 所有收到的响应原文：最后统一查一次"有没有泄漏 Key"
HANDLES: list = []              # 子进程日志句柄，结束时统一关掉


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印；**不中断**——判据要全项跑完，最后用退出码统一裁决。"""
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


# ---------------- HTTP ----------------
def http(method: str, path: str, *, payload=None, raw: bytes | None = None,
         origin: str | None = None, ctype: str | None = None, port: int = API_PORT,
         timeout: int = 90) -> tuple:
    url = f"http://127.0.0.1:{port}{path}"
    data = raw
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if ctype:
        headers["Content-Type"] = ctype
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers), body


def jbody(body: str) -> dict:
    SEEN_BODIES.append(body)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def query(payload: dict, **kw) -> dict:
    status, _, body = http("POST", "/api/query", payload=payload, **kw)
    return {"status": status, "json": jbody(body), "body": body}


# ---------------- 进程 ----------------
def spawn(cmd: list, *, env_extra: dict | None = None, drop_env: tuple = (), log: Path):
    env = dict(os.environ)
    for k in drop_env:
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    handle = log.open("w", encoding="utf-8")
    HANDLES.append(handle)
    return subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=handle,
                            stderr=subprocess.STDOUT, text=True)


def kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:                                          # noqa: BLE001
        proc.kill()


def wait_health(port: int, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            status, _, _ = http("GET", "/api/health", port=port, timeout=3)
            if status == 200:
                return True
        except Exception:                                      # noqa: BLE001
            pass
        time.sleep(0.25)
    return False


def start_stub(mode: str, log: Path) -> subprocess.Popen:
    proc = spawn([sys.executable, "tools/llm_stub_server.py", "--port", str(STUB_PORT),
                  "--mode", mode, "--log", str(log)], log=log.with_suffix(".stdout.log"))
    for _ in range(60):
        try:
            status, _, _ = http("GET", "/__stub__/count", port=STUB_PORT, timeout=3)
            if status == 200:
                return proc
        except Exception:                                      # noqa: BLE001
            pass
        time.sleep(0.25)
    raise RuntimeError("桩服务没起来")


def start_api(with_key: bool, log: Path) -> subprocess.Popen:
    env_extra = {"DEEPSEEK_API_KEY": SENTINEL_KEY} if with_key else {}
    proc = spawn([sys.executable, "api_v3.py", "--port", str(API_PORT),
                  "--llm-base-url", STUB_BASE, "--llm-model", "stub-model"],
                 env_extra=env_extra, drop_env=("DEEPSEEK_API_KEY",) if not with_key else (),
                 log=log)
    if not wait_health(API_PORT):
        raise RuntimeError("api_v3 没起来")
    return proc


def stub_count() -> int:
    _, _, body = http("GET", "/__stub__/count", port=STUB_PORT, timeout=5)
    return int(json.loads(body)["count"])


def stub_entries(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------- 期望值：直接调 rag_core ----------------
def rc_expect(corpus_id: str, question: str, top_k: int):
    assembled = rc.expand_and_assemble(question, top_k=top_k, corpus_id=corpus_id)
    ctx_map = {e["id"]: e for e in assembled["contexts"]}
    return assembled, ctx_map


def items_of(entries: list) -> list:
    return [{k: e[k] for k in FIELDS} for e in entries]


# ---------------- 各段验收 ----------------
def checks_meta(meta: dict) -> None:
    check("接口·meta：data_source=api、schema_version=2",
          meta.get("data_source") == "api" and meta.get("schema_version") == 2,
          f"data_source={meta.get('data_source')} schema={meta.get('schema_version')}")
    check("接口·meta：Key 只返回布尔（且有 Key 时为 true），响应里不出现 Key 内容",
          meta.get("api_key_configured") is True
          and isinstance(meta.get("api_key_configured"), bool)
          and SENTINEL_KEY not in json.dumps(meta, ensure_ascii=False),
          f"api_key_configured={meta.get('api_key_configured')!r}")
    check("接口·meta：Top-K 选项为 1–5",
          meta.get("top_k_choices") == [1, 2, 3, 4, 5],
          f"top_k_choices={meta.get('top_k_choices')}")

    exp = {cid: rc.corpus_stats(cid) for cid in rc.corpus_ids()}
    got = {c["corpus_id"]: c for c in meta.get("corpora", [])}
    same = all(
        got.get(cid, {}).get("source_count") == exp[cid]["source_count"]
        and got.get(cid, {}).get("chunk_count") == exp[cid]["chunk_count"]
        and got.get(cid, {}).get("total_chars") == exp[cid]["total_chars"]
        and got.get(cid, {}).get("version") == exp[cid]["version"]
        and got.get(cid, {}).get("tokenizer") == exp[cid]["tokenizer"]
        and got.get(cid, {}).get("params") == exp[cid]["params"]
        and got.get(cid, {}).get("name") == exp[cid]["name"]
        and got.get(cid, {}).get("note") == exp[cid]["note"]
        for cid in exp
    )
    check("接口·meta：两个知识库的真实统计与 rag_core 现算逐字段一致（来源数/chunk 数/字数/版本/分词器/参数）",
          same and len(got) == len(exp),
          " · ".join(f"{cid}={got.get(cid, {}).get('source_count')}来源/"
                     f"{got.get(cid, {}).get('chunk_count')}chunk" for cid in exp))
    check("接口·meta：电力示例库带「示例资料、不是正式规程」标注，学习库不带",
          got.get(rc.POWER_DEMO, {}).get("is_sample_only") is True
          and got.get(rc.POWER_DEMO, {}).get("warning")
          and got.get(rc.RAG_LEARNING, {}).get("is_sample_only") is False,
          f"power.warning={got.get(rc.POWER_DEMO, {}).get('warning')!r}")

    snapshot = REPO / "frontend_v3" / "src" / "mocks" / "api_snapshots.json"
    if snapshot.exists():
        mock_corpora = json.loads(snapshot.read_text(encoding="utf-8"))["corpora"]
        check("接口·meta：api 与 mock 快照的知识库展示字段完全一致（两端文案/数字不许漂移）",
              mock_corpora == meta.get("corpora"),
              f"mock={len(mock_corpora)} 条 / api={len(meta.get('corpora', []))} 条")


def checks_routing() -> None:
    check("接口·路由：未知路径 404",
          http("GET", "/api/nope")[0] == 404, "")
    check("接口·路由：GET /api/query 返回 405（不是 404 也不是 200）",
          http("GET", "/api/query")[0] == 405, "")
    check("接口·路由：POST /api/meta 返回 405",
          http("POST", "/api/meta", payload={})[0] == 405, "")

    status, headers, _ = http("OPTIONS", "/api/query", origin=ORIGIN_OK)
    check("接口·CORS：允许来源的预检返回 204 且回显该来源，允许 POST/GET",
          status == 204 and headers.get("Access-Control-Allow-Origin") == ORIGIN_OK
          and "POST" in (headers.get("Access-Control-Allow-Methods") or ""),
          f"status={status} ACAO={headers.get('Access-Control-Allow-Origin')}")
    status, headers, body = http("GET", "/api/meta", origin=ORIGIN_BAD)
    check("接口·CORS：未在名单里的来源被 403 拒绝，且不回显任何 CORS 头",
          status == 403 and "Access-Control-Allow-Origin" not in headers
          and jbody(body).get("error", {}).get("code") == "origin_not_allowed",
          f"status={status} body_code={jbody(body).get('error', {}).get('code')}")


BAD_PAYLOADS = [
    ("未知知识库", {"corpus_id": "nope", "mode": "generate", "top_k": 3, "question": "x"}, 400, "unknown_corpus"),
    ("未知模式", {"corpus_id": "rag_learning", "mode": "chat", "top_k": 3, "question": "x"}, 400, "unknown_mode"),
    ("Top-K 越界", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 9, "question": "x"}, 400, "top_k_out_of_range"),
    ("Top-K 为 0", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 0, "question": "x"}, 400, "top_k_out_of_range"),
    ("Top-K 是布尔", {"corpus_id": "rag_learning", "mode": "generate", "top_k": True, "question": "x"}, 400, "invalid_top_k"),
    ("Top-K 是字符串", {"corpus_id": "rag_learning", "mode": "generate", "top_k": "3", "question": "x"}, 400, "invalid_top_k"),
    ("空问题", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 3, "question": "   "}, 400, "question_required"),
    ("问题过长", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 3, "question": "问" * 501}, 400, "question_too_long"),
    ("多带字段", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 3, "question": "x", "debug": 1}, 400, "unexpected_field"),
    ("缺字段", {"corpus_id": "rag_learning", "mode": "generate", "top_k": 3}, 400, "missing_field"),
]


def checks_fail_closed() -> None:
    for label, payload, want_status, want_code in BAD_PAYLOADS:
        r = query(payload)
        code = (r["json"].get("error") or {}).get("code")
        check(f"接口·fail closed：{label} → {want_status}/{want_code}",
              r["status"] == want_status and code == want_code,
              f"实际 {r['status']}/{code}")
    status, _, body = http("POST", "/api/query", raw=b"{not json",
                           ctype="application/json")
    check("接口·fail closed：请求体不是合法 JSON → 400/invalid_json",
          status == 400 and jbody(body).get("error", {}).get("code") == "invalid_json",
          f"实际 {status}")
    status, _, body = http("POST", "/api/query", raw=b"a=1", ctype="text/plain")
    check("接口·fail closed：Content-Type 不是 application/json → 400/bad_content_type",
          status == 400 and jbody(body).get("error", {}).get("code") == "bad_content_type",
          f"实际 {status}")
    status, _, body = http("POST", "/api/query", raw=b"", ctype="application/json")
    check("接口·fail closed：空请求体 → 400/empty_body",
          status == 400 and jbody(body).get("error", {}).get("code") == "empty_body",
          f"实际 {status}")
    big = json.dumps({"corpus_id": "rag_learning", "mode": "generate", "top_k": 3,
                      "question": "x" * 70000}).encode("utf-8")
    status, _, body = http("POST", "/api/query", raw=big, ctype="application/json")
    check("接口·fail closed：请求体超过 64 KB → 413/body_too_large",
          status == 413 and jbody(body).get("error", {}).get("code") == "body_too_large",
          f"实际 {status}")


def checks_retrieval_differential() -> None:
    """命中的 id/顺序/分数、同节补充、上下文统计、引用解析：API 必须与直接调 rag_core 一致。"""
    for top_k in (1, 2, 3, 4, 5):
        payload = {"corpus_id": rc.RAG_LEARNING, "mode": "retrieve_only",
                   "top_k": top_k, "question": Q_TRIAD}
        r = query(payload)
        exp, _ = rc_expect(rc.RAG_LEARNING, Q_TRIAD, top_k)
        check(f"接口·对拍 Top-K={top_k}：命中 id/顺序/分数/全部字段与直接调 rag_core 一致",
              r["status"] == 200 and r["json"].get("hits") == items_of(exp["hits"]),
              f"api={[h['id'] for h in r['json'].get('hits', [])]} "
              f"rc={[h['id'] for h in exp['hits']]}")
        check(f"接口·对拍 Top-K={top_k}：request 原样回显、recorded_top_k 等于本次 Top-K",
              r["json"].get("request") == payload and r["json"].get("recorded_top_k") == top_k, "")

    # 仅检索模式：语义上"没有走到生成"，因此不返回生成上下文（同节补充 / 上下文预算都为"不适用"）
    r = query({"corpus_id": rc.RAG_LEARNING, "mode": "retrieve_only", "top_k": 3, "question": Q_TRIAD})
    check("接口·仅检索：notice=retrieve_only、无回答、无同节补充、无上下文预算（如实表达「没经过生成」）",
          r["json"].get("notice") == "retrieve_only" and r["json"].get("answer") is None
          and r["json"].get("neighbors") == [] and r["json"].get("context_chars") is None
          and r["json"].get("refs") == [] and r["json"].get("ref_occurrences") == 0, "")

    # 电力示例库（另一个知识库、另一种切分链路）也要对得上
    payload = {"corpus_id": rc.POWER_DEMO, "mode": "retrieve_only", "top_k": 3, "question": Q_GROUND}
    r = query(payload)
    exp, _ = rc_expect(rc.POWER_DEMO, Q_GROUND, 3)
    check("接口·对拍（电力示例库）：命中与直接调 rag_core 一致（另一条切分链路不串库）",
          r["json"].get("hits") == items_of(exp["hits"]) and len(r["json"].get("hits", [])) == 3,
          f"api={[h['id'] for h in r['json'].get('hits', [])]}")


def checks_zero_hits() -> None:
    before = stub_count()
    r = query({"corpus_id": rc.POWER_DEMO, "mode": "generate", "top_k": 3, "question": Q_ZERO})
    exp, _ = rc_expect(rc.POWER_DEMO, Q_ZERO, 3)
    check("接口·零结果：notice=zero_hits、命中为空、不生成回答（零结果短路）",
          exp["hits"] == [] and r["json"].get("notice") == "zero_hits"
          and r["json"].get("hits") == [] and r["json"].get("answer") is None
          and r["json"].get("neighbors") == [],
          f"rc_hits={len(exp['hits'])} api_notice={r['json'].get('notice')}")
    check("接口·零结果：确实没有向生成层发过请求（桩计数不变）",
          stub_count() == before, f"{before} → {stub_count()}")


def checks_generate_with_stub(stub_log: Path) -> None:
    """生成层成功：答案来自本机桩；引用解析用桩日志里的回答原文独立复算。"""
    for top_k in (1, 3, 5):
        before = stub_count()
        r = query({"corpus_id": rc.RAG_LEARNING, "mode": "generate", "top_k": top_k,
                   "question": Q_TRIAD})
        exp, ctx_map = rc_expect(rc.RAG_LEARNING, Q_TRIAD, top_k)
        entries = stub_entries(stub_log)
        check(f"接口·生成 Top-K={top_k}：恰好发出 1 次生成请求（桩计数 +1）",
              stub_count() == before + 1 and len(entries) >= 1, f"{before} → {stub_count()}")
        last = entries[-1]
        want_ids = [e["id"] for e in exp["contexts"]][:8]
        check(f"接口·生成 Top-K={top_k}：送到桩的 prompt 里正是 rag_core 组装的上下文块（按序，前 8 个）",
              last["ids_in_prompt"] == want_ids and last["auth_present"] is True,
              f"桩看到的={last['ids_in_prompt']} 期望={want_ids}")
        want_refs = rc.extract_refs(last["answer"], ctx_map)
        check(f"接口·生成 Top-K={top_k}：命中/同节补充/上下文统计与 rag_core 一致，回答=桩返回原文",
              r["json"].get("hits") == items_of(exp["hits"])
              and r["json"].get("neighbors") == items_of(exp["neighbors"])
              and r["json"].get("context_chars") == {"total": exp["total_chars"],
                                                    "hit": exp["hit_chars"],
                                                    "max": exp["max_chars"],
                                                    "radius": exp["radius"]}
              and r["json"].get("answer") == last["answer"],
              f"hits={len(r['json'].get('hits', []))} neighbors={len(r['json'].get('neighbors', []))}")
        check(f"接口·生成 Top-K={top_k}：引用按上下文独立复算一致（refs={want_refs}）",
              r["json"].get("refs") == want_refs
              and r["json"].get("ref_occurrences") == rc.count_ref_occurrences(last["answer"]),
              f"api_refs={r['json'].get('refs')} 复算={want_refs}")
        check(f"接口·生成 Top-K={top_k}：回答与数据来源都如实写明来自本机桩、未调用真实模型",
              "本机桩" in (r["json"].get("answer") or "")
              and "实时生成" in (r["json"].get("recorded_from") or "")
              and "未调用真实模型供应商" in (r["json"].get("recorded_from") or ""),
              f"recorded_from={r['json'].get('recorded_from')}")


def checks_generation_failure(stub_log_fail: Path) -> None:
    before = stub_count()
    r = query({"corpus_id": rc.RAG_LEARNING, "mode": "generate", "top_k": 3, "question": Q_TRIAD})
    exp, _ = rc_expect(rc.RAG_LEARNING, Q_TRIAD, 3)
    check("接口·生成失败：状态为 error、notice=generation_failed，且错误说明不为空",
          r["status"] == 200 and r["json"].get("status") == "error"
          and r["json"].get("notice") == "generation_failed"
          and bool(r["json"].get("notice_detail")),
          f"detail={r['json'].get('notice_detail')!r}")
    check("接口·生成失败：真实检索证据完整保留，但没有回答、没有引用（不伪造）",
          r["json"].get("hits") == items_of(exp["hits"]) and r["json"].get("answer") is None
          and r["json"].get("refs") == [] and r["json"].get("ref_occurrences") == 0
          and r["json"].get("neighbors") == items_of(exp["neighbors"]),
          f"hits={len(r['json'].get('hits', []))} neighbors={len(r['json'].get('neighbors', []))}")
    check("接口·生成失败：错误详情里不含 Key，重试失败也不会把凭据写进响应",
          SENTINEL_KEY not in (r["json"].get("notice_detail") or "")
          and SENTINEL_KEY not in r["body"], "")
    check("接口·生成失败：确实是桩返回失败（桩计数 +1，且日志里 status=500）",
          stub_count() == before + 1
          and any(e.get("status") == 500 for e in stub_entries(stub_log_fail)),
          f"{before} → {stub_count()}")


def checks_no_key(meta: dict) -> None:
    check("接口·无 Key：meta.api_key_configured 为布尔 false（不是 null、不是字符串）",
          meta.get("api_key_configured") is False, f"值={meta.get('api_key_configured')!r}")
    before = stub_count()
    r = query({"corpus_id": rc.RAG_LEARNING, "mode": "generate", "top_k": 3, "question": Q_TRIAD})
    exp, _ = rc_expect(rc.RAG_LEARNING, Q_TRIAD, 3)
    check("接口·无 Key：notice=no_api_key，仍返回真实检索证据，但没有回答/引用/生成上下文",
          r["json"].get("notice") == "no_api_key"
          and r["json"].get("hits") == items_of(exp["hits"])
          and r["json"].get("answer") is None and r["json"].get("refs") == []
          and r["json"].get("neighbors") == [] and r["json"].get("context_chars") is None, "")
    check("接口·无 Key：完全没有向生成层发请求（桩计数不变）",
          stub_count() == before, f"{before} → {stub_count()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="api_v3_evidence_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict = {"service": "api_v3", "api_port": API_PORT, "stub_port": STUB_PORT,
                     "sentinel_key_used": True, "phases": []}
    stub_log = out_dir / "llm_stub_requests.jsonl"
    stub_log_fail = out_dir / "llm_stub_requests_fail_mode.jsonl"
    stub_log_phase3 = out_dir / "llm_stub_requests_phase3.jsonl"

    stub = api = None
    try:
        print("--- 阶段 1：桩 ok + api_v3 有 Key ---")
        stub = start_stub("ok", stub_log)
        api = start_api(True, out_dir / "api_v3.log")
        status, _, body = http("GET", "/api/meta")
        meta = jbody(body)
        checks_meta(meta)
        checks_routing()
        checks_fail_closed()
        checks_retrieval_differential()
        checks_zero_hits()
        checks_generate_with_stub(stub_log)
        results["phases"].append({"name": "stub_ok_with_key", "stub_requests": stub_count()})

        print("\n--- 阶段 2：桩改成失败模式（验证「生成失败」路径）---")
        kill(stub)
        stub = start_stub("fail", stub_log_fail)
        checks_generation_failure(stub_log_fail)
        results["phases"].append({"name": "stub_fail", "stub_requests": stub_count()})

        print("\n--- 阶段 3：重启 api_v3 且不带 Key（验证「无 Key」路径）---")
        kill(stub)
        stub = start_stub("ok", stub_log_phase3)
        kill(api)
        api = start_api(False, out_dir / "api_v3_no_key.log")
        _, _, body = http("GET", "/api/meta")
        checks_no_key(jbody(body))
        results["phases"].append({"name": "no_key", "stub_requests": stub_count()})
    except Exception as exc:                                   # noqa: BLE001
        # 中途崩了也要留下现场与结论，不能静默变成"没跑过"
        FAILED.append(f"验收中途异常：{type(exc).__name__}: {exc}")
        print(f"[FAIL] 验收中途异常：{type(exc).__name__}: {exc}", flush=True)
    finally:
        kill(api)
        kill(stub)
        for h in HANDLES:
            try:
                h.close()
            except Exception:                                  # noqa: BLE001
                pass

    check("接口·凭据不泄漏（总检）：本次全部响应原文里都没有出现 Key 内容",
          not any(SENTINEL_KEY in b for b in SEEN_BODIES), f"响应 {len(SEEN_BODIES)} 份")

    # 真实模型请求为 0 的证据链：请求台账（桩日志）+ 生成目标地址，两者都要对得上
    led = [stub_entries(stub_log), stub_entries(stub_log_fail), stub_entries(stub_log_phase3)]
    api_log = (out_dir / "api_v3.log").read_text(encoding="utf-8", errors="replace") \
        if (out_dir / "api_v3.log").exists() else ""
    check("接口·生成目标就是本机桩：api_v3 启动横幅写明 base_url 是桩地址，日志里没有任何公网模型域名",
          f"base_url={STUB_BASE}" in api_log
          and "api.deepseek.com" not in api_log and "bigmodel.cn" not in api_log
          and all(e["path"] == "/v1/chat/completions" for grp in led for e in grp),
          f"横幅命中={f'base_url={STUB_BASE}' in api_log} 公网域名={('api.deepseek.com' in api_log)}")
    check("接口·生成调用次数与台账一致：有 Key 且命中 3 次 + 桩失败 1 次 = 恰好 4 次生成请求，"
          "仅检索/零结果/无 Key 三次尝试一次都没发",
          [len(g) for g in led] == [3, 1, 0],
          f"三个阶段的桩请求数={[len(g) for g in led]}")

    results["passed"] = PASSED
    results["failed"] = FAILED
    results["stub_log"] = [e for e in stub_entries(stub_log)]
    results["stub_log_fail_mode"] = [e for e in stub_entries(stub_log_fail)]
    (out_dir / "verify_api_v3_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "-" * 78)
    print(f"接口层 PASS {len(PASSED)} 项；FAIL {len(FAILED)} 项。证据写入：{out_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
