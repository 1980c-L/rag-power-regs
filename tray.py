# -*- coding: utf-8 -*-
"""便携版系统托盘 —— tray.py（0.5.0）

只做一件事：在 Windows 通知区域放一个图标，右键给出「打开页面 / 复制页面地址 /
打开资料文件夹 / 退出」。**零依赖**：只用 ctypes 调 Shell_NotifyIcon，不引入
pystray / Pillow（便携包里多一个图像库不划算，而且托盘图标本来就是 EXE 自带的那个）。

设计边界（为什么是"可选件"）：
  - 托盘是**体验项不是功能项**：注册失败（被策略拦、Explorer 没起来、非 Windows）
    只记一行日志并返回 False，服务照常运行、照常可以用 `--stop` 退出。
    绝不允许"托盘没起来"变成"程序起不来"。
  - **不杀进程**：菜单里的「退出」走的是和 `--stop` 完全一样的路径（设置
    退出事件 → 主线程收尾 → 释放端口），不做任何 TerminateProcess。

线程模型：
    托盘窗口和消息循环跑在**独立线程**里 —— Windows 要求"谁创建窗口谁收消息"，
    不能借用主线程（主线程在跑 HTTP 服务）。主线程退出时调用 stop()，
    它会 PostMessage(WM_CLOSE) 让托盘线程自己收尾（NIM_DELETE + 销毁窗口）。

主要可验收点（verify_tray.py 就是查这几条）：
    1. 隐藏窗口类名固定（CLASS_NAME），可用 FindWindowW 找到 → 证明托盘窗口真的建起来了；
    2. start() 只有在**图标注册成功之后**才返回 True（用事件同步，不是 sleep 猜）；
    3. stop() 之后窗口类被注销、线程退出。
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
import threading
from ctypes import wintypes
from pathlib import Path

# ---------------- Win32 常量 ----------------
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
CALLBACK_MESSAGE = WM_APP + 1                  # 托盘图标事件都发到这个自定义消息
WM_TASKBARCREATED = 0                          # 运行时用 RegisterWindowMessageW 取真值

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
LR_SHARED = 0x8000
MF_STRING = 0x0000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
IDI_APPLICATION = 32512

# 菜单项 id
MENU_OPEN_PAGE = 1
MENU_COPY_URL = 2
MENU_OPEN_DATA = 3
MENU_EXIT = 4

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    """NOTIFYICONDATAW（Vista 及以后版本：带 guidItem / hBalloonIcon）。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def _setup_signatures() -> None:
    """64 位下必须显式声明参数/返回类型，否则句柄会被截断成 32 位。"""
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                   ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.LPVOID]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


if IS_WINDOWS:
    _setup_signatures()


def set_clipboard_text(text: str) -> bool:
    """把文本放进剪贴板（菜单里的「复制页面地址」）。失败返回 False，不抛异常。"""
    if not IS_WINDOWS:
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, buf, size)
            kernel32.GlobalUnlock(handle)
            # SetClipboardData 成功后所有权归系统，不要再 GlobalFree
            return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
        finally:
            user32.CloseClipboard()
    except Exception:                                              # noqa: BLE001
        return False


