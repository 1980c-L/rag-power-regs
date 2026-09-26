# -*- coding: utf-8 -*-
"""「我的资料库」v1 —— 用户自导入资料（40 号方案第 3 步）。

定位与边界（刻意保持小而可验证）：
  - 独立于内置 `rag_learning`（**永不覆盖、永不删除、不混库**）；
    内置库的 Hit@K / MRR 评测指标**不适用**于本库：本库没有评测问题集，
    只提供来源数 / chunk 数 / 字符数 / 指纹这类事实性统计。
  - 切分与 BM25 完全复用 `rag_core.py` 的既有逻辑（kind="paragraph" 路径），
    本文件里没有一行检索或切分算法；只做"资料的进出来自哪里"这一层。
  - 通过把自身注册进 `rag_core.CORPORA` 挂载为第三个知识库
    （id = `user_library`），不改 `rag_core.py` 的任何字节。
  - 第一版只接受纯文本：`.txt` / `.md` 文件上传 + 粘贴文本；
    不做 PDF / Word / URL 抓取 / OCR（40 号明确不做）。

导入流程（与 40 号方案一致）：
  校验类型/大小/编码/空内容 → 计算 SHA-256（重复资料提示而不重复导入）
  → 写入 documents/（临时文件 + 原子替换）→ 原子更新 manifest
  → 用 rag_core 现有切分重建 chunk 快照（index/）→ 返回逐条结果。

安全边界：
  - 存储文件名一律由程序生成（标题净化 + 内容哈希前 8 位），
    用户提供的原文件名只作为展示信息，绝不用于拼路径（拒绝路径穿越）；
  - 删除必须带显式确认（API 层要求 confirm=true，页面层二次确认）；
  - 所有落盘都是"临时文件 + os.replace"原子替换，进程中断不留半份 manifest。

测试隔离：设环境变量 `RAG_USER_LIBRARY_DIR` 可把整个数据目录指到别处
（回归脚本用它把测试数据隔离在临时目录，不污染真实用户资料）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import rag_core as rc                                             # noqa: E402

# ---------------- 标识与路径 ----------------
USER_LIBRARY = "user_library"

# 环境变量覆盖（只服务于回归测试；正常使用永远指向仓库内的 用户资料/）
_DATA_ROOT = Path(os.environ.get("RAG_USER_LIBRARY_DIR") or (REPO / "用户资料"))
DOCUMENTS_DIR = _DATA_ROOT / "documents"      # 用户导入的正文（全部存成 .txt）
INDEX_DIR = _DATA_ROOT / "index"              # 程序重建的 chunk 快照
MANIFEST_PATH = _DATA_ROOT / "user_manifest.json"   # 标题 / 来源 / 原文件名 / 时间 / sha256

# ---------------- 第一版限制（40 号方案定值，超出直接拒绝） ----------------
MAX_FILE_BYTES = 2 * 1024 * 1024          # 单份资料 ≤ 2 MB
MAX_BATCH_ITEMS = 10                      # 一次最多 10 份
MAX_TOTAL_BYTES = 50 * 1024 * 1024        # 全库 ≤ 50 MB
ALLOWED_SUFFIXES = (".txt", ".md")
MANIFEST_SCHEMA = 1

# 存储文件名里允许出现的字符：中英文、数字、点、横线、下划线（其余一律剥掉）
_SAFE_NAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)
_TITLE_MAX = 40

# 原文编码探测顺序：utf-8（含 BOM）→ gbk；都解不开就拒绝，绝不静默乱码
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gbk")


class LibraryError(Exception):
    """带机器可读错误码的资料库操作失败（API 层转成 4xx 响应）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 注册进 rag_core（不改 rag_core 一个字节） ----------------
def register() -> None:
    """把「我的资料库」挂载为第三个知识库。重复调用幂等。"""
    rc.CORPORA[USER_LIBRARY] = {
        "id": USER_LIBRARY,
        "name": "我的资料库",
        "note": "本机导入的个人资料（.txt/.md + 粘贴文本），与内置库物理分开，"
                "不参与任何评测，不继承内置库指标",
        "kind": "paragraph",              # 段落式切分（电力示例库同一条既有路径）
        "dir": DOCUMENTS_DIR,
        "glob": "*.txt",                  # 所有内容统一存成 .txt，复用同一加载器
        "manifest": None,                 # 用户库有自己的 manifest（本文件管理），不走 rag_core 的
        "questions": None,                # 没有评测问题集 → 没有 Hit@K / MRR
        "index": INDEX_DIR / "chunks_user_library.json",
        "report": None,
    }


