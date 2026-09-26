# -*- coding: utf-8 -*-
"""第三版独立前端的**本机薄接口**（第二阶段 A）—— api_v3.py

它只做三件事：
  1. 校验请求参数（白名单 / 枚举 / 长度 / 未知字段 fail closed）；
  2. 调用 `rag_core.py` 的既有函数；
  3. 把结果转成 `frontend_v3/src/types/index.ts` 约定的 JSON 形状。

**刻意不做**：不复制 BM25、不复制同节相邻补全、不复制引用解析、不复制 prompt、
不复制切分逻辑；本文件里没有一行检索或生成算法。

运行（本机演示 / 联调）：
    python api_v3.py                     # 默认 127.0.0.1:5280
    python api_v3.py --port 5280

接口（检索两个 + 资料库四个 + 批量四个，够用就好）：
    GET  /api/meta    → 知识库真实统计、Top-K 选项、服务端 Key 的**布尔**状态、我的资料库摘要、批量能力上限
    POST /api/query   → {corpus_id, mode, top_k, question} → 真实检索（+可选生成）
    GET    /api/user-documents          → 我的资料库：来源清单与统计
    POST   /api/user-documents          → {"items":[...]} 导入 .txt/.md/粘贴文本（去重、限额）
    POST   /api/user-documents/reindex  → 用 rag_core 现有切分重建索引快照
    DELETE /api/user-documents/{id}     → {"confirm": true} 删除并重建（无 confirm 一律拒绝）
    POST   /api/batch-jobs              → 创建批量任务（1–20 条；生成模式必须二次确认调用数）
    GET    /api/batch-jobs/{id}         → 进度与逐条结果
    POST   /api/batch-jobs/{id}/cancel  → 请求取消（当前这条跑完就停，之后不再启动）
    GET    /api/batch-jobs/{id}/export  → 导出 DOCX / CSV（只读已保存结果，零模型请求）

批量的所有执行语义都在 `batch_jobs.py`（状态机、1–20 硬上限、串行、零重试、系统性错误停手），
导出格式在 `batch_export.py`：**本文件仍然只是薄入口**，不复制任何批量循环或报表逻辑。

安全与边界（本文件是"本机薄接口"，不是生产服务）：
  - 只绑定 127.0.0.1，不做 TLS、不做鉴权、不监听外网；不要部署到公网；
  - CORS 只允许本机前端来源（默认 5273 端口的 preview）；带其它 Origin 的请求直接 403；
  - API Key 只留在服务端进程：/api/meta 只返回 true/false，绝不返回值、前缀或长度；
  - **默认不发任何模型请求**：只有 mode=generate 且服务端检测到 Key 时才会走到生成层；
    生成层的 base_url / model 默认取 config.py，可用 --llm-base-url 指向本机桩做联调
    （命令行里显式可见，且启动横幅会打印实际使用的目标地址）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import config                                                 # noqa: E402
import rag_core as rc                                         # noqa: E402
import user_library as ul                                     # noqa: E402  （import 即注册第三个知识库）
import batch_jobs as bj                                       # noqa: E402  （47 号方案阶段 A）
import batch_export as bx                                     # noqa: E402  （47 号方案阶段 B）

# ---------------- 边界常量（全部在这里，别散落到处理函数里） ----------------
SCHEMA_VERSION = 2
HOST = "127.0.0.1"                  # 固定本机回环，不提供 --host 开关
DEFAULT_PORT = 5280
TOP_K_CHOICES = (1, 2, 3, 4, 5)     # 页面上允许选的 Top-K，服务端独立再校验一次
TOP_K_MIN, TOP_K_MAX = min(TOP_K_CHOICES), max(TOP_K_CHOICES)
MAX_QUESTION_CHARS = 500            # 问题长度上限（非空 + 上限，超了直接拒）
MAX_BODY_BYTES = 64 * 1024          # 请求体上限，防止本机端口被灌大包
ALLOWED_CORPORA = tuple(rc.corpus_ids())
ALLOWED_MODES = ("retrieve_only", "generate")
DEFAULT_CORPUS = rc.RAG_LEARNING
# 只有这三个字段允许出现在请求体里，多一个就拒（fail closed）
ALLOWED_QUERY_FIELDS = frozenset({"corpus_id", "mode", "top_k", "question"})
# ---------------- 我的资料库（40 号方案 v1）----------------
MAX_USERDOC_BODY_BYTES = 5 * 1024 * 1024    # 上传走 base64（约 +33%），5 MB 足够 2 MB 文件
# 每条导入 item 允许的字段；kind 与必带字段由 user_library 校验
ALLOWED_USERDOC_ITEM_FIELDS = frozenset(
    {"kind", "text", "title", "source_name", "source_url", "original_filename", "data_b64"})
# CORS：只放行本机前端（vite preview / dev 都跑在 5273，见 vite.config.ts）
ALLOWED_ORIGINS = (
    "http://127.0.0.1:5273",
    "http://localhost:5273",
)
# ---------------- 批量提问（47 号方案阶段 A/B） ----------------
# 创建任务的请求体只允许这五个字段，多一个就拒（fail closed，与单题同一风格）
ALLOWED_BATCH_FIELDS = frozenset({"corpus_id", "mode", "top_k", "questions", "confirm_calls"})
BATCH_EXPORT_FORMATS = ("csv", "docx")

# ---------------- 页面文案（与 mock 阶段保持同一套说法，避免同一件事两种口径） ----------------
API_NOTE = ("本机 API（api_v3）第二阶段 A：知识库统计与检索结果由 rag_core.py **现算**，"
            "不读取第一阶段的前端快照。")
UNVERIFIED_NOTE = ("生成层尚未完成正式验证：离线验收里「无依据不伪造」（S4）是**尚未验证"
                   "（NOT VALIDATED）**，不是已执行且失败；目前只有单轮 8 例用户侧样本。")


class ApiError(Exception):
    """带 HTTP 状态码与错误码的校验失败。错误正文里不允许出现 Key。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------- Key 状态：只暴露布尔 ----------------