class TrayIcon:
    """一个托盘图标（含菜单）。start()/stop() 都是幂等的。"""

    CLASS_NAME = "RagPortableTrayWnd"          # 固定类名：验收脚本靠它找窗口
    WINDOW_TITLE = "RAG问答助手·托盘"
    ICON_ID = 1

    def __init__(self, *, tooltip: str, page_url: str, on_open_page, on_exit,
                 on_open_data, icon_path: Path | None = None, log=print):
        self.tooltip = tooltip
        self.page_url = page_url
        self.on_open_page = on_open_page
        self.on_exit = on_exit
        self.on_open_data = on_open_data
        self.icon_path = Path(icon_path) if icon_path else None
        self.log = log

        self._hwnd = 0
        self._hicon = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._wndproc_ref = None                # 必须持有引用，否则回调被 GC 掉
        self._taskbar_created_msg = 0
        self._registered = False

    # ---------------- 对外接口 ----------------
    def start(self, timeout: float = 8.0) -> bool:
        """起托盘线程；等图标**真的注册成功**再返回（超时/失败返回 False）。"""
        if not IS_WINDOWS:
            self.log("[托盘] 非 Windows 平台，跳过托盘。")
            return False
        if self._thread is not None:
            return self._registered
        self._thread = threading.Thread(target=self._pump, name="tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.log(f"[托盘] {timeout:.0f} 秒内没有注册成功（不重试，服务不受影响）。")
            return False
        return self._registered

    def stop(self, timeout: float = 5.0) -> None:
        """请求托盘线程收尾（删除图标、销毁窗口、退出消息循环）。"""
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:                                      # noqa: BLE001
                pass
        if self._thread is not None:
            self._done.wait(timeout)

    # ---------------- 内部：窗口与消息循环 ----------------
    def _load_icon(self) -> int:
        """优先用 EXE 自带的图标资源；源码运行时退回 assets 里的 .ico 文件。"""
        try:
            if getattr(sys, "frozen", False):
                count = ctypes.c_uint(0)
                large = wintypes.HICON()
                small = wintypes.HICON()
                got = shell32.ExtractIconExW(str(sys.executable), 0,
                                             ctypes.byref(large), ctypes.byref(small), 1)
                if got and large.value:
                    return int(large.value)
            if self.icon_path and self.icon_path.exists():
                handle = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON,
                                           0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if handle:
                    return int(handle)
        except Exception:                                          # noqa: BLE001
            pass
        return int(user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION)) or 0)

    def _add_icon(self) -> bool:
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self._hwnd
        data.uID = self.ICON_ID
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = CALLBACK_MESSAGE
        data.hIcon = self._hicon
        data.szTip = self.tooltip[:127]
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

    def _pump(self) -> None:
        try:
            self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            self._hicon = self._load_icon()

            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc)

            wc = WNDCLASSEX()
            wc.cbSize = ctypes.sizeof(WNDCLASSEX)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS_NAME
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                # 183 = 类已存在（上一次没退干净），这次算失败但不影响服务
                self.log(f"[托盘] 注册窗口类失败（WinError {err}）。")
                return

            self._hwnd = user32.CreateWindowExW(0, self.CLASS_NAME, self.WINDOW_TITLE,
                                                0, 0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                self.log(f"[托盘] 创建窗口失败（WinError {ctypes.get_last_error()}）。")
                return

            self._registered = self._add_icon()
            if self._registered:
                self.log("[托盘] 图标已注册：右键查看菜单，双击打开页面。")
            else:
                self.log(f"[托盘] 图标注册失败（WinError {ctypes.get_last_error()}）。")
            self._ready.set()

            if not self._registered:
                user32.DestroyWindow(self._hwnd)
                return

            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[托盘] 线程异常退出：{type(exc).__name__}: {exc}")
        finally:
            self._ready.set()                                      # 失败路径也要放行等待者
            if self._registered and self._hwnd:
                try:
                    data = NOTIFYICONDATA()
                    data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
                    data.hWnd = self._hwnd
                    data.uID = self.ICON_ID
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                except Exception:                                  # noqa: BLE001
                    pass
            self._registered = False
            self._done.set()

    def _wndproc(self, hwnd, msg, wparam, lparam) -> int:
        try:
            if msg == CALLBACK_MESSAGE:
                self._on_icon_event(lparam & 0xFFFF)
                return 0
            if msg == self._taskbar_created_msg and self._taskbar_created_msg:
                # Explorer 重启后通知区域被重建，需要重新添加图标
                self._registered = self._add_icon()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._hwnd = 0
                user32.PostQuitMessage(0)
                return 0
        except Exception:                                          # noqa: BLE001
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_icon_event(self, event: int) -> None:
        """通知区域发回来的鼠标事件。"""
        if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            self._do_open_page()
            return
        if event == WM_RBUTTONUP:
            self._show_menu()

    def _show_menu(self, command: int | None = None) -> int:
        """弹菜单；TrackPopupMenu 直接返回被点中的 id（TPM_RETURNCMD）。"""
        hwnd = self._hwnd
        if not hwnd:
            return 0
        menu = user32.CreatePopupMenu()
        if not menu:
            return 0
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_PAGE, "打开页面")
            user32.AppendMenuW(menu, MF_STRING, MENU_COPY_URL, "复制页面地址")
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_DATA, "打开资料文件夹")
            user32.AppendMenuW(menu, MF_STRING, 0, None)            # 分隔线
            user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # 经典要求：弹菜单前把窗口调到前台，否则菜单会"点不掉"
            user32.SetForegroundWindow(hwnd)
            picked = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                           point.x, point.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        picked = int(picked) if command is None else int(command)
        self._handle_command(picked)
        return picked

    def _handle_command(self, command: int) -> None:
        if command == MENU_OPEN_PAGE:
            self._do_open_page()
        elif command == MENU_COPY_URL:
            ok = set_clipboard_text(self.page_url)
            self.log(f"[托盘] 复制页面地址：{'成功' if ok else '失败'}")
        elif command == MENU_OPEN_DATA:
            try:
                self.on_open_data()
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[托盘] 打开资料文件夹失败：{type(exc).__name__}")
        elif command == MENU_EXIT:
            self.log("[托盘] 菜单里选择了「退出」。")
            try:
                self.on_exit()
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[托盘] 退出回调异常：{type(exc).__name__}: {exc}")

    def _do_open_page(self) -> None:
        try:
            self.on_open_page()
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[托盘] 打开页面失败：{type(exc).__name__}")


