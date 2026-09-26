# -*- coding: utf-8 -*-
"""开工基线记录（47 号方案第二节要求）—— tools/record_baseline.py

为什么需要它：47 号方案明确要求"不得通过整目录覆盖来实施"，开工前必须先把现场固化下来，
否则事后没人能证明"哪些文件是本轮改的、哪些是原来就这样"。本脚本只做一件事：
把当前现场（文件清单 + SHA-256 + 工具链版本 + 冻结锚点核对）写成两份可复核的记录。

它**只读**：除了 `--out-dir` 下的 `baseline.json` / `baseline.md`，不写任何文件。

冻结锚点（FROZEN_EXPECT）：47 号方案点名不得改动的文件。哈希取前 16 位（与 41/46 号
交付说明披露的口径一致）。任何一条不匹配都会在输出里标 MISMATCH —— 那意味着现场已被
本轮之外的改动动过，必须先停下来核对，而不是继续往下做。

运行：
    python tools/record_baseline.py --out-dir <证据目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 纳入基线的文件（白名单式，显式列出来，避免把 .run / 缓存 / 用户资料算进去）
INCLUDE_PATTERNS = (
    "*.py", "*.ps1", "*.bat", "*.txt", "*.md", "*.csv",
    "tools/**/*", "data/**/*", "eval/**/*", "index/**/*", "output/**/*",
    "frontend_v3/src/**/*", "frontend_v3/dist-api/**/*",
    "frontend_v3/index.html", "frontend_v3/package.json",
    "frontend_v3/vite.config.ts", "frontend_v3/tsconfig.json",
)

# 永不纳入：临时运行目录、日志、真实用户资料、依赖树
EXCLUDE_PARTS = ("node_modules", ".run", "logs", "用户资料", "__pycache__",
                 ".git", ".vite", ".pytest_cache")

# 47 号方案点名"本轮不得修改"的文件 → 预期哈希（SHA-256 前 16 位，大写）
FROZEN_EXPECT = {
    "rag_core.py": "E2BF2FDEB9270E96",
    "config.py": "EC8BC77162C99AC0",
    "tools/real_generation_batch.py": "D74FC8FF2C1034A8",
    "tools/real_gen_card.json": "6489BAB15396869F",
    "verify_real_gen_gates.py": "90062147D7F740C9",
    "data/rag_learning/corpus_manifest.json": "6789A821A2C0C8E5",
    "user_library.py": "C9067E70260FD039",
    "api_v3.py": "9B1BE7C9511E0F40",
    "verify_user_library.py": "8287772D62A73DED",
    "verify_portable_package.py": "A0CF68F63514F396",
    "启动-RAG助手.ps1": "FD40CC927C9BDD17",
    "停止-RAG助手.ps1": "FEAEB94822108172",
    "requirements-api.txt": "DDE8D6295A9DFB56",
    "frontend_v3/src/types/index.ts": "9B5490D155640584",
    "frontend_v3/src/services/apiService.ts": "674A1B30CFD97FC9",
    "frontend_v3/src/components/UserLibraryPanel.tsx": "F473699067425367",
}
# 注：04_eval.py / 05_gen_eval.py 只披露过前 8 位（46 号交付说明），无法按 16 位核对，
# 因此不放进 FROZEN_EXPECT，改由 46 号证据目录里的落盘记录承担；本脚本对它们
# 只做"是否在本轮前后变化"的对比（见 --compare 用法说明）。

PIP_PACKAGES = ("jieba", "requests", "python-docx", "playwright", "streamlit", "pyinstaller")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def excluded(path: Path) -> bool:
    return any(part in EXCLUDE_PARTS for part in path.parts)


def collect_files() -> dict:
    seen: dict = {}
    for pattern in INCLUDE_PATTERNS:
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file() or excluded(path):
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in seen:
                continue
            seen[rel] = {"sha256": sha256_of(path), "size": path.stat().st_size}
    return dict(sorted(seen.items()))


def pip_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:                                          # noqa: BLE001
        return None


def cmd_version(cmd: str) -> str | None:
    exe = shutil.which(cmd)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return text[0] if text else None
    except Exception:                                          # noqa: BLE001
        return None


def collect_toolchain() -> dict:
    return {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "node": cmd_version("node"),
        "npm": cmd_version("npm"),
        "os": platform.platform(),
        "packages": {name: pip_version(name) for name in PIP_PACKAGES},
    }


def check_frozen(files: dict) -> dict:
    out = {}
    for rel, expect in FROZEN_EXPECT.items():
        actual = files.get(rel, {}).get("sha256", "")
        out[rel] = {
            "expect16": expect,
            "actual16": actual[:16].upper(),
            "match": actual[:16].upper() == expect.upper(),
        }
    return out


def compare_with(old: dict, files: dict) -> dict:
    """与上一份基线对比，回答"本轮到底改了哪些文件"。

    这是用户协作约定里最容易被搞错的一条：不能凭记忆说"我只改了两个文件"，
    必须用哈希对拍列出来；同时也能抓出"顺手改了别的"或"漏报"。
    """
    old_files = {k: v.get("sha256") for k, v in (old.get("files") or {}).items()}
    new_files = {k: v.get("sha256") for k, v in files.items()}
    changed = sorted(k for k, v in new_files.items() if k in old_files and old_files[k] != v)
    added = sorted(k for k in new_files if k not in old_files)
    removed = sorted(k for k in old_files if k not in new_files)
    return {
        "old_generated_at": old.get("generated_at", ""),
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged": len(new_files) - len(changed) - len(added),
    }


def render_md(baseline: dict) -> str:
    tc = baseline["toolchain"]
    lines = [
        "# 开工基线（47 号方案第二节）",
        "",
        f"> 采集时间：{baseline['generated_at']}",
        f"> 采集脚本：`tools/record_baseline.py`（只读）",
        f"> 仓库：`{baseline['repo']}`",
        "",
        "## 工具链",
        "",
        f"- Python：{tc['python']}（`{tc['python_exe']}`）",
        f"- Node：{tc['node'] or '未找到'}；npm：{tc['npm'] or '未找到'}",
        f"- 系统：{tc['os']}",
        "",
        "| 包 | 版本 |",
        "|---|---|",
    ]
    for name, ver in tc["packages"].items():
        lines.append(f"| {name} | {ver or '**未安装**'} |")

    lines += ["", "## 冻结锚点核对（前 16 位）", "",
              "| 文件 | 预期 | 实测 | 结论 |", "|---|---|---|---|"]
    for rel, info in baseline["frozen"].items():
        lines.append(f"| `{rel}` | `{info['expect16']}` | `{info['actual16']}` | "
                     f"{'一致' if info['match'] else '**MISMATCH**'} |")

    diff = baseline.get("diff")
    if diff:
        lines += ["", "## 与上一份基线的差异（本轮真正改动的文件）", "",
                  f"- 对比对象：{diff['old_generated_at']} 的基线",
                  f"- 改动：{len(diff['changed'])} 个；新增：{len(diff['added'])} 个；"
                  f"删除：{len(diff['removed'])} 个；未变：{diff['unchanged']} 个",
                  "", "| 类型 | 文件 |", "|---|---|"]
        for rel in diff["changed"]:
            lines.append(f"| 改动 | `{rel}` |")
        for rel in diff["added"]:
            lines.append(f"| 新增 | `{rel}` |")
        for rel in diff["removed"]:
            lines.append(f"| 删除 | `{rel}` |")

    lines += ["", f"## 文件清单（{len(baseline['files'])} 个）", "",
              "| 文件 | SHA-256 | 字节 |", "|---|---|---|"]
    for rel, info in baseline["files"].items():
        lines.append(f"| `{rel}` | `{info['sha256']}` | {info['size']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="开工/收工基线记录（只读）")
    ap.add_argument("--out-dir", required=True, help="记录输出目录（通常是证据目录）")
    ap.add_argument("--compare", default="", help="与上一份 baseline.json 对比，列出本轮真正变化的文件")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files()
    baseline = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by": "tools/record_baseline.py",
        "repo": str(REPO),
        "note": "47 号方案第二节要求的开工记录：文件清单 + SHA-256 + 工具链 + 冻结锚点核对。",
        "toolchain": collect_toolchain(),
        "frozen": check_frozen(files),
        "file_count": len(files),
        "files": files,
    }

    json_path = out_dir / "baseline.json"
    md_path = out_dir / "baseline.md"
    if args.compare:
        old_path = Path(args.compare)
        if not old_path.exists():
            raise SystemExit(f"找不到要对比的基线：{old_path}")
        old = json.loads(old_path.read_text(encoding="utf-8"))
        diff = compare_with(old, files)
        baseline["compared_with"] = str(old_path)
        baseline["diff"] = diff
        print(f"[baseline] 对比 {old_path.name}：改动 {len(diff['changed'])}、"
              f"新增 {len(diff['added'])}、删除 {len(diff['removed'])}、未变 {diff['unchanged']}",
              flush=True)
    json_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(baseline), encoding="utf-8", newline="\n")

    mismatch = [rel for rel, info in baseline["frozen"].items() if not info["match"]]
    print(f"[baseline] 文件 {len(files)} 个 → {json_path}", flush=True)
    print(f"[baseline] 冻结锚点 {len(baseline['frozen'])} 项，MISMATCH {len(mismatch)} 项"
          + (f"：{', '.join(mismatch)}" if mismatch else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
