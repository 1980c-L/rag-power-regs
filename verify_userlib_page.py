# -*- coding: utf-8 -*-
"""「我的资料库」页面的最小真浏览器验收（42 号窄修第 4 项）。

此前 mock 39 / api 35 一字未改，只能证明旧界面回归未坏，不能证明新资料管理路径已验收。
本套件用 Playwright 驱动**真实浏览器 + 真实 api_v3（临时用户库目录、无 Key）**，判据：

  1. 空库时「我的资料库」**不可选**（不出现第三个知识库选项），面板如实显示"还没有资料"；
  2. 通过页面 UI 粘贴导入 → 成功反馈、资料入列表、「我的资料库」选项**即时出现**；
  3. 选中用户库提问 → 真实检索结果渲染为证据行；
  4. 再次导入相同内容 → 反馈清晰写明"未重复导入"（不是"已导入"）；
  5. 上传 2 MB+1 文件 → 反馈清晰给出 file_too_large 拒绝原因（不经页面绕过限额）；
  6. 删除时点「取消」→ **不发 DELETE 请求**、资料仍在；
  7. 删除时点「确定」→ DELETE 请求发出、界面回到空库形态。

计数独立（与 mock 39 / api 35 不合并）。全程零真实模型请求。
运行：python verify_userlib_page.py [--out <证据目录>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

API_PORT = 5280        # dist-api 构建时烧进的 API 地址，必须一致
WEB_PORT = 5273        # CORS 白名单来源
URL = f"http://127.0.0.1:{WEB_PORT}"

PASSED: list = []
FAILED: list = []
HANDLES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def wait_port_free(port: int, deadline_s: int = 15) -> None:
    import socket
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            time.sleep(0.3)      # 端口仍被占用
        except OSError:
            s.close()
            return
    raise RuntimeError(f"端口 {port} 在 {deadline_s}s 内未释放，无法开始验收")


def wait_health(deadline_s: int = 30) -> None:
    import urllib.request
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:                                          # noqa: BLE001
            time.sleep(0.4)
    raise RuntimeError("api_v3 未在预期时间内就绪")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="rag_userlib_page_"))
    os.environ["RAG_USER_LIBRARY_DIR"] = str(tmp / "用户资料")

    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # 若上一轮残留占用端口，先如实失败（避免测到别的进程）
    wait_port_free(API_PORT)
    wait_port_free(WEB_PORT)

    api_log = (tmp / "api_v3_log.txt").open("w", encoding="utf-8")
    HANDLES.append(api_log)
    api_proc = subprocess.Popen([sys.executable, str(REPO / "api_v3.py"), "--port", str(API_PORT)],
                                cwd=str(REPO), env=env, stdout=api_log,
                                stderr=subprocess.STDOUT, text=True)
    web_log = (tmp / "web_log.txt").open("w", encoding="utf-8")
    HANDLES.append(web_log)
    web_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(WEB_PORT), "--bind", "127.0.0.1",
         "--directory", str(REPO / "frontend_v3" / "dist-api")],
        cwd=str(REPO), stdout=web_log, stderr=subprocess.STDOUT, text=True)

    delete_requests: list = []
    dialog_mode = {"action": "dismiss"}     # 删除确认弹窗的行为开关：dismiss / accept
    dialogs_seen: list = []
    try:
        wait_health()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            page.on("request", lambda r: delete_requests.append(r.url)
                    if r.method == "DELETE" else None)

            def on_dialog(d) -> None:
                dialogs_seen.append(d.message)
                if dialog_mode["action"] == "accept":
                    d.accept()
                else:
                    d.dismiss()
            page.on("dialog", on_dialog)

            radio_sel = '.aside input[name="corpus"]'

            # ---- 1. 空库：用户库不可选 ----
            page.goto(f"{URL}/", wait_until="load", timeout=60000)
            page.wait_for_selector(radio_sel, timeout=60000)
            time.sleep(1.0)
            radios = page.locator(radio_sel).count()
            check("页面·空库：只有内置两个知识库选项（我的资料库不可选）", radios == 2, f"radios={radios}")
            check("页面·空库：面板如实显示还没有资料",
                  page.locator("#user-library-panel").inner_text().find("还没有资料") >= 0, "")

            # ---- 2. UI 粘贴导入 → 选项即时出现 ----
            page.fill(".userlib-textarea", "页面验收资料：BM25 结合词频与逆文档频率排序。")
            page.fill('#user-library-panel input[placeholder*="标题"]', "页面验收笔记")
            page.get_by_role("button", name="导入粘贴文本").click()
            page.wait_for_selector(".userlib-msg--ok", timeout=30000)
            page.wait_for_function(
                "document.querySelectorAll('%s').length === 3" % radio_sel, timeout=30000)
            check("页面·导入：粘贴导入成功后，「我的资料库」选项即时出现（无需手动刷新）",
                  page.locator(radio_sel).count() == 3, "")
            check("页面·导入：资料出现在列表里",
                  page.locator(".userlib-item").count() == 1, "")

            # ---- 3. 选中用户库提问 → 真实检索 ----
            page.locator(radio_sel).nth(2).check()
            page.fill("#question", "BM25 怎么排序？")
            page.get_by_role("button", name="开始检索").click()
            page.wait_for_selector(".evidence-row", timeout=45000)
            time.sleep(0.8)
            hit_text = page.locator(".evidence-row").first.inner_text()
            check("页面·检索：选中我的资料库提问，命中真实导入的资料（含来源 id）",
                  "页面验收笔记" in hit_text, hit_text[:80].replace("\n", " "))

            # ---- 4. 重复导入反馈清晰 ----
            page.fill(".userlib-textarea", "页面验收资料：BM25 结合词频与逆文档频率排序。")
            page.fill('#user-library-panel input[placeholder*="标题"]', "换个标题也一样")
            page.get_by_role("button", name="导入粘贴文本").click()
            page.wait_for_function(
                "(() => { const e = document.querySelector('.userlib-msg');"
                " return e && e.innerText.includes('未重复导入'); })()", timeout=30000)
            check("页面·重复：重复内容反馈明确写「未重复导入」",
                  page.locator(".userlib-item").count() == 1, "")

            # ---- 5. 超限文件反馈清晰（2 MB+1，经页面上传路径） ----
            big = tmp / "大文件.txt"
            big.write_bytes(b"0" * (2 * 1024 * 1024 + 1))
            page.set_input_files(".userlib-file", str(big))
            page.wait_for_function(
                "(() => { const e = document.querySelector('.userlib-msg--err');"
                " return e && e.innerText.length > 0; })()", timeout=30000)
            err = page.locator(".userlib-msg--err").inner_text()
            check("页面·超限：2 MB+1 文件被拒并给出清晰的限额原因",
                  "拒绝" in err and "2 MB" in err, err[:100].replace("\n", " "))

            # ---- 6. 取消删除：不发 DELETE ----
            page.locator(".userlib-del").first.click()
            time.sleep(1.5)
            check("页面·取消删除：点「取消」后没有发出任何 DELETE 请求，资料仍在",
                  len(delete_requests) == 0 and page.locator(".userlib-item").count() == 1,
                  f"deletes={len(delete_requests)}")

            # ---- 7. 确认删除：回空库 ----
            dialog_mode["action"] = "accept"
            page.locator(".userlib-del").first.click()
            page.wait_for_function(
                "document.querySelectorAll('%s').length === 2" % radio_sel, timeout=30000)
            page.wait_for_function(
                "(() => { const e = document.querySelector('#user-library-panel');"
                " return e && e.innerText.includes('还没有资料'); })()", timeout=30000)
            check("页面·确认删除：DELETE 已发出，界面回到空库形态（选项消失、显示还没有资料）",
                  len(delete_requests) == 1 and page.locator(radio_sel).count() == 2,
                  f"deletes={len(delete_requests)}")

            browser.close()
    finally:
        for proc in (api_proc, web_proc):
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                proc.terminate()
        for h in HANDLES:
            h.close()

    print("\n===== 汇总 =====", flush=True)
    print(f"PASS {len(PASSED)} / FAIL {len(FAILED)}", flush=True)
    if FAILED:
        for name in FAILED:
            print(f"  FAIL: {name}", flush=True)

    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "verify_userlib_page_result.json").write_text(json.dumps({
            "pass": len(PASSED), "fail": len(FAILED), "failed": FAILED, "passed": PASSED,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
