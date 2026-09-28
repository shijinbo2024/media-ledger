# -*- coding: utf-8 -*-
"""run_benchmark.py — MediaLedger 100 倍规模 benchmark 跑批器（进程内，零真调）。

跑法：进程内 import runtime_media，账本一次加载，循环逐题调 ask()——
禁止每题起子进程（引擎配方沙箱 subprocess 是 runtime_media.ask 自身设计，原样保留）。

每题记录：
  匹配结果 status/recipe/sim/fuzzy/args；命中题（direct）按配方判分口径判 payload；
  参数提取正误（域内命中题 args 与 ground truth slots 逐 slot 归一化比对）；
  ask() 延迟 ms。

判分口径（照搬 runtime_media.judge_payload 三段断言，不自创）：
  expected = 配方 fn 按 ground truth slots（经 norm_q 归一化，与引擎切槽口径一致）
  进程内执行所得 payload；judge_payload(got, expected) 要求 got 在 expected 全部键上
  递归精确相等。expected 不用引擎输出（避免循环论证），用账本 fn 独立重算——
  fn 源码与 tests 均经现有 selftest 验证。

产出：
  benchmark/benchmark_full.jsonl   逐题流（题/类别/status/recipe/sim/fuzzy/args/
                                   ground_truth/参数正误/判分/延迟ms）
  benchmark/benchmark_summary.json 汇总指标
  benchmark/live_call_manifest.json 真调抽样清单（只生成清单，绝不执行真调）
  benchmark/BENCHMARK.md           开源报告（中文）

纪律：零 API 真调、零 key、零 LLM 调用、零网络请求；不改引擎与 state/；
     异常不吞，全部进 summary.anomalies 与 BENCHMARK.md 异常段。
"""
import io
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import runtime_media as R  # noqa: E402

CASES_PATH = HERE / "benchmark_cases.jsonl"
FULL_PATH = HERE / "benchmark_full.jsonl"
SUMMARY_PATH = HERE / "benchmark_summary.json"
MANIFEST_PATH = HERE / "live_call_manifest.json"
REPORT_PATH = HERE / "BENCHMARK.md"

N_IMAGE_MANIFEST = 30   # 30 条图
N_VIDEO_PER_RECIPE = 1  # 3 条视频（3 个视频配方各 1）


def pct(sorted_vals, p):
    """延迟分位数：最近秩法（ceil(p/100*n)-1 下标）。"""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return round(sorted_vals[k], 3)


def latency_block(vals):
    s = sorted(vals)
    if not s:
        return {"n": 0}
    return {"n": len(s), "mean": round(sum(s) / len(s), 3),
            "p50": pct(s, 50), "p90": pct(s, 90), "p99": pct(s, 99),
            "max": round(s[-1], 3)}


