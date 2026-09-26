# -*- coding: utf-8 -*-
"""RAG 问答助手 —— 核心模块（多知识库）

整体流程：
  1. load_documents(corpus_id)  读取语料 → 切分 → 每段带出处
  2. BM25(docs)                 对每段分词，统计词频/文档频率，建 BM25 索引
  3. bm25.search(query, k)      输入问题，返回最相关的 Top-K 段 + 出处
  4. retrieve(...)              便捷封装
  5. generate_answer(...)       把命中的段拼成上下文，调大模型生成带引用的回答

两个知识库（互相隔离，不串库）：
  power_demo   电力示例库      data/regs/*.txt            段落式切分，id = {文件}#P{段}
  rag_learning RAG 技术学习库  data/rag_learning/...      标题感知切分，id = {source_id}#s{节}p{段}

chunk 通用字段：
  id / corpus_id / source_id / source_title / source_org / source_url
  / file / section（标题层级路径）/ para / text（正文）/ search_text（含标题，供检索）/ tokens

依赖说明：
  - 检索层：标准库 + jieba（可选，缺失自动退回字符 bigram）
  - 生成层：需要 requests + DeepSeek API key（环境变量 DEEPSEEK_API_KEY）

设计原则：刻意不引入 LangChain 等重框架，每行都能讲清，方便面试时逐层解释。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

try:
    import jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

# ---------------- 可调参数（面试时可以说"这里我调过，结果会变"） ----------------
CHUNK_MAX_CHARS = 400   # 单个 chunk 的最大字符数，超过会再切
TOP_K = 3               # 检索返回的最相关段落数
BM25_K1 = 1.5           # BM25 词频饱和参数
BM25_B = 0.75           # BM25 长度归一化参数

# ---- 生成上下文组装（不改检索与评测口径，只在"喂给模型"这一步做补全） ----
CONTEXT_NEIGHBOR_RADIUS = 2   # 每个真实命中在「同节」内左右各补几个 chunk
CONTEXT_MAX_CHARS = 4000      # 补充片段的字符预算；真实命中不受此限制

# 分词器与切分参数会写进评测报告：换了它们，指标就不可比
TOKENIZER_NAME = "jieba" if _HAS_JIEBA else "char-bigram"

# ---------------- 路径 ----------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "regs"          # 兼容旧引用：默认（电力示例库）语料目录
INDEX_DIR = BASE_DIR / "index"
EVAL_DIR = BASE_DIR / "eval"
OUTPUT_DIR = BASE_DIR / "output"

RAG_LEARNING_DIR = BASE_DIR / "data" / "rag_learning"

# ---------------- 知识库注册表 ----------------
POWER_DEMO = "power_demo"
RAG_LEARNING = "rag_learning"
DEFAULT_CORPUS = POWER_DEMO

CORPORA: dict = {
    POWER_DEMO: {
        "id": POWER_DEMO,
        "name": "电力示例库",
        "note": "示例语料（约 3.4 KB，无来源元数据），仅用于演示流程跑通",
        "kind": "paragraph",
        "dir": DATA_DIR,
        "glob": "*.txt",
        "manifest": None,
        "questions": EVAL_DIR / "questions.json",
        "index": INDEX_DIR / "chunks.json",
        "report": OUTPUT_DIR / "eval_report_power_demo.json",
    },
    RAG_LEARNING: {
        "id": RAG_LEARNING,
        "name": "RAG 技术学习库",
        "note": "Datawhale《All-in-RAG》第一批 10 篇，带来源 / 版本 / 许可元数据",
        "kind": "section",
        "dir": RAG_LEARNING_DIR / "documents",
        "glob": "*.txt",
        "manifest": RAG_LEARNING_DIR / "corpus_manifest.json",
        "questions": EVAL_DIR / "questions_rag_learning.json",
        "index": INDEX_DIR / "chunks_rag_learning.json",
        "report": OUTPUT_DIR / "eval_report_rag_learning.json",
    },
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def corpus_ids() -> list:
    """返回知识库 id 列表（顺序稳定）。"""
    return list(CORPORA.keys())


def corpus_name(corpus_id: str) -> str:
    return get_corpus(corpus_id)["name"]


def get_corpus(corpus_id: str | None = None) -> dict:
    cid = corpus_id or DEFAULT_CORPUS
    if cid not in CORPORA:
        raise KeyError(f"未知知识库：{cid}（可选：{', '.join(CORPORA)}）")
    return CORPORA[cid]


# ---------------- 分词 ----------------
def tokenize(text: str) -> list:
    """中文分词；没有 jieba 时退回字符 bigram（保证零依赖也能跑）。"""
    if _HAS_JIEBA:
        return [t for t in jieba.lcut(text) if t.strip()]
    t = re.sub(r"\s+", "", text)
    return [t[i:i + 2] for i in range(len(t) - 1)]


# ---------------- 第 1 步：文档切分 ----------------
def _split_sections(text: str) -> list:
    """按 Markdown 标题切分，返回 [(标题层级路径, 正文)]，代码块内的 # 行不算标题。"""
    sections: list = []
    stack: list = []          # [(level, title), ...]
    body: list = []
    in_fence = False

    def flush() -> None:
        if stack:
            path = " > ".join(t for _, t in stack)
            sections.append((path, "\n".join(body).strip()))

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            body = []
            continue
        body.append(line)
    flush()
    return sections


LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _is_list_block(text: str) -> bool:
    return bool(LIST_RE.match(text.splitlines()[0])) if text.strip() else False


def _split_paragraphs(body: str) -> list:
    """按空行切块，但保持两种边界完整：

    - 代码块：围栏内即使有空行也不切开（否则会出现半个代码块）；
    - 列表：相邻的列表块合并成一个语义单元，避免把一份列表拆成十几条碎片。
      非列表内容仍按「空行 = 段落边界」处理，与历史行为一致。
    """
    blocks: list = []
    cur: list = []
    in_fence = False

    def flush() -> None:
        if cur:
            text = "\n".join(cur).strip()
            if text:
                blocks.append(text)
        cur.clear()

    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            cur.append(line)
            continue
        if in_fence:
            cur.append(line)
            continue
        if not line.strip():
            flush()
            continue
        cur.append(line)
    flush()

    merged: list = []
    for b in blocks:
        if merged and _is_list_block(b) and _is_list_block(merged[-1]):
            merged[-1] = merged[-1] + "\n" + b
        else:
            merged.append(b)
    return merged


def _chunks_of(para: str) -> list:
    """超长段落按固定字数再切（本轮不做 overlap / 语义边界，属已知局限）。"""
    if not para:
        return []
    return [para[j:j + CHUNK_MAX_CHARS] for j in range(0, len(para), CHUNK_MAX_CHARS)]


def _load_paragraph_corpus(corpus: dict) -> list:
    """段落式切分（电力示例库的历史行为，保持一致以便历史结果可复现）。"""
    dirp: Path = corpus["dir"]
    if not dirp.exists():
        raise FileNotFoundError(f"语料目录不存在：{dirp}")
    docs: list = []
    for fp in sorted(dirp.glob(corpus["glob"])):
        raw = fp.read_text(encoding="utf-8")
        paras = _split_paragraphs(raw)
        for i, para in enumerate(paras):
            chunks = _chunks_of(para)
            for k, chunk in enumerate(chunks):
                suffix = f"_{k + 1}" if len(chunks) > 1 else ""
                docs.append({
                    "id": f"{fp.stem}#P{i + 1}{suffix}",
                    "corpus_id": corpus["id"],
                    "source_id": fp.stem,
                    "source_title": fp.stem,
                    "source_org": "",
                    "source_url": "",
                    "file": fp.name,
                    "section": "",
                    "para": i + 1,
                    "text": chunk,
                    "search_text": chunk,      # 无标题层级，检索文本即正文
                    "tokens": tokenize(chunk),
                })
    return docs


