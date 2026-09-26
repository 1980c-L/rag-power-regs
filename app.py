# -*- coding: utf-8 -*-
"""Streamlit 单页工作台 —— RAG 问答助手（双知识库）

复用 rag_core.py 的切分/检索/生成逻辑，不修改 CLI 用法。
启动：streamlit run app.py

设计要点：
  - 知识库选择：电力示例库 / RAG 技术学习库（两个库完全隔离，不串库）
  - 语料统计：来源数、chunk 数、版本与切分参数一目了然
  - 两种模式：仅检索 / 检索并生成
  - **检索命中与同节补充严格分开展示**：真实 BM25 Top-K 一套，生成时补入的
    同节相邻上下文另一套（逐条折叠、明确标注"不计入 Hit@K/MRR"）
  - 引用对照按首次出现顺序**去重**展示（回答正文保持不变）
  - BM25 得分明确标注"非置信度"
  - 缓存按「语料指纹 + 知识库」失效，界面重绘不重复调用模型
"""
import time

import streamlit as st

import rag_core as rc
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

# ---------------- 页面基础设置（必须是第一个 st 命令） ----------------
st.set_page_config(page_title="RAG 问答工作台", page_icon="🔍", layout="wide")


# ---------------- 缓存层（避免重复调用模型） ----------------
@st.cache_data(show_spinner=False)
def _assemble(query: str, top_k: int, fp: str, corpus_id: str):
    """缓存"检索 + 同节补全"的组装结果；语料指纹变化时自动重算。"""
    return rc.expand_and_assemble(query, top_k=top_k, corpus_id=corpus_id)


@st.cache_data(show_spinner=False)
def _generate(question: str, top_k: int, fp: str, corpus_id: str):
    """缓存生成结果，避免界面重绘时重复调用模型（API 慢且花钱）。

    零结果时直接返回 None，不调用模型。
    抛异常时 st.cache_data 默认不缓存异常，不会把错误结果固化。
    """
    assembled = _assemble(question, top_k, fp, corpus_id)
    if not assembled["hits"]:
        return None
    return rc.generate_answer(question, assembled["contexts"], DEEPSEEK_API_KEY,
                              DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, corpus_id)


def render_entry(entry: dict, prefix: str, expanded: bool) -> None:
    """渲染一个上下文片段（真实命中或同节补充）。"""
    score = entry.get("score")
    head = f"{prefix} {entry['id']}"
    if score is not None:
        head += f" — 检索得分(BM25) {score:.4f}"
    else:
        head += " — 同节补充上下文"
    with st.expander(head, expanded=expanded):
        st.markdown(entry["text"])
        meta = [f"**来源**：{entry.get('source_title') or entry['source_id']}"]
        if entry.get("section"):
            meta.append(f"**章节**：{entry['section']}")
        if entry.get("source_org"):
            meta.append(f"**来源组织**：{entry['source_org']}")
        st.caption(" · ".join(meta))
        if entry.get("source_url"):
            st.markdown(f"[查看原文]({entry['source_url']})")
        if score is not None:
            st.caption("注：BM25 得分为关键词相关性排序分数，非置信度/概率。")
        else:
            st.caption("注：该片段是生成时按「同 source_id + 同章节」补入的相邻内容，"
                       "**不计入检索命中，也不计入 Hit@K / MRR**。")