def main():
    t_wall0 = time.time()
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ledger = R.load_ledger()
    fn_cache = {}

    def recipe_fn(rid, code):
        if rid not in fn_cache:
            ns = {}
            exec(compile(code, "<recipe:%s>" % rid, "exec"), ns)  # 账本单行 def（selftest 已验）
            fn_cache[rid] = ns["f"]
        return fn_cache[rid]

    cases = [json.loads(l) for l in
             CASES_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None   # 冒烟：limit 取前 N 题
    if limit:
        cases = cases[:limit]
    out.write("loaded %d cases\n" % len(cases))

    per_recipe = {}       # rid -> dict
    anomalies = []        # fail-closed：一切异常如实记录
    far_leaks = []        # 账外泄漏样本（status=direct / 带 payload）
    ood_status = {}       # subtype -> Counter
    lat_all, lat_hit, lat_miss = [], [], []
    n = {"exact_hit": 0, "fuzzy_hit": 0, "judge_pass": 0, "judge_fail": 0,
         "param_ok": 0, "param_bad": 0, "in_miss": 0}
    hit_store = []        # 命中题快照（供 manifest 抽样）
    t_run0 = time.time()

    with FULL_PATH.open("w", encoding="utf-8") as ff:
        for i, case in enumerate(cases):
            rec = {"case_id": case["case_id"], "category": case["category"],
                   "subtype": case.get("subtype"), "text": case["text"],
                   "ground_truth": case["ground_truth"]}
            try:
                t0 = time.perf_counter()
                res = R.ask(ledger, case["text"])
                dt = round((time.perf_counter() - t0) * 1000, 3)
            except Exception as e:   # fail-closed：异常不吞
                dt = None
                anomalies.append({"case_id": case["case_id"], "text": case["text"],
                                  "error": "%s: %s" % (type(e).__name__, e)})
                rec.update({"status": "EXCEPTION", "latency_ms": None,
                            "error": "%s: %s" % (type(e).__name__, e)})
                ff.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue

            status = res.get("status")
            rec.update({"status": status, "recipe": res.get("recipe"),
                        "sim": res.get("sim"), "fuzzy": res.get("fuzzy"),
                        "args": res.get("args"), "latency_ms": dt})
            lat_all.append(dt)
            gt = case["ground_truth"]
            rid = gt.get("recipe_id")
            pr = per_recipe.setdefault(rid, {"n": 0, "hit": 0, "hit_exact": 0,
                                             "hit_fuzzy": 0, "judge_pass": 0,
                                             "judge_fail": 0, "param_ok": 0,
                                             "param_bad": 0, "lat_sum": 0.0})
            pr["n"] += 1
            pr["lat_sum"] += dt

            if case["category"] == "out_domain":
                ood_status.setdefault(case["subtype"], {})
                ood_status[case["subtype"]][status] = \
                    ood_status[case["subtype"]].get(status, 0) + 1
                if status == "direct" or "payload" in res:   # 泄漏口径与 selftest 一致
                    far_leaks.append({"case_id": case["case_id"],
                                      "subtype": case["subtype"],
                                      "text": case["text"], "status": status,
                                      "recipe": res.get("recipe")})
                lat_miss.append(dt)

            if status == "direct":
                lat_hit.append(dt)
                n["fuzzy_hit" if res.get("fuzzy") else "exact_hit"] += 1
                pr["hit"] += 1
                pr["hit_fuzzy" if res.get("fuzzy") else "hit_exact"] += 1
                # 参数提取正确性：逐 slot 归一化比对（值一致即正确）
                args = res.get("args") or {}
                slots = gt.get("slots") or {}
                slot_ok = {s: (R.norm_q(str(args.get(s))) == R.norm_q(str(v)))
                           for s, v in slots.items()}
                ok_all = bool(slot_ok) and all(slot_ok.values())
                n["param_ok" if ok_all else "param_bad"] += 1
                pr["param_ok" if ok_all else "param_bad"] += 1
                rec["param_check"] = slot_ok
                rec["param_ok"] = ok_all
                # 判分：配方 fn 按 gt 参数独立重算 expected → judge_payload 三段断言
                try:
                    rmeta = next(r for r in ledger["recipes"]
                                 if r["id"] == res["recipe"])
                    argv = [R.norm_q(str(slots[p])) for p in rmeta["params"]]
                    expected = recipe_fn(res["recipe"], rmeta["fn"])(*argv)
                    judge_ok = R.judge_payload(res.get("payload"), expected)
                except Exception as e:   # fail-closed
                    judge_ok = False
                    anomalies.append({"case_id": case["case_id"],
                                      "text": case["text"],
                                      "error": "judge: %s: %s"
                                               % (type(e).__name__, e)})
                n["judge_pass" if judge_ok else "judge_fail"] += 1
                pr["judge_pass" if judge_ok else "judge_fail"] += 1
                rec["judge_pass"] = judge_ok
                hit_store.append({"case_id": case["case_id"], "text": case["text"],
                                  "recipe_id": res["recipe"],
                                  "subtype": case.get("subtype"),
                                  "fuzzy": bool(res.get("fuzzy")),
                                  "param_ok": ok_all, "judge_pass": judge_ok,
                                  "payload": res.get("payload"),
                                  "slots": slots})
            elif case["category"] == "in_domain":
                n["in_miss"] += 1
                lat_miss.append(dt)
                rec["miss_detail"] = res.get("message") or \
                    json.dumps(res.get("closest"), ensure_ascii=False)
            ff.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if (i + 1) % 2000 == 0:
                out.write("  %d/%d  elapsed=%.1fs\n"
                          % (i + 1, len(cases), time.time() - t_run0))
                out.flush()

    wall = time.time() - t_wall0
    n_total = len(cases)
    n_in = sum(c["category"] == "in_domain" for c in cases)
    n_out = n_total - n_in
    n_hit = n["exact_hit"] + n["fuzzy_hit"]

    summary = {
        "seed": 20260925,
        "ledger_version": ledger.get("version"),
        "n_recipes": len(ledger["recipes"]),
        "total": n_total, "in_domain": n_in, "out_domain": n_out,
        "in_hit": n_hit, "in_hit_rate": round(n_hit / n_in, 4) if n_in else None,
        "exact_hits": n["exact_hit"], "fuzzy_hits": n["fuzzy_hit"],
        "in_miss": n["in_miss"],
        "judge_pass": n["judge_pass"], "judge_fail": n["judge_fail"],
        "judge_scope": "全量判分（无抽样）；口径=runtime_media.judge_payload 三段断言，"
                       "expected=账本配方 fn 按 ground truth 参数进程内独立重算",
        "judge_pass_rate": round(n["judge_pass"] / n_hit, 4) if n_hit else None,
        "param_checked": n["param_ok"] + n["param_bad"],
        "param_ok": n["param_ok"],
        "param_acc": round(n["param_ok"] / (n["param_ok"] + n["param_bad"]), 4)
                     if (n["param_ok"] + n["param_bad"]) else None,
        "far_n_out": n_out, "far_leaks": len(far_leaks),
        "far_leak_samples": far_leaks[:20],
        "ood_status_by_subtype": ood_status,
        "latency_all": latency_block(lat_all),
        "latency_hit": latency_block(lat_hit),
        "latency_miss": latency_block(lat_miss),
        "per_recipe": {k: {**v, "lat_mean": round(v["lat_sum"] / v["n"], 3)
                           if v["n"] else None}
                       for k, v in per_recipe.items()},
        "anomalies": anomalies,
        "wall_seconds": round(wall, 1),
        "run_finished": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    # ---------------- 真调抽样清单（只生成清单，绝不执行真调） ----------------
    build_manifest(hit_store, summary)

    write_report(summary, n_total)
    out.write(json.dumps({k: v for k, v in summary.items()
                          if k not in ("per_recipe", "anomalies",
                                       "far_leak_samples")},
                         ensure_ascii=False, indent=1) + "\n")
    out.write("PER-RECIPE: %s\n" % json.dumps(summary["per_recipe"],
                                              ensure_ascii=False))
    out.write("ANOMALIES: %d\n" % len(anomalies))
    out.write("wall=%.1fs done\n" % wall)
    out.flush()
    return 0


def build_manifest(hit_store, summary):
    """从命中题选 30 图 + 3 视频，覆盖全部 13 配方与 4 个 adapter 意图。"""
    by_rid = {}
    for h in hit_store:   # 保持 case 顺序；优先 exact、判分过、参数对
        if not (h["judge_pass"] and h["param_ok"]):
            continue
        by_rid.setdefault(h["recipe_id"], []).append(h)

    def pick(rid, k):
        pool = by_rid.get(rid, [])
        pool.sort(key=lambda h: (h["fuzzy"], h["case_id"]))
        return pool[:k]

    image_rids = ["r001", "r002", "r003", "r004", "r005", "r006", "r007",
                  "r011", "r012", "r013"]
    video_rids = ["r008", "r009", "r010"]
    entries, intent_cov = [], {"ark_image": 0, "ark_video": 0,
                               "minimax_image": 0, "minimax_video": 0}
    mm_image_given = 0
    for rid in image_rids:
        for h in pick(rid, N_IMAGE_MANIFEST // len(image_rids)):
            e = {"kind": "image", "case_id": h["case_id"], "text": h["text"],
                 "recipe_id": rid, "slots": h["slots"],
                 "payload": h["payload"], "adapter_variants": {}}
            intent_cov["ark_image"] += 1
            # minimax_image 意图：前 2 条给 adapter 覆盖变体（--adapter 同款口径，
            # 模型名由 adapters.call_payload 的跨后端路由负责）
            if mm_image_given < 2:
                e["adapter_variants"]["minimax_image"] = dict(
                    h["payload"], adapter="minimax_image")
                intent_cov["minimax_image"] += 1
                mm_image_given += 1
            entries.append(e)
    mm_video_given = 0
    for rid in video_rids:
        for h in pick(rid, N_VIDEO_PER_RECIPE):
            e = {"kind": "video", "case_id": h["case_id"], "text": h["text"],
                 "recipe_id": rid, "slots": h["slots"],
                 "payload": h["payload"], "adapter_variants": {}}
            intent_cov["ark_video"] += 1
            if mm_video_given < 2:
                e["adapter_variants"]["minimax_video"] = dict(
                    h["payload"], adapter="minimax_video")
                intent_cov["minimax_video"] += 1
                mm_video_given += 1
            entries.append(e)

    manifest = {
        "purpose": "本清单供主会话带 key 真调；benchmark 自身零真调、零 key、"
                   "零 LLM 调用、零网络请求——全部结果为进程内跑引擎所得。",
        "source": "benchmark_full.jsonl（case_id 可回溯逐题记录）",
        "n_image": sum(1 for e in entries if e["kind"] == "image"),
        "n_video": sum(1 for e in entries if e["kind"] == "video"),
        "recipe_coverage": sorted({e["recipe_id"] for e in entries}),
        "n_recipes_covered": len({e["recipe_id"] for e in entries}),
        "adapter_intent_coverage": intent_cov,
        "adapter_note": "ark_* 为 payload 原生意图；minimax_* 为 adapter 覆盖变体"
                        "（照搬 runtime_media --adapter 口径），调用层模型名由 "
                        "adapters.call_payload 跨后端路由负责换名。真调时 key 只从"
                        "环境变量 ARK_API_KEY / MINIMAX_API_KEY 读取。",
        "entries": entries,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    summary["manifest"] = {"n_image": manifest["n_image"],
                           "n_video": manifest["n_video"],
                           "n_entries": len(entries),
                           "recipes_covered": manifest["recipe_coverage"],
                           "n_recipes_covered": manifest["n_recipes_covered"],
                           "adapter_intent_coverage": intent_cov}


def write_report(s, n_total):
    pr = s["per_recipe"]
    rids = sorted(k for k in pr.keys() if k)
    rows = "\n".join(
        "| %s | %d | %d | %d | %d | %d | %.1f |" %
        (k, pr[k]["n"], pr[k]["hit"], pr[k]["hit_exact"], pr[k]["hit_fuzzy"],
         pr[k]["judge_pass"], pr[k]["lat_mean"])
        for k in rids)
    lat = s["latency_all"]

    def fmt(b):
        return ("%d 题，mean %.1fms，p50 %.1f，p90 %.1f，p99 %.1f，max %.1f"
                % (b["n"], b["mean"], b["p50"], b["p90"], b["p99"], b["max"])) \
            if b.get("n") else "0 题"

    anom = ("\n".join("- `%s`" % json.dumps(a, ensure_ascii=False)
                      for a in s["anomalies"]) or "（无）") \
        if len(s["anomalies"]) <= 50 else \
        ("\n".join("- `%s`" % json.dumps(a, ensure_ascii=False)
                   for a in s["anomalies"][:50])
         + "\n- …（另有 %d 条，见 benchmark_summary.json anomalies 全量）"
           % (len(s["anomalies"]) - 50))
    leak = ("\n".join("- `%s`" % json.dumps(a, ensure_ascii=False)
                      for a in s["far_leak_samples"]) or "（无——FAR=0）")
    ood_rows = "\n".join(
        "| %s | %d | %s |" % (st, sum(cnt.values()),
                              "、".join("%s=%d" % (k2, v2)
                                        for k2, v2 in sorted(cnt.items())))
        for st, cnt in sorted(s["ood_status_by_subtype"].items()))

    md = """# MediaLedger 开源主包 100 倍规模 Benchmark 报告

- 账本版本：{ledger_version}（{n_recipes} 配方 / 70 骨架）
- 生成 seed：{seed}（随机可复现）
- 运行方式：进程内 import runtime_media，账本一次加载，逐题调 `ask()`，每题不起子进程
  （配方沙箱 subprocess 为引擎自身设计，原样保留计时）。
- 纪律：零 API 真调、零 key、零 LLM 调用、零网络请求；引擎与 state/ 未做任何改动。
- 全量跑批耗时：{wall} 秒；完成时间：{finished}

## 1. 总题量与构成

| 构成 | 题量 |
|---|---|
| 总题量 | {total} |
| 域内（账本骨架 × 参数实例） | {ind} |
| ├ exact 直出形态 | {in_exact} |
| ├ wrapped（句首/句尾寒暄包裹，走 L2 模糊通道口径） | {in_wrap} |
| 账外（FAR 层，全部应诚实回退） | {outd} |

账外构成见第 5 节。域内实例化时每条题的生成参数真值（骨架/配方/slot 填值）随题落盘；
exact 实例均经引擎 `seg_match(strict=True)` 真值校验，wrapped 经 `strict=False` 校验，
保证 ground truth 与模板切槽口径一致（仅校验，不改引擎）。

## 2. 域内命中率（exact vs fuzzy）

| 指标 | 数值 |
|---|---|
| 域内题量 | {ind} |
| 命中（status=direct） | {hit} |
| **域内命中率** | **{hit_rate}** |
| ├ exact（L1 分段锚定，fuzzy=false） | {eh} |
| └ fuzzy（L2 模糊召回+锚定，fuzzy=true） | {fh} |
| 未命中（near_miss / fallback / error） | {miss} |

说明：exact 实例按骨架逐字措辞生成，理论走 L1；wrapped 变体刻意破坏首尾对齐，
考察 L2 模糊通道的召回与切槽，未中（near_miss）属引擎诚实回退行为，如实计为未命中。

## 3. 判分通过率（含口径说明）

| 指标 | 数值 |
|---|---|
| 命中题 | {hit} |
| 判分通过 | {jp} |
| 判分未过 | {jf} |
| **判分通过率** | **{jpr}**（口径：**{jscope}**） |

## 4. 参数提取正确率

| 指标 | 数值 |
|---|---|
| 域内命中题（参与比对） | {pchk} |
| 逐 slot 全对 | {pok} |
| **参数提取正确率** | **{pacc}** |

比对口径：提取 args 与 ground truth slots 逐 slot 经 `norm_q` 归一化后字符串全等。

## 5. FAR：账外泄漏读数

- 账外题量：{outd}；**泄漏数（status=direct 或带 payload）：{leaks}**
- 泄漏判定口径与 tests/test_selftest.py `far_sentinels` 完全一致。
- 账外分型状态分布：

| 分型 | 题量 | 状态分布 |
|---|---|---|
{ood_rows}

泄漏样本（应为空，非空则如实记录，不改引擎不掩盖）：

{leak_block}

## 6. 延迟分位数表（ask() 单题耗时，ms）

| 范围 | n | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|
| 全部题 | {lat_n} | {lat_mean} | {lat_p50} | {lat_p90} | {lat_p99} | {lat_max} |
| 命中题（direct） | {lhit} |
| 未命中/账外题 | {lmiss} |

命中延迟主要由引擎配方沙箱 subprocess（python 子进程启动）构成，属引擎自身设计。

## 7. 按配方分布表

| 配方 | 题数 | 命中数 | exact 命中 | fuzzy 命中 | 判分通过 | 平均延迟 ms |
|---|---|---|---|---|---|---|
{rows}

## 8. 异常清单（fail-closed，如实记录，引擎不改）

{anom}

## 9. 真调抽样清单（live_call_manifest.json）

- 规模：{m_img} 条图 + {m_vid} 条视频，共 {m_n} 条；覆盖配方 {m_rc}（{m_nr}/13）。
- adapter 意图覆盖：{m_int}。
- 清单头部声明：本清单供主会话带 key 真调；benchmark 自身零真调。
- 每条含：原始需求句 + 完整 payload + recipe_id + 对应 benchmark_full.jsonl 的 case_id。

## 10. 复现方式

```bash
python benchmark/gen_benchmark.py    # seed=20260925 生成题目
python benchmark/run_benchmark.py    # 进程内全量跑批并产出本报告
python runtime_media.py --selftest --out tests/selftest_after_benchmark.json
```
""".format(
        ledger_version=s["ledger_version"], n_recipes=s["n_recipes"],
        seed=s["seed"], wall=s["wall_seconds"], finished=s["run_finished"],
        total=s["total"], ind=s["in_domain"], outd=s["out_domain"],
        in_exact=IN_EXACT[0], in_wrap=IN_EXACT[1], hit=s["in_hit"], hit_rate=s["in_hit_rate"],
        eh=s["exact_hits"], fh=s["fuzzy_hits"], miss=s["in_miss"],
        jp=s["judge_pass"], jf=s["judge_fail"], jpr=s["judge_pass_rate"],
        jscope=s["judge_scope"], pchk=s["param_checked"], pok=s["param_ok"],
        pacc=s["param_acc"], leaks=s["far_leaks"], ood_rows=ood_rows,
        leak_block=leak,
        lat_n=lat["n"], lat_mean=lat["mean"], lat_p50=lat["p50"],
        lat_p90=lat["p90"], lat_p99=lat["p99"], lat_max=lat["max"],
        lhit=fmt(s["latency_hit"]), lmiss=fmt(s["latency_miss"]),
        rows=rows, anom=anom,
        m_img=s["manifest"]["n_image"], m_vid=s["manifest"]["n_video"],
        m_n=s["manifest"]["n_entries"], m_rc="、".join(s["manifest"]["recipes_covered"]),
        m_nr=s["manifest"]["n_recipes_covered"],
        m_int=json.dumps(s["manifest"]["adapter_intent_coverage"], ensure_ascii=False),
    )
    REPORT_PATH.write_text(md, encoding="utf-8")


# 报告第 1 节的 exact/wrapped 拆分数（由生成摘要读入，占位在 main 填充）
IN_EXACT = [0, 0]


if __name__ == "__main__":
    casessum = HERE / "benchmark_cases_summary.json"
    if casessum.exists():
        cs = json.loads(casessum.read_text(encoding="utf-8"))
        IN_EXACT[0] = cs["in_exact"]
        IN_EXACT[1] = cs["in_wrapped"]
    sys.exit(main())
