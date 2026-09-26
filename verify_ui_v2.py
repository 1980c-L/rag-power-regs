# -*- coding: utf-8 -*-
"""第二版界面（app_v2.py）离线验收 —— 全程零真实模型请求。

对应 25 号《界面第二版落地方案》第二步「六条路径验收」，
并补 27 号独立复审提出的两个反例：生成成功但**无可用引用**（第 4 步不得点亮）、
以及 S4 必须写成「尚未验证（NOT VALIDATED）」而不是「未通过」。

本地桩：脚本内起一个 127.0.0.1 回环地址上的假 OpenAI 兼容端点，固定返回
21 号证据里**已记录的真实回答原文**；不访问任何外部地址。回答原文来源二选一：
  - `--answers-dir <21 号证据目录>`：从 01-三元组.json / 07-接地线.json 读原文（推荐）；
  - 不传则用本文件内嵌的副本；
两者同时可用时会做**逐字节一致性校验**，防止内嵌副本与证据漂移。

运行：
    python verify_ui_v2.py --answers-dir <21 号证据目录> --out <证据输出目录>
本文件内不写任何个人绝对路径（verify_flows.py 的 S10 隐私扫描会扫到本文件）。
"""
import argparse
import importlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
STUB_PORT = 8899
STUB_URL = f"http://127.0.0.1:{STUB_PORT}"
# 假凭据在运行期拼接，且长度刻意短于 sk- 密钥形态，避免本文件被 S10 密钥扫描命中
FAKE_KEY = "sk-" + "ui" + "v2" + "stub"

PASSED: list = []
NOT_VALIDATED: list = []
EVIDENCE: dict = {"scenarios": []}

# ---------------- 桩的回答原文（21 号证据内嵌副本，运行期会与证据文件比对） ----------------
EMBEDDED = {
    "三元组": (
        "RAG 评估三元组包含以下三个维度：\n\n"
        "1. **上下文相关性 (Context Relevance)**：评估检索器（Retriever）的性能，核心问题是检索到的"
        "上下文内容是否与用户的查询（Query）高度相关 [air-evaluation#s2p2]。\n\n"
        "2. **忠实度 / 可信度 (Faithfulness / Groundedness)**：评估生成器的可靠性，核心问题是生成的"
        "答案是否完全基于所提供的上下文信息 [air-evaluation#s2p3]。\n\n"
        "3. **答案相关性 (Answer Relevance)**：评估系统的端到端（End-to-End）表现，核心问题是最终"
        "生成的答案是否直接、完整且有效地回答了用户的原始问题 [air-evaluation#s2p4]。\n\n"
        "这三个维度共同构成了 RAG 三元组这一诊断视角，通过对它们的评估，可以初步判断问题主要来自"
        "检索还是生成环节 [air-evaluation#s2p6]。需要注意的是，RAG 三元组是一种诊断视角，并不是"
        "完整的评估清单，实际项目通常还需要关注答案正确性与完整性、不可回答问题的拒答能力、鲁棒性，"
        "以及延迟、吞吐量和调用成本等系统指标 [air-evaluation#s2p7]。"
    ),
    "接地线": (
        "装设接地线时，应先接接地端，后接导体端；拆除接地线的顺序与此相反。"
        "[来源：示例语料-电力安全通用要点#P5]"
    ),
    # 27 号复审的反例：有检索命中、但回答里没有任何可用引用
    "红烧肉": "资料中未找到相关依据。",
}
ANSWER_FILES = {"三元组": "01-三元组.json", "接地线": "07-接地线.json",
                "红烧肉": "03-红烧肉.json"}