def _load_section_corpus(corpus: dict) -> list:
    """标题感知切分：先按标题分节，再按段落切，最后按字数兜底。

    - chunk id 形如 {source_id}#s{节号}p{段号}，只由「文档内容 + 排序」决定，
      与文件遍历顺序无关，重复运行稳定可复现；
    - 检索文本带上标题层级，便于 BM25 命中"章节名"这类词；
    - 展示文本只保留正文，避免界面里重复堆标题。
    """
    manifest_path: Path = corpus["manifest"]
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"找不到来源清单：{manifest_path}\n"
            f"请先运行：python tools/import_rag_corpus.py"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    meta = {d["source_id"]: d for d in manifest["documents"]}

    docs: list = []
    for sid in sorted(meta):                      # 排序 → 与遍历顺序无关
        entry = meta[sid]
        fp = corpus["dir"] / Path(entry["file"]).name
        if not fp.exists():
            raise FileNotFoundError(f"来源清单声明了 {entry['file']}，但文件不存在：{fp}")
        raw = fp.read_text(encoding="utf-8")

        for si, (path, body) in enumerate(_split_sections(raw), 1):
            if not body:
                continue                          # 只有标题没有正文的空节，跳过
            # 标题层级路径去掉文档大标题那一层，界面展示更清爽
            parts = [p.strip() for p in path.split(" > ")]
            shown = " > ".join(parts[1:]) if len(parts) > 1 else parts[0]
            for pi, para in enumerate(_split_paragraphs(body), 1):
                chunks = _chunks_of(para)
                for k, ch in enumerate(chunks):
                    suffix = f"-{k + 1}" if len(chunks) > 1 else ""
                    docs.append({
                        "id": f"{sid}#s{si}p{pi}{suffix}",
                        "corpus_id": corpus["id"],
                        "source_id": sid,
                        "source_title": entry["title"],
                        "source_org": entry["author_or_org"],
                        "source_url": entry["source_url"],
                        "file": fp.name,
                        "section": shown,
                        "para": pi,
                        "text": ch,
                        "search_text": f"{path}\n{ch}",
                        "tokens": tokenize(f"{path}\n{ch}"),
                    })
    return docs


def load_documents(corpus_id: str | None = None) -> list:
    """读取指定知识库的语料并切分成 chunk 列表。"""
    corpus = get_corpus(corpus_id)
    loader = _load_section_corpus if corpus["kind"] == "section" else _load_paragraph_corpus
    docs = loader(corpus)
    if not docs:
        raise ValueError(f"知识库「{corpus['name']}」没有可用语料：{corpus['dir']}")
    return docs


# ---------------- 第 2 步：BM25 索引与检索 ----------------
class BM25:
    """极简 BM25 实现，零第三方依赖，方便逐行讲清原理。

    BM25 打分思想：一个词在查询中出现越多、在全库越稀有（逆文档频率高）、
    在当前段出现越频繁，该段得分越高。
    """

    def __init__(self, docs: list):
        self.docs = docs
        self.n = len(docs)
        self.avgdl = sum(len(d["tokens"]) for d in docs) / self.n
        self.df = Counter()
        for d in docs:
            for t in set(d["tokens"]):
                self.df[t] += 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def _score(self, query_tokens: list, doc: dict) -> float:
        tf = Counter(doc["tokens"])
        dl = len(doc["tokens"])
        score = 0.0
        for t in set(query_tokens):
            if self.df.get(t, 0) == 0:
                continue
            f = tf.get(t, 0)
            denom = f + BM25_K1 * (1 - BM25_B + BM25_B * dl / self.avgdl)
            score += self._idf(t) * (f * (BM25_K1 + 1)) / denom
        return score

    def search(self, query: str, k: int = TOP_K) -> list:
        """返回 [(score, doc), ...]，按得分降序，过滤掉零分。"""
        qt = tokenize(query)
        scored = [(self._score(qt, d), d) for d in self.docs]
        scored.sort(key=lambda x: -x[0])
        return [(s, d) for s, d in scored[:k] if s > 0]


# ---------------- 第 3 步：检索封装 ----------------
def build_index(corpus_id: str | None = None) -> BM25:
    return BM25(load_documents(corpus_id))


def retrieve(query: str, top_k: int = TOP_K, corpus_id: str | None = None) -> list:
    return build_index(corpus_id).search(query, top_k)


# ---------------- 知识库统计与指纹 ----------------
def corpus_fingerprint(corpus_id: str | None = None) -> str:
    """语料指纹：**文件内容哈希** + 分词器 + 切分参数。

    这里刻意用字节内容求 SHA-256，而不是 mtime/大小：否则"改成同样字节数的内容
    再把 mtime 改回去"这种边界下指纹不变，Streamlit 缓存就不会失效（复审 F5 的反例）。
    """
    corpus = get_corpus(corpus_id)
    h = hashlib.sha256()
    h.update(f"{corpus['id']}|{TOKENIZER_NAME}|{CHUNK_MAX_CHARS}".encode("utf-8"))

    if corpus["manifest"]:
        # 标题感知库：以 manifest 声明的文件为准，避免目录里无关文件造成误失效
        manifest_path = Path(corpus["manifest"])
        h.update(b"|manifest|")
        h.update(manifest_path.read_bytes())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in sorted(manifest["documents"], key=lambda d: d["source_id"]):
            fp = corpus["dir"] / Path(entry["file"]).name
            h.update(f"|doc:{entry['source_id']}|".encode("utf-8"))
            if fp.exists():
                h.update(hashlib.sha256(fp.read_bytes()).digest())
    else:
        for fp in sorted(corpus["dir"].glob(corpus["glob"])):
            h.update(f"|{fp.name}|".encode("utf-8"))
            h.update(hashlib.sha256(fp.read_bytes()).digest())
    return h.hexdigest()