register()  # import 即注册：api_v3 计算 ALLOWED_CORPORA 前必须先 import 本模块


# ---------------- manifest ----------------
def _empty_manifest() -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "note": "「我的资料库」来源清单；由程序维护，手工修改可能被下次导入覆盖",
        "documents": [],
    }


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return _empty_manifest()
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LibraryError("manifest_corrupt", "user_manifest.json 不是合法 JSON，"
                          "请用「重建索引」尝试恢复，或手工备份后清空。")
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise LibraryError("manifest_corrupt", "user_manifest.json 结构不对（缺 documents 列表）。")
    return data


def _save_manifest_atomic(manifest: dict) -> None:
    """临时文件 + os.replace：中断也不会留下半份 manifest。"""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, MANIFEST_PATH)


# ---------------- 指纹 / 统计 ----------------
def library_fingerprint() -> str:
    """库指纹：全部正文文件字节哈希（按文件名排序）+ 分词器 + 切分参数。

    rag_learning 的指标冻结不适用于本库，这里给出指纹只是为了"资料变了"可被看见。
    """
    h = hashlib.sha256()
    h.update(f"{USER_LIBRARY}|{rc.TOKENIZER_NAME}|{rc.CHUNK_MAX_CHARS}".encode("utf-8"))
    for fp in sorted(DOCUMENTS_DIR.glob("*.txt")) if DOCUMENTS_DIR.exists() else []:
        h.update(f"|{fp.name}|".encode("utf-8"))
        h.update(hashlib.sha256(fp.read_bytes()).digest())
    return h.hexdigest()


def total_bytes() -> int:
    if not DOCUMENTS_DIR.exists():
        return 0
    return sum(fp.stat().st_size for fp in DOCUMENTS_DIR.glob("*.txt"))


def available() -> bool:
    """库里有没有可用资料（空库不可选为知识库）。"""
    return bool(DOCUMENTS_DIR.exists() and list(DOCUMENTS_DIR.glob("*.txt")))


def stats() -> dict:
    """统计全部来自 rag_core 现算；空库返回零值而不是报错。"""
    if not available():
        return {"source_count": 0, "chunk_count": 0, "total_chars": 0,
                "fingerprint": library_fingerprint()}
    st = rc.corpus_stats(USER_LIBRARY)
    return {"source_count": st["source_count"], "chunk_count": st["chunk_count"],
            "total_chars": st["total_chars"], "fingerprint": st["fingerprint"]}


def meta_summary() -> dict:
    """给 /api/meta 的用户库摘要。documents 只给展示字段，不含正文。"""
    manifest = _load_manifest()
    st = stats()
    return {
        "available": available(),
        "name": rc.CORPORA[USER_LIBRARY]["name"],
        "note": rc.CORPORA[USER_LIBRARY]["note"],
        "source_count": st["source_count"],
        "chunk_count": st["chunk_count"],
        "total_chars": st["total_chars"],
        "fingerprint": st["fingerprint"],
        "total_bytes": total_bytes(),
        "limits": {"max_file_bytes": MAX_FILE_BYTES, "max_batch_items": MAX_BATCH_ITEMS,
                   "max_total_bytes": MAX_TOTAL_BYTES, "allowed_suffixes": list(ALLOWED_SUFFIXES)},
        "documents": manifest["documents"],
    }


# ---------------- 导入 ----------------
def _safe_stem(title: str, fallback: str) -> str:
    """把展示标题净化成可安全入盘的文件名主干（剥离路径分隔符与控制字符）。"""
    text = (title or fallback).strip()
    text = _SAFE_NAME_RE.sub("", text).strip(".-")[:_TITLE_MAX]
    return text or "未命名"


