# -*- coding: utf-8 -*-
"""Streamlit 工作台 · 第二版（独立入口 app_v2.py）

定位（25 号《界面第二版落地方案》第一步）：
  - **只改展示与操作路径**：复用 rag_core.py 的切分 / BM25 检索 / 同节补全 / 引用解析 / 生成，
    检索口径、语料、prompt、模型请求路径与 app.py 完全一致；
  - 保留原入口 app.py，便于同尺寸截图 A/B 对照与回退；
  - 不新增向量检索、多轮对话、上传管理、评测图表或任何新的模型请求路径。

相对 app.py 的界面差异（对应 25 号「设计稿落地前要改正的地方」1–5）：
  1. 唯一控件集：知识库 / 生成模式 / Top-K 只在右侧设置区出现一次；主区只显示
     **只读**的当前设置摘要，不重复放控件（避免两处控件状态不一致）。
  2. 状态互斥：无 Key / 零结果 / 生成失败 / 仅检索四种状态由真实状态决定，
     渲染路径是 if/elif 链，同一时刻最多显示一条，不会同时出现互相矛盾的提示卡。
  3. 数字不自造：版本、chunk 数、切分上限、分词器、k1/b、Top-K、BM25 分数全部来自
     corpus_stats() / 真实检索结果 / 当前控件值；界面里没有任何写死的演示常量。
  4. 提示随知识库切换：电力库才显示「示例资料、不是正式规程」；学习库显示真实来源与许可。
     API Key 只显示服务端「已检测到 / 未检测到」，没有前端开关，也不回显内容。
  5. 引用可对照：引用对照列出**完整 chunk id**（不画 `[1][2][3]` 式假链接），并标注该 id 属于
     「检索命中」还是「同节补充」；证据片段区把两者分区展示，补充片段明确不计入 Hit@K / MRR；
     BM25 得分标注为排序分而非置信度。

启动：streamlit run app_v2.py
"""
import time

import streamlit as st

import rag_core as rc
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

# ---------------- 页面基础设置（必须是第一个 st 命令） ----------------
st.set_page_config(page_title="RAG 问答工作台 · 第二版", page_icon="🔍", layout="wide")

# ---------------- 外观样式（只负责间距 / 字号 / 卡片外观，不承载任何数据） ----------------
st.markdown(
    """
    <style>
      .v2-steps { display: flex; gap: 8px; flex-wrap: wrap; margin: 2px 0 12px 0; }
      .v2-step { flex: 1 1 150px; border: 1px solid #e6e8eb; border-radius: 10px;
                 padding: 8px 10px; font-size: 13px; line-height: 1.4; background: #fafbfc; }
      .v2-step b { display: block; font-size: 11px; font-weight: 600; color: #6b7280; }
      .v2-step.active { border-color: #1e6fd9; background: #eef4fe; }
      .v2-step.done { border-color: #9bbde8; background: #f6faff; color: #4b5563; }
      .v2-step.pending { border-color: #e2b33c; background: #fffaf0; color: #8a6116; }
    </style>
    """,
    unsafe_allow_html=True,
)

STEPS = (("第 1 步", "提问"), ("第 2 步", "找证据"), ("第 3 步", "生成回答"), ("第 4 步", "对照引用"))


def render_steps(active: int, step4: str = STEPS[3][1]) -> None:
    """流程指示条：只反映**真实**的执行阶段，内容全为固定文案（不含用户数据）。

    step4 允许被替换：生成成功但没有解析到任何有效引用时，第 4 步既不能点亮、
    也不该写成「对照引用」——那时显示「引用检查：无可用引用」并标成 pending 样式。
    """
    labels = list(STEPS)
    labels[3] = (STEPS[3][0], step4)
    overridden = step4 != STEPS[3][1]
    cells = []
    for i, (no, label) in enumerate(labels, 1):
        cls = "v2-step active" if i == active else ("v2-step done" if i < active else "v2-step")
        if i == 4 and overridden:
            cls += " pending"
        cells.append(f'<div class="{cls}"><b>{no}</b>{label}</div>')
    st.markdown('<div class="v2-steps">' + "".join(cells) + "</div>", unsafe_allow_html=True)


