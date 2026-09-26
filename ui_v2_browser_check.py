# -*- coding: utf-8 -*-
"""浏览器级检查：app_v2.py（第二版）与 app.py（原入口）同尺寸对照。

对应 25 号方案第二步最后一段：常见桌面宽度下检查首屏层级、展开片段、切换知识库、
无嵌套折叠、无 pageerror；两版截图同尺寸便于直接比较。

零成本：生成请求全部指向本脚本内起的**本机回环桩**（假 OpenAI 兼容端点，固定返回
21 号已记录的真实回答原文），不访问任何真实模型接口。

运行：
    python ui_v2_browser_check.py --answers-dir <21 号证据目录> --out <截图输出目录>
本文件内不写任何个人绝对路径（verify_flows.py 的 S10 隐私扫描会扫到本文件）。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
STUB_PORT = 8899
STUB_URL = f"http://127.0.0.1:{STUB_PORT}"
FAKE_KEY = "sk-" + "ui" + "v2" + "stub"
WIDTHS = (1500, 1280)
HEIGHT = 950
TALL = 2800          # 全页截图用的加高视口（Streamlit 内容在内部滚动容器里，full_page 抓不全）

Q_LEARN = "RAG 评估三元组包含哪三个维度？"
Q_POWER = "装设接地线的顺序是什么？"
Q_NO_REF = "红烧肉怎么做？"       # 27 号反例：学习库里确有命中，但回答零引用

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
    "接地线": ("装设接地线时，应先接接地端，后接导体端；拆除接地线的顺序与此相反。"
              "[来源：示例语料-电力安全通用要点#P5]"),
    # 27 号复审的反例：有检索命中、但回答里没有任何可用引用
    "红烧肉": "资料中未找到相关依据。",
}
ANSWER_FILES = {"三元组": "01-三元组.json", "接地线": "07-接地线.json",
                "红烧肉": "03-红烧肉.json"}

STATE = {"answers": dict(EMBEDDED), "requests": []}

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        raise AssertionError(f"{name} 未通过：{detail}")


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                     # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        prompt, q = "", ""
        try:
            prompt = json.loads(raw.decode("utf-8"))["messages"][0]["content"]
        except Exception:                                  # noqa: BLE001
            pass
        if "【问题】" in prompt:
            q = prompt.split("【问题】\n", 1)[1].split("\n\n【回答】")[0].strip()
        STATE["requests"].append(q)
        if "接地线" in q:
            ans = EMBEDDED["接地线"]
        elif "红烧肉" in q:
            ans = EMBEDDED["红烧肉"]        # 无可用引用的反例
        else:
            ans = EMBEDDED["三元组"]
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": ans}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_stub() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def app_env() -> dict:
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = FAKE_KEY
    env["DEEPSEEK_BASE_URL"] = STUB_URL
    env["DEEPSEEK_MODEL"] = "stub-model"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("NODE_OPTIONS", None)
    return env


def start_streamlit(script: str, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", script,
         "--server.port", str(port), "--server.headless", "true",
         "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false"],
        cwd=str(REPO), env=app_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    health = f"http://127.0.0.1:{port}/_stcore/health"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"streamlit {script} 启动失败（提前退出，exit={proc.returncode}）")
        try:
            with urllib.request.urlopen(health, timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:                                  # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"streamlit {script} 健康检查超时")


PROBE = r"""() => {
  const t = document.body.innerText || '';
  const heads = [...document.querySelectorAll('h1,h2,h3,h4')]
      .map(e => e.innerText.trim()).filter(Boolean);
  const box = (sel) => { const e = document.querySelector(sel); if (!e) return null;
      const r = e.getBoundingClientRect(); return {top: Math.round(r.top), left: Math.round(r.left)}; };
  const head_top = (kw) => { const e = [...document.querySelectorAll('h1,h2,h3,h4')]
      .find(x => x.innerText.includes(kw)); return e ? Math.round(e.getBoundingClientRect().top) : null; };
  return {
    headings: heads,
    inputBox: box('input[type="text"]'),
    answerTop: head_top('回答'),
    refTop: head_top('引用对照'),
    evidenceTop: head_top('证据片段'),
    details: document.querySelectorAll('details').length,
    nestedDetails: document.querySelectorAll('details details').length,
    selectboxes: document.querySelectorAll('[data-testid="stSelectbox"]').length,
    radios: document.querySelectorAll('[data-testid="stRadio"]').length,
    checkboxes: document.querySelectorAll('[data-testid="stCheckbox"]').length,
    hasRefSection: t.includes('引用对照'),
    hasEvidence: t.includes('证据片段'),
    hasNeighborBlock: t.includes('同节补充（'),
    hasFakeParams: t.includes('切分长度 800') || t.includes('重叠 120'),
    realParam: t.includes('切分上限 400 字符（无 overlap）'),
    bm25Label: t.includes('排序分，非置信度'),
    orderNotice: t.includes('右侧设置已变更'),
    powerWarning: t.includes('示例资料，不是正式规程'),
    leakLearning: t.includes('air-evaluation'),
    hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
  };
}"""


def scroll_top(pg) -> None:
    """把页面滚回顶部：Streamlit 的滚动可能发生在 window 或内部容器上，两者都归零。"""
    pg.evaluate("""() => {
      window.scrollTo(0, 0);
      document.querySelectorAll('section.main, [data-testid="stAppViewContainer"],'
        + ' [data-testid="stMain"], [data-testid="stSidebar"]')
        .forEach(e => { e.scrollTop = 0; });
    }""")


def wait_text(pg, needle: str, timeout: int = 45000) -> bool:
    try:
        pg.wait_for_selector(f"text={needle}", timeout=timeout)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def fill_and_ask(pg, question: str, button: str) -> None:
    pg.fill("input[type=text]", question)
    pg.get_by_role("button", name=button).click()
    wait_text(pg, "引用对照", 45000)


def select_corpus(pg, label: str) -> None:
    pg.get_by_role("combobox").first.click(timeout=15000)
    time.sleep(0.8)
    pg.get_by_text(label, exact=True).first.click(timeout=15000)
    time.sleep(2.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers-dir", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.answers_dir:
        for kw, fname in ANSWER_FILES.items():
            data = json.loads((Path(args.answers_dir) / fname).read_text(encoding="utf-8"))
            check(f"桩回答与 21 号证据逐字节一致（{kw}）", data["answer"] == EMBEDDED[kw])
        print(f"桩回答来源：{Path(args.answers_dir).name}\n")

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="ui_v2_shots_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    start_stub()

    from playwright.sync_api import sync_playwright

    result: dict = {"widths": list(WIDTHS), "shots": [], "probes": {}, "stub_requests": []}
    v2 = start_streamlit("app_v2.py", 8501)
    v1 = start_streamlit("app.py", 8502)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
            pg = ctx.new_page()
            console_err, page_errors = [], []
            pg.on("console", lambda m: console_err.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: page_errors.append(str(e)))

            print("\n--- app_v2.py（第二版）---")
            pg.goto("http://127.0.0.1:8501", wait_until="load", timeout=90000)
            pg.wait_for_selector("input[type=text]", timeout=90000)
            time.sleep(3)
            fill_and_ask(pg, Q_LEARN, "开始检索")
            time.sleep(2)

            for w in WIDTHS:
                pg.set_viewport_size({"width": w, "height": HEIGHT})
                time.sleep(1.5)
                r = pg.evaluate(PROBE)
                result["probes"][f"v2_{w}"] = r
                heads = r["headings"]
                ii = heads.index("回答") if "回答" in heads else -1
                ri = next((i for i, h in enumerate(heads) if "引用对照" in h), -1)
                ei = next((i for i, h in enumerate(heads) if "证据片段" in h), -1)
                check(f"V2-{w} 首屏层级：问题输入 → 回答/引用对照（并列）→ 证据片段",
                      r["inputBox"] and r["inputBox"]["top"] < r["answerTop"]
                      and 0 <= ii < ri < ei,
                      f"输入框 top={r['inputBox'] and r['inputBox']['top']} "
                      f"回答 top={r['answerTop']} 引用 top={r['refTop']} 证据 top={r['evidenceTop']}")
                check(f"V2-{w} 回答与引用对照确实并列（同一行、左右分栏）",
                      abs(r["answerTop"] - r["refTop"]) <= 40,
                      f"top 差 {abs(r['answerTop'] - r['refTop'])}px")
                check(f"V2-{w} 证据片段在两者下方",
                      r["evidenceTop"] > r["answerTop"] + 100,
                      f"证据 top - 回答 top = {r['evidenceTop'] - r['answerTop']}px")
                check(f"V2-{w} 无嵌套折叠（details details = 0）", r["nestedDetails"] == 0,
                      f"details={r['details']} nested={r['nestedDetails']}")
                check(f"V2-{w} 页面上只有一套控件（主区不重复放选择器）",
                      r["selectboxes"] == 2 and r["radios"] == 1 and r["checkboxes"] == 0,
                      f"selectbox={r['selectboxes']} radio={r['radios']} checkbox={r['checkboxes']}")
                check(f"V2-{w} 真实参数与标注在页面上、设计稿假参数不出现",
                      r["realParam"] and r["bm25Label"] and not r["hasFakeParams"])
                check(f"V2-{w} 无横向溢出（{w}px 宽下列/卡片不撑破）", not r["hOverflow"])
                scroll_top(pg)
                time.sleep(0.5)
                shot = out_dir / f"v2-{w}-首屏.png"
                pg.screenshot(path=str(shot))
                result["shots"].append(shot.name)
                print(f"    截图：{shot.name}")
                pg.set_viewport_size({"width": w, "height": TALL})
                time.sleep(2.0)
                full = out_dir / f"v2-{w}-全页.png"
                pg.screenshot(path=str(full))
                result["shots"].append(full.name)
                print(f"    截图：{full.name}")
                pg.set_viewport_size({"width": w, "height": HEIGHT})
                time.sleep(1.0)

            # 展开一个证据片段，确认展开后正文可见
            pg.set_viewport_size({"width": WIDTHS[0], "height": HEIGHT})
            time.sleep(1)
            pg.locator("details summary").first.click()
            time.sleep(1.2)
            opened = pg.evaluate("""() => {
              const d = document.querySelector('details');
              return {open: !!(d && d.open), nested: document.querySelectorAll('details details').length,
                      text: (d ? d.innerText : '').length};
            }""")
            check("V2 展开片段：折叠块可展开且展开后正文可见，展开态下仍无嵌套折叠",
                  opened["open"] and opened["text"] > 80 and opened["nested"] == 0,
                  f"open={opened['open']} 展开文本 {opened['text']} 字符 "
                  f"nested={opened['nested']}")
            pg.screenshot(path=str(out_dir / f"v2-{WIDTHS[0]}-展开片段.png"))
            pg.locator("details summary").first.click()
            time.sleep(0.8)

            # 切换知识库：警告随库切换 + 旧结果保留 + 变更提示
            select_corpus(pg, "电力示例库")
            r_switch = pg.evaluate(PROBE)
            result["probes"]["v2_switch_power"] = r_switch
            check("V2 切换知识库：出现示例资料警告，并提示当前设置已变更、旧结果仍在",
                  r_switch["powerWarning"] and r_switch["orderNotice"] and r_switch["hasEvidence"],
                  f"警告={r_switch['powerWarning']} 变更提示={r_switch['orderNotice']}")
            fill_and_ask(pg, Q_POWER, "开始检索")
            time.sleep(2)
            r_power = pg.evaluate(PROBE)
            result["probes"]["v2_power"] = r_power
            has_p5 = pg.evaluate("() => (document.body.innerText || '').includes"
                                 "('示例语料-电力安全通用要点#P5')")
            check("V2 电力库第 07 题：引用对照出现完整 id 且学习库内容不串入",
                  has_p5 and not r_power["leakLearning"] and r_power["powerWarning"],
                  f"#P5 可见={has_p5} 学习库串入={r_power['leakLearning']}")
            scroll_top(pg)
            time.sleep(0.8)
            pg.screenshot(path=str(out_dir / f"v2-{WIDTHS[0]}-电力库07.png"))

            # 27 号复审第 3 点：有命中、生成成功、但回答零引用 → 第 4 步不得点亮
            select_corpus(pg, "RAG 技术学习库")
            fill_and_ask(pg, Q_NO_REF, "开始检索")
            time.sleep(2)
            r_noref = pg.evaluate("""() => {
              const t = document.body.innerText || '';
              const bar = document.querySelector('div.v2-steps');
              const cells = bar ? [...bar.children] : [];
              const step4 = cells[3] || null;
              return {hasPendingLabel: t.includes('引用检查：无可用引用'),
                      hasNoRefNote: t.includes('本次回答没有可用引用'),
                      step4Class: step4 ? step4.className : null,
                      step4Text: step4 ? step4.innerText : null,
                      activeCount: cells.filter(c => c.className.includes('active')).length,
                      details: document.querySelectorAll('details').length,
                      nestedDetails: document.querySelectorAll('details details').length,
                      hOverflow: document.documentElement.scrollWidth
                                 > document.documentElement.clientWidth + 2};
            }""")
            result["probes"]["v2_no_usable_refs"] = r_noref
            check("V2 无可用引用反例：第 4 步显示「引用检查：无可用引用」且不点亮，"
                  "第 3 步仍亮，无嵌套折叠/无溢出",
                  r_noref["hasPendingLabel"] and r_noref["hasNoRefNote"]
                  and "pending" in (r_noref["step4Class"] or "")
                  and "done" not in (r_noref["step4Class"] or "")
                  and r_noref["activeCount"] == 1
                  and r_noref["nestedDetails"] == 0 and not r_noref["hOverflow"],
                  f"第4步 class={r_noref['step4Class']} 文本={r_noref['step4Text']} "
                  f"active={r_noref['activeCount']}")
            scroll_top(pg)
            time.sleep(0.8)
            pg.screenshot(path=str(out_dir / f"v2-{WIDTHS[0]}-无可用引用.png"))

            check("V2 无 pageerror / 无 console error",
                  not page_errors and not console_err,
                  f"pageerror={page_errors[:2]} console={console_err[:2]}")
            result["probes"]["pageerrors"] = page_errors
            result["probes"]["console_errors"] = console_err
            ctx.close()

            # ---- 原入口 app.py 同尺寸对照 ----
            print("\n--- app.py（原入口，同尺寸对照）---")
            ctx2 = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
            pg2 = ctx2.new_page()
            err2 = []
            pg2.on("pageerror", lambda e: err2.append(str(e)))
            pg2.goto("http://127.0.0.1:8502", wait_until="load", timeout=90000)
            pg2.wait_for_selector("input[type=text]", timeout=90000)
            time.sleep(3)
            pg2.fill("input[type=text]", Q_LEARN)
            pg2.get_by_role("button", name="提问").click()
            wait_text(pg2, "回答", 45000)
            time.sleep(2)
            for w in WIDTHS:
                pg2.set_viewport_size({"width": w, "height": HEIGHT})
                time.sleep(1.5)
                scroll_top(pg2)
                time.sleep(0.5)
                shot = out_dir / f"v1-{w}-首屏.png"
                pg2.screenshot(path=str(shot))
                result["shots"].append(shot.name)
                print(f"    截图：{shot.name}")
                pg2.set_viewport_size({"width": w, "height": TALL})
                time.sleep(2.0)
                full = out_dir / f"v1-{w}-全页.png"
                pg2.screenshot(path=str(full))
                result["shots"].append(full.name)
                print(f"    截图：{full.name}")
                pg2.set_viewport_size({"width": w, "height": HEIGHT})
                time.sleep(1.0)
            check("V1 原入口对照页无 pageerror（确认两版截图环境一致）", not err2,
                  f"pageerror={err2[:2]}")
            result["probes"]["pageerrors_v1"] = err2
            ctx2.close()
            browser.close()
    finally:
        for proc in (v2, v1):
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:                              # noqa: BLE001
                proc.kill()

    result["stub_requests"] = STATE["requests"]
    (out_dir / "ui_v2_browser_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "-" * 78)
    print(f"PASS {len(PASSED)} 项；FAIL {len(FAILED)} 项。")
    print(f"桩共收到 {len(STATE['requests'])} 次生成请求，全部指向本机 {STUB_URL}；"
          "未访问任何真实模型接口。")
    print(f"截图与探针写入：{out_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