def _decode(raw: bytes, *, hint_name: str) -> tuple[str, str]:
    for enc in _ENCODING_CANDIDATES:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, ValueError):
            continue
    raise LibraryError("encoding_unsupported",
                       f"{hint_name}：不是 UTF-8 也不是 GBK 编码的纯文本，已拒绝"
                       "（第一版不支持 PDF / Word / 二进制文件）。")


def _normalize_paste_text(text: str) -> str:
    # 统一换行，去掉 BOM 与结尾多余空行；保留正文内部原样
    return text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def import_documents(items: list) -> dict:
    """批量导入。每个 item：{kind: paste|file, text, title, source_name,
    source_url, original_filename}。返回逐条结果（不因单条失败中断整批）。"""
    if not isinstance(items, list) or not items:
        raise LibraryError("empty_batch", "items 必须是非空列表。")
    if len(items) > MAX_BATCH_ITEMS:
        raise LibraryError("batch_too_large", f"一次最多导入 {MAX_BATCH_ITEMS} 份资料。")

    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    by_sha = {d["sha256"]: d for d in manifest["documents"]}
    results: list = []
    pending_manifest = False

    # 预算检查：本批累计字节数也要计入 50 MB 上限
    # 42 号 P2 窄修：准备期用 seen_sha（库内已有 + 本批已准备）判重，
    # 同一批里第二份相同内容返回 duplicate，而不是两份都 imported
    seen_sha = dict(by_sha)
    batch_bytes = 0
    prepared: list = []
    for idx, item in enumerate(items):
        try:
            prep = _prepare_item(item, seen_sha)
            prep["index"] = idx
            prepared.append(prep)
            batch_bytes += prep["size_bytes"]
            if not prep.get("dup_of"):
                seen_sha[prep["sha256"]] = prep["entry"]   # 登记进批内待写集合
        except LibraryError as exc:
            results.append({"index": idx, "status": "rejected",
                            "reason": exc.message, "code": exc.code})
    if batch_bytes + total_bytes() > MAX_TOTAL_BYTES:
        raise LibraryError("library_full",
                           f"全库上限 {MAX_TOTAL_BYTES // (1024 * 1024)} MB："
                           "这批资料放不下，未写入任何文件。")

    for prep in prepared:
        if prep.get("dup_of"):
            results.append({"index": prep["index"], "status": "duplicate",
                            "id": prep["dup_of"]["id"], "title": prep["dup_of"]["title"],
                            "reason": "内容完全相同（SHA-256 一致），未重复导入"})
            continue
        # 写正文：临时文件 + 原子替换；文件名由程序生成，与用户输入隔离
        target = DOCUMENTS_DIR / f"{prep['doc_id']}.txt"
        tmp = target.with_suffix(".txt.tmp")
        tmp.write_text(prep["text"], encoding="utf-8", newline="\n")
        os.replace(tmp, target)
        manifest["documents"].append(prep["entry"])
        by_sha[prep["sha256"]] = prep["entry"]
        pending_manifest = True
        item_result = {"index": prep["index"], "status": "imported",
                       "id": prep["doc_id"], "title": prep["entry"]["title"],
                       "chars": prep["entry"]["chars"], "encoding": prep["entry"]["encoding"]}
        if prep.get("source_encoding") and prep["source_encoding"] != "utf-8":
            item_result["source_encoding"] = prep["source_encoding"]
        results.append(item_result)

    if pending_manifest:
        _save_manifest_atomic(manifest)
        rebuild_index()
    return {"results": results, "library": meta_summary()}


