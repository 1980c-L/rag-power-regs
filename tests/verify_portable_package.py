# -*- coding: utf-8 -*-
"""便携包打包验收（40 号方案实施顺序第 4 步）。

做法：把"解压即用"所需的最小文件集复制到一个**全新临时目录**（等价于用户解压 ZIP
后的目录），在删掉 DEEPSEEK_API_KEY 的环境里跑真实的 启动-RAG助手.ps1 / 停止-RAG助手.ps1，
逐项验证 40 号交接块点名的验收内容：

  1. 启动：一键启动后 API 与页面都就绪；
  2. 导入：通过页面同款接口导入粘贴文本；
  3. 检索：我的资料库与内置 rag_learning 都能检索，结果与直接调用 rag_core 一致；
  4. 无 Key：generate 模式只落 no_api_key，回答为空（零模型请求——本环境根本没有 Key）；
  5. 重启持久化：停止 → 再启动 → 已导入资料仍在、可检索；
  6. 删除隔离：删除只影响我的资料库，内置库逐字节不变；
  7. 停止：只停本包记录的两个 PID，端口释放。

运行：python tests/verify_portable_package.py [--out <证据目录>] [--keep]
退出码：0 = 全部 PASS；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import rag_core as rc                                          # noqa: E402

ORIGIN_OK = "http://127.0.0.1:5273"
API = "http://127.0.0.1:5280"
WEB = "http://127.0.0.1:5273"
COPY_FILES = [
    "api_v3.py", "rag_core.py", "user_library.py", "config.py",
    # 47 号方案阶段 A/B：批量提问与报告导出。api_v3 顶层就 import 这两个模块，
    # 少一个便携包就起不来（本轮真实踩到：ModuleNotFoundError: batch_jobs）。
    "batch_jobs.py", "batch_export.py",
    "启动-RAG助手.ps1", "停止-RAG助手.ps1", "requirements-api.txt",
]
COPY_DIRS = ["data", Path("frontend_v3") / "dist-api"]

PASSED: list = []
FAILED: list = []
_PKG_DIR: Path | None = None      # 当前验收包目录：异常兜底停止服务用


def stop_pkg_quietly() -> None:
    """验收中途崩溃时也要把已启动的服务停掉（读 .run 里的 pid）。"""
    if _PKG_DIR is None:
        return
    pid_dir = _PKG_DIR / ".run"
    if not ((pid_dir / "api.pid").exists() or (pid_dir / "web.pid").exists()):
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_PKG_DIR / "停止-RAG助手.ps1")],
            cwd=str(_PKG_DIR), timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:                                              # noqa: BLE001
        pass


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机验收直连，不走系统代理


def http(method: str, url: str, *, payload=None, origin: str | None = None,
         timeout: int = 30) -> tuple:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def wait_url(url: str, deadline_s: int = 60) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            status, _ = http("GET", url, timeout=3)
            if status == 200:
                return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.5)
    return False


def run_ps(script: Path) -> int:
    """跑启动/停止脚本；显式删掉 DEEPSEEK_API_KEY（无 Key 红线）。

    输出**落文件**而不是管道：启动脚本拉起的隐藏 python 子进程如果继承了
    管道句柄，capture_output 的 EOF 永远不来，run() 会假死（41 轮真实踩到）。
    """
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env["RAG_NO_BROWSER"] = "1"
    # 42 号口径 3：锁定 UTF-8 子进程环境，避免 31/35 式环境抖动
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log = script.parent / f".run_ps_{script.stem}.log"
    script.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(script.parent), env=env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=180,
        )
    text = log.read_text(encoding="utf-8", errors="replace")
    print(text.strip(), flush=True)
    return proc.returncode


def check_stop_refuses_unrelated_pid(pkg: Path) -> None:
    """42 号 P1 反例的回归：pid 文件里塞一个无关 Python 哨兵，
    停止脚本必须**拒绝停止并保留证据**，哨兵活下来。"""
    run_dir = pkg / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S.000")
        (run_dir / "api.pid").write_text(json.dumps({
            "pid": sentinel.pid, "created": now,
            "cmdline": "python api_v3.py --port 5280",   # 与哨兵真实命令行不符
            "marker": "api_v3.py",
        }), encoding="ascii")
        rc = run_ps(pkg / "停止-RAG助手.ps1")
        time.sleep(0.5)
        alive = sentinel.poll() is None
        kept = (run_dir / "api.pid").exists()
        check("验收·停止安全：pid 文件被塞入无关 Python 时拒绝停止、保留证据、哨兵存活",
              rc != 0 and alive and kept,
              f"rc={rc} alive={alive} pid_file_kept={kept}")
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)
        (run_dir / "api.pid").unlink(missing_ok=True)


def check_stop_rejects_incomplete_records(pkg: Path) -> None:
    """44 号 P1：残缺/旧版/坏日期记录都不能授权杀进程。"""
    cases = [
        ("pid_only", lambda pid: json.dumps({"pid": pid})),
        ("missing_marker", lambda pid: json.dumps({
            "pid": pid, "created": "2026-09-21 12:00:00.000",
            "cmdline": "python api_v3.py --port 5280",
        })),
        ("wrong_pid_type", lambda pid: json.dumps({
            "pid": str(pid), "created": "2026-09-21 12:00:00.000",
            "cmdline": "python api_v3.py --port 5280", "marker": "api_v3.py",
        })),
        ("bad_date", lambda pid: json.dumps({
            "pid": pid, "created": "not-a-date",
            "cmdline": "python api_v3.py --port 5280", "marker": "api_v3.py",
        })),
        ("wrong_marker", lambda pid: json.dumps({
            "pid": pid, "created": "2026-09-21 12:00:00.000",
            "cmdline": "python api_v3.py --port 5280", "marker": "http.server",
        })),
        ("legacy_plain_pid", lambda pid: str(pid)),
    ]
    outcomes = []
    for label, make_record in cases:
        run_dir = pkg / ".run"
        run_dir.mkdir(parents=True, exist_ok=True)
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid_file = run_dir / "api.pid"
        try:
            pid_file.write_text(make_record(sentinel.pid), encoding="ascii")
            rc = run_ps(pkg / "停止-RAG助手.ps1")
            time.sleep(0.2)
            outcomes.append((label, rc != 0, sentinel.poll() is None, pid_file.exists()))
        finally:
            if sentinel.poll() is None:
                sentinel.kill()
                sentinel.wait(timeout=10)
            pid_file.unlink(missing_ok=True)
    check("验收·停止安全：残缺/坏日期/旧版 PID 记录全部 fail closed",
          all(nonzero and alive and kept for _, nonzero, alive, kept in outcomes),
          f"outcomes={outcomes}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep", action="store_true", help="保留验收临时目录（默认删除）")
    args = ap.parse_args()

    pkg = Path(tempfile.mkdtemp(prefix="rag_portable_pkg_"))
    global _PKG_DIR
    _PKG_DIR = pkg
    print(f"[verify_portable] 全新解压目录：{pkg}", flush=True)

    for f in COPY_FILES:
        shutil.copy2(REPO / f, pkg / Path(f).name)
    for d in COPY_DIRS:
        dst = pkg / d                      # 保持相对结构：frontend_v3/dist-api 等
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / d, dst)

    # 解压目录里不该混进任何 Key 或调用卡
    leftovers = [p.name for p in pkg.rglob("*")
                 if p.is_file() and ("real_gen_card" in p.name or ".env" in p.name)]
    check("打包·内容：最小文件集复制完成，且不含调用卡 / .env 等敏感文件",
          len(list(pkg.rglob("*.py"))) >= 4 and not leftovers, f"leftovers={leftovers}")

    # 42 号 P1 反例回归：无关 Python PID 被塞进 pid 文件时必须拒绝误杀
    check_stop_refuses_unrelated_pid(pkg)
    check_stop_rejects_incomplete_records(pkg)

    # ---- 第 1 轮：启动 → 导入 → 检索 → 无 Key 生成 ----
    rc1 = run_ps(pkg / "启动-RAG助手.ps1")
    check("验收·启动：启动脚本退出码 0 且两个服务就绪", rc1 == 0, f"rc={rc1}")

    if rc1 == 0:
        status, meta = http("GET", f"{API}/api/meta", origin=ORIGIN_OK)
        check("验收·meta：200、Key=false、我的资料库初始为空（available=false）",
              status == 200 and meta.get("api_key_configured") is False
              and meta.get("user_library", {}).get("available") is False, "")

        status, body = http("POST", f"{API}/api/user-documents", origin=ORIGIN_OK, payload={
            "items": [{"kind": "paste",
                       "text": "便携包验收资料：BM25 使用词频与逆文档频率对文档排序。",
                       "title": "验收笔记", "source_name": "打包验收"}]})
        check("验收·导入：粘贴文本导入成功",
              status == 200 and body["results"][0]["status"] == "imported", "")

        q = "BM25 怎么排序？"
        status, body = http("POST", f"{API}/api/query", origin=ORIGIN_OK, payload={
            "corpus_id": "user_library", "mode": "retrieve_only", "top_k": 3, "question": q})
        # 对照：在验收进程里注册一个指向解压目录的对照语料（同一切分逻辑 → 同 chunk id）
        rc.CORPORA["user_library_check"] = {
            "id": "user_library_check", "name": "打包验收对照", "note": "",
            "kind": "paragraph", "dir": pkg / "用户资料" / "documents",
            "glob": "*.txt", "manifest": None, "questions": None,
            "index": None, "report": None,
        }
        expect = rc.expand_and_assemble(q, top_k=3, corpus_id="user_library_check")
        check("验收·检索（我的资料库）：与直接调用 rag_core 逐 id 一致",
              status == 200
              and [h["id"] for h in body.get("hits", [])] == [h["id"] for h in expect["hits"]], "")

        status, body = http("POST", f"{API}/api/query", origin=ORIGIN_OK, payload={
            "corpus_id": "rag_learning", "mode": "retrieve_only", "top_k": 3,
            "question": "RAG 评估三元组包含哪三个维度？"})
        check("验收·检索（内置库）：rag_learning 正常返回真实命中",
              status == 200 and len(body.get("hits", [])) > 0
              and body["hits"][0]["id"].startswith("air-"), f"{body.get('hits', [{}])[0].get('id')}")

        status, body = http("POST", f"{API}/api/query", origin=ORIGIN_OK, payload={
            "corpus_id": "user_library", "mode": "generate", "top_k": 3, "question": q})
        check("验收·无 Key：generate 只落 no_api_key，answer 为空（零模型请求）",
              status == 200 and body.get("notice") == "no_api_key"
              and body.get("answer") is None
              and body.get("elapsed_ms", {}).get("generate") is None, "")

        status, page = http("GET", f"{WEB}/")
        check("验收·页面：5273 静态服务返回 api 版前端（含挂载点）",
              status == 200 and 'id="root"' in page, "")

        # ---- 停止：只停本包进程，端口释放 ----
        rc2 = run_ps(pkg / "停止-RAG助手.ps1")
        time.sleep(1.0)
        api_down = web_down = False
        try:
            http("GET", f"{API}/api/health", timeout=2)
        except Exception:                                          # noqa: BLE001
            api_down = True
        try:
            http("GET", f"{WEB}/", timeout=2)
        except Exception:                                          # noqa: BLE001
            web_down = True
        check("验收·停止：脚本退出码 0，API 与页面端口都已释放",
              rc2 == 0 and api_down and web_down, f"rc={rc2} api_down={api_down} web_down={web_down}")

        # ---- 第 2 轮：重启持久化 → 删除隔离 → 再停止 ----
        rc3 = run_ps(pkg / "启动-RAG助手.ps1")
        check("验收·重启：二次启动成功", rc3 == 0, f"rc={rc3}")
        if rc3 == 0:
            status, body = http("GET", f"{API}/api/user-documents", origin=ORIGIN_OK)
            docs = body.get("documents", [])
            check("验收·持久化：重启后已导入资料仍在（manifest + 正文 + 索引同目录保存）",
                  status == 200 and len(docs) == 1 and docs[0]["title"] == "验收笔记", "")

            status, body = http("POST", f"{API}/api/query", origin=ORIGIN_OK, payload={
                "corpus_id": "user_library", "mode": "retrieve_only", "top_k": 3, "question": q})
            check("验收·持久化：重启后我的资料库仍可检索",
                  status == 200 and len(body.get("hits", [])) > 0, "")

            mpath = REPO / "data" / "rag_learning" / "corpus_manifest.json"
            import hashlib
            before = hashlib.sha256(mpath.read_bytes()).hexdigest()
            status, body = http("DELETE",
                                f"{API}/api/user-documents/{urllib.parse.quote(docs[0]['id'])}",
                                origin=ORIGIN_OK, payload={"confirm": True})
            after = hashlib.sha256(mpath.read_bytes()).hexdigest()
            check("验收·删除隔离：confirm 删除我的资料库成功，且内置库 manifest 逐字节未动",
                  status == 200 and body.get("deleted") == docs[0]["id"]
                  and before == after, "")

            status, body = http("GET", f"{API}/api/user-documents", origin=ORIGIN_OK)
            check("验收·删除隔离：删除后我的资料库回到空库（available=false）",
                  status == 200 and body.get("available") is False, "")

            rc4 = run_ps(pkg / "停止-RAG助手.ps1")
            check("验收·收尾：二次停止成功", rc4 == 0, f"rc={rc4}")

    print("\n===== 汇总 =====", flush=True)
    print(f"PASS {len(PASSED)} / FAIL {len(FAILED)}", flush=True)
    if FAILED:
        for name in FAILED:
            print(f"  FAIL: {name}", flush=True)

    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "verify_portable_result.json").write_text(json.dumps({
            "pass": len(PASSED), "fail": len(FAILED), "failed": FAILED, "passed": PASSED,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.keep and not FAILED:
        shutil.rmtree(pkg, ignore_errors=True)
        print(f"[verify_portable] 临时解压目录已清理：{pkg}", flush=True)
    else:
        print(f"[verify_portable] 临时解压目录保留：{pkg}", flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        stop_pkg_quietly()
        raise
    sys.exit(code)
