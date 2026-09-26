# -*- coding: utf-8 -*-
"""第 1 步：切分文档 + 建索引。

用法：
    python 01_prepare.py                     # 全部知识库
    python 01_prepare.py --corpus rag_learning
产出：index/chunks.json（电力示例库）、index/chunks_rag_learning.json（RAG 技术学习库）
"""
import argparse
import json
import sys

import rag_core as rc


def main():
    ap = argparse.ArgumentParser(description="切分语料并导出 chunk 索引")
    ap.add_argument("--corpus", default=None,
                    help=f"知识库 id（{' / '.join(rc.corpus_ids())}），默认全部")
    args = ap.parse_args()

    ids = [args.corpus] if args.corpus else rc.corpus_ids()
    rc.INDEX_DIR.mkdir(parents=True, exist_ok=True)

    for cid in ids:
        corpus = rc.get_corpus(cid)
        docs = rc.load_documents(cid)
        out = corpus["index"]
        out.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
        stats = rc.corpus_stats(cid)
        print(f"[OK] {corpus['name']}：{stats['source_count']} 份来源 / {len(docs)} 个 chunk"
              f" / {stats['total_chars']} 字符 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
