# -*- coding: utf-8 -*-
"""为第三版独立前端生成 mock 数据（28 号方案第一阶段）。

为什么要生成而不是手写：28 号「本轮不做」明确要求**不为展示伪造 BM25 分数、引用 id、
资料数量**。本脚本直接调用 `rag_core.py` 取真实检索结果与真实统计，回答正文取自
21 号证据里**已记录的真实回答原文**（通过 --answers-dir 传入目录，逐字节写入，不手抄）。

全程离线：只做切分 / BM25 检索 / 同节补全 / 引用解析，**不调用任何模型接口**。

产出结构按 28 号约定的未来接口字段组织（settings / run / answer / evidence 四组），
第二阶段把 mockService 换成 apiService 时页面组件不用改。

运行：
    python tools/build_frontend_mocks.py --answers-dir <21 号证据目录> [--out <输出 json>]
本文件内不写任何个人绝对路径。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import rag_core as rc                                        # noqa: E402

DEFAULT_OUT = REPO / "frontend_v3" / "src" / "mocks" / "api_snapshots.json"

# 场景表：key → (知识库, 问题, Top-K, 回答来源文件名 或 None)
SCENARIOS = [
    ("learning_triad", rc.RAG_LEARNING, "RAG 评估三元组包含哪三个维度？", 3, "01-三元组.json"),
    ("power_grounding", rc.POWER_DEMO, "装设接地线的顺序是什么？", 3, "07-接地线.json"),
    ("learning_no_refs", rc.RAG_LEARNING, "红烧肉怎么做？", 3, "03-红烧肉.json"),
    ("power_zero", rc.POWER_DEMO, "红烧肉怎么做？", 3, None),
]

# 页面允许选的 Top-K；每个值都要有对应快照，否则控件就只是改标签
TOP_K_CHOICES = (1, 2, 3, 4, 5)

UNVERIFIED_NOTE = ("生成层尚未完成正式验证：离线验收里「无依据不伪造」（S4）是**尚未验证"
                   "（NOT VALIDATED）**，不是已执行且失败；目前只有单轮 8 例用户侧样本。")


def load_recorded_answers(dirpath: Path) -> dict:
    answers = {}
    for _, _, _, _, fname in SCENARIOS:
        if fname:
            answers[fname] = json.loads((dirpath / fname).read_text(encoding="utf-8"))
    return answers


def item(e: dict) -> dict:
    """把 rag_core 的片段结构转成接口字段（只做搬运，不改数值）。"""
    return {
        "id": e["id"],
        "origin": e["origin"],                 # hit / neighbor
        "rank": e["rank"],                     # 命中名次；补充片段记录其所属命中的名次
        "score": e["score"],                   # BM25 排序分；补充片段为 null
        "source_id": e["source_id"],
        "source_title": e["source_title"],
        "source_org": e["source_org"],
        "source_url": e["source_url"],
        "section": e["section"],
        "text": e["text"],
    }


def corpus_meta(corpus_id: str) -> dict:
    st = rc.corpus_stats(corpus_id)
    corpus = rc.get_corpus(corpus_id)
    if corpus["kind"] == "section":
        chunking = ("按 Markdown 标题分节 → 按空行分段（相邻列表合并）→ 超过上限再按字数切；"
                    "chunk id = {来源}#s{节}p{段}")
        license_note = "Datawhale《All-in-RAG》，许可 CC BY-NC-SA 4.0（署名 · 非商业 · 相同方式共享）"
    else:
        chunking = "按空行分段 → 超过上限再按字数切；chunk id = {文件名}#P{段}（无标题层级）"
        license_note = ""
    return {
        "corpus_id": st["corpus_id"],
        "name": st["name"],
        "note": st["note"],
        "source_count": st["source_count"],
        "chunk_count": st["chunk_count"],
        "total_chars": st["total_chars"],
        "version": st["version"],
        "tokenizer": st["tokenizer"],
        "params": st["params"],
        "chunking_note": chunking,
        "license_note": license_note,
        "is_sample_only": corpus_id == rc.POWER_DEMO,
        "warning": ("示例资料，不是正式规程，不可用于现场作业或安全决策。"
                    if corpus_id == rc.POWER_DEMO else ""),
    }


def build(answers: dict) -> dict:
    """每个场景都为 Top-K = 1..5 各生成一份快照。

    30 号复审 P1：Top-K 控件不能只改标签——页面允许选 1–5，快照就必须真的按该 Top-K
    重算命中、同节补充、上下文预算与引用解析，否则会出现「Top-1 却列 3 段命中」的矛盾。
    """
    scenarios = {}
    for key, corpus_id, question, recorded_top_k, fname in SCENARIOS:
        by_top_k = {}
        for k in TOP_K_CHOICES:
            assembled = rc.expand_and_assemble(question, top_k=k, corpus_id=corpus_id)
            ctx_map = {e["id"]: e for e in assembled["contexts"]}
            answer = answers[fname]["answer"] if fname else None
            by_top_k[str(k)] = {
                "hits": [item(e) for e in assembled["hits"]],
                "neighbors": [item(e) for e in assembled["neighbors"]],
                "answer": answer,
                # 引用按**该 Top-K** 的上下文重新解析：Top-K 变了，能对上的 id 也可能变
                "refs": rc.extract_refs(answer, ctx_map) if answer else [],
                "ref_occurrences": rc.count_ref_occurrences(answer) if answer else 0,
                "context_chars": {
                    "total": assembled["total_chars"],
                    "hit": assembled["hit_chars"],
                    "max": assembled["max_chars"],
                    "radius": assembled["radius"],
                },
                # 用真实记录里的耗时（秒）换算成毫秒；没有记录的场景写 null，前端不编造
                "elapsed_ms": {
                    "retrieve": None,
                    "generate": (int(answers[fname]["elapsed_s"] * 1000) if fname else None),
                },
                "recorded_from": (f"21 号证据 {fname}（真实模型输出，逐字节写入）"
                                  if fname else ""),
            }
        scenarios[key] = {
            "request": {
                "question": question, "corpus_id": corpus_id,
                "mode": "generate", "top_k": recorded_top_k,
            },
            "recorded_top_k": recorded_top_k,
            "by_top_k": by_top_k,
        }
    return {
        "schema_version": 2,
        "data_source": "mock",
        "mock_note": ("第一阶段本地示例数据：数值来自 rag_core 的真实检索快照与本项目已记录的"
                      "真实回答原文，没有编造分数或引用 id；但未接入真实语料与模型调用。"),
        "generation_verified": False,
        "unverified_note": UNVERIFIED_NOTE,
        # 第一阶段没有服务端可检测，Key 状态只能是"未知"；
        # 演示"无 Key"状态时才由前端显式置为 false（不伪造"已检测到"）
        "api_key_configured_default": None,
        "top_k_choices": list(TOP_K_CHOICES),
        "corpora": [corpus_meta(rc.RAG_LEARNING), corpus_meta(rc.POWER_DEMO)],
        "default_corpus": rc.RAG_LEARNING,
        "scenarios": scenarios,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers-dir", required=True,
                    help="21 号证据目录（读 01/03/07 的回答原文，按字节搬运）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    src = Path(args.answers_dir)
    if not src.is_dir():
        print(f"[FAIL] 找不到回答来源目录：{src.name}")
        return 1
    answers = load_recorded_answers(src)
    data = build(answers)
    data["answers_source"] = src.name

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for key, sc in data["scenarios"].items():
        brief = " · ".join(
            f"Top-{k}:{len(v['hits'])}+{len(v['neighbors'])}/引用{len(v['refs'])}"
            for k, v in sc["by_top_k"].items())
        print(f"[OK] {key}（记录时 Top-K={sc['recorded_top_k']}）：{brief}")
    # 输出目录可能被指定为项目外路径，relative_to 会抛错；这时直接打印绝对路径
    try:
        shown = out.relative_to(REPO).as_posix()
    except ValueError:
        shown = str(out)
    print(f"写入 {shown}（来源：{src.name}，全程离线、零模型请求）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