def check(name: str, ok: bool, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        raise AssertionError(f"{name} 未通过：{detail}")


def mark_not_validated(name: str, detail: str = "") -> None:
    NOT_VALIDATED.append(name)
    print(f"[NOT VALIDATED] {name}" + (f" — {detail}" if detail else ""), flush=True)


def load_answers(dirpath: Path) -> dict:
    out = {}
    for kw, fname in ANSWER_FILES.items():
        data = json.loads((dirpath / fname).read_text(encoding="utf-8"))
        out[kw] = data["answer"]
    return out


# ---------------- 本地桩：假 OpenAI 兼容端点（只监听回环地址） ----------------
STATE = {"mode": "ok", "requests": [], "answers": dict(EMBEDDED)}


def pick_answer(question: str, prompt: str) -> str:
    """按**问题**（而不是整段 prompt）选固定回答。

    注意：prompt 里有一段固定指令本身就含「三元组」字样，若按整段 prompt 匹配，
    电力库的问题也会被误判成「三元组」题，所以必须先只看问题文本。
    """
    for kw, ans in STATE["answers"].items():
        if kw in question:
            return ans
    body = prompt.split("【资料】", 1)[-1]      # 兜底：只在资料正文里找，不碰指令段
    for kw, ans in STATE["answers"].items():
        if kw in body:
            return ans
    return "（本地桩）根据所给资料无法给出更多结论。"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                    # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        prompt = ""
        try:
            prompt = json.loads(raw.decode("utf-8"))["messages"][0]["content"]
        except Exception:                                  # noqa: BLE001
            pass
        q = ""
        if "【问题】" in prompt:
            q = prompt.split("【问题】\n", 1)[1].split("\n\n【回答】")[0].strip()
        STATE["requests"].append(q)

        if STATE["mode"] == "fail":
            body = json.dumps({"error": "stub failure"}).encode("utf-8")
            self.send_response(500)
        else:
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant",
                                          "content": pick_answer(q, prompt)}}]},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                          # 静默，避免刷屏
        pass


def start_stub() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------------- 运行环境：把生成指向本机桩 ----------------
os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
os.environ["DEEPSEEK_BASE_URL"] = STUB_URL
os.environ["DEEPSEEK_MODEL"] = "stub-model"

import streamlit as st                                     # noqa: E402
from streamlit.testing.v1 import AppTest                   # noqa: E402

import config                                              # noqa: E402
import rag_core as rc                                      # noqa: E402

PAGE = str(REPO / "app_v2.py")


def set_key(present: bool) -> None:
    """切换「服务端是否检测到 API Key」：改环境变量后重载 config（app_v2 每次运行都重新 import）。"""
    if present:
        os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    else:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    importlib.reload(config)


def run_page(corpus_id=None, mode=None, top_k=None, question=None, click=True) -> "AppTest":
    """打开真实入口 app_v2.py 并按参数交互；click=False 时只加载页面不提问。"""
    st.cache_data.clear()          # 避免上一路径的缓存结果串味（stub 回答按问题缓存）
    at = AppTest.from_file(PAGE, default_timeout=180).run()
    if corpus_id is not None:
        at.selectbox[0].select(corpus_id)
    if mode is not None:
        at.radio[0].set_value(mode)
    if top_k is not None:
        at.selectbox[1].select(top_k)
    if question is not None:
        at.text_input[0].set_value(question)
    if click:
        return at.button[0].click().run()
    return at.run()


def page_text(at: "AppTest") -> str:
    parts = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "text",
                 "info", "warning", "error", "success", "code"):
        for el in at.get(kind):
            v = getattr(el, "value", None)
            if v:
                parts.append(str(v))
    for e in at.expander:
        parts.append(str(e.label))
    text = "\n".join(parts)
    # 每次抓页面文本都顺手记一份"状态卡"快照，用来核对状态互斥（同一时刻最多一条）
    CARD_SNAPSHOTS.append([k for k in CARD_KEYS if k in text])
    return text


# 第二版里 4 张状态卡的唯一识别文案（互斥判据），改文案会让这里的快照统计失效
CARD_KEYS = ("未检索到相关资料：不生成回答", "无 API Key：仅展示检索结果",
             "模型调用失败：已保留检索到的资料", "仅检索模式：不调用模型")
CARD_SNAPSHOTS: list = []


