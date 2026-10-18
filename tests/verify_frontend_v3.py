# -*- coding: utf-8 -*-
"""第三版独立前端（frontend_v3）浏览器验收。

【2026-09-28 重设计说明】视觉基准切换为 Open WebUI 系（用户从 D 系 mockup 选定，
规范见 本地设计文档（未随仓库发布））：
布局改为「左侧文字导航栏（262px）+ 四视图（对话/资料库/批量任务/设置）」，
只有对话列居中，其余页左对齐；卡片扁平无阴影、圆角 8px、近黑主按钮。
随之更新的判据（功能/数据契约判据全部原样保留）：
  - 视觉① 由「设置栏在右」改为「侧栏在左、内容列居中、宽度比、不出界」；
  - 验收② 保留「回答/引用并排两卡」的原始形态（首屏密度要求不变），断言几何不变；
  - 验收⑧（2026-09-28 聊天式布局，用户要求）提问框常驻对话页底部：
    .ask-card 在内容之下且贴住 #pane-chat 底部，内容区改为 .chat-scroll 内部滚动；
  - 验收⑤ 的视觉 token 更新为新设计值（8px / 无阴影 / 标题 15px）；
  - keybox / 知识库统计 / 控件唯一性等原本依赖「同屏设置栏」的判据，
    改为导航到「设置」页后抓取（设置控件仍只有一套，仍全部在 .aside 内）；
  - 设置漂移（CE-A / api 漂移）流程改为：对话页提问 → 设置页改控件 → 回对话页看归属提示；
    文案随新设计从「与右侧当前设置不一致」改为「与当前设置不一致」。
判据计数随窄修演进（69 号 R1 收口后：mock 42 项 / api 37 项，新增均为草稿切页
保留与切页状态回归，无删减）；2026-09-28 聊天式布局新增验收⑧与 api 视觉③
→ mock 43 项 / api 39 项；文本契约（NOT VALIDATED 区分、零结果短路、
不可达与失败分开表达等）一字未动。
2026-09-29 对话流改版（v1.3，用户拍板）：步骤条/双卡并排/常驻证据表 → 单轮问答
消息流（用户气泡在右、回答裸排在左），证据默认折叠进 details.disc，参数收进输入条。
随之更新的判据（计数不变：mock 43 / api 39）：
  - PROBE 页面文本改用 textContent（折叠 details 内文本不进 innerText，但断言的是真实 DOM）；
  - 验收②/③/④/⑥与各状态的 steps 断言改为对话流等价判据（气泡/消息几何、
    披露条默认折叠、输入条参数行、引用对照诚实说明）；
  - ask()/api_ask()/ok 状态取证流程：等 #result-scope 出现后点开 #evidence-card 再等 .evidence-row；
  - 控件唯一性改为「.aside 内 1 select + 4 radio」（输入条新增 2 个 select 与设置页同源）。

两种模式，**计数分开报告，互不覆盖**：

    --backend mock（默认）  第一阶段回归：`vite preview` 服务 mock 构建产物，
                            页面数据来自本地快照（真实 rag_core 检索结果 + 21 号已记录回答原文），
                            零真实模型请求、零外部网络。
                            含 28 号「视觉验收」七条、七种 ?state= 演示状态、30 号真实交互反例 CE-A/CE-B。
    --backend api           第二阶段 A：页面通过本机 `api_v3.py` 读**真实学习资料**并实时检索，
                            生成层走 `tools/llm_stub_server.py`（本机桩，绝不联网）。
                            验真实统计 / 真实命中 / 逐字段对拍 / 仅检索 / 零结果 / 无 Key /
                            桩失败 / API 不可达 / 设置漂移 / Top-K 真改 / 视觉不回退。

两种模式都会先按对应数据源**重新构建**（api 模式注入 VITE_DATA_SOURCE/VITE_API_BASE），
并核对构建产物里有没有出现对应模式的标志文案，避免拿旧构建凑结论。

运行：
    python tests/verify_frontend_v3.py --backend mock [--out <截图目录>] [--no-build]
    python tests/verify_frontend_v3.py --backend api  [--out <截图目录>]
api 模式必须按本轮隔离端口重建；--no-build 仅供 mock 模式使用。
前置：frontend_v3 里已 `npm install`；api 模式会自动拉起本机桩与 api_v3.py。
本文件内不写任何个人绝对路径。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend_v3"
URL = "http://127.0.0.1:5273"
WIDTHS = (1500, 1280)
HEIGHT = 950
STATES = ("initial", "loading", "ok", "no-refs", "zero", "no-key", "failure")

# ---- api 模式：接口/桩的端口与凭据哨兵都复用 verify_api_v3.py，避免两份实现漂移 ----
sys.path.insert(0, str(REPO))
import verify_api_v3 as iap                                   # noqa: E402

API_BASE = f"http://127.0.0.1:{iap.API_PORT}"
Q_ZERO_LOCAL = "红烧肉怎么做？"      # 在电力示例库里是零结果
Q_GROUND = "装设接地线的顺序是什么？"

PASSED, FAILED = [], []
PROBES: dict = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        raise AssertionError(f"{name} 未通过：{detail}")


def stop_tree(proc: subprocess.Popen) -> None:
    """结束预览服务。

    Windows 下 `npm run preview` 会派生子进程，真正监听端口的是子进程；
    只 terminate 外壳会留下占着 5273 的孤儿 node 进程（上一轮踩过），所以整棵树一起杀。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:                                          # noqa: BLE001
        proc.kill()


def build_frontend(backend: str) -> dict:
    """按数据源重新构建，并核对产物里确实带了该模式的标志文案。

    为什么必须构建后再验收：preview 服务的是 `dist/`，如果端上还躺着上一次别的数据源的
    构建，验收就会"验了个寂寞"。这里连构建产物一起验。
    """
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("找不到 npm，无法构建前端")
    env = dict(os.environ)
    if backend == "api":
        env["VITE_DATA_SOURCE"] = "api"
        env["VITE_API_BASE"] = API_BASE
    else:
        env.pop("VITE_DATA_SOURCE", None)
        env.pop("VITE_API_BASE", None)
    proc = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"npm run build 失败（backend={backend}）：{(proc.stdout or '')[-600:]}")

    assets = sorted((FRONTEND / "dist" / "assets").glob("*.js"))
    if not assets:
        raise RuntimeError("构建产物里没有 JS，dist/assets 目录异常")
    blob = "".join(p.read_text(encoding="utf-8", errors="replace") for p in assets)
    marker = "已接入本机 API" if backend == "api" else "第一阶段：本地示例数据"
    stale = "第一阶段：本地示例数据" if backend == "api" else "已接入本机 API"
    return {"js_files": [p.name for p in assets], "js_bytes": sum(p.stat().st_size for p in assets),
            "marker_ok": marker in blob, "stale_marker_present": stale in blob}


