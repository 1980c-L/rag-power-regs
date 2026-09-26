# -*- coding: utf-8 -*-
"""便携 EXE 构建脚本 —— build_release.py（47 号方案阶段 C3）

一条命令产出可分发目录 + ZIP，并且把"能不能发"写成可复核的判据：

    1. **构建前预扫**（fail closed）：将要打包的源码/前端产物里不许出现 Key 形态、
       不许出现开发机绝对路径 —— 命中就拒绝构建（EXE 里的字符串事后没法靠扫目录发现，
       所以必须在这一步拦住）；
    2. **不覆盖旧版本**：目标目录已存在就拒绝，除非显式 --overwrite（发布包不做静默覆盖）；
    3. PyInstaller onedir（不用 onefile：onefile 每次启动都要解压，慢且杀软更敏感），
       主启动器 `launcher.py` → `RAG问答助手.exe`；
    4. 组装发布目录 + `使用说明.txt` + `构建清单.json`；
    5. 对**成品目录**再做一遍隐私与内容扫描（该带的带齐、不该带的一律不许出现）；
    6. 生成 ZIP + SHA-256 清单，打印摘要。

产物：
    release/RAG问答助手-<version>/        ← 直接双击 RAG问答助手.exe
    release/RAG问答助手-<version>.zip
    release/RAG问答助手-<version>-清单.json

用法（**务必带 `-X utf8`**：中文 Windows 的默认编码不是 UTF-8，会让子进程的文本管道
按 GBK 解码，遇到非 GBK 字节就在读取线程里抛 UnicodeDecodeError —— 不影响产物，
但会往构建日志里塞无关的 traceback，干扰复核；本轮真踩过）：
    python -X utf8 build_release.py                 # 正常构建
    python -X utf8 build_release.py --overwrite     # 覆盖同名旧目录（明确要求时才用）
    python -X utf8 build_release.py --skip-scan     # 仅排查构建问题时使用（默认必须扫描）
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
FRONTEND_DIST = REPO / "frontend_v3" / "dist-api"
DATA_DIR = REPO / "data"
BUILD_ROOT = REPO / "build_exe"
RELEASE_ROOT = REPO / "release"
APP_NAME = "RAG问答助手"
VERSION = "0.5.2"          # 与 launcher.py 的 VERSION 保持一致
EXE_NAME = f"{APP_NAME}.exe"
# 0.5.0：EXE 图标 = 系统托盘图标。源文件 assets/icon.svg，用 tools/make_icon.py 生成多尺寸 .ico。
ICON_PATH = REPO / "assets" / "rag-assistant.ico"

# 与 verify_flows.py 的 S10 同一口径：Key 形态与个人绝对路径
SECRET_RES = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似真实 API key"),
    (re.compile(r"api[_-]?key\s*[:=]\s*[\"'][^\"']{8,}"), "硬编码 key 赋值"),
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]{1,2}(Users|codebuddy|CodeBuddy|AppData|Desktop|Documents)"),
     "个人绝对路径"),
]
SCAN_SUFFIXES = {".py", ".md", ".txt", ".json", ".ps1", ".cmd", ".bat", ".yml", ".yaml",
                 ".toml", ".js", ".css", ".html", ".map", ".csv"}
# 发布包必须排除的东西：测试/证据/运行痕迹/个人资料
FORBIDDEN_IN_RELEASE = [
    (re.compile(r"verify_.*\.py$"), "验收脚本不得进发布包"),
    (re.compile(r"\.pid$"), "进程身份记录不得进发布包"),
    (re.compile(r"(^|[\\/])output([\\/]|$)"), "评测输出不得进发布包"),
    (re.compile(r"(^|[\\/])用户资料([\\/]|$)"), "个人资料不得进发布包"),
    (re.compile(r"(^|[\\/])logs([\\/]|$)"), "运行日志不得进发布包"),
    (re.compile(r"(^|[\\/])\.run([\\/]|$)"), "运行状态目录不得进发布包"),
    (re.compile(r"__pycache__"), "缓存目录不得进发布包"),
    (re.compile(r"(^|[\\/])eval([\\/]|$)"), "评测集不得进发布包"),
]
PRE_SCAN_PATHS = ["launcher.py", "tray.py", "api_v3.py", "rag_core.py", "batch_jobs.py",
                  "batch_export.py", "user_library.py", "config.py",
                  "requirements-api.txt", "frontend_v3/dist-api"]

# 49 号 P2：47 号 §1 的交付物清单里点名的示例问题 CSV（必须随包发出去）
SAMPLE_CSV_NAME = "示例问题.csv"
SAMPLE_CSV = REPO / SAMPLE_CSV_NAME
SAMPLE_CSV_MIN_QUESTIONS = 3


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def scan_text_file(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:                                              # noqa: BLE001
        return []
    return [reason for pattern, reason in SECRET_RES if pattern.search(text)]


# ---------------- 1. 构建前预扫 ----------------
def pre_scan() -> list:
    findings: list = []
    for rel in PRE_SCAN_PATHS:
        target = REPO / rel
        if not target.exists():
            findings.append(f"{rel}: 缺失（构建前置条件不满足）")
            continue
        files = [target] if target.is_file() else list(iter_files(target))
        for fp in files:
            if fp.suffix.lower() not in SCAN_SUFFIXES:
                continue
            for reason in scan_text_file(fp):
                findings.append(f"{fp.relative_to(REPO).as_posix()}: {reason}")
    return findings


# ---------------- 2. 成品目录扫描 ----------------
def release_scan(root: Path) -> tuple:
    privacy: list = []
    content: list = []
    for fp in iter_files(root):
        rel = fp.relative_to(root).as_posix()
        for pattern, reason in FORBIDDEN_IN_RELEASE:
            if pattern.search(rel):
                content.append(f"{rel}: {reason}")
        if fp.suffix.lower() in SCAN_SUFFIXES:
            for reason in scan_text_file(fp):
                privacy.append(f"{rel}: {reason}")
    return privacy, content


# ---------------- 2b. 示例问题 CSV 校验（49 号 P2） ----------------
def validate_sample_csv(path: Path) -> list:
    """示例问题 CSV 的判据（构建前后各查一次，用的就是这个函数）。

    判据来自 47 号 §1 与 49 号 §P2：**UTF-8 BOM + 必须有 question 列 + 至少 3 条**，
    且每条非空、不超过服务端 500 字符上限（否则用户一导入就被拒，等于没带）。
    """
    import csv
    problems: list = []
    if not path.exists():
        return [f"{path.name}: 缺失（47 号 §1 点名的交付物，必须随包发出）"]
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        problems.append(f"{path.name}: 没有 UTF-8 BOM（Excel 打开会乱码）")
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    except Exception as exc:                                       # noqa: BLE001
        return [f"{path.name}: 无法按 CSV 解析（{type(exc).__name__}）"]
    if not rows:
        return [f"{path.name}: 空文件"]
    header = [cell.strip().lower() for cell in rows[0]]
    if "question" not in header:
        problems.append(f"{path.name}: 表头缺少 question 列（当前 {rows[0]}）")
        return problems
    col = header.index("question")
    questions = [(r[col].strip() if col < len(r) else "") for r in rows[1:]]
    questions = [q for q in questions if q]
    if len(questions) < SAMPLE_CSV_MIN_QUESTIONS:
        problems.append(f"{path.name}: 只有 {len(questions)} 条问题"
                        f"（至少 {SAMPLE_CSV_MIN_QUESTIONS} 条）")
    if len(questions) > 20:
        problems.append(f"{path.name}: {len(questions)} 条超过服务端 20 条上限")
    too_long = [q for q in questions if len(q) > 500]
    if too_long:
        problems.append(f"{path.name}: {len(too_long)} 条超过 500 字符上限")
    return problems


# ---------------- 3. PyInstaller ----------------
def run_pyinstaller(overwrite: bool) -> tuple:
    dist_dir = BUILD_ROOT / "dist"
    work_dir = BUILD_ROOT / "work"
    spec_dir = BUILD_ROOT
    if BUILD_ROOT.exists():
        if not overwrite:
            raise SystemExit(f"构建目录已存在：{BUILD_ROOT}\n"
                             f"（发布包不做静默覆盖；确认要重来请加 --overwrite）")
        shutil.rmtree(BUILD_ROOT, ignore_errors=True)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", APP_NAME,
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(spec_dir),
        "--add-data", f"{FRONTEND_DIST}{os.pathsep}webapp",
        "--add-data", f"{DATA_DIR}{os.pathsep}data",
        "--add-data", f"{ICON_PATH}{os.pathsep}assets",   # 托盘图标的兜底来源
        "--icon", str(ICON_PATH),                         # EXE 图标（托盘优先从 EXE 取）
        "--collect-data", "jieba",          # jieba 的 dict.txt 必须一起打包
        "--collect-data", "docx",           # python-docx 的默认模板
        "--hidden-import", "docx",
        "launcher.py",
    ]
    log_path = BUILD_ROOT / "pyinstaller.log"
    interesting = ("Analyzing", "Processing", "Building", "Appending", "Copying",
                   "WARNING", "ERROR", "Traceback")
    # 让 PyInstaller 自己（以及它派生的子进程）都用 UTF-8 读写：
    # 否则在中文 Windows 上，它内部按 GBK 解码子进程输出会抛 UnicodeDecodeError，
    # 虽然不影响产物，但会往日志里塞无意义的 traceback，干扰复核。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with log_path.open("w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        # 直接让孩子写文件句柄：**不建 PIPE、不建 reader 线程**。
        # 中文 Windows 上，文本模式 PIPE 的读取线程按 GBK 解码子进程输出，
        # 一旦遇到非 GBK 字节就在 reader 线程里抛 UnicodeDecodeError（不影响产物，
        # 但会往构建日志里塞无意义的 traceback，干扰复核）。
        proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=fh,
                                stderr=subprocess.STDOUT)
        watchdog_stop = threading.Event()

        def follow_progress() -> None:
            """进度可见：跟随日志文件（读的是我们自己的 UTF-8 文件，不碰子进程管道）。"""
            pos = 0
            while not watchdog_stop.is_set():
                try:
                    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(pos)
                        for line in handle:
                            if line.startswith(interesting):
                                print("       " + line.rstrip(), flush=True)
                        pos = handle.tell()
                except Exception:                                  # noqa: BLE001
                    pass
                time.sleep(0.5)

        watcher = threading.Thread(target=follow_progress, daemon=True)
        watcher.start()
        rc = proc.wait()
        watchdog_stop.set()
        watcher.join(timeout=3)
    return rc, log_path, dist_dir / APP_NAME


# ---------------- 4. 组装 ----------------
README = """RAG 问答助手（便携版 {version}）
=====================================

