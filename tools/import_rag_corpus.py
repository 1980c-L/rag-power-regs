# -*- coding: utf-8 -*-
"""从 Datawhale《All-in-RAG》本地克隆导入「RAG 技术学习库」语料。

为什么要有这个脚本（而不是直接把 md 拷进 data/）：
  1. 来源可追溯：每份资料的 source_id / 标题 / 原始 URL / 仓库相对路径 /
     commit SHA / 获取日期 / 许可证 / 是否允许再分发，全部写进 corpus_manifest.json；
  2. 可重复执行：只要本地克隆在钉扎 commit 上，任何人重跑都能得到同样的语料
     （每份文件与 manifest 都带 sha256，可逐字节核对）；
  3. 清洗可审计：去图片/HTML/纯链接噪声，丢掉「参考文献 / 练习」类尾部章节，
     保留标题、段落、列表与代码块边界。

三种校验（**名字即语义，不要混用**）：
    --self-check       交付文档 vs 交付 manifest 的 sha256。**只证明交付物自洽**，
                       本地克隆不存在也能 PASS，不涉及上游。
    --upstream-check   在钉扎 commit 上**重新清洗上游文件**，与交付文档逐字节比较。
                       克隆不存在或 commit 不匹配 → 直接失败（非零退出码）。
    --check            兼容用别名，等价于 --self-check；会打印提示说明它不校验上游。

许可证与再分发（重要，已核实）：
  上游仓库根目录没有 LICENSE 文件，但 README 明确声明采用
  **CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享 4.0 国际）**。
  含义：允许复制与再分发，但必须 ①署名 ②非商业用途 ③衍生作品以相同协议共享。
  本作品为个人学习与求职展示（非商业），已在 README 与 manifest 中署名并标明协议。
  若将来用于商业用途，必须重新评估。

用法：
    python tools/import_rag_corpus.py                  # 导入（校验 commit）
    python tools/import_rag_corpus.py --self-check     # 交付物自校验
    python tools/import_rag_corpus.py --upstream-check # 上游钉扎重建校验
    python tools/import_rag_corpus.py --repo <路径>    # 指定本地克隆位置
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------- 上游钉扎信息（来源可追溯的关键） ----------------
REPO_URL = "https://github.com/datawhalechina/all-in-rag"
PINNED_COMMIT = "583a61b09869bc3afc4552289171f6ec188f2c76"
PINNED_COMMIT_SHORT = PINNED_COMMIT[:7]
FETCHED_AT = "2026-09-18"
LICENSE_NAME = "CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享 4.0 国际）"
LICENSE_NOTE = (
    "上游仓库根目录未提供 LICENSE 文件，但 README.md「许可证」一节明确声明采用 "
    "CC BY-NC-SA 4.0。允许再分发，条件是署名 Datawhale、非商业用途、"
    "衍生作品以相同协议共享。本作品为本机个人学习与求职展示用途，非商业。"
)
REDISTRIBUTABLE = "yes-with-conditions"

# ---------------- 语料选择：第一批 10 篇（对齐方案里的 10 个候选主题） ----------------
# (source_id, 主题, 仓库相对路径)
DOCUMENTS = [
    ("air-rag-intro", "RAG 基本流程", "docs/chapter1/01_RAG_intro.md"),
    ("air-get-start", "四步构建 RAG（流程对照）", "docs/chapter1/03_get_start_rag.md"),
    ("air-data-load", "文档加载与清洗", "docs/chapter2/04_data_load.md"),
    ("air-text-chunking", "文本分块与 overlap", "docs/chapter2/05_text_chunking.md"),
    ("air-vector-embedding", "embedding 与向量检索", "docs/chapter3/06_vector_embedding.md"),
    ("air-hybrid-search", "稀疏检索(BM25) 与混合检索", "docs/chapter4/11_hybrid_search.md"),
    ("air-rerank", "retrieve-and-rerank", "docs/chapter4/15_advanced_retrieval_techniques.md"),
    ("air-formatted-generation", "生成质量、引用与拒答边界", "docs/chapter5/16_formatted_generation.md"),
    ("air-evaluation", "RAG 检索评测", "docs/chapter6/18_system_evaluation.md"),
    ("air-eval-tools", "评测工具与指标", "docs/chapter6/19_common_tools.md"),
]

# 只丢弃明确的非正文尾部章节；其余原样保留，避免"清洗"变成"改写"
DROP_SECTIONS = {"参考文献", "参考链接", "参考资料", "练习"}

BASE_DIR = Path(__file__).resolve().parent.parent          # rag-power-regs/
OUT_DIR = BASE_DIR / "data" / "rag_learning"
DOCS_DIR = OUT_DIR / "documents"
MANIFEST = OUT_DIR / "corpus_manifest.json"
DEFAULT_REPO = BASE_DIR.parent / "rag-学习资料" / "all-in-rag"

CORPUS_ID = "rag_learning"
CORPUS_NAME = "RAG 技术学习库"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


# ---------------- 文本清洗 ----------------
def convert_html_tables(text: str) -> str:
    """把 <table> 块还原成 markdown 表格行，避免单元格被拍平成缩进行。"""
    def repl(m: re.Match) -> str:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), flags=re.S | re.I)
        lines = []
        for r in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    return re.sub(r"<table[^>]*>.*?</table>", repl, text, flags=re.S | re.I)


def strip_inline_noise(line: str) -> str:
    """去掉图片、HTML 标签、把 markdown 链接收敛成可读文本。"""
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)          # 图片
    line = re.sub(r"</?[A-Za-z][^>]*>", "", line)            # HTML 标签
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)     # [文本](链接) → 文本
    return line.rstrip()


def clean_markdown(text: str) -> tuple:
    """清洗 + 丢弃尾部噪声章节。

    返回 (清洗后文本, 保留的一级/二级章节标题列表)。
    代码块边界被完整保留，围栏内的 # 行不会被当成标题。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = convert_html_tables(text)
    out_lines: list = []
    kept_headings: list = []
    skip_level = None          # 非空时表示正在丢弃某个章节
    in_fence = False

    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            if skip_level is None:
                out_lines.append(raw)
            continue

        m = None if in_fence else HEADING_RE.match(raw)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            # 前缀匹配：上游标题常带括号补充，如「练习（可利用大模型辅助完成）」
            if any(title.startswith(k) for k in DROP_SECTIONS):
                skip_level = level
                continue
            if skip_level is not None:
                if level > skip_level:
                    continue          # 仍在被丢弃的章节内部
                skip_level = None     # 同级或更高级标题 → 恢复正常收录
            if level <= 2:
                kept_headings.append(title)
            out_lines.append(raw)
            continue

        if skip_level is not None:
            continue
        out_lines.append(strip_inline_noise(raw))

    cleaned = "\n".join(out_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n", kept_headings


def extract_title(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    return m.group(1).strip() if m else ""


def count_sections(text: str) -> int:
    """统计标题数（忽略代码块内的 # 行）。"""
    n, in_fence = 0, False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and HEADING_RE.match(line):
            n += 1
    return n


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------- 上游 commit 校验 ----------------
def read_repo_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def require_pinned_repo(repo: Path) -> tuple:
    """返回 (ok, commit)。克隆不存在或 commit 不符都视为失败，不做静默降级。"""
    if not repo.exists():
        print(f"[BLOCKED] 找不到本地克隆：{repo}")
        print("          请先克隆：git -c http.proxy= -c https.proxy= clone "
              f"{REPO_URL} \"{repo}\"")
        return False, ""
    actual = read_repo_commit(repo)
    if actual != PINNED_COMMIT:
        print("[BLOCKED] 上游 commit 与钉扎值不一致，拒绝继续（来源无法固定）。")
        print(f"          期望 {PINNED_COMMIT}")
        print(f"          实际 {actual or '(读不到，可能不是 git 仓库)'}")
        return False, actual
    return True, actual


# ---------------- 生成语料 ----------------
def build_entries(repo: Path, out_dir: Path) -> list:
    """按清洗规则重建语料到 out_dir，返回来源条目列表（含 sha256）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list = []

    for source_id, topic, repo_rel in DOCUMENTS:
        src = repo / repo_rel
        if not src.exists():
            raise FileNotFoundError(f"缺少上游文件：{repo_rel}")

        raw = src.read_text(encoding="utf-8", errors="ignore")
        cleaned, headings = clean_markdown(raw)
        title = extract_title(raw) or src.stem

        out = out_dir / f"{source_id}.txt"
        out.write_text(cleaned, encoding="utf-8")   # 正文本身以「# 原始标题」开头

        body = out.read_text(encoding="utf-8")
        entries.append({
            "source_id": source_id,
            "topic": topic,
            "title": title,
            "author_or_org": "Datawhale · all-in-rag 教程项目",
            "source_url": f"{REPO_URL}/blob/{PINNED_COMMIT}/{repo_rel}",
            "repo_path": repo_rel,
            "repo_url": REPO_URL,
            "commit": PINNED_COMMIT,
            "commit_short": PINNED_COMMIT_SHORT,
            "fetched_at": FETCHED_AT,
            "license": LICENSE_NAME,
            "license_note": LICENSE_NOTE,
            "redistributable": REDISTRIBUTABLE,
            "file": f"documents/{out.name}",
            "chars": len(body),
            "sha256": sha256_of(out),
            "section_count": count_sections(body),
            "top_sections": headings,
        })
        print(f"[OK] {source_id:24s} {len(body):6d} 字符  {count_sections(body):2d} 个章节")
    return entries


def build_manifest(entries: list) -> dict:
    return {
        "corpus_id": CORPUS_ID,
        "corpus_name": CORPUS_NAME,
        "created_at": FETCHED_AT,
        "description": "RAG 技术学习库：来自 Datawhale《All-in-Rag》教程正文的第一批 10 篇资料。",
        "upstream": {
            "repo_url": REPO_URL,
            "commit": PINNED_COMMIT,
            "commit_short": PINNED_COMMIT_SHORT,
            "fetched_at": FETCHED_AT,
            "license": LICENSE_NAME,
            "license_source": f"{REPO_URL}/blob/{PINNED_COMMIT}/README.md（「许可证」一节）",
            "license_note": LICENSE_NOTE,
            "redistributable": REDISTRIBUTABLE,
        },
        "cleaning": {
            "removed": ["HTML 注释", "图片", "HTML 标签", "纯链接包装（保留链接文字）"],
            "dropped_sections": sorted(DROP_SECTIONS),
            "kept": ["标题层级", "段落", "列表", "表格", "代码块（含围栏边界）"],
        },
        "document_count": len(entries),
        "total_chars": sum(e["chars"] for e in entries),
        "documents": entries,
    }


def do_import(repo: Path) -> int:
    ok, _ = require_pinned_repo(repo)
    if not ok:
        return 3
    entries = build_entries(repo, DOCS_DIR)
    manifest = build_manifest(entries)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] {len(entries)} 篇 / {manifest['total_chars']} 字符")
    print(f"[OK] 来源清单 → {MANIFEST}")
    return 0


# ---------------- 校验 1：交付物自校验（不涉及上游） ----------------
def self_check() -> int:
    """交付文档 vs 交付 manifest 的 sha256。**本地克隆不存在也能 PASS。**"""
    if not MANIFEST.exists():
        print(f"[BLOCKED] 找不到 {MANIFEST}，请先执行导入。")
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    for e in manifest["documents"]:
        fp = OUT_DIR / e["file"]
        if not fp.exists():
            print(f"[FAIL] 缺文件 {e['file']}")
            bad += 1
            continue
        got = sha256_of(fp)
        if got != e["sha256"]:
            print(f"[FAIL] {e['source_id']} sha256 不符：{got[:12]} != {e['sha256'][:12]}")
            bad += 1
        else:
            print(f"[PASS] {e['source_id']:24s} sha256 {got[:12]}… 一致")
    print(f"\n[{'PASS' if bad == 0 else 'FAIL'}] 自校验："
          f"{len(manifest['documents']) - bad}/{len(manifest['documents'])} 份与交付 manifest 一致")
    print("      注意：自校验只证明交付物内部自洽，不证明它等于钉扎上游的重建结果。")
    return 0 if bad == 0 else 5


# ---------------- 校验 2：上游钉扎重建校验 ----------------
def upstream_check(repo: Path) -> int:
    """在钉扎 commit 上重新清洗上游文件，与交付文档逐字节比较。"""
    ok, _ = require_pinned_repo(repo)
    if not ok:
        return 3
    if not MANIFEST.exists():
        print(f"[BLOCKED] 找不到 {MANIFEST}，无法比较。")
        return 2

    delivered = json.loads(MANIFEST.read_text(encoding="utf-8"))
    delivered_by_id = {e["source_id"]: e for e in delivered["documents"]}

    tmp_root = Path(tempfile.mkdtemp(prefix="rag-corpus-rebuild-"))
    try:
        print(f"[INFO] 在临时目录重建：{tmp_root}")
        rebuilt = build_entries(repo, tmp_root / "documents")
    finally:
        pass

    bad = 0
    try:
        rebuilt_ids = {e["source_id"] for e in rebuilt}
        if rebuilt_ids != set(delivered_by_id):
            print(f"[FAIL] 文档集合不一致：重建 {sorted(rebuilt_ids)} "
                  f"vs 交付 {sorted(delivered_by_id)}")
            bad += 1
        for e in rebuilt:
            d = delivered_by_id.get(e["source_id"])
            if d is None:
                continue
            if e["sha256"] != d["sha256"]:
                print(f"[FAIL] {e['source_id']} 与上游重建不一致："
                      f"重建 {e['sha256'][:12]} vs 交付 {d['sha256'][:12]}")
                bad += 1
            else:
                print(f"[PASS] {e['source_id']:24s} 与上游重建逐字节一致 {e['sha256'][:12]}…")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n[{'PASS' if bad == 0 else 'FAIL'}] 上游钉扎重建校验："
          f"{len(rebuilt) - bad}/{len(rebuilt)} 份与交付文档一致"
          f"（上游 commit {PINNED_COMMIT_SHORT}）")
    return 0 if bad == 0 else 6


def main() -> int:
    ap = argparse.ArgumentParser(description="导入 / 校验 RAG 技术学习库语料")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="all-in-rag 本地克隆路径")
    ap.add_argument("--self-check", action="store_true",
                    help="交付文档 vs 交付 manifest（不校验上游）")
    ap.add_argument("--upstream-check", action="store_true",
                    help="在钉扎 commit 上重建上游语料并逐字节比较（克隆/commit 不符即失败）")
    ap.add_argument("--check", action="store_true",
                    help="兼容别名：等价于 --self-check")
    args = ap.parse_args()

    if args.check:
        print("[INFO] --check 是兼容别名，本次等价于 --self-check（不校验上游）。"
              "需要校验上游请用 --upstream-check。")
    if args.self_check or args.check:
        return self_check()
    if args.upstream_check:
        return upstream_check(Path(args.repo))
    return do_import(Path(args.repo))


if __name__ == "__main__":
    sys.exit(main())
