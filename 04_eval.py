# -*- coding: utf-8 -*-
"""第 4 步：离线评测检索质量（不需要 API key，不调用任何模型）。

一条命令重跑全部指标：
    python 04_eval.py                       # 全部知识库
    python 04_eval.py --corpus rag_learning

标准证据的三种显式判据（**不用子串匹配**，子串匹配会把"路径里恰好含父节名"的
不相关子节算成命中，这正是复审 F1 指出的漏洞）：

    {"mode": "chunk_id",       "chunk_id": "air-formatted-generation#s7p1"}
    {"mode": "section_exact",  "source_id": "...", "section": "<完整节路径>"}
    {"mode": "section_prefix", "source_id": "...", "section": "<节路径根>"}
        # 精确匹配该节，或匹配它的任意子节（section == 根 或 section 以 "根 > " 开头）

题集在评测前会先做**证据自检**：模式非法、节路径不存在、chunk_id 不存在都直接报错，
避免"写错一个字就永远不命中/永远命中"。

指标：
  - Hit@1 / Hit@3 / Hit@5：Top-K 里是否出现标准证据对应的 chunk
  - MRR：第一条命中标准证据的排名倒数，取平均
  - 关键词代理命中率：历史口径，仅在题目标了 keywords 时计算（**不是回答准确率**）
  - 拒答题单列：不计入 Hit/MRR，只记录 Top-1 观察，不构成"能拒答"的验收证据

产出：output/eval_report_power_demo.json、output/eval_report_rag_learning.json
（output/eval_report.json 是最初的示例语料快照，保留不再覆盖）
"""
import argparse
import json
import sys
from datetime import datetime

import rag_core as rc

HIT_KS = (1, 3, 5)
KEYWORD_PROXY_K = 3          # 历史代理指标固定用 Top-3，保证与旧快照同口径
VALID_MODES = ("chunk_id", "section_exact", "section_prefix")


def _match(evidence: dict, doc: dict) -> bool:
    """一条标准证据是否命中某个 chunk（三种显式模式，无子串匹配）。"""
    mode = evidence.get("mode")
    if mode == "chunk_id":
        return doc["id"] == evidence.get("chunk_id")
    if evidence.get("source_id") != doc["source_id"]:
        return False
    section = doc.get("section") or ""
    root = evidence.get("section", "")
    if mode == "section_exact":
        return section == root
    if mode == "section_prefix":
        return section == root or section.startswith(root + " > ")
    return False


def _validate_evidence(questions: list, docs: list) -> None:
    """出题即自检：证据必须能在语料里真实落地，否则直接报错。"""
    doc_ids = {d["id"] for d in docs}
    doc_sections = {(d["source_id"], d.get("section") or "") for d in docs}
    for item in questions:
        qid = item.get("question_id", "?")
        for e in item.get("expected_evidence", []):
            if "section_contains" in e:
                raise ValueError(
                    f"{qid} 仍在使用已废弃的 section_contains（子串匹配）——"
                    "请改成 chunk_id / section_exact / section_prefix"
                )
            mode = e.get("mode")
            if mode not in VALID_MODES:
                raise ValueError(f"{qid} 证据模式非法：{mode!r}，只允许 {VALID_MODES}")
            if mode == "chunk_id":
                if e.get("chunk_id") not in doc_ids:
                    raise ValueError(f"{qid} 引用了语料中不存在的 chunk_id：{e.get('chunk_id')}")
                continue
            root = e.get("section", "")
            if mode == "section_exact":
                # 精确模式只能由精确节落地；不能因为存在子节就错误放行。
                hit_any = (e.get("source_id"), root) in doc_sections
            else:
                # section_prefix 才允许根节本身或其任意子节。
                hit_any = (e.get("source_id"), root) in doc_sections or any(
                    sid == e.get("source_id") and sec.startswith(root + " > ")
                    for sid, sec in doc_sections
                )
            if not hit_any:
                raise ValueError(
                    f"{qid} 的节路径在语料中不存在：{e.get('source_id')} · {root!r}"
                )


def _fmt_evidence(evidence: list) -> str:
    parts = []
    for e in evidence:
        if e.get("mode") == "chunk_id":
            parts.append(e["chunk_id"])
        else:
            parts.append(f"{e.get('source_id')} · {e.get('section')}（{e.get('mode')}）")
    return " | ".join(parts) or "(无)——拒答题"