【怎么用】
1. 双击 RAG问答助手.exe。程序在后台启动本机服务（127.0.0.1）并自动打开一个应用窗口；
   屏幕右下角通知区域会出现一个蓝色图标 —— 那就是本程序，它没有终端窗口。
2. 退出：右键那个图标 →「退出」。也可以在命令行运行：
       RAG问答助手.exe --stop
   查看状态：RAG问答助手.exe --status
   右键菜单里还有：打开页面 / 复制页面地址 / 打开资料文件夹。
3. 关掉浏览器里的页面**不会**停止服务（服务仍在后台运行）；双击托盘图标可以重新打开页面。
   排障时想看到实时输出，用：RAG问答助手.exe --keep-console

【需要的运行环境】
- Windows 10/11 64 位。**不需要安装 Python 或 Node**，全部依赖都在 _internal 目录里。
- 没有任何联网依赖：不配置 API Key 也能正常使用检索功能。

【关于 API Key（可选）】
- 只有「检索并生成」这一步需要真实模型，需要在系统里设置环境变量：
      setx DEEPSEEK_API_KEY "你的key"
  设置后重新启动本程序生效。程序只读取它，不显示、不写进任何文件。
- 没有 Key 时：检索照常、批量提问的「仅检索」模式照常；需要生成时会如实提示"未检测到 Key"。

