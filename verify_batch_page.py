# -*- coding: utf-8 -*-
"""批量提问页面的真浏览器验收（47 号方案 §7 A 的页面部分）—— verify_batch_page.py

它为什么不并进 verify_frontend_v3.py：47 号方案要求"新功能新增独立套件，不得篡改既有计数"，
所以批量面板的交互判据单独一套，**既有 mock 39 / api 35 的判据一个字都不动**（它们仍会跑，
用来证明新面板没有破坏旧页面）。

判据（全部来自 47 号方案 A1/A4）：
  1. 面板是**独立入口**（不在单题结果卡片里），api 模式下可用、不显示"不可用"；
  2. 粘贴多行 → 问题数、摘要（知识库/模式/问题数/Top-K/预计调用数）都按服务端口径显示；
  3. 导入 .txt 与带 BOM 的 .csv（question 列）成功；缺 question 列的 CSV **在发请求前**就被拒；
  4. 21 条超上限、重复问题保留，都在页面上如实显示；
  5. 仅检索模式跑完：进度 N/N、逐题"完成"、**桩计数 0**（零模型请求）；
  6. 生成模式：二次确认弹窗里写明"预计最多调用模型 N 次"，跑完状态词正确（拒答=未找到资料依据）；
  7. 导出链接指向 csv/docx（服务端下载），页面明说"导出不新增模型调用"并且不出现"正确率"口径；
  8. 取消：终态"已取消"，后续条目显示"未执行"；
  9. 刷新页面后凭 job id 恢复本次进度；
 10. 「未执行」「失败」「拒答」「完成」四种状态词同屏可区分。

运行：
    python verify_batch_page.py [--out <证据目录>] [--no-build]
**全程零真实模型请求**：api_v3 的生成目标指向 tools/batch_stub_server.py。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
FRONTEND = REPO / "frontend_v3"
URL = "http://127.0.0.1:5273"
API_PORT = 5290
STUB_PORT = 5291
API_BASE = f"http://127.0.0.1:{API_PORT}"
STUB_BASE = f"http://127.0.0.1:{STUB_PORT}/v1"
SENTINEL_KEY = "sk-BATCH-PAGE-SENTINEL-DO-NOT-LEAK-3d90"
STUB_STATE = f"http://127.0.0.1:{STUB_PORT}/__batch_stub__/state"
STUB_RESET = f"http://127.0.0.1:{STUB_PORT}/__batch_stub__/reset"
DIALOGS: list = []
PROBES: dict = {}

PASSED: list = []
FAILED: list = []
PROCESSES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def http_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def stub_state() -> dict:
    return http_json(STUB_STATE)


def stub_reset(script: str = "", delay: float | None = None) -> None:
    params: dict = {}
    if script:
        params["script"] = script
    if delay is not None:
        params["delay"] = str(delay)
    http_json(STUB_RESET + ("?" + urllib.parse.urlencode(params) if params else ""))


def stop_process(proc) -> None:
    if proc is None:
        return
    if isinstance(proc, subprocess.Popen):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:                                          # noqa: BLE001
            proc.kill()


def wait_http(url: str, tries: int = 120) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.25)
    return False


def build_frontend() -> dict:
    """按 api 数据源重新构建，并核对产物里确实带了 api 模式的标志文案。

    为什么必须构建后再验收：preview 服务的是 dist/，端上留着别的数据源的旧构建就会"验了个寂寞"。
    """
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("找不到 npm，无法构建前端")
    env = dict(os.environ)
    env["VITE_DATA_SOURCE"] = "api"
    env["VITE_API_BASE"] = API_BASE
    proc = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"npm run build 失败：{(proc.stdout or '')[-800:]}")
    assets = sorted((FRONTEND / "dist" / "assets").glob("*.js"))
    blob = "".join(p.read_text(encoding="utf-8", errors="replace") for p in assets)
    return {"js_files": [p.name for p in assets],
            "api_marker": "已接入本机 API" in blob,
            "batch_marker": "批量提问" in blob,
            "mock_marker_present": "第一阶段：本地示例数据" in blob}


def start_preview() -> subprocess.Popen:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not (FRONTEND / "dist" / "index.html").exists():
        raise RuntimeError("frontend_v3/dist 不存在，请先构建")
    proc = subprocess.Popen([npm, "run", "preview"], cwd=str(FRONTEND),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCESSES.append(proc)
    if not wait_http(URL + "/"):
        raise RuntimeError("预览服务没有起来")
    return proc


def start_api(with_key: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if with_key:
        env["DEEPSEEK_API_KEY"] = SENTINEL_KEY
    else:
        env.pop("DEEPSEEK_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "api_v3.py", "--port", str(API_PORT),
                             "--llm-base-url", STUB_BASE, "--llm-model", "batch-stub"],
                            cwd=str(REPO), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCESSES.append(proc)
    if not wait_http(API_BASE + "/api/health"):
        raise RuntimeError("api_v3 没有起来")
    return proc


def start_stub(script: str = "", delay: float = 0.02, log: Path | None = None) -> subprocess.Popen:
    log = log or (Path(tempfile.gettempdir()) / "rag_batch_page_stub.jsonl")
    proc = subprocess.Popen([sys.executable, "tools/batch_stub_server.py", "--port", str(STUB_PORT),
                             "--log", str(log), "--script", script, "--delay", str(delay)],
                            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCESSES.append(proc)
    if not wait_http(STUB_STATE):
        raise RuntimeError("批量桩没有起来")
    return proc


def norm(text: str) -> str:
    """把页面文本规范化（去掉换行与空白），用于"文案必须写到"这类判据。"""
    return "".join((text or "").split())


def panel_text(page) -> str:
    try:
        return page.inner_text("#batch-panel")
    except Exception:                                              # noqa: BLE001
        return ""


def style_states(page) -> list:
    return [s.strip() for s in page.locator(".batch-item__status").all_inner_texts()]


def wait_state(page, targets, timeout: float = 180.0) -> str:
    """等 #batch-state 文本进入 targets 之一（返回最后的文本，超时也返回，交给断言裁决）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = page.inner_text("#batch-state")
        except Exception:                                          # noqa: BLE001
            last = ""
        if any(t in last for t in targets):
            return last
        page.wait_for_timeout(200)
    return last