def alert_values(at: "AppTest", kind: str) -> list:
    return [str(getattr(el, "value", "")) for el in at.get(kind)]


def exp_labels(at: "AppTest") -> list:
    return [str(e.label) for e in at.expander]


def record(name: str, **facts) -> None:
    EVIDENCE["scenarios"].append({"scenario": name, **facts})


# ================= V 组：设计稿 5 处问题的结构性核对 =================
def v_design_fixes() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, "RAG 评估三元组包含哪三个维度？")
    text = page_text(at)

    check("V1 全页只有一套控件（知识库/Top-K 各 1 个下拉、生成模式 1 个单选、问题 1 个输入框，"
          "主区没有重复控件）",
          len(at.selectbox) == 2 and len(at.radio) == 1 and len(at.text_input) == 1,
          f"selectbox={len(at.selectbox)} radio={len(at.radio)} text_input={len(at.text_input)}")

    check("V2 主区只有只读设置摘要，且摘要值与右侧控件一致",
          f"当前设置：知识库 **{rc.corpus_name(rc.RAG_LEARNING)}** · 生成模式 **检索并生成** · "
          f"Top-K **3**" in text
          and "只在右侧「设置」里改" in text)

    stats = rc.corpus_stats(rc.RAG_LEARNING)
    check("V3 参数取自真实数据：切分上限/分词器与 corpus_stats 一致，且不含设计稿的假参数",
          f"切分上限 {stats['params']['chunk_max_chars']} 字符（无 overlap）" in text
          and stats["tokenizer"] in text
          and "切分长度 800" not in text and "重叠 120" not in text,
          f"chunk_max_chars={stats['params']['chunk_max_chars']} tokenizer={stats['tokenizer']}")

    check("V4 API Key 是只读的服务端状态（无前端开关、无 key 输入框、不回显 key）",
          "服务端已检测到" in text and "不提供前端开关" in text
          and len(at.checkbox) == 0 and len(at.text_area) == 0,
          f"checkbox={len(at.checkbox)} text_area={len(at.text_area)}")

    check("V5 BM25 得分标注为排序分而非置信度",
          "排序分，非置信度" in text and "没有相关性阈值" in text)

    check("V6 生成层未验证与「命中≠答对」提示在页面上",
          "生成层尚未完成正式验证" in text and "命中 ≠ 答对" in text)


