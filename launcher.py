# -*- coding: utf-8 -*-
"""便携版主启动器 —— launcher.py（0.5.0）

一个双击就能用的本机助手：启动 API（默认 127.0.0.1:5280）与页面静态服务（默认 127.0.0.1:5273），
然后用 Edge 的 `--app` 窗口（没有 Edge 时退回默认浏览器）打开页面；退出走系统托盘菜单或 `--stop`。

0.5.1 修的（0.5.0 首次双击暴露的缺陷）：
  脱离控制台之后，原本指向控制台的标准流必须换成 devnull —— 否则 `api_v3` 的请求日志
  （写 `sys.stderr`）会在**每个请求**上抛 OSError：API 全线不可用，而页面服务因为日志走
  本文件的 `log()` 看着一切正常，最终表现为「页面就绪=True、API 就绪=False」。
  细节与实测证据见 tray.ensure_no_stale_console 的注释。

0.5.0 起的变化（无窗口体验，47 号方案阶段 D）：
  - 双击启动时，如果这个进程**独占**一个终端窗口（= 用户双击的场景），启动早期就把它
    脱离（FreeConsole）—— 用户看到的是"托盘图标 + 页面"，不再有一个终端窗口常驻；
  - 托盘菜单的「退出」与 `--stop` 走**完全同一条**收尾路径（设置退出事件 → 释放端口 →
    清运行记录），任何情况下都不杀进程；
  - 控制台没有被脱离时（命令行启动、验收脚本启动）行为与 0.4.0 **完全一致**：
    stdout 照常输出，既有 EXE 验收项一条都不用改；
  - 脱离之后用户看不到 print 了，所以"启动失败"这类必须被看见的信息改走系统弹窗
    （notify_user），不会变成"双击没反应"。

与 45 号"双进程 ps1"的关系（为什么这里是一个进程）：
  - 45 号那套要启动两个 Python 子进程，并用 PID+创建时间+命令行三元组核对身份，是为了**停止时不误杀**；
  - 便携版没有系统 Python 可用，两个服务都在**同一个进程**里用线程跑：
    没有子进程 → 不可能留下孤儿；退出时直接 server_close() → 端口释放是结构性的。
  - 停止不再"杀进程"，而是**让进程自己退出**：
        启动时生成一次性令牌，写进 <便携根>\\.run\\launcher.json；
        `--stop` 先向 `/api/launcher/status` 核对"记录里的 pid == 现场 pid"，
        再用记录里的令牌请求 `/api/launcher/shutdown`；
    令牌不对 → 403；pid 不对 → 直接拒绝。**任何情况下都不会去杀别的进程**，
    所以伪造 / 残缺 / 过期的身份记录最多导致"拒绝停止"，不会误伤无关进程。

数据位置（便携语义：数据跟着文件夹走）：
    <EXE 同级>\\用户资料\\     ← 我的资料库（可用 RAG_USER_LIBRARY_DIR 覆盖）
    <EXE 同级>\\.run\\         ← 进程记录 + 批量任务状态快照（可用 RAG_BATCH_DIR 覆盖）
    <EXE 同级>\\logs\\         ← 启动器日志

数据目录可写性（49 号 P2：fail closed，不是 traceback）：
    - 启动任何服务之前，先对上面三个数据目录**真写一次探测文件**再删掉；只要有写不了的，
      就直接中文提示并退出（退出码 6），不绑定端口、不启动服务；
    - 运行中写进程记录失败同样 fail closed：先停掉刚起的服务、释放端口，再按同一套话术退出；
    - 日志目录不在门禁内：日志写不了只影响日志本身，不影响产品功能（`log()` 本来就吞异常）。

命令行：
    RAG问答助手.exe                     正常启动（托盘 + 页面；双击时不留终端窗口）
    RAG问答助手.exe --stop              停止正在运行的实例（先核对身份，绝不误杀）
    RAG问答助手.exe --no-browser        只起服务不开浏览器（自动化验收用）
    RAG问答助手.exe --status            打印当前记录与现场进程是否一致
    RAG问答助手.exe --keep-console      保留终端窗口（排障：看实时输出）
    RAG问答助手.exe --no-tray           不注册托盘图标（排障：等价 0.4.0 的启动方式）
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 托盘是**体验项不是功能项**：导入失败只记一行日志，服务照常可跑。
try:
    from tray import TrayIcon, ensure_no_stale_console, message_box
    TRAY_IMPORT_ERROR = ""
except Exception as exc:                                           # noqa: BLE001
    TrayIcon = None                                                # type: ignore[assignment]
    ensure_no_stale_console = None                                 # type: ignore[assignment]
    message_box = None                                             # type: ignore[assignment]
    TRAY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _portable_base() -> Path:
    """便携根目录：打包后是 EXE 所在目录；源码运行时是仓库目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _portable_base()