【能做什么】
- 单题：检索（BM25 关键词检索）或检索 + 生成，结果里给出真实命中片段与引用对照。
- 我的资料库：导入 .txt/.md 或粘贴文本（存在 用户资料\\，与内置学习库物理分开）。
- **批量提问**：一次 1–20 道问题，串行执行、每题最多 1 次模型请求、**不自动重试**；
  遇到认证/网络/服务端错误会停止启动后续问题；可以随时停止（当前这条跑完就停）。
- **导出报告**：DOCX（可读报告）与 CSV（明细）。导出只读取已保存的结果，不会重新检索或重新生成。

【文件在哪里】
    用户资料\\    你导入的资料（删掉它等于清空"我的资料库"）
    .run\\        进程记录与批量任务状态快照（可随时删除；删除后历史任务不再显示）
    logs\\        启动日志

【示例问题.csv】
- 根目录的 示例问题.csv 可以直接在「批量提问」里导入（UTF-8 BOM，带 question 列）。
- 里面是 10 道来自内置学习库与电力示例库的问题，用来自动演示"一次问多道"与报告导出；
  不含任何个人资料。

【必须知道的边界】
- 生成层尚未完成正式验证：离线验收里「无依据不伪造」是**尚未验证（NOT VALIDATED）**，
  目前只有单轮 8 例用户侧样本；回答仅供参考。
