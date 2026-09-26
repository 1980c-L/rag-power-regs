# -*- coding: utf-8 -*-
"""第 2 步：检索（不需要 API key）。

用法：
    python 02_retrieve.py "你的问题"
    python 02_retrieve.py "BM25 和向量检索有什么区别" --corpus rag_learning --top-k 5
输出：最相关的 Top-K 段落 + 出处 + 得分
"""
import argparse
import sys

import rag_core as rc


def main():
    ap = argparse.ArgumentParser(description="BM25 检索")
    ap.add_argument("question", nargs="?", default=None, help="问题")
    ap.add_argument("--corpus", default=rc.DEFAULT_CORPUS,
                    help=f"知识库 id（{' / '.join(rc.corpus_ids())}）")
    ap.add_argument("--top-k", type=int, default=rc.TOP_K)
    args = ap.parse_args()

    if not args.question:
        print('用法：python 02_retrieve.py "你的问题" [--corpus rag_learning] [--top-k 5]')
        return 2

    corpus = rc.get_corpus(args.corpus)
    hits = rc.retrieve(args.question, top_k=args.top_k, corpus_id=args.corpus)
    if not hits:
        print(f"[{corpus['name']}] 没有检索到相关内容。")
        return 0

    print(f"知识库：{corpus['name']}（{rc.corpus_version(args.corpus)}）")
    print(f"问题：{args.question}\n")
    for rank, (score, d) in enumerate(hits, 1):
        where = f" · {d['section']}" if d.get("section") else ""
        print(f"[{rank}] {d['id']}  (score={score:.4f})")
        print(f"    来源：{d['source_title']}{where}")
        if d.get("source_url"):
            print(f"    原文：{d['source_url']}")
        print(f"    {d['text'][:80]}...\n")
    print("提示：BM25 得分是关键词相关性排序分数，不是置信度/概率。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
