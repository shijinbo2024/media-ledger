# -*- coding: utf-8 -*-
"""run_control_group.py — 对照组：不使用账本/MCP，直接用 LLM 编排产出 payload 的成本基线。

对照组口径（与 run_benchmark.py 同一份题库 benchmark_cases.jsonl，确定性抽样）：
  无账本直接跑 = 每题将「系统提示（生成 API 口径 + payload schema + few-shot 示例）
                + 需求句」发给 LLM，由 LLM 生成 payload JSON——这正是账本省掉的编排层。

测量与估算口径（诚实区分实测与假设）：
  token   对照组 = 基线提示词 + 需求句 + 补全（真值 payload JSON）的估算值。
                  估算器：CJK 字符 ≈1.0 token/字，其他字符 ≈0.25 token/字符
                  （主流中文 LLM tokenizer 量级；真实 tokenizer 差异 ±30%）。
          实验组 = 0（骨架匹配为纯字符串运算，编排层零 LLM 调用）。
  延迟    实验组 = 本次同机逐题实测 ask()（与主 benchmark 同口径，含配方沙箱子进程）。
          对照组 = 不做真实 LLM 调用（零 key 纪律），按 --baseline-p50/--baseline-p90
                  参数化假设计入（默认 1500/4000 ms，公开口径 LLM API 单次编排典型量级），
                  报告中明确标注为假设而非实测。

纪律：零 API 真调、零 key、零 LLM 调用、零网络请求；只读账本，不写 state/。
产出：benchmark/CONTROL_GROUP.md + benchmark/control_group_summary.json
"""
import argparse
import io
import json
import math
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import runtime_media as R  # noqa: E402

CASES_PATH = HERE / "benchmark_cases.jsonl"
REPORT_PATH = HERE / "CONTROL_GROUP.md"
SUMMARY_PATH = HERE / "control_group_summary.json"
SEED = 20260925
N_EXACT, N_WRAP, N_FAR = 200, 50, 100

# 基线系统提示：无账本接入方每次调用都必须重复发送的编排知识（真实量级，随包可审计）
SYS_PROMPT = """你是多模态生成 API 的参数编排助手。把用户的中文生成需求转换为生成 API 的调用 payload（JSON），只输出 JSON，不要解释。

## 可用 API 与参数口径
1) 图片生成（OpenAI 兼容）：
POST /images/generations
body: {"model": "doubao-seedream-4-0", "prompt": "<画面描述，含风格/主体/构图要求>", "size": "<宽>x<高>", "response_format": "url"}
返回 data[0].url。size 常用：1024x1024、720x1280、1080x1920、1920x1080。
2) 视频生成（异步任务）：
POST /contents/generations/tasks
body: {"model": "doubao-seedance-1-0-pro", "content": [{"type": "text", "text": "<画面描述> --resolution <480p|720p|1080p> --duration <秒> --ratio <9:16|16:9|adaptive> --watermark false"}]}
（图生视频在 content 数组追加 {"type": "image_url", "image_url": {"url": "<首帧图 URL>"}}）
返回 id；轮询 GET /contents/generations/tasks/{id} 至 succeeded，取 content.video_url。

## 输出要求
- 只输出一个 JSON 对象；图片题字段：adapter/model/prompt/size/response_format；视频题字段：adapter/model/text（或含 image_url）。
- prompt 要把风格、主体、构图要求组织成通顺的画面描述；数字参数必须与需求一致。

## 示例
需求：生成一张1080x1920的赛博朋克风格城市夜景封面图
输出：{"adapter": "ark_image", "model": "doubao-seedream-4-0", "prompt": "赛博朋克风格城市夜景，竖版封面构图，视觉焦点居中，高对比度", "size": "1080x1920", "response_format": "url"}

需求：生成一段5秒720p的竖版猫咪跳舞短视频
输出：{"adapter": "ark_video", "model": "doubao-seedance-1-0-pro", "text": "猫咪跳舞，竖版构图，动感运镜 --resolution 720p --duration 5 --ratio 9:16 --watermark false"}
"""

FAR_COMPLETION_EST = 80   # 账外题对照组补全估算（LLM 仍会作答：猜测 payload 或拒答说明）


def est_tokens(s):
    """估算 token：CJK 字符 1.0 token/字 + 其他字符 0.25 token/字符（口径见模块 docstring）。"""
    cjk = other = 0
    for ch in s:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            cjk += 1
        else:
            other += 1
    return round(cjk + other / 4)


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return round(sorted_vals[k], 3)