# ================= 路径 1：学习库正常检索 + 本地桩生成 =================
def path1_learning_stub_ok() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    before = len(STATE["requests"])
    q = "RAG 评估三元组包含哪三个维度？"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, q)
    text = page_text(at)
    labels = exp_labels(at)

    hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    assembled = rc.expand_and_assemble(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    hit_ids = [d["id"] for _, d in hits]
    neigh_ids = [e["id"] for e in assembled["neighbors"]]
    ctx_map = {e["id"]: e for e in assembled["contexts"]}
    refs = rc.extract_refs(EMBEDDED["三元组"], ctx_map)

    check("P1a 页面无异常、无错误提示", not at.exception and not at.error,
          f"exception={len(at.exception)} error={len(at.error)}")
    check("P1b 回答来自本地桩，且与 21 号已记录的真实回答原文逐字节一致",
          EMBEDDED["三元组"] in text,
          f"回答 {len(EMBEDDED['三元组'])} 字符")
    check("P1c 正向对照：桩确实收到了 1 次请求（本轮零真实模型请求）",
          len(STATE["requests"]) == before + 1 and STATE["requests"][-1] == q,
          f"桩请求数 {before} → {len(STATE['requests'])}，问题={STATE['requests'][-1]!r}")
    check("P1d 真实命中数与 rag_core 检索结果一致，且每个命中 id 都出现在页面上",
          all(i in text for i in hit_ids) and len(hit_ids) == 3,
          f"命中 {hit_ids}")
    check("P1e 引用对照列出完整 chunk id（去重后与 extract_refs 完全一致，不画假链接）",
          all(r in text for r in refs) and len(refs) == len(set(refs)) and "[1]" not in text,
          f"引用 {refs}")
    check("P1f 引用逐条标注「检索命中 / 同节补充」，两类都出现（命中与补充不混同）",
          all(("检索命中" if ctx_map[r]["origin"] == "hit" else "同节补充")
              in _ref_block(at, r) for r in refs)
          and any(ctx_map[r]["origin"] == "hit" for r in refs)
          and any(ctx_map[r]["origin"] == "neighbor" for r in refs),
          " · ".join(f"{r}={'hit' if ctx_map[r]['origin'] == 'hit' else 'neighbor'}" for r in refs))
    check("P1g 证据片段区把命中与补充分成两个区块、数量与真实数据一致",
          f"**检索命中（{len(hit_ids)} 段）**" in text
          and f"**同节补充（{len(neigh_ids)} 段，只在生成时使用）**" in text,
          f"命中 {len(hit_ids)} / 补充 {len(neigh_ids)}")
    check("P1h 折叠块精确计数 = 1（侧栏更多信息）+ 命中 + 补充，无多余外层折叠",
          len(at.expander) == 1 + len(hit_ids) + len(neigh_ids),
          f"页面 {len(at.expander)} 个折叠块（期望 {1 + len(hit_ids) + len(neigh_ids)}）")
    check("P1i 补充片段明确不计入 Hit@K / MRR",
          "不计入 Hit@K / MRR" in text)

    record("路径1 学习库 + 本地桩生成",
           question=q, hits=hit_ids, neighbors=neigh_ids, refs=refs,
           expanders=labels, stub_requests=len(STATE["requests"]))


def _steps_html(at: "AppTest") -> str:
    """取步骤条的 HTML。

    注意：不能只在 markdown 里找 "v2-steps" —— 页面样式 `<style>` 块里也有这个选择器，
    会被先匹配到，导致断言看着"核过了"其实核的是 CSS（这里踩过一次）。
    """
    for el in at.get("markdown"):
        v = str(getattr(el, "value", ""))
        if v.lstrip().startswith('<div class="v2-steps">'):
            return v
    return ""


def _ref_block(at: "AppTest", rid: str) -> str:
    """取出引用对照里包含该 id 的那一条 markdown，用来核对它的类型标签。"""
    for el in at.get("markdown"):
        v = str(getattr(el, "value", ""))
        if v.startswith(f"- `{rid}`"):
            return v
    return ""


# ================= 路径 2：电力库第 07 题旧回答 =================
def path2_power_legacy_answer() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    q = "装设接地线的顺序是什么？"
    at = run_page(rc.POWER_DEMO, "检索并生成", 3, q)
    text = page_text(at)

    assembled = rc.expand_and_assemble(q, top_k=3, corpus_id=rc.POWER_DEMO)
    ctx_map = {e["id"]: e for e in assembled["contexts"]}
    refs = rc.extract_refs(EMBEDDED["接地线"], ctx_map)
    rid = "示例语料-电力安全通用要点#P5"

    check("P2a 电力库第 07 题旧回答被正确渲染（含 [来源：<id>] 写法）",
          EMBEDDED["接地线"] in text and not at.error)
    check("P2b 引用对照把 #P5 解析出来并标为「检索命中」",
          refs == [rid] and f"- `{rid}` · **检索命中**" in text,
          f"refs={refs}")
    check("P2c 示例资料警告随知识库切换出现",
          "示例资料，不是正式规程" in text)
    check("P2d 学习库内容不串入电力库页面", "air-" not in text)
    check("P2e 参数随库切换（电力库 cut 参数与 corpus_stats 一致、无章节字段）",
          f"切分上限 {rc.corpus_stats(rc.POWER_DEMO)['params']['chunk_max_chars']} 字符"
          "（无 overlap）" in text)

    record("路径2 电力库第07题旧回答",
           question=q, refs=refs, hits=[e["id"] for e in assembled["hits"]],
           neighbors=[e["id"] for e in assembled["neighbors"]])


# ================= 路径 3：仅检索模式 =================
def path3_retrieve_only() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    before = len(STATE["requests"])
    q = "RAG 评估三元组包含哪三个维度？"
    at = run_page(rc.RAG_LEARNING, "仅检索", 3, q)
    text = page_text(at)

    hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)

    check("P3a 仅检索模式：有命中但没有生成调用", len(STATE["requests"]) == before,
          f"桩请求数保持 {before}")
    check("P3b 页面明确说明「仅检索模式：不调用模型」",
          any("仅检索模式：不调用模型" in v for v in alert_values(at, "info")))
    check("P3c 检索结果仍在页面上并逐条可展开",
          len(at.expander) == 1 + len(hits) and len(hits) > 0,
          f"折叠块 {len(at.expander)}（期望 {1 + len(hits)}）")
    check("P3d 仅检索模式不展示生成阶段才用的同节补充",
          "**同节补充（" not in text and "同节补充只在生成阶段使用" in text)
    check("P3e 没有回答、没有引用对照内容，也没有错误",
          "没有生成回答：当前是仅检索模式" in text and not at.error)

    record("路径3 仅检索模式", question=q, hits=[d["id"] for _, d in hits],
           neighbors_shown=False, stub_requests=len(STATE["requests"]))