def corpus_version(corpus_id: str | None = None) -> str:
    """语料版本：学习库用上游 commit；示例库没有版本，显式说明。"""
    corpus = get_corpus(corpus_id)
    if corpus["manifest"] and Path(corpus["manifest"]).exists():
        m = json.loads(Path(corpus["manifest"]).read_text(encoding="utf-8"))
        up = m.get("upstream", {})
        return f"{up.get('commit_short', 'unknown')}（{up.get('license', '')}）"
    return "无版本（示例语料）"


def corpus_stats(corpus_id: str | None = None) -> dict:
    """界面与报告用：来源数、chunk 数、总字数、版本、切分参数。"""
    corpus = get_corpus(corpus_id)
    docs = load_documents(corpus["id"])
    return {
        "corpus_id": corpus["id"],
        "name": corpus["name"],
        "note": corpus["note"],
        "source_count": len({d["source_id"] for d in docs}),
        "chunk_count": len(docs),
        "total_chars": sum(len(d["text"]) for d in docs),
        "version": corpus_version(corpus["id"]),
        "tokenizer": TOKENIZER_NAME,
        "fingerprint": corpus_fingerprint(corpus["id"]),
        "params": {
            "chunk_max_chars": CHUNK_MAX_CHARS,
            "top_k_default": TOP_K,
            "bm25_k1": BM25_K1,
            "bm25_b": BM25_B,
        },
    }


# ---------------- 第 3.5 步：生成上下文组装（同节相邻补全，P1） ----------------
def assemble_context(hits: list, corpus_id: str | None = None,
                     radius: int = CONTEXT_NEIGHBOR_RADIUS,
                     max_chars: int = CONTEXT_MAX_CHARS) -> dict:
    """把「真实检索命中」组装成生成用上下文，并按需补齐**同节相邻**片段。

    为什么需要它：BM25 是按 chunk 打分的，命中往往落在章节的**前言**或**总结**上，
    而真正枚举答案的相邻片段（如「上下文相关性 / 忠实度 / 答案相关性」三条）
    可能整段掉在 Top-K 之外 —— 模型于是诚实地说"资料未列出"。

    铁律（防止把"补充"当成"命中"）：
      - 真实命中集合与顺序**原样保留**，本函数不新增、不删除、不重排命中；
      - 只有在**同一 source_id 且同一 section** 内、且位置相邻的 chunk 才会被补进来；
      - 补充片段带 `origin="neighbor"`，界面上与评测口径都不当命中看待；
      - `max_chars` 只约束补充片段：真实命中永远完整保留（否则模型将无据可依）。

    返回 dict：contexts（按"所属分组的最佳命中名次 + 语料内顺序"排列）、
    hits、neighbors、neighbor_count、total_chars、max_chars、radius。
    """
    corpus = get_corpus(corpus_id)
    docs = load_documents(corpus["id"])
    position = {d["id"]: i for i, d in enumerate(docs)}
    hit_ids = [d["id"] for _, d in hits]

    # 分组键 = (source_id, section)；组内记录最佳命中名次，用于排序
    groups: dict = {}
    for rank, (score, d) in enumerate(hits, 1):
        key = (d["source_id"], d.get("section") or "")
        g = groups.setdefault(key, {"best_rank": rank, "items": {}})
        g["best_rank"] = min(g["best_rank"], rank)
        g["items"][position[d["id"]]] = {
            "id": d["id"], "text": d["text"], "section": d.get("section", ""),
            "source_id": d["source_id"], "source_title": d.get("source_title", ""),
            "source_org": d.get("source_org", ""), "source_url": d.get("source_url", ""),
            "score": round(score, 4), "rank": rank, "origin": "hit",
        }

    for rank, (score, d) in enumerate(hits, 1):
        i = position[d["id"]]
        key = (d["source_id"], d.get("section") or "")
        g = groups[key]
        for j in range(max(0, i - radius), min(len(docs), i + radius + 1)):
            nd = docs[j]
            if nd["id"] in hit_ids or j in g["items"]:
                continue
            if nd["source_id"] != d["source_id"]:
                continue
            if (nd.get("section") or "") != (d.get("section") or ""):
                continue          # 只补同节，绝不跨节
            g["items"][j] = {
                "id": nd["id"], "text": nd["text"], "section": nd.get("section", ""),
                "source_id": nd["source_id"], "source_title": nd.get("source_title", ""),
                "source_org": nd.get("source_org", ""), "source_url": nd.get("source_url", ""),
                "score": None, "rank": rank, "origin": "neighbor",
            }

    ordered: list = []
    for key in sorted(groups, key=lambda k: (groups[k]["best_rank"], k)):
        g = groups[key]
        for j in sorted(g["items"]):
            ordered.append(g["items"][j])

    # 真实命中的字符数是硬底；补充片段受 max_chars 约束
    hit_chars = sum(len(e["text"]) for e in ordered if e["origin"] == "hit")
    total = hit_chars
    kept: list = []
    for e in ordered:
        if e["origin"] == "hit":
            kept.append(e)
            continue
        if total + len(e["text"]) > max_chars:
            continue
        total += len(e["text"])
        kept.append(e)

    return {
        "corpus_id": corpus["id"],
        "corpus_name": corpus["name"],
        "contexts": kept,
        "hits": [e for e in kept if e["origin"] == "hit"],
        "neighbors": [e for e in kept if e["origin"] == "neighbor"],
        "neighbor_count": sum(1 for e in kept if e["origin"] == "neighbor"),
        "hit_count": sum(1 for e in kept if e["origin"] == "hit"),
        "hit_chars": hit_chars,
        "total_chars": total,
        "max_chars": max_chars,
        "radius": radius,
    }