# ---------------- 侧边栏 ----------------
with st.sidebar:
    st.header("设置")

    cid = st.selectbox(
        "知识库",
        options=rc.corpus_ids(),
        format_func=rc.corpus_name,
        index=rc.corpus_ids().index(rc.RAG_LEARNING),   # 默认进真实资料库
    )
    corpus = rc.get_corpus(cid)
    st.caption(corpus["note"])

    mode = st.radio("模式", ["仅检索", "检索并生成"], index=1)
    top_k = st.slider("Top-K 检索数量", 1, 5, 3)

    has_key = bool(DEEPSEEK_API_KEY)
    if mode == "检索并生成":
        if has_key:
            st.success("已检测到 API key（服务端环境变量）")
        else:
            st.warning("未检测到 API key，将仅展示检索结果（不调用模型）")

    st.divider()
    st.subheader("语料信息")
    stats = rc.corpus_stats(cid)
    st.caption(
        f"来源 **{stats['source_count']}** 份 · chunk **{stats['chunk_count']}** 个 · "
        f"{stats['total_chars']} 字符"
    )
    st.caption(f"版本：{stats['version']}")
    st.caption(
        f"切分上限 {stats['params']['chunk_max_chars']} 字符（无 overlap） · "
        f"分词 {stats['tokenizer']} · k1={stats['params']['bm25_k1']} b={stats['params']['bm25_b']}"
    )
    st.caption(f"语料指纹：{stats['fingerprint'][:24]}…")
    if cid == rc.RAG_LEARNING:
        st.info(
            "本库资料来自 Datawhale《All-in-RAG》（CC BY-NC-SA 4.0，"
            "署名 · 非商业 · 相同方式共享）。BM25 得分是关键词相关性，不是置信度。"
        )
    if cid == rc.POWER_DEMO:
        st.warning("电力库为**示例资料，不是正式规程**，不可用于现场作业或安全决策。")


# ---------------- 主区 ----------------
st.title("🔍 RAG 问答工作台")
st.caption(f"当前知识库：{corpus['name']} · 单轮问答 · BM25 检索 + 大模型生成")

question = st.text_input("输入你的问题", placeholder="例如：RAG 评估三元组包含哪三个维度？")
submit = st.button("提问", type="primary")

if submit and question.strip():
    q = question.strip()
    fp = rc.corpus_fingerprint(cid)

    # —— 第 1 步：检索 + 组装生成上下文 ——
    t0 = time.perf_counter()
    assembled = _assemble(q, top_k, fp, cid)
    t_retrieve = time.perf_counter() - t0
    hits, neighbors = assembled["hits"], assembled["neighbors"]

    st.subheader("检索结果（BM25 真实命中）")
    st.caption(f"检索耗时 {t_retrieve * 1000:.1f} ms · 命中 {len(hits)} 段")

    if not hits:
        # —— 空结果路径：不调用模型 ——
        st.info("未检索到相关内容，未调用模型。")
    else:
        for entry in hits:
            render_entry(entry, prefix=f"[{entry['rank']}]", expanded=entry["rank"] == 1)

    # —— 生成时补入的同节相邻上下文：与命中分开呈现 ——
    if mode == "检索并生成" and hits and neighbors:
        st.caption(
            f"生成时另补入 **同节相邻上下文 {len(neighbors)} 段**"
            f"（共 {assembled['total_chars']} 字符，预算 {assembled['max_chars']}）——"
            "用于补全同一章节里的枚举，**不计入 Hit@K / MRR**。"
        )
        st.markdown(f"**补充的 {len(neighbors)} 段相邻上下文**")
        for entry in neighbors:
            render_entry(entry, prefix="＋", expanded=False)

    # —— 第 2 步：生成 ——
    if mode == "检索并生成" and hits:
        if not has_key:
            st.info("未检测到 API key，已展示检索结果，跳过生成。")
        else:
            t1 = time.perf_counter()
            try:
                answer = _generate(q, top_k, fp, cid)
                t_gen = time.perf_counter() - t1
                st.subheader("回答")
                st.caption(f"生成耗时 {t_gen * 1000:.1f} ms · "
                           "生成层尚未完成正式验证，回答仅供参考")
                st.markdown(answer)

                # —— 答案与引用对照（按首次出现顺序去重） ——
                ctx_map = {e["id"]: e for e in assembled["contexts"]}
                refs = rc.extract_refs(answer, ctx_map)
                if refs:
                    raw_count = rc.count_ref_occurrences(answer)
                    st.markdown("**引用对照**"
                                + (f"（共 {len(refs)} 处不同来源）" if raw_count > len(refs) else ""))
                    for rid in refs:
                        e = ctx_map[rid]
                        tag = "检索命中" if e["origin"] == "hit" else "同节补充"
                        st.markdown(f"- `{rid}`（{tag}）：{e['text']}")
                elif answer:
                    st.caption("未从回答中解析到可用引用。")
            except Exception as e:
                # —— API 失败路径：保留上方检索结果，仅提示错误 ——
                st.error(f"生成失败（已保留上方检索结果）：{type(e).__name__}: {e}")
