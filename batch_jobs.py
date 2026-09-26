# -*- coding: utf-8 -*-
"""批量提问 v1 —— batch_jobs.py（47 号方案阶段 A）

这个文件只做一件事：**把"N 道问题按顺序跑完"这件事变成可验证的状态机**。
检索仍走 `rag_core.expand_and_assemble`，生成仍走 `rag_core.generate_answer`；
本文件里没有一行 BM25、切分、同节补全、引用解析或 prompt 逻辑（与 api_v3 同一条红线）。

47 号方案 A2 的三条硬约束（都在这里落实，不靠页面自觉）：
  1. **每题最多 1 次模型请求**：没有重试循环，"零重试"是结构性事实（台账 `retries` 恒为 0）；
  2. **并发固定为 1**：单 worker 线程顺序执行，前一条不收尾不启动下一条；
  3. **系统性错误立即停手**：认证 / 配置 / 网络 / 模型服务错误发生后不再启动后续问题，
     避免把同一个错误重复消费 N 次；非系统错误（例如模型返回体结构异常）只记这一条失败。
  4. **取消在两个检查点生效**（49 号 P1）：每题开始前，以及**检索完成、发出生成请求之前**
     （同一把锁内复核）；后者保证"还在检索时点停止"不会在取消之后补发一次模型请求。
另外：检索模式（retrieve_only）**预计模型请求数恒为 0**，且执行路径里根本没有生成调用。

不做的事（47 号方案第六节）：
  - 不并发、不自动重试、不断点续跑、不定时批处理；
  - 进程重启后旧任务不恢复、不重放（第一版明确不做续跑；内存态是唯一权威）。

安全与隔离：
  - `job_id` 由 `secrets.token_hex` 生成，只用于状态文件名，不拼任何用户输入；
  - 状态快照只写 `.run/batches/`（可用环境变量 `RAG_BATCH_DIR` 覆盖，回归脚本用它隔离测试数据），
    绝不写进内置资料目录；
  - 落盘与返回的错误文本都经过 scrub：不含 Key、不含完整异常栈；
  - 本文件从不读取或回显 Key 内容，只接受"Key 是否存在"这一个布尔事实。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import rag_core as rc                                             # noqa: E402

# ---------------- 硬上限（47 号方案 A1/A2 定值） ----------------
MIN_QUESTIONS = 1
MAX_QUESTIONS = 20
MAX_QUESTION_CHARS = 500                      # 与单题接口 MAX_QUESTION_CHARS 同一口径
TOP_K_MIN, TOP_K_MAX = 1, 5
ALLOWED_MODES = ("retrieve_only", "generate")
# 与 rag_core.build_prompt 里的拒答文案同源；只用于**分类**，不用于生成
REFUSAL_MARK = "资料中未找到相关依据"

BATCH_DIR = Path(os.environ.get("RAG_BATCH_DIR") or (REPO / ".run" / "batches"))

# 任务状态机（47 号方案 A3）
STATE_PENDING = "PENDING"
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_CANCELLED = "CANCELLED"
STATE_STOPPED_ON_ERROR = "STOPPED_ON_ERROR"
FINAL_STATES = (STATE_COMPLETED, STATE_CANCELLED, STATE_STOPPED_ON_ERROR)

# 单题状态（47 号方案 A4：等待 / 运行中 / 完成 / 拒答 / 失败 / 未执行）
ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_REFUSED = "refused"
ITEM_FAILED = "failed"
ITEM_NOT_EXECUTED = "not_executed"

_LOOPBACK_RE = re.compile(r"://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)")

_JOBS: dict = {}
_LOCK = threading.RLock()


class BatchError(Exception):
    """带 HTTP 状态码与机器可读错误码的批量任务失败（api_v3 转成 4xx 响应）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------- 小工具 ----------------
def scrub(text: object, api_key: str = "") -> str:
    """把可能混进异常文本里的凭据抹掉，并截断（防御性：宁可少显示也不回显凭据）。"""
    out = str(text)
    key = (api_key or "").strip()
    if key:
        out = out.replace(key, "***")
    return out[:300]