# ---------------- 缓存层（避免重复调用模型；语料指纹变化时自动失效） ----------------
@st.cache_data(show_spinner=False)
def _assemble(query: str, top_k: int, fp: str, corpus_id: str):
    """缓存「检索 + 同节补全」的组装结果。"""
    return rc.expand_and_assemble(query, top_k=top_k, corpus_id=corpus_id)


@st.cache_data(show_spinner=False)
def _generate(question: str, top_k: int, fp: str, corpus_id: str):
    """缓存生成结果，避免界面重绘时重复调用模型（API 慢且花钱）。

    零结果直接返回 None，不调用模型；异常不缓存（st.cache_data 默认行为），
    避免把失败固化。注意：接口地址与模型名来自 config，与 app.py 完全一致，
    第二版没有额外增加任何模型请求路径。
    """
    assembled = _assemble(question, top_k, fp, corpus_id)
    if not assembled["hits"]:
        return None
    return rc.generate_answer(question, assembled["contexts"], DEEPSEEK_API_KEY,
                              DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, corpus_id)


# ---------------- 展示辅助 ----------------
def entry_meta(entry: dict) -> str:
    """来源与位置：来源标题 · 章节 · 来源组织（空值不占位）。"""
    bits = [entry.get("source_title") or entry.get("source_id") or ""]
    if entry.get("section"):
        bits.append(entry["section"])
    if entry.get("source_org"):
        bits.append(entry["source_org"])
    return " · ".join(bits)