# 数据位置必须在 import api_v3 之前定下来：user_library / batch_jobs 在 import 时读环境变量
os.environ.setdefault("RAG_USER_LIBRARY_DIR", str(BASE / "用户资料"))
os.environ.setdefault("RAG_BATCH_DIR", str(BASE / ".run" / "batches"))

DEFAULT_API_PORT = 5280
DEFAULT_WEB_PORT = 5273
RUN_DIR = BASE / ".run"
LOG_DIR = BASE / "logs"
RECORD_PATH = RUN_DIR / "launcher.json"
MARKER = "rag-power-regs-launcher"
VERSION = "0.5.2"           # 0.5.2：批量报告导出修复（中文字体显式声明 + 检索命中原文落地）

_LOG_LOCK = threading.Lock()   # 主线程与托盘线程都会写日志，避免行与行交错
# 是否已经脱离独占终端窗口（由 main() 在启动早期设置）。
# 它决定"必须让用户看见的提示"走 print 还是走系统弹窗，见 notify_user()。
_CONSOLE_DETACHED = False


def _fix_console_encoding() -> None:
    """输出被重定向（管道/文件）时改用 UTF-8，避免复核方读到乱码。

    直接在终端里运行时保持系统编码（中文控制台本来就正常显示）；
    只有"被重定向"这种情况才动编码 —— 验收脚本、CI 都属于后者。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                          # noqa: BLE001
            pass


_fix_console_encoding()


# ---------------- 日志 ----------------
def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    # 脱离控制台之后 stdout 可能已经失效（句柄指向被销毁的控制台）。
    # 打印失败绝不能让服务挂掉：日志文件才是唯一的权威记录。
    try:
        print(line, flush=True)
    except Exception:                                              # noqa: BLE001
        pass
    # 日志文件**每次开、写完就关**。0.4.0 用的是常驻句柄，副作用是用户在记事本/资源
    # 管理器里会撞到"文件正被占用"，验收脚本也读不到它 —— 日志量很小，这点开销换"随时可读"。
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with (LOG_DIR / f"launcher-{time.strftime('%Y%m%d')}.log").open(
                    "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:                                              # noqa: BLE001
        pass          # 日志写不了不能影响服务本身


# ---------------- 必须让用户看见的信息 ----------------
def notify_user(message: str, error: bool = False) -> None:
    """给用户看的最终结论：控制台还在就打印，已经脱离控制台就弹系统弹窗。

    脱离控制台是为了不让终端窗口常驻（用户的要求），代价是 print 没人看得见 ——
    所以"启动失败""停止结果"这类必须被看见的信息一律走这里，
    绝不会出现"双击了没反应、也不知道为什么"。
    """
    if _CONSOLE_DETACHED and callable(message_box):
        try:
            message_box(message, error=error)
            return
        except Exception:                                          # noqa: BLE001
            pass
    try:
        print(message, flush=True)
    except Exception:                                              # noqa: BLE001
        pass


def detach_console_if_alone() -> bool:
    """双击场景下脱离终端窗口；控制台是共用的时候（命令行/验收脚本启动）绝不动手。

    只在打包后的 EXE 里生效：源码运行时（开发调试、验收脚本 import 本模块）
    保持 0.4.0 的行为，免得把调用方自己的终端窗口给关掉。
    """
    global _CONSOLE_DETACHED
    if not getattr(sys, "frozen", False):
        return False
    if not callable(ensure_no_stale_console):
        return False
    try:
        done = bool(ensure_no_stale_console())
    except Exception as exc:                                       # noqa: BLE001
        log(f"脱离控制台窗口失败（不影响启动）：{type(exc).__name__}: {exc}")
        return False
    if done:
        _CONSOLE_DETACHED = True
    return done


# ---------------- 资源定位 ----------------
def resource_root() -> Path:
    """PyInstaller 解压目录（onedir 的 _internal）。源码运行时就是仓库目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def web_root() -> Path:
    root = resource_root()
    for cand in (root / "webapp", root / "frontend_v3" / "dist-api"):
        if (cand / "index.html").exists():
            return cand
    raise RuntimeError(f"找不到前端构建产物（dist-api）：{root}")


