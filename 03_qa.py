# -*- coding: utf-8 -*-
"""第 3 步：检索 + 大模型生成带引用的回答（需要 API key）。

用法：
    $env:DEEPSEEK_API_KEY = "sk-xxxx"
    python 03_qa.py "装设接地线的顺序是什么"
    python 03_qa.py "RAG 评估三元组包含哪三个维度" --corpus rag_learning

零结果行为（与界面 app.py 对齐）：
    检索结果为 0 条时，直接输出「资料中未找到相关依据」，**不调用模型**，
    不产生任何 API 请求与费用。

检索命中 vs 同节补充：
    "检索命中"是真实的 BM25 Top-K（口径不变）；生成时还会按「同 source_id + 同章节」
    补入相邻片段，用于补全同一章节里的枚举。两者在输出里分开打印，
    补充片段**不计入命中、也不计入 Hit@K / MRR**。
"""
import argparse
import sys

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
import rag_core as rc


def main():
    ap = argparse.ArgumentParser(description="检索 + 生成")
    ap.add_argument("question", nargs="?", default=None, help="问题")
    ap.add_argument("--corpus", default=rc.DEFAULT_CORPUS,
                    help=f"知识库 id（{' / '.join(rc.corpus_ids())}）")
    ap.add_argument("--top-k", type=int, default=rc.TOP_K)
    args = ap.parse_args()

    if not args.question:
        print('用法：python 03_qa.py "你的问题" [--corpus rag_learning]')
        return 2

    q = args.question
    corpus = rc.get_corpus(args.corpus)
    assembled = rc.expand_and_assemble(q, top_k=args.top_k, corpus_id=args.corpus)
    hits = assembled["hits"]

    print(f"知识库：{corpus['name']}")
    print(f"检索命中（BM25 Top-{args.top_k}，{len(hits)} 段）：")
    for e in hits:
        print(f"  [{e['id']}] {e['text'][:60]}...")

    # —— 零结果短路：不调用模型 ——
    if not hits:
        print("\n资料中未找到相关依据。")
        print("[未调用模型] 检索结果为 0 条，已跳过生成，不产生 API 请求。")
        return 0

    if assembled["neighbor_count"]:
        print(f"\n生成时另补入同节相邻上下文 {assembled['neighbor_count']} 段"
              f"（与上面 {len(hits)} 段命中分开，不计入检索命中）：")
        for e in assembled["neighbors"]:
            print(f"  [+]{e['id']}  {e['text'][:40]}...")

    if not DEEPSEEK_API_KEY:
        print('\n请先设置环境变量：$env:DEEPSEEK_API_KEY = "sk-xxxx"')
        return 3

    print("\n生成中...\n")
    ans = rc.generate_answer(q, assembled["contexts"], DEEPSEEK_API_KEY,
                             DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, args.corpus)
    print(ans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
