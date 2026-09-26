# -*- coding: utf-8 -*-
"""第 6 步（新增，任务 A）：BM25 与向量检索的**同口径对照评测**。

跑法（离线、零付费、不需要 API key）：
    python 06_eval_vector.py --corpus rag_learning
    python 06_eval_vector.py --corpus rag_learning --field text      # 另一轮实验（口径不同，不混算）

它做什么 / 不做什么：
  - **不改 `04_eval.py` 一个字节**：判据函数（`_match` / `_validate_evidence`）用
    importlib 从 `04_eval.py` 里读进来复用，避免"复制一套判据慢慢漂移"。
  - 同一份 chunk、同一份 30 题、同一 Top-K、同一套判据下，把**两个检索器**
    （原 BM25 与新增向量后端）各跑一遍：`search(query, k)` 接口同形，所以
    评测循环只有一份。
  - 顺带做一次**原口径回归**：BM25 在同一条代码路径上的结果，必须与已发布的
    `output/eval_report_rag_learning.json` 完全一致（Hit@1/3/5、MRR、逐题名次）。
    不一致就直接报错退出——这是"新代码没有偷偷改口径"的第一道闸门。
  - 只写**新增**报告文件，不覆盖任何历史报告：
        output/eval_report_rag_learning_vector.json        （向量）
        output/eval_report_rag_learning_bm25_recheck.json  （BM25 同路径复算）
        output/vector_vs_bm25_detail.csv                   （逐题对照表）
  - 拒答题只做观察记录，**不**把"返回低分/没返回"写成拒答能力 PASS。

**发布顺序与退出码（60 号阻断项 + 62 号两处收口的修复）**：
  先算结果（全在内存里）→ **先判基线闸门** → 闸门通过才落盘。基线缺失或对拍不一致时，
  一个字节都不写，就不会留下"看着像通过、其实是失败轮"的 JSON/CSV。

  落盘分两步，**最后一步才是发布清单**：
    1) 三件对照产物各写 `.tmp`，逐个 `os.replace`；随后**回读校验**（目标文件哈希必须等于
       落盘前 `.tmp` 的哈希）。任何替换失败或校验不符 → 清掉残留 `.tmp`、**不写清单**、退出 4。
    2) 校验通过后，才写**发布清单**（含三件产物的名字 + 字节数 + sha256 + 基线锚点）。
  > 注意：**三次 `os.replace` 本身不构成"整组原子发布"**。中途 I/O 失败时，磁盘上可能已留下
  > 部分新产物（62 号反例：第二次替换失败 → 向量报告已更新、另两件未更新）。所以判定"这一轮
  > 是否发布完成"**只能看清单**：`--verify-published` 会逐件核对清单里的哈希，对不上就不算完成。
  > 下游不得只凭"某个 JSON/CSV 存在"认定对照实验通过。

      退出码 0 = 闸门通过，三件产物 + 发布清单均已发布（可用 --verify-published 复核）
             1 = 基线对拍**不一致** → 未发布任何产物
             2 = 参数非法（argparse 直接退出，例如 `--k` 不是 5）
             3 = 历史基线**缺失 / 读不到 / 结构不可用** → 未发布任何产物。
                 "结构不可用"的实际判定范围（64 号要求写清；66 号补两条）：顶层不是对象；
                 缺 `detail` 或 `hit_at_1`；`detail` 某行不是对象；某行缺合法的 `question_id`；
                 **某行缺 `first_evidence_rank` 键**；
                 某行 `first_evidence_rank` 既不是**精确整数**也不是 `null`
                 （`true` / `false` 不算整数 —— Python 的 `bool` 是 `int` 子类且 `True == 1`）。
             4 = **发布校验失败**（替换出错或回读哈希不符）→ 不写发布清单
             5 = `--verify-published` 失败。校验集合**由程序自己预期的三件产物决定**，
                 不采信清单自报的 `artifacts` 集合（64 号 P1：曾可把同一份报告列三次、
                 删掉另两件而仍退出 0）。以下情形一律退出 5 且不泄漏 traceback：
                   清单缺失 / 读不到 / 非法 JSON / 顶层不是对象 / 缺 `artifacts` 数组 /
                   某项不是对象 / 某项缺合法 `name` / 出现非预期文件名 / 同一文件名重复出现 /
                   缺 `sha256` 或不是 64 位十六进制 / 缺少任一预期产物 / 任一产物文件缺失或哈希不符。

  `--k` 本轮固定为 5：Hit@1/3/5 与检索深度绑定，用 `--k 1` 只取 1 条却仍打印 Hit@5
  会得到貌似有效、其实无意义的汇总，所以直接从参数层面禁掉。
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import rag_core as rc

BASE_DIR = Path(__file__).resolve().parent
EVAL_K = 5                      # 与 04_eval.py 的 max(HIT_KS) 保持一致
DEFAULT_FIELD = "search_text"   # 与 BM25 输入字段对齐（含标题/节路径）
PUBLISHED_REPORT = rc.OUTPUT_DIR / "eval_report_rag_learning.json"


def artifact_paths(corpus_id: str, field: str) -> dict:
    """产物路径。首轮（search_text）沿用约定名；换输入字段属于另一轮实验，全部另存。"""
    suffix = "" if field == DEFAULT_FIELD else f"_field-{field}"
    out = rc.OUTPUT_DIR
    return {
        "vector": out / f"eval_report_{corpus_id}_vector{suffix}.json",
        "bm25": out / f"eval_report_{corpus_id}_bm25_recheck{suffix}.json",
        "csv": out / f"vector_vs_bm25_detail{suffix}.csv",
        "manifest": out / f"vector_vs_bm25_publish_manifest{suffix}.json",
    }


def sha256_file(path: Path):
    """文件哈希；文件不存在返回 None（不抛异常，方便校验分支自己给结论）。"""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


ARTIFACT_KEYS = ("vector", "bm25", "csv")   # 程序预期的三件产物（校验集合以此为准）


def expected_artifacts(paths: dict) -> dict:
    """本轮**程序预期**的三件产物：{文件名: 逻辑键}。

    `--verify-published` 的校验集合只认它，不认清单自报的 `artifacts`
    （64 号 P1：清单自报集合曾可把同一份报告列三次、删掉另两件而仍退出 0）。
    """
    return {paths[key].name: key for key in ARTIFACT_KEYS}


def is_sha256_hex(value) -> bool:
    """64 位十六进制字符串才算合法哈希（大小写不敏感，比较时统一小写）。"""
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value.lower()))


def verify_published(paths: dict) -> int:
    """只校验"发布清单 + 程序预期的三件产物"是不是一套；不跑检索、不写任何文件。

    返回 0 = 成一套；5 = 任何不成立的情形（64 号要求：一律 5，且不泄漏 traceback）。
    """
    try:
        return _verify_published(paths)
    except Exception as exc:                      # 兜底：绝不让异常退化成退出码 1
        print(f"[FAIL] 校验过程中出现未预期错误（{type(exc).__name__}: {str(exc)[:120]}）"
              f" → 本轮发布**不算完成**（退出码 5）")
        return 5


def _verify_published(paths: dict) -> int:
    manifest_path = paths["manifest"]
    expected = expected_artifacts(paths)          # {文件名: 逻辑键}
    print("== 校验发布完整性（--verify-published）==")
    print(f"   预期产物（由程序决定，不采信清单自报集合）：{'、'.join(expected)}")

    if not manifest_path.exists():
        print(f"[FAIL] 找不到发布清单 {manifest_path.name} → 本轮发布**不算完成**（退出码 5）")
        return 5
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[FAIL] 发布清单读不到或不是合法 JSON（{type(exc).__name__}）"
              f" → 本轮发布**不算完成**（退出码 5）")
        return 5
    if not isinstance(manifest, dict):
        print(f"[FAIL] 发布清单顶层不是对象（实际 {type(manifest).__name__}）"
              f" → 本轮发布**不算完成**（退出码 5）")
        return 5
    items = manifest.get("artifacts")
    if not isinstance(items, list):
        print("[FAIL] 发布清单缺少 artifacts 数组 → 本轮发布**不算完成**（退出码 5）")
        return 5

    bad = []
    seen = {}                                     # 文件名 → 清单声明的哈希
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            bad.append(f"第 {idx} 项不是对象（实际 {type(item).__name__}）")
            continue
        name, want = item.get("name"), item.get("sha256")
        if not isinstance(name, str) or not name:
            bad.append(f"第 {idx} 项缺少合法的 name 字段")
            continue
        if name not in expected:
            bad.append(f"{name} 不是本轮预期产物（清单不得自行指定要校验的文件集合）")
            continue
        if name in seen:
            bad.append(f"{name} 在清单里重复出现")
            continue
        if not is_sha256_hex(want):
            bad.append(f"{name} 的 sha256 缺失或不是 64 位十六进制")
            continue
        got = sha256_file(paths[expected[name]])
        state = "OK" if (got is not None and got.lower() == want.lower()) else (
            "缺失" if got is None else "哈希不符")
        print(f"  {state:6} {name}  清单={want[:16]}… 现场={(got or '无')[:16]}…")
        if state != "OK":
            bad.append(f"{name} {state}")
            continue
        seen[name] = want

    missing = [name for name in expected if name not in seen]
    if missing:
        bad.append("清单缺少预期产物：" + "、".join(missing))
    if bad:
        print(f"[FAIL] 发布清单不成立（{len(bad)} 处）：")
        for one in bad:
            print(f"        - {one}")
        print("       → 本轮发布**不算完成**（退出码 5）")
        return 5
    print(f"[OK] 程序预期的三件产物逐一与清单对上：本轮发布完成"
          f"（published_at={manifest.get('published_at')}）")
    return 0


def load_eval_module():
    """把 04_eval.py 当模块读进来（它自己的 main() 不会执行）。"""
    path = BASE_DIR / "04_eval.py"
    spec = importlib.util.spec_from_file_location("eval04", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_retrieval(search, questions: list, docs: list, match, k: int) -> list:
    """一份评测循环，两个检索器共用。返回逐题记录（键与 04_eval.py 对齐）。"""
    rows = []
    for item in questions:
        question = item["question"]
        evidence = item.get("expected_evidence", [])
        hits = search(question, k)
        ranked = [{
            "rank": i,
            "id": d["id"],
            "score": round(float(s), 4),
            "source_id": d["source_id"],
            "section": d.get("section", ""),
            "title": d.get("source_title", ""),
        } for i, (s, d) in enumerate(hits, 1)]

        first_rank = None
        for i, (_, d) in enumerate(hits, 1):
            if any(match(e, d) for e in evidence):
                first_rank = i
                break

        candidates = [d["id"] for d in docs if any(match(e, d) for e in evidence)]
        rows.append({
            "question_id": item.get("question_id", ""),
            "question": question,
            "type": item.get("type", ""),
            "should_refuse": bool(item.get("should_refuse")),
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
        })
    return rows


def split_rows(rows: list):
    """与 04_eval.py 同一套分桶：拒答 / 计分（有标准证据）/ 仅旧关键词口径。"""
    refused = [r for r in rows if r["should_refuse"]]
    scored = [r for r in rows if not r["should_refuse"] and r["expected_evidence"]]
    legacy = [r for r in rows if not r["should_refuse"] and not r["expected_evidence"]]
    for r in refused:
        r["returned_result"] = bool(r["top_hits"])
        r["top1_score"] = r["top_hits"][0]["score"] if r["top_hits"] else None
    return refused, scored, legacy


def rate(rows: list, key: str):
    return round(sum(1 for r in rows if r[key]) / len(rows), 4) if rows else None


def mrr(rows: list):
    if not rows:
        return None
    return round(sum(1 / r["first_evidence_rank"] for r in rows
                     if r["first_evidence_rank"]) / len(rows), 4)


def build_report(corpus_id, retriever_name, retriever_config, rows, stats, questions, k):
    refused, scored, legacy = split_rows(rows)
    failures = [r for r in scored if not r["hit_at_5"]]
    return {
        "corpus_id": corpus_id,
        "corpus_name": stats["name"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus_version": stats["version"],
        "corpus_fingerprint": stats["fingerprint"],
        "retriever": retriever_name,
        "retriever_config": dict(retriever_config, top_k=k, threshold=None),
        "tokenizer": stats["tokenizer"] if retriever_name == "bm25" else retriever_config.get("model_id", ""),
        "params": dict(stats["params"], eval_k=k),
        "source_count": stats["source_count"],
        "chunk_count": stats["chunk_count"],
        "total_chars": stats["total_chars"],
        "question_count": len(questions),
        "scored_question_count": len(scored),
        "refuse_question_count": len(refused),
        "proxy_only_question_count": len(legacy),
        "hit_at_1": rate(scored, "hit_at_1"),
        "hit_at_3": rate(scored, "hit_at_3"),
        "hit_at_5": rate(scored, "hit_at_5"),
        "mrr": mrr(scored),
        "notes": [
            "Hit@K / MRR 的判据是「标准证据 chunk」是否进入 Top-K，与 04_eval.py 同一套函数"
            "（chunk_id / section_exact / section_prefix，无子串匹配）；**不评判模型回答**。",
            "两个检索器共用同一份 chunk、同一份 30 题、同一 Top-K、同一判据；"
            "只换了「谁来做排序」。",
            "向量侧未设相似度阈值，也没有做 BM25+向量的融合，"
            "余弦分数与 BM25 分数**不做数值比较**，只比名次与命中。",
            "拒答题只作观察：向量侧一定会返回 Top-K（没有阈值），"
            "记录 Top-1 不构成「能拒答」的验收证据。",
        ],
        "failures": [{
            "question_id": r["question_id"], "question": r["question"],
            "top1": r["top_hits"][0]["id"] if r["top_hits"] else "(无结果)",
        } for r in failures],
        "refuse_analysis": [{
            "question_id": r["question_id"], "question": r["question"],
            "returned_result": r["returned_result"],
            "top1": r["top_hits"][0]["id"] if r["top_hits"] else "(无结果)",
            "top1_section": r["top_hits"][0]["section"] if r["top_hits"] else "",
            "top1_score": r["top1_score"],
            "status": "NOT VALIDATED",
            "note": "观察项：无相关性阈值，拒答题也会返回 Top-K；不构成本场景的通过证据。",
        } for r in refused],
        "detail": rows,
    }


def compare_with_published(report: dict) -> dict:
    """BM25 同路径复算 vs 已发布基线报告：命中率与逐题名次都要一致。

    拿不到基线（文件不存在 / 读不动 / 不是合法 JSON / 结构不可用）→ `checked=False`，
    调用方必须**非零退出且不落盘**（60 号复审阻断项），退出码按约定是 3。

    64 号 P2 收口：`detail` 不只看"是不是数组"，还要守每行的字段与类型 ——
    否则畸形行会在下面直接 `r["question_id"]` 处抛 KeyError，退化成一个带 traceback 的
    普通失败（退出码 1），而契约要求这类结构问题走 `checked=False`（退出码 3）。
    """
    if not PUBLISHED_REPORT.exists():
        return {"checked": False, "reason": f"找不到已发布基线报告 {PUBLISHED_REPORT.name}"}
    try:
        old = json.loads(PUBLISHED_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"checked": False,
                "reason": f"基线报告无法解析（{type(exc).__name__}: {str(exc)[:80]}）"}
    # 顶层类型先守住：合法 JSON 也可能是数组/字符串（62 号反例），
    # 直接 .get() 会抛 AttributeError 变成"普通失败"，退出码就不是约定的 3 了。
    if not isinstance(old, dict):
        return {"checked": False,
                "reason": f"基线报告顶层不是对象（实际 {type(old).__name__}），无法作为基线"}
    if not isinstance(old.get("detail"), list) or "hit_at_1" not in old:
        return {"checked": False, "reason": "基线报告结构不完整（缺少 detail / hit_at_1）"}
    for idx, row in enumerate(old["detail"], start=1):
        if not isinstance(row, dict):
            return {"checked": False,
                    "reason": f"基线报告 detail 第 {idx} 行不是对象（实际 {type(row).__name__}）"}
        if not isinstance(row.get("question_id"), str) or not row["question_id"]:
            return {"checked": False,
                    "reason": f"基线报告 detail 第 {idx} 行缺少合法的 question_id"}
        # 66 号 P2-1：键必须**显式存在**。原来用 row.get() 把"缺键"读成了 None（当作合法未命中），
        # 随后构造 old_rank 时又用 r["first_evidence_rank"] 直接索引 → KeyError，
        # 没按约定返回 checked=false（入口本该退出 3）。
        if "first_evidence_rank" not in row:
            return {"checked": False,
                    "reason": f"基线报告 detail 第 {idx} 行缺少 first_evidence_rank 键"}
        rank = row["first_evidence_rank"]
        # 66 号 P2-2：只接受**精确整数**或 null。Python 里 bool 是 int 的子类且 True == 1，
        # 用 isinstance(rank, int) 会把 JSON true 当成名次 1 收下，误报 checked=true / identical=true。
        if rank is not None and type(rank) is not int:
            return {"checked": False,
                    "reason": f"基线报告 detail 第 {idx} 行 first_evidence_rank 类型不可用"
                              f"（应为整数或 null，实际 {type(rank).__name__}）"}
    keys = ("hit_at_1", "hit_at_3", "hit_at_5", "mrr", "scored_question_count",
            "chunk_count", "corpus_fingerprint")
    same_summary = {k: (old.get(k), report.get(k)) for k in keys}
    old_rank = {r["question_id"]: r["first_evidence_rank"] for r in old.get("detail", [])}
    new_rank = {r["question_id"]: r["first_evidence_rank"] for r in report["detail"]}
    diff_q = {q: (old_rank.get(q), new_rank.get(q)) for q in set(old_rank) | set(new_rank)
              if old_rank.get(q) != new_rank.get(q)}
    ok = all(a == b for a, b in same_summary.values()) and not diff_q
    return {
        "checked": True,
        "published_report": PUBLISHED_REPORT.name,
        "summary_field_by_field": {k: {"published": a, "recheck": b}
                                   for k, (a, b) in same_summary.items()},
        "per_question_rank_diff": diff_q,
        "identical": ok,
    }


def main():
    ap = argparse.ArgumentParser(description="BM25 与向量检索的同口径对照评测（离线）")
    ap.add_argument("--corpus", default=rc.RAG_LEARNING)
    ap.add_argument("--field", default=DEFAULT_FIELD, choices=["search_text", "text"],
                    help="向量输入字段；首轮固定 search_text（与 BM25 对齐）")
    ap.add_argument("--k", type=int, default=EVAL_K, choices=[EVAL_K],
                    help=f"本轮固定 Top-{EVAL_K}：Hit@1/3/5 与检索深度绑定，"
                         "换深度会让汇总口径不自洽（另立实验时请同时另存报告）")
    ap.add_argument("--model-dir", default=None, help="本地 ONNX 模型目录（默认读环境变量）")
    ap.add_argument("--no-cache", action="store_true", help="忽略旧向量缓存，强制重建")
    ap.add_argument("--verify-published", action="store_true",
                    help="只校验「发布清单 + 三件产物」是不是一套（不跑检索、不写文件）")
    args = ap.parse_args()

    if args.verify_published:
        return verify_published(artifact_paths(args.corpus, args.field))

    import vector_retriever as vr

    eval04 = load_eval_module()
    match, validate = eval04._match, eval04._validate_evidence

    corpus = rc.get_corpus(args.corpus)
    questions = json.loads(corpus["questions"].read_text(encoding="utf-8"))
    docs = rc.load_documents(args.corpus)
    validate(questions, docs)
    stats = rc.corpus_stats(args.corpus)

    print(f"语料：{stats['name']} · {stats['chunk_count']} chunk · 指纹 {stats['fingerprint'][:16]}…")
    print(f"题集：{len(questions)} 道（证据已通过自检）· 判据来自 04_eval.py（未改动）")

    # ---- 检索器 1：原 BM25（口径不变） ----
    bm25 = rc.BM25(docs)
    bm25_rows = run_retrieval(lambda q, k: bm25.search(q, k), questions, docs, match, args.k)

    # ---- 检索器 2：向量 ----
    retriever = vr.build_vector_retriever(
        args.corpus, docs, stats["fingerprint"], field=args.field,
        model_dir=args.model_dir, use_cache=not args.no_cache)
    vector_rows = run_retrieval(retriever.search, questions, docs, match, args.k)
    enc_id = retriever.enc.identity()
    cache_status = getattr(retriever, "cache_status", "")
    print(f"向量：模型 {enc_id['model_id']} · dim={enc_id['dim']} · "
          f"池化={enc_id['pooling']} · {enc_id['normalize']} 归一化 · "
          f"输入字段={args.field} · 查询前缀={enc_id['query_template']} · "
          f"索引={cache_status}"
          + (f"（构建 {retriever.build_seconds}s）" if retriever.build_seconds else "（未重新编码）"))

    bm25_report = build_report(args.corpus, "bm25", {
        "field": "search_text（BM25 分词输入）", "tokenizer": stats["tokenizer"],
    }, bm25_rows, stats, questions, args.k)
    vector_report = build_report(args.corpus, "vector", {
        "field": args.field,
        "model_id": enc_id["model_id"],
        "base_model": enc_id["base_model"],
        "onnx_sha256": enc_id["onnx_sha256"],
        "tokenizer_sha256": enc_id["tokenizer_sha256"],
        "dim": enc_id["dim"],
        "pooling": enc_id["pooling"],
        "normalize": enc_id["normalize"],
        "max_len": enc_id["max_len"],
        "query_template": enc_id["query_template"],
        "index": "faiss.IndexFlatIP（暴力精确检索，无近似）",
        "index_cache_status": cache_status,
        "index_build_seconds": retriever.build_seconds,
        "vectors_file": retriever.cache_paths()[1].name,
    }, vector_rows, stats, questions, args.k)

    # ---- 第一道闸门（**决定能不能发布**）：BM25 复算必须与已发布基线一致 ----
    # 注意：到这里为止只在内存里算，没有任何文件被写出去。
    gate = compare_with_published(bm25_report)
    print("\n== 原口径回归（同代码路径复算 BM25）==")
    if gate["checked"]:
        for k, v in gate["summary_field_by_field"].items():
            flag = "OK " if v["published"] == v["recheck"] else "差异"
            print(f"  {flag} {k}: 已发布={v['published']} 复算={v['recheck']}")
        if gate["per_question_rank_diff"]:
            print(f"  逐题名次差异：{gate['per_question_rank_diff']}")
        print(f"  结论：{'完全一致' if gate['identical'] else '不一致（必须停下排查）'}")
    else:
        print(f"  基线不可用：{gate['reason']}")

    # 闸门没过 → 在**任何落盘之前**停止：不发布向量报告 / BM25 复算报告 / CSV。
    # 否则"一轮已判失败的对照"会以 JSON/CSV 形式留在 output/ 里，被后来者当成通过的对照实验
    # （60 号复审阻断项）。失败轮也不得覆盖已有的通过版产物 —— 因为这里根本没写文件。
    if not gate["checked"]:
        print(f"\n[BLOCKED] 拿不到历史基线报告（{PUBLISHED_REPORT.name}）：")
        print(f"           {gate['reason']}")
        print("           本轮对照不成立 → **未发布任何产物**（退出码 3）。")
        return 3
    if not gate["identical"]:
        print("\n[BLOCKED] 原 BM25 口径回归与历史基线不一致：本轮对照不成立 "
              "→ **未发布任何产物**（退出码 1），请先排查。")
        return 1

    # ---- 对照表 ----
    print("\n== BM25 vs 向量（27 道计分题）==")
    head = f"{'':6}{'Hit@1':>9}{'Hit@3':>9}{'Hit@5':>9}{'MRR':>9}"
    print(head)
    for name, rep in (("BM25", bm25_report), ("向量", vector_report)):
        print(f"{name:6}{rep['hit_at_1']:>9.2%}{rep['hit_at_3']:>9.2%}"
              f"{rep['hit_at_5']:>9.2%}{rep['mrr']:>9.4f}")
    print(f"分母：计分 {bm25_report['scored_question_count']} 题 · "
          f"拒答单列 {bm25_report['refuse_question_count']} 题")

    disagreements = [{
        "question_id": b["question_id"], "question": b["question"],
        "bm25_rank": b["first_evidence_rank"], "vector_rank": v["first_evidence_rank"],
        "winner": "vector" if (v["first_evidence_rank"] or 99) < (b["first_evidence_rank"] or 99)
                  else ("bm25" if (b["first_evidence_rank"] or 99) < (v["first_evidence_rank"] or 99)
                        else "tie"),
        "bm25_top1": b["top_hits"][0]["id"] if b["top_hits"] else None,
        "vector_top1": v["top_hits"][0]["id"] if v["top_hits"] else None,
    } for b, v in zip(bm25_rows, vector_rows)
        if b["first_evidence_rank"] != v["first_evidence_rank"]]
    wins = sum(1 for d in disagreements if d["winner"] == "vector")
    losses = sum(1 for d in disagreements if d["winner"] == "bm25")
    print(f"名次有差异的题：{len(disagreements)} 道（向量更好 {wins} · BM25 更好 {losses}）")
    for d in disagreements:
        print(f"  {d['question_id']} BM25={d['bm25_rank']} 向量={d['vector_rank']} "
              f"({d['winner']}) {d['question']}")

    # ---- 发布（闸门已通过；只新增文件，不覆盖历史报告） ----
    vector_report["comparison"] = {
        "against": "bm25（同一代码路径复算）",
        "bm25_hit_at_1": bm25_report["hit_at_1"], "bm25_hit_at_3": bm25_report["hit_at_3"],
        "bm25_hit_at_5": bm25_report["hit_at_5"], "bm25_mrr": bm25_report["mrr"],
        "rank_diff_question_count": len(disagreements),
        "vector_better": wins, "bm25_better": losses,
        "disagreements": disagreements,
    }
    bm25_report["regression_vs_published"] = gate
    vector_report["regression_vs_published"] = gate

    paths = artifact_paths(args.corpus, args.field)
    vec_path, bm25_path = paths["vector"], paths["bm25"]
    csv_path, manifest_path = paths["csv"], paths["manifest"]
    rc.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 第一步：三件产物各写 .tmp，记下落盘前哈希（回读校验要用）。
    staged: list = []
    for path, text in ((vec_path, json.dumps(vector_report, ensure_ascii=False, indent=2)),
                       (bm25_path, json.dumps(bm25_report, ensure_ascii=False, indent=2))):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        staged.append((tmp, path, sha256_file(tmp)))

    tmp_csv = csv_path.with_name(csv_path.name + ".tmp")
    with open(tmp_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "type", "should_refuse", "question",
                    "bm25_first_rank", "vector_first_rank", "diff",
                    "bm25_top1", "vector_top1", "bm25_top5_ids", "vector_top5_ids"])
        for b, v in zip(bm25_rows, vector_rows):
            w.writerow([
                b["question_id"], b["type"], b["should_refuse"], b["question"],
                b["first_evidence_rank"], v["first_evidence_rank"],
                (v["first_evidence_rank"] or 99) - (b["first_evidence_rank"] or 99),
                b["top_hits"][0]["id"] if b["top_hits"] else "",
                v["top_hits"][0]["id"] if v["top_hits"] else "",
                " | ".join(h["id"] for h in b["top_hits"]),
                " | ".join(h["id"] for h in v["top_hits"]),
            ])
    staged.append((tmp_csv, csv_path, sha256_file(tmp_csv)))

    # 第二步：逐个替换，然后**回读校验**。
    # 注意：这一步不是"整组原子" —— 中途 I/O 失败时磁盘上可能已留下部分新产物（62 号反例）。
    # 所以判定"是否发布完成"只能看第三步写出的发布清单。
    publish_error = None
    try:
        for tmp, path, _ in staged:
            os.replace(tmp, path)
    except OSError as exc:
        publish_error = f"{type(exc).__name__}: {exc}"

    mismatched = [str(path.name) for _, path, want in staged if sha256_file(path) != want]
    for tmp, _, _ in staged:                      # 清掉可能的残留 .tmp
        if tmp.exists():
            tmp.unlink()
    if publish_error or mismatched:
        print(f"\n[BLOCKED] 发布校验失败：替换错误={publish_error or '无'} · "
              f"回读不符={mismatched or '无'}")
        print("           已清掉 .tmp，但**可能已留下部分新产物**；"
              "**未写发布清单** → 本轮发布不算完成（退出码 4）。")
        return 4

    # 第三步（最后一步）：写发布清单，把三件产物的哈希绑在一起。
    artifacts = [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
                 for _, path, _ in staged]
    baseline_sha = sha256_file(PUBLISHED_REPORT)
    manifest = {
        "published_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus_id": args.corpus,
        "corpus_fingerprint": stats["fingerprint"],
        "field": args.field,
        "top_k": args.k,
        "gate": {"baseline_report": PUBLISHED_REPORT.name,
                 "baseline_sha256": baseline_sha,
                 "bm25_recheck_identical": bool(gate.get("identical"))},
        "retriever": {"model_id": enc_id["model_id"], "onnx_sha256": enc_id["onnx_sha256"],
                      "dim": enc_id["dim"], "pooling": enc_id["pooling"],
                      "normalize": enc_id["normalize"], "query_template": enc_id["query_template"]},
        "artifacts": artifacts,
        "how_to_check": "python 06_eval_vector.py --corpus "
                        f"{args.corpus} --verify-published（退出 0 = 三件产物与清单成一套）",
        "note": "清单是最后一步写的。清单缺失、或任一产物哈希与清单不符 → 本轮发布不算完成；"
                "不得只凭单个 JSON/CSV 存在就认定对照实验通过。",
    }
    tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)

    print(f"\n报告 → {vec_path}\n报告 → {bm25_path}\n对照表 → {csv_path}\n清单 → {manifest_path}")
    print("闸门：原 BM25 口径与历史基线完全一致 → 三件产物 + 发布清单已发布（退出码 0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