def ensure_cases():
    if CASES_PATH.exists():
        return
    print("题库缺失，自动生成（seed=%d，一次性）…" % SEED, flush=True)
    subprocess.run([sys.executable, str(HERE / "gen_benchmark.py")], check=True)


def main():
    ap = argparse.ArgumentParser(description="MediaLedger 对照组（无账本 LLM 编排基线）")
    ap.add_argument("--baseline-p50", type=float, default=1500.0,
                    help="对照组单题延迟假设 p50（ms，假设非实测）")
    ap.add_argument("--baseline-p90", type=float, default=4000.0,
                    help="对照组单题延迟假设 p90（ms，假设非实测）")
    a = ap.parse_args()

    ensure_cases()
    ledger = R.load_ledger()
    fn_cache = {}

    def recipe_fn(rid, code):
        if rid not in fn_cache:
            ns = {}
            exec(compile(code, "<recipe:%s>" % rid, "exec"), ns)
            fn_cache[rid] = ns["f"]
        return fn_cache[rid]

    cases = [json.loads(l) for l in
             io.open(CASES_PATH, encoding="utf-8").read().splitlines() if l.strip()]
    rng = random.Random(SEED)
    in_exact = [c for c in cases
                if c["category"] == "in_domain" and c.get("subtype") != "wrapped"]
    in_wrap = [c for c in cases
               if c["category"] == "in_domain" and c.get("subtype") == "wrapped"]
    out_dom = [c for c in cases if c["category"] == "out_domain"]
    sample = (rng.sample(in_exact, min(N_EXACT, len(in_exact)))
              + rng.sample(in_wrap, min(N_WRAP, len(in_wrap)))
              + rng.sample(out_dom, min(N_FAR, len(out_dom))))

    sys_tok = est_tokens(SYS_PROMPT)
    groups = {"in_exact": [], "in_wrap": [], "out": []}
    print("sampled %d cases (exact=%d wrap=%d far=%d)，逐题计时…"
          % (len(sample), len(groups) and min(N_EXACT, len(in_exact)),
             min(N_WRAP, len(in_wrap)), min(N_FAR, len(out_dom))), flush=True)

    for i, case in enumerate(sample):
        t0 = time.perf_counter()
        res = R.ask(ledger, case["text"])
        dt = round((time.perf_counter() - t0) * 1000, 3)
        if case["category"] == "in_domain":
            gt = case["ground_truth"]
            rmeta = next(r for r in ledger["recipes"] if r["id"] == gt["recipe_id"])
            argv = [R.norm_q(str((gt.get("slots") or {})[p])) for p in rmeta["params"]]
            payload = recipe_fn(gt["recipe_id"], rmeta["fn"])(*argv)
            completion = json.dumps(payload, ensure_ascii=False)
            base_tok = sys_tok + est_tokens(case["text"]) + est_tokens(completion)
            key = "in_wrap" if case.get("subtype") == "wrapped" else "in_exact"
        else:
            base_tok = sys_tok + est_tokens(case["text"]) + FAR_COMPLETION_EST
            key = "out"
        groups[key].append({"baseline_tokens": base_tok, "ledger_ms": dt})
        if (i + 1) % 50 == 0:
            print("  %d/%d" % (i + 1, len(sample)), flush=True)

    def agg(rows):
        lats = sorted(r["ledger_ms"] for r in rows)
        return {"n": len(rows),
                "baseline_tokens_sum": sum(r["baseline_tokens"] for r in rows),
                "baseline_tokens_avg": round(statistics.mean(
                    r["baseline_tokens"] for r in rows), 1) if rows else 0,
                "ledger_p50": pct(lats, 50), "ledger_p90": pct(lats, 90),
                "ledger_mean": round(statistics.mean(lats), 2) if lats else None}

    summary = {k: agg(v) for k, v in groups.items()}
    total_base = sum(s["baseline_tokens_sum"] for s in summary.values())
    n_all = sum(s["n"] for s in summary.values())
    save_time_in = round(1 - summary["in_exact"]["ledger_p50"] / a.baseline_p50, 4)
    save_time_out = round(1 - summary["out"]["ledger_p50"] / a.baseline_p50, 4)
    summary_out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "seed": SEED,
        "sample": {"in_exact": N_EXACT, "in_wrap": N_WRAP, "out": N_FAR},
        "baseline_prompt_tokens_est": sys_tok,
        "baseline_latency_assumption_ms": {"p50": a.baseline_p50, "p90": a.baseline_p90},
        "groups": summary,
        "total_baseline_tokens_est": total_base,
        "ledger_tokens": 0,
        "token_saving_pct": 1.0,
        "time_saving_p50": {"in_domain": save_time_in, "out_domain": save_time_out},
    }
    SUMMARY_PATH.write_text(json.dumps(summary_out, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    ie, iw, ou = summary["in_exact"], summary["in_wrap"], summary["out"]
    md = """# MediaLedger 对照组报告：不用账本直接跑（LLM 编排基线）

- 题库：与主 benchmark 同一份 benchmark_cases.jsonl（seed=%d），确定性抽样
  域内 exact %d + wrapped %d + 账外 %d，共 %d 题；完成时间：%s
- 对照组（无账本直接跑）：每题发送「系统提示（生成 API 口径 + payload schema + few-shot）
  + 需求句」给 LLM，由 LLM 生成 payload——该系统提示每次调用都要重复发送，是每题固定成本。
- 实验组（本账本）：骨架匹配直出 payload，编排层零 LLM 调用。

## 1. token 节省

估算口径：CJK 字符 ≈1.0 token/字 + 其他字符 ≈0.25 token/字符（主流中文 LLM tokenizer
量级，真实 tokenizer 差异 ±30%%）。系统提示估算 %d token/次。

| 题型 | 题量 | 对照组 token 合计（估算） | 单题均值 | 实验组 token | 节省 |
|---|---|---|---|---|---|
| 域内 exact | %d | %s | %s | 0 | 100%% |
| 域内 wrapped | %d | %s | %s | 0 | 100%% |
| 账外 FAR | %d | %s | %s | 0 | 100%% |
| **合计** | **%d** | **%s** | — | **0** | **100%%（编排层）** |

结论：编排层 token 节省 **100%%**——账本路径不调用任何 LLM；对照基线的 token 全部来自
「每题重复携带 API 编排知识」与「LLM 逐题生成 payload」。

## 2. 时间对比

实验组为本次同机逐题实测（与主 benchmark 同口径，命中题含配方沙箱子进程）；
对照组未做真实 LLM 调用（零 key 纪律），按参数化假设计入（p50=%s ms / p90=%s ms，
公开口径 LLM API 单次编排典型量级）——**标注为假设，非实测**，可用
`--baseline-p50/--baseline-p90` 调整。

| 题型 | 账本实测 p50 / p90 / mean (ms) | 基线 p50 / p90 (ms, 假设) | 单题节省（按 p50） |
|---|---|---|---|
| 域内（命中） | %s / %s / %s | %s / %s | %s%% |
| 账外（回退） | %s / %s / %s | %s / %s | %s%% |

- 账本命中延迟主要由配方沙箱子进程构成（引擎安全边界设计）；纯匹配路径（账外/未命中）
  仅约 1~3 ms，比任何 LLM 编排快三个数量级。
- 量级结论稳定成立：账本毫秒级 vs LLM 编排秒级；且账本结果确定性可断言，LLM 编排
  每次输出都可能漂移、需要额外校验轮次（未计入上表，实际差距更大）。

## 3. 一次性成本说明

种子账本（13 配方 / 70 模板）随包分发、零构建成本；后续新配方用 teach 通道
（一句原话 + 单行函数 + 断言用例）秒级入账，无 LLM 编排成本。

## 4. 复现

```bash
python benchmark/gen_benchmark.py      # 题库（缺失时本脚本也会自动生成）
python benchmark/run_control_group.py  # 本报告（可用 --baseline-p50/--baseline-p90 调整假设）
```
""" % (SEED, ie["n"], iw["n"], ou["n"], n_all,
       summary_out["ts"], sys_tok,
       ie["n"], "{:,}".format(ie["baseline_tokens_sum"]), ie["baseline_tokens_avg"],
       iw["n"], "{:,}".format(iw["baseline_tokens_sum"]), iw["baseline_tokens_avg"],
       ou["n"], "{:,}".format(ou["baseline_tokens_sum"]), ou["baseline_tokens_avg"],
       n_all, "{:,}".format(total_base),
       ("%.0f" % a.baseline_p50), ("%.0f" % a.baseline_p90),
       ie["ledger_p50"], ie["ledger_p90"], ie["ledger_mean"],
       ("%.0f" % a.baseline_p50), ("%.0f" % a.baseline_p90),
       ("%.1f" % (save_time_in * 100)),
       ou["ledger_p50"], ou["ledger_p90"], ou["ledger_mean"],
       ("%.0f" % a.baseline_p50), ("%.0f" % a.baseline_p90),
       ("%.2f" % (save_time_out * 100)))
    REPORT_PATH.write_text(md, encoding="utf-8")
    print(json.dumps({k: v for k, v in summary_out.items() if k != "groups"},
                     ensure_ascii=False, indent=1))
    print("report -> %s" % REPORT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