# ================= 路径 4：无 API Key =================
def path4_no_api_key() -> None:
    set_key(False)
    STATE["mode"] = "ok"
    before = len(STATE["requests"])
    q = "RAG 评估三元组包含哪三个维度？"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, q)
    text = page_text(at)

    hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)

    check("P4a 无 Key 状态：只显示检索 + 明确提示，不同时显示「已配置 key」",
          any("无 API Key：仅展示检索结果" in v for v in alert_values(at, "info"))
          and "服务端已检测到" not in text and "服务端未检测到" in text)
    check("P4b 无 Key 不调用模型（桩请求数为 0 增量），也不显示任何回答文本",
          len(STATE["requests"]) == before
          and EMBEDDED["三元组"] not in text and EMBEDDED["接地线"] not in text,
          f"桩请求数保持 {before}")
    check("P4c 检索结果照常保留（检索不被 Key 状态阻断）",
          len(at.expander) == 1 + len(hits) and len(hits) > 0,
          f"折叠块 {len(at.expander)}（期望 {1 + len(hits)}）")
    check("P4d 无 Key 时不展示「生成上下文/同节补充」，并说明原因",
          "**同节补充（" not in text and "因此不展示只在生成阶段才会用到的同节补充片段" in text)
    check("P4e 无 White Screen / 无异常", not at.exception and not at.error)

    record("路径4 无 API Key", question=q, hits=[d["id"] for _, d in hits],
           stub_requests=len(STATE["requests"]))
    set_key(True)


# ================= 路径 5：零结果 =================
def path5_zero_hits() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    before = len(STATE["requests"])
    q = "红烧肉怎么做？"
    at = run_page(rc.POWER_DEMO, "检索并生成", 3, q)
    text = page_text(at)

    check("P5a 零结果时显示「未检索到相关资料：不生成回答」",
          any("未检索到相关资料：不生成回答" in v for v in alert_values(at, "warning")))
    check("P5b 零结果短路：没有模型调用", len(STATE["requests"]) == before,
          f"桩请求数保持 {before}")
    check("P5c 零结果页面没有回答、没有引用对照内容、没有折叠的证据片段",
          "没有生成回答：没有检索命中" in text
          and "没有生成回答，因此没有引用可对照" in text
          and len(at.expander) == 1 and "没有可展示的片段" in text,
          f"折叠块 {len(at.expander)}（只有侧栏「更多信息」）")
    check("P5d 零结果不显示互相矛盾的提示卡（不同时出现「无 Key」或「生成失败」）",
          not any("无 API Key" in v for v in alert_values(at, "info")) and not at.error)

    record("路径5 零结果", question=q, hits=[], stub_requests=len(STATE["requests"]))


