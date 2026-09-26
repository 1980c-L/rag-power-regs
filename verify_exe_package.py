# -*- coding: utf-8 -*-
"""便携 EXE 验收（47 号方案阶段 C3 的验收清单）—— verify_exe_package.py

它验的是**发布包本身**，不是源码环境：
  1. 从 release ZIP 解压到全新临时目录（等价用户拿到压缩包解压）；
  2. 用**清掉 Python/Node 的 PATH**、没有 DEEPSEEK_API_KEY 的环境启动 EXE；
  3. 启动 / 页面 / 检索 / 无 Key 生成 / 批量提问 / 导出 DOCX+CSV / 用户资料导入；
  4. **进程身份伪造、残缺、过期时不会误杀无关进程**（用哨兵进程实证）；
  5. 停止后端口释放、重启后数据仍在、二次停止；
  6. 发布目录里不含验收脚本、运行痕迹与个人资料，且含 示例问题.csv（49 号 P2）；
  7. **安装位置不可写时 fail closed**（49 号 P2）：EXE 级用"同名文件占住 .run"制造写失败，
     源码级另测 `write_record()` 兜底分支 —— 两者都必须给中文提示、退出码 6、不留端口。

**全程零真实模型请求**：验收环境没有 Key，所有生成路径都落 `no_api_key`；
批量只用「仅检索」模式（它本来就不发模型请求）。
本文件内不写任何个人绝对路径。

运行：
    python verify_exe_package.py --zip <release/RAG问答助手-x.y.z.zip> [--out <证据目录>] [--keep]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import rag_core as rc                                             # noqa: E402
import batch_export as bx                                         # noqa: E402

EXE_NAME = "RAG问答助手.exe"
SAMPLE_NAME = "示例问题.csv"
API_PORT = 5280
WEB_PORT = 5273
GUARD_API_PORT = 5396          # 源码级"写记录失败"用例专用端口（避开真实运行端口）
GUARD_WEB_PORT = 5397
API = f"http://127.0.0.1:{API_PORT}"
WEB = f"http://127.0.0.1:{WEB_PORT}"
SENTINEL_KEY = "sk-EXE-SENTINEL-DO-NOT-LEAK-51af"

PASSED: list = []
FAILED: list = []
PROBES: dict = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def clean_env() -> dict:
    """**清掉 Python / Node** 的环境：验证 EXE 不依赖开发机运行时。

    保留 SystemRoot 等 Windows 必需变量（缺了它们连 socket 都起不来）；
    绝不带 DEEPSEEK_API_KEY（本轮验收要求"没有 Key 也能用"）。
    """
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return {
        "SystemRoot": root,
        "windir": root,
        "PATH": f"{root}\\System32;{root}",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": tempfile.gettempdir(),
        "TMP": tempfile.gettempdir(),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
    }


def http_json(method: str, url: str, payload=None, timeout: int = 60, origin: str = ""):
    """返回 (status, headers, body_text, body_bytes)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, dict(r.headers), raw.decode("utf-8", "replace"), raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), raw.decode("utf-8", "replace"), raw


def jbody(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def wait_http(url: str, tries: int = 120) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.25)
    return False


def port_listening(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port_free(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.25)
    return False


class ExeRunner:
    """启动/停止便携 EXE（干净环境 + 日志留证）。"""

    def __init__(self, exe_dir: Path, log_dir: Path):
        self.exe_dir = exe_dir
        self.exe = exe_dir / EXE_NAME
        self.log_dir = log_dir
        self.proc: subprocess.Popen | None = None

    def start(self, extra_args: list | None = None) -> subprocess.Popen:
        # 0.5.0：托盘图标由 verify_tray.py 专门验收；这里加 --no-tray，免得自动化跑几十次
        # 时在通知区域反复闪图标。**只加启动参数，不改任何断言。**
        args = [str(self.exe), "--no-browser", "--no-tray", *(extra_args or [])]
        handle = (self.log_dir / "exe_stdout.log").open("a", encoding="utf-8")
        self.proc = subprocess.Popen(args, cwd=str(self.exe_dir), env=clean_env(),
                                     stdout=handle, stderr=subprocess.STDOUT)
        return self.proc

    def run_cmd(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.exe), *args], cwd=str(self.exe_dir), env=clean_env(),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)