def api_key_configured() -> bool:
    """服务端是否配置了 Key。**只返回布尔**，不返回值、前缀、长度或来源。"""
    return bool((config.DEEPSEEK_API_KEY or "").strip())


def _scrub(text: str) -> str:
    """把可能混进异常文本里的 Key 抹掉（防御性：宁可少显示，也不回显凭据）。"""
    key = (config.DEEPSEEK_API_KEY or "").strip()
    out = str(text)
    if key:
        out = out.replace(key, "***")
    return out[:300]


# ---------------- 调用 rag_core：纯搬运，不改数值 ----------------
def _item(entry: dict) -> dict:
    """把 rag_core 的片段结构转成接口字段（逐字段搬运，不重算、不四舍五入）。"""
    return {
        "id": entry["id"],
        "origin": entry["origin"],            # hit / neighbor
        "rank": entry["rank"],                # 命中名次；补充片段记录其所属命中的名次
        "score": entry["score"],              # BM25 排序分；补充片段为 null
        "source_id": entry["source_id"],
        "source_title": entry["source_title"],
        "source_org": entry["source_org"],
        "source_url": entry["source_url"],
        "section": entry["section"],
        "text": entry["text"],
    }


def _corpus_meta(corpus_id: str) -> dict:
    """知识库统计：数字全部来自 rag_core.corpus_stats()，这里只补展示用说明。

    说明文案与 `tools/build_frontend_mocks.py` 保持一致（第一阶段的页面已经按这套说法验收过）；
    两边若漂移，`verify_api_v3.py` 会用快照逐字段比对抓出来。
    """
    st = rc.corpus_stats(corpus_id)
    corpus = rc.get_corpus(corpus_id)
    if corpus["kind"] == "section":
        chunking = ("按 Markdown 标题分节 → 按空行分段（相邻列表合并）→ 超过上限再按字数切；"
                    "chunk id = {来源}#s{节}p{段}")
        license_note = "Datawhale《All-in-RAG》，许可 CC BY-NC-SA 4.0（署名 · 非商业 · 相同方式共享）"
    else:
        chunking = "按空行分段 → 超过上限再按字数切；chunk id = {文件名}#P{段}（无标题层级）"
        license_note = ""
    return {
        "corpus_id": st["corpus_id"],
        "name": st["name"],
        "note": st["note"],
        "source_count": st["source_count"],
        "chunk_count": st["chunk_count"],
        "total_chars": st["total_chars"],
        "version": st["version"],
        "tokenizer": st["tokenizer"],
        "params": st["params"],
        "chunking_note": chunking,
        "license_note": license_note,
        "is_sample_only": corpus_id == rc.POWER_DEMO,
        "warning": ("示例资料，不是正式规程，不可用于现场作业或安全决策。"
                    if corpus_id == rc.POWER_DEMO else ""),
    }