# ================= 路径 6：本地桩返回失败 =================
def path6_stub_failure() -> None:
    set_key(True)
    STATE["mode"] = "fail"
    before = len(STATE["requests"])
    q = "RAG 评估三元组包含哪三个维度？"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, q)
    text = page_text(at)

    hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    neighbors = rc.expand_and_assemble(q, top_k=3, corpus_id=rc.RAG_LEARNING)["neighbors"]

    check("P6a 生成失败时给出清楚错误提示并保留检索结果",
          any("模型调用失败：已保留检索到的资料" in v for v in alert_values(at, "error"))
          and all(d["id"] in text for d, _ in [(d, s) for s, d in hits])
          and len(at.expander) == 1 + len(hits) + len(neighbors),
          f"折叠块 {len(at.expander)}（期望 {1 + len(hits) + len(neighbors)}）")
    check("P6b 失败来自真实调用尝试：桩收到 1 次请求且返回 500",
          len(STATE["requests"]) == before + 1
          and "HTTPError" in "".join(alert_values(at, "error")),
          f"桩请求数 {before} → {len(STATE['requests'])}")
    check("P6c 失败时没有回答、也不伪造引用",
          "没有生成回答：模型调用失败" in text and "[air-evaluation#s2p2]" not in text)
    check("P6d 无白屏：脚本没有抛异常", not at.exception)
    check("P6e 失败路径仍解释补充片段不计入指标",
          "不计入 Hit@K / MRR" in text)

    record("路径6 本地桩返回失败", question=q, hits=[d["id"] for _, d in hits],
           neighbors=[e["id"] for e in neighbors], stub_requests=len(STATE["requests"]))
    STATE["mode"] = "ok"


# ================= 路径 7：生成成功但没有可用引用（27 号反例） =================
def path7_no_usable_refs() -> None:
    """27 号复审第 3 点：只要生成成功就点亮「对照引用」会暗示已完成引用对照，属表达不准。

    这里用的是 21 号**已记录**的真实反例：学习库问「红烧肉怎么做？」→ 有 3 段检索命中，
    模型回答「资料中未找到相关依据。」（零引用）。
    """
    set_key(True)
    STATE["mode"] = "ok"
    q = "红烧肉怎么做？"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, q)
    text = page_text(at)
    steps_html = _steps_html(at)

    hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    check("P7a 前置条件成立：该问题在**学习库**里确有检索命中（不是零结果）", len(hits) == 3,
          f"命中 {[d['id'] for _, d in hits]}")
    check("P7b 回答照常渲染（就是那句无引用回答）", EMBEDDED["红烧肉"] in text)
    check("P7c 引用对照明确写出「本次回答没有可用引用」，不出现旧的含糊提示",
          "本次回答没有可用引用" in text and "未从回答中解析到可用引用" not in text)
    check("P7d 第 4 步不点亮：步骤条显示「引用检查：无可用引用」且该格是 pending 样式",
          "引用检查：无可用引用" in steps_html and "v2-step pending" in steps_html,
          f"步骤条 HTML 长度 {len(steps_html)}；片段={steps_html[:260]!r}")
    check("P7e 第 3 步（生成回答）仍被点亮，说明没有把「没引用」误报成「没生成」",
          "v2-step active" in steps_html)
    check("P7f 命中证据仍在页面上（引用缺失不影响证据展示）",
          len(at.expander) == 1 + len(hits) + len(
              rc.expand_and_assemble(q, top_k=3, corpus_id=rc.RAG_LEARNING)["neighbors"]),
          f"折叠块 {len(at.expander)}")

    record("路径7 生成成功但无可用引用", question=q, hits=[d["id"] for _, d in hits], refs=[])

    # 反向对照：有引用的路径，第 4 步必须是点亮且不带 pending（证明 P7d 不是恒真）
    at2 = run_page(rc.RAG_LEARNING, "检索并生成", 3, "RAG 评估三元组包含哪三个维度？")
    html2 = _steps_html(at2)
    check("P7g 反向对照：有可用引用时第 4 步点亮且不出现 pending（P7d 不是恒真断言）",
          "v2-step active" in html2 and "pending" not in html2
          and "引用检查：无可用引用" not in html2)


