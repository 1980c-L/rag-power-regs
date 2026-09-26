# -*- coding: utf-8 -*-
"""生成侧最小评测执行器（05_gen_eval.py）——「找得准」之外，开始量「答得对不对」。

为什么要它：`04_eval.py` 只做**检索侧**评测（Hit@K / MRR，判据是标准证据有没有进前 K），
完全不看模型回答。本脚本补**生成侧**：把每道题的真实链路（检索 → 同节补全 → 生成 → 引用解析）
固化成可复核的样本记录，并把判据拆成两层：

  A 层（脚本判，无需人看）：
    A1 引用有效性：回答里出现的方括号 id 是否**全部来自本次上下文**（编造的 id 会被单独列出）；
    A2 引用存在性：有据题应至少 1 条有效引用；拒答题应为 0 条引用；
    A3 拒答表现：拒答题的回答是否明确说「资料中未找到相关依据」；
    A4 上下文与命中的事实记录（命中 id / 分数 / 同节补充数 / 上下文字符数），只记录不下判。

  B 层（人工判，三档）：答案是否**被资料支持**、**预期要点是否覆盖**——脚本只给出空列，
    因为语义支持性不能被关键词匹配冒充。

两种目标：
  --target stub  （默认）本机桩 tools/llm_stub_server.py，**零真实模型请求**，用于验证框架与判据；
  --target real  真实模型。**需要显式授权**：必须同时给 --confirm-real 与 --max-calls N（N ≤ 20，
                 硬上限），且服务端必须有 DEEPSEEK_API_KEY（只检查存在性，不回显）、base_url 不能是回环。

真实模式纪律（对齐本项目既有门禁口径）：
  - **0 自动重试**：一次题目一次调用，失败就如实记错，不偷偷重发；
  - 调用次数**先算后发**：题目数 > --max-calls 时直接拒绝启动；
  - 每次调用写台账（时间 / 模型 / base_url / 题目编号 / 耗时），**不写 Key**；
  - 跑完打印真实调用次数与上限的对照，供复核。

运行：
    python 05_gen_eval.py --target stub
    python 05_gen_eval.py --target real --confirm-real --max-calls 20
输出（默认 output/gen_eval/，可用 --out 改）：
    样本.jsonl       每条样本的完整记录（可程序复算）
    评审表.md        A 层结果已填好 + B 层两列留空等你勾三档
    汇总.json        A 层统计、调用台账、运行参数
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import config                                                   # noqa: E402
import rag_core as rc                                           # noqa: E402

QUESTIONS_PATH = REPO / "eval" / "gen_eval_questions.json"
STUB_PORT = 5281
STUB_BASE = f"http://127.0.0.1:{STUB_PORT}/v1"
# 真实调用的硬上限：超过它一律拒绝启动（要改就得改代码并过复审，不是改命令行）
REAL_CALLS_HARD_CAP = 20

REFUSAL_PHRASE = "资料中未找到相关依据"


def loopback(base_url: str) -> bool:
    import re
    return bool(re.search(r"://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", base_url or ""))


def load_questions() -> dict:
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


def resolve_api_key() -> str:
    """取服务端 Key（只做存在性检查与去空白；任何输出里都不出现其内容）。"""
    return (config.DEEPSEEK_API_KEY or "").strip().replace("\r", "").replace("\n", "")


def start_stub(outdir: Path) -> subprocess.Popen:
    log = outdir / "stub_requests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tools" / "llm_stub_server.py"),
         "--port", str(STUB_PORT), "--log", str(log)],
        cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{STUB_PORT}/__stub__/count", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:                                       # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError("本机桩未在预期时间内就绪")


def stub_count() -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{STUB_PORT}/__stub__/count", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("count", -1)
    except Exception:                                           # noqa: BLE001
        return -1


def kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:                                           # noqa: BLE001
        pass


def fabricated_refs(answer: str, context_ids: set) -> list:
    """回答里出现的方括号 token 中，**对不上本次上下文**的那些（编造出来的引用）。

    判据 v2（本轮修正）：只有**长得像 chunk id 的**方括号 token 才算"引用候选"——
    本项目所有 chunk id 都含 `#`（`{source}#s{节}p{段}` 或 `{文件}#P{段}`）。
    判据 v1 曾把模型写的数学示例 `[0.16, 0.29, -0.88, ...]` 误判成"编造引用"，
    这是判据自身的问题（真实跑真实暴露），不是模型编造；修正后重判**不需要任何新调用**。
    """
    out: list = []
    for m in rc._REF_TOKEN_RE.finditer(answer or ""):           # noqa: SLF001（口径与 rag_core 一致）
        raw = rc.normalize_ref_token(m.group(1))
        if "#" not in raw:                                      # 普通括号内容（数字、短语）不是引用候选
            continue
        if raw and raw not in context_ids and raw not in out:
            out.append(raw)
    return out


def citation_candidates(answer: str) -> list:
    """回答里的**引用候选**（含 `#` 的方括号 token，规范化后按首次出现去重）。"""
    out: list = []
    for m in rc._REF_TOKEN_RE.finditer(answer or ""):           # noqa: SLF001
        raw = rc.normalize_ref_token(m.group(1))
        if "#" in raw and raw not in out:
            out.append(raw)
    return out


# 近似要点覆盖要排除的泛词：它们出现与否说明不了"要点有没有被答到"
_COVERAGE_STOP = {"什么", "为什么", "怎么", "如何", "包含", "哪些", "以及", "进行", "可以",
                  "使用", "这种", "一个", "我们", "就是", "需要", "应该", "资料", "问题"}


def readability_stats(expected_points: str, answer: str) -> dict:
    """回答的**可读性读数**（长度 / 引用密度 / 近似要点覆盖）。

    定位：这是**给人和复审看的读数，不是判据**——"是否被资料支持"仍需人工三档判。
    为什么要它：用户实测反馈"内容全面但不精简"，那就把"啰嗦"量出来（字数、句子数、引用密度），
    并且给一个**近似的**要点覆盖比（预期要点分词后有多少实词真的出现在回答里），
    让人只对少数几行做人工判定，而不是硬读 15 份长回答。
    """
    if not answer:
        return {"answer_chars": 0, "answer_sentences": 0, "ref_per_1k_chars": None,
                "approx_point_coverage": None}
    chars = len(answer)
    sentences = len([x for x in re.split(r"[。！？\n]+", answer) if x.strip()])
    terms = [t for t in dict.fromkeys(rc.tokenize(expected_points or ""))
             if len(t) >= 2 and re.match(r"^[\u4e00-\u9fffA-Za-z0-9]+$", t) and t not in _COVERAGE_STOP]
    cov = None
    if terms:
        hit = [t for t in terms if t in answer]
        cov = {"terms": len(terms), "hit": len(hit),
               "ratio": round(len(hit) / len(terms), 2),
               "missing": [t for t in terms if t not in answer][:8]}
    return {"answer_chars": chars, "answer_sentences": sentences,
            "ref_per_1k_chars": None if not chars else round(len(citation_candidates(answer)) * 1000 / chars, 2),
            "approx_point_coverage": cov, "note": "读数列：仅作提示，不构成判据"}


def run_one(q: dict, *, api_key: str, base_url: str, model: str, corpus_id: str,
            top_k: int, outdir: Path, call_log: list) -> dict:
    t0 = time.perf_counter()
    assembled = rc.expand_and_assemble(q["question"], top_k=top_k, corpus_id=corpus_id)
    retrieve_ms = int((time.perf_counter() - t0) * 1000)

    context_ids = {e["id"] for e in assembled["contexts"]}
    answer = None
    error = ""
    generate_ms = None
    if api_key:
        t1 = time.perf_counter()
        try:
            answer = rc.generate_answer(q["question"], assembled["contexts"],
                                        api_key, base_url, model, corpus_id)
        except Exception as exc:                                # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:300]
        generate_ms = int((time.perf_counter() - t1) * 1000)
        call_log.append({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "eval_id": q["eval_id"], "model": model, "base_url": base_url,
            "elapsed_ms": generate_ms, "ok": answer is not None,
            "error": error,
        })

    refs = rc.extract_refs(answer or "", context_ids) if answer else []
    occ = rc.count_ref_occurrences(answer or "") if answer else 0
    cands = citation_candidates(answer or "") if answer else []
    bad = fabricated_refs(answer or "", context_ids) if answer else []

    a1 = (answer is not None) and not bad
    if q["should_refuse"]:
        # 判据 v2：拒答题**不以引用数量判定**——模型可能先正确拒答、再补邻近相关资料并带引用
        # （真实跑 G13 就是这个形态）；拒答题的主判据是 A3「拒答短语」。
        a2 = None
        a3 = bool(answer) and (REFUSAL_PHRASE in (answer or ""))
    else:
        a2 = len(refs) >= 1
        a3 = None

    return {
        "eval_id": q["eval_id"],
        "source_question_id": q.get("source_question_id"),
        "type": q["type"],
        "question": q["question"],
        "should_refuse": q["should_refuse"],
        "expected_points": q.get("expected_points", ""),
        "expected_evidence": q.get("expected_evidence", []),
        "hits": [{"id": h["id"], "score": h["score"], "origin": h["origin"],
                  "source_title": h.get("source_title", "")} for h in assembled["hits"]],
        "neighbors": [{"id": n["id"], "source_id": n.get("source_id", "")}
                      for n in assembled["neighbors"]],
        "context_chars": {"total": assembled["total_chars"], "hit": assembled["hit_chars"],
                          "max": assembled["max_chars"]},
        "answer": answer,
        "generate_error": error,
        "refs": refs,
        "ref_occurrences": occ,
        "citation_candidates": cands,
        "fabricated_refs": bad,
        "readability": readability_stats(q.get("expected_points", ""), answer or ""),
        "elapsed_ms": {"retrieve": retrieve_ms, "generate": generate_ms},
        "machine": {"A1_引用全部有效": bool(a1), "A2_引用数量符合题型": bool(a2),
                    "A3_拒答短语": a3, "note": "" if answer else "本次没有生成（无 Key 或调用失败）"},
    }


def render_report(meta: dict, samples: list) -> str:
    lines: list = []
    lines.append("# 生成侧评测 · 评审表\n")
    lines.append(f"> 目标：{meta['target']}（model={meta['model']}，base_url={meta['base_url']}）；"
                 f"知识库={meta['corpus_id']}，Top-K={meta['top_k']}；运行时间 {meta['ran_at']}\n")
    lines.append(f"> 真实模型调用次数：**{meta['real_calls']}**（上限 {meta['max_calls']}，0 重试）\n")
    lines.append("\n## 一、汇总（A 层已由脚本判定，B 层请你勾三档）\n\n")
    lines.append("| 编号 | 题型 | 是否该拒答 | A1 引用全部有效 | A2 引用数量 | A3 拒答短语 | B1 是否被资料支持 | B2 要点覆盖 |\n")
    lines.append("|---|---|---|---|---|---|---|---|\n")
    for s in samples:
        m = s["machine"]

        def tri(v) -> str:
            return "-" if v is None else ("是" if v else "**否**")

        lines.append(
            f"| {s['eval_id']} | {s['type']} | {'是' if s['should_refuse'] else '否'} | "
            f"{tri(m['A1_引用全部有效'])} | {tri(m['A2_引用数量符合题型'])} | "
            f"{tri(m['A3_拒答短语'])} | | |\n")
    lines.append("\n> B1 请填：`支持` / `不支持` / `拿不准`；B2 请填：`覆盖` / `部分` / `未覆盖`。\n")

    lines.append("\n## 二、逐条详情\n")
    for s in samples:
        lines.append(f"\n### {s['eval_id']}（{s['type']}，{'应拒答' if s['should_refuse'] else '有据可答'}）\n")
        if s.get("source_question_id"):
            lines.append(f"- 沿用题目：{s['source_question_id']}\n")
        lines.append(f"- **问题**：{s['question']}\n")
        lines.append(f"- **预期要点**：{s['expected_points']}\n")
        if s.get("expected_evidence"):
            ev = "；".join(e.get("section") or e.get("source_id", "") for e in s["expected_evidence"])
            lines.append(f"- **标准证据**：{ev}\n")
        hit_txt = "、".join(f"{h['id']}({h['score']})" for h in s["hits"]) or "（无命中）"
        lines.append(f"- **检索命中**：{hit_txt}\n")
        nb = "、".join(n["id"] for n in s["neighbors"]) or "（无）"
        lines.append(f"- **同节补充**：{nb}；上下文 {s['context_chars']['total']} 字符"
                     f"（命中 {s['context_chars']['hit']}，预算 {s['context_chars']['max']}）\n")
        if s["generate_error"]:
            lines.append(f"- **生成失败**：{s['generate_error']}\n")
        lines.append(f"- **模型回答**：\n\n> " + (s["answer"] or "（空）").replace("\n", "\n> ") + "\n")
        lines.append(f"- **引用（去重后）**：{('、'.join(s['refs']) or '（无）')}"
                     f"；正文出现 {s['ref_occurrences']} 次\n")
        if s["fabricated_refs"]:
            lines.append(f"- ⚠️ **对不上上下文的引用**：{'、'.join(s['fabricated_refs'])}\n")
        lines.append(f"- A 层判据：{json.dumps(s['machine'], ensure_ascii=False)}\n")
        rd = s.get("readability") or {}
        if rd:
            cov = rd.get("approx_point_coverage")
            cov_txt = ("无预期要点" if cov is None
                       else f"{cov['hit']}/{cov['terms']}（{cov['ratio']}）"
                            + (f"，未见：{'、'.join(cov['missing'])}" if cov["missing"] else ""))
            lines.append(f"- 读数（**仅提示，不作判据**）：回答 {rd.get('answer_chars')} 字符 / "
                         f"{rd.get('answer_sentences')} 句；每千字引用 {rd.get('ref_per_1k_chars')} 处；"
                         f"近似要点覆盖 {cov_txt}\n")
        lines.append("\n- B1 是否被资料支持：______　B2 要点覆盖：______　备注：______\n")
    return "".join(lines)


def readability_summary(samples: list) -> dict:
    """整体读数列：回答长度分布 + 近似要点覆盖的平均值（**仅提示，不作判据**）。"""
    lens = [s["readability"]["answer_chars"] for s in samples
            if s.get("readability") and s["readability"]["answer_chars"]]
    covs = [s["readability"]["approx_point_coverage"]["ratio"] for s in samples
            if s.get("readability") and s["readability"].get("approx_point_coverage")]
    if not lens:
        return {}
    lens_sorted = sorted(lens)
    return {
        "回答字符数": {"最短": lens_sorted[0], "中位": lens_sorted[len(lens_sorted) // 2],
                       "最长": lens_sorted[-1], "平均": round(sum(lens) / len(lens))},
        "近似要点覆盖比例_平均": round(sum(covs) / len(covs), 2) if covs else None,
        "说明": "读数列只描述'回答有多长、要点大概提到多少'，不支持'回答是否正确'的结论",
    }


def rejudge(samples_path: Path, outdir: Path) -> int:
    """用**已保存的回答**重算 A 层判据（**零模型调用**），并重写评审表与汇总。

    为什么需要它：判据本身也可能有缺陷（真实跑第一次就把"括号里的数学示例"误判成编造引用）。
    修正判据后必须能用同一批回答重判，否则"改判据"就变成"再花一次钱重新采样"——那是两回事。
    """
    samples = [json.loads(l) for l in samples_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for s in samples:
        context_ids = {h["id"] for h in s["hits"]} | {n["id"] for n in s["neighbors"]}
        ans = s.get("answer") or ""
        cands = citation_candidates(ans)
        bad = fabricated_refs(ans, context_ids)
        s["citation_candidates"] = cands
        s["fabricated_refs"] = bad
        s["refs"] = [c for c in cands if c in context_ids]
        s["ref_occurrences"] = rc.count_ref_occurrences(ans)
        s["readability"] = readability_stats(s.get("expected_points", ""), ans)
        a1 = bool(s.get("answer")) and not bad
        if s["should_refuse"]:
            s["machine"] = {"A1_引用全部有效": a1, "A2_引用数量符合题型": None,
                            "A3_拒答短语": bool(ans) and (REFUSAL_PHRASE in ans),
                            "note": "判据 v2：拒答题不以引用数量判定（可能先拒答再补邻近引用）"}
        else:
            s["machine"] = {"A1_引用全部有效": a1, "A2_引用数量符合题型": len(s["refs"]) >= 1,
                            "A3_拒答短语": None, "note": ""}

    summary_path = samples_path.parent / "汇总.json"
    meta = {}
    if summary_path.exists():
        meta = json.loads(summary_path.read_text(encoding="utf-8")).get("meta", {})
    meta.update({"rejudged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "rejudge_note": "A 层判据 v2 重判（引用候选只认含 # 的 chunk id 形状；"
                                 "拒答题不以引用数量判定）；本次零模型调用"})

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "样本.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    (outdir / "评审表.md").write_text(render_report(meta, samples), encoding="utf-8")
    (outdir / "汇总.json").write_text(json.dumps({
        "meta": meta,
        "machine_summary": {
            "A1_全部有效": sum(1 for s in samples if s["machine"]["A1_引用全部有效"]),
            "A2_符合题型": sum(1 for s in samples if s["machine"]["A2_引用数量符合题型"]),
            "A3_拒答题命中短语": sum(1 for s in samples if s["machine"]["A3_拒答短语"] is True),
            "拒答题数": sum(1 for s in samples if s["should_refuse"]),
            "有据题数": sum(1 for s in samples if not s["should_refuse"]),
            "编造引用样本数": sum(1 for s in samples if s["fabricated_refs"]),
            "生成失败数": sum(1 for s in samples if s.get("generate_error")),
        },
        "readability_summary": readability_summary(samples),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[05_gen_eval] 已按判据 v2 重判 {len(samples)} 条（零模型调用），输出目录：{outdir}", flush=True)
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成侧最小评测（默认本机桩，零真实请求）")
    ap.add_argument("--target", choices=["stub", "real"], default="stub")
    ap.add_argument("--confirm-real", action="store_true",
                    help="真实模式必须显式确认（配合 --max-calls 使用）")
    ap.add_argument("--max-calls", type=int, default=0,
                    help=f"真实模式允许的最大调用次数（硬上限 {REAL_CALLS_HARD_CAP}）")
    ap.add_argument("--out", default=str(REPO / "output" / "gen_eval"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用）")
    ap.add_argument("--rejudge", default=None,
                    help="用已保存的 样本.jsonl 重算 A 层判据（零模型调用）")
    args = ap.parse_args(argv)

    if args.rejudge:
        return rejudge(Path(args.rejudge), Path(args.out))

    data = load_questions()
    questions = data["questions"]
    if args.limit:
        questions = questions[:args.limit]
    corpus_id, top_k = data["target_corpus"], data["top_k"]
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    api_key, base_url, model = resolve_api_key(), config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL
    real_calls_allowed = 0
    stub_proc = None

    if args.target == "real":
        # ---- 真实模式门禁：先算后发，缺一不发 ----
        if not args.confirm_real:
            print("[05_gen_eval] 拒绝：真实模式必须显式 --confirm-real。", flush=True)
            return 2
        if args.max_calls <= 0:
            print("[05_gen_eval] 拒绝：真实模式必须给 --max-calls（本次上限）。", flush=True)
            return 2
        if args.max_calls > REAL_CALLS_HARD_CAP:
            print(f"[05_gen_eval] 拒绝：--max-calls 超过硬上限 {REAL_CALLS_HARD_CAP}。", flush=True)
            return 2
        if len(questions) > args.max_calls:
            print(f"[05_gen_eval] 拒绝：题目 {len(questions)} 道 > 本次上限 {args.max_calls} 次。", flush=True)
            return 2
        if not api_key:
            print("[05_gen_eval] 拒绝：服务端没有 DEEPSEEK_API_KEY。", flush=True)
            return 2
        if loopback(base_url):
            print(f"[05_gen_eval] 拒绝：真实模式的 base_url 不能是回环地址（{base_url}）。", flush=True)
            return 2
        real_calls_allowed = args.max_calls
        print(f"[05_gen_eval] 真实模式：模型={model} base_url={base_url}")
        print(f"[05_gen_eval] 本次题目 {len(questions)} 道，授权上限 {args.max_calls} 次，**0 重试**；"
              f"Key 只做存在性检查，不回显。", flush=True)
    else:
        api_key = "sk-STUB-NOT-A-REAL-KEY"      # 桩只校验头存在，不校验内容
        base_url = STUB_BASE
        stub_proc = start_stub(outdir)
        print(f"[05_gen_eval] 桩模式：base_url={base_url}（只绑本机回环，零真实模型请求）", flush=True)

    samples: list = []
    call_log: list = []
    try:
        for i, q in enumerate(questions, 1):
            s = run_one(q, api_key=api_key, base_url=base_url, model=model,
                        corpus_id=corpus_id, top_k=top_k, outdir=outdir, call_log=call_log)
            samples.append(s)
            print(f"[{i}/{len(questions)}] {s['eval_id']} 命中={len(s['hits'])} "
                  f"引用={len(s['refs'])} 编造={len(s['fabricated_refs'])} "
                  f"生成={'失败' if s['generate_error'] else ('有' if s['answer'] else '无')}", flush=True)
    finally:
        stub_calls = stub_count() if stub_proc else -1
        kill(stub_proc)

    ran_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta = {
        "target": args.target, "model": model, "base_url": base_url,
        "corpus_id": corpus_id, "top_k": top_k, "ran_at": ran_at,
        "questions": len(samples),
        "real_calls": 0 if args.target == "stub" else len(call_log),
        "stub_calls": stub_calls if args.target == "stub" else None,
        "max_calls": real_calls_allowed,
        "hard_cap": REAL_CALLS_HARD_CAP,
    }

    (outdir / "样本.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    (outdir / "评审表.md").write_text(render_report(meta, samples), encoding="utf-8")
    (outdir / "汇总.json").write_text(json.dumps({
        "meta": meta,
        "machine_summary": {
            "A1_全部有效": sum(1 for s in samples if s["machine"]["A1_引用全部有效"]),
            "A2_符合题型": sum(1 for s in samples if s["machine"]["A2_引用数量符合题型"]),
            "A3_拒答题命中短语": sum(1 for s in samples if s["machine"]["A3_拒答短语"] is True),
            "拒答题数": sum(1 for s in samples if s["should_refuse"]),
            "编造引用样本数": sum(1 for s in samples if s["fabricated_refs"]),
            "生成失败数": sum(1 for s in samples if s["generate_error"]),
        },
        "readability_summary": readability_summary(samples),
        "call_log": call_log,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    call_txt = (f"桩请求 {stub_calls} 次（真实模型请求 0 次）" if args.target == "stub"
                else f"真实调用 {len(call_log)} 次")
    print(f"\n[05_gen_eval] 完成：{len(samples)} 道题；{call_txt}")
    print(f"[05_gen_eval] 输出目录：{outdir}", flush=True)
    if args.target == "real":
        print(f"[05_gen_eval] 真实调用台账：{len(call_log)} 次 / 上限 {real_calls_allowed} 次（0 重试）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