def _prepare_item(item: object, by_sha: dict) -> dict:
    """单条资料的全部校验与准备。通过后返回写盘所需的一切，不落盘。"""
    if not isinstance(item, dict):
        raise LibraryError("invalid_item", "每条资料必须是对象。")
    kind = item.get("kind")
    if kind not in ("paste", "file", "file_b64"):
        raise LibraryError("invalid_kind",
                           "kind 只允许 paste（粘贴文本）/ file（已解码文本）/ file_b64（原始字节）。")

    unknown = sorted(set(item) - {"kind", "text", "title", "source_name",
                                  "source_url", "original_filename", "data_b64"})
    if unknown:
        raise LibraryError("unexpected_field", f"不认识的字段：{', '.join(unknown)}")

    original_filename = item.get("original_filename") if kind in ("file", "file_b64") else None
    if kind == "file_b64":
        if not isinstance(original_filename, str) or not original_filename.strip():
            raise LibraryError("filename_required", "上传文件必须提供 original_filename。")
        suffix = Path(original_filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise LibraryError("type_unsupported",
                               f"只支持 {' / '.join(ALLOWED_SUFFIXES)}，拒绝了 {original_filename!r}。")
        import base64
        b64 = item.get("data_b64")
        if not isinstance(b64, str) or not b64:
            raise LibraryError("data_required", "file_b64 必须携带 data_b64（base64 的文件字节）。")
        try:
            file_bytes = base64.b64decode(b64, validate=True)
        except Exception:                                          # noqa: BLE001
            raise LibraryError("bad_base64", "data_b64 不是合法的 base64。")
        # 42 号 P1 窄修：限额必须对**原始字节**做（base64 后的字符串长度不代表文件大小，
        # 5 MB 请求体上限也不等于 2 MB 文件上限——独立复审用 2 MB+1 字节反例抓到过漏网）
        if len(file_bytes) > MAX_FILE_BYTES:
            raise LibraryError("file_too_large",
                               f"单份资料上限 {MAX_FILE_BYTES // (1024 * 1024)} MB。")
        text, src_enc = _decode(file_bytes, hint_name=original_filename)
        # 源编码记录在结果里；落盘仍统一 utf-8
        text = _normalize_paste_text(text)
        if not text.strip():
            raise LibraryError("empty_text", "资料正文为空（只有空白），已拒绝。")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        dup = by_sha.get(sha)
        title = (item.get("title") or "").strip() or Path(original_filename).stem
        if dup:
            return {"sha256": sha, "text": text, "size_bytes": len(file_bytes),
                    "dup_of": dup, "source_encoding": src_enc}
        doc_id = f"{_safe_stem(title, original_filename)}-{sha[:8]}"
        source_url = (item.get("source_url") or "").strip()
        if source_url and not re.match(r"^https?://", source_url):
            raise LibraryError("bad_source_url", "来源链接必须以 http:// 或 https:// 开头。")
        entry = {
            "id": doc_id, "title": title or "未命名",
            "source_name": (item.get("source_name") or "").strip(),
            "source_url": source_url,
            "original_filename": original_filename.strip(),
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sha256": sha, "chars": len(text), "encoding": "utf-8",
        }
        return {"sha256": sha, "text": text, "size_bytes": len(file_bytes),
                "doc_id": doc_id, "entry": entry, "dup_of": None,
                "source_encoding": src_enc}
    if kind == "file":
        if not isinstance(original_filename, str) or not original_filename.strip():
            raise LibraryError("filename_required", "上传文件必须提供 original_filename。")
        suffix = Path(original_filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise LibraryError("type_unsupported",
                               f"只支持 {' / '.join(ALLOWED_SUFFIXES)}，拒绝了 {original_filename!r}。")

    text = item.get("text")
    if not isinstance(text, str):
        raise LibraryError("text_required", "text 必须是字符串。")
    raw = text.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise LibraryError("file_too_large",
                           f"单份资料上限 {MAX_FILE_BYTES // (1024 * 1024)} MB。")
    body = _normalize_paste_text(text)
    if not body.strip():
        raise LibraryError("empty_text", "资料正文为空（只有空白），已拒绝。")

    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    dup = by_sha.get(sha)
    title = (item.get("title") or "").strip() or (
        Path(original_filename).stem if kind == "file" else "")
    if dup:
        return {"sha256": sha, "text": body,
                "size_bytes": len(raw), "dup_of": dup}
    doc_id = f"{_safe_stem(title, original_filename or '粘贴文本')}-{sha[:8]}"

    # source_url 必须真的是 http(s) 链接或者空；别的协议直接拒绝
    source_url = (item.get("source_url") or "").strip()
    if source_url and not re.match(r"^https?://", source_url):
        raise LibraryError("bad_source_url", "来源链接必须以 http:// 或 https:// 开头。")

    entry = {
        "id": doc_id,
        "title": title or "未命名",
        "source_name": (item.get("source_name") or "").strip(),
        "source_url": source_url,
        "original_filename": (original_filename or "").strip() or None,
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sha256": sha,
        "chars": len(body),
        "encoding": "utf-8",   # 落盘统一 utf-8；原始编码见下
    }
    return {"sha256": sha, "text": body,
            "size_bytes": len(raw), "doc_id": doc_id, "entry": entry, "dup_of": None}


def _file_to_item(file_bytes: bytes, original_filename: str, title: str,
                  source_name: str, source_url: str) -> tuple[dict, str]:
    """把上传文件的字节解成一条导入 item（含编码探测）。返回 (item, 实际编码)。"""
    if len(file_bytes) > MAX_FILE_BYTES:
        raise LibraryError("file_too_large",
                           f"单份资料上限 {MAX_FILE_BYTES // (1024 * 1024)} MB。")
    text, enc = _decode(file_bytes, hint_name=original_filename)
    return ({"kind": "file", "text": text, "title": title, "source_name": source_name,
             "source_url": source_url, "original_filename": original_filename}, enc)


# ---------------- 删除与重建 ----------------
def delete_document(doc_id: str) -> dict:
    """删除一份资料并重建。doc_id 必须逐字对上 manifest（防路径穿越：绝不拼用户输入）。"""
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise LibraryError("invalid_id", "缺少要删除的资料 id。")
    manifest = _load_manifest()
    kept = [d for d in manifest["documents"] if d.get("id") == doc_id]
    if not kept:
        raise LibraryError("unknown_document", f"资料库里没有 id 为 {doc_id!r} 的资料。")
    target = DOCUMENTS_DIR / f"{doc_id}.txt"
    # 双保险：即使 id 被构造过，也只允许删除 manifest 里登记过的那个文件名形状
    if target.exists() and target.parent == DOCUMENTS_DIR:
        os.remove(target)
    manifest["documents"] = [d for d in manifest["documents"] if d.get("id") != doc_id]
    _save_manifest_atomic(manifest)
    rebuild_index()
    return {"deleted": doc_id, "library": meta_summary()}


def rebuild_index() -> dict:
    """用 rag_core 现有切分重建 chunk 快照（index/chunks_user_library.json，原子替换）。

    检索本身始终由 rag_core 从 documents/ 现算；这份快照是**可核查的产物**：
    页面与验收脚本用它确认"manifest ↔ 磁盘 ↔ 索引"三者一致。
    """
    manifest = _load_manifest()
    missing = [d["id"] for d in manifest["documents"]
               if not (DOCUMENTS_DIR / f"{d['id']}.txt").exists()]
    orphans = [fp.stem for fp in (DOCUMENTS_DIR.glob("*.txt") if DOCUMENTS_DIR.exists() else [])
               if fp.stem not in {d["id"] for d in manifest["documents"]}]
    chunks: list = []
    if available():
        docs = rc.load_documents(USER_LIBRARY)
        chunks = [{"id": d["id"], "source_id": d["source_id"], "para": d["para"],
                   "chars": len(d["text"]), "text": d["text"]} for d in docs]
        # 顺带验证 BM25 真的可建（空词表等极端情况在这里暴露）
        rc.build_index(USER_LIBRARY)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fingerprint": library_fingerprint(),
        "chunk_count": len(chunks),
        "consistency": {"missing_documents": missing, "orphan_files": orphans},
        "chunks": chunks,
    }
    target = INDEX_DIR / "chunks_user_library.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return snapshot


def reindex() -> dict:
    """显式重建入口（API 的 /api/user-documents/reindex）。"""
    snapshot = rebuild_index()
    return {"reindexed": True, "fingerprint": snapshot["fingerprint"],
            "chunk_count": snapshot["chunk_count"],
            "consistency": snapshot["consistency"], "library": meta_summary()}


# ---------------- 上传文件的便捷入口（api_v3 用） ----------------
def import_file_bytes(file_bytes: bytes, *, original_filename: str, title: str = "",
                      source_name: str = "", source_url: str = "") -> dict:
    """上传文件 → 编码探测 → 走统一的 import_documents。"""
    item, enc = _file_to_item(file_bytes, original_filename, title, source_name, source_url)
    out = import_documents([item])
    if enc != "utf-8":
        for r in out["results"]:
            if r.get("status") == "imported":
                r["source_encoding"] = enc
    return out