def port_busy(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pick_free_port(preferred: int) -> int:
    """默认端口被占（例如正式 launcher 常驻在 5273/5280）时改用系统分配的空闲端口。
    70 号复审期间实测：健康检查只看 200 会被正式服务的应答骗过，套件在陌生构建上跑完整轮。"""
    if not port_busy(preferred):
        return preferred
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _localhost_get(port: int, path: str = "/") -> tuple[int, str]:
    """只对字面量 127.0.0.1 发 HTTP GET（测试工具自身用的探活/回读，不接任何外部目标）。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def start_preview(port: int | None = None) -> subprocess.Popen:
    global URL
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("找不到 npm，无法启动预览服务")
    if not (FRONTEND / "dist" / "index.html").exists():
        raise RuntimeError("frontend_v3/dist 不存在，请先运行 npm run build")
    if port is None:
        port = pick_free_port(5273)
    if port != 5273:
        print(f"[INFO] 127.0.0.1:5273 已被其他服务占用（可能是正式 launcher），"
              f"mock 回归改用空闲端口 {port}", flush=True)
    proc = subprocess.Popen(
        [npm, "run", "preview", "--", "--port", str(port), "--strictPort"],
        cwd=str(FRONTEND),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    URL = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"预览服务提前退出（exit={proc.returncode}）")
        try:
            status, body = _localhost_get(port)
            if status == 200:
                # 服务内容身份校验：回读页面必须引用本轮 dist 的 JS，
                # 否则说明端口上应答的是别的服务，拒绝在陌生构建上跑回归
                expected = {p.name for p in (FRONTEND / "dist" / "assets").glob("index-*.js")}
                if expected and not any(name in body for name in expected):
                    raise RuntimeError(
                        f"{URL} 返回的构建不属于本轮 dist（{sorted(expected)}）"
                        "——端口被外部服务占用，拒绝继续")
                return proc
        except RuntimeError:
            raise
        except Exception:                                   # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("预览服务健康检查超时")


def open_view(pg, view: str) -> None:
    """点左侧导航切换视图（data-view 属性定位，不依赖按钮文案）。"""
    pg.click(f".sb-navbtn[data-view='{view}']")
    time.sleep(0.4)


def wait_meta(pg, timeout: int = 30000) -> None:
    """等 api 模式的 meta 落定：顶栏标签变为「已接入」（或「不可达」）才算到位。
    不能用 select.select 出现来判断——它带 [1..5] 兜底选项，meta 没到也会渲染。"""
    pg.wait_for_function(
        "() => { const t = document.querySelector('.topbar .tag');"
        " return !!t && (t.innerText.includes('已接入') || t.innerText.includes('不可达')); }",
        timeout=timeout)


# 一次性抓齐所有验收判据，避免多次往返造成状态漂移
PROBE = r"""() => {
  const t = document.body.textContent || '';  // v1.3：证据默认折叠（details 内文本不进 innerText），改用 textContent
  const q = (sel) => document.querySelector(sel);
  const rect = (sel) => { const e = q(sel); if (!e) return null;
      const r = e.getBoundingClientRect();
      return {top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right),
              bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height)}; };
  const steps = [...document.querySelectorAll('.stepbar .step')].map(e => ({
      cls: e.className, text: e.innerText.replace(/\s+/g, ' ').trim(),
      labelClipped: (() => { const l = e.querySelector('.step__label');
          return l ? l.scrollWidth > l.clientWidth + 1 : false; })()}));
  const rows = [...document.querySelectorAll('.evidence-row')];
  const firstRow = rows[0] ? rows[0].getBoundingClientRect() : null;
  const lists = [...document.querySelectorAll('.evidence-list')];
  const listIds = (i) => lists[i]
      ? [...lists[i].querySelectorAll('.evidence-row__head')].map(e => e.title) : [];
  // 只取带小数点的数：行里还写着 "BM25"，直接剔非数字会把牌子里的 25 也当成分数
  const listScores = (i) => lists[i]
      ? [...lists[i].querySelectorAll('.evidence-row__score')]
          .map(e => { const m = e.textContent.match(/(\d+\.\d+)/); return m ? m[1] : ''; }) : [];
  const cs = (sel, prop) => { const e = q(sel); return e ? getComputedStyle(e)[prop] : null; };
  // 2026-09-28 R1 窄修后四页常驻挂载：notice / select 必须收窄作用域，
  // 否则会抓到隐藏页（资料库提示条 / 批量表单的 3 个 select）的元素
  const widest = [...document.querySelectorAll('.page *')].reduce((m, e) => {
      const r = e.getBoundingClientRect(); return Math.max(m, r.right); }, 0);
  return {
    text: t,
    steps,
    sidebar: rect('.sidebar'),
    notice: (q('.main-col .notice') ? {cls: q('.main-col .notice').className, text: q('.main-col .notice').innerText.trim()} : null),
    aside: rect('.aside'),
    main: rect('.main-col'),
    answerCard: rect('#answer-card'),
    refCard: rect('#ref-card'),
    evidenceTitle: rect('#evidence-title'),
    // 2026-09-28 聊天式布局：提问框常驻对话页底部（验收⑧取材）
    askCard: rect('.ask-card'),
    paneChat: rect('#pane-chat'),
    // —— v1.3 对话流：消息流几何与证据折叠状态 ——
    msgUserBubble: rect('.msg--user .msg__bubble'),
    // 70 号复审 R4：首屏可见性必须相对滚动容器的可见区判定，不能只跟视口比
    chatClip: rect('#chat-scroll'),
    evidenceCovered: (() => { const el = q('#evidence-title'); if (!el) return null;
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(
            r.left + Math.min(r.width / 2, 40), Math.max(r.top + Math.min(r.height / 2, 10), 1));
        return hit ? !el.contains(hit) : null; })(),
    discOpen: q('#evidence-card') ? q('#evidence-card').hasAttribute('open') : null,
    helloVisible: !!q('#hello'),
    firstRowTop: firstRow ? Math.round(firstRow.top) : null,
    rowCount: rows.length,
    detailsCount: document.querySelectorAll('details').length,
    nestedDetails: document.querySelectorAll('details details').length,
    selectCount: document.querySelectorAll('select').length,
    radioCount: document.querySelectorAll('input[type=radio]').length,
    selectInAside: document.querySelectorAll('.aside select').length,
    radioInAside: document.querySelectorAll('.aside input[type=radio]').length,
    keyboxClass: q('.keybox') ? q('.keybox').className : null,
    keyboxText: q('.keybox') ? q('.keybox').innerText.trim() : null,
    tagText: q('.topbar .tag') ? q('.topbar .tag').innerText.trim() : null,
    citeCount: document.querySelectorAll('.answer-body .cite').length,
    strongCount: document.querySelectorAll('.answer-body strong').length,
    refItems: [...document.querySelectorAll('.ref-item')].map(e => e.innerText.replace(/\s+/g, ' ').trim()),
    chips: [...document.querySelectorAll('.chip')].map(e => e.innerText.trim()),
    hasSkeleton: !!q('.skeleton'),
    computed: {
      cardRadius: cs('.card', 'borderRadius'), cardBg: cs('.card', 'backgroundColor'),
      cardBorder: cs('.card', 'borderTopColor'), cardShadow: cs('.card', 'boxShadow'),
      pageBg: cs('body', 'backgroundColor'), titleSize: cs('.topbar__title', 'fontSize'),
      panelTitleSize: cs('.msg__who b', 'fontSize'), stepActiveBg: cs('.step.is-active', 'backgroundColor'),
    },
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
    contentMaxRight: Math.round(widest),
    // —— 30 号复审用：结果归属与 Top-K 一致性 ——
    resultScope: q('#result-scope')
        ? q('#result-scope').innerText.replace(/\s+/g, ' ').trim() : null,
    staleNotice: q('#stale-notice')
        ? q('#stale-notice').innerText.replace(/\s+/g, ' ').trim() : null,
    // —— 第二阶段 A（api 模式）用：数据源标注、传输层失败、回答来源行 ——
    dataSource: q('.page') ? q('.page').getAttribute('data-datasource') : null,
    forcedStateAttr: q('.page') ? q('.page').getAttribute('data-forced-state') : null,
    transportNotice: q('#transport-notice')
        ? q('#transport-notice').innerText.replace(/\s+/g, ' ').trim() : null,
    answerMetas: [...document.querySelectorAll('#answer-card .panel__meta')]
        .map(e => e.innerText.replace(/\s+/g, ' ').trim()),
    inlineWarn: q('.inline-warn') ? q('.inline-warn').innerText.replace(/\s+/g, ' ').trim() : null,
    hitIds: listIds(0),
    neighborIds: listIds(1),
    hitScores: listScores(0),
    // 用"选中项所在的 label 文字"判断，不依赖 React 受控 radio 是否渲染 value 属性
    corpusChecked: (() => { const el = q('input[name=corpus]:checked');
        const lab = el && el.closest('label'); return lab ? lab.innerText.trim() : null; })(),
    modeChecked: (() => { const el = q('input[name=mode]:checked');
        const lab = el && el.closest('label'); return lab ? lab.innerText.trim() : null; })(),
    topKValue: q('.aside select.select') ? q('.aside select.select').value : null,
  };
}"""


Q_TRIAD = "RAG 评估三元组包含哪三个维度？"
SNAPSHOT_PATH = FRONTEND / "src" / "mocks" / "api_snapshots.json"


def _expected(snap: dict, scenario: str, top_k: int):
    """从 mock 快照现算期望值：命中 id / 补充 id / BM25 分数（页面必须逐条对上）。"""
    v = snap["scenarios"][scenario]["by_top_k"][str(top_k)]
    return ([h["id"] for h in v["hits"]],
            [h["id"] for h in v["neighbors"]],
            [f"{h['score']:.4f}" for h in v["hits"]])


def interactive_counterexamples(pg, out_dir: Path, results: dict) -> None:
    """30 号复审要求的不带 `?state=` 的**真实交互**反例。

    CE-A：学习库出结果后切电力库 / 切模式 / 切 Top-K —— 旧结果不得被新设置重新标记；
    CE-B：Top-K = 1/3/5 必须真的改变命中、补充与分数，而不是只改标签。
    期望值全部从 mock 快照现算，逐条与页面 DOM 对齐。
    2026-09-28 重设计：设置控件在独立「设置」页，切换控件须先经左侧导航过去，再回对话页看归属。
    """
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    def ask(question: str, top_k: int | None = None) -> dict:
        pg.goto(f"{URL}/", wait_until="load", timeout=60000)
        if top_k is not None:
            open_view(pg, "set")
            # 70 号复审期间暴露的竞态：不收窄的 "select.select" 会把批量页（常驻挂载）的
            # #batch-corpus 当作首个匹配等可见，批量页 select 先挂载即超时。与 69 号
            # R1 的选择操作收窄同口径：等待也只认设置页 .aside 里的那一个。
            pg.wait_for_selector(".aside select.select", timeout=60000)
            pg.select_option(".aside select.select", str(top_k))
            open_view(pg, "chat")
        pg.wait_for_selector("#question", timeout=60000)
        pg.fill("#question", question)
        pg.get_by_role("button", name="开始检索").click()
        # v1.3 对话流：证据默认折叠——先等结果归属行出现，再展开披露框取证
        pg.wait_for_selector("#result-scope", timeout=45000)
        pg.click("#evidence-card > summary")
        pg.wait_for_selector(".evidence-row", timeout=45000)
        time.sleep(0.8)
        return pg.evaluate(PROBE)

    # ---- CE-A：设置漂移不得重新标记旧结果 ----
    r0 = ask(Q_TRIAD)
    exp_hits, exp_neigh, _ = _expected(snap, "learning_triad", 3)
    check("CE-A1 反例前置成立：真实交互（不带 ?state=）出学习库结果，且结果按学习库标注、无漂移提示",
          r0["hitIds"] == exp_hits and r0["neighborIds"] == exp_neigh
          and "RAG 技术学习库" in (r0["resultScope"] or "")
          and "Top-K 3" in (r0["resultScope"] or "") and r0["staleNotice"] is None,
          f"命中 id {r0['hitIds']} / scope={r0['resultScope']}")

    # 按"控件在设置页里的第几个"定位，不依赖 value 属性是否被渲染。
    # 切换在「设置」页完成，归属提示回「对话」页看——两页状态同源（同一 React 状态树）。
    open_view(pg, "set")
    pg.wait_for_selector(".aside", timeout=60000)
    pg.locator('.aside input[name="corpus"]').nth(1).check()
    time.sleep(0.8)
    r1set = pg.evaluate(PROBE)
    check("CE-A2a 设置页内：电力库单选确实被选中（切库动作真实生效）",
          r1set["corpusChecked"] == "电力示例库", f"选中={r1set['corpusChecked']}")
    open_view(pg, "chat")
    time.sleep(0.8)
    r1 = pg.evaluate(PROBE)
    check("CE-A2 切到电力库后回到对话页：出现「设置已变更」提示，且结果的归属仍标为学习库",
          r1["staleNotice"] is not None
          and "知识库 → 电力示例库" in r1["staleNotice"]
          and "RAG 技术学习库" in (r1["resultScope"] or "")
          and "与当前设置不一致" in (r1["resultScope"] or ""),
          f"stale={r1['staleNotice']}")
    check("CE-A3 旧学习库结果没有被表述成电力库结果（页面上不出现电力库 chunk id）",
          "电力安全通用要点" not in r1["text"] and bool(r1["hitIds"])
          and all(i.startswith("air-") for i in r1["hitIds"]),
          f"命中 id {r1['hitIds']}；页面是否含电力库 id="
          f"{'电力安全通用要点' in r1['text']}")

    open_view(pg, "set")
    pg.locator('.aside input[name="mode"]').nth(0).check()
    time.sleep(0.8)
    open_view(pg, "chat")
    time.sleep(0.8)
    r2 = pg.evaluate(PROBE)
    check("CE-A4 切生成模式（→ 仅检索）同样提示已变更，结果归属仍是「检索并生成」",
          "生成模式 → 仅检索" in (r2["staleNotice"] or "")
          and "检索并生成" in (r2["resultScope"] or ""),
          f"stale={r2['staleNotice']}")

    open_view(pg, "set")
    pg.select_option(".aside select.select", "5")
    time.sleep(0.8)
    open_view(pg, "chat")
    time.sleep(0.8)
    r3 = pg.evaluate(PROBE)
    check("CE-A5 切 Top-K（→ 5）同样提示已变更，结果归属仍是 Top-K 3",
          "Top-K → 5" in (r3["staleNotice"] or "")
          and "Top-K 3" in (r3["resultScope"] or ""),
          f"stale={r3['staleNotice']} scope={r3['resultScope']}")
    pg.screenshot(path=str(out_dir / "v3-CE-A-切换设置后结果归属.png"))
    results["shots"].append("v3-CE-A-切换设置后结果归属.png")

    # ---- CE-B：Top-K 必须真的改变结果 ----
    counts: dict = {}
    scores: dict = {}
    for k in (1, 3, 5):
        r = ask(Q_TRIAD, top_k=k)
        exp_hits, exp_neigh, exp_scores = _expected(snap, "learning_triad", k)
        counts[k], scores[k] = len(r["hitIds"]), r["hitScores"]
        check(f"CE-B1 Top-K={k}：展示的命中 id 与该 Top-K 快照逐条一致（不是只改标签）",
              r["hitIds"] == exp_hits and len(exp_hits) == k,
              f"页面 {r['hitIds']} / 快照 {exp_hits}")
        check(f"CE-B2 Top-K={k}：同节补充也按该 Top-K 重算，行数 = 命中 + 补充",
              r["neighborIds"] == exp_neigh
              and r["rowCount"] == len(exp_hits) + len(exp_neigh)
              and f"BM25 Top-{k} 命中 {k} 段" in r["text"],
              f"补充 {len(r['neighborIds'])} 段 / 页面共 {r['rowCount']} 行")
        check(f"CE-B3 Top-K={k}：BM25 分数与快照一致，结果归属写明 Top-K {k}，且无漂移提示",
              r["hitScores"] == exp_scores and f"Top-K {k}" in (r["resultScope"] or "")
              and r["staleNotice"] is None,
              f"分数 {r['hitScores']} / 快照 {exp_scores}")
    check("CE-B4 反向对照：Top-1/3/5 的命中数与分数列表两两不同（证明断言不是只查文字）",
          counts.get(1) == 1 and counts.get(3) == 3 and counts.get(5) == 5
          and len({tuple(scores[k]) for k in (1, 3, 5)}) == 3,
          f"命中数 {counts}；分数列表 { {k: len(v) for k, v in scores.items()} }")
    # Top-K 选择器真实停留在最后选择的值（设置页内核对，不依赖对话页的 DOM）
    open_view(pg, "set")
    pg.wait_for_selector(".aside select.select", timeout=60000)
    time.sleep(0.4)
    r_set = pg.evaluate(PROBE)
    check("CE-B5 设置页内 Top-K 选择器停留在最后选择的值（选择真实生效，不是只改标签）",
          r_set["topKValue"] == "5", f"select.value={r_set['topKValue']}")
    open_view(pg, "chat")
    # CE-C（R1 窄修回归，69 号复审）：四页常驻挂载后，切页不得丢会话状态——
    # 对话结果在去设置页再回来后应原样保留（运行状态存于 App 级 useWorkbench）
    time.sleep(0.8)
    rc_back = pg.evaluate(PROBE)
    exp5_hits, exp5_neigh, _ = _expected(snap, "learning_triad", 5)
    check("CE-C 会话内切页保留：对话结果在去设置页再回来后原样保留（Top-K 5 的命中与归属不变）",
          rc_back["rowCount"] == len(exp5_hits) + len(exp5_neigh)
          and rc_back["hitIds"] == exp5_hits and rc_back["neighborIds"] == exp5_neigh
          and "Top-K 5" in (rc_back["resultScope"] or "") and rc_back["staleNotice"] is None,
          f"行 {rc_back['rowCount']}（命中 {len(exp5_hits)} + 补充 {len(exp5_neigh)}）"
          f" / scope={rc_back['resultScope']}")
    pg.screenshot(path=str(out_dir / f"v3-CE-B-TopK5-{WIDTHS[0]}.png"))
    results["shots"].append(f"v3-CE-B-TopK5-{WIDTHS[0]}.png")


# ---------------- 第二阶段 A：api 模式的页面验收 ----------------
def rc_expected(corpus_id: str, question: str, top_k: int):
    """期望值来自**直接调用 rag_core**（不是接口、不是快照）。"""
    import rag_core as rc
    assembled = rc.expand_and_assemble(question, top_k=top_k, corpus_id=corpus_id)
    return ([h["id"] for h in assembled["hits"]],
            [h["id"] for h in assembled["neighbors"]],
            [f"{h['score']:.4f}" for h in assembled["hits"]])


# 等待判据用**普通 JS 表达式**（Playwright 会反复求值到它为真）
WAIT_ROWS = ("document.querySelectorAll('.evidence-row').length > 0"
             " && !!document.querySelector('#result-scope')")
WAIT_TEXT = ("!!document.querySelector('.main-col .notice')"
             " && document.querySelector('.main-col .notice').innerText.includes('%s')")
WAIT_TRANSPORT = "!!document.querySelector('#transport-notice')"


def api_ask(pg, question: str, *, top_k: int | None = None, mode: str | None = None,
            corpus: int | None = None, expect_js: str = WAIT_ROWS, timeout: int = 90000) -> dict:
    """真实交互（每次重载页面，避免读到上一次的 DOM）：设设置 → 提问 → 等判据 → 抓现场。
    2026-09-28 重设计：设置控件在独立「设置」页，先经左侧导航过去改，再回对话页提问。"""
    pg.goto(f"{URL}/", wait_until="load", timeout=60000)
    if any(v is not None for v in (top_k, mode, corpus)):
        open_view(pg, "set")
        pg.wait_for_selector(".aside", timeout=60000)
        # meta 异步到达：等顶栏标签落定（已接入/不可达），控件（radio/select）才有真实数据
        wait_meta(pg)
        if corpus is not None:
            pg.locator('.aside input[name="corpus"]').nth(corpus).check()
        if mode is not None:
            pg.locator('.aside input[name="mode"]').nth(0 if mode == "retrieve_only" else 1).check()
        if top_k is not None:
            pg.select_option(".aside select.select", str(top_k))
        open_view(pg, "chat")
    pg.wait_for_selector("#question", timeout=60000)
    pg.fill("#question", question)
    pg.get_by_role("button", name="开始检索").click()
    pg.wait_for_function(expect_js, timeout=timeout)
    # v1.3 对话流：证据默认折叠，探针读 DOM 不受折叠影响（行数用 querySelectorAll、
    # 文本用 textContent），这里不点开披露框——api 视觉②要断言「默认折叠」
    time.sleep(0.6)
    return pg.evaluate(PROBE)


def run_api_backend(out_dir: Path, skip_build: bool) -> int:
    # 71 号复审 T1：旧构建可能仍指向正式 API。先拒绝跳过构建，不能启动服务后再发现地址不符。
    if skip_build:
        print("[ERROR] api 验收不支持 --no-build：必须按本轮隔离端口重新构建，"
              "避免旧产物指向正式后端。请移除 --no-build 后重试。", file=sys.stderr, flush=True)
        return 2

    import rag_core as rc
    from playwright.sync_api import sync_playwright

    # 70 号复审期间发现：正式 launcher 常驻时 5280 可能被占。api 回归的前端构建把 API 地址
    # 烧进产物，绝不允许请求默默打到正式后端（带真实 Key）——端口被占就整体换隔离端口，
    # 并用该端口重烧前端。iap 的启动器都是调用期读模块全局，这里重赋值即全链生效。
    global API_BASE
    api_port = pick_free_port(iap.API_PORT)
    if api_port != iap.API_PORT:
        print(f"[INFO] 127.0.0.1:{iap.API_PORT} 已被占用（可能是正式 api_v3 服务），"
              f"api 回归改用隔离端口 {api_port}，前端构建同步烧入该地址", flush=True)
    stub_port = pick_free_port(iap.STUB_PORT)
    # 预览端口要先于 api 后端定下来：api_v3 的 CORS 白名单在启动时冻结，
    # 必须把本次预览的实际端口放进去（api_v3.py 本体零改动，见 verify_api_v3.start_api）
    preview_port = pick_free_port(5273)
    cors_origins = (f"http://127.0.0.1:{preview_port}", f"http://localhost:{preview_port}")
    iap.API_PORT = api_port
    iap.STUB_PORT = stub_port
    iap.STUB_BASE = f"http://127.0.0.1:{stub_port}/v1"
    API_BASE = f"http://127.0.0.1:{api_port}"

    results: dict = {"backend": "api", "api_base": API_BASE, "stub_base": iap.STUB_BASE,
                     "shots": [], "phases": []}
    build = build_frontend("api")
    results["build"] = build
    check("页面·api 前置：构建产物确实是 api 数据源（含「已接入本机 API」且不含第一阶段示例数据文案）",
          build["marker_ok"] and not build["stale_marker_present"],
          f"JS {build['js_files']} 共 {build['js_bytes']} 字节")

    meta = rc.corpus_stats(rc.RAG_LEARNING)
    stub_log = out_dir / "llm_stub_requests.jsonl"
    api_proc = stub_proc = preview = None
    api_starts = 0
    page_errors: list = []
    console_errors: list = []
    errors_mark = 0

    try:
        stub_proc = iap.start_stub("ok", stub_log)
        api_proc = iap.start_api(True, out_dir / "api_v3_key.log", cors_origins=cors_origins)
        api_starts += 1
        preview = start_preview(port=preview_port)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: page_errors.append(str(e)))
            pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

            print("\n--- api 模式：元数据与真实资料统计 ---")
            pg.goto(f"{URL}/", wait_until="load", timeout=60000)
            pg.wait_for_selector(".sidebar", timeout=60000)
            # 知识库统计与 Key 状态在「设置」页：导航过去抓（顶栏标签在任何页都在）
            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            wait_meta(pg)
            time.sleep(0.8)
            r0 = pg.evaluate(PROBE)
            PROBES["api_meta_1500"] = r0
            check("页面·api：顶栏标注「已接入本机 API」，页面不再出现「第一阶段：本地示例数据」",
                  r0["dataSource"] == "api" and "已接入本机 API" in (r0["tagText"] or "")
                  and "本地示例数据" not in r0["text"],
                  f"data-source={r0['dataSource']} tag={r0['tagText']}")
            check("页面·api：设置页的知识库统计就是真实学习库（与 rag_core 现算一致）",
                  f"来源 {meta['source_count']} 份" in r0["text"]
                  and f"chunk {meta['chunk_count']} 个" in r0["text"],
                  f"rag_core 现算：{meta['source_count']} 来源 / {meta['chunk_count']} chunk")
            check("页面·api：Key 如实显示「服务端已检测到」，页面不出现任何 key 内容",
                  "服务端已检测到" in (r0["keyboxText"] or ""), f"keybox={r0['keyboxText']}")

            pg.goto(f"{URL}/?state=ok", wait_until="load", timeout=60000)
            pg.wait_for_selector(".sidebar", timeout=60000)
            time.sleep(0.8)
            r_forced = pg.evaluate(PROBE)
            PROBES["api_forced_state_1500"] = r_forced
            check("页面·api：带 ?state=ok 打开也不会用示例数据伪造结果（api 模式忽略演示开关）",
                  r_forced["rowCount"] == 0 and r_forced["forcedStateAttr"] == ""
                  and "还没有提问" in r_forced["text"],
                  f"证据行={r_forced['rowCount']} forced={r_forced['forcedStateAttr']!r}")

            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            wait_meta(pg)
            pg.locator('.aside input[name="corpus"]').nth(1).check()
            time.sleep(0.6)
            open_view(pg, "chat")
            time.sleep(0.6)
            r_power = pg.evaluate(PROBE)
            PROBES["api_power_warn_1500"] = r_power
            check("页面·api：电力示例库仍标注「示例资料，不是正式规程」（没有因为接了真实资料就升级口径）",
                  "示例资料，不是正式规程" in (r_power["inlineWarn"] or ""),
                  f"inline-warn={r_power['inlineWarn']}")

            print("\n--- api 模式：真实检索 + 生成（本机桩）---")
            r1 = api_ask(pg, Q_TRIAD, top_k=3)
            PROBES["api_ok_1500"] = r1
            e_hits, e_neigh, e_scores = rc_expected(rc.RAG_LEARNING, Q_TRIAD, 3)
            check("页面·api：真实检索命中（id / 顺序 / BM25 分数）与直接调 rag_core 逐条一致",
                  r1["hitIds"] == e_hits and r1["hitScores"] == e_scores,
                  f"页面={r1['hitIds']} rag_core={e_hits}")
            check("页面·api：同节补充与 rag_core 一致，证据行数 = 命中 + 补充",
                  r1["neighborIds"] == e_neigh and r1["rowCount"] == len(e_hits) + len(e_neigh)
                  and f"BM25 Top-3 命中 3 段" in r1["text"],
                  f"补充 {len(r1['neighborIds'])} 段 / 页面共 {r1['rowCount']} 行")
            check("页面·api：回答走的是生成路径（本机桩），引用解析出 3 条并区分「检索命中 / 同节补充」",
                  r1["citeCount"] >= 3 and len(r1["refItems"]) == 3
                  and "检索命中" in r1["chips"] and "同节补充" in r1["chips"],
                  f"引用标记 {r1['citeCount']} 个 / 引用行 {len(r1['refItems'])} 条")
            check("页面·api：回答来源如实写明「本机 API 实时生成…未调用真实模型供应商」",
                  any("本机 API 实时生成" in m and "未调用真实模型供应商" in m
                      for m in r1["answerMetas"]),
                  f"来源行={r1['answerMetas']}")
            # 「NOT VALIDATED」对比说明在设置页的使用提示里：合并两页文本断言
            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            time.sleep(0.4)
            r1set = pg.evaluate(PROBE)
            PROBES["api_ok_set_1500"] = r1set
            open_view(pg, "chat")
            check("页面·api：仍保留「生成层尚未完成正式验证」提示（没有因为接了真实资料就宣称已验证）",
                  "生成层尚未完成正式验证" in (r1["text"] + "\n" + r1set["text"])
                  and "NOT VALIDATED" in (r1["text"] + "\n" + r1set["text"]), "")
            check("页面·api：结果归属写明本次设置（学习库 / 检索并生成 / Top-K 3），且无漂移提示",
                  "RAG 技术学习库" in (r1["resultScope"] or "")
                  and "Top-K 3" in (r1["resultScope"] or "") and r1["staleNotice"] is None,
                  f"scope={r1['resultScope']}")
            pg.screenshot(path=str(out_dir / "v3api-真实检索与生成-1500.png"))
            results["shots"].append("v3api-真实检索与生成-1500.png")

            print("\n--- api 模式：草稿跨页保留（R1 窄修回归，69 号复审）---")
            pg.goto(f"{URL}/", wait_until="load", timeout=60000)
            pg.wait_for_selector(".sidebar", timeout=60000)
            open_view(pg, "batch")
            pg.wait_for_selector("#batch-questions", timeout=60000)
            pg.fill("#batch-questions", "测试问题一（草稿保留）\n测试问题二（草稿保留）")
            pg.select_option("#batch-corpus", label="电力示例库")
            time.sleep(0.4)
            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            time.sleep(0.4)
            open_view(pg, "batch")
            pg.wait_for_selector("#batch-questions", timeout=60000)
            time.sleep(0.4)
            kept_batch = pg.evaluate(
                "() => ({q: document.querySelector('#batch-questions').value,"
                " corpus: document.querySelector('#batch-corpus').selectedOptions[0].text,"
                " count: document.querySelector('#batch-count') ? document.querySelector('#batch-count').innerText : ''})")
            check("页面·api R1：批量草稿与参数在切页往返后保留（问题文本、知识库选择、条数都不丢）",
                  "测试问题一（草稿保留）" in kept_batch["q"]
                  and kept_batch["corpus"] == "电力示例库"
                  and "已读取 2 道问题" in kept_batch["count"],
                  f"corpus={kept_batch['corpus']} count={kept_batch['count'][:24]}")

            open_view(pg, "lib")
            pg.wait_for_selector("#user-library-panel", timeout=60000)
            pg.fill("#user-library-panel textarea", "这是一段尚未提交的资料草稿（R1 回归用）。")
            pg.fill("#user-library-panel input.userlib-input", "R1 草稿标题")
            time.sleep(0.4)
            open_view(pg, "chat")
            time.sleep(0.4)
            open_view(pg, "lib")
            pg.wait_for_selector("#user-library-panel", timeout=60000)
            time.sleep(0.4)
            kept_lib = pg.evaluate(
                "() => ({t: document.querySelector('#user-library-panel textarea').value,"
                " title: document.querySelector('#user-library-panel input.userlib-input').value})")
            check("页面·api R1：资料库导入草稿在切页往返后保留（正文与标题不丢）",
                  "尚未提交的资料草稿" in kept_lib["t"] and kept_lib["title"] == "R1 草稿标题",
                  f"title={kept_lib['title']}")
            open_view(pg, "chat")

            print("\n--- api 模式：仅检索 / 零结果 ---")
            before_stub = iap.stub_count()
            r2 = api_ask(pg, Q_TRIAD, top_k=3, mode="retrieve_only", expect_js=WAIT_ROWS)
            check("页面·api：仅检索模式如实提示不调用模型，且不展示「同节补充」（没生成就没有生成上下文）",
                  r2["notice"] is not None and "仅检索模式" in r2["notice"]["text"]
                  and "同节补充" not in r2["chips"] and len(r2["refItems"]) == 0
                  and "本次没有生成回答：当前是仅检索模式" in r2["text"],
                  f"notice={r2['notice']} chips={r2['chips']}")
            check("页面·api：仅检索模式的命中仍与 rag_core 一致，且没有向生成层发请求（桩计数不变）",
                  r2["hitIds"] == e_hits and iap.stub_count() == before_stub,
                  f"命中 {r2['hitIds']} / 桩 {before_stub} → {iap.stub_count()}")
            r3 = api_ask(pg, Q_ZERO_LOCAL, corpus=1, expect_js=WAIT_TEXT % "未检索到相关资料")
            check("页面·api：电力示例库里的无关问题为零结果：缺口卡如实说明不生成、无证据行、无引用",
                  "未检索到相关资料" in r3["notice"]["text"] and r3["rowCount"] == 0
                  and len(r3["refItems"]) == 0 and "没有生成回答" in r3["text"]
                  and "没有调用模型" in r3["text"],
                  f"rows={r3['rowCount']} notice={r3['notice'] and r3['notice']['text'][:30]}")

            print("\n--- api 模式：Top-K 真改结果 + 设置漂移 ---")
            counts, score_lists = {}, {}
            for k in (1, 3, 5):
                rk = api_ask(pg, Q_TRIAD, top_k=k)
                kh, kn, ks = rc_expected(rc.RAG_LEARNING, Q_TRIAD, k)
                counts[k], score_lists[k] = len(rk["hitIds"]), rk["hitScores"]
                check(f"页面·api Top-K={k}：命中 id / 分数 / 同节补充都与该 Top-K 的 rag_core 结果逐条一致",
                      rk["hitIds"] == kh and rk["hitScores"] == ks and rk["neighborIds"] == kn
                      and len(kh) == k and f"Top-K {k}" in (rk["resultScope"] or ""),
                      f"页面 {rk['hitIds']} / rag_core {kh}")
            check("页面·api 反向对照：Top-1/3/5 的命中数与分数列表两两不同（证明不是只改标签）",
                  counts == {1: 1, 3: 3, 5: 5}
                  and len({tuple(score_lists[k]) for k in (1, 3, 5)}) == 3,
                  f"命中数 {counts}")

            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            wait_meta(pg)
            pg.locator('.aside input[name="corpus"]').nth(1).check()
            time.sleep(0.8)
            open_view(pg, "chat")
            time.sleep(0.8)
            r_drift = pg.evaluate(PROBE)
            PROBES["api_drift_1500"] = r_drift
            check("页面·api：真实接口下设置漂移契约仍然成立——旧结果不被新设置重新归属",
                  r_drift["staleNotice"] is not None and "知识库 → 电力示例库" in r_drift["staleNotice"]
                  and "RAG 技术学习库" in (r_drift["resultScope"] or "")
                  and "与当前设置不一致" in (r_drift["resultScope"] or "")
                  and all(i.startswith("air-") for i in r_drift["hitIds"]),
                  f"stale={r_drift['staleNotice']}")
            pg.screenshot(path=str(out_dir / "v3api-设置漂移后结果归属.png"))
            results["shots"].append("v3api-设置漂移后结果归属.png")

            print("\n--- api 模式：视觉不回退（真实资料下 1500 / 1280）---")
            for w in WIDTHS:
                pg.set_viewport_size({"width": w, "height": HEIGHT})
                rw = api_ask(pg, Q_TRIAD, top_k=3)
                PROBES[f"api_ok_{w}"] = rw
                a, m = rw["sidebar"], rw["main"]
                check(f"页面·api-{w} 视觉① 侧栏在左、内容列居中、宽度比符合新设计、内容不出界",
                      a and m and m["left"] > a["right"] and 240 <= a["width"] <= 300
                      and m["width"] / a["width"] >= 2.5 and not rw["hOverflow"]
                      and rw["contentMaxRight"] <= rw["clientWidth"] + 1,
                      f"侧栏宽={a and a['width']} 比值={m and round(m['width'] / a['width'], 2)} "
                      f"横向溢出={rw['hOverflow']}")
                # 70 号复审 R4：可见性按 chat-scroll 的可见区判定（上沿/下沿都落在其中），
                # 再用 elementFromPoint 查是否被遮挡；只比视口高度会把被滚动容器裁掉的元素误判为可见。
                check(f"页面·api-{w} 视觉② 首屏能看到「检索证据」披露条（默认折叠、在可见对话区内、未被遮挡）",
                      rw["chatClip"] and rw["evidenceTitle"]
                      and rw["evidenceTitle"]["top"] >= rw["chatClip"]["top"] - 1
                      and rw["evidenceTitle"]["bottom"] <= rw["chatClip"]["bottom"] + 1
                      and rw["evidenceCovered"] is False
                      and rw["discOpen"] is False
                      and "生成层尚未完成正式验证" in rw["text"],
                      f"披露条 top={rw['evidenceTitle'] and rw['evidenceTitle']['top']} "
                      f"bottom={rw['evidenceTitle'] and rw['evidenceTitle']['bottom']}"
                      f"（chat-scroll 可见区 {rw['chatClip'] and rw['chatClip']['top']}"
                      f"~{rw['chatClip'] and rw['chatClip']['bottom']}） "
                      f"遮挡={rw['evidenceCovered']} 折叠={rw['discOpen']}")
                # 视觉③（2026-09-28 聊天式布局）：与 mock 侧验收⑧同源——api 味是 launcher 实际服务的构建
                check(f"页面·api-{w} 视觉③ 聊天式布局：提问框常驻对话页底部（在内容之下、贴住视口底）",
                      rw["askCard"] and rw["paneChat"]
                      and rw["askCard"]["top"] > rw["answerCard"]["top"]
                      and abs(rw["askCard"]["bottom"] - rw["paneChat"]["bottom"]) <= 28,
                      f"提问框 top={rw['askCard'] and rw['askCard']['top']}"
                      f"（回答卡 top={rw['answerCard']['top']}），"
                      f"提问框 bottom={rw['askCard'] and rw['askCard']['bottom']}"
                      f"（视口底={rw['paneChat'] and rw['paneChat']['bottom']}）")
                pg.screenshot(path=str(out_dir / f"v3api-真实资料-{w}-首屏.png"))
                results["shots"].append(f"v3api-真实资料-{w}-首屏.png")
            pg.set_viewport_size({"width": WIDTHS[0], "height": HEIGHT})

            print("\n--- api 模式：桩失败 / 无 Key / API 不可达 ---")
            r4 = api_ask(pg, Q_TRIAD, top_k=3)
            errors_mark = len(console_errors)
            iap.kill(api_proc)
            api_proc = None
            pg.fill("#question", Q_GROUND)
            pg.get_by_role("button", name="开始检索").click()
            pg.wait_for_selector("#transport-notice", timeout=60000)
            time.sleep(0.5)
            r_down = pg.evaluate(PROBE)
            PROBES["api_unreachable_1500"] = r_down
            check("页面·api：本机 API 不可达时明确说「连接不上本机 API」，与「模型调用失败」分开表达",
                  "连接不上本机 API" in (r_down["transportNotice"] or "")
                  and "模型调用失败" not in (r_down["transportNotice"] or "")
                  and "本次请求没有拿到任何结果" in (r_down["transportNotice"] or ""),
                  f"transport={r_down['transportNotice']}")
            check("页面·api：不可达时保留上一次运行的结果，并写明它仍是上一次的（不冒充本次）",
                  r_down["rowCount"] > 0 and "仍是上一次运行的结果" in (r_down["transportNotice"] or "")
                  and r_down["rowCount"] == r4["rowCount"],
                  f"证据行 {r_down['rowCount']}（上一次 {r4['rowCount']}）")
            pg.screenshot(path=str(out_dir / "v3api-API不可达.png"))
            results["shots"].append("v3api-API不可达.png")
            api_proc = iap.start_api(True, out_dir / "api_v3_key2.log", cors_origins=cors_origins)
            api_starts += 1

            iap.kill(stub_proc)
            stub_proc = iap.start_stub("fail", out_dir / "llm_stub_requests_fail.jsonl")
            r5 = api_ask(pg, Q_TRIAD, top_k=3, expect_js=WAIT_TEXT % "模型调用失败")
            PROBES["api_generation_failed_1500"] = r5
            check("页面·api：生成层失败时提示「模型调用失败」并保留全部真实证据，但没有回答与引用",
                  "模型调用失败" in r5["notice"]["text"] and "notice--error" in r5["notice"]["cls"]
                  and r5["rowCount"] == len(e_hits) + len(e_neigh) and len(r5["refItems"]) == 0
                  and r5["citeCount"] == 0 and "本次没有生成回答" in r5["text"],
                  f"rows={r5['rowCount']} refs={len(r5['refItems'])}")
            pg.screenshot(path=str(out_dir / "v3api-生成失败.png"))
            results["shots"].append("v3api-生成失败.png")
            iap.kill(stub_proc)
            stub_proc = iap.start_stub("ok", out_dir / "llm_stub_requests_after_fail.jsonl")

            iap.kill(api_proc)
            api_proc = iap.start_api(False, out_dir / "api_v3_no_key.log", cors_origins=cors_origins)
            api_starts += 1
            before_stub = iap.stub_count()
            r6 = api_ask(pg, Q_TRIAD, top_k=3, expect_js=WAIT_TEXT % "无 API Key")
            PROBES["api_no_key_1500"] = r6
            # Key 状态框在「设置」页：过去补抓一份（页面 meta 是本次刷新的，未检测到如实反映）
            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            time.sleep(0.4)
            r6set = pg.evaluate(PROBE)
            PROBES["api_no_key_set_1500"] = r6set
            open_view(pg, "chat")
            check("页面·api：无 Key 时如实提示「无 API Key：仅展示检索结果」，Key 状态显示未检测到",
                  "无 API Key" in r6["notice"]["text"] and "未检测到" in (r6set["keyboxText"] or "")
                  and "keybox--warn" in (r6set["keyboxClass"] or ""),
                  f"keybox={r6set['keyboxText']}")
            check("页面·api：无 Key 时真实检索结果完整保留，且没有生成上下文与引用（不伪造回答）",
                  r6["hitIds"] == e_hits and r6["rowCount"] == len(e_hits) and len(r6["refItems"]) == 0
                  and "同节补充" not in r6["chips"]
                  and "本次没有生成回答：未检测到服务端 API Key" in r6["text"],
                  f"命中 {r6['hitIds']} / 证据行 {r6['rowCount']}")
            check("页面·api：无 Key 时一次生成请求都没发（桩计数不变）",
                  iap.stub_count() == before_stub, f"{before_stub} → {iap.stub_count()}")
            pg.screenshot(path=str(out_dir / "v3api-无Key.png"))
            results["shots"].append("v3api-无Key.png")

            iap.kill(api_proc)
            api_proc = None
            pg.goto(f"{URL}/", wait_until="load", timeout=60000)
            pg.wait_for_selector(".topbar", timeout=60000)
            # 浏览器要过一会儿才把"连接被拒绝"暴露给 fetch（实测约 2.5 s），必须等状态落定再断言
            try:
                pg.wait_for_function(
                    "!!document.querySelector('.topbar .tag')"
                    " && document.querySelector('.topbar .tag').innerText.includes('不可达')",
                    timeout=30000)
            except Exception:                                  # noqa: BLE001
                pass
            time.sleep(0.4)
            # Key 状态框在「设置」页：导航过去抓，再回对话页提问
            open_view(pg, "set")
            pg.wait_for_selector(".aside", timeout=60000)
            time.sleep(0.4)
            r7 = pg.evaluate(PROBE)
            PROBES["api_meta_unreachable_1500"] = r7
            check("页面·api：接口不可达时顶栏如实写「本机 API 不可达」，不退回成「示例数据」也不假装已接入",
                  "本机 API 不可达" in (r7["tagText"] or "")
                  and "本地示例数据" not in (r7["tagText"] or "")
                  and "已接入本机 API" not in (r7["tagText"] or ""),
                  f"tag={r7['tagText']}")
            check("页面·api：接口不可达时 Key 状态框不留空白，如实写「本机 API 不可达，状态未知」",
                  "本机 API 不可达，状态未知" in (r7["keyboxText"] or "")
                  and "未接入服务端" not in r7["text"],
                  f"keybox={r7['keyboxText']!r}")
            open_view(pg, "chat")
            pg.fill("#question", Q_TRIAD)
            pg.get_by_role("button", name="开始检索").click()
            pg.wait_for_selector("#transport-notice", timeout=60000)
            time.sleep(0.4)
            r8 = pg.evaluate(PROBE)
            check("页面·api：接口不可达时提问也不会静默失败（明确提示本次没有任何结果）",
                  "本次请求没有拿到任何结果" in (r8["transportNotice"] or "")
                  and r8["rowCount"] == 0,
                  f"transport={r8['transportNotice']}")

            new_errors = console_errors[errors_mark:]
            check("页面·api：不可达期间的浏览器报错只有连接失败（没有别的脚本错误混进来）",
                  all(("ERR_CONNECTION_REFUSED" in e) or ("Failed to load resource" in e)
                      or ("Failed to fetch" in e) for e in new_errors),
                  f"{new_errors[:3]}")
            check("页面·api：除「API 不可达」那一段外，全程无 pageerror、无 console error",
                  not page_errors and not console_errors[:errors_mark],
                  f"pageerror={page_errors[:2]} console={console_errors[:errors_mark][:2]}")

            ctx.close()
            browser.close()
    except Exception as exc:                                   # noqa: BLE001
        FAILED.append(f"api 模式验收中途异常：{type(exc).__name__}: {exc}")
        print(f"[FAIL] api 模式验收中途异常：{type(exc).__name__}: {exc}", flush=True)
    finally:
        iap.kill(api_proc)
        iap.kill(stub_proc)
        if preview is not None:
            stop_tree(preview)
        for h in iap.HANDLES:
            try:
                h.close()
            except Exception:                                  # noqa: BLE001
                pass

    results["probes"] = PROBES
    results["passed"], results["failed"] = list(PASSED), list(FAILED)
    results["api_process_starts"] = api_starts
    results["phases"] = [{"name": "stub_ok_with_key", "stub_requests": len(iap.stub_entries(stub_log))},
                         {"name": "stub_fail", "stub_requests": len(iap.stub_entries(
                             out_dir / "llm_stub_requests_fail.jsonl"))},
                         {"name": "after_fail_and_no_key", "stub_requests": len(iap.stub_entries(
                             out_dir / "llm_stub_requests_after_fail.jsonl"))}]
    (out_dir / "frontend_v3_api_probe.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "-" * 78)
    print(f"页面·api/真实资料联调 PASS {len(PASSED)} 项；FAIL {len(FAILED)} 项。"
          f"截图 {len(results['shots'])} 张 + 探针 json，写入：{out_dir}")
    return 1 if FAILED else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--backend", choices=("mock", "api"), default="mock",
                    help="mock=第一阶段回归（默认）；api=第二阶段 A 真实资料联调")
    ap.add_argument("--no-build", action="store_true", help="跳过 mock 构建；api 模式不支持，必须按隔离端口重建")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else Path(
        tempfile.mkdtemp(prefix=f"frontend_v3_{args.backend}_shots_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.backend == "api":
        return run_api_backend(out_dir, args.no_build)

    if not args.no_build:
        # 只重建、不静默改判据：mock 判据集合 = 31/32 号 39 项 + 69 号 R1 窄修新增 3 项
        # （CE-A2a / CE-B5 / CE-C）+ v1.2 聊天式布局验收⑧，v1.3 对话流改版计数不变
        # （判据对象更新，见改动声明 v1.3；70 号 R4 收紧的是判据强度，不加项），
        # 独立复核者复跑应当是 43 PASS / 0 FAIL
        b = build_frontend("mock")
        print(f"[INFO] mock 构建完成：JS {b['js_files']} 共 {b['js_bytes']} 字节；"
              f"示例数据标志文案={b['marker_ok']}", flush=True)

    from playwright.sync_api import sync_playwright

    proc = start_preview()
    results: dict = {"widths": list(WIDTHS), "states": {}, "shots": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
            pg = ctx.new_page()
            page_errors, console_errors = [], []
            pg.on("pageerror", lambda e: page_errors.append(str(e)))
            pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

            print("\n--- 30 号复审反例：真实交互（不带 ?state=）---")
            interactive_counterexamples(pg, out_dir, results)

            for state in STATES:
                print(f"\n--- state={state} ---")
                for w in WIDTHS:
                    pg.set_viewport_size({"width": w, "height": HEIGHT})
                    pg.goto(f"{URL}/?state={state}", wait_until="load", timeout=60000)
                    pg.wait_for_selector(".sidebar", timeout=60000)
                    time.sleep(0.8)
                    r = pg.evaluate(PROBE)
                    PROBES[f"{state}_{w}"] = r
                    pg.screenshot(path=str(out_dir / f"v3-{state}-{w}-首屏.png"))
                    results["shots"].append(f"v3-{state}-{w}-首屏.png")
                # 设置页探针（1500 宽）：keybox / 控件唯一性判据的取材页——
                # 2026-09-28 重设计后设置控件在独立「设置」页，这里每状态补抓一份
                pg.set_viewport_size({"width": WIDTHS[0], "height": HEIGHT})
                pg.goto(f"{URL}/?state={state}", wait_until="load", timeout=60000)
                pg.wait_for_selector(".sidebar", timeout=60000)
                open_view(pg, "set")
                pg.wait_for_selector(".aside", timeout=60000)
                time.sleep(0.6)
                PROBES[f"set_{state}"] = pg.evaluate(PROBE)
                # 全页与展开只在 ok 状态做，其余状态用首屏即可
                if state == "ok":
                    for w in WIDTHS:
                        pg.set_viewport_size({"width": w, "height": HEIGHT})
                        pg.goto(f"{URL}/?state=ok", wait_until="load", timeout=60000)
                        pg.wait_for_selector("#result-scope", timeout=60000)
                        pg.click("#evidence-card > summary")   # v1.3：证据默认折叠，展开后全页截图
                        pg.wait_for_selector(".evidence-row", timeout=60000)
                        time.sleep(0.6)
                        pg.screenshot(path=str(out_dir / f"v3-ok-{w}-全页.png"), full_page=True)
                        results["shots"].append(f"v3-ok-{w}-全页.png")
                    pg.set_viewport_size({"width": WIDTHS[0], "height": HEIGHT})
                    pg.goto(f"{URL}/?state=ok", wait_until="load", timeout=60000)
                    pg.wait_for_selector("#result-scope", timeout=60000)
                    pg.click("#evidence-card > summary")   # v1.3：先展开披露框再展开行
                    pg.wait_for_selector(".evidence-row", timeout=60000)
                    pg.locator(".evidence-row__head").first.click()
                    time.sleep(0.6)
                    expanded = pg.evaluate(
                        "() => ({open: !!document.querySelector('.evidence-row.is-open'),"
                        " nested: document.querySelectorAll('details details').length,"
                        " bodyLen: (document.querySelector('.evidence-row__body')"
                        " ? document.querySelector('.evidence-row__body').innerText.length : 0)})")
                    check("V3 证据片段可展开：展开后正文可见、展开态下仍无嵌套折叠",
                          expanded["open"] and expanded["bodyLen"] > 80 and expanded["nested"] == 0,
                          f"open={expanded['open']} 正文 {expanded['bodyLen']} 字符 "
                          f"nested={expanded['nested']}")
                    pg.screenshot(path=str(out_dir / "v3-ok-1500-展开证据.png"))
                    results["shots"].append("v3-ok-1500-展开证据.png")
                results["states"][state] = PROBES[f"{state}_{WIDTHS[0]}"]

            check("V3 全程无 pageerror、无 console error",
                  not page_errors and not console_errors,
                  f"pageerror={page_errors[:2]} console={console_errors[:2]}")
            ctx.close()
            browser.close()
    finally:
        stop_tree(proc)

    # 先落盘探针：断言哪怕中途失败，也留着现场供排查（这里踩过一次）
    results["probes"] = PROBES
    results["passed"], results["failed"] = list(PASSED), list(FAILED)
    (out_dir / "frontend_v3_probe.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 28 号「视觉验收」七条（判据随 2026-09-28 Open WebUI 重设计更新） ----------------
    for w in WIDTHS:
        a, m = PROBES[f"initial_{w}"]["sidebar"], PROBES[f"initial_{w}"]["main"]
        check(f"V3-{w} 验收① 侧栏在左侧，内容列居中且不出界，宽度比符合新设计",
              a and m and m["left"] > a["right"]
              and PROBES[f"initial_{w}"]["contentMaxRight"] <= PROBES[f"initial_{w}"]["clientWidth"] + 1
              and 240 <= a["width"] <= 300 and m["width"] / a["width"] >= 2.5,
              f"侧栏宽={a and a['width']} / 主内容宽={m and m['width']} "
              f"比值={m and round(m['width'] / a['width'], 2)}")

        r = PROBES[f"ok_{w}"]
        # 验收②（2026-09-29 v1.3 对话流改版）：用户气泡在右上、回答消息在左下，
        # 引用对照收在回答消息内部（#ref-card 含于 #answer-card），纵向堆叠不重叠
        check(f"V3-{w} 验收② 对话流：用户气泡在右上、回答消息在左下，引用对照收在消息内",
              r["msgUserBubble"] and r["answerCard"] and r["refCard"]
              and r["msgUserBubble"]["bottom"] <= r["answerCard"]["top"] + 2
              and r["msgUserBubble"]["right"] > r["answerCard"]["left"] + 200
              and r["answerCard"]["top"] <= r["refCard"]["top"] < r["answerCard"]["bottom"],
              f"气泡 bottom={r['msgUserBubble'] and r['msgUserBubble']['bottom']} right={r['msgUserBubble'] and r['msgUserBubble']['right']}"
              f"（消息 top={r['answerCard'] and r['answerCard']['top']} left={r['answerCard'] and r['answerCard']['left']}），"
              f"引用 top={r['refCard'] and r['refCard']['top']}（消息 bottom={r['answerCard'] and r['answerCard']['bottom']}）")

        # 验收③（v1.3；70 号复审 R4 收紧）：证据默认折叠，且披露条真实首屏可见——
        # 上沿/下沿都落在 chat-scroll 可见区内，并用 elementFromPoint 查遮挡；
        # 只比视口高度（top < HEIGHT）会被滚动容器裁掉也判绿。
        check(f"V3-{w} 验收③ 首屏能看到「检索证据」披露条（默认折叠、在可见对话区内、未被遮挡）",
              r["chatClip"] and r["evidenceTitle"]
              and r["evidenceTitle"]["top"] >= r["chatClip"]["top"] - 1
              and r["evidenceTitle"]["bottom"] <= r["chatClip"]["bottom"] + 1
              and r["evidenceCovered"] is False
              and r["discOpen"] is False
              and "生成层尚未完成正式验证" in r["text"],
              f"披露条 top={r['evidenceTitle'] and r['evidenceTitle']['top']} "
              f"bottom={r['evidenceTitle'] and r['evidenceTitle']['bottom']}"
              f"（chat-scroll 可见区 {r['chatClip'] and r['chatClip']['top']}"
              f"~{r['chatClip'] and r['chatClip']['bottom']}） "
              f"遮挡={r['evidenceCovered']} 折叠={r['discOpen']}")

        check(f"V3-{w} 验收④ 无横向滚动条、内容不出界、输入条参数行完整",
              not r["hOverflow"] and r["contentMaxRight"] <= r["clientWidth"] + 1
              and r["askCard"] is not None and r["askCard"]["height"] >= 90,
              f"scrollWidth={r['scrollWidth']} clientWidth={r['clientWidth']} "
              f"内容最右={r['contentMaxRight']} 输入条高={r['askCard'] and r['askCard']['height']}")

    # 验收⑧（2026-09-28 聊天式布局，用户要求）：提问框常驻对话页底部——
    # 在内容之下、贴住 #pane-chat 底部，不随内容卷走。
    dock = PROBES[f"ok_{WIDTHS[0]}"]
    check("V3 验收⑧ 聊天式布局：提问框常驻对话页底部（在内容之下、贴住视口底）",
          dock["askCard"] and dock["paneChat"]
          and dock["askCard"]["top"] > dock["answerCard"]["top"]
          and abs(dock["askCard"]["bottom"] - dock["paneChat"]["bottom"]) <= 28,
          f"提问框 top={dock['askCard'] and dock['askCard']['top']}（回答卡 top={dock['answerCard']['top']}），"
          f"提问框 bottom={dock['askCard'] and dock['askCard']['bottom']}"
          f"（视口底={dock['paneChat'] and dock['paneChat']['bottom']}）")

    ok = PROBES[f"ok_{WIDTHS[0]}"]["computed"]
    check("V3 验收⑤ 视觉基调与新设计一致（Open WebUI 系：8px 圆角 / 白卡 / 扁平无阴影 / 标题层级）",
          ok["cardRadius"] == "8px" and ok["cardBg"] == "rgb(255, 255, 255)"
          and ok["cardShadow"] == "none" and ok["titleSize"] == "15px"
          and ok["panelTitleSize"] == "13.5px",
          f"圆角={ok['cardRadius']} 卡片底={ok['cardBg']} 阴影={ok['cardShadow'][:28]} "
          f"顶栏标题={ok['titleSize']} 消息名={ok['panelTitleSize']}")

    nr = PROBES[f"no-refs_{WIDTHS[0]}"]
    # 验收⑥（v1.3）：无步骤条后，改查引用对照区的诚实说明——无引用时如实说明且不渲染引用药丸
    check("V3 验收⑥ 无可用引用时引用对照如实说明（不渲染引用药丸、不假装完成对照）",
          len(nr["refItems"]) == 0 and "本次回答没有可用引用" in nr["text"]
          and "引用检查不通过" in nr["text"],
          f"引用行={len(nr['refItems'])}")

    # 页面里**故意**有一句「尚未验证（NOT VALIDATED）」不等于「验证失败」的对比说明，
    # 断言时先把它剔除，再检查有没有别处把未验证误写成失败。
    # 2026-09-28 重设计：使用提示在「设置」页，这里取对话页 + 设置页的合并文本。
    contrast_lines = [
        '「尚未验证（NOT VALIDATED）」不等于「验证失败」，两者分开表达。',
    ]
    text = PROBES[f"ok_{WIDTHS[0]}"]["text"] + "\n" + PROBES[f"set_ok"]["text"]
    safe = text
    for s in contrast_lines:
        safe = safe.replace(s, '')
    check("V3 验收⑦ 「尚未验证」与「验证失败」分开表达（不得把 NOT VALIDATED 说成失败）",
          "NOT VALIDATED" in text and "尚未验证" in text and "不是已执行且失败" in text
          and "未通过" not in safe and "验证失败" not in safe,
          f"剔除明确对比句后：未通过={'未通过' in safe} 验证失败={'验证失败' in safe}")

    # ---------------- 七种状态各自的页面事实 ----------------
    t_init = PROBES[f"initial_{WIDTHS[0]}"]
    check("V3 状态「初始待提问」：空状态问候可见、无状态提示、提示还没有提问",
          t_init["notice"] is None and t_init["helloVisible"]
          and "还没有提问" in t_init["text"] and t_init["rowCount"] == 0)

    t_load = PROBES[f"loading_{WIDTHS[0]}"]
    check("V3 状态「检索中」：助手消息内出现进行中提示与骨架、且没有回答与证据",
          t_load["notice"] is not None and "正在检索" in t_load["notice"]["text"]
          and t_load["hasSkeleton"] and t_load["rowCount"] == 0)

    t_ok = PROBES[f"ok_{WIDTHS[0]}"]
    check("V3 状态「有引用」：回答渲染（含加粗与引用标记）、引用 5 条、证据 3+7 行、归属行写明 Top-K 3",
          t_ok["strongCount"] > 0 and t_ok["citeCount"] >= 5 and len(t_ok["refItems"]) == 5
          and t_ok["rowCount"] == 10
          and "air-evaluation#s2p2" in t_ok["text"] and "命中 3" in t_ok["text"]
          and "Top-K 3" in (t_ok["resultScope"] or ""),
          f"加粗 {t_ok['strongCount']} 处 / 引用标记 {t_ok['citeCount']} 个 / 引用行 "
          f"{len(t_ok['refItems'])} 条 / 证据行 {t_ok['rowCount']} 条")
    check("V3 状态「有引用」：命中与补充两类徽标都出现，补充明确不计入指标",
          "检索命中" in t_ok["chips"] and "同节补充" in t_ok["chips"]
          and "不计入 Hit@K / MRR" in t_ok["text"])

    t_nr = PROBES[f"no-refs_{WIDTHS[0]}"]
    check("V3 状态「无可用引用」：回答仍在、引用区如实说明、证据仍保留",
          "资料中未找到相关依据" in t_nr["text"] and t_nr["rowCount"] == 11
          and len(t_nr["refItems"]) == 0,
          f"证据行 {t_nr['rowCount']} 条（3 命中 + 8 补充）")

    t_zero = PROBES[f"zero_{WIDTHS[0]}"]
    check("V3 状态「零检索结果」：缺口卡如实说明不生成、无回答、无证据行、无引用",
          t_zero["notice"] is not None and "未检索到相关资料" in t_zero["notice"]["text"]
          and t_zero["rowCount"] == 0 and len(t_zero["refItems"]) == 0
          and "没有生成回答" in t_zero["text"])

    t_nk = PROBES[f"no-key_{WIDTHS[0]}"]
    t_nk_set = PROBES[f"set_no-key"]
    check("V3 状态「无 API Key」：提示只展示检索结果、设置页 Key 状态为未检测到、证据保留",
          t_nk["notice"] is not None and "无 API Key" in t_nk["notice"]["text"]
          and "keybox--warn" in (t_nk_set["keyboxClass"] or "")
          and "未检测到" in (t_nk_set["keyboxText"] or "") and t_nk["rowCount"] == 3
          and "未检测到服务端 API Key" in t_nk["text"],
          f"keybox={t_nk_set['keyboxText']}")

    t_fail = PROBES[f"failure_{WIDTHS[0]}"]
    check("V3 状态「模型调用失败」：错误提示清楚、证据仍保留、没有伪造回答与引用",
          t_fail["notice"] is not None and "notice--error" in t_fail["notice"]["cls"]
          and t_fail["rowCount"] == 10 and len(t_fail["refItems"]) == 0
          and t_fail["citeCount"] == 0 and "本次没有生成回答" in t_fail["text"]
          and "模型调用失败" in t_fail["notice"]["text"],
          f"证据行 {t_fail['rowCount']} / 引用行 {len(t_fail['refItems'])} / "
          f"正文引用标记 {t_fail['citeCount']}")

    # ---------------- 控件唯一 / 无嵌套折叠 / 如实标注 ----------------
    # 控件唯一性在「设置」页核对（重设计后设置控件集中在该页，仍只有一套）；
    # v1.3 对话流：输入条新增 2 个 select（知识库/Top-K，与设置页同源）常驻挂载
    t_ok_set = PROBES[f"set_ok"]
    check("V3 设置页控件唯一：.aside 内仍只有一套（1 select + 4 radio），输入条 2 个 select 与其同源",
          t_ok_set["selectInAside"] == 1
          and t_ok_set["radioInAside"] == 4
          and t_ok_set["selectCount"] >= 3,
          f"select 总数={t_ok_set['selectCount']}（栏内 {t_ok_set['selectInAside']}）"
          f" radio（栏内 {t_ok_set['radioInAside']}）")
    check("V3 无嵌套折叠（含状态栏的「更多信息」）",
          t_ok["nestedDetails"] == 0, f"details={t_ok['detailsCount']} nested={t_ok['nestedDetails']}")
    check("V3 第一阶段如实标注：顶栏标注本地示例数据、回答区说明本轮未调用模型、"
          "且不假装已检测到 API Key",
          "本地示例数据" in (t_ok["tagText"] or "") and "本轮未调用任何模型" in t_ok["text"]
          and "未接入服务端，状态未知" in (t_ok_set["keyboxText"] or ""),
          f"标签={t_ok['tagText']} / Key 状态={t_ok_set['keyboxText']}")

    results["probes"] = PROBES
    results["passed"], results["failed"] = list(PASSED), list(FAILED)
    (out_dir / "frontend_v3_probe.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "-" * 78)

    print(f"mock 回归（第一阶段，数据源=本地示例数据）PASS {len(PASSED)} 项；FAIL {len(FAILED)} 项。")
    print(f"截图 {len(results['shots'])} 张 + 探针 json，写入：{out_dir}")
    print("注：api 模式的判据单独统计，请另跑 --backend api（两者计数不合并、不互相覆盖）。")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
