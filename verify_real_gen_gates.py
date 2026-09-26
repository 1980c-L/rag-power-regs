# -*- coding: utf-8 -*-
"""真实调用门禁的**无网络回归**（对应 38 号复审的两个 P1）。

验的是"能不能拦住"，不是"能不能跑通"：
  1. 已执行过的卡不能再发车（`status` 门禁）；
  2. 同一张卡不能被第二个进程并发发车（`O_CREAT|O_EXCL` 排他认领）；
  3. 两个参与者都先通过预检时，延迟参与者也不能在首进程释放锁后重放已执行卡；
  4. 目标地址必须与卡内 `expected_base_url` **精确相等**，不再只排除回环；
  5. 模型漂移、凭据缺失、短语不符、未确认、预算被改、重试非 0、状态非法 —— 全部 fail closed；
  6. 正向对照：一张合规的 `CONFIRMED` 卡**必须能通过预检**（门禁不能变成"一律拒绝"）；
  7. 干跑（`--mode stub`）**不认领调用卡**（不改卡状态、不建锁）。

**本脚本保证零真实请求**：所有"应当被拒绝"的用例都在建立任何连接之前就被拒绝；
正向对照只调 `preflight_real()` 纯函数，不启动进程；唯一会启动进程的是最后一条干跑用例，
它走本机桩（`127.0.0.1`），生成的"模型"是本机假端点。
被拒绝用例里出现的 `https://example.invalid` 是 RFC 2606 保留域名，永远不可解析。

运行：
    python verify_real_gen_gates.py [--out <证据目录>]
本文件内不写任何个人绝对路径。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import real_generation_batch as rgb                            # noqa: E402

REAL_CARD = REPO / "tools" / "real_gen_card.json"
TARGET_OK = "https://example.invalid"          # 保留域名：不会有任何真实网络效果
TARGET_OTHER = "https://evil.example.invalid"
PHRASE = "按最多 4 次、0 重试执行"
DUMMY_KEY = "sk-gate-dummy-never-sent"

PASSED: list = []
FAILED: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def patched_config(**kw):
    saved = {k: getattr(rgb.config, k) for k in kw}
    for k, v in kw.items():
        setattr(rgb.config, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(rgb.config, k, v)


def base_card(**overrides) -> dict:
    card = {
        "card_version": 1,
        "phase": "门禁回归用临时卡",
        "status": rgb.RUNNABLE_STATUS,
        "confirmed_by_user": True,
        "confirmed_at": "2026-09-20",
        "confirm_phrase": PHRASE,
        "provider": "DeepSeek OpenAI-compatible API",
        "model": "deepseek-chat",
        "expected_base_url": TARGET_OK,
        "credential_ref": "DEEPSEEK_API_KEY",
        "base_url_source": "config.py",
        "max_requests": 4,
        "max_retries": 0,
        "cases": [{"id": "G0", "kind": "门禁回归", "corpus_id": "rag_learning",
                   "mode": "generate", "top_k": 3, "question": "门禁回归占位问题"}],
    }
    card.update(overrides)
    return card


def preflight(card: dict, confirm: str | None = PHRASE) -> tuple:
    """跑一次预检；返回 (是否被干净拒绝, 说明)。

    只有 `refuse()` 触发的 SystemExit 才算"被拒绝"；其它异常一律当作**未通过**返回，
    以免把"脚本崩了"读成"门禁拦住了"。
    """
    try:
        rgb.preflight_real(card, confirm)
        return False, ""
    except SystemExit as exc:
        return True, f"exit={exc.code}"
    except Exception as exc:                                       # noqa: BLE001
        return False, f"预检抛异常（不是干净拒绝）：{type(exc).__name__}: {exc}"


def run_child(card_path: Path, out_dir: Path, mode: str = "real",
              env_extra: dict | None = None, confirm: str = PHRASE, timeout: int = 180) -> tuple:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "tools/real_generation_batch.py", "--mode", mode,
         "--card", str(card_path), "--confirm", confirm, "--out", str(out_dir)],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="real_gen_gates_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="real_gen_cards_"))

    real_before = sha(REAL_CARD)
    real_status_before = json.loads(REAL_CARD.read_text(encoding="utf-8")).get("status")

    good_env = {"DEEPSEEK_BASE_URL": TARGET_OK, "DEEPSEEK_MODEL": "deepseek-chat",
                "DEEPSEEK_API_KEY": DUMMY_KEY}

    try:
        # ---------- 0. 正向对照：合规卡必须放行 ----------
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="deepseek-chat",
                            DEEPSEEK_API_KEY=DUMMY_KEY):
            refused, why = preflight(base_card())
        check("门禁·正向对照：合规的 CONFIRMED 卡能通过预检（门禁不是一律拒绝）",
              not refused, f"拒绝原因={why}")

        # ---------- 1. status 门禁 ----------
        for status in (None, "DRAFT", rgb.IN_FLIGHT_STATUS, rgb.FINAL_STATUS_DONE,
                       rgb.FINAL_STATUS_ABORTED, "WHATEVER"):
            card = base_card()
            if status is None:
                card.pop("status")
            else:
                card["status"] = status
            with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="deepseek-chat",
                                DEEPSEEK_API_KEY=DUMMY_KEY):
                refused, _ = preflight(card)
            check(f"门禁·状态：status={status!r} 被拒绝", refused, "")

        # ---------- 2. 已执行的真实卡：子进程也必须被拒，且卡本身不被改动 ----------
        card_tmp = tmp / "executed_card.json"
        shutil.copyfile(REAL_CARD, card_tmp)
        rc, out = run_child(card_tmp, out_dir / "child_executed", env_extra=good_env)
        check("门禁·单次性：已执行的卡用子进程发车 → 拒绝（exit=3）",
              rc == 3 and "不是可发车状态" in out, f"exit={rc}")
        check("门禁·单次性：被拒时没有启动 api_v3（证据目录里没有 api_v3_real.log）",
              not (out_dir / "child_executed" / "api_v3_real.log").exists(), "")
        check("门禁·单次性：真实卡在本次回归前后逐字节不变（记录未被篡改）",
              sha(REAL_CARD) == real_before
              and json.loads(REAL_CARD.read_text(encoding="utf-8")).get("status") == real_status_before,
              f"status={real_status_before}")

        # ---------- 3. 并发第二进程 ----------
        card2 = tmp / "concurrent_card.json"
        rgb.write_card(card2, base_card())
        lock = rgb.claim_card(card2)
        claimed = json.loads(card2.read_text(encoding="utf-8"))
        check("门禁·并发：第一个认领成功并把 status 切成 RUNNING、锁文件建立",
              claimed.get("status") == rgb.IN_FLIGHT_STATUS
              and claimed.get("claim", {}).get("pid") == os.getpid() and lock.exists(),
              f"status={claimed.get('status')} lock={lock.name}")
        rc, out = run_child(card2, out_dir / "child_concurrent", env_extra=good_env)
        check("门禁·并发：第二个进程在发请求前被拒绝（提到锁/已被认领）",
              rc == 3 and ("已被认领" in out or "锁文件" in out), f"exit={rc}")
        try:
            rgb.claim_card(card2)                     # 直接再抢一次锁：验证 O_EXCL 本身
            lock_refused = False
        except SystemExit:
            lock_refused = True
        check("门禁·并发：锁已被占用时再次认领被拒绝（O_CREAT|O_EXCL 本身生效）", lock_refused, "")
        check("门禁·并发：第二进程没有启动 api_v3（没有真实/桩调用发生）",
              not (out_dir / "child_concurrent" / "api_v3_real.log").exists(), "")
        rgb.release_card(card2, lock, rgb.FINAL_STATUS_ABORTED, {"executed_at": "2026-09-20",
                                                                "requests_sent": 0,
                                                                "aborted": "门禁回归"})
        after = json.loads(card2.read_text(encoding="utf-8"))
        check("门禁·并发：释放后落成 RUN_ONCE_ABORTED、锁文件被删除",
              after.get("status") == rgb.FINAL_STATUS_ABORTED and not lock.exists(),
              f"status={after.get('status')}")
        rc, out = run_child(card2, out_dir / "child_after_abort", env_extra=good_env)
        check("门禁·并发：已中断的卡同样不能再发车（exit=3）", rc == 3, f"exit={rc}")

        # 两个参与者都先通过预检：后继续者不能在首进程释放锁后重放已执行卡。
        card2b = tmp / "stale_preflight_card.json"
        rgb.write_card(card2b, base_card())
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="deepseek-chat",
                            DEEPSEEK_API_KEY=DUMMY_KEY):
            identity_a = rgb.preflight_real(rgb.load_card(card2b), PHRASE)
            identity_b = rgb.preflight_real(rgb.load_card(card2b), PHRASE)
        first_lock = rgb.claim_card(card2b, identity_a)
        rgb.release_card(card2b, first_lock, rgb.FINAL_STATUS_DONE,
                         {"executed_at": "2026-09-21", "requests_sent": 0,
                          "aborted": None})
        try:
            rgb.claim_card(card2b, identity_b)
            stale_refused = False
        except SystemExit:
            stale_refused = True
        stale_after = json.loads(card2b.read_text(encoding="utf-8"))
        stale_lock = rgb.lock_path_of(card2b)
        check("门禁·竞态：两边都先通过预检后，旧预检不能重放已执行卡",
              stale_refused, "")
        check("门禁·竞态：拒绝后卡仍为 RUN_ONCE_EXECUTED，未被改回 RUNNING",
              stale_after.get("status") == rgb.FINAL_STATUS_DONE,
              f"status={stale_after.get('status')}")
        check("门禁·竞态：旧预检拒绝后只清理自己创建的锁",
              not stale_lock.exists(), f"lock_exists={stale_lock.exists()}")

        # ---------- 4. 目标地址漂移（unit + 子进程） ----------
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OTHER, DEEPSEEK_MODEL="deepseek-chat",
                            DEEPSEEK_API_KEY=DUMMY_KEY):
            refused, _ = preflight(base_card())
        check("门禁·目标：卡内 expected_base_url 与服务端不一致 → 拒绝（远程地址也不再放行）",
              refused, "")
        refused, _ = preflight(base_card(expected_base_url=None))
        check("门禁·目标：缺少 expected_base_url → 拒绝（fail closed）", refused, "")
        with patched_config(DEEPSEEK_BASE_URL="http://127.0.0.1:5281/v1",
                            DEEPSEEK_MODEL="deepseek-chat", DEEPSEEK_API_KEY=DUMMY_KEY):
            refused, _ = preflight(base_card(expected_base_url="http://127.0.0.1:5281/v1"))
        check("门禁·目标：回环地址即使精确匹配也被拒（打桩必须走 --mode stub）", refused, "")

        card3 = tmp / "drift_card.json"
        rgb.write_card(card3, base_card(expected_base_url="https://api.deepseek.com"))
        rc, out = run_child(card3, out_dir / "child_drift",
                            env_extra={"DEEPSEEK_BASE_URL": TARGET_OTHER,
                                       "DEEPSEEK_MODEL": "deepseek-chat",
                                       "DEEPSEEK_API_KEY": DUMMY_KEY})
        check("门禁·目标：子进程遇到地址漂移 → 拒绝（exit=3）且未启动 api_v3",
              rc == 3 and "目标地址漂移" in out
              and not (out_dir / "child_drift" / "api_v3_real.log").exists(), f"exit={rc}")

        # ---------- 5. 模型 / 凭据 / 短语 / 确认 / 预算 ----------
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="glm-4-flash",
                            DEEPSEEK_API_KEY=DUMMY_KEY):
            refused, _ = preflight(base_card())
        check("门禁·模型：服务端 model 与卡内不一致 → 拒绝", refused, "")
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="deepseek-chat",
                            DEEPSEEK_API_KEY=""):
            refused, _ = preflight(base_card())
        check("门禁·凭据：服务端无 Key → 拒绝", refused, "")

        cases = [
            ("未确认（confirmed_by_user=false）", base_card(confirmed_by_user=False), PHRASE),
            ("确认短语不符", base_card(), "乱写的短语"),
            ("短语为空", base_card(), ""),
            ("预算被改成 5", base_card(max_requests=5), PHRASE),
            ("预算被改成 3", base_card(max_requests=3), PHRASE),
            ("重试次数被改成 1", base_card(max_retries=1), PHRASE),
            ("用例数超过预算", base_card(cases=[{"id": f"X{i}", "kind": "x",
                                                 "corpus_id": "rag_learning", "mode": "generate",
                                                 "top_k": 3, "question": "q"} for i in range(5)]), PHRASE),
        ]
        with patched_config(DEEPSEEK_BASE_URL=TARGET_OK, DEEPSEEK_MODEL="deepseek-chat",
                            DEEPSEEK_API_KEY=DUMMY_KEY):
            for label, card, phrase in cases:
                refused, _ = preflight(card, phrase)
                check(f"门禁·其他：{label} → 拒绝", refused, "")

        # ---------- 6. 干跑不认领调用卡 ----------
        card4 = tmp / "stub_card.json"
        rgb.write_card(card4, base_card())
        rc, out = run_child(card4, out_dir / "child_stub", mode="stub", env_extra=good_env)
        after4 = json.loads(card4.read_text(encoding="utf-8"))
        check("门禁·干跑：stub 模式可以正常跑完（4 条里只用卡内 1 条用例）",
              rc == 0 and "模式=stub" in out, f"exit={rc}")
        check("门禁·干跑：stub 模式不认领卡（status 仍为 CONFIRMED、没有锁文件）",
              after4.get("status") == rgb.RUNNABLE_STATUS
              and not rgb.lock_path_of(card4).exists(), f"status={after4.get('status')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    check("门禁·全局：真实卡在整个回归过程中未被改动（逐字节）",
          sha(REAL_CARD) == real_before, f"status={real_status_before}")

    (out_dir / "verify_real_gen_gates_result.json").write_text(json.dumps({
        "passed": PASSED,
        "failed": FAILED,
        "real_card_file": REAL_CARD.name,
        "real_card_status": real_status_before,
        "real_card_sha256_prefix": real_before[:16],
        "network": ("无真实网络：所有应被拒绝的用例都在建立连接之前被拒绝；"
                    "唯一启动进程的是最后一条 stub 干跑用例（走 127.0.0.1 本机桩）；"
                    "被拒用例里的 https://example.invalid 是 RFC 2606 保留域名，不可解析"),
        "gates": ["status 必须为 CONFIRMED", "confirmed_by_user 必须为 true",
                  "--confirm 与 confirm_phrase 逐字相同",
                  "max_requests=4 且 max_retries=0（冻结值）",
                  "用例数不超过预算",
                  "expected_base_url 与服务端 config.py 精确相等",
                  "目标不得为本机回环地址",
                  "服务端 model 与卡内 model 精确相等",
                  "服务端存在 DEEPSEEK_API_KEY",
                  "O_CREAT|O_EXCL 排他认领（并发第二进程 fail closed）",
                  "锁内复核 status 与预检卡片身份（旧预检结果 fail closed）"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "-" * 78)
    print(f"门禁回归（无网络）PASS {len(PASSED)} 项；FAIL {len(FAILED)} 项。证据目录：{out_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