def expand_and_assemble(query: str, top_k: int = TOP_K,
                        corpus_id: str | None = None, **kw) -> dict:
    """一步到位：检索 → 组装生成上下文。检索口径不变。"""
    return assemble_context(retrieve(query, top_k=top_k, corpus_id=corpus_id),
                            corpus_id=corpus_id, **kw)


# ---------------- 引用解析（P2：按首次出现顺序去重） ----------------
# 引用 token 的唯一定义：`[id]`，且右方括号**后面不能紧接 `(`**。
# 后半条用于排除 markdown 链接 `[文字](目标)`：链接文字即使恰好等于真实 chunk id，
# 也只是正文里的超链接文字，不代表"回答引用了该资料"。
# extract_refs() 与 count_ref_occurrences() **必须共用这一条规则**，否则
# "展示条数"与"正文出现次数"会对同一段文本给出互相矛盾的解释。
_REF_TOKEN_RE = re.compile(r"\[([^\]\n]{1,120})\](?!\()")

# 2026-09-20 窄修（22 号窄任务）：21 号真实样本里，模型有时会把"来源："也写进方括号，
# 形如 `[来源：示例语料-电力安全通用要点#P5]`（见第 07 题）。这里统一剥掉可选前缀
# （`来源`/`出处` + 可选中/半角冒号 + 空白）后再拿**完整 id** 去比对上下文。
# 只接受这一种前缀，不做"在括号里搜 id"的宽松匹配，避免把无关文字误判成引用。
_REF_PREFIX_RE = re.compile(r"^(?:来源|出处)\s*[:：]?\s*")


def normalize_ref_token(raw: str) -> str:
    """把方括号里的原始文字规范成候选 id：去空白/反引号，再剥掉可选的「来源／出处」前缀。"""
    text = raw.strip().strip("`").strip()
    text = _REF_PREFIX_RE.sub("", text)
    return text.strip().strip("`").strip()


def extract_refs(answer: str, known_ids) -> list:
    """从回答里提取引用 id，**按首次出现顺序去重**，并只保留能对上上下文的 id。

    - 去重：同一 chunk 在回答正文里可以出现多次，"引用对照"只展示一次；
    - 过滤：模型编造的 id、markdown 链接的链接文字（**含链接文字恰好是真实 id** 的情形）
      都不会当成引用；
    - 识别：方括号里带「来源：」前缀的写法（`[来源：<id>]`）按 `[<id>]` 处理；
    - 本函数只影响展示，**不修改回答正文**。
    """
    known = set(known_ids)
    out: list = []
    for m in _REF_TOKEN_RE.finditer(answer or ""):
        rid = normalize_ref_token(m.group(1))
        if rid in known and rid not in out:
            out.append(rid)
    return out