# ================= 路径 8：措辞核对（27 号第 2 点） =================
def path8_wording() -> None:
    """27 号复审第 2 点：不得把 S4 的 NOT VALIDATED 写成「未通过」。"""
    set_key(True)
    STATE["mode"] = "ok"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, "RAG 评估三元组包含哪三个维度？")
    text = page_text(at)

    check("P8a 页面把 S4 写成「尚未验证（NOT VALIDATED）」", "NOT VALIDATED" in text
          and "尚未验证" in text)
    check("P8b 页面不再出现「仍未通过 / 未通过」这种把未验证说成失败的表述",
          "未通过" not in text)
    check("P8c 文案明确区分两者（含「不是已执行且失败」）", "不是已执行且失败" in text)

    src = (REPO / "app_v2.py").read_text(encoding="utf-8")
    check("P8d 源码级核对：app_v2.py 里不存在「未通过」字样",
          "未通过" not in src,
          "防的是这次只改了渲染分支、源码里还留着旧说法")


# ================= 跨路径：不点按钮不生成 / 设置变更提示 =================
def cross_checks() -> None:
    set_key(True)
    STATE["mode"] = "ok"
    before = len(STATE["requests"])
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, "红烧肉怎么做才好吃？", click=False)
    check("X1 只加载页面（不提问）不产生任何模型调用，也不显示回答",
          len(STATE["requests"]) == before and not at.error
          and "输入问题后点击「开始检索」" in page_text(at),
          f"桩请求数保持 {before}")

    # 先提问，再改设置但不重新提问：结果保留 + 明确提示"设置已变更"
    at = run_page(rc.RAG_LEARNING, "检索并生成", 3, "RAG 评估三元组包含哪三个维度？")
    at.selectbox[0].select(rc.POWER_DEMO)
    at.run()
    text = page_text(at)
    check("X2 切换知识库后旧结果仍可见，并明确提示当前设置已变更、需重新提问",
          "右侧设置已变更" in text and "请重新点击「开始检索」" in text
          and "air-evaluation#s2p1" in text,
          "旧结果未丢失 + 新旧设置不一致有提示")
    check("X3 结果区标注「本次结果使用的设置」，避免把旧结果误当成新设置的结果",
          "本次结果使用的设置：知识库 RAG 技术学习库 · 生成模式 检索并生成 · Top-K 3" in text)

    check("X4 全程只访问本机回环地址（真实模型请求 0 次）",
          STUB_URL in os.environ["DEEPSEEK_BASE_URL"]
          and all("127.0.0.1" in os.environ["DEEPSEEK_BASE_URL"] for _ in [0]),
          f"DEEPSEEK_BASE_URL={os.environ['DEEPSEEK_BASE_URL']}")

    counts = [len(s) for s in CARD_SNAPSHOTS]
    check("X5 状态卡互斥：本次所有页面快照里，同时出现的状态卡最多 1 条（且至少有一条快照真的出现了状态卡）",
          bool(counts) and max(counts) <= 1 and any(c == 1 for c in counts),
          f"{len(counts)} 次快照，最多同时出现 {max(counts)} 条；"
          f"出现过的状态卡：{sorted({k for s in CARD_SNAPSHOTS for k in s})}")
    EVIDENCE["card_snapshots"] = CARD_SNAPSHOTS


