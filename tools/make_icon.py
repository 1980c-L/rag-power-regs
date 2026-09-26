# -*- coding: utf-8 -*-
"""生成发布图标 —— tools/make_icon.py

用途：把 `assets/icon.svg` 变成 `assets/rag-assistant.ico`（多尺寸），供
      PyInstaller `--icon` 与系统托盘使用。**只在出包机上跑，不随发布包分发。**

为什么用 Edge 渲染：
  构建机没有 Pillow 之类的图像库，而 Edge 是 Windows 10/11 自带的；
  渲染走同一个浏览器引擎，图标观感与页面一致。找不到 Edge **直接报错退出**，
  不静默降级 —— 否则会产出一个没图标的发布包还以为成功了。

为什么手写 ICO 容器：
  Vista 之后的 ICO 允许直接内嵌 PNG（本工具就是这么做的），因此只需要
  `struct` 拼一个 6 字节头 + 每尺寸 16 字节目录项 + 原始 PNG 字节，零依赖。

用法（务必带 -X utf8，中文 Windows 下更稳）：
    python -X utf8 tools/make_icon.py
    python -X utf8 tools/make_icon.py --sizes 256,64,48,32,16
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SVG = REPO / "assets" / "icon.svg"
OUT_ICO = REPO / "assets" / "rag-assistant.ico"
DEFAULT_SIZES = (256, 128, 64, 48, 32, 16)


def find_edge() -> Path:
    """按 launcher 相同的顺序找 Edge；找不到就明确失败。"""
    candidates = []
    for env_key, rel in (("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
                         ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
                         ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe")):
        base = os.environ.get(env_key)
        if base:
            candidates.append(Path(base) / rel)
    for exe in candidates:
        if exe.exists():
            return exe
    raise SystemExit("找不到 Microsoft Edge，无法渲染图标（本工具依赖 Edge 的无头模式）。")


def render_png(edge: Path, svg: Path, size: int, out_png: Path, profile: Path) -> None:
    """用 Edge 无头模式把 SVG 渲染成指定边长的透明 PNG。"""
    cmd = [
        str(edge),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        # 独立的临时 profile：绝不碰用户自己的 Edge 配置
        f"--user-data-dir={profile}",
        "--default-background-color=00000000",     # 圆角之外保持透明
        "--force-device-scale-factor=1",
        f"--window-size={size},{size}",
        f"--screenshot={out_png}",
        svg.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if not out_png.exists():
        raise SystemExit(f"Edge 渲染 {size}x{size} 失败（rc={proc.returncode}）："
                         f"{proc.stderr.decode('utf-8', 'replace')[:300]}")


def read_png_size(path: Path) -> tuple:
    """读 PNG 头里的宽高，用于核对渲染结果与声明尺寸一致。"""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path.name} 不是 PNG")
    width, height = struct.unpack(">II", raw[16:24])
    return int(width), int(height)


def build_ico(entries: list, out_path: Path) -> int:
    """entries: [(size, png_bytes)] → ICO 文件。返回总字节数。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    blobs = b""
    for size, data in entries:
        # 256 在 ICO 目录项里用 0 表示
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    out_path.write_bytes(header + directory + blobs)
    return len(header) + len(directory) + len(blobs)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 assets/rag-assistant.ico")
    ap.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES),
                    help="逗号分隔的边长列表（默认 256,128,64,48,32,16）")
    ap.add_argument("--preview", default="", help="额外导出一张 256x256 PNG 到该路径（人工核对用）")
    args = ap.parse_args()

    if not SVG.exists():
        raise SystemExit(f"缺少矢量源文件：{SVG}")
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    for size in sizes:
        if not 16 <= size <= 256:
            raise SystemExit(f"尺寸越界：{size}（ICO 支持 16–256）")

    edge = find_edge()
    print(f"[图标] Edge：{edge}")
    print(f"[图标] 源文件：{SVG.relative_to(REPO).as_posix()}")

    entries = []
    with tempfile.TemporaryDirectory(prefix="rag-icon-") as tmp:
        tmp_dir = Path(tmp)
        profile = tmp_dir / "edge-profile"
        for size in sizes:
            png = tmp_dir / f"icon-{size}.png"
            render_png(edge, SVG, size, png, profile)
            actual = read_png_size(png)
            if actual != (size, size):
                raise SystemExit(f"渲染尺寸不符：期望 {size}x{size}，实际 {actual[0]}x{actual[1]}")
            entries.append((size, png.read_bytes()))
            print(f"[图标] {size}x{size} 渲染完成（{len(entries[-1][1]) / 1024:.1f} KB）")

    total = build_ico(entries, OUT_ICO)
    print(f"[图标] 已写出：{OUT_ICO.relative_to(REPO).as_posix()}"
          f"（{len(entries)} 个尺寸，{total / 1024:.1f} KB）")

    if args.preview:
        preview = Path(args.preview).resolve()
        preview.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rag-icon-") as tmp:
            png = Path(tmp) / "preview.png"
            render_png(edge, SVG, 256, png, Path(tmp) / "edge-profile")
            preview.write_bytes(png.read_bytes())
        print(f"[图标] 预览图：{preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