def clip(text: str, n: int = 90) -> str:
    """把片段压成一行短文本，供「引用对照」列显示；完整正文见下方证据片段区。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= n else flat[:n] + "…"


def render_chunk(entry: dict, head: str) -> None:
    """渲染证据片段：命中带 BM25 得分（标注为非置信度），补充明确标注不计入指标。"""
    score = entry.get("score")
    title = f"{head} `{entry['id']}`"
    if score is not None:
        title += f" — BM25 得分 {score:.4f}（排序分，非置信度）"
    else:
        title += " — 同节补充（非检索命中）"
    with st.expander(title, expanded=False):
        st.markdown(entry["text"])
        st.caption(entry_meta(entry))
        if entry.get("source_url"):
            st.markdown(f"[查看原文]({entry['source_url']})")
        if score is None:
            st.caption("该片段是生成阶段按「同 source_id + 同章节」补入的相邻内容，"
                       "**不计入检索命中，也不计入 Hit@K / MRR**。")
        else:
            st.caption("BM25 得分只表示该段与问题在**关键词**上的相关程度排序，"
                       "不是置信度、不是正确率；检索层没有相关性阈值。")


# ---------------- 右侧设置区（全页唯一的控件入口） ----------------
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

    mode = st.radio("生成模式", ["仅检索", "检索并生成"], index=1)
    top_k = st.selectbox("Top-K（检索返回的段数）", options=[1, 2, 3, 4, 5], index=2)

    has_key = bool(DEEPSEEK_API_KEY)
    if has_key:
        st.success("API Key：服务端已检测到（来自环境变量）")
        st.caption("界面不显示 key 内容，也不提供前端开关；能否真正调用以实际请求结果为准。")
    else:
        st.warning("API Key：服务端未检测到")
        st.caption("生成模式只会展示检索结果，不调用模型。")

    st.divider()
    st.subheader("资料与参数")
    stats = rc.corpus_stats(cid)
    st.caption(f"来源 **{stats['source_count']}** 份 · chunk **{stats['chunk_count']}** 个 · "
               f"{stats['total_chars']} 字符")
    st.caption(f"切分上限 {stats['params']['chunk_max_chars']} 字符（无 overlap）")
    with st.expander("更多信息", expanded=False):
        st.markdown(f"- 语料版本：{stats['version']}")
        st.markdown(f"- 分词器：{stats['tokenizer']}；BM25 k1={stats['params']['bm25_k1']} "
                    f"b={stats['params']['bm25_b']}；Top-K 默认 {stats['params']['top_k_default']}")
        if corpus["kind"] == "section":
            st.markdown("- 切分链路：按 Markdown 标题分节 → 按空行分段（相邻列表合并）"
                        "→ 超过上限再按字数切；chunk id = `{来源}#s{节}p{段}`")
        else:
            st.markdown("- 切分链路：按空行分段 → 超过上限再按字数切；"
                        "chunk id = `{文件名}#P{段}`（该库无标题层级与来源元数据）")
        st.markdown(f"- 语料指纹：`{stats['fingerprint'][:24]}…`（按文件字节内容计算，改语料即失效）")
        st.markdown("- 检索只有 **BM25 关键词检索**：没有向量检索、混合检索、reranker、多轮对话。")
        st.markdown("- BM25 得分是排序分，不是置信度；检索层**没有**相关性阈值，"
                    "因此「没有命中」只表示没有词面相关的段落。")

    st.divider()
    st.subheader("使用提示")
    st.markdown("- **命中 ≠ 答对**：Hit@K 只检查规定的证据片段有没有进前 K 名，不评判回答质量。")
    st.markdown("- **补充 ≠ 命中**：同节补充是生成时才补入的上下文，不计入任何检索指标。")
    st.markdown("- 生成层尚未完成正式验证（目前只有单轮 8 例用户侧样本），回答仅供参考。")
    if cid == rc.POWER_DEMO:
        st.markdown("- 本库是**示例资料，不是正式规程**，不能用于现场作业或安全决策。")
    else:
        st.markdown("- 学习库资料来自 Datawhale《All-in-RAG》，许可是 CC BY-NC-SA 4.0"
                    "（署名 · 非商业 · 相同方式共享）。")


# ---------------- 主区：提问（只读摘要，不重复放控件） ----------------
st.title("🔍 RAG 问答工作台")
st.caption(f"单轮问答 · 检索 BM25（{stats['tokenizer']} 分词）· 生成模型 {DEEPSEEK_MODEL}"
           " · 生成层尚未完成正式验证")

q_col, btn_col = st.columns([5, 1], gap="small")
question = q_col.text_input("输入你的问题", placeholder="例如：RAG 评估三元组包含哪三个维度？",
                            label_visibility="collapsed")
submit = btn_col.button("开始检索", type="primary")

st.caption(f"当前设置：知识库 **{corpus['name']}** · 生成模式 **{mode}** · Top-K **{top_k}**"
           "（以上设置只在右侧「设置」里改，主区不重复放置控件）")

if cid == rc.POWER_DEMO:
    st.warning("本知识库是**示例资料，不是正式规程**，不可用于现场作业或安全决策。")
else:
    # 版本串形如 "583a61b（CC BY-NC-SA 4.0（署名…））"，这里拆成 commit 与许可两段展示
    version = stats["version"]
    if "（" in version:
        commit_short, license_text = version.split("（", 1)
        st.caption(f"资料来源与许可：commit {commit_short} · 许可 {license_text.rstrip('）')}")
    else:
        st.caption(f"资料来源与许可：{version}")


# ---------------- 本次运行状态 ----------------
def _generation(run: dict, rq: str, rtop: int, rfp: str, rcid: str,
                want_generate: bool, hits: list):
    """受控生成：只有「生成模式 + 有命中 + 服务端检测到 Key」三条同时成立才会走到模型调用。

    结果按本次提问缓存进 session_state，避免侧栏交互导致重绘时重复请求；
    失败时缓存错误文本，避免每次重绘都重打一次失败端点。
    """
    if not want_generate or not hits or not has_key:
        return None, None, None
    key = f"{rq}\x1f{rtop}\x1f{rfp}\x1f{rcid}"
    if run.get("gen_key") == key:
        return run.get("answer"), run.get("gen_error"), run.get("gen_ms")
    t1 = time.perf_counter()
    try:
        answer, gen_error = _generate(rq, rtop, rfp, rcid), None
    except Exception as e:                      # noqa: BLE001 —— 失败原因要原样让用户看到
        answer, gen_error = None, f"{type(e).__name__}: {e}"
    gen_ms = (time.perf_counter() - t1) * 1000
    run.update({"gen_key": key, "answer": answer, "gen_error": gen_error, "gen_ms": gen_ms})
    return answer, gen_error, gen_ms


if "v2_run" not in st.session_state:
    st.session_state["v2_run"] = None

if submit:
    if question.strip():
        st.session_state["v2_run"] = {
            "question": question.strip(),
            "corpus_id": cid,
            "mode": mode,
            "top_k": top_k,
            "fingerprint": rc.corpus_fingerprint(cid),
        }
    else:
        st.session_state["v2_run"] = {"empty": True}

run = st.session_state["v2_run"]

if not run:
    render_steps(1)
    st.info("输入问题后点击「开始检索」。检索与生成是两步：**没有命中就不会调用模型**；"
            "检索命中、回答、引用对照与证据片段会依次出现在下面。")
    st.stop()

if run.get("empty"):
    render_steps(1)
    st.warning("请先输入问题。")
    st.stop()

# 本次结果的相关参数（与侧栏当前值分开保存，避免"设置已改、结果还是旧的"被混淆）
rq, rcid, rmode, rtop, rfp = (run["question"], run["corpus_id"], run["mode"],
                              run["top_k"], run["fingerprint"])
rname = rc.corpus_name(rcid)
want_generate = rmode == "检索并生成"

changed = []
if cid != rcid:
    changed.append(f"知识库 → {rc.corpus_name(cid)}")
if mode != rmode:
    changed.append(f"生成模式 → {mode}")
if top_k != rtop:
    changed.append(f"Top-K → {top_k}")

st.subheader(f"问题：{rq}")
st.caption(f"本次结果使用的设置：知识库 {rname} · 生成模式 {rmode} · Top-K {rtop}")
if changed:
    st.warning("右侧设置已变更（" + "、".join(changed) + "），但下面显示的仍是上一次提问的结果；"
               "请重新点击「开始检索」按新设置重跑。")

# —— 第 1 步：检索 + 组装生成上下文（口径与 app.py / CLI 完全一致） ——
t0 = time.perf_counter()
assembled = _assemble(rq, rtop, rfp, rcid)
t_retrieve = (time.perf_counter() - t0) * 1000
hits, neighbors = assembled["hits"], assembled["neighbors"]
ctx_map = {e["id"]: e for e in assembled["contexts"]}

# —— 第 2 步：生成（受控路径） ——
answer, gen_error, gen_ms = _generation(run, rq, rtop, rfp, rcid, want_generate, hits)
gen_attempted = bool(want_generate and hits and has_key)
# 引用必须在点亮步骤条**之前**解析：回答成功但没有可用引用时，第 4 步不能假装完成
refs = rc.extract_refs(answer, ctx_map) if answer else []

if not hits or gen_error:
    render_steps(3 if gen_error else 2)
elif refs:
    render_steps(4)
elif gen_attempted:
    render_steps(3, "引用检查：无可用引用")
else:
    render_steps(2)

# —— 状态区：if/elif 互斥，同一时刻最多一条 ——
if not hits:
    st.warning("**未检索到相关资料：不生成回答。** 当前知识库里没有与问题词面相关的片段，"
               "因此没有调用模型（零结果短路）。")
elif want_generate and not has_key:
    st.info("**无 API Key：仅展示检索结果。** 服务端未检测到 API Key（环境变量），因此不调用模型；"
            "界面不提供 key 输入框，也不会回显 key。")
elif gen_error:
    st.error(f"**模型调用失败：已保留检索到的资料。** {gen_error}")
elif not want_generate:
    st.info("**仅检索模式：不调用模型。** 下面只有 BM25 检索结果与证据片段，没有回答与引用对照。")

# —— 回答 / 引用对照：并列 ——
ans_col, ref_col = st.columns([1, 1], gap="medium")

with ans_col:
    st.markdown("#### 回答")
    if answer is not None:
        st.caption(f"生成耗时 {gen_ms:.1f} ms · **生成层尚未完成正式验证，回答仅供参考**"
                   "（离线验收里「无依据不伪造」（S4）是**尚未验证（NOT VALIDATED）**，"
                   "不是已执行且失败——两者不能混为一谈）。")
        st.markdown(answer)
    elif gen_error:
        st.caption("本次没有生成回答：模型调用失败，检索结果与证据片段仍完整保留在下方。")
    elif not want_generate:
        st.caption("本次没有生成回答：当前是仅检索模式。")
    elif not has_key:
        st.caption("本次没有生成回答：服务端未检测到 API Key。")
    else:
        st.caption("本次没有生成回答：没有检索命中，生成被短路。")

with ref_col:
    st.markdown("#### 引用对照")
    if refs:
        raw = rc.count_ref_occurrences(answer)
        st.caption(f"共 **{len(refs)}** 处不同来源；回答正文里方括号引用共出现 **{raw}** 次"
                   "（含重复与对不上上下文的写法），重复的只列一次。这里显示**完整 chunk id**，"
                   "与回答正文里的 [id] 一一对应，不另做简写编号、不画跳转链接；完整正文见下方证据片段。")
        for rid in refs:
            e = ctx_map[rid]
            tag = "检索命中" if e["origin"] == "hit" else "同节补充"
            st.markdown(f"- `{rid}` · **{tag}** · {entry_meta(e)}\n\n    {clip(e['text'])}")
    elif answer:
        st.caption("**本次回答没有可用引用**：回答里没有出现上下文中的任何 chunk id，"
                   "因此第 4 步不点亮（步骤条显示「引用检查：无可用引用」）。"
                   "这只能说明引用解析没对上，不代表回答正确或错误。")
    elif gen_attempted and gen_error:
        st.caption("模型调用失败，没有回答可解析引用。")
    else:
        st.caption("没有生成回答，因此没有引用可对照。")

# —— 证据片段（可展开）：命中与补充分区 ——
st.divider()
st.markdown("#### 证据片段")
if not hits:
    st.caption("本次没有检索命中，没有可展示的片段。")
else:
    # 同节补充只在**真的走到生成调用**时才展示：没调用模型就没有"生成上下文"这回事，
    # 不能让人误以为这些片段参与过本次生成。
    if gen_attempted:
        ctx_line = (f" · 本次实际送入生成的上下文共 {len(assembled['contexts'])} 段 / "
                    f"{assembled['total_chars']} 字符（命中 {assembled['hit_chars']} + 补充 "
                    f"{assembled['total_chars'] - assembled['hit_chars']}）")
    else:
        ctx_line = " · 本次没有调用模型，因此不存在生成上下文"
    st.caption(f"检索：BM25 Top-{rtop} 命中 **{len(hits)}** 段（**这一段才进入 Hit@K / MRR 口径**）"
               f" · 检索耗时 {t_retrieve:.1f} ms" + ctx_line)
    st.markdown(f"**检索命中（{len(hits)} 段）**")
    for entry in hits:
        render_chunk(entry, f"命中 {entry['rank']}")
    if gen_attempted and neighbors:
        st.markdown(f"**同节补充（{len(neighbors)} 段，只在生成时使用）**")
        st.caption(f"按「同 source_id + 同章节」在命中片段左右各补 {assembled['radius']} 段，"
                   f"补充部分受 {assembled['max_chars']} 字符预算约束（真实命中不受限）。"
                   "**补充片段不是检索命中，不计入 Hit@K / MRR。**")
        for entry in neighbors:
            render_chunk(entry, "补充")
    elif gen_attempted:
        st.caption("本次没有可补入的同节相邻片段。")
    elif not want_generate:
        st.caption("同节补充只在生成阶段使用；本次是仅检索模式，因此不展示补充片段。")
    else:
        st.caption("本次没有调用模型（服务端未检测到 API Key），"
                   "因此不展示只在生成阶段才会用到的同节补充片段。")