def unpack(zip_path: Path, work: Path) -> tuple:
    """解压 release ZIP 到全新目录（等价用户解压），并做一次包内容体检。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        zf.extractall(work)
    roots = {n.split("/", 1)[0] for n in names}
    root = work / sorted(roots)[0] if len(roots) == 1 else work
    forbidden = [n for n in names
                 if re.search(r"verify_.*\.py$|\.pid$|(^|/)output/|用户资料/|(^|/)logs/|(^|/)\.run/|__pycache__",
                              n)]
    return root, {"entries": len(names), "forbidden": forbidden}


ORIGIN = "http://127.0.0.1:5273"
Q_TRIAD = "RAG 评估三元组包含哪三个维度？"
Q_GROUND = "装设接地线的顺序是什么？"
Q_FLOW = "RAG 的基本流程是什么？"


def check_package_content(root: Path, info: dict) -> dict:
    import hashlib
    check("发布包：解压后含主程序与使用说明",
          (root / EXE_NAME).exists() and (root / "使用说明.txt").exists())
    check("发布包：不含验收脚本 / 进程记录 / 运行痕迹 / 个人资料 / 评测集",
          not info["forbidden"], str(info["forbidden"][:3]))
    check("发布包：自带 _internal 运行库（不依赖系统 Python）", (root / "_internal").is_dir())
    manifest_path = root / "构建清单.json"
    check("发布包：含构建清单（版本 / 工具链 / 哈希）", manifest_path.exists())
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check("发布包：清单写明未签名与工具链版本（可复核）",
          manifest.get("signed") is False and bool(manifest.get("pyinstaller"))
          and bool(manifest.get("python")) and manifest.get("version"))
    same = True
    checked = 0
    for entry in manifest.get("files", []):
        if entry["path"] in (EXE_NAME, "使用说明.txt", SAMPLE_NAME):
            real = hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
            same = same and real == entry["sha256"]
            checked += 1
    check("发布包：清单里的哈希与实际文件一致（抽查主程序 / 使用说明 / 示例问题）",
          same and checked == 3, f"抽查 {checked} 项")

    # 49 号 P2：47 号 §1 点名的 示例问题.csv 必须真的在包里，且能被「批量提问」直接导入
    sample = root / SAMPLE_NAME
    rows, header, questions = [], [], []
    if sample.exists():
        import csv
        rows = list(csv.reader(io.StringIO(sample.read_bytes().decode("utf-8-sig"))))
        header = [cell.strip().lower() for cell in rows[0]] if rows else []
        if "question" in header:
            col = header.index("question")
            questions = [(r[col].strip() if col < len(r) else "") for r in rows[1:]]
            questions = [q for q in questions if q]
    check("发布包：含 示例问题.csv（UTF-8 BOM + question 列 + 至少 3 条问题）",
          sample.exists() and sample.read_bytes().startswith(b"\xef\xbb\xbf")
          and "question" in header and len(questions) >= 3,
          f"header={header} 问题数={len(questions)}")
    check("发布包：示例问题全部非空且不超过服务端 500 字符上限（导入即用，不会被拒）",
          bool(questions) and all(0 < len(q) <= 500 for q in questions),
          f"{len(questions)} 条，最长 {max((len(q) for q in questions), default=0)} 字符")
    check("发布包：示例问题不引用「我的资料库」等用户产物（拿到的包是干净的）",
          all("用户资料" not in q for q in questions))
    PROBES["sample_csv"] = {"header": header, "questions": questions,
                            "sha256": hashlib.sha256(sample.read_bytes()).hexdigest()
                            if sample.exists() else ""}
    return manifest


def check_runtime(runner: ExeRunner, out_dir: Path) -> dict:
    runner.start()
    check("EXE·启动：本机 API 就绪（127.0.0.1:5280）", wait_http(API + "/api/health", 240))
    check("EXE·启动：页面服务就绪（127.0.0.1:5273）", wait_http(WEB + "/", 120))

    code, _, body, _ = http_json("GET", API + "/api/meta")
    meta = jbody(body)
    check("EXE·meta：data_source=api 且 Key 未配置（验收环境确实没有 Key）",
          code == 200 and meta.get("data_source") == "api"
          and meta.get("api_key_configured") is False)
    corpora = meta.get("corpora") or []
    check("EXE·meta：内置学习库统计与源码环境一致（语料真的打进包了）",
          bool(corpora) and corpora[0].get("chunk_count") == rc.corpus_stats(rc.RAG_LEARNING)["chunk_count"],
          f"exe={corpora[0].get('chunk_count') if corpora else None} "
          f"src={rc.corpus_stats(rc.RAG_LEARNING)['chunk_count']}")
    batch = meta.get("batch") or {}
    check("EXE·meta：批量能力上限来自服务端（1–20、串行、零重试）",
          batch.get("max_questions") == 20 and batch.get("concurrency") == 1
          and batch.get("auto_retry") == 0, str(batch)[:90])

    with urllib.request.urlopen(WEB + "/", timeout=10) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'src="/(assets/index-[^"]+\.js)"', html)
    check("EXE·页面：返回的是 api 版前端（含 assets/index-*.js）", bool(m))
    if m:
        _, _, _, js = http_json("GET", f"{WEB}/{m.group(1)}")
        check("EXE·页面：前端产物里含批量提问面板（发布包用的是本轮构建）",
              "批量提问" in js.decode("utf-8", "replace"))

    payload = {"corpus_id": rc.RAG_LEARNING, "mode": "retrieve_only", "top_k": 3,
               "question": Q_TRIAD}
    code, _, body, _ = http_json("POST", API + "/api/query", payload, origin=ORIGIN)
    res = jbody(body)
    exp = rc.expand_and_assemble(Q_TRIAD, top_k=3, corpus_id=rc.RAG_LEARNING)
    check("EXE·检索：命中 id 与顺序与源码环境逐字段一致",
          code == 200 and [h["id"] for h in res.get("hits", [])] == [e["id"] for e in exp["hits"]],
          f"exe={[h['id'] for h in res.get('hits', [])]}")
    check("EXE·检索：仅检索模式不生成（无回答、notice=retrieve_only）",
          res.get("answer") is None and res.get("notice") == "retrieve_only")

    code, _, body, _ = http_json("POST", API + "/api/query",
                                 {**payload, "mode": "generate"}, origin=ORIGIN)
    res = jbody(body)
    check("EXE·无 Key：生成请求落到 no_api_key，没有回答与引用（零模型请求）",
          res.get("notice") == "no_api_key" and res.get("answer") is None
          and res.get("refs") == [], f"notice={res.get('notice')}")

    # ---- 批量提问（仅检索：本来就零模型请求）----
    questions = [Q_TRIAD, Q_GROUND, Q_FLOW]
    code, _, body, _ = http_json("POST", API + "/api/batch-jobs",
                                 {"corpus_id": rc.RAG_LEARNING, "mode": "retrieve_only",
                                  "top_k": 3, "questions": questions, "confirm_calls": 0},
                                 origin=ORIGIN)
    job = jbody(body).get("job") or {}
    jid = job.get("job_id", "")
    check("EXE·批量：创建 3 条仅检索任务成功（计划模型请求 0）",
          code == 200 and bool(jid) and job.get("planned_calls") == 0, f"code={code}")
    final: dict = {}
    for _ in range(400):
        _, _, body, _ = http_json("GET", f"{API}/api/batch-jobs/{jid}")
        final = jbody(body).get("job") or {}
        if final.get("state") in ("COMPLETED", "CANCELLED", "STOPPED_ON_ERROR"):
            break
        time.sleep(0.2)
    check("EXE·批量：跑完且严格保持输入顺序、逐条完成",
          final.get("state") == "COMPLETED"
          and [i.get("question") for i in final.get("items", [])] == questions
          and all(i.get("status") == "done" for i in final.get("items", [])),
          f"state={final.get('state')}")
    check("EXE·批量：仅检索模式实际模型请求为 0、零重试",
          final.get("started_calls") == 0 and final.get("retries") == 0)
    same_hits = True
    for item in final.get("items", []):
        exp2 = rc.expand_and_assemble(item["question"], top_k=3, corpus_id=rc.RAG_LEARNING)
        same_hits = same_hits and [(h["id"], h.get("text")) for h in item.get("hits", [])] == \
            [(e["id"], e["text"]) for e in exp2["hits"]]
    check("EXE·批量：每条的命中（含原文）与源码环境逐字段一致", same_hits)

    code, _, _, raw_csv = http_json("GET", f"{API}/api/batch-jobs/{jid}/export?format=csv")
    text_csv = raw_csv.decode("utf-8-sig")
    header_csv = text_csv.splitlines()[0]
    check("EXE·导出：CSV 成功（200/BOM/表头 15 列含 hit_* 命中证据列/行数=题目数+1）",
          code == 200 and raw_csv.startswith(b"\xef\xbb\xbf")
          and tuple(header_csv.split(",")) == bx.CSV_HEADERS
          and len(text_csv.strip().splitlines()) == len(questions) + 1,
          f"lines={len(text_csv.strip().splitlines())}")
    code, _, _, raw_docx = http_json("GET", f"{API}/api/batch-jobs/{jid}/export?format=docx")
    check("EXE·导出：DOCX 成功且是合法 docx（PK 头）",
          code == 200 and raw_docx[:2] == b"PK", f"code={code}")
    from docx import Document
    docx_text = "\n".join(p.text for p in Document(io.BytesIO(raw_docx)).paragraphs)
    check("EXE·导出：DOCX 含边界说明与逐题问题（导出的是本次记录）",
          bx.DOCX_BOUNDARY in docx_text and Q_TRIAD in docx_text)
    hit0 = (final.get("items") or [{}])[0].get("hits") or []
    probe = "".join(((hit0[0].get("text") if hit0 else "") or "").split())[:30]
    check("EXE·导出：DOCX 里真的有命中原文（报告能自证「命中了什么」）",
          bool(probe) and probe in "".join(docx_text.split()),
          f"探针={probe[:16]}")
    (out_dir / "exe_batch_report.csv").write_bytes(raw_csv)
    (out_dir / "exe_batch_report.docx").write_bytes(raw_docx)

    # ---- 我的资料库：导入 → 检索 → 数据落在 EXE 同级 ----
    code, _, body, _ = http_json("POST", API + "/api/user-documents",
                                 {"items": [{"kind": "paste", "title": "便携版导入测试",
                                             "text": "便携版用户资料正文：装设接地线必须先验电。"}]},
                                 origin=ORIGIN)
    lib = jbody(body).get("library") or {}
    check("EXE·用户库：粘贴导入成功（来源数 1）",
          code == 200 and lib.get("source_count") == 1, f"code={code}")
    code, _, body, _ = http_json("POST", API + "/api/query",
                                 {"corpus_id": "user_library", "mode": "retrieve_only",
                                  "top_k": 2, "question": "接地线"}, origin=ORIGIN)
    check("EXE·用户库：导入后可以检索到",
          code == 200 and len(jbody(body).get("hits") or []) >= 1)
    check("EXE·数据位置：用户资料目录在 EXE 同级（便携语义：数据跟着文件夹走）",
          (runner.exe_dir / "用户资料").is_dir())
    return final


def check_identity_defense(runner: ExeRunner) -> None:
    """伪造 / 残缺 / 过期的启动记录都不能让别的进程被停止。

    做法是拿一个**哨兵进程**（一个与本助手毫无关系的长跑 python）当靶子：
    把它的 PID 写进伪造记录，然后运行 `--stop` —— 正确实现必须拒绝，且哨兵还活着。
    """
    run_dir = runner.exe_dir / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)
    record_path = run_dir / "launcher.json"
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(240)"])
    try:
        record_path.write_text(json.dumps({
            "marker": "rag-power-regs-launcher", "version": "0.4.0", "pid": sentinel.pid,
            "token": "0" * 32, "api_port": API_PORT, "web_port": WEB_PORT,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "exe": str(runner.exe),
        }, ensure_ascii=False), encoding="utf-8")
        proc = runner.run_cmd(["--stop"])
        check("EXE·身份防护：伪造记录不会让任何进程被停止（明确拒绝）",
              proc.returncode != 0, f"rc={proc.returncode} out={(proc.stdout or '').strip()[:70]}")
        check("EXE·身份防护：伪造记录指向的哨兵进程仍然活着（没有被误杀）",
              sentinel.poll() is None, f"poll={sentinel.poll()}")

        record_path.write_text(json.dumps(
            {"marker": "rag-power-regs-launcher", "pid": sentinel.pid,
             "api_port": API_PORT, "web_port": WEB_PORT}, ensure_ascii=False), encoding="utf-8")
        proc = runner.run_cmd(["--stop"])
        check("EXE·身份防护：残缺记录同样被拒绝（不猜、不杀）",
              proc.returncode != 0 and sentinel.poll() is None, f"rc={proc.returncode}")

        ghost = subprocess.Popen([sys.executable, "-c", "pass"])
        ghost.wait(timeout=30)
        record_path.write_text(json.dumps({
            "marker": "rag-power-regs-launcher", "version": "0.4.0", "pid": ghost.pid,
            "token": "a" * 32, "api_port": API_PORT, "web_port": WEB_PORT,
            "started_at": "2020-01-01 00:00:00", "exe": str(runner.exe),
        }, ensure_ascii=False), encoding="utf-8")
        proc = runner.run_cmd(["--stop"])
        check("EXE·身份防护：过期记录被拒绝（不会顺着复用该 PID 的进程去杀）",
              proc.returncode != 0 and sentinel.poll() is None, f"rc={proc.returncode}")
    finally:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        if sentinel.poll() is None:
            sentinel.kill()
            sentinel.wait(timeout=15)


def _run_until_exit(exe: Path, args: list, cwd: Path, timeout: float = 60.0) -> tuple:
    """跑一条**应当自己退出**的命令：返回 (退出码 or None, 输出)。

    用 Popen + communicate(timeout) 而不是 subprocess.run 的理由：如果 fail-closed 没生效，
    这个进程会变成长期运行的服务，subprocess.run 会抛 TimeoutExpired（把"FAIL"变成"脚本崩"）。
    这里超时只代表"它没有自己退出"，交给判据去报 FAIL，脚本本身继续跑完。
    """
    proc = subprocess.Popen([str(exe), *args], cwd=str(cwd), env=clean_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return None, out or ""


def check_readonly_install(runner: ExeRunner, out_dir: Path) -> None:
    """49 号 P2（EXE 级）：安装位置不可写 → 启动前 fail closed，可理解提示，不留服务/端口。

    制造"不可写"的方式：把 `.run` 换成**同名文件** —— 于是 `RUN_DIR.mkdir()` 必然抛
    OSError（FileExistsError），与权限被拒（PermissionError）在代码路径上等价，
    但不需要改权限、不需要管理员、也不会污染其它目录。

    末尾带一个对照组：移除该障碍后能正常启动并正常停止 —— 证明上面的失败确实是这个
    障碍引起的，而不是"本来就起不来"。
    """
    run_dir = runner.exe_dir / ".run"
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
    elif run_dir.exists():
        run_dir.unlink()
    run_dir.write_text("占位文件：故意让 .run 目录无法创建（模拟安装位置不可写）\n",
                       encoding="utf-8")
    try:
        rc, out = _run_until_exit(runner.exe, ["--no-browser"], runner.exe_dir)
        (out_dir / "readonly_install_stdout.log").write_text(out, encoding="utf-8")
        check("EXE·只读位置：启动前 fail closed，退出码明确（6；不是崩溃、也不是挂住）",
              rc == 6, f"rc={rc}")
        check("EXE·只读位置：给出普通用户看得懂的中文提示（不是 Python traceback）",
              "启动失败" in out and "不可写" in out
              and "Traceback (most recent call last)" not in out,
              (out.strip().splitlines() or [""])[0][:80])
        check("EXE·只读位置：如实说明本次没有启动服务、没有绑定端口",
              "没有启动任何服务" in out)
        check("EXE·只读位置：失败后没有留下监听端口",
              not port_listening(API_PORT) and not port_listening(WEB_PORT))
    finally:
        try:
            run_dir.unlink()
        except FileNotFoundError:
            pass

    # 对照组：排除该障碍后一切照常
    runner.start()
    started = wait_http(API + "/api/health", 240)
    check("EXE·只读位置（对照）：移除障碍后可以正常启动", started)
    stop = runner.run_cmd(["--stop"])
    check("EXE·只读位置（对照）：可以正常停止且端口释放",
          stop.returncode == 0 and wait_port_free(API_PORT) and wait_port_free(WEB_PORT),
          f"rc={stop.returncode}")


def check_launcher_write_record_guard(out_dir: Path) -> None:
    """49 号 P2（源码级）：预检通过、但真正写进程记录时失败 —— 同样 fail closed 并收尾。

    为什么要单独测这一支：EXE 级用例会被启动前的可写性预检挡在更前面，走不到
    `write_record()` 的兜底分支。这里把预检替换成"通过"（只测兜底），使用可写临时目录，
    并在临时记录已经写成后让 `os.replace()` 定向失败，再在同一进程里调用 launcher.main()：

      - 判据 1：不把异常抛给用户/调用方（退出码 6，而不是 traceback）；
      - 判据 2：刚启动的两个服务被停掉、端口被释放（不留半启动实例）；
      - 判据 3：失败确实发生在临时记录写成之后；
      - 判据 4：最终记录和含令牌的临时记录都没有留下。
    """
    import contextlib

    sys.path.insert(0, str(REPO))
    import launcher

    work = Path(tempfile.mkdtemp(prefix="rag_launcher_guard_"))
    saved = {"RUN_DIR": launcher.RUN_DIR, "RECORD_PATH": launcher.RECORD_PATH,
             "check": launcher.check_data_dirs, "replace": launcher.os.replace}
    launcher.RUN_DIR = work / ".run"
    launcher.RECORD_PATH = launcher.RUN_DIR / "launcher.json"
    guarded_record = launcher.RECORD_PATH
    guarded_tmp = guarded_record.with_suffix(".json.tmp")
    replace_observation = {"written": False, "has_token": False}

    def fail_after_temp_write(src, _dst):
        src = Path(src)
        replace_observation["written"] = src.is_file()
        if src.is_file():
            payload = json.loads(src.read_text(encoding="utf-8"))
            replace_observation["has_token"] = bool(payload.get("token"))
        raise PermissionError("simulated atomic replace failure")

    launcher.check_data_dirs = lambda: []          # 只测兜底分支（预检已由 EXE 级用例覆盖）
    launcher.os.replace = fail_after_temp_write
    buf = io.StringIO()
    rc, raised = None, ""
    try:
        with contextlib.redirect_stdout(buf):
            rc = launcher.main(["--no-browser", "--api-port", str(GUARD_API_PORT),
                                "--web-port", str(GUARD_WEB_PORT)])
    except BaseException as exc:                                       # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    finally:
        launcher.RUN_DIR = saved["RUN_DIR"]
        launcher.RECORD_PATH = saved["RECORD_PATH"]
        launcher.check_data_dirs = saved["check"]
        launcher.os.replace = saved["replace"]
    out = buf.getvalue()
    (out_dir / "launcher_write_record_guard.log").write_text(out, encoding="utf-8")
    check("启动器·源码：写记录失败时把失败交给调用方（退出码 6，不抛异常）",
          rc == 6 and not raised, f"rc={rc} raised={raised[:60]}")
    check("启动器·源码：失败提示可理解且不含 traceback",
          "启动失败" in out and "无法写入运行状态文件" in out
          and "Traceback (most recent call last)" not in out,
          (out.strip().splitlines() or [""])[0][:80])
    check("启动器·源码：收尾完成（刚启动的两个端口都已释放，不留半启动实例）",
          not port_listening(GUARD_API_PORT) and not port_listening(GUARD_WEB_PORT),
          f"{GUARD_API_PORT}={port_listening(GUARD_API_PORT)} "
          f"{GUARD_WEB_PORT}={port_listening(GUARD_WEB_PORT)}")
    check("启动器·源码：反例确实在临时记录写成且含令牌后触发原子替换失败",
          replace_observation["written"] and replace_observation["has_token"],
          json.dumps(replace_observation, ensure_ascii=False))
    check("启动器·源码：没有留下运行记录文件",
          not guarded_record.exists(), str(guarded_record))
    check("启动器·源码：没有留下含令牌的临时运行记录",
          not guarded_tmp.exists(), str(guarded_tmp))
    shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="便携 EXE 验收（47 号方案阶段 C3）")
    ap.add_argument("--zip", required=True, help="release 目录里的 ZIP")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true", help="保留解压目录（默认删除）")
    args = ap.parse_args()

    zip_path = Path(args.zip)
    if not zip_path.exists():
        raise SystemExit(f"找不到发布 ZIP：{zip_path}")
    work = Path(tempfile.mkdtemp(prefix="rag_exe_verify_"))
    out_dir = Path(args.out) if args.out else (work / "evidence")
    out_dir.mkdir(parents=True, exist_ok=True)

    check("发布包：ZIP 存在且结构完整（能被 zipfile 打开）", zipfile.is_zipfile(zip_path))
    root, info = unpack(zip_path, work)
    PROBES["unpack"] = {**info, "root": str(root)}
    manifest = check_package_content(root, info)

    runner = ExeRunner(root, out_dir)
    jid = ""
    try:
        final = check_runtime(runner, out_dir)
        jid = final.get("job_id", "")
        check("EXE·运行：进程是单进程托管（没有派生子进程：退出即释放端口）",
              _child_process_count(runner.proc.pid) == 0 if runner.proc else False,
              f"children={_child_process_count(runner.proc.pid) if runner.proc else 'n/a'}")

        check_identity_defense(runner)
    finally:
        runner.kill()
    check("EXE·停止：强制结束验收实例后两个端口都已释放（不留监听）",
          wait_port_free(API_PORT) and wait_port_free(WEB_PORT))

    # ---- 重启：数据仍在、批量历史不续跑 ----
    runner.start()
    check("EXE·重启：服务再次就绪", wait_http(API + "/api/health", 240))
    _, _, body, _ = http_json("GET", API + "/api/meta")
    lib = (jbody(body).get("user_library") or {})
    check("EXE·重启：用户资料仍在（数据落在 EXE 同级，重启不丢）",
          lib.get("source_count") == 1, f"source_count={lib.get('source_count')}")
    code, _, body, _ = http_json("GET", f"{API}/api/batch-jobs/{jid}")
    check("EXE·重启：旧批量任务不再可见（第一版不续跑、也不自动重放）",
          code == 404 and jbody(body).get("error", {}).get("code") == "unknown_job",
          f"code={code}")

    stop = runner.run_cmd(["--stop"])
    check("EXE·停止：--stop 正常退出（rc=0）", stop.returncode == 0,
          f"rc={stop.returncode} out={(stop.stdout or '').strip()[:70]}")
    check("EXE·停止：两个端口都已释放", wait_port_free(API_PORT) and wait_port_free(WEB_PORT))
    again = runner.run_cmd(["--stop"])
    check("EXE·停止：再次 --stop 如实报告「没有可停止的实例」（不是假成功）",
          again.returncode != 0, f"rc={again.returncode}")

    # ---- 49 号 P2：只读位置 fail closed（EXE 级 + 源码级兜底分支）----
    check_readonly_install(runner, out_dir)
    check_launcher_write_record_guard(out_dir)

    result = {"service": "exe_package", "passed": len(PASSED), "failed": len(FAILED),
              "passed_names": PASSED, "failed_names": FAILED, "probes": PROBES,
              "manifest": {k: manifest.get(k) for k in ("app", "version", "python",
                                                        "pyinstaller", "node", "file_count",
                                                        "total_bytes", "signed")} if manifest else {}}
    (out_dir / "exe_verify_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    print("\n" + "=" * 70)
    if FAILED:
        print("FAIL 详情：")
        for name in FAILED:
            print(f"  - {name}")
    print(f"便携 EXE 验收：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    print(f"证据写入：{out_dir}")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
        print(f"[verify_exe_package] 解压目录已清理：{work}")
    return 1 if FAILED else 0


def _child_process_count(pid: int) -> int:
    """当前进程的直接子进程数（用系统 PowerShell 查，不引入新依赖）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"@(Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\").Count"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return int((out.stdout or "0").strip() or "0")
    except Exception:                                              # noqa: BLE001
        return -1


if __name__ == "__main__":
    sys.exit(main())