def is_loopback(base_url: str) -> bool:
    """生成目标是不是本机回环（是的话导出报告要如实写"未调用真实模型供应商"）。"""
    return bool(_LOOPBACK_RE.search(base_url or ""))


def _system_error(exc: BaseException) -> bool:
    """这个异常是否属于"继续跑下去只会白白重复消费"的系统性错误？

    判定口径（保守但可解释）：
      - HTTP 状态 401/403/404/408/409/429 与 5xx：认证/权限/额度/服务端故障；
      - 网络层异常（连接、超时、SSL、代理、远端断开）；
      - 文本里出现明确的认证/网络关键词（兜住 requests 之外的实现）。
    其它异常（例如返回体结构不符合预期）只算这一条失败，不终止整个批次。
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and (status in (401, 403, 404, 408, 409, 429) or status >= 500):
        return True
    name = type(exc).__name__
    if name in {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError",
                "ProxyError", "RemoteDisconnected", "NewConnectionError", "MaxRetryError",
                "ConnectionResetError", "ConnectionAbortedError", "IncompleteRead"}:
        return True
    text = str(exc).lower()
    for kw in ("unauthorized", "invalid api key", "authentication", "permission denied",
               "rate limit", "connection", "timed out", "timeout", "ssl", "proxy",
               "temporarily unavailable", "bad gateway", "service unavailable"):
        if kw in text:
            return True
    return False


# ---------------- 输入规范化 ----------------
def normalize_questions(raw: object) -> list:
    """多行文本 / TXT / CSV 的最终归宿：字符串数组 → 保序、去空行、不做去重。

    47 号方案 A1：重复问题**不静默去重**（用户可能故意观察一致性）。
    """
    if not isinstance(raw, list):
        raise BatchError(400, "invalid_questions", "questions 必须是字符串数组。")
    out: list = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise BatchError(400, "invalid_question", f"第 {i + 1} 道问题不是字符串。")
        text = item.strip()
        if not text:
            continue                                  # 空行直接跳过（不计入 1–20）
        if len(text) > MAX_QUESTION_CHARS:
            raise BatchError(400, "question_too_long",
                             f"第 {i + 1} 道问题超过 {MAX_QUESTION_CHARS} 个字符。")
        out.append(text)
    if len(out) < MIN_QUESTIONS:
        raise BatchError(400, "empty_questions", "至少要有一道非空问题。")
    if len(out) > MAX_QUESTIONS:
        raise BatchError(400, "too_many_questions",
                         f"一次最多 {MAX_QUESTIONS} 道问题（收到 {len(out)} 道）。")
    return out


def plan_calls(mode: str, count: int) -> int:
    """预计模型请求数：仅检索固定 0；生成每题最多 1 次。"""
    return count if mode == "generate" else 0


def _corpus_snapshot(corpus_id: str) -> dict:
    """建任务时快照知识库统计（来源数/片段数/字符数/版本/指纹），只给导出报告用。

    放在建任务时取，是为了让"导出"这一步彻底变成纯读取：导出时不重算、不检索、不调用模型。
    """
    try:
        st = rc.corpus_stats(corpus_id)
    except Exception as exc:                                      # noqa: BLE001
        return {"error": scrub(f"{type(exc).__name__}: {exc}")}
    return {
        "source_count": st.get("source_count"),
        "chunk_count": st.get("chunk_count"),
        "total_chars": st.get("total_chars"),
        "version": st.get("version", ""),
        "fingerprint": st.get("fingerprint", ""),
    }


# ---------------- 落盘（原子写，只在 .run/batches/） ----------------
def _persist(job: dict) -> None:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    target = BATCH_DIR / f"{job['job_id']}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


# ---------------- 创建 ----------------
def create_job(*, corpus_id: str, mode: str, top_k: int, questions: object,
               confirm_calls: object, llm_base_url: str, llm_model: str,
               api_key: str = "") -> dict:
    """校验 → 建任务 → 立刻返回（执行在后台单线程里）。

    `confirm_calls` 是页面的二次确认值：47 号方案 A2 要求"确认值必须与服务端
    重新计算的 N 完全一致"，不一致直接拒绝，不靠页面自觉。
    """
    qs = normalize_questions(questions)

    if corpus_id not in rc.corpus_ids():
        raise BatchError(400, "unknown_corpus", f"未知知识库：{corpus_id}")
    if mode not in ALLOWED_MODES:
        raise BatchError(400, "unknown_mode", f"模式只允许：{', '.join(ALLOWED_MODES)}")
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise BatchError(400, "invalid_top_k", "top_k 必须是整数。")
    if not (TOP_K_MIN <= top_k <= TOP_K_MAX):
        raise BatchError(400, "top_k_out_of_range",
                         f"top_k 必须在 {TOP_K_MIN}–{TOP_K_MAX} 之间。")

    planned = plan_calls(mode, len(qs))
    if mode == "generate":
        if not (api_key or "").strip():
            # Key 缺失：在任何请求发出前整体拒绝（47 号方案 A2）
            raise BatchError(409, "no_api_key",
                             "生成模式需要服务端配置 DEEPSEEK_API_KEY；当前可以改用「仅检索」。")
        if confirm_calls != planned:
            raise BatchError(400, "confirm_mismatch",
                             f"预计最多调用 {planned} 次模型，确认值与服务端计算不一致"
                             f"（收到 {confirm_calls!r}）。")
    else:
        if confirm_calls not in (None, 0):
            raise BatchError(400, "confirm_mismatch",
                             "仅检索模式不会调用模型，confirm_calls 必须为 0 或不传。")

    job_id = secrets.token_hex(12)
    job = {
        "job_id": job_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": None,
        "corpus_id": corpus_id,
        "corpus_name": rc.corpus_name(corpus_id),
        # 建任务时快照一次库统计：导出报告直接读快照，不在导出时重算（47 号 §4）
        "corpus_snapshot": _corpus_snapshot(corpus_id),
        "mode": mode,
        "top_k": top_k,
        "state": STATE_PENDING,
        "total": len(qs),
        "planned_calls": planned,
        "started_calls": 0,        # 真实发出（或即将发出）的生成请求数
        "succeeded": 0,
        "failed": 0,
        "refused": 0,
        "retries": 0,              # 结构性事实：本文件没有重试循环
        "cancel_requested": False,
        "cancelled_at_seq": None,  # 取消发生在"第几条之前"
        "stop_reason": "",         # 系统性错误时写系统错误的分类摘要
        "llm_model": llm_model,
        "llm_loopback": is_loopback(llm_base_url),
        "items": [
            {
                "sequence": i + 1,
                "question": q,
                "status": ITEM_PENDING,
                "answer": None,
                "refs": [],            # [{id, source_title, section, text, origin}]
                "ref_occurrences": 0,
                "hits": [],            # [{id, source_title, section, score, text}]
                "neighbors_count": 0,
                "notice": None,        # zero_hits / retrieve_only / refused / 生成失败
                "error": "",
                "elapsed_ms": {"retrieve": None, "generate": None},
            }
            for i, q in enumerate(qs)
        ],
    }

    with _LOCK:
        busy = [j["job_id"] for j in _JOBS.values()
                if j["mode"] == "generate" and j["state"] in (STATE_PENDING, STATE_RUNNING)]
        if mode == "generate" and busy:
            raise BatchError(409, "generate_busy",
                             "已有一个生成批量任务在运行；同一进程只允许一个（请先等待或取消它）。")
        _JOBS[job_id] = job
        _persist(job)

    threading.Thread(target=_run_job, args=(job_id, llm_base_url, llm_model, api_key),
                     name=f"batch-{job_id}", daemon=True).start()
    return public_job(job)


# ---------------- 读取 / 取消 ----------------
def get_job(job_id: str) -> dict:
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        # 内存里没有 = 不存在或服务已重启（第一版不跨重启续跑，也绝不重放）
        raise BatchError(404, "unknown_job", f"没有这个批量任务：{job_id}")
    return public_job(job)


def request_cancel(job_id: str) -> dict:
    """请求取消：当前这条**已发出的请求等它结束**（并计入实际调用数），之后不再启动下一条。

    两个检查点（49 号 P1）：
      - 每题开始前：还没开始检索 → 这条直接记为未执行；
      - 检索完成、发出生成请求前的锁内复核：还在检索时点取消 → 这条**不会**发出模型请求。
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise BatchError(404, "unknown_job", f"没有这个批量任务：{job_id}")
        if job["state"] in FINAL_STATES:
            return public_job(job)
        job["cancel_requested"] = True
        _persist(job)
    return public_job(job)


