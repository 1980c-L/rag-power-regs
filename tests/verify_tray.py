# -*- coding: utf-8 -*-
"""便携版托盘验收 —— verify_tray.py（0.5.1 / 47 号方案阶段 D）

判的是"无窗口体验"里**能被外部观测**的部分。刻意遵守一条纪律：**不拿程序自己的
日志当唯一证据**（自家脚本的自评标记不算证据）——凡是能从外面看到的，就从外面看：

  A. 源码级（import 仓库里的 tray.py）
    A1 剪贴板写入**读回来核对**（不看函数返回值）；
    A2 与终端共用控制台时 ensure_no_stale_console() 返回 False —— **不许误伤别人的终端**；
  B. EXE 级（解压 release ZIP 后真跑）
    B1 服务就绪（HTTP）；
    B2 托盘窗口真的被创建（FindWindowW 找固定类名，是窗口事实）；
    B3 **服务运行期间，外部能按"记事本那种打开方式"读到日志文件**
       （0.4.0 的常驻句柄会让这一步失败，是用户实打实会撞到的问题）；
    B4 --stop 正常退出、端口释放、**托盘窗口随之消失**（没留下野窗口）；
    B5 --no-tray 实例不创建托盘窗口（反证 B2 不是假阳性）；
    B6 --no-tray 实例 --stop 正常退出；
  C. 双击等价性（CREATE_NEW_CONSOLE，标准流仍在文件上）
    C1 独立控制台启动：服务就绪且托盘窗口在；
    C2 日志里出现"控制台：已脱离（双击启动）" —— **只是过程证据，不单独作为 PASS 依据**；
  D. **真实双击面（os.startfile，完全不做重定向）** —— 0.5.0 的翻车正是漏了这一条：
    C 组把标准流重定向到文件，于是 FreeConsole 不影响它们，一路 PASS；
    而用户双击时 stderr 指向控制台，一脱离就失效 → api_v3 的请求日志抛 OSError → API 全灭。
    D1 服务就绪（**API 健康检查必须过**）；D2 托盘窗口在；D3 能正常停止。

用法：
    python -X utf8 verify_tray.py --zip release\\RAG问答助手-0.5.1.zip
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXE_NAME = "RAG问答助手.exe"
TRAY_CLASS = "RagPortableTrayWnd"
API_PORT, WEB_PORT = 5280, 5273
API = f"http://127.0.0.1:{API_PORT}"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PASSED: list = []
FAILED: list = []
NOTES: list = []

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
CF_UNICODETEXT = 13


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""), flush=True)
    (PASSED if ok else FAILED).append(name)


def note(text: str) -> None:
    NOTES.append(text)
    print(f"      · {text}", flush=True)


# ---------------- 外部观测小工具 ----------------
def tray_window() -> int:
    """托盘隐藏窗口的句柄（0 = 不存在）。这是窗口事实，不是程序自述。"""
    return int(user32.FindWindowW(TRAY_CLASS, None) or 0)


def wait_tray_window(tries: int = 40) -> int:
    """等托盘窗口出现：托盘是在健康检查之后才建的，刚就绪时可能还没轮到它。"""
    for _ in range(tries):
        hwnd = tray_window()
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return 0


def readable_like_notepad(path: Path) -> tuple:
    """按"记事本/资源管理器那种打开方式"（只请求共享读）打开日志文件。

    0.4.0 把日志句柄一直攥在手里，这种打开方式会失败（用户会看到"文件正被占用"）。
    返回 (能不能打开, 原因)。
    """
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, None,
                                  OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False, f"WinError {ctypes.get_last_error()}"
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True, ""


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:                                              # noqa: BLE001
        return False


def wait_http(url: str, tries: int = 240) -> bool:
    for _ in range(tries):
        if http_ok(url):
            return True
        time.sleep(0.5)
    return False


def port_listening(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port_free(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.25)
    return False


def clean_env() -> dict:
    env = dict(os.environ)
    for key in ("DEEPSEEK_API_KEY", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME",
                "PYTHONSTARTUP", "RAG_USER_LIBRARY_DIR", "RAG_BATCH_DIR"):
        env.pop(key, None)
    return env


def launcher_log(exe_dir: Path) -> Path:
    return exe_dir / "logs" / f"launcher-{time.strftime('%Y%m%d')}.log"


def read_log(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception as exc:                                       # noqa: BLE001
        return f"<读不到日志：{type(exc).__name__}: {exc}>"


class ExeRunner:
    def __init__(self, exe_dir: Path, log_dir: Path):
        self.exe_dir = exe_dir
        self.exe = exe_dir / EXE_NAME
        self.log_dir = log_dir
        self.proc: subprocess.Popen | None = None

    def start(self, extra: tuple = (), flags: int = CREATE_NO_WINDOW, tag: str = "run"):
        args = [str(self.exe), "--no-browser", *extra]
        handle = (self.log_dir / f"{tag}_stdout.log").open("a", encoding="utf-8")
        self.proc = subprocess.Popen(args, cwd=str(self.exe_dir), env=clean_env(),
                                     creationflags=flags, stdout=handle,
                                     stderr=subprocess.STDOUT)
        return self.proc

    def run_cmd(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.exe), *args], cwd=str(self.exe_dir), env=clean_env(),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)


# ---------------- A. 源码级判据 ----------------
def read_clipboard() -> str:
    text = ""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
            if ptr:
                text = ctypes.wstring_at(ptr)
                kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
        user32.CloseClipboard()
    return text


def check_source_level() -> None:
    sys.path.insert(0, str(REPO))
    import tray

    # 剪贴板是用户的，先存下来，测完还回去
    original = read_clipboard()
    probe = "rag-tray-验收-0.5.0"
    tray.set_clipboard_text(probe)
    got = read_clipboard()                      # 读回来核对，不看函数返回值
    check("托盘·源码：剪贴板写入真的生效（读回来核对，不看函数返回值）",
          got == probe, f"读回={got[:40]!r}")
    if original and original != probe:
        tray.set_clipboard_text(original)
        note("剪贴板已还原为验收前的内容。")

    # 本进程与终端共用控制台，因此绝不允许被"脱离"（脱离会把调用方的终端也带走）
    detached = tray.ensure_no_stale_console()
    check("托盘·源码：与终端共用控制台时不动手（不误伤调用方的终端）",
          detached is False, f"返回值={detached}")


# ---------------- B/C. EXE 级判据 ----------------
def check_exe_level(runner: ExeRunner, out_dir: Path) -> None:
    log_path = launcher_log(runner.exe_dir)

    # ---- B1~B4：默认启动（启用托盘）----
    runner.start(tag="tray_on")
    check("EXE·托盘：服务就绪（托盘模式下也能正常起来）", wait_http(API + "/api/health"))
    hwnd_on = wait_tray_window()
    check("EXE·托盘：托盘窗口真的被创建（FindWindowW 找固定类名）", hwnd_on != 0,
          f"hwnd={hwnd_on}")

    readable, reason = readable_like_notepad(log_path)
    check("EXE·日志：服务运行期间，外部能按普通打开方式读到日志文件（0.4.0 这里是失败的）",
          readable, reason or str(log_path))
    text_while_running = read_log(log_path)
    (out_dir / "launcher_log_while_running.txt").write_text(text_while_running,
                                                            encoding="utf-8")

    stop = runner.run_cmd(["--stop"])
    check("EXE·托盘：--stop 正常退出（rc=0）", stop.returncode == 0,
          f"rc={stop.returncode} out={(stop.stdout or '').strip()[:60]}")
    check("EXE·托盘：两个端口都已释放",
          wait_port_free(API_PORT) and wait_port_free(WEB_PORT))
    runner.proc.wait(timeout=30)
    check("EXE·托盘：退出后托盘窗口没有留下（不残留野窗口）", tray_window() == 0,
          f"hwnd={tray_window()}")
    runner.kill()

    # ---- B5~B6：--no-tray（反证 B2 不是假阳性）----
    runner.start(extra=("--no-tray",), tag="tray_off")
    check("EXE·--no-tray：服务仍然正常就绪", wait_http(API + "/api/health"))
    check("EXE·--no-tray：确实不创建托盘窗口（反证上一条不是假阳性）",
          tray_window() == 0, f"hwnd={tray_window()}")
    stop2 = runner.run_cmd(["--stop"])
    check("EXE·--no-tray：--stop 正常退出（rc=0）", stop2.returncode == 0,
          f"rc={stop2.returncode}")
    wait_port_free(API_PORT)
    runner.kill()

    # ---- C1：双击等价（独立控制台）----
    runner.start(flags=CREATE_NEW_CONSOLE, tag="tray_new_console")
    check("EXE·双击等价：独立控制台启动时服务就绪（不崩、不卡）",
          wait_http(API + "/api/health"))
    hwnd_new = wait_tray_window()
    if hwnd_new == 0:
        (out_dir / "new_console_run.log").write_text(read_log(log_path), encoding="utf-8")
    check("EXE·双击等价：独立控制台启动时托盘窗口也在", hwnd_new != 0,
          f"hwnd={hwnd_new}")
    text = read_log(log_path)
    has_detach_line = "控制台：已脱离（双击启动）" in text
    note("过程证据（不作为 PASS 依据）：日志里"
         f"{'出现' if has_detach_line else '没有出现'}「控制台：已脱离（双击启动）」"
         " —— 这一行是程序自述，窗口是不是真的消失要人工看一眼。")
    stop3 = runner.run_cmd(["--stop"])
    check("EXE·双击等价：能正常停止（rc=0）", stop3.returncode == 0,
          f"rc={stop3.returncode} out={(stop3.stdout or '').strip()[:120]!r}")
    wait_port_free(API_PORT)
    runner.kill()


def kill_record_instance(exe_dir: Path) -> None:
    """兜底清理：万一 D 组启动的实例卡在失败弹窗上，按运行记录里的 pid 收掉它。"""
    try:
        data = json.loads((exe_dir / ".run" / "launcher.json").read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except Exception:                                              # noqa: BLE001
        return
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)


def check_real_double_click(runner: ExeRunner, out_dir: Path) -> None:
    """D. 真实双击面：用 os.startfile（ShellExecute）启动 —— **完全不做标准流重定向**。

    这一组是 0.5.0 翻车的直接守卫。0.5.0 用 subprocess + 重定向启动了无数次都 PASS：
    那时 stdout/stderr 指向文件，FreeConsole 不影响它们；而用户双击时它们指向控制台，
    一脱离就变成无效句柄 —— api_v3 的请求日志（写 sys.stderr）在每个请求上抛 OSError，
    API 全灭，页面服务却看着正常。
    """
    log_path = launcher_log(runner.exe_dir)
    before = read_log(log_path)
    os.startfile(str(runner.exe), "open", "--no-browser")     # 等价双击：不传任何句柄
    ready = wait_http(API + "/api/health", 160)
    if not ready:
        (out_dir / "real_double_click_failure.log").write_text(read_log(log_path),
                                                               encoding="utf-8")
        check("EXE·真实双击面：服务就绪（API 健康检查通过）—— 0.5.0 正是死在这一条", False,
              "80 秒内 API 没有就绪（日志尾部已写入证据）")
        kill_record_instance(runner.exe_dir)                   # 别把弹窗实例留在机器上
        check("EXE·真实双击面：托盘窗口在", False, "服务没起来，未继续")
        check("EXE·真实双击面：能正常停止（rc=0）", False, "服务没起来，未继续")
        return
    check("EXE·真实双击面：服务就绪（API 健康检查通过）—— 0.5.0 正是死在这一条", True)
    hwnd = wait_tray_window()
    check("EXE·真实双击面：托盘窗口在", hwnd != 0, f"hwnd={hwnd}")

    stop = runner.run_cmd(["--stop"])
    check("EXE·真实双击面：能正常停止（rc=0）", stop.returncode == 0,
          f"rc={stop.returncode} out={(stop.stdout or '').strip()[:60]}")
    if stop.returncode != 0:
        kill_record_instance(runner.exe_dir)
    wait_port_free(API_PORT)
    text = read_log(log_path)
    (out_dir / "real_double_click_run.log").write_text(text[len(before):], encoding="utf-8")
    runner.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description="便携版托盘验收（0.5.0）")
    ap.add_argument("--zip", required=True, help="release 目录里的 ZIP")
    ap.add_argument("--out", default="", help="证据输出目录（默认临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留解压目录（排查用）")
    args = ap.parse_args()

    zip_path = Path(args.zip).resolve()
    if not zip_path.exists():
        print(f"找不到 ZIP：{zip_path}")
        return 2
    if port_listening(API_PORT) or port_listening(WEB_PORT):
        print(f"端口 {API_PORT}/{WEB_PORT} 已被占用：先停掉正在运行的实例再验收。")
        return 2

    out_dir = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="rag_tray_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rag_tray_unzip_"))
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work)
    roots = {name.split("/", 1)[0] for name in zipfile.ZipFile(zip_path).namelist()}
    exe_dir = work / sorted(roots)[0] if len(roots) == 1 else work
    print(f"解压目录：{exe_dir}")
    print(f"证据目录：{out_dir}\n")

    runner = ExeRunner(exe_dir, out_dir)
    try:
        check_source_level()
        check_exe_level(runner, out_dir)
        check_real_double_click(runner, out_dir)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 70)
    print("人工确认项（脚本无法自动判定，请照着看一遍）：")
    print("  H1 双击 RAG问答助手.exe：终端窗口闪一下就消失，之后不再有窗口常驻；")
    print("  H2 屏幕右下角通知区域出现蓝色图标，右键菜单四项齐全（打开页面/复制页面地址/"
          "打开资料文件夹/退出）；")
    print("  H3 双击托盘图标能打开页面；点菜单「退出」后图标消失、页面失效；")
    print("  H4 启动失败时（例如把文件夹设为只读）会弹窗，而不是「双击了没反应」。")
    if NOTES:
        print("\n过程证据（不作为 PASS 依据）：")
        for item in NOTES:
            print(f"  · {item}")
    if FAILED:
        print("\nFAIL 详情：")
        for name in FAILED:
            print(f"  - {name}")
    print(f"\n托盘验收：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    (out_dir / "tray_verify_result.json").write_text(
        json.dumps({"passed": len(PASSED), "failed": len(FAILED),
                    "passed_names": PASSED, "failed_names": FAILED,
                    "notes": NOTES, "zip": str(zip_path)},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据写入：{out_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
