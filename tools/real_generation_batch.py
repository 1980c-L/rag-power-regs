# -*- coding: utf-8 -*-
"""第二阶段 B 的真实生成执行器（调用卡驱动）—— **默认干跑，不碰真实模型**。

它做三件事：
  1. 读调用卡（默认 `tools/real_gen_card.json`，可用 `--card` 指定别的卡）；
  2. 按卡里的用例逐条走**真实接口链路**（`api_v3.py` → `rag_core.py` 真实检索 → 生成层）；
  3. 把每条用例的证据落盘（问题、检索证据 id、回答、引用、状态、耗时），不记请求头、不记 Key。

两种模式：

  --mode stub（默认）  生成层指向本机桩 `tools/llm_stub_server.py`，**零真实请求**；
                       干跑**不认领调用卡**（不改卡状态），可以反复跑。
  --mode real          真发请求。发车前必须全部通过下面的门禁，任一不过即拒绝（退出码 3），
                       且拒绝发生在启动 api_v3 / 建立任何连接**之前**。

## 真实模式的门禁（fail closed）

| # | 门禁 | 说明 |
|---|---|---|
| 1 | **卡状态必须是"已确认、尚未执行"**（`status == "CONFIRMED"`） | `DRAFT` / `RUNNING` / `RUN_ONCE_EXECUTED` / `RUN_ONCE_ABORTED` / 缺字段 一律拒绝 —— 一张卡只能发车一次 |
| 2 | `confirmed_by_user == true` | 口头授权要落成字段 |
| 3 | `--confirm` 与卡内 `confirm_phrase` **逐字相同** | 防手滑 |
| 4 | `max_requests == 4` 且 `max_retries == 0` | 复审冻结值；改预算必须另发新卡（并同步改执行器后再复审） |
| 5 | 卡内有 `expected_base_url`，且与 `config.DEEPSEEK_BASE_URL` **精确相等** | 目标漂移立即停，不再只排除回环 |
| 6 | 目标不是本机回环地址 | 打桩请用 `--mode stub`，真实模式不接受回环目标 |
| 7 | 服务端 `DEEPSEEK_MODEL` 与卡内 `model` 精确相等 | 模型漂移立即停 |
| 8 | 服务端存在 `DEEPSEEK_API_KEY` | 凭据缺失即停 |

## 单次性（排他认领）

通过门禁后，执行器用 `O_CREAT|O_EXCL` 建 `<卡>.lock`（跨进程排他），在锁内重读并复核
`status` 与预检卡片身份，再把卡切成 `RUNNING`，然后才启动 `api_v3`；结束时落成
`RUN_ONCE_EXECUTED` / `RUN_ONCE_ABORTED` 并删除锁文件。
因此**并发的第二个进程**、以及**任何已执行/已中断的卡**，都会在发请求前被拒绝。
锁文件残留（硬崩）也会让后续运行 fail closed，需人工查验后另发新卡。

## 调用卡字段（最小集）

```jsonc
{
  "card_version": 1, "phase": "...",
  "status": "CONFIRMED",                  // 只有这个状态可以发车
  "confirmed_by_user": true, "confirmed_at": "YYYY-MM-DD",
  "confirm_phrase": "……",
  "provider": "...", "model": "deepseek-chat",
  "expected_base_url": "https://api.deepseek.com",
  "credential_ref": "...", "base_url_source": "...",
  "max_requests": 4, "max_retries": 0,
  "stop_conditions": [...], "output_policy": "...", "purpose": "...",
  "cases": [ {"id": "...", "kind": "...", "corpus_id": "...", "mode": "generate",
              "top_k": 3, "question": "..."} ]
}
```

运行：
    python tools/real_generation_batch.py --out <证据目录>                        # 干跑（安全）
    python tools/real_generation_batch.py --mode real --card <新卡> --confirm "…" --out <证据目录>
本文件内不写任何个人绝对路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import config                                                   # noqa: E402
import verify_api_v3 as iap                                     # noqa: E402（复用进程控制与 HTTP 工具）

DEFAULT_CARD = REPO / "tools" / "real_gen_card.json"
HARD_MAX_REQUESTS = 4            # 复审冻结值
RUNNABLE_STATUS = "CONFIRMED"    # 唯一可发车状态
IN_FLIGHT_STATUS = "RUNNING"
FINAL_STATUS_DONE = "RUN_ONCE_EXECUTED"
FINAL_STATUS_ABORTED = "RUN_ONCE_ABORTED"
KNOWN_STATUSES = ("DRAFT", RUNNABLE_STATUS, IN_FLIGHT_STATUS, FINAL_STATUS_DONE, FINAL_STATUS_ABORTED)
REQUIRED_KEYS = ("request", "recorded_top_k", "status", "notice", "elapsed_ms",
                 "hits", "neighbors", "context_chars", "answer", "refs", "ref_occurrences")

LOOPBACK_RE = re.compile(r"://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)")


def normalize_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def load_card(path: Path) -> dict:
    if not path.exists():
        refuse(f"找不到调用卡：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def card_identity(card: dict) -> str:
    """调用卡的稳定语义身份；用于绑定预检与锁内认领。"""
    payload = json.dumps(card, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_card(path: Path, card: dict) -> None:
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refuse(reason: str) -> None:
    print(f"[REFUSED] {reason}", flush=True)
    print("[REFUSED] 没有发出任何真实请求。", flush=True)
    raise SystemExit(3)


def preflight_real(card: dict, confirm: str | None) -> str:
    """真实模式的全部门禁；通过时返回供锁内复核的调用卡身份。"""
    status = card.get("status")
    if status != RUNNABLE_STATUS:
        extra = {
            IN_FLIGHT_STATUS: "该卡已被认领（可能有另一个进程正在跑，或上次硬崩留下锁）。",
            FINAL_STATUS_DONE: "该卡已经执行过：一张卡只能发车一次，请另发新卡并重新授权。",
            FINAL_STATUS_ABORTED: "该卡上次已中断：请人工查验后另发新卡。",
            "DRAFT": "该卡还是草稿，未被批准。",
        }.get(status, "状态非法或缺失（fail closed）。")
        refuse(f"调用卡 status={status!r} 不是可发车状态 {RUNNABLE_STATUS!r}。{extra}")
    if card.get("confirmed_by_user") is not True:
        refuse(f"调用卡 confirmed_by_user 仍为 {card.get('confirmed_by_user')!r}，"
               "用户尚未确认调用卡，不允许真实调用。")
    if (confirm or "") != card.get("confirm_phrase"):
        refuse("--confirm 与调用卡里的确认短语不一致（需要逐字相同）。")
    if int(card.get("max_requests", 0)) != HARD_MAX_REQUESTS:
        refuse(f"调用卡 max_requests={card.get('max_requests')!r} 与冻结值 {HARD_MAX_REQUESTS} 不一致；"
               "预算变更必须另发新卡并同步修改执行器后再复审。")
    if int(card.get("max_retries", 0)) != 0:
        refuse("调用卡 max_retries 必须为 0。")
    if len(card.get("cases") or []) > int(card.get("max_requests", 0)):
        refuse(f"调用卡里有 {len(card.get('cases') or [])} 条用例，超过预算 "
               f"{card.get('max_requests')} 次。")

    expected = card.get("expected_base_url")
    if not expected:
        refuse("调用卡缺少 expected_base_url：无法锁定生成目标地址（fail closed）。")
    if normalize_url(expected) != normalize_url(config.DEEPSEEK_BASE_URL):
        refuse(f"目标地址漂移：卡内 expected_base_url={expected!r} 与服务端 "
               f"config.py 的 {config.DEEPSEEK_BASE_URL!r} 不一致 → 停止。")
    if LOOPBACK_RE.search(config.DEEPSEEK_BASE_URL or ""):
        refuse(f"生成目标 base_url={config.DEEPSEEK_BASE_URL} 是本机回环地址；"
               "真实模式只接受卡内写明的远程 provider 地址（要打桩请用 --mode stub）。")
    if (config.DEEPSEEK_MODEL or "").strip() != card.get("model"):
        refuse(f"服务端配置的 model={config.DEEPSEEK_MODEL!r} 与调用卡 {card.get('model')!r} 不一致"
               "（模型漂移即停止；要换模型必须先改卡）。")
    if not (config.DEEPSEEK_API_KEY or "").strip():
        refuse("服务端没有检测到 DEEPSEEK_API_KEY（凭据缺失即停止）。")

    print(f"[card] status={status} provider={card.get('provider')} model={card.get('model')}", flush=True)
    print(f"[card] expected_base_url={expected}（与服务端一致）", flush=True)
    print(f"[card] 预算：最多 {card['max_requests']} 次请求、{card['max_retries']} 次重试；"
          f"用例 {len(card.get('cases', []))} 条", flush=True)
    return card_identity(card)


# ---------------- 单次性：排他认领 ----------------
def lock_path_of(card_path: Path) -> Path:
    return card_path.with_suffix(card_path.suffix + ".lock")


def claim_card(card_path: Path, expected_identity: str | None = None) -> Path:
    """锁内复核状态/身份后，把已确认卡排他地切成运行中。"""
    lock = lock_path_of(card_path)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        refuse(f"调用卡已被认领（锁文件 {lock.name} 已存在）：可能有另一个进程正在运行，"
               "或上次运行硬崩留下残留。拒绝发车（fail closed），请人工查验后另发新卡。")
    claimed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"pid={os.getpid()} claimed_at={claimed_at}\n")
    card = load_card(card_path)
    if card.get("status") != RUNNABLE_STATUS:
        lock.unlink()
        refuse(f"调用卡在预检后已变成 status={card.get('status')!r}，不再是可发车状态 "
               f"{RUNNABLE_STATUS!r}；拒绝使用旧预检结果（fail closed）。")
    if expected_identity is not None and card_identity(card) != expected_identity:
        lock.unlink()
        refuse("调用卡在预检后发生内容漂移；拒绝认领（fail closed），请重新预检并人工确认。")
    card["status"] = IN_FLIGHT_STATUS
    card["claim"] = {"pid": os.getpid(), "claimed_at": claimed_at, "lock": lock.name}
    write_card(card_path, card)
    print(f"[claim] 已排他认领调用卡（pid={os.getpid()}），status → {IN_FLIGHT_STATUS}", flush=True)
    return lock


def release_card(card_path: Path, lock: Path, final_status: str, run_log: dict) -> None:
    card = load_card(card_path)
    card["status"] = final_status
    merged = dict(card.get("run_log") or {})
    merged.update(run_log)
    card["run_log"] = merged
    card.pop("claim", None)
    write_card(card_path, card)
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    print(f"[release] status → {final_status}，锁已释放", flush=True)


def classify_failure(detail: str) -> str:
    """把生成失败归类到停止条件里，便于人工复核（不重试，直接停）。"""
    d = (detail or "").lower()
    if "401" in d or "403" in d or "unauthorized" in d or "forbidden" in d or "invalid api key" in d:
        return "auth"
    if "429" in d or "rate" in d or "too many requests" in d:
        return "rate_limit"
    if "timeout" in d or "timed out" in d or "connection" in d or "max retries" in d:
        return "network"
    return "structure_or_other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("stub", "real"), default="stub")
    ap.add_argument("--card", default=str(DEFAULT_CARD), help="调用卡路径（默认 tools/real_gen_card.json）")
    ap.add_argument("--confirm", default=None, help="真实模式必须与调用卡确认短语逐字相同")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    card_path = Path(args.card)
    card = load_card(card_path)
    out_dir = Path(args.out) if args.out else REPO / "output" / "phase_b_dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = list(card.get("cases") or [])

    if len(cases) > int(card["max_requests"]):
        refuse(f"调用卡里有 {len(cases)} 条用例，超过预算 {card['max_requests']} 次。")

    lock: Path | None = None
    if args.mode == "real":
        preflight_identity = preflight_real(card, args.confirm)
        lock = claim_card(card_path, preflight_identity)  # 锁内复核后才允许启动 api_v3
        card = load_card(card_path)             # 认领后的卡（status=RUNNING）
        target_note = f"真实模型（{card.get('provider')} / {card.get('model')}）"
    else:
        target_note = "本机桩 tools/llm_stub_server.py（干跑：零真实模型请求；不认领调用卡）"

    print(f"[run] 模式={args.mode}；卡={card_path.name}；生成目标={target_note}", flush=True)
    print(f"[run] 用例 {len(cases)} 条；证据目录 {out_dir}", flush=True)

    ledger_path = out_dir / f"real_gen_ledger_{args.mode}.jsonl"
    ledger_path.write_text("", encoding="utf-8")
    result: dict = {
        "mode": args.mode,
        "card_file": card_path.name,
        "card": {k: card.get(k) for k in ("card_version", "phase", "status", "confirmed_by_user",
                                          "provider", "model", "expected_base_url",
                                          "max_requests", "max_retries")},
        "target_note": target_note,
        "generation_verified": False,
        "cases": [],
        "aborted": None,
    }
    aborted: str | None = None
    sent = 0
    stub = api = None
    try:
        if args.mode == "stub":
            stub = iap.start_stub("ok", out_dir / "llm_stub_requests.jsonl")
            api = iap.start_api(True, out_dir / "api_v3.log")
        else:
            # 真实模式：不覆盖 base_url / model，也不用哨兵 Key —— 用服务端环境变量的真实凭据
            api = iap.spawn([sys.executable, "api_v3.py", "--port", str(iap.API_PORT)],
                            log=out_dir / "api_v3_real.log")
            if not iap.wait_health(iap.API_PORT):
                aborted = "api_v3 没能起来，本次没有发出生成请求"
            else:
                banner = (out_dir / "api_v3_real.log").read_text(encoding="utf-8", errors="replace")
                resolved = next((line for line in banner.splitlines() if "生成层目标" in line), "")
                result["resolved_generation_target"] = resolved.strip()
                if normalize_url(card.get("expected_base_url")) not in resolved:
                    aborted = (f"启动横幅里的生成目标与卡内 expected_base_url 不符 → 立即停止"
                               f"（横幅：{resolved.strip()[:120]}）")

        if args.mode == "stub":
            banner = (out_dir / "api_v3.log").read_text(encoding="utf-8", errors="replace")
            resolved = next((line for line in banner.splitlines() if "生成层目标" in line), "")
            result["resolved_generation_target"] = resolved.strip()

        for idx, case in enumerate(cases, 1):
            if aborted:
                break
            if sent >= int(card["max_requests"]):
                aborted = f"已达预算上限 {card['max_requests']}，停止"
                break
            payload = {"corpus_id": case["corpus_id"], "mode": case["mode"],
                       "top_k": int(case["top_k"]), "question": case["question"]}
            print(f"\n[{idx}/{len(cases)}] {case['id']} · {case['kind']}：{case['question']}", flush=True)
            sent += 1
            t0 = time.time()
            resp = iap.query(payload)                       # 复用 verify_api_v3 的 HTTP 工具
            wall_ms = int((time.time() - t0) * 1000)
            body = resp["json"]

            missing = [k for k in REQUIRED_KEYS if k not in body]
            entry = {
                "case_id": case["id"],
                "kind": case["kind"],
                "question": case["question"],
                "request": payload,
                "http_status": resp["status"],
                "status": body.get("status"),
                "notice": body.get("notice"),
                "notice_detail": body.get("notice_detail", ""),
                "elapsed_ms": body.get("elapsed_ms"),
                "wall_ms": wall_ms,
                "hit_ids": [h["id"] for h in body.get("hits", [])],
                "hit_scores": [h["score"] for h in body.get("hits", [])],
                "neighbor_count": len(body.get("neighbors", [])),
                "answer": body.get("answer"),
                "refs": body.get("refs"),
                "ref_occurrences": body.get("ref_occurrences"),
                "recorded_from": body.get("recorded_from", ""),
                "request_index": sent,
            }
            with ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            result["cases"].append(entry)

            if resp["status"] != 200 or missing:
                aborted = (f"响应异常（HTTP {resp['status']}，缺少字段 {missing}）→ 立即停止，"
                           f"不重试")
                break
            if body.get("status") == "error" or body.get("notice") == "generation_failed":
                why = classify_failure(body.get("notice_detail", ""))
                aborted = (f"生成失败（归类：{why}）→ 立即停止，不重试。"
                           f"detail={body.get('notice_detail', '')[:120]}")
                break
            print(f"    → notice={body.get('notice')} 命中 {len(body.get('hits', []))} 段 / "
                  f"引用 {len(body.get('refs') or [])} 条 / 状态 {body.get('status')}", flush=True)
    finally:
        iap.kill(api)
        iap.kill(stub)
        for h in iap.HANDLES:
            try:
                h.close()
            except Exception:                                  # noqa: BLE001
                pass
        if lock is not None:
            done = (aborted is None) and sent == len(cases)
            release_card(card_path, lock,
                         FINAL_STATUS_DONE if done else FINAL_STATUS_ABORTED,
                         {"executed_at": time.strftime("%Y-%m-%d"),
                          "mode": args.mode,
                          "requests_sent": sent,
                          "requests_budget": int(card["max_requests"]),
                          "retries": 0,
                          "aborted": aborted,
                          "evidence_dir": str(out_dir)})

    result["requests_sent"] = sent
    result["aborted"] = aborted
    (out_dir / f"real_gen_result_{args.mode}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# 第二阶段 B 执行记录（mode={args.mode}）", "",
             f"- 调用卡：{card_path.name}（status 见卡内 run_log）",
             f"- 生成目标：{target_note}",
             f"- 本轮请求数：**{sent}** / 上限 {card['max_requests']}；重试：0（失败即停）",
             f"- 中断原因：{aborted or '无（全部用例执行完毕）'}", "",
             "| 用例 | 类型 | 问题 | notice | 命中 | 引用 | 耗时(ms) |", "|---|---|---|---|---:|---:|---:|"]
    for e in result["cases"]:
        gen = (e["elapsed_ms"] or {}).get("generate")
        lines.append(f"| {e['case_id']} | {e['kind']} | {e['question']} | {e['notice'] or '—'} | "
                     f"{len(e['hit_ids'])} | {len(e['refs'] or [])} | {gen if gen is not None else '—'} |")
    lines += ["", "> 说明：`generation_verified` 恒为 false —— 生成层尚未完成正式验证；",
              "> 本轮样本只用于暴露问题，不能用来宣称生成质量或长期稳定性。"]
    (out_dir / f"real_gen_summary_{args.mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "-" * 78)
    print(f"请求数 {sent}（上限 {card['max_requests']}）；中断：{aborted or '无'}")
    print(f"证据写入：{out_dir}")
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