# ---------------- 进程身份记录 ----------------
def read_record() -> dict | None:
    if not RECORD_PATH.exists():
        return None
    try:
        data = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    except Exception:                                              # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("marker") != MARKER:
        return None
    for field in ("pid", "token", "api_port", "web_port", "started_at"):
        if not data.get(field):
            return None
    return data


def write_record(*, pid: int, token: str, api_port: int, web_port: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "marker": MARKER,
        "version": VERSION,
        "pid": int(pid),
        "token": token,
        "api_port": int(api_port),
        "web_port": int(web_port),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "exe": str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) else str(Path(__file__)),
    }
    tmp = RECORD_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, RECORD_PATH)
    finally:
        # 原子替换失败时也不能把含启动令牌的半成品留在磁盘上。
        # 清理失败不得覆盖原始写入异常；main() 的 clear_record() 还会再做一次兜底。
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except Exception:                                          # noqa: BLE001
            pass


def clear_record() -> None:
    for path in (RECORD_PATH, RECORD_PATH.with_suffix(".json.tmp")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:                                          # noqa: BLE001
            pass


# ---------------- 数据目录可写性（49 号 P2：启动前 fail closed） ----------------
def batch_state_dir() -> Path:
    """批量任务状态目录（便携语义：跟着文件夹走；可用环境变量覆盖）。"""
    return Path(os.environ.get("RAG_BATCH_DIR") or (BASE / ".run" / "batches"))


def user_library_dir() -> Path:
    """「我的资料库」目录（便携语义：跟着文件夹走；可用环境变量覆盖）。

    托盘菜单的「打开资料文件夹」和启动自检用的是同一个来源，不会出现两处不一致。
    """
    return Path(os.environ.get("RAG_USER_LIBRARY_DIR") or (BASE / "用户资料"))


def data_dirs() -> list:
    """本次运行**一定会写**的目录：[(给用户看的名字, 路径)]。

    名字里带绝对路径：用户截图反馈"启动失败"时能直接看出是哪个文件夹。
    """
    return [
        ("运行状态目录", RUN_DIR),
        ("批量任务状态目录", batch_state_dir()),
        ("我的资料库目录", user_library_dir()),
    ]


def probe_writable(path: Path) -> str:
    """真写一个临时文件再删掉：可写返回空串，否则返回能直接展示给用户的原因。"""
    path = Path(path)
    probe = path / f".write-probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:                                       # noqa: BLE001
        return f"无法创建目录（{type(exc).__name__}）"
    try:
        probe.write_text("ok", encoding="utf-8")
        return ""
    except Exception as exc:                                       # noqa: BLE001
        return f"无法写入文件（{type(exc).__name__}）"
    finally:
        try:
            probe.unlink()
        except Exception:                                          # noqa: BLE001
            pass


def check_data_dirs() -> list:
    """启动前自检：返回 [(名字, 路径, 原因)]；空列表 = 全部可写。"""
    problems = []
    for name, path in data_dirs():
        reason = probe_writable(Path(path))
        if reason:
            problems.append((name, str(path), reason))
    return problems


def report_unwritable(problems: list) -> None:
    """把"不可写"翻译成普通用户照着做就能解决的话（绝不打印 traceback）。

    日志里记全文；**弹窗只给摘要**（用户照着做得到的那几句），免得一大段文字看不清重点。
    """
    log("启动失败：程序所在的位置不可写，无法保存运行状态和你的资料。")
    for name, path, reason in problems:
        log(f"  - {name}：{path}（{reason}）")
    log("  常见原因：解压在 C:\\Program Files、C:\\ 根目录或只读共享目录；"
        "也可能被杀毒软件/管控策略拦住了写入。")
    log("  处理办法（任选其一）：")
    log(f"    1) 把整个文件夹移动到「文档」「桌面」等可写目录，再重新双击 {entry_name()}；")
    log("    2) 右键该文件夹 → 属性 → 安全 → 给当前用户「写入」权限。")
    log("  本次没有启动任何服务、没有绑定端口（只做了一次可写性探测，探测文件已删除）。")

    lines = ["启动失败：程序所在的位置不可写，无法保存运行状态和你的资料。", ""]
    for name, path, reason in problems:
        lines.append(f"· {name}：{path}（{reason}）")
    lines += ["", "处理办法（任选其一）：",
              "1) 把整个文件夹移动到「文档」「桌面」等可写目录，再重新双击；",
              "2) 右键该文件夹 → 属性 → 安全 → 给当前用户「写入」权限。",
              "", "本次没有启动任何服务、没有绑定端口（只做了一次可写性探测，探测文件已删除）。"]
    notify_user("\n".join(lines), error=True)


def entry_name() -> str:
    return Path(sys.executable).name if getattr(sys, "frozen", False) else "launcher.py"


# ---------------- 小工具 ----------------
def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_get_json(url: str, timeout: float = 3.0, token: str = "") -> tuple:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("X-Launcher-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:                                          # noqa: BLE001
            return e.code, {}
    except Exception:                                              # noqa: BLE001
        return 0, {}


def http_post_json(url: str, timeout: float = 5.0, token: str = "") -> tuple:
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Launcher-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:                                          # noqa: BLE001
            return e.code, {}
    except Exception:                                              # noqa: BLE001
        return 0, {}


def open_app_window(url: str) -> str:
    """优先用 Edge 的 --app 窗口（像本机应用，没有地址栏）；失败就退回默认浏览器。"""
    candidates = []
    for env_key, rel in (("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
                         ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
                         ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe")):
        base = os.environ.get(env_key)
        if base:
            candidates.append(Path(base) / rel)
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe), f"--app={url}"], close_fds=True)
                return f"edge-app（{exe.name}）"
            except Exception as exc:                               # noqa: BLE001
                log(f"Edge --app 启动失败，改用默认浏览器：{type(exc).__name__}")
    try:
        webbrowser.open(url)
        return "默认浏览器"
    except Exception as exc:                                       # noqa: BLE001
        return f"未能自动打开浏览器（{type(exc).__name__}）：请手动访问 {url}"


# ---------------- 静态页面服务 ----------------
class WebHandler(SimpleHTTPRequestHandler):
    """只服务便携包内的 dist-api 目录：禁止列目录、禁止越出根目录。"""

    server_version = "rag-portable-web"
    protocol_version = "HTTP/1.1"
    root_dir = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.root_dir), **kwargs)

    def log_message(self, fmt: str, *args) -> None:                # noqa: A003
        log("[web] " + fmt % args)

    def list_directory(self, path):                                # noqa: A003
        self.send_error(404, "Not Found")
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