- Hit@K / MRR 是**检索命中率**，只适用于内置学习库，不是答案正确率；「我的资料库」不继承该指标。
- 检索只有 BM25 关键词检索：没有向量检索、混合检索、reranker、多轮对话。
- 电力示例库是**示例资料，不是正式规程**，不能用于现场作业或安全决策。
- 学习库资料来自 Datawhale《All-in-RAG》，许可 CC BY-NC-SA 4.0（署名 · 非商业 · 相同方式共享）。
- 本程序只绑定 127.0.0.1（本机回环），不做鉴权、不面向公网；请勿部署到公网。

【已知限制】
- 本程序所在目录**必须可写**：运行状态、日志和你的资料都存在它旁边。若解压到
  C:\\Program Files 等只读位置，启动时会明确提示并直接退出（退出码 6），不会半启动。
- 本程序**没有终端窗口**，平时在通知区域常驻。万一托盘图标没能注册（被安全策略拦、
  通知区域被禁用等），启动时会弹窗告诉你，退出请用 RAG问答助手.exe --stop。
- 双击启动的**一瞬间**可能闪一下终端窗口（Windows 先分配控制台、程序随后脱离它）。
  窗口会自动消失，不是报错，也不影响后台服务。
- 可执行文件**未做代码签名**：Windows SmartScreen 可能提示"未知发布者"，选择"仍要运行"即可。
- 首次启动（Windows Defender 扫描新程序）可能需要几秒钟。
"""


def copy_dist(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


def write_manifest(root: Path, info: dict) -> dict:
    files = []
    total = 0
    for fp in iter_files(root):
        if fp.name == "构建清单.json":
            continue
        size = fp.stat().st_size
        total += size
        files.append({"path": fp.relative_to(root).as_posix(),
                      "size": size, "sha256": sha256_of(fp)})
    manifest = {
        "app": APP_NAME,
        "version": VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "built_on": info.get("built_on", ""),
        "python": sys.version.split()[0],
        "pyinstaller": info.get("pyinstaller", ""),
        "node": info.get("node", ""),
        "frontend_build": info.get("frontend_build", {}),
        "file_count": len(files),
        "total_bytes": total,
        "signed": False,
        "signing_note": "未做代码签名：SmartScreen 可能提示未知发布者（47 号方案不授权签名）。",
        "files": files,
    }
    (root / "构建清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return manifest


def build_zip(root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in iter_files(root):
            zf.write(fp, arcname=f"{root.name}/{fp.relative_to(root).as_posix()}")


def toolchain_info() -> dict:
    import platform
    node = ""
    exe = shutil.which("node")
    if exe:
        try:
            node = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=30).stdout.strip()
        except Exception:                                          # noqa: BLE001
            node = "unknown"
    try:
        import PyInstaller
        pyinstaller = PyInstaller.__version__
    except Exception:                                              # noqa: BLE001
        pyinstaller = "unknown"
    return {"built_on": platform.platform(), "pyinstaller": pyinstaller, "node": node}


def frontend_build_info() -> dict:
    """dist-api 的实际指纹：证明发布包里的前端确实是本轮的 api 构建。"""
    assets = sorted(p for p in (FRONTEND_DIST / "assets").glob("*")) if FRONTEND_DIST.exists() else []
    blob = ""
    for path in assets:
        if path.suffix == ".js":
            blob += path.read_text(encoding="utf-8", errors="replace")
    return {
        "assets": [p.name for p in assets],
        "has_batch_panel": "批量提问" in blob,
        "api_marker": "已接入本机 API" in blob,
        "mock_marker_present": "第一阶段：本地示例数据" in blob,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="便携 EXE 构建（47 号方案阶段 C3）")
    ap.add_argument("--overwrite", action="store_true", help="覆盖同名发布目录与构建目录")
    ap.add_argument("--skip-scan", action="store_true", help="跳过构建前预扫（仅排查用）")
    args = ap.parse_args()

    if not sys.flags.utf8_mode:
        print("[构建] 提示：当前不是 UTF-8 模式（建议用 python -X utf8 运行）："
              "子进程文本输出会按系统编码解码，可能在日志里留下无关的 traceback。")

    print("[构建] 1/6 前置检查")
    if not FRONTEND_DIST.exists():
        raise SystemExit(f"缺少前端产物：{FRONTEND_DIST}（先构建 api 版并同步到 dist-api）")
    if not ICON_PATH.exists():
        raise SystemExit(f"缺少图标：{ICON_PATH}（先跑 python -X utf8 tools/make_icon.py）")
    fb = frontend_build_info()
    if not (fb["has_batch_panel"] and fb["api_marker"]) or fb["mock_marker_present"]:
        raise SystemExit(f"dist-api 不是本轮的 api 构建，拒绝打包：{fb}")
    for mod in ("jieba", "requests", "docx", "PyInstaller"):
        try:
            __import__(mod)
        except ImportError:
            raise SystemExit(f"缺少构建依赖：{mod}（见 requirements-build.txt）")
    print(f"        前端产物：{fb['assets']}；含批量面板={fb['has_batch_panel']}")
    csv_problems = validate_sample_csv(SAMPLE_CSV)
    if csv_problems:
        print("       示例问题 CSV 不合格，拒绝构建：")
        for item in csv_problems:
            print(f"         - {item}")
        return 2
    print(f"        示例问题 CSV：{SAMPLE_CSV_NAME}（UTF-8 BOM + question 列 校验通过）")

    if not args.skip_scan:
        print("[构建] 2/6 构建前隐私预扫（Key 形态 / 个人绝对路径）")
        findings = pre_scan()
        if findings:
            print("       命中以下问题，拒绝构建：")
            for item in findings:
                print(f"         - {item}")
            return 2
        print("       未发现 Key 形态或个人绝对路径")

    print("[构建] 3/6 准备发布目录")
    target = RELEASE_ROOT / f"{APP_NAME}-{VERSION}"
    if target.exists():
        if not args.overwrite:
            print(f"       目标已存在：{target}\n"
                  f"       发布包不做静默覆盖：请手工删除，或加 --overwrite。")
            return 3
        shutil.rmtree(target, ignore_errors=True)
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)

    print("[构建] 4/6 PyInstaller 打包（可能要一两分钟）")
    rc, log_path, dist_app = run_pyinstaller(args.overwrite)
    if rc != 0 or not dist_app.exists():
        print(f"       PyInstaller 失败（rc={rc}），日志：{log_path}")
        return 4
    copy_dist(dist_app, target)
    (target / "使用说明.txt").write_text(README.format(version=VERSION),
                                        encoding="utf-8-sig", newline="\r\n")
    # 49 号 P2：47 号 §1 点名的示例问题 CSV 必须真的进成品（上一版漏了，ZIP 根目录没有它）
    shutil.copyfile(SAMPLE_CSV, target / SAMPLE_CSV_NAME)

    print("[构建] 5/6 成品扫描（该带的带齐、不该带的一律不许有）")
    privacy, content = release_scan(target)
    if privacy or content:
        print("       命中以下问题，构建产物不可分发：")
        for item in privacy + content:
            print(f"         - {item}")
        return 5
    if not (target / EXE_NAME).exists():
        print(f"       缺少主程序 {EXE_NAME}")
        return 5
    csv_problems = validate_sample_csv(target / SAMPLE_CSV_NAME)
    if csv_problems:
        print("       成品里的示例问题 CSV 不合格：")
        for item in csv_problems:
            print(f"         - {item}")
        return 5
    print("       无敏感信息、无验收脚本/运行痕迹；主程序与示例问题 CSV 都在")

    print("[构建] 6/6 生成清单与 ZIP")
    info = toolchain_info()
    info["frontend_build"] = fb
    manifest = write_manifest(target, info)
    zip_path = RELEASE_ROOT / f"{APP_NAME}-{VERSION}.zip"
    if zip_path.exists():
        zip_path.unlink()
    build_zip(target, zip_path)
    manifest_copy = RELEASE_ROOT / f"{APP_NAME}-{VERSION}-清单.json"
    manifest_copy.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    exe_size = (target / EXE_NAME).stat().st_size
    print("\n" + "=" * 70)
    print(f"发布目录：{target}")
    print(f"主程序：{EXE_NAME}（{exe_size / 1024 / 1024:.1f} MB）；"
          f"整包 {manifest['file_count']} 个文件 / {manifest['total_bytes'] / 1024 / 1024:.1f} MB")
    print(f"ZIP：{zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB，"
          f"SHA-256 {sha256_of(zip_path)[:16].upper()}）")
    print(f"清单：{manifest_copy}")
    print(f"工具链：Python {manifest['python']} / PyInstaller {manifest['pyinstaller']} / "
          f"Node {manifest['node']}")
    print("提示：未签名，首次运行 SmartScreen 可能提示未知发布者。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