def export_data(job_id: str) -> dict:
    """导出用快照：**只读本次实际记录**，不重新检索、不重新生成、不新增模型调用。"""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise BatchError(404, "unknown_job", f"没有这个批量任务：{job_id}")
        return json.loads(json.dumps(job, ensure_ascii=False))


def reset() -> None:
    """清空进程内任务表（**只给回归脚本用**：让每个用例互不干扰）。"""
    with _LOCK:
        _JOBS.clear()


def active_generate_jobs() -> list:
    with _LOCK:
        return [j["job_id"] for j in _JOBS.values()
                if j["mode"] == "generate" and j["state"] in (STATE_PENDING, STATE_RUNNING)]


# ---------------- 对外结构 ----------------
def public_job(job: dict) -> dict:
    """给页面/导出用的结构（深拷贝，调用方改不动内部状态）。"""
    with _LOCK:
        return json.loads(json.dumps(job, ensure_ascii=False))


# ---------------- 执行（单 worker，串行） ----------------
def _run_job(job_id: str, llm_base_url: str, llm_model: str, api_key: str) -> None:
    job = _JOBS[job_id]
    with _LOCK:
        job["state"] = STATE_RUNNING
        _persist(job)

    ctx_map: dict = {}
    for item in job["items"]:
        with _LOCK:
            if job["cancel_requested"]:
                job["cancelled_at_seq"] = item["sequence"]
                break
            item["status"] = ITEM_RUNNING
            _persist(job)

        q = item["question"]
        # ---- 检索（两种模式都要做）----
        t0 = time.perf_counter()
        try:
            assembled = rc.expand_and_assemble(q, top_k=job["top_k"], corpus_id=job["corpus_id"])
        except Exception as exc:                                  # noqa: BLE001
            with _LOCK:
                item["status"] = ITEM_FAILED
                item["error"] = scrub(f"{type(exc).__name__}: {exc}", api_key)
                item["elapsed_ms"]["retrieve"] = _ms(t0)
                job["failed"] += 1
                _persist(job)
            continue

        hits = assembled["hits"]
        ctx_map = {e["id"]: e for e in assembled["contexts"]}
        with _LOCK:
            item["elapsed_ms"]["retrieve"] = _ms(t0)
            # 命中快照连同**原文**一起落盘（50 号工单第一节）：报告要能自证"命中到底命中
            # 了什么"，只留 chunk id 等于把可读证据丢在检索返回里。这里顺手存下来，
            # 导出侧就永远只需读这份快照 —— 不许为了补报告重新检索、重建索引或调用模型。
            item["hits"] = [{"id": e["id"], "source_title": e["source_title"],
                             "section": e["section"], "score": e["score"],
                             "text": e["text"]} for e in hits]
            item["neighbors_count"] = len(assembled["neighbors"])

        # ---- 仅检索：到此结束（预计模型请求恒为 0，这里没有任何生成调用）----
        if job["mode"] == "retrieve_only":
            with _LOCK:
                item["status"] = ITEM_DONE
                item["notice"] = "retrieve_only" if hits else "zero_hits"
                job["succeeded"] += 1
                _persist(job)
            continue

        # ---- 生成：零命中短路（不调用模型），否则每题恰好 1 次请求 ----
        if not hits:
            with _LOCK:
                item["status"] = ITEM_DONE
                item["notice"] = "zero_hits"
                job["succeeded"] += 1
                _persist(job)
            continue

        with _LOCK:
            # 49 号 P1：检索完成、零命中短路之后，在**同一把锁内**再复核一次取消。
            # 为什么必须在这里再查一次：用户在"当前题正在检索"的窗口里点停止时，模型请求
            # 还没有发出；若只信循环开头那次检查，这一条会在检索结束后照旧发出请求，
            # 等于"取消之后仍然新增了一次真实调用"，突破 47 号 A2 的成本与授权边界。
            # 放在锁内是为了让"复核 + 记数"成为原子动作：取消请求要么在记数之前生效
            # （这一条不发请求），要么在记数之后到达（按"已发出的请求等它结束"处理）。
            if job["cancel_requested"]:
                job["cancelled_at_seq"] = item["sequence"]
                break
            job["started_calls"] += 1        # "已经发出（或即将发出）"都要计入实际调用数
            _persist(job)
        t1 = time.perf_counter()
        try:
            answer = rc.generate_answer(q, assembled["contexts"], api_key,
                                        llm_base_url, llm_model, job["corpus_id"])
        except Exception as exc:                                  # noqa: BLE001
            system = _system_error(exc)
            with _LOCK:
                item["status"] = ITEM_FAILED
                item["error"] = scrub(f"{type(exc).__name__}: {exc}", api_key)
                item["elapsed_ms"]["generate"] = _ms(t1)
                item["notice"] = "generation_failed"
                job["failed"] += 1
                if system:
                    # 47 号方案 A2：系统性错误 → 不再启动后续问题
                    job["state"] = STATE_STOPPED_ON_ERROR
                    job["stop_reason"] = (f"第 {item['sequence']} 条发生系统性错误"
                                          f"（{type(exc).__name__}），已停止启动后续问题。")
                _persist(job)
            if system:
                break
            continue

        refs = rc.extract_refs(answer, ctx_map)
        with _LOCK:
            item["answer"] = answer
            item["elapsed_ms"]["generate"] = _ms(t1)
            item["refs"] = [{"id": rid,
                             "source_title": ctx_map[rid]["source_title"],
                             "section": ctx_map[rid]["section"],
                             "text": ctx_map[rid]["text"],
                             "origin": ctx_map[rid].get("origin", "hit")} for rid in refs]
            item["ref_occurrences"] = rc.count_ref_occurrences(answer)
            if REFUSAL_MARK in answer:
                item["status"] = ITEM_REFUSED
                item["notice"] = "refused"
                job["refused"] += 1
            else:
                item["status"] = ITEM_DONE
                item["notice"] = None
            job["succeeded"] += 1
            _persist(job)

    # ---- 收尾：未执行条目如实标记；终态一步到位 ----
    with _LOCK:
        for item in job["items"]:
            if item["status"] in (ITEM_PENDING, ITEM_RUNNING):
                item["status"] = ITEM_NOT_EXECUTED
                item["notice"] = item["notice"] or "not_executed"
        if job["state"] != STATE_STOPPED_ON_ERROR:
            job["state"] = STATE_CANCELLED if job["cancel_requested"] else STATE_COMPLETED
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _persist(job)


def _ms(t0: float) -> int:
    return int(round((time.perf_counter() - t0) * 1000))


# ---------------- 供导出/页面复用的汇总 ----------------
def summary(job: dict) -> dict:
    """按状态计数（导出报告第 6 项与页面进度条共用一份口径）。"""
    counts = {"done": 0, "refused": 0, "failed": 0, "not_executed": 0,
              "pending": 0, "running": 0}
    for item in job["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    finished = counts["done"] + counts["refused"] + counts["failed"]
    return {
        "total": job["total"],
        "finished": finished,
        "done": counts["done"],
        "refused": counts["refused"],
        "failed": counts["failed"],
        "not_executed": counts["not_executed"],
        "pending": counts["pending"],
        "running": counts["running"],
        "planned_calls": job["planned_calls"],
        "started_calls": job["started_calls"],
        "succeeded": job["succeeded"],
        "retries": job["retries"],
    }