# ---------------- 启动 / 停止 ----------------
def api_handler_class():
    """复用 api_v3 的 Handler，只多挂两个启动器端点（产品接口一个字节都不改）。"""
    import api_v3

    class LauncherHandler(api_v3.Handler):
        token = ""
        shutdown_event: threading.Event | None = None

        def log_message(self, fmt: str, *args) -> None:            # noqa: A003
            """请求日志改走启动器的文件日志。

            api_v3 原本写 `sys.stderr`；双击启动时控制台已被脱离、标准流指向 devnull，
            再往那儿写等于把访问日志丢掉。写文件既留证，也不影响 api_v3 单独运行时的行为。
            """
            log("[api] " + fmt % args)

        def _path_only(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/")

        def do_GET(self) -> None:                                  # noqa: N802
            if self._path_only() == "/api/launcher/status":
                self._send_json(200, {"ok": True, "marker": MARKER, "version": VERSION,
                                      "pid": os.getpid()})
                return
            super().do_GET()

        def do_POST(self) -> None:                                 # noqa: N802
            if self._path_only() == "/api/launcher/shutdown":
                if (self.headers.get("X-Launcher-Token") or "") != self.token:
                    self._send_json(403, {"error": {"code": "bad_token",
                                                    "message": "启动令牌不匹配，拒绝退出。"}})
                    return
                self._send_json(200, {"ok": True, "stopping": True})
                if self.shutdown_event is not None:
                    self.shutdown_event.set()
                return
            super().do_POST()

    return LauncherHandler


def cmd_status() -> int:
    record = read_record()
    if record is None:
        notify_user("[状态] 没有可用的启动记录（可能没启动过，或记录被破坏）。")
        return 1
    api_port = int(record["api_port"])
    code, body = http_get_json(f"http://127.0.0.1:{api_port}/api/launcher/status")
    same = code == 200 and body.get("pid") == record.get("pid") and body.get("marker") == MARKER
    notify_user(f"[状态] 记录：pid={record['pid']} 端口={api_port}/{record['web_port']} "
                f"启动于 {record['started_at']}\n"
                f"[状态] 现场：{'一致（就是它）' if same else '不一致或服务已退出'}"
                f"（HTTP {code}{'，pid=' + str(body.get('pid')) if body.get('pid') else ''}）")
    return 0 if same else 1


def cmd_stop() -> int:
    """停止：**只请求记录里那个实例自己退出**，绝不杀进程。

    三种情况都不动别人的进程：
      - 记录损坏/过期 → 拒绝并保留记录；
      - 记录里的 pid 与现场不一致 → 拒绝（可能被伪造或已被复用）；
      - 令牌不匹配 → 服务端 403。

    与托盘菜单的「退出」是同一条路径：都只是让服务自己收尾，不做 TerminateProcess。
    """
    record = read_record()
    if record is None:
        notify_user("[停止] 没有可用的启动记录，未做任何操作。")
        return 1
    api_port = int(record["api_port"])
    code, body = http_get_json(f"http://127.0.0.1:{api_port}/api/launcher/status")
    if code != 200:
        notify_user(f"[停止] 记录里的实例没有响应（HTTP {code}）：可能已经退出。"
                    "记录文件保留，请自行核对后删除。")
        return 1
    if body.get("pid") != record.get("pid"):
        notify_user(f"[停止] 拒绝：现场 pid={body.get('pid')} 与记录 pid={record.get('pid')} 不一致。"
                    "记录文件已保留作证据，未做任何操作。")
        return 1
    code, body = http_post_json(f"http://127.0.0.1:{api_port}/api/launcher/shutdown",
                                token=str(record["token"]))
    if code != 200:
        notify_user(f"[停止] 拒绝：令牌校验失败（HTTP {code}）。记录文件已保留，未做任何操作。")
        return 1
    # 等端口真正释放（最多 20 秒）
    for _ in range(100):
        if not port_in_use(api_port):
            notify_user(f"[停止] 已退出，端口 {api_port} 已释放。")
            return 0
        time.sleep(0.2)
    notify_user(f"[停止] 已发送退出请求，但端口 {api_port} 20 秒内仍在监听，请手工核对。")
    return 1


def _running_instance(record: dict | None) -> bool:
    if record is None:
        return False
    code, body = http_get_json(f"http://127.0.0.1:{int(record['api_port'])}/api/launcher/status")
    return code == 200 and body.get("pid") == record.get("pid") and body.get("marker") == MARKER


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="RAG 问答助手（便携版启动器）")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的实例")
    ap.add_argument("--status", action="store_true", help="查看运行状态")
    ap.add_argument("--no-browser", action="store_true", help="不打开浏览器窗口")
    ap.add_argument("--keep-console", action="store_true",
                    help="保留终端窗口（排障：看实时输出）")
    ap.add_argument("--no-tray", action="store_true", help="不注册系统托盘图标（排障用）")
    ap.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    ap.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    args = ap.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.stop:
        return cmd_stop()

    # 0. 无窗口体验的第一件事：双击场景下把终端窗口脱离掉。
    #    判据在 tray.ensure_no_stale_console()：只有"这个控制台里只有我一个进程"
    #    才会动手；命令行 / 验收脚本启动时控制台是共用的，绝不碰（否则会把用户的终端关掉）。
    if not args.keep_console:
        detach_console_if_alone()

    log(f"RAG 问答助手（便携版 {VERSION}）启动中…")
    log(f"程序目录：{BASE}")
    log(f"运行方式：{'打包 EXE' if getattr(sys, 'frozen', False) else '源码（开发）'}；"
        f"Python：{sys.version.split()[0]}；"
        f"控制台：{'已脱离（双击启动）' if _CONSOLE_DETACHED else '保留'}")
    if TRAY_IMPORT_ERROR:
        log(f"托盘模块不可用（只影响托盘图标，不影响服务）：{TRAY_IMPORT_ERROR}")

    record = read_record()
    if _running_instance(record):
        url = f"http://127.0.0.1:{int(record['web_port'])}/"
        log(f"检测到已经在运行（pid={record['pid']}）→ 直接打开页面：{url}")
        log(f"打开方式：{open_app_window(url)}")
        return 0
    if record is not None:
        log("存在启动记录但现场对不上（过期或被伪造）：本次不采信该记录，也不动任何进程；"
            "继续按新实例启动。")

    # 49 号 P2：可写性在**绑定端口之前**判定 —— 不可写就不半启动，直接给出能照着做的提示。
    problems = check_data_dirs()
    if problems:
        report_unwritable(problems)
        return 6

    for port in (args.api_port, args.web_port):
        if port_in_use(port):
            message = (f"端口 {port} 已被其它程序占用：本助手不抢端口、也不停止别的程序。\n"
                       f"请先释放该端口，或用 --api-port/--web-port 换一对端口。")
            log(message)
            if _CONSOLE_DETACHED:
                notify_user("启动失败：" + message, error=True)
            return 3

    import api_v3
    api_v3.Handler.llm_base_url = api_v3.config.DEEPSEEK_BASE_URL
    api_v3.Handler.llm_model = api_v3.config.DEEPSEEK_MODEL

    shutdown_event = threading.Event()
    token = secrets.token_hex(16)
    handler_cls = api_handler_class()
    handler_cls.token = token
    handler_cls.shutdown_event = shutdown_event

    servers = []
    try:
        web_dir = web_root()
    except RuntimeError as exc:
        log(f"启动失败：{exc}")
        if _CONSOLE_DETACHED:
            notify_user(f"启动失败：找不到页面文件。\n\n{exc}\n\n"
                        "请确认整个文件夹是完整解压出来的（_internal 目录不能少）。", error=True)
        return 4

    WebHandler.root_dir = web_dir

    try:
        api_server = ThreadingHTTPServer(("127.0.0.1", args.api_port), handler_cls)
        api_server.daemon_threads = True
        web_server = ThreadingHTTPServer(("127.0.0.1", args.web_port), WebHandler)
        web_server.daemon_threads = True
    except OSError as exc:
        log(f"启动失败（端口绑定不上，可能是权限或端口刚被占用）：{type(exc).__name__}: {exc}")
        if _CONSOLE_DETACHED:
            notify_user(f"启动失败：本机端口绑定不上（{type(exc).__name__}）。\n\n"
                        "通常是端口刚被别的程序占用；再点一次，或改用别的端口。", error=True)
        return 3

    servers = [api_server, web_server]
    for server, name in ((api_server, "本机 API"), (web_server, "页面服务")):
        threading.Thread(target=server.serve_forever, name=f"serve-{name}",
                         daemon=True).start()

    try:
        write_record(pid=os.getpid(), token=token, api_port=args.api_port, web_port=args.web_port)
    except Exception as exc:                                       # noqa: BLE001
        # 49 号 P2 兜底分支：预检通过但真正写记录时仍然失败（权限在两次之间被改、被杀软拦住、
        # 磁盘满……）。这里的判据不是"能不能写"，而是"失败之后不许留下半启动的实例"。
        log("启动失败：无法写入运行状态文件（程序所在的位置不可写或被杀毒软件拦截）。")
        log(f"  - 运行状态目录：{RUN_DIR}（无法写入记录文件：{type(exc).__name__}）")
        log("  处理办法（任选其一）：")
        log(f"    1) 把整个文件夹移动到「文档」「桌面」等可写目录，再重新双击 {entry_name()}；")
        log("    2) 右键该文件夹 → 属性 → 安全 → 给当前用户「写入」权限。")
        for server in servers:
            server.shutdown()
            server.server_close()
        clear_record()
        log("  本次服务已停止、端口已释放，没有留下运行中的实例。")
        notify_user("启动失败：无法写入运行状态文件（程序所在的位置不可写或被杀毒软件拦截）。\n\n"
                    f"运行状态目录：{RUN_DIR}\n\n"
                    "处理办法（任选其一）：\n"
                    "1) 把整个文件夹移动到「文档」「桌面」等可写目录，再重新双击；\n"
                    "2) 右键该文件夹 → 属性 → 安全 → 给当前用户「写入」权限。\n\n"
                    "本次服务已停止、端口已释放，没有留下运行中的实例。", error=True)
        return 6

    # 健康检查：两个服务都要真的应答，否则如实失败并收拾干净
    ok_api = ok_web = False
    for _ in range(60):
        code, _body = http_get_json(f"http://127.0.0.1:{args.api_port}/api/health", timeout=2)
        ok_api = code == 200
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.web_port}/", timeout=2) as r:
                ok_web = r.status == 200
        except Exception:                                          # noqa: BLE001
            ok_web = False
        if ok_api and ok_web:
            break
        time.sleep(0.5)

    if not (ok_api and ok_web):
        log(f"启动失败：API 就绪={ok_api} 页面就绪={ok_web}")
        for server in servers:
            server.shutdown()
            server.server_close()
        clear_record()
        if _CONSOLE_DETACHED:
            notify_user(f"启动失败：服务没有就绪（API 就绪={ok_api}，页面就绪={ok_web}）。\n\n"
                        f"详细原因在日志里：{LOG_DIR}", error=True)
        return 5

    url = f"http://127.0.0.1:{args.web_port}/"
    key_state = "已检测到 DEEPSEEK_API_KEY（选「检索并生成」时才会调用模型）" \
        if (api_v3.config.DEEPSEEK_API_KEY or "").strip() else \
        "未检测到 DEEPSEEK_API_KEY（只提供检索结果，不会发任何模型请求）"
    log(f"本机 API：http://127.0.0.1:{args.api_port}（只绑本机回环）")
    log(f"页面地址：{url}")
    log(f"模型调用：{key_state}")
    if not args.no_browser:
        log(f"打开方式：{open_app_window(url)}")

    # 托盘：把"退出"从终端窗口搬到通知区域。
    # 控制台已经被脱离时（双击启动），它是用户唯一的可见入口 —— 所以失败要明说怎么退出。
    tray_icon = None
    if args.no_tray:
        log("已按 --no-tray 跳过托盘图标。")
    elif callable(TrayIcon):
        try:
            tray_icon = TrayIcon(
                tooltip=f"RAG 问答助手 {VERSION}（运行中）",
                page_url=url,
                on_open_page=lambda: open_app_window(url),
                on_exit=shutdown_event.set,
                on_open_data=lambda: os.startfile(str(user_library_dir())),
                icon_path=resource_root() / "assets" / "rag-assistant.ico",
                log=log,
            )
            if not tray_icon.start(timeout=8):
                tray_icon = None
        except Exception as exc:                                   # noqa: BLE001
            log(f"托盘启动异常（不影响服务）：{type(exc).__name__}: {exc}")
            tray_icon = None

    if tray_icon is not None:
        log("退出方式：右键托盘图标 →「退出」（等价于 --stop；关掉浏览器窗口不会停止服务）。")
    else:
        log("退出方式：运行 RAG问答助手.exe --stop（Ctrl+C 也可以）。")
        if _CONSOLE_DETACHED:
            # 没有窗口、托盘也没起来：用户手上没有任何可见入口，必须明说怎么退出。
            notify_user("RAG 问答助手已经在后台运行，页面稍后会自动打开。\n\n"
                        "（托盘图标没有注册成功，通知区域看不到它。）\n"
                        "要退出时，请再次运行 RAG问答助手.exe --stop。\n"
                        "关闭浏览器里的页面不会停止服务。")

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(0.5)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出。")
    finally:
        if tray_icon is not None:
            tray_icon.stop()
        for server in servers:
            server.shutdown()
            server.server_close()
        clear_record()
        log("已退出，端口已释放。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