def count_ref_occurrences(answer: str) -> int:
    """回答正文里方括号引用**出现次数**（含重复），用于对比"去重后展示条数"。

    与 `extract_refs()` 共用同一条 token 规则（同样排除 markdown 链接文字），
    因此两者对"什么算一次正文引用"的判断一致；`[来源：<id>]` 记 1 次。
    """
    return len(_REF_TOKEN_RE.findall(answer or ""))


# ---------------- 第 4 步：大模型生成 ----------------
def _normalize_contexts(contexts: list) -> list:
    """兼容两种入参：组装后的 dict 列表，或 [(score, doc)] 检索结果。

    旧调用方（直接传 retrieve 结果）仍可用，此时全部按真实命中处理。
    """
    out: list = []
    for item in contexts:
        if isinstance(item, dict):
            out.append(item)
            continue
        score, d = item
        out.append({
            "id": d["id"], "text": d["text"], "section": d.get("section", ""),
            "source_id": d["source_id"], "source_title": d.get("source_title", ""),
            "source_org": d.get("source_org", ""), "source_url": d.get("source_url", ""),
            "score": round(score, 4), "rank": None, "origin": "hit",
        })
    return out


def build_prompt(question: str, contexts: list, corpus_id: str | None = None) -> str:
    corpus = get_corpus(corpus_id)
    entries = _normalize_contexts(contexts)
    blocks = []
    for d in entries:
        # 补充片段必须自报家门：不能让它看起来像一次检索命中
        tag = "" if d.get("origin") == "hit" else "（同节补充上下文，非检索命中）"
        blocks.append(f"[{d['id']}]{tag} {d['text']}")
    refs = "\n\n".join(blocks)
    return (
        f"你是「{corpus['name']}」的资料问答助手。请【只依据】下面提供的【资料】回答问题，"
        "并在答案中标注引用来源（如 [来源id]）。\n"
        "标了「同节补充上下文」的片段来自同一章节的相邻内容，用于补全同一处列举；"
        "可以引用，但不要把它说成另一次独立检索结果。\n"
        # 2026-09-19 窄改（19 号方案第 1 项）：专治「上下文里已有可用口径，却回答没找到」
        "如果问题在问「从哪几方面 / 哪几个维度」这类评价口径，而资料里已经出现了可用的口径或框架，"
        "就必须按资料原样把该口径列出来并标注来源 id（资料给了几条就列几条），并说明它覆盖的范围；"
        "若资料只支持入门口径（如「检索相关性 / 生成质量」），就说明这是该资料的入门口径，"
        "不要把它硬凑成严格的三元组（上下文相关性 / 忠实度 / 答案相关性）。\n"
        "只有当资料确实没有与问题相关的信息时，才回答「资料中未找到相关依据」；"
        "不要因为资料没有用提问里的原词表述，就当作没有足够信息。不要编造。\n\n"
        f"【资料】\n{refs}\n\n"
        f"【问题】\n{question}\n\n"
        "【回答】"
    )


def generate_answer(question: str, contexts: list,
                    api_key: str, base_url: str, model: str,
                    corpus_id: str | None = None) -> str:
    import requests
    prompt = build_prompt(question, contexts, corpus_id)
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def answer(question: str, api_key: str | None = None,
           corpus_id: str | None = None) -> dict:
    """端到端：检索 →（可选）组装同节上下文 →（可选）生成。

    返回结构里 `hits` 是真实 BM25 命中（口径不变），`neighbors` 是生成时才补进来的
    同节相邻片段；两者分开返回，调用方不得把 neighbors 当成检索结果展示。
    """
    cid = corpus_id or DEFAULT_CORPUS
    assembled = expand_and_assemble(question, corpus_id=cid)
    result = {
        "question": question,
        "corpus_id": cid,
        "hits": assembled["hits"],
        "neighbors": assembled["neighbors"],
        "contexts": assembled["contexts"],
        "context_chars": assembled["total_chars"],
    }
    if api_key:
        from config import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
        result["answer"] = generate_answer(question, assembled["contexts"], api_key,
                                           DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, cid)
    return result