def _console_stream_fds() -> list:
    """判断哪几个标准流**正指向控制台**（必须在 FreeConsole 之前调用）。

    脱离之后 fd 1/2 直接变成无效句柄，连 `os.fstat(fd)` 都抛 OSError（WinError 6），
    那时就再也分辨不出"它原本是控制台"还是"它本来就是文件"了。
    """
    console_fds = []
    for fd in (1, 2):
        try:
            if stat.S_ISCHR(os.fstat(fd).st_mode):
                console_fds.append(fd)
        except OSError:
            pass
    return console_fds


def _replace_console_streams(console_fds: list) -> None:
    """把原本指向控制台的标准流换成 devnull 文件对象（FreeConsole 之后调用）。

    为什么必须换成"sys 层的新对象"，而不是 `os.dup2` 到 devnull（实测踩过，别退回去）：
        Python 检测到标准流是控制台时会用 `_WindowsConsoleIO` 包装，它攥的是**控制台
        句柄本身**，不是 fd 的重定向 —— 所以 dup2 之后 `sys.stderr.write` 依然是
        WinError 1/6；只有把 `sys.stdout` / `sys.stderr` 整个换成别的流才有效。

    为什么非修不可（0.5.0 首次双击翻车的根因）：
        `api_v3` 的请求日志写 `sys.stderr`。双击启动时 stderr 指向控制台，
        FreeConsole 之后它就是无效句柄 —— **每个 API 请求都在写访问日志那一步抛
        OSError**，API 全线不可用；而页面服务的日志走 launcher 的 `log()`
        （有 try 保护 + 写文件），于是看起来一切正常，最终表现为
        「页面就绪=True、API 就绪=False」。

    只替换原本是控制台的流：被重定向到文件/管道的场景保持原样，
    所以从命令行启动、验收脚本抓 stdout 的行为一字不变。
    """
    for fd, name in ((1, "stdout"), (2, "stderr")):
        if fd not in console_fds:
            continue
        try:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
        except OSError:
            pass


def ensure_no_stale_console() -> bool:
    """如果本进程**独占**一个控制台窗口，就把它脱离（返回 True）。

    为什么需要：便携版是 console 子系统（保留 stdout，验收脚本要用），双击时
    Windows 会给它分配一个终端窗口。用户不想看见这个窗口常驻。

    为什么能安全做：先看 GetConsoleProcessList —— 只有自己一个进程时才动手。
    从命令行/验收脚本启动时，列表里还有父进程（cmd、pwsh、python…），
    这时**绝不动**，否则会把用户自己的终端窗口关掉。

    顺序（踩过）：先在脱离前记下"哪些标准流是控制台"，脱离后再把它们换成 devnull。

    False 表示"什么都没做"（没有控制台，或者控制台是和别人共用的）。
    """
    if not IS_WINDOWS:
        return False
    try:
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        pids = (ctypes.c_uint * 8)()
        count = kernel32.GetConsoleProcessList(pids, 8)
        if count != 1 or pids[0] != os.getpid():
            return False
        console_fds = _console_stream_fds()
        kernel32.FreeConsole()
        _replace_console_streams(console_fds)
        return True
    except Exception:                                              # noqa: BLE001
        return False


def message_box(text: str, title: str = "RAG 问答助手", error: bool = False) -> None:
    """脱离控制台之后，用户能看到的唯一提示通道（启动失败时用）。"""
    if not IS_WINDOWS:
        print(text)
        return
    MB_OK = 0x00000000
    MB_ICONERROR = 0x00000010
    MB_ICONINFORMATION = 0x00000040
    MB_TOPMOST = 0x00040000
    try:
        user32.MessageBoxW(None, text, title,
                           MB_OK | MB_TOPMOST | (MB_ICONERROR if error else MB_ICONINFORMATION))
    except Exception:                                              # noqa: BLE001
        print(text)
