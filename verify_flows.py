# -*- coding: utf-8 -*-
"""验收脚本：覆盖本轮验收场景，并对两个被复审判定为"假 PASS"的检查补对抗回归。

运行：python verify_flows.py

状态约定（重要）：
  - **PASS**：本次运行真的验证了该行为；
  - **NOT VALIDATED**：本轮**没有**可离线验证的证据，不冒充通过。
    目前只有一项：验收场景 4「无依据问题不会被伪造成有资料支持」——
    BM25 没有相关性阈值，3 道拒答题全部返回了 Top-K；产品层拒答依赖生成层 prompt，
    而生成层本轮未验证。因此这项只能记为 NOT VALIDATED。

边界声明：
  - 全程离线，不调用任何真实模型。唯一涉及网络的地方是「API 失败路径」，
    它故意指向本机不可达端口（127.0.0.1:9）并配假 key，用来做**负向验证**；
    正向对照会让假端点真的收到一次调用并失败，以此证明负向结论不是空转；
  - CLI 用 subprocess 跑 03_qa.py，界面用 streamlit.testing 跑 app.py，
    都是真实入口，不是"读了 rag_core 的返回再推断"。

对抗回归（复审 F3 要求）：
  ① 报告一致性：任改 hit_at_1/hit_at_3/hit_at_5/mrr/failures/detail 之一，
     规范化比较必须能识别出差异（旧实现比较位置在 generated_at 就断开了，改数字也 PASS）。
  ② 隐私扫描：植入「盘符 + 用户目录/工作目录」形态的样本必须被识别
     （样本在运行期拼接，本文件里不出现字面路径，否则扫描器会命中自己），
     且 output/ 下的机器可读报告必须在扫描范围内（旧实现跳过 .json 与 output/）。
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

# 假凭据在运行期拼接，避免本文件自身被 S10 的密钥扫描命中
FAKE_KEY = "sk-" + "verify" + "-" + "fake"
FAKE_BASE_URL = "http://127.0.0.1:9"          # 本机必然不可达的端口
REPO = Path(__file__).resolve().parent

os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
os.environ["DEEPSEEK_BASE_URL"] = FAKE_BASE_URL
os.environ["DEEPSEEK_MODEL"] = "no-such-model"

import rag_core as rc                            # noqa: E402

PASSED: list = []
NOT_VALIDATED: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"{name} 未通过：{detail}")


def mark_not_validated(name: str, detail: str = "") -> None:
    NOT_VALIDATED.append(name)
    print(f"[NOT VALIDATED] {name}" + (f" — {detail}" if detail else ""))


def load_report(corpus_id: str) -> dict:
    return json.loads(rc.get_corpus(corpus_id)["report"].read_text(encoding="utf-8"))


# ================= S0 证据模式防回归 =================
def s0_evidence_mode_guard() -> None:
    """section_exact 不得把仅存在的子节当成精确父节；prefix 则应允许子节。"""
    spec = importlib.util.spec_from_file_location("eval_guard_probe", REPO / "04_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    docs = [{"id": "probe#1", "source_id": "probe", "section": "Parent > Child"}]
    exact = [{
        "question_id": "PROBE-EXACT",
        "expected_evidence": [{
            "mode": "section_exact", "source_id": "probe", "section": "Parent",
        }],
    }]
    prefix = [{
        "question_id": "PROBE-PREFIX",
        "expected_evidence": [{
            "mode": "section_prefix", "source_id": "probe", "section": "Parent",
        }],
    }]

    exact_rejected = False
    try:
        module._validate_evidence(exact, docs)
    except ValueError:
        exact_rejected = True

    prefix_accepted = True
    try:
        module._validate_evidence(prefix, docs)
    except ValueError:
        prefix_accepted = False

    check("S0 证据模式防回归：exact 拒绝仅有子节，prefix 接受子节",
          exact_rejected and prefix_accepted)


# ================= S1 双库隔离 =================
def s1_isolation() -> None:
    power_ids = {d["id"] for d in rc.load_documents(rc.POWER_DEMO)}
    rag_ids = {d["id"] for d in rc.load_documents(rc.RAG_LEARNING)}
    check("S1a 两库 chunk id 命名空间不重叠", not (power_ids & rag_ids),
          f"电力 {len(power_ids)} 个 / 学习库 {len(rag_ids)} 个")

    q = "安全距离与 BM25 打分"
    a = rc.retrieve(q, top_k=5, corpus_id=rc.POWER_DEMO)
    b = rc.retrieve(q, top_k=5, corpus_id=rc.RAG_LEARNING)
    check("S1b 检索结果只来自被选中的库",
          all(d["corpus_id"] == rc.POWER_DEMO for _, d in a)
          and all(d["corpus_id"] == rc.RAG_LEARNING for _, d in b))
    check("S1c 同一个问题在两个库里返回不同结果（说明真的换了索引）",
          {d["id"] for _, d in a} != {d["id"] for _, d in b})
    check("S1d 学习库结果不含示例语料 chunk",
          all(d["source_id"].startswith("air-") for _, d in b))


# ================= S2 基础问题命中正确章节 =================
def s2_basic_hit() -> None:
    hits = rc.retrieve("RAG 评估三元组包含哪三个维度？", top_k=3,
                       corpus_id=rc.RAG_LEARNING)
    ok = bool(hits) and "RAG评估三元组" in hits[0][1]["section"]
    check("S2 RAG 基础问题命中正确章节", ok,
          f"Top1 = {hits[0][1]['id']} · {hits[0][1]['section']}" if hits else "无结果")


# ================= S3 同义改写暴露能力边界 =================
def s3_paraphrase_boundary() -> None:
    detail = load_report(rc.RAG_LEARNING)["detail"]
    para = [d for d in detail if d["type"] == "同义改写" and not d["should_refuse"]]
    defi = [d for d in detail if d["type"] == "定义" and not d["should_refuse"]]
    ph = sum(1 for d in para if d["hit_at_1"]) / len(para)
    dh = sum(1 for d in defi if d["hit_at_1"]) / len(defi)
    check(f"S3 同义改写的 Hit@1（{ph:.0%}）低于定义题（{dh:.0%}），边界被真实暴露",
          ph < dh, "同义改写题首命中名次："
          + ", ".join(f"{d['question_id']}={d['first_evidence_rank']}" for d in para))


# ================= S4 无依据不被伪造成有资料支持 =================
def s4_no_fabrication() -> None:
    r = load_report(rc.RAG_LEARNING)
    refuse = r["refuse_analysis"]

    check("S4a 拒答题在报告中单列，不混入计分指标",
          r["refuse_question_count"] == len(refuse) == 3
          and r["scored_question_count"] == 27
          and all(x["status"] == "NOT VALIDATED" for x in refuse))

    hits = rc.retrieve("红烧肉怎么做才好吃？", top_k=3, corpus_id=rc.RAG_LEARNING)
    prompt = rc.build_prompt("红烧肉怎么做才好吃？", hits, rc.RAG_LEARNING)
    # 2026-09-19（19 号方案第 1 项窄改）：prompt 现在含两条约束——
    # ①「资料里已有可用口径就必须按资料列出并注明范围」（治"有据可答却拒答"）
    # ②「资料确实没有相关信息才拒答」的拒答约束（不能因为改了①就丢掉）
    # 两条一起断言：任一条被静默删掉都会 FAIL。
    has_refuse = "资料中未找到相关依据" in prompt
    has_scope = ("已经出现了可用的口径或框架" in prompt
                 and "不要把它硬凑成严格的三元组" in prompt)
    check("S4b 生成层 prompt 同时含「按资料列出可用口径」与「资料中未找到相关依据」两条约束（结构性检查）",
          has_refuse and has_scope,
          f"拒答约束={has_refuse}；分口径约束={has_scope}（任一缺失即 FAIL）")

    returned = [x["question_id"] for x in refuse if x["returned_result"]]
    check("S4c 已如实记录「BM25 无阈值 → 拒答题仍返回 Top-K」", len(returned) == len(refuse),
          f"{len(returned)}/{len(refuse)} 道拒答题返回了结果")

    # 关键：以上三条都不构成"系统能拒答"的证据
    mark_not_validated(
        "S4 验收场景 4：无依据问题不会被伪造成有资料支持",
        "检索层无相关性阈值、3/3 拒答题返回 Top-K；产品层拒答依赖生成层 prompt，"
        "而生成层本轮未验证（零真实模型请求）。需实现可离线验证的拒答决策，"
        "或由用户明确授权后用当前 prompt 做 3–5 例受控复验。"
    )


# ================= S5 CLI / 界面零结果不调模型 =================
def _run_cli(question: str, corpus_id: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "03_qa.py", question, "--corpus", corpus_id],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO), timeout=180,
    )


def s5_cli_zero_result() -> None:
    r = _run_cli("红烧肉怎么做？", rc.POWER_DEMO)
    check("S5a CLI 零结果：正常退出且未进入生成", r.returncode == 0
          and "资料中未找到相关依据" in r.stdout and "生成中" not in r.stdout,
          f"exit={r.returncode}")

    r2 = _run_cli("装设接地线的顺序是什么", rc.POWER_DEMO)
    check("S5b 正向对照：有结果时假端点确实被调用并失败",
          r2.returncode != 0 or "Traceback" in r2.stderr,
          f"exit={r2.returncode}（该步证明 S5a 的结论不是空转）")


def s5_app_zero_result() -> None:
    from streamlit.testing.v1 import AppTest

    def ask(corpus_id: str, question: str) -> "AppTest":
        at = AppTest.from_file(str(REPO / "app.py"), default_timeout=180).run()
        at.selectbox[0].select(corpus_id)
        at.radio[0].set_value("检索并生成")
        at.text_input[0].set_value(question)
        return at.button[0].click().run()

    at = ask(rc.POWER_DEMO, "红烧肉怎么做？")
    infos = [i.value for i in at.info]
    check("S5c 界面零结果：提示未调用模型且没有生成错误",
          not at.error and any("未调用模型" in v for v in infos), f"info={infos}")

    at2 = ask(rc.POWER_DEMO, "装设接地线的顺序是什么")
    check("S5d 界面正向对照：生成失败但检索结果保留",
          bool(at2.error) and len(at2.expander) > 0,
          f"error={[e.value[:40] for e in at2.error]} expanders={len(at2.expander)}")

    # 界面上「检索命中」与「同节补充」必须分开渲染：补充区在命中区之后单独成块
    # 2026-09-19（17 号界面窄修后）：外层 st.expander 已去掉，改为「标题 + 逐条折叠区」。
    # 因此这里从 >= hits+1 收紧为**精确等式** hits+neighbors：原先的 +1 就是给那个外层折叠区的，
    # 若有人再包一层（旧结构回归），计数变多即 FAIL。
    # 注：「补充片段之间无嵌套折叠」属 DOM 层事实，AppTest 拿不到嵌套关系，由浏览器检查覆盖。
    hits = rc.retrieve("装设接地线的顺序是什么", top_k=3, corpus_id=rc.POWER_DEMO)
    neighbors = rc.expand_and_assemble("装设接地线的顺序是什么", top_k=3,
                                       corpus_id=rc.POWER_DEMO)["neighbors"]
    title = f"补充的 {len(neighbors)} 段相邻上下文"
    n_expanders = len(at2.expander)
    check("S5e 界面把『检索命中』与『同节补充』分开渲染（精确计数 = 命中 + 补充，无多余外层折叠）",
          len(neighbors) > 0
          and n_expanders == len(hits) + len(neighbors)
          and any(title in m.value for m in at2.markdown),
          f"命中 {len(hits)} 段 + 补充 {len(neighbors)} 段 → 页面 {n_expanders} 个折叠块"
          f"（期望 {len(hits) + len(neighbors)}）；补充区标题「{title}」存在="
          f"{any(title in m.value for m in at2.markdown)}")


# ================= S6 语料变更 → 缓存失效（含对抗） =================
def s6_cache_invalidation() -> None:
    cid = rc.RAG_LEARNING
    target = rc.RAG_LEARNING_DIR / "documents" / "air-eval-tools.txt"
    manifest = json.loads(rc.get_corpus(cid)["manifest"].read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"] if d["source_id"] == "air-eval-tools")

    original = target.read_bytes()
    st0 = target.stat()
    fp0 = rc.corpus_fingerprint(cid)
    check("S6a 指纹可重复（同一语料两次结果一致）", rc.corpus_fingerprint(cid) == fp0)

    marker = "ZZZ_CACHE_PROBE_9f3a"
    try:
        # (1) 追加内容：最常见的"语料被改"
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n## 缓存探针\n\n{marker} 是本次验收写入的临时内容。\n")
        fp1 = rc.corpus_fingerprint(cid)
        check("S6b 追加内容后指纹改变（缓存随之失效）", fp1 != fp0,
              f"{fp0[:12]} → {fp1[:12]}")
        hits = rc.retrieve(marker, top_k=1, corpus_id=cid)
        check("S6c 变更后的内容可被检索到（索引确实重建）",
              bool(hits) and marker in hits[0][1]["text"])

        # (2) 对抗：改成**同字节数**内容并把 mtime 改回去
        #     —— 这正是复审 F5 的反例，旧实现（mtime+size 指纹）会保持不变
        target.write_bytes(original)
        text = original.decode("utf-8")
        mutated = None
        for i, ch in enumerate(text):
            if len(ch.encode("utf-8")) == 3 and ch != "測":     # 找一个 3 字节汉字
                mutated = text[:i] + "測" + text[i + 1:]        # 长度不变，内容必变
                break
        assert mutated is not None and mutated != text, "对抗样本必须真的改动内容"
        assert len(mutated.encode("utf-8")) == len(original), "对抗样本必须保持字节数不变"
        target.write_bytes(mutated.encode("utf-8"))
        os.utime(target, (st0.st_atime, st0.st_mtime))     # 把 mtime 改回原值
        st_mut = target.stat()
        same_size = st_mut.st_size == st0.st_size
        same_mtime = int(st_mut.st_mtime) == int(st0.st_mtime)
        content_changed = hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]
        fp2 = rc.corpus_fingerprint(cid)
        check("S6d 对抗①：同字节数改写 + 恢复 mtime，指纹仍然变化（内容哈希生效）",
              same_size and same_mtime and content_changed and fp2 != fp0,
              f"size 相同={same_size} mtime 相同={same_mtime} 内容已变={content_changed} "
              f"指纹 {fp0[:12]} → {fp2[:12]}")
    finally:
        target.write_bytes(original)

    restored = hashlib.sha256(target.read_bytes()).hexdigest()
    check("S6e 已按 sha256 逐字节还原原始语料",
          restored == entry["sha256"] and rc.corpus_fingerprint(cid) == fp0,
          f"sha256 {restored[:12]}… 与来源清单一致")


# ================= S7 来源可追溯 + 两级校验（含对抗） =================
def s7_traceability() -> None:
    docs = rc.load_documents(rc.RAG_LEARNING)
    manifest = json.loads(rc.get_corpus(rc.RAG_LEARNING)["manifest"].read_text(encoding="utf-8"))
    commit = manifest["upstream"]["commit"]

    check("S7a 每个 chunk 都有来源标题、章节与原文链接",
          all(d["source_title"] and d["section"] and d["source_url"] for d in docs),
          f"{len(docs)} 个 chunk")
    check("S7b 来源链接钉扎到具体 commit（不会随 main 漂移）",
          all(f"/blob/{commit}/" in d["source_url"] for d in docs))

    mismatched = []
    for e in manifest["documents"]:
        fp = rc.RAG_LEARNING_DIR / "documents" / Path(e["file"]).name
        if hashlib.sha256(fp.read_bytes()).hexdigest() != e["sha256"]:
            mismatched.append(e["source_id"])
    check("S7c 语料与来源清单逐字节一致（10/10 sha256 通过）",
          not mismatched, f"不一致：{mismatched}" if mismatched else "10/10")

    check("S7d 界面可读到版本与许可",
          commit[:7] in rc.corpus_version(rc.RAG_LEARNING)
          and "CC BY-NC-SA 4.0" in rc.corpus_version(rc.RAG_LEARNING))


def _run_import_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "tools/import_rag_corpus.py", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), cwd=str(REPO), timeout=300,
    )


def s7b_two_level_check() -> None:
    r_self = _run_import_tool("--self-check")
    check("S7e 自校验（交付文档 vs 交付 manifest）通过", r_self.returncode == 0,
          f"exit={r_self.returncode}")

    r_up = _run_import_tool("--upstream-check")
    check("S7f 上游钉扎重建校验通过（在 commit 上重新清洗并逐字节比对）",
          r_up.returncode == 0 and "与上游重建逐字节一致" in r_up.stdout,
          f"exit={r_up.returncode}")

    # 对抗②：上游不可用时必须**失败**，不能静默 PASS（复审 F6）
    r_missing = _run_import_tool("--upstream-check", "--repo", str(REPO / "不存在的克隆路径"))
    check("S7g 对抗②：上游克隆不存在时上游校验必须失败",
          r_missing.returncode != 0 and "BLOCKED" in r_missing.stdout,
          f"exit={r_missing.returncode}")

    r_alias = _run_import_tool("--check")
    check("S7h --check 作为别名会明确声明它不校验上游",
          r_alias.returncode == 0 and "不校验上游" in r_alias.stdout)


# ================= S8 评测可重生成 + 报告一致性（含对抗） =================
NON_DECISIVE_KEYS = {"generated_at"}


def report_body(rep: dict) -> dict:
    """规范化报告正文：只排除明确非决定性的字段（时间戳），其余全部参与比较。"""
    return {k: v for k, v in rep.items() if k not in NON_DECISIVE_KEYS}


def s8_eval_regenerable() -> None:
    report_path = rc.get_corpus(rc.RAG_LEARNING)["report"]
    before = json.loads(report_path.read_text(encoding="utf-8"))
    r = subprocess.run([sys.executable, "04_eval.py", "--corpus", rc.RAG_LEARNING],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       cwd=str(REPO), timeout=600)
    after = json.loads(report_path.read_text(encoding="utf-8"))
    check("S8a 一条命令可重生成评测报告（指标可复现）",
          r.returncode == 0 and "Hit@1" in r.stdout
          and after["scored_question_count"] == 27 and after["chunk_count"] > 0,
          f"Hit@1={after['hit_at_1']:.2%} Hit@3={after['hit_at_3']:.2%} "
          f"Hit@5={after['hit_at_5']:.2%} MRR={after['mrr']:.4f}")
    check("S8b 重跑未改变报告结论（除 generated_at 外，规范化全对象一致）",
          report_body(before) == report_body(after))


def s8_adversarial() -> None:
    """对抗①：任一决定字段被改动，一致性检查必须能识别（复审 F3）。"""
    base = load_report(rc.RAG_LEARNING)
    undetected = []
    probes = {
        "hit_at_1": 0.0,
        "hit_at_3": 0.0,
        "hit_at_5": 0.0,
        "mrr": 0.0,
        "failures": [],
        "refuse_analysis": [],
        "chunk_count": 1,
        "detail": [],
    }
    for key, value in probes.items():
        tampered = deepcopy(base)
        tampered[key] = value
        if report_body(tampered) == report_body(base):
            undetected.append(key)
    check("S8c 对抗①：篡改 hit@K / MRR / failures / detail 等任一字段，比较必须识别",
          not undetected, f"未被识别的字段：{undetected}" if undetected else
          f"已逐一验证 {len(probes)} 个字段")

    # 只改时间戳时，必须仍然视为"结论一致"（否则检查会天天误报）
    only_ts = deepcopy(base)
    only_ts["generated_at"] = "1970-01-01T00:00:00+00:00"
    check("S8d 只改 generated_at 时仍判定为结论一致（避免误报）",
          report_body(only_ts) == report_body(base))


# ================= S11 生成上下文补全（P1） =================
def s11_context_expansion() -> None:
    q = "RAG 评估三元组包含哪三个维度？"
    raw_hits = rc.retrieve(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    a = rc.expand_and_assemble(q, top_k=3, corpus_id=rc.RAG_LEARNING)
    ids = [e["id"] for e in a["contexts"]]

    missing = [x for x in ("#s2p2", "#s2p3", "#s2p4") if not any(i.endswith(x) for i in ids)]
    check("S11a RAG Triad 的生成上下文覆盖同节枚举片段 s2p2–s2p4", not missing,
          f"上下文共 {len(ids)} 段" + ("；缺 " + str(missing) if missing else ""))

    check("S11b 真实命中集合、顺序与分数未被改动（Top-K 口径不变）",
          [e["id"] for e in a["hits"]] == [d["id"] for _, d in raw_hits]
          and [e["score"] for e in a["hits"]] == [round(s, 4) for s, _ in raw_hits],
          " · ".join(e["id"] for e in a["hits"]))

    hit_groups = {(e["source_id"], e["section"]) for e in a["hits"]}
    crossing = [e["id"] for e in a["neighbors"]
                if (e["source_id"], e["section"]) not in hit_groups]
    check("S11c 补充片段只限同 source_id + 同章节（不跨节、不跨库）", not crossing,
          f"越界：{crossing}" if crossing else f"{a['neighbor_count']} 段全部同节")

    check("S11d 上下文无重复片段，且总字符受预算约束",
          len(ids) == len(set(ids)) and a["total_chars"] <= max(a["max_chars"], a["hit_chars"]),
          f"总 {a['total_chars']} 字符 = 命中 {a['hit_chars']} + 补充 "
          f"{a['total_chars'] - a['hit_chars']}（预算 {a['max_chars']}）")

    check("S11e 补充片段不冒充命中：命中数仍等于 top_k 且带 origin 标记",
          a["hit_count"] == len(raw_hits)
          and all(e["origin"] == "neighbor" for e in a["neighbors"])
          and all(e["origin"] == "hit" for e in a["hits"]))

    p = rc.expand_and_assemble("装设接地线的顺序是什么", top_k=3, corpus_id=rc.POWER_DEMO)
    p_ids = [e["id"] for e in p["contexts"]]
    check("S11f 电力库的补全不越库（不出现学习库片段）",
          bool(p_ids) and all(not i.startswith("air-") for i in p_ids),
          f"{len(p_ids)} 段全部来自电力示例语料")

    z = rc.expand_and_assemble("红烧肉怎么做？", top_k=3, corpus_id=rc.POWER_DEMO)
    check("S11g 零命中时不做任何补全（零结果短路语义不被破坏）",
          not z["hits"] and not z["neighbors"] and z["total_chars"] == 0)

    eval_src = (REPO / "04_eval.py").read_text(encoding="utf-8")
    check("S11h 评测脚本不使用上下文补全（Hit@K / MRR 与补全解耦）",
          "assemble_context" not in eval_src and "expand_and_assemble" not in eval_src)


# ================= S12 引用去重（P2） =================
def s12_ref_dedup() -> None:
    known = {"a#1", "b#2", "c#3"}
    answer = "结论见 [a#1] 与 [b#2]；补充说明 [a#1]，再引 [c#3]。"
    refs = rc.extract_refs(answer, known)
    check("S12a 引用按首次出现顺序去重", refs == ["a#1", "b#2", "c#3"], f"{refs}")

    raw = rc.count_ref_occurrences(answer)
    check("S12b 展示条数小于正文引用出现次数（确实去重）",
          len(refs) < raw, f"{len(refs)} 条 < 正文出现 {raw} 次")

    dirty = "见 [不存在的#9]、[链接文字](http://example.com) 与 [a#1]"
    # 复审 12 号给出的反例：链接文字**恰好是真实 id** 时，旧规则会把它当成正文引用
    linked = "参考 [a#1](https://example.com/doc) 的说明，正文没有资料引用。"
    # 22 号窄任务：真实样本里模型会写成 `[来源：<id>]`，这种写法应当被识别（而不是被丢掉）
    prefixed = "见 [来源：a#1] 与 [出处: b#2]，另有编造的 [不存在的#9]。"
    check("S12c 编造 id 与 markdown 链接文字不算引用，但「来源：id」要算（含链接文字恰好是真实 id）",
          rc.extract_refs(dirty, known) == ["a#1"] and rc.extract_refs(linked, known) == []
          and rc.extract_refs(prefixed, known) == ["a#1", "b#2"],
          f"编造/无效链接 → {rc.extract_refs(dirty, known)}；"
          f"真实 id 链接 → {rc.extract_refs(linked, known)}（期望 []）；"
          f"「来源：」前缀 → {rc.extract_refs(prefixed, known)}（期望 ['a#1', 'b#2']）")

    check("S12d 去重只影响展示：原回答文本不被修改",
          answer == "结论见 [a#1] 与 [b#2]；补充说明 [a#1]，再引 [c#3]。")

    app_src = (REPO / "app.py").read_text(encoding="utf-8")
    check("S12e 界面引用对照使用去重函数（结构确认）",
          "rc.extract_refs" in app_src and "count_ref_occurrences" in app_src)

    mixed = "参考 [a#1](https://example.com/doc) 与 [来源：b#2]，正文引用 [a#1]。"
    check("S12f 出现次数统计与引用提取共用同一条 token 规则（不互相矛盾）",
          rc.count_ref_occurrences(mixed) == 2
          and rc.extract_refs(mixed, known) == ["b#2", "a#1"],
          f"正文出现 {rc.count_ref_occurrences(mixed)} 次（期望 2，链接文字不计）、"
          f"去重 {rc.extract_refs(mixed, known)}（期望 ['b#2', 'a#1']）")

    # 22 号窄任务：用 21 号**已保存的真实回答原文**做离线回放（两段文本逐字来自
    # 21-证据-8例真实复测/07-接地线.json 与 08-安全距离.json 的 answer 字段）。
    ans07 = ("装设接地线时，应先接接地端，后接导体端；拆除接地线的顺序与此相反。"
             "[来源：示例语料-电力安全通用要点#P5]")
    ans08 = ("资料中给出的常见电压等级安全距离为：10千伏及以下为0.7米，35千伏为1.0米，"
             "110千伏为1.5米，220千伏为3.0米，500千伏为5.0米。资料还说明，人体与带电体之间"
             "必须保持足够的安全距离，电压等级越高，安全距离越大。[示例语料-电力安全通用要点#P6]")
    known_power = {"示例语料-电力安全通用要点#P5", "示例语料-电力安全通用要点#P6"}
    r07 = rc.extract_refs(ans07, known_power)
    r08 = rc.extract_refs(ans08, known_power)
    r07_other_ctx = rc.extract_refs(ans07, {"air-evaluation#s2p2"})
    check("S12g 21 号真实样本回放：「[来源：<id>]」与「[<id>]」都能解析成上下文里的完整 id（跨库/编造仍排除）",
          r07 == ["示例语料-电力安全通用要点#P5"]
          and r08 == ["示例语料-电力安全通用要点#P6"]
          and rc.count_ref_occurrences(ans07) == 1
          and r07_other_ctx == [],
          f"07（来源：前缀）→ {r07}；08（无前缀）→ {r08}；"
          f"07 计数={rc.count_ref_occurrences(ans07)}；换成本库外上下文 → "
          f"{r07_other_ctx}（期望 []）")


# ================= S9 API 失败仍保留检索结果 =================
def s9_api_failure() -> None:
    contexts = rc.retrieve("装设接地线的顺序是什么", top_k=3, corpus_id=rc.POWER_DEMO)
    assert contexts, "本场景需要非空检索结果"
    try:
        rc.generate_answer("装设接地线的顺序是什么", contexts, FAKE_KEY,
                           FAKE_BASE_URL, "no-such-model", rc.POWER_DEMO)
    except Exception as e:
        check("S9 API 失败时检索结果独立保留", bool(contexts),
              f"{type(e).__name__}，检索结果 {len(contexts)} 段仍在（界面正向对照见 S5d）")
    else:
        check("S9 API 失败时检索结果独立保留", False, "预期生成抛异常，实际未抛")


# ================= S10 无密钥 / 无个人路径（含对抗） =================
SCAN_DIRS_ALLOWED = {"__pycache__", ".git"}
SCAN_SUFFIXES = {".py", ".md", ".txt", ".json", ".ps1", ".cmd", ".yml", ".yaml", ".toml"}

SECRET_RES = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似真实 API key"),
    (re.compile(r"api[_-]?key\s*[:=]\s*[\"'][^\"']{8,}"), "硬编码 key 赋值"),
    # 个人绝对路径：盘符 + 用户目录/工作目录（复审 F4 的植入样本形态）
    (re.compile(r"[A-Za-z]:[\\/]{1,2}(Users|codebuddy|CodeBuddy|AppData|Desktop|Documents)"),
     "个人绝对路径"),
]
# 只做统计的一般盘符路径（教程里可能出现 Windows 路径示例，不直接判失败）
# 负向环视用于排除 URL 里的 "s://"，否则 https:// 会被误计成盘符路径
GENERIC_ABSPATH_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]{1,2}")


def iter_scannable_files() -> list:
    """扫描范围：**包含 .json 与 output/**（旧实现跳过二者，等于漏掉最该扫的报告）。"""
    out = []
    for fp in sorted(REPO.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SCAN_DIRS_ALLOWED for part in fp.relative_to(REPO).parts):
            continue
        out.append(fp)
    return out


def scan_text(text: str) -> list:
    findings = []
    for pat, label in SECRET_RES:
        for m in pat.finditer(text):
            findings.append(f"{label} → {m.group(0)[:44]}")
    return findings


def s10_no_secrets() -> None:
    files = iter_scannable_files()
    findings = []
    generic_hits = []
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = fp.relative_to(REPO).as_posix()
        findings += [f"{rel}: {f}" for f in scan_text(text)]
        if GENERIC_ABSPATH_RE.search(text):
            generic_hits.append(rel)

    check("S10a 代码与文档中无 API key、无个人绝对路径",
          not findings, f"扫描 {len(files)} 个文本文件（含 .json 与 output/）；"
          + ("问题：" + "; ".join(findings) if findings else "无命中"))

    out_scanned = any(str(Path("output")) in f.parts for f in [fp.relative_to(REPO) for fp in files])
    check("S10b 机器可读报告与评测集确实在扫描范围内（output/ 与 *.json 未被跳过）",
          out_scanned and any(fp.suffix == ".json" for fp in files),
          f"一般盘符路径出现 {len(generic_hits)} 个文件（教程示例，仅统计不判失败）")


def s10_adversarial() -> None:
    """对抗②：植入样本必须被识别（复审 F4）。"""
    planted = "见 " + "D:" + "\\codebuddy\\private.txt 以及 " + "C:" + "\\Users\\" + "someone"
    found = scan_text(planted)
    check("S10c 对抗②：植入个人绝对路径的样本必须被识别", len(found) >= 2,
          f"识别到 {len(found)} 处")

    planted_key = "key = " + '"' + ("sk-" + "a1b2c3d4e5f6g7h8i9j0") + '"'
    check("S10d 对抗②：植入超长 key 形态的样本必须被识别", bool(scan_text(planted_key)))

    # 证明 output/ 下的报告真的会被读：临时把报告改成含植入路径，扫描应命中，随后还原
    report_path = rc.get_corpus(rc.RAG_LEARNING)["report"]
    original = report_path.read_text(encoding="utf-8")
    try:
        report_path.write_text(original.replace(
            '"corpus_id"', '"corpus_id": "' + "D:" + "\\codebuddy\\private.txt" + '", "x"', 1),
            encoding="utf-8")
        hit = [f for f in scan_text(report_path.read_text(encoding="utf-8"))]
        check("S10e 对抗②：output/ 报告被植入私密路径时扫描会失败", bool(hit))
    finally:
        report_path.write_text(original, encoding="utf-8")
    check("S10f 还原后报告内容不变",
          json.loads(report_path.read_text(encoding="utf-8"))["corpus_id"] == "rag_learning")


def main() -> int:
    print("=" * 76)
    print("验收：真实语料 RAG 学习库 + 电力示例库（全程离线，不用真实模型）")
    print("=" * 76)
    try:
        s0_evidence_mode_guard()
        s1_isolation()
        s2_basic_hit()
        s3_paraphrase_boundary()
        s4_no_fabrication()
        s5_cli_zero_result()
        s5_app_zero_result()
        s6_cache_invalidation()
        s7_traceability()
        s7b_two_level_check()
        s8_eval_regenerable()
        s8_adversarial()
        s9_api_failure()
        s10_no_secrets()
        s10_adversarial()
        s11_context_expansion()
        s12_ref_dedup()
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        return 1

    print("\n" + "-" * 76)
    print(f"PASS {len(PASSED)} 项；NOT VALIDATED {len(NOT_VALIDATED)} 项（不计入通过）。")
    for name in NOT_VALIDATED:
        print(f"  - NOT VALIDATED：{name}")
    print("说明：本脚本的 PASS 只代表所声明的检查项在本次运行成立，"
          "不构成独立复审结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