def build_meta() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "data_source": "api",
        # 字段名沿用了第一阶段的结构（前端只认这一套形状）；api 模式下它就是"数据来源说明"
        "mock_note": API_NOTE,
        "generation_verified": False,
        "unverified_note": UNVERIFIED_NOTE,
        "api_key_configured": api_key_configured(),
        "top_k_choices": list(TOP_K_CHOICES),
        "corpora": [_corpus_meta(DEFAULT_CORPUS), _corpus_meta(rc.POWER_DEMO)],
        "default_corpus": DEFAULT_CORPUS,
        # 我的资料库（40 号方案）：独立字段，不进 corpora——
        # mock 快照比对与内置库统计口径保持原样，用户库没有评测指标
        "user_library": ul.meta_summary(),
        # 批量提问能力（47 号方案阶段 A）：上限与"结构性事实"由服务端给出，页面照原样显示。
        # 并发固定 1、零自动重试是 batch_jobs 里的结构，不是页面上的可调参数。
        "batch": {
            "min_questions": bj.MIN_QUESTIONS,
            "max_questions": bj.MAX_QUESTIONS,
            "max_question_chars": bj.MAX_QUESTION_CHARS,
            "top_k_range": [bj.TOP_K_MIN, bj.TOP_K_MAX],
            "modes": list(bj.ALLOWED_MODES),
            "concurrency": 1,
            "auto_retry": 0,
            "export_formats": list(BATCH_EXPORT_FORMATS),
            "state_note": ("批量任务只存在本机进程内：重启后不恢复、不重放；状态快照写在 "
                           ".run/batches/ 下，不写进任何资料目录。"),
        },
    }