def wait_text(page, selector: str, contains: str, timeout: float = 15.0) -> bool:
    """轮询等待某选择器文本包含目标子串：meta 到达、文件解析完成都是异步的。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if contains in page.inner_text(selector):
                return True
        except Exception:                                          # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    return False


def wait_new_job(page, prev_id: str, timeout: float = 30.0) -> str:
    """等结果区的任务短 id 变化 = 新任务已经创建。

    为什么需要它：上一个任务完成时结果区里就是"已完成"，若不先确认换了任务，
    wait_state 会立刻读到**上一个任务**的状态与逐题结果，把"还没跑"误判成"跑完了"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = page.inner_text("#batch-result code")
            if cur and cur != prev_id:
                return cur
        except Exception:                                          # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    return ""


def on_dialog(dialog) -> None:
    DIALOGS.append(dialog.message)
    dialog.accept()


def run_browser_checks(page) -> None:
    page.goto(URL, wait_until="load")
    page.wait_for_selector("#batch-panel", timeout=30000)
    # meta 是异步取的：批量输入区只在拿到 meta.batch 之后才渲染，等到它出现再开始断言，
    # 否则会把"还没加载完"误判成"功能不可用"。
    page.wait_for_selector("#batch-questions", timeout=30000)
    panel = panel_text(page)

    # ---- 1. 独立入口与可用性 ----
    check("页面·批量：面板是独立入口（不在单题结果卡片 #cards-row 里）",
          page.locator("#batch-panel").count() == 1
          and page.locator("#cards-row #batch-panel").count() == 0)
    unavailable = (page.inner_text("#batch-unavailable")
                   if page.locator("#batch-unavailable").count() else "")
    check("页面·批量：api 模式下批量入口可用（不显示不可用文案）",
          page.locator("#batch-unavailable").count() == 0, unavailable[:90])
    check("页面·批量：面板写明硬上限与执行语义（1–20、串行、每题 1 次、不自动重试）",
          "20" in panel and "串行" in panel and "不自动重试" in panel)
    check("页面·批量：面板明确声明不包含 Hit@K / MRR、不当作答案正确率",
          "不包含Hit@K/MRR" in norm(panel) and "不把它当作答案正确率" in norm(panel))

    # ---- 2. 粘贴多行：计数与运行前摘要 ----
    page.fill("#batch-questions", "RAG 评估三元组包含哪三个维度？\n装设接地线的顺序是什么？")
    check("页面·批量：粘贴两行后计数显示 2 道问题",
          wait_text(page, "#batch-count", "已读取 2 道问题"),
          page.inner_text("#batch-count"))
    summary = norm(page.inner_text("#batch-summary"))
    check("页面·批量：运行前摘要含知识库 / 模式 / 问题数 / Top-K / 预计调用数",
          "知识库" in summary and "模式" in summary and "问题数2" in summary
          and "Top-K3" in summary)
    check("页面·批量：仅检索模式的预计模型请求数显示为 0",
          "预计最多模型请求0次" in summary, summary[:80])

    # ---- 3. 导入 TXT ----
    page.set_input_files("#batch-file", files=[{
        "name": "batch_q.txt", "mimeType": "text/plain",
        "buffer": "问题甲？\n\n问题乙？\n问题丙？\n".encode("utf-8"),
    }])
    check("页面·批量：导入 .txt（一行一个问题、忽略空行）→ 3 道问题",
          wait_text(page, "#batch-count", "已读取 3 道问题"),
          page.inner_text("#batch-count"))
    check("页面·批量：导入成功后有如实反馈（文件名 + 条数）",
          wait_text(page, "#batch-notice", "batch_q.txt"))

    # ---- 4/5. 导入 CSV：带 BOM 的正常文件 vs 缺 question 列的坏文件 ----
    csv_ok = "\ufeffquestion,note\n问题一？,a\n问题二？,b\n".encode("utf-8")
    page.set_input_files("#batch-file", files=[{
        "name": "batch_q.csv", "mimeType": "text/csv", "buffer": csv_ok}])
    check("页面·批量：导入带 BOM 的 .csv（有 question 列）→ 2 道问题",
          wait_text(page, "#batch-count", "已读取 2 道问题"),
          page.inner_text("#batch-count"))
    check("页面·批量：CSV 导入反馈里说明读的是 question 列",
          wait_text(page, "#batch-notice", "question 列"))

    csv_bad = "题目,备注\n问题一？,a\n".encode("utf-8")
    page.set_input_files("#batch-file", files=[{
        "name": "batch_bad.csv", "mimeType": "text/csv", "buffer": csv_bad}])
    check("页面·批量：缺 question 列的 CSV 在发出任何请求前就被拒（并指出原因）",
          wait_text(page, "#batch-error", "question 列"), page.inner_text("#batch-error")[:90])
    check("页面·批量：坏文件没有污染输入框（问题数仍是上一次有效的 2 道）",
          "已读取 2 道问题" in page.inner_text("#batch-count"),
          page.inner_text("#batch-count"))

    # ---- 6. 超过 20 条：页面在发请求前就拒绝 ----
    page.fill("#batch-questions", "\n".join(f"第 {i} 问？" for i in range(1, 22)))
    check("页面·批量：21 条超上限时页面立即拒绝（20 条上限，还没点开始）",
          wait_text(page, "#batch-error", "最多 20 道"), page.inner_text("#batch-error")[:90])
    check("页面·批量：超限时开始按钮被禁用（不会带着非法输入发请求）",
          page.locator("#batch-start").is_disabled())
    check("页面·批量：被拒时没有产生任何任务（页面不显示结果区）",
          page.locator("#batch-result").count() == 0)

    # ---- 7. 重复问题保留 ----
    page.fill("#batch-questions", "重复的问题？\n重复的问题？")
    check("页面·批量：重复问题保留（计数 2，不静默去重）",
          wait_text(page, "#batch-count", "已读取 2 道问题"))

    # ---- 8. 仅检索模式跑完：零模型请求 ----
    stub_reset()
    DIALOGS.clear()
    page.click("#batch-start")
    state = wait_state(page, ["已完成", "遇系统性错误已停止", "已取消"])
    check("页面·批量：仅检索模式跑完并显示「已完成」", "已完成" in state, state)
    check("页面·批量：进度显示 2 / 2",
          norm(page.inner_text("#batch-progress")) == "2/2",
          page.inner_text("#batch-progress"))
    check("页面·批量：逐题状态都是「完成」", style_states(page) == ["完成", "完成"],
          str(style_states(page)))
    check("页面·批量：仅检索模式全程桩计数为 0（页面没有偷偷发模型请求）",
          stub_state()["count"] == 0, f"桩计数={stub_state()['count']}")
    check("页面·批量：仅检索模式不弹二次确认（它压根不调模型）", DIALOGS == [], str(DIALOGS))
    check("页面·批量：完成后出现导出 DOCX / CSV 两个下载链接",
          page.locator("#batch-export-docx").count() == 1
          and page.locator("#batch-export-csv").count() == 1)
    href_docx = page.get_attribute("#batch-export-docx", "href") or ""
    href_csv = page.get_attribute("#batch-export-csv", "href") or ""
    check("页面·批量：导出链接指向服务端下载端点（带 job id 与格式）",
          "/api/batch-jobs/" in href_docx and "format=docx" in href_docx
          and "format=csv" in href_csv, f"{href_docx} / {href_csv}")

    # ---- 9. 刷新页面凭 job id 恢复 ----
    short_id = page.inner_text("#batch-result code")
    page.reload(wait_until="load")
    page.wait_for_selector("#batch-result", timeout=30000)
    check("页面·批量：刷新后凭 job id 恢复本次进度（不做历史中心）",
          page.inner_text("#batch-result code") == short_id
          and "已完成" in page.inner_text("#batch-state"),
          f"id={short_id} state={page.inner_text('#batch-state')}")

    # ---- 10. 生成模式：二次确认弹窗 + 拒答状态 ----
    page.fill("#batch-questions", "RAG 评估三元组包含哪三个维度？\n装设接地线的顺序是什么？")
    page.select_option("#batch-mode", "generate")
    check("页面·批量：切到生成模式后摘要显示预计最多模型请求 2 次",
          wait_text(page, "#batch-summary", "预计最多模型请求 2 次"),
          norm(page.inner_text("#batch-summary"))[:90])
    stub_reset(script="refuse", delay=0.02)
    DIALOGS.clear()
    prev_id = page.inner_text("#batch-result code")
    page.click("#batch-start")
    page.wait_for_timeout(600)
    check("页面·批量：生成模式必须二次确认，弹窗写明「预计最多调用模型 2 次」",
          any("预计最多调用模型 2 次" in m for m in DIALOGS), str(DIALOGS)[:160])
    new_id = wait_new_job(page, prev_id)
    check("页面·批量：点到新任务后结果区切换到新任务（不复用上一个任务的结果）",
          bool(new_id), f"{prev_id} → {new_id}")
    state = wait_state(page, ["已完成", "遇系统性错误已停止", "已取消"])
    check("页面·批量：拒答任务终态是「已完成」（拒答也是把这一条跑完了）",
          "已完成" in state, state)
    check("页面·批量：拒答状态如实显示为「未找到资料依据（拒答）」",
          style_states(page) == ["未找到资料依据（拒答）"] * 2, str(style_states(page)))
    stats = norm(page.inner_text("#batch-stats"))
    check("页面·批量：拒答计入「拒答」而不是「完成」",
          "完成：0" in stats and "未找到资料依据（拒答）：2" in stats, stats)

    # ---- 11. 取消：慢桩 + 三题，停在第 1 条之后 ----
    stub_reset(delay=0.7)
    page.fill("#batch-questions", "问题甲？\n问题乙？\n问题丙？")
    DIALOGS.clear()
    prev_id = page.inner_text("#batch-result code")
    page.click("#batch-start")
    wait_new_job(page, prev_id)                     # 先确认换成了新任务
    page.wait_for_timeout(1500)                     # 再让第 1 条真正进入运行中
    running_seen = any("运行中" in s for s in style_states(page))
    page.click("#batch-cancel")
    state = wait_state(page, ["已取消", "已完成", "遇系统性错误已停止"])
    check("页面·批量：点停止之前确实有「运行中」的条目（取消是针对运行中的任务）",
          running_seen, str(style_states(page)))
    check("页面·批量：停止后终态为「已取消」", "已取消" in state, state)
    states = style_states(page)
    check("页面·批量：取消后未执行的条目显示「未执行」（不冒充失败或空答案）",
          any("未执行" in s for s in states) and not any(s == "失败" for s in states),
          str(states))
    check("页面·批量：取消状态下导出链接仍可用（已完成的条目可以导出）",
          page.locator("#batch-export-csv").count() == 1)

    # ---- 12. 状态词口径兜底 ----
    allowed = ("完成", "未找到资料依据", "失败", "未执行", "运行中", "等待")
    seen = set(style_states(page))
    check("页面·批量：出现过的状态词全部落在服务端口径内（完成/拒答/失败/未执行/运行中/等待）",
          bool(seen) and all(any(k in s for k in allowed) for s in seen), str(seen))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="rag_batch_page_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    build_info: dict = {"skipped": bool(args.no_build)}
    errors: list = []

    try:
        start_stub(delay=0.02, log=out_dir / "batch_stub_requests.jsonl")
        start_api(with_key=True)
        if not args.no_build:
            build_info = build_frontend()
            check("页面·批量：构建产物带 api 模式标志且确实包含批量面板（不拿旧构建凑结论）",
                  build_info["api_marker"] and build_info["batch_marker"]
                  and not build_info["mock_marker_present"],
                  json.dumps(build_info, ensure_ascii=False))
        start_preview()

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1500})
            page.on("dialog", on_dialog)
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                run_browser_checks(page)
                page.screenshot(path=str(out_dir / "batch_panel.png"), full_page=True)
            except Exception as exc:                               # noqa: BLE001
                check("页面·批量：验收流程本身没有中途抛异常", False,
                      f"{type(exc).__name__}: {exc}")
                try:
                    page.screenshot(path=str(out_dir / "batch_panel_failure.png"), full_page=True)
                except Exception:                                  # noqa: BLE001
                    pass
            finally:
                browser.close()
    finally:
        check("页面·批量：全程没有 JS 运行时错误", not errors, str(errors[:2]))
        for proc in reversed(PROCESSES):
            stop_process(proc)

    result = {"service": "batch_panel_browser", "passed": len(PASSED), "failed": len(FAILED),
              "passed_names": PASSED, "failed_names": FAILED, "build": build_info,
              "dialogs": DIALOGS, "probes": PROBES}
    (out_dir / "batch_page_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    if FAILED:
        print("FAIL 详情：")
        for name in FAILED:
            print(f"  - {name}")
    print(f"批量页面验收：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    print(f"证据写入：{out_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())