def evaluate(corpus_id: str) -> dict:
    corpus = rc.get_corpus(corpus_id)
    qf = corpus["questions"]
    if not qf.exists():
        raise FileNotFoundError(f"找不到评测集：{qf}")

    questions = json.loads(qf.read_text(encoding="utf-8"))
    docs = rc.load_documents(corpus_id)
    _validate_evidence(questions, docs)
    bm25 = rc.BM25(docs)
    stats = rc.corpus_stats(corpus_id)
    k_max = max(HIT_KS)

    detail: list = []
    failures: list = []
    refuse_rows: list = []
    scored_rows: list = []
    legacy_only_rows: list = []

    for item in questions:
        q = item["question"]
        evidence = item.get("expected_evidence", [])
        should_refuse = bool(item.get("should_refuse"))
        hits = bm25.search(q, k=k_max)

        ranked = [{
            "rank": i,
            "id": d["id"],
            "score": round(s, 4),
            "source_id": d["source_id"],
            "section": d.get("section", ""),
            "title": d.get("source_title", ""),
        } for i, (s, d) in enumerate(hits, 1)]

        first_rank = None
        for i, (_, d) in enumerate(hits, 1):
            if any(_match(e, d) for e in evidence):
                first_rank = i
                break

        # 证据落地面：哪些 chunk 算作标准证据，供人工/复审逐题核对
        candidates = [d["id"] for d in docs if any(_match(e, d) for e in evidence)]

        row = {
            "question_id": item.get("question_id", ""),
            "question": q,
            "type": item.get("type", ""),
            "should_refuse": should_refuse,
            "expected_evidence": evidence,
            "evidence_modes": sorted({e.get("mode", "") for e in evidence}),
            "evidence_candidate_count": len(candidates),
            "evidence_candidates": candidates,
            "expected_points": item.get("expected_points", ""),
            "top_hits": ranked,
            "first_evidence_rank": first_rank,
            "matched_chunk_id": (ranked[first_rank - 1]["id"] if first_rank else None),
            "hit_at_1": bool(first_rank and first_rank <= 1),
            "hit_at_3": bool(first_rank and first_rank <= 3),
            "hit_at_5": bool(first_rank and first_rank <= 5),
        }

        # 历史关键词代理口径（仅旧电力库题目有 keywords）
        keywords = item.get("keywords", [])
        if keywords:
            texts = " || ".join(d["text"] for _, d in bm25.search(q, k=KEYWORD_PROXY_K))
            row["keywords"] = keywords
            row["keyword_proxy_hit"] = any(kw in texts for kw in keywords)

        if should_refuse:
            # 只做观察记录：BM25 没有相关性阈值，拒答题也会返回 Top-K。
            # 这**不构成**"系统能拒答"的验收证据（复审 F2）。
            row["returned_result"] = bool(hits)
            row["top1_score"] = ranked[0]["score"] if ranked else None
            refuse_rows.append(row)
        elif evidence:
            scored_rows.append(row)
            if not row["hit_at_5"]:
                failures.append(row)
        else:
            # 旧电力库题目没有标注标准证据，只有关键词口径 → 不计入 Hit@K / MRR
            legacy_only_rows.append(row)

        detail.append(row)

    def rate(rows: list, key: str):
        """没有可计分题目时返回 None（表示"不适用"），不要伪装成 0%。"""
        return round(sum(1 for r in rows if r[key]) / len(rows), 4) if rows else None

    mrr = round(sum(1 / r["first_evidence_rank"] for r in scored_rows
                     if r["first_evidence_rank"]) / len(scored_rows), 4) if scored_rows else None

    proxy_rows = [r for r in detail if "keyword_proxy_hit" in r]
    report = {
        "corpus_id": corpus_id,
        "corpus_name": stats["name"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus_version": stats["version"],
        "corpus_fingerprint": stats["fingerprint"],
        "tokenizer": stats["tokenizer"],
        "params": dict(stats["params"], eval_k=k_max),
        "source_count": stats["source_count"],
        "chunk_count": stats["chunk_count"],
        "total_chars": stats["total_chars"],
        "question_count": len(questions),
        "scored_question_count": len(scored_rows),
        "refuse_question_count": len(refuse_rows),
        "proxy_only_question_count": len(legacy_only_rows),
        "hit_at_1": rate(scored_rows, "hit_at_1"),
        "hit_at_3": rate(scored_rows, "hit_at_3"),
        "hit_at_5": rate(scored_rows, "hit_at_5"),
        "mrr": mrr,
        "keyword_proxy": {
            "top_k": KEYWORD_PROXY_K,
            "count": len(proxy_rows),
            "hit_rate": rate(proxy_rows, "keyword_proxy_hit"),
            "note": "关键词是否出现在 Top-K 文本里的代理指标，非回答准确率",
        } if proxy_rows else None,
        "evidence_modes_in_use": sorted({
            m for r in scored_rows for m in r["evidence_modes"]
        }),
        "notes": [
            "Hit@K / MRR 判据是「标准证据 chunk」，用 chunk_id / section_exact / section_prefix "
            "三种显式模式匹配，不使用子串匹配。",
            "BM25 没有相关性阈值：只要查询词在全库出现就会返回结果。"
            "因此本轮**没有**可离线验证的拒答决策；拒答题只作观察记录，"
            "「无依据问题不会被伪造成有资料支持」这一场景标记为 NOT VALIDATED。",
            "切分是固定字数（无 overlap、无句子边界），属已知局限，本轮不做切分实验。",
            "本轮不对 400 字符或某个 Top-K 声称最优。",
        ],
        "failures": [
            {"question_id": r["question_id"], "question": r["question"],
             "expected": _fmt_evidence(r["expected_evidence"]),
             "top1": (r["top_hits"][0]["id"] if r["top_hits"] else "(无结果)")}
            for r in failures
        ],
        "refuse_analysis": [
            {"question_id": r["question_id"], "question": r["question"],
             "returned_result": r["returned_result"],
             "top1": (r["top_hits"][0]["id"] if r["top_hits"] else "(无结果)"),
             "top1_section": (r["top_hits"][0]["section"] if r["top_hits"] else ""),
             "top1_score": r["top1_score"],
             "status": "NOT VALIDATED",
             "note": "观察项：BM25 无相关性阈值，拒答题也会返回 Top-K；"
                     "本轮没有可离线验证的拒答决策，故不构成本场景的通过证据。"}
            for r in refuse_rows
        ],
        "detail": detail,
    }

    rc.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    corpus["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description="离线评测检索质量")
    ap.add_argument("--corpus", default=None,
                    help=f"知识库 id（{' / '.join(rc.corpus_ids())}），默认全部")
    args = ap.parse_args()

    ids = [args.corpus] if args.corpus else rc.corpus_ids()
    for cid in ids:
        r = evaluate(cid)
        print(f"\n=== {r['corpus_name']}（{r['corpus_id']}）===")
        print(f"语料：{r['source_count']} 份来源 / {r['chunk_count']} chunk / "
              f"{r['total_chars']} 字符 · 版本 {r['corpus_version']}")
        print(f"参数：chunk<= {r['params']['chunk_max_chars']} 字符 · 分词 {r['tokenizer']} · "
              f"k1={r['params']['bm25_k1']} b={r['params']['bm25_b']}")
        print(f"证据模式：{' / '.join(r['evidence_modes_in_use']) or '（无）'}")
        print(f"题目：{r['question_count']} 道（计标准证据 {r['scored_question_count']} + "
              f"拒答 {r['refuse_question_count']} + 仅关键词口径 {r['proxy_only_question_count']}）")
        if r["scored_question_count"]:
            print(f"Hit@1 = {r['hit_at_1']:.2%}   Hit@3 = {r['hit_at_3']:.2%}   "
                  f"Hit@5 = {r['hit_at_5']:.2%}   MRR = {r['mrr']:.4f}")
        else:
            print("Hit@1 / Hit@3 / Hit@5 / MRR：不适用"
                  "（该库题目未标注标准证据，只有关键词代理口径）")
        if r["keyword_proxy"]:
            kp = r["keyword_proxy"]
            print(f"关键词代理命中率（Top-{kp['top_k']}，历史口径，非回答准确率）= "
                  f"{kp['hit_rate']:.2%}（{kp['count']} 题）")
        if r["failures"]:
            print("未命中标准证据的题：")
            for f in r["failures"]:
                print(f"  - {f['question_id']} 期望 {f['expected']} → Top1 {f['top1']}")
        if r["refuse_question_count"]:
            print(f"拒答题 {r['refuse_question_count']} 道：状态 NOT VALIDATED"
                  "（无离线可验证的拒答决策，只作观察）")
        print(f"报告 → {rc.get_corpus(cid)['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