# ---------------- 请求校验 ----------------
def validate_query(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_payload", "请求体必须是 JSON 对象。")
    unknown = sorted(set(payload) - ALLOWED_QUERY_FIELDS)
    if unknown:
        raise ApiError(400, "unexpected_field", f"不认识的字段：{', '.join(unknown)}")
    missing = sorted(ALLOWED_QUERY_FIELDS - set(payload))
    if missing:
        raise ApiError(400, "missing_field", f"缺少字段：{', '.join(missing)}")

    corpus_id = payload["corpus_id"]
    if not isinstance(corpus_id, str) or corpus_id not in ALLOWED_CORPORA:
        raise ApiError(400, "unknown_corpus",
                       f"知识库只允许：{', '.join(ALLOWED_CORPORA)}")
    # 空库 fail closed：用户库一条资料都没有时不允许选它提问
    if corpus_id == ul.USER_LIBRARY and not ul.available():
        raise ApiError(409, "user_library_empty",
                       "「我的资料库」还没有资料，请先在页面「资料管理」里导入。")

    mode = payload["mode"]
    if not isinstance(mode, str) or mode not in ALLOWED_MODES:
        raise ApiError(400, "unknown_mode", f"模式只允许：{', '.join(ALLOWED_MODES)}")

    top_k = payload["top_k"]
    # bool 是 int 的子类，必须显式排除；浮点/字符串也一律拒绝（不做隐式转换）
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ApiError(400, "invalid_top_k", "top_k 必须是整数。")
    if not (TOP_K_MIN <= top_k <= TOP_K_MAX):
        raise ApiError(400, "top_k_out_of_range",
                       f"top_k 必须在 {TOP_K_MIN}–{TOP_K_MAX} 之间。")

    question = payload["question"]
    if not isinstance(question, str) or not question.strip():
        raise ApiError(400, "question_required", "question 必须是非空字符串。")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(400, "question_too_long",
                       f"question 不能超过 {MAX_QUESTION_CHARS} 个字符。")
    return {"corpus_id": corpus_id, "mode": mode, "top_k": top_k, "question": question}


# ---------------- 查询：检索（+ 可选生成） ----------------
def _loopback(base_url: str) -> bool:
    """生成目标是本机回环吗？是的话页面要如实说明"没调用真实模型供应商"。"""
    return bool(re.search(r"://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", base_url or ""))


def run_query(payload: dict, *, llm_base_url: str, llm_model: str) -> dict:
    corpus_id, mode, top_k, question = (payload["corpus_id"], payload["mode"],
                                        payload["top_k"], payload["question"])

    t0 = time.perf_counter()
    assembled = rc.expand_and_assemble(question, top_k=top_k, corpus_id=corpus_id)
    retrieve_ms = int(round((time.perf_counter() - t0) * 1000))

    hits = [_item(e) for e in assembled["hits"]]
    context_map = {e["id"]: e for e in assembled["contexts"]}
    neighbor_all = [_item(e) for e in assembled["neighbors"]]

    zero_hits = not hits
    key_ok = api_key_configured()
    # 是否真的走到生成调用：零结果短路 / 仅检索模式 / 无 Key 三种情况都不会调用生成层
    will_generate = (not zero_hits) and mode == "generate" and key_ok

    status = "done"
    notice: str | None = None
    notice_detail = ""
    answer: str | None = None
    refs: list = []
    ref_occurrences = 0
    generate_ms: int | None = None
    # recorded_from 只由服务端给：页面照原样显示，不自己编造数据来源
    provenance = "本机 API 实时检索（rag_core 现算，未读取前端快照）"

    if zero_hits:
        notice = "zero_hits"           # 零结果短路：不调用生成层
    elif not will_generate and mode == "retrieve_only":
        notice = "retrieve_only"
    elif not will_generate:
        notice = "no_api_key"
    else:
        t1 = time.perf_counter()
        try:
            answer = rc.generate_answer(question, assembled["contexts"],
                                        config.DEEPSEEK_API_KEY.strip(),
                                        llm_base_url, llm_model, corpus_id)
        except Exception as exc:                                   # noqa: BLE001
            status = "error"
            notice = "generation_failed"
            notice_detail = f"生成层调用失败：{_scrub(f'{type(exc).__name__}: {exc}')}"
            answer = None
        generate_ms = int(round((time.perf_counter() - t1) * 1000))
        if answer is not None:
            refs = rc.extract_refs(answer, context_map)
            ref_occurrences = rc.count_ref_occurrences(answer)
            provenance = (f"本机 API 实时生成（model={llm_model}，base_url={llm_base_url}）"
                          + ("；本机回环地址，未调用真实模型供应商" if _loopback(llm_base_url) else ""))

    # 同节补充与上下文预算只在**真的走到生成调用**时才回给页面：
    # 没生成就没有"生成上下文"，页面也不该出现"同节补充"（与第一阶段口径一致）；
    # 生成调用失败时仍返回：那一次上下文确实组装好并送出去了，失败不该吞掉已找到的资料
    neighbors = neighbor_all if will_generate else []
    context_chars = None
    if will_generate:
        context_chars = {
            "total": assembled["total_chars"],
            "hit": assembled["hit_chars"],
            "max": assembled["max_chars"],
            "radius": assembled["radius"],
        }

    return {
        # 结果归属：本次结果用的那一次请求参数，页面不得用"当前控件值"覆盖它
        "request": {"corpus_id": corpus_id, "mode": mode, "top_k": top_k, "question": question},
        "recorded_top_k": top_k,
        "status": status,
        "notice": notice,
        "notice_detail": notice_detail,
        "elapsed_ms": {"retrieve": retrieve_ms, "generate": generate_ms},
        "hits": hits,
        "neighbors": neighbors,
        "context_chars": context_chars,
        "answer": answer,
        "refs": refs,
        "ref_occurrences": ref_occurrences,
        "recorded_from": provenance,
    }


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "rag-api-v3"
    protocol_version = "HTTP/1.1"

    # 供 main() 注入
    llm_base_url = config.DEEPSEEK_BASE_URL
    llm_model = config.DEEPSEEK_MODEL

    def log_message(self, fmt: str, *args: Any) -> None:        # noqa: A003
        sys.stderr.write("[api_v3] %s - %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- 响应工具 ----
    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin else None

    def _cors_headers(self, origin: str | None) -> dict:
        if origin and origin in ALLOWED_ORIGINS:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Vary": "Origin",
            }
        return {}

    def _send_json(self, code: int, obj: dict, origin: str | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self._cors_headers(origin).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, data: bytes, *, content_type: str, filename: str,
                    origin: str | None = None) -> None:
        """二进制下载（批量报告导出）。

        一次性发完整字节：报表在内存里生成完毕才走到这里，生成失败时根本不会产生文件，
        因此不存在 47 号方案担心的"半截文件 / 需要清理的临时文件"。
        文件名同时给 ASCII fallback 与 RFC 5987 的 filename*，中文名在现代浏览器里正确落盘。
        """
        ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "report"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{ascii_fallback}"; '
                         f"filename*=UTF-8''{urllib.parse.quote(filename)}")
        for k, v in self._cors_headers(origin).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _guard_origin(self) -> str | None:
        """带 Origin 的请求必须在白名单里；不在就 403（fail closed）。"""
        origin = self._origin()
        if origin and origin not in ALLOWED_ORIGINS:
            self._send_json(403, {"error": {"code": "origin_not_allowed",
                                            "message": "只允许本机前端来源访问。"}})
            return None
        return origin or ""

    def _read_json_body(self, *, max_bytes: int = MAX_BODY_BYTES) -> Any:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(400, "bad_content_type", "Content-Type 必须是 application/json。")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_content_length", "Content-Length 不合法。")
        if length <= 0:
            raise ApiError(400, "empty_body", "请求体不能为空。")
        if length > max_bytes:
            raise ApiError(413, "body_too_large", f"请求体不能超过 {max_bytes} 字节。")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON。")

    # ---------------- 我的资料库：四个端点的处理 ----------------
    def _handle_userdocs(self, method: str, doc_id: str | None, origin: str) -> None:
        """GET 列表 / POST 导入 / POST reindex / DELETE 删除（必须 confirm）。"""
        if method == "GET":
            if doc_id is not None:
                raise ApiError(404, "not_found", "列表端点不接受 id。")
            self._send_json(200, ul.meta_summary(), origin)
            return
        if method == "POST":
            if doc_id == "reindex":
                self._send_json(200, ul.reindex(), origin)
                return
            if doc_id is not None:
                raise ApiError(404, "not_found", f"未知路径：/api/user-documents/{doc_id}")
            payload = self._read_json_body(max_bytes=MAX_USERDOC_BODY_BYTES)
            if not isinstance(payload, dict) or sorted(payload) != ["items"]:
                raise ApiError(400, "invalid_payload", '请求体必须是 {"items": [...]}。')
            items = payload["items"]
            if not isinstance(items, list) or not items:
                raise ApiError(400, "empty_batch", "items 必须是非空列表。")
            for it in items:
                if not isinstance(it, dict):
                    raise ApiError(400, "invalid_item", "每条资料必须是对象。")
                unknown = sorted(set(it) - ALLOWED_USERDOC_ITEM_FIELDS)
                if unknown:
                    raise ApiError(400, "unexpected_field",
                                   f"不认识的字段：{', '.join(unknown)}")
            try:
                self._send_json(200, ul.import_documents(items), origin)
            except ul.LibraryError as exc:
                self._send_json(400, {"error": {"code": exc.code, "message": exc.message}}, origin)
            return
        if method == "DELETE":
            if not doc_id:
                raise ApiError(400, "invalid_id", "缺少要删除的资料 id。")
            payload = self._read_json_body(max_bytes=1024)
            if not isinstance(payload, dict) or payload.get("confirm") is not True:
                # 删除是破坏性操作：必须显式 confirm=true（页面层另有二次确认弹窗）
                raise ApiError(400, "confirm_required",
                               "删除必须携带 {\"confirm\": true}（页面会先弹窗二次确认）。")
            try:
                self._send_json(200, ul.delete_document(doc_id), origin)
            except ul.LibraryError as exc:
                status = 404 if exc.code == "unknown_document" else 400
                self._send_json(status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            return
        raise ApiError(405, "method_not_allowed", "不支持的请求方法。")

    # ---------------- 批量提问：四个端点的处理（执行语义全在 batch_jobs.py） ----------------
    def _create_batch(self, origin: str) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_payload", "请求体必须是 JSON 对象。")
        unknown = sorted(set(payload) - ALLOWED_BATCH_FIELDS)
        if unknown:
            raise ApiError(400, "unexpected_field", f"不认识的字段：{', '.join(unknown)}")
        missing = sorted({"corpus_id", "mode", "top_k", "questions"} - set(payload))
        if missing:
            raise ApiError(400, "missing_field", f"缺少字段：{', '.join(missing)}")
        # 空库 fail closed：与单题接口同一口径（其余参数校验由 batch_jobs 承担，避免两处各写一套）
        corpus_id = payload["corpus_id"]
        if isinstance(corpus_id, str) and corpus_id == ul.USER_LIBRARY and not ul.available():
            raise ApiError(409, "user_library_empty",
                           "「我的资料库」还没有资料，请先在页面「资料管理」里导入。")
        try:
            job = bj.create_job(corpus_id=corpus_id, mode=payload["mode"], top_k=payload["top_k"],
                                questions=payload["questions"],
                                confirm_calls=payload.get("confirm_calls"),
                                llm_base_url=self.llm_base_url, llm_model=self.llm_model,
                                api_key=config.DEEPSEEK_API_KEY or "")
        except bj.BatchError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            return
        self._send_json(200, {"job": job, "summary": bj.summary(job)}, origin)

    def _batch_status(self, job_id: str, origin: str, *, cancel: bool = False) -> None:
        try:
            job = bj.request_cancel(job_id) if cancel else bj.get_job(job_id)
        except bj.BatchError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            return
        self._send_json(200, {"job": job, "summary": bj.summary(job)}, origin)

    def _export_batch(self, job_id: str, fmt: str, origin: str) -> None:
        if fmt not in BATCH_EXPORT_FORMATS:
            raise ApiError(400, "unknown_format",
                           f"导出格式只允许：{', '.join(BATCH_EXPORT_FORMATS)}")
        try:
            job = bj.export_data(job_id)
        except bj.BatchError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            return
        try:
            out = bx.build_export(job, fmt)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            raise ApiError(500, "export_failed", _scrub(f"{type(exc).__name__}: {exc}"))
        self._send_bytes(200, out["data"], content_type=out["content_type"],
                         filename=out["filename"], origin=origin)

    # ---- 路由 ----
    def do_OPTIONS(self) -> None:                               # noqa: N802
        origin = self._guard_origin()
        if origin is None:
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        for k, v in self._cors_headers(origin).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:                                   # noqa: N802
        origin = self._guard_origin()
        if origin is None:
            return
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(raw_query)
        try:
            if path == "/api/meta":
                self._send_json(200, build_meta(), origin)
            elif path == "/api/health":
                self._send_json(200, {"ok": True, "data_source": "api"}, origin)
            elif path == "/api/user-documents":
                self._handle_userdocs("GET", None, origin)
            elif path.startswith("/api/user-documents/"):
                doc_id = urllib.parse.unquote(path[len("/api/user-documents/"):])
                self._handle_userdocs("GET", doc_id, origin)
            elif path == "/api/batch-jobs":
                self._send_json(405, {"error": {"code": "method_not_allowed",
                                                "message": "/api/batch-jobs 只接受 POST。"}}, origin)
            elif path.startswith("/api/batch-jobs/"):
                rest = urllib.parse.unquote(path[len("/api/batch-jobs/"):])
                if rest.endswith("/export"):
                    self._export_batch(rest[: -len("/export")],
                                       (params.get("format") or [""])[0].strip().lower(), origin)
                else:
                    self._batch_status(rest, origin)
            elif path == "/api/query":
                self._send_json(405, {"error": {"code": "method_not_allowed",
                                                "message": "/api/query 只接受 POST。"}}, origin)
            else:
                self._send_json(404, {"error": {"code": "not_found", "message": f"未知路径：{path}"}}, origin)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": _scrub(f"{type(exc).__name__}: {exc}")}}, origin)

    def do_POST(self) -> None:                                  # noqa: N802
        origin = self._guard_origin()
        if origin is None:
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/meta":
            self._send_json(405, {"error": {"code": "method_not_allowed",
                                            "message": "/api/meta 只接受 GET。"}}, origin)
            return
        if path == "/api/user-documents" or path.startswith("/api/user-documents/"):
            doc_id = (urllib.parse.unquote(path[len("/api/user-documents/"):])
                      if path.startswith("/api/user-documents/") else None)
            try:
                self._handle_userdocs("POST", doc_id, origin)
            except ApiError as exc:
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            except Exception as exc:                            # noqa: BLE001
                traceback.print_exc()
                self._send_json(500, {"error": {"code": "internal_error",
                                                "message": _scrub(f"{type(exc).__name__}: {exc}")}}, origin)
            return
        if path == "/api/batch-jobs" or path.startswith("/api/batch-jobs/"):
            try:
                if path == "/api/batch-jobs":
                    self._create_batch(origin)
                elif path.endswith("/cancel"):
                    job_id = urllib.parse.unquote(path[len("/api/batch-jobs/"): -len("/cancel")])
                    self._batch_status(job_id, origin, cancel=True)
                else:
                    self._send_json(404, {"error": {"code": "not_found",
                                                    "message": f"未知路径：{path}"}}, origin)
            except ApiError as exc:
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
            except Exception as exc:                            # noqa: BLE001
                traceback.print_exc()
                self._send_json(500, {"error": {"code": "internal_error",
                                                "message": _scrub(f"{type(exc).__name__}: {exc}")}}, origin)
            return
        if path != "/api/query":
            self._send_json(404, {"error": {"code": "not_found", "message": f"未知路径：{path}"}}, origin)
            return
        try:
            payload = validate_query(self._read_json_body())
            result = run_query(payload, llm_base_url=self.llm_base_url, llm_model=self.llm_model)
            self._send_json(200, result, origin)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": _scrub(f"{type(exc).__name__}: {exc}")}}, origin)

    def do_DELETE(self) -> None:                                # noqa: N802
        origin = self._guard_origin()
        if origin is None:
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not path.startswith("/api/user-documents/"):
            if path == "/api/user-documents":
                self._send_json(400, {"error": {"code": "invalid_id",
                                                "message": "DELETE 需要 /api/user-documents/{id}。"}}, origin)
            else:
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": f"未知路径：{path}"}}, origin)
            return
        doc_id = urllib.parse.unquote(path[len("/api/user-documents/"):])
        try:
            self._handle_userdocs("DELETE", doc_id, origin)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, origin)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": _scrub(f"{type(exc).__name__}: {exc}")}}, origin)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="第三版前端的本机薄接口（只绑 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--llm-base-url", default=config.DEEPSEEK_BASE_URL,
                    help="生成层目标地址；默认取 config.py，联调时可指向本机桩")
    ap.add_argument("--llm-model", default=config.DEEPSEEK_MODEL)
    args = ap.parse_args(argv)

    Handler.llm_base_url = args.llm_base_url
    Handler.llm_model = args.llm_model

    # 启动横幅：把"会不会真的调模型""调去哪里"写在明面上，便于复核
    print(f"[api_v3] 绑定 http://{HOST}:{args.port}（只绑本机回环）")
    print(f"[api_v3] 生成层目标 base_url={args.llm_base_url} model={args.llm_model}")
    print(f"[api_v3] 服务端 Key：{'已检测到（只返回布尔，不回显内容）' if api_key_configured() else '未检测到'}")
    print(f"[api_v3] 允许的 CORS 来源：{', '.join(ALLOWED_ORIGINS)}")
    print(f"[api_v3] 知识库：{', '.join(ALLOWED_CORPORA)}；Top-K 允许 {TOP_K_MIN}–{TOP_K_MAX}；"
          f"问题上限 {MAX_QUESTION_CHARS} 字符", flush=True)
    print(f"[api_v3] 批量提问：每次 {bj.MIN_QUESTIONS}–{bj.MAX_QUESTIONS} 道；生成模式串行、"
          f"每题最多 1 次请求、零自动重试；导出 {', '.join(BATCH_EXPORT_FORMATS)}", flush=True)

    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[api_v3] 收到中断，退出。", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
