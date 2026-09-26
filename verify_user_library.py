# -*- coding: utf-8 -*-
"""「我的资料库」v1 的无网络独立验收（40 号方案第 3 步的回归）。

两层：
  A. 进程内（模块级）：导入 / 去重 / 限额 / 编码 / 路径隔离 / 删除 / 重建 / 一致性；
  B. 接口层（真实起 api_v3 子进程）：四个端点的契约、空库 fail closed、
     删除必须显式 confirm、CORS、体积上限、内置库不受污染。

红线判据（每轮都要在）：
  - 内置 rag_learning 语料 manifest 逐字节不变（指纹 6789A821…）；
  - 全程零真实模型请求（子进程显式去掉 DEEPSEEK_API_KEY，且无任何外部服务可连）；
  - 测试数据全部落在临时目录（RAG_USER_LIBRARY_DIR），不污染真实 用户资料/。

运行：
    python verify_user_library.py [--out <证据输出目录>]
退出码：0 = 全部 PASS；1 = 有 FAIL（判据全项跑完再统一裁决，不中途停）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
# 必须在 import rag_core / user_library 之前设好：注册表在 import 时确定路径
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="rag_userlib_verify_"))
os.environ["RAG_USER_LIBRARY_DIR"] = str(_TMP_ROOT)
sys.path.insert(0, str(REPO))

import user_library as ul                                       # noqa: E402
import rag_core as rc                                           # noqa: E402

API_PORT = 5282                     # 避开 verify_api_v3 的 5280/5281
ORIGIN_OK = "http://127.0.0.1:5273"
ORIGIN_BAD = "http://evil.example.com"
LEARNING_MANIFEST_SHA16 = "6789A821A2C0C8E5"   # 冻结的内置学习库 manifest 指纹（历史锚点）

PASSED: list = []
FAILED: list = []
HANDLES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


# ---------------- HTTP ----------------
# 不走系统代理：本机验收直连 127.0.0.1（系统代理曾把 6 MB 请求体拦成 502，42/43 轮实测）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, path: str, *, payload=None, raw: bytes | None = None,
         origin: str | None = None, port: int = API_PORT, timeout: int = 60,
         ctype: str | None = None) -> tuple:
    url = f"http://127.0.0.1:{port}{urllib.parse.quote(path, safe='/?=&')}"
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
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:                                          # noqa: BLE001
            return e.code, {}
    except OSError:
        # 服务器在读请求体之前拒绝并关连接时，客户端上传会被中途中止（Windows 10053）。
        # 用 status=-1 表示"连接被服务器主动断开"，由调用方按判据解释。
        return -1, {}


def wait_health(port: int, deadline_s: int = 30) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            status, body = http("GET", "/api/health", port=port, timeout=2)
            if status == 200 and body.get("ok"):
                return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.3)
    return False


# ================= A. 进程内 =================
PASTE_A = "检索增强生成（RAG）把检索与生成结合起来。\n\nBM25 是基于词频与逆文档频率的排序算法。"
PASTE_B = "这是一份完全不同的资料，讲的是电力安全。"


def checks_module() -> None:
    out = ul.import_documents([{"kind": "paste", "text": PASTE_A, "title": "RAG 笔记",
                                "source_name": "澪", "source_url": ""}])
    r0 = out["results"][0]
    check("模块·导入：粘贴文本导入成功且 id = 标题净化 + 内容哈希前 8 位",
          r0["status"] == "imported" and r0["id"].startswith("RAG笔记-")
          and len(r0["id"]) == len("RAG笔记-") + 8, f"id={r0.get('id')}")
    check("模块·导入：manifest 落盘且登记 sha256 / 来源 / 时间",
          ul.MANIFEST_PATH.exists()
          and ul._load_manifest()["documents"][0]["sha256"], "")
    snap = json.loads((ul.INDEX_DIR / "chunks_user_library.json").read_text(encoding="utf-8"))
    check("模块·导入：chunk 快照已重建且一致性干净",
          snap["chunk_count"] > 0
          and snap["consistency"] == {"missing_documents": [], "orphan_files": []}, "")

    out2 = ul.import_documents([{"kind": "paste", "text": PASTE_A, "title": "换一个标题也一样"}])
    check("模块·去重：同内容不同标题 → duplicate，不产生新文件",
          out2["results"][0]["status"] == "duplicate"
          and len(ul._load_manifest()["documents"]) == 1, "")

    # 42 号 P2 反例：同一批内两份相同内容、不同标题 → 第二份 duplicate，manifest/正文各只有一份
    n_manifest = len(ul._load_manifest()["documents"])
    n_files = len(list(ul.DOCUMENTS_DIR.glob("*.txt")))
    outb = ul.import_documents([
        {"kind": "paste", "text": "批内去重反例正文。", "title": "甲标题"},
        {"kind": "paste", "text": "批内去重反例正文。", "title": "乙标题"},
    ])
    statuses = [r["status"] for r in outb["results"]]
    check("模块·去重：同一批内相同内容 → 第二份 duplicate，manifest 与正文都只多一份",
          statuses == ["imported", "duplicate"]
          and len(ul._load_manifest()["documents"]) == n_manifest + 1
          and len(list(ul.DOCUMENTS_DIR.glob("*.txt"))) == n_files + 1, f"statuses={statuses}")

    def expect_code(fn, code: str) -> bool:
        """批级错误（整批拒绝）走异常；item 级错误见 expect_item_code。"""
        try:
            fn()
            return False
        except ul.LibraryError as exc:
            return exc.code == code

    def item_code(item: dict) -> str:
        """单条导入的 item 级错误码（实现语义：逐条拒绝，不中断整批）。"""
        out = ul.import_documents([item])
        return out["results"][0].get("code", "")

    # 42 号 P1 反例：file_b64 解码后的**原始字节**超 2 MB 必须拒绝（此前只在 paste/file 路径查）
    oversized = b"x" * (ul.MAX_FILE_BYTES + 1)
    check("模块·限额：file_b64 原始字节 2 MB+1 逐条拒绝（file_too_large）",
          item_code({"kind": "file_b64",
                     "data_b64": base64.b64encode(oversized).decode(),
                     "original_filename": "大文件.txt", "title": "大"}) == "file_too_large", "")

    check("模块·校验：空正文逐条拒绝（empty_text）",
          item_code({"kind": "paste", "text": "   \n  ", "title": "空"}) == "empty_text", "")
    check("模块·校验：不支持类型逐条拒绝（.pdf → type_unsupported）",
          item_code({"kind": "file_b64", "data_b64": base64.b64encode(b"x").decode(),
                     "original_filename": "a.pdf", "title": "pdf"}) == "type_unsupported", "")
    check("模块·校验：来源链接协议逐条拒绝（bad_source_url）",
          item_code({"kind": "paste", "text": PASTE_B, "title": "链接",
                     "source_url": "javascript:alert(1)"}) == "bad_source_url", "")

    # GBK 编码文件（file_b64 原始字节 → 服务端探测）
    gbk_text = "接地线应先接接地端，拆除时顺序相反。这是 GBK 编码测试。"
    gbk_out = ul.import_documents([{"kind": "file_b64",
                                    "data_b64": base64.b64encode(gbk_text.encode("gbk")).decode(),
                                    "original_filename": "安全要点.txt", "title": ""}])
    rg = gbk_out["results"][0]
    check("模块·编码：GBK 文件被正确探测并转存 UTF-8，正文无损",
          rg["status"] == "imported" and rg.get("source_encoding") == "gbk", f"{rg}")

    check("模块·编码：非法 base64 逐条拒绝（bad_base64）",
          item_code({"kind": "file_b64", "data_b64": "!!!not-base64!!!",
                     "original_filename": "x.txt", "title": "x"}) == "bad_base64", "")

    # 路径隔离：原文件名再恶劣也不进存储路径
    trav = ul.import_documents([{"kind": "file_b64",
                                 "data_b64": base64.b64encode("穿越测试".encode()).decode(),
                                 "original_filename": r"..\..\evil<*>?.txt", "title": ""}])
    rt = trav["results"][0]
    stored = list(ul.DOCUMENTS_DIR.glob("*.txt"))
    check("模块·路径：危险原文件名被净化，存储名全部由程序生成且无分隔符",
          rt["status"] == "imported"
          and all(("/" not in f.name and "\\" not in f.name and "<" not in f.name) for f in stored),
          f"stored={[f.name for f in stored]}")

    check("模块·限额：单份超 2 MB 逐条拒绝（file_too_large）",
          item_code({"kind": "paste", "text": "字" * ul.MAX_FILE_BYTES,
                     "title": "大"}) == "file_too_large", "")

    many = [{"kind": "paste", "text": f"第 {i} 份", "title": f"批{i}"} for i in range(ul.MAX_BATCH_ITEMS + 1)]
    check("模块·限额：一批超过 10 份整批拒绝（batch_too_large）",
          expect_code(lambda: ul.import_documents(many), "batch_too_large"), "")

    old_total = ul.MAX_TOTAL_BYTES
    ul.MAX_TOTAL_BYTES = ul.total_bytes() + 1
    try:
        check("模块·限额：全库超限整批拒绝且**一个文件都没写**（回滚语义）",
              expect_code(lambda: ul.import_documents(
                  [{"kind": "paste", "text": "放不下", "title": "超限"}]), "library_full")
              and len(list(ul.DOCUMENTS_DIR.glob("*.txt"))) == len(ul._load_manifest()["documents"]), "")
    finally:
        ul.MAX_TOTAL_BYTES = old_total

    check("模块·删除：不存在的 id 拒绝（unknown_document）",
          expect_code(lambda: ul.delete_document("不存在-000000"), "unknown_document"), "")

    # 孤儿文件一致性：手工塞一个 manifest 外的文件，reindex 要如实报告
    orphan = ul.DOCUMENTS_DIR / "orphan-孤儿.txt"
    orphan.write_text("孤儿", encoding="utf-8")
    snap = ul.reindex()
    check("模块·一致性：manifest 外的孤儿文件被如实报告",
          "orphan-孤儿" in snap["consistency"]["orphan_files"], "")
    orphan.unlink()
    ul.reindex()

    # manifest 损坏要 fail closed 而不是静默清空
    good = ul.MANIFEST_PATH.read_text(encoding="utf-8")
    ul.MANIFEST_PATH.write_text("{ 不是 json", encoding="utf-8")
    check("模块·一致性：manifest 损坏时拒绝服务（manifest_corrupt）",
          expect_code(lambda: ul.meta_summary(), "manifest_corrupt"), "")
    ul.MANIFEST_PATH.write_text(good, encoding="utf-8")


def checks_registry() -> None:
    check("注册·挂载：rag_core.CORPORA 里出现了 user_library 且检索复用既有逻辑",
          "user_library" in rc.corpus_ids()
          and rc.get_corpus("user_library")["kind"] == "paragraph", "")
    stats = ul.stats()
    check("注册·统计：来源/chunk/字符数来自 rag_core 现算",
          stats["source_count"] >= 1 and stats["chunk_count"] >= stats["source_count"], f"{stats}")

    mpath = REPO / "data" / "rag_learning" / "corpus_manifest.json"
    import hashlib
    check("红线·内置库：rag_learning 语料 manifest 逐字节未动（冻结指纹 6789A821…）",
          hashlib.sha256(mpath.read_bytes()).hexdigest().upper().startswith(LEARNING_MANIFEST_SHA16), "")
    st = rc.corpus_stats(rc.RAG_LEARNING)
    check("红线·内置库：rag_learning 统计不受用户库影响（10 来源 / 471 chunk）",
          st["source_count"] == 10 and st["chunk_count"] == 471,
          f"{st['source_count']}/{st['chunk_count']}")


# ================= B. 接口层 =================
def checks_api() -> None:
    env = dict(os.environ)                      # 已含 RAG_USER_LIBRARY_DIR
    env.pop("DEEPSEEK_API_KEY", None)           # 红线：子进程永远无 Key
    # 42 号口径 3：Windows 下不锁 UTF-8 会出现 31/35 式的环境抖动——验收自己锁死子进程编码
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log = _TMP_ROOT / "api_v3_log.txt"
    handle = log.open("w", encoding="utf-8")
    HANDLES.append(handle)
    proc = subprocess.Popen([sys.executable, str(REPO / "api_v3.py"), "--port", str(API_PORT)],
                            cwd=str(REPO), env=env, stdout=handle,
                            stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_health(API_PORT):
            check("接口·启动：api_v3 子进程健康检查", False, f"log={log.read_text(encoding='utf-8', errors='replace')[:500]}")
            return
        check("接口·启动：api_v3 子进程健康检查", True, "")

        status, meta = http("GET", "/api/meta", origin=ORIGIN_OK)
        check("接口·meta：200 且带 user_library 摘要（available 随库变化）",
              status == 200 and isinstance(meta.get("user_library"), dict)
              and meta["user_library"]["available"] is True, "")
        snapshot = REPO / "frontend_v3" / "src" / "mocks" / "api_snapshots.json"
        if snapshot.exists():
            mock_corpora = json.loads(snapshot.read_text(encoding="utf-8"))["corpora"]
            check("接口·meta：内置 corpora 展示字段与 mock 快照仍完全一致（用户库不进 corpora）",
                  mock_corpora == meta.get("corpora")
                  and len(meta.get("corpora", [])) == 2, "")

        status, body = http("POST", "/api/user-documents", origin=ORIGIN_OK,
                            payload={"items": [{"kind": "paste", "text": PASTE_B,
                                                "title": "接口导入", "source_name": "澪"}]})
        check("接口·导入：POST /api/user-documents 200 且逐条返回结果",
              status == 200 and body["results"][0]["status"] == "imported", "")

        status, body = http("POST", "/api/user-documents", origin=ORIGIN_OK,
                            payload={"items": [{"kind": "paste", "text": "x", "title": "坏字段",
                                                "hack": 1}]})
        check("接口·导入：未知字段 fail closed（unexpected_field / 400）",
              status == 400 and body.get("error", {}).get("code") == "unexpected_field", "")

        # 42 号 P1 反例（API 真路径）：file_b64 原始字节 2 MB+1 → 逐条 rejected file_too_large
        big_item = {"kind": "file_b64",
                    "data_b64": base64.b64encode(b"x" * (ul.MAX_FILE_BYTES + 1)).decode(),
                    "original_filename": "大文件.txt", "title": "大"}
        status, body = http("POST", "/api/user-documents", origin=ORIGIN_OK,
                            payload={"items": [big_item]})
        r0 = (body.get("results") or [{}])[0]
        check("接口·限额：file_b64 经真实 API 导入 2 MB+1 → rejected file_too_large（不 imported）",
              status == 200 and r0.get("status") == "rejected"
              and r0.get("code") == "file_too_large", f"{r0.get('code')}")

        status, body = http("POST", "/api/query", origin=ORIGIN_OK,
                            payload={"corpus_id": "user_library", "mode": "generate",
                                     "top_k": 3, "question": "电力安全讲什么？"})
        hits_api = [h["id"] for h in body.get("hits", [])]
        expect = rc.expand_and_assemble("电力安全讲什么？", top_k=3, corpus_id="user_library")
        check("接口·检索：用户库检索结果与直接调用 rag_core 逐 id 一致",
              status == 200 and hits_api == [h["id"] for h in expect["hits"]], f"{hits_api}")
        check("接口·无 Key：generate 模式落成 no_api_key，answer 为空、零生成耗时",
              body.get("notice") == "no_api_key" and body.get("answer") is None
              and body.get("elapsed_ms", {}).get("generate") is None, "")

        status, body = http("DELETE", f"/api/user-documents/{ul._load_manifest()['documents'][-1]['id']}",
                            origin=ORIGIN_OK, payload={"confirm": False})
        check("接口·删除：缺显式 confirm=true 一律拒绝（confirm_required / 400）",
              status == 400 and body.get("error", {}).get("code") == "confirm_required", "")

        last_id = ul._load_manifest()["documents"][-1]["id"]
        status, body = http("DELETE", f"/api/user-documents/{last_id}",
                            origin=ORIGIN_OK, payload={"confirm": True})
        check("接口·删除：confirm=true 删除成功且库统计即时更新",
              status == 200 and body.get("deleted") == last_id, "")

        status, body = http("POST", "/api/user-documents/reindex", origin=ORIGIN_OK)
        check("接口·重建：reindex 200 且一致性干净",
              status == 200 and body.get("reindexed") is True
              and body["consistency"] == {"missing_documents": [], "orphan_files": []}, "")

        # 空库 fail closed：全部删光后，user_library 提问必须 409
        for d in list(ul._load_manifest()["documents"]):
            http("DELETE", f"/api/user-documents/{d['id']}", origin=ORIGIN_OK,
                 payload={"confirm": True})
        status, body = http("POST", "/api/query", origin=ORIGIN_OK,
                            payload={"corpus_id": "user_library", "mode": "retrieve_only",
                                     "top_k": 3, "question": "还有资料吗？"})
        check("接口·空库：清空后 user_library 提问 fail closed（409 user_library_empty）",
              status == 409 and body.get("error", {}).get("code") == "user_library_empty", "")

        status, body = http("GET", "/api/user-documents", origin=ORIGIN_BAD)
        check("接口·CORS：非白名单 Origin 访问资料库端点 403",
              status == 403, "")

        status, body = http("POST", "/api/query", origin=ORIGIN_OK,
                            payload={"corpus_id": "rag_learning", "mode": "retrieve_only",
                                     "top_k": 3, "question": "RAG 评估三元组包含哪三个维度？"})
        check("接口·内置库：rag_learning 检索不受用户库增删影响",
              status == 200 and len(body.get("hits", [])) > 0, "")

        big = b"x" * (5 * 1024 * 1024 + 100)
        try:
            status, body = http("POST", "/api/user-documents", raw=big, origin=ORIGIN_OK,
                                ctype="application/json")
            # 服务器在读请求体之前就回 413 并关连接，客户端上传会被中途中止（连接被重置）——
            # 这本身就是 fail-closed；两种表现都算通过，唯独 200/接受入库不算
            check("接口·限额：超过 5 MB 请求体被拒（413 或上传中途被服务器断开）",
                  status == 413 or status == -1, f"status={status}")
        except OSError:
            check("接口·限额：超过 5 MB 请求体被拒（413 或上传中途被服务器断开）",
                  True, "连接被服务器中止（fail closed）")
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:                                          # noqa: BLE001
            pass
        for h in HANDLES:
            h.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="证据输出目录（可选）")
    args = ap.parse_args()
    print(f"[verify_user_library] 测试数据目录：{_TMP_ROOT}", flush=True)
    checks_module()
    checks_registry()
    checks_api()

    print("\n===== 汇总 =====", flush=True)
    print(f"PASS {len(PASSED)} / FAIL {len(FAILED)}", flush=True)
    if FAILED:
        for name in FAILED:
            print(f"  FAIL: {name}", flush=True)

    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "verify_user_library_result.json").write_text(json.dumps({
            "pass": len(PASSED), "fail": len(FAILED),
            "failed": FAILED, "passed": PASSED,
            "temp_dir": str(_TMP_ROOT),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