def adversarial_expander_count() -> None:
    """对抗：证明「折叠块精确计数」不是空转断言。

    把真实入口复制成一份**临时变体**，给证据区再包一层折叠（旧结构回归的形态），
    计数必须从 1+命中+补充 变成 +1；原文件全程不被修改。
    """
    src = (REPO / "app_v2.py").read_text(encoding="utf-8")
    marker = '    st.markdown(f"**检索命中（{len(hits)} 段）**")'
    check("X6a 对抗锚点存在（否则该对抗样本会退化成空操作）", marker in src)
    mutant = src.replace(
        marker,
        '    with st.expander("PROBE 外层折叠", expanded=False):\n'
        '        ' + marker.strip(), 1)
    check("X6b 对抗样本确实改动了结构（内容真的变了）", mutant != src)

    tmpdir = Path(tempfile.mkdtemp(prefix="v2_mutant_"))
    tmp = tmpdir / "app_v2_mutant.py"
    tmp.write_text(mutant, encoding="utf-8")
    try:
        q = "RAG 评估三元组包含哪三个维度？"
        st.cache_data.clear()
        at = AppTest.from_file(str(tmp), default_timeout=180).run()
        at.selectbox[0].select(rc.RAG_LEARNING)
        at.radio[0].set_value("检索并生成")
        at.text_input[0].set_value(q)
        at = at.button[0].click().run()

        hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)
        neighbors = rc.expand_and_assemble(q, top_k=3, corpus_id=rc.RAG_LEARNING)["neighbors"]
        baseline = 1 + len(hits) + len(neighbors)
        n = len(at.expander)
        check("X6c 多包一层折叠后计数确实变化（说明 P1h 的精确等式会真的 FAIL，不是空转）",
              n == baseline + 1,
              f"变体 {n} 个折叠块 vs 基线 {baseline} → 期望基线+1")
    finally:
        tmp.unlink(missing_ok=True)
        tmpdir.rmdir()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers-dir", default="", help="21 号证据目录（读 01/07 的回答原文）")
    ap.add_argument("--out", default="", help="证据输出目录，默认系统临时目录")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="ui_v2_evidence_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    start_stub()
    print("=" * 78)
    print("第二版界面（app_v2.py）离线验收：六条约定路径 + 27 号两个反例 —— 本地桩，零真实模型请求")
    print(f"桩地址：{STUB_URL}（仅回环地址）")
    print("=" * 78)

    if args.answers_dir:
        real = load_answers(Path(args.answers_dir))
        for kw, ans in real.items():
            check(f"桩回答来源与 21 号证据逐字节一致（{kw}）", ans == EMBEDDED[kw],
                  f"{len(ans)} 字符")
        STATE["answers"] = real
        src = Path(args.answers_dir).name
    else:
        src = "内嵌副本（未提供 --answers-dir）"
        mark_not_validated("桩回答与 21 号证据的一致性",
                           "未提供 --answers-dir，本次用的是文件内嵌副本，未与证据文件比对。")
    EVIDENCE["answers_source"] = src
    print(f"桩回答来源：{src}\n")

    try:
        v_design_fixes()
        path1_learning_stub_ok()
        path2_power_legacy_answer()
        path3_retrieve_only()
        path4_no_api_key()
        path5_zero_hits()
        path6_stub_failure()
        path7_no_usable_refs()
        path8_wording()
        cross_checks()
        adversarial_expander_count()
    except AssertionError as e:
        EVIDENCE["result"] = "FAIL"
        EVIDENCE["failures"] = [str(e)]
        (out_dir / "verify_ui_v2_result.json").write_text(
            json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[FAIL] {e}")
        return 1

    EVIDENCE["result"] = "PASS"
    EVIDENCE["stub_total_requests"] = len(STATE["requests"])
    EVIDENCE["stub_questions"] = STATE["requests"]
    (out_dir / "verify_ui_v2_result.json").write_text(
        json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "-" * 78)
    print(f"PASS {len(PASSED)} 项；NOT VALIDATED {len(NOT_VALIDATED)} 项（不计入通过）。")
    for name in NOT_VALIDATED:
        print(f"  - NOT VALIDATED：{name}")
    print(f"桩共收到 {len(STATE['requests'])} 次请求，全部指向本机 {STUB_URL}；"
          "未访问任何真实模型接口。")
    print(f"证据写入：{out_dir}")
    print("说明：本地桩只证明页面渲染与状态分支正确，不构成回答质量证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
