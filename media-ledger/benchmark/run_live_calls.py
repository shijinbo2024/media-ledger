# -*- coding: utf-8 -*-
"""run_live_calls.py — benchmark 真调抽样层：按 live_call_manifest.json 逐条真调。

零 LLM；生成 API 走 MiniMax（minimax_image/minimax_video，--adapter 覆盖），
后端实现见本目录 minimax_backend.py（主包 adapters.py 不含此第二后端），
key 只读环境变量 MINIMAX_API_KEY（绝不落盘）。产物与结果落 benchmark/live_cache/
与 benchmark/live_calls_result.json。跑批层（匹配/判分）之外的成本层验证：
命中 payload 在真实 API 上的一次过率。
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import adapters  # noqa: E402  仅用其 download 工具
import minimax_backend  # noqa: E402  MiniMax 真调实现（benchmark 复现专用）
import runtime_media as R  # noqa: E402

MANIFEST = HERE / "live_call_manifest.json"
OUT_DIR = HERE / "live_cache"
RESULT = HERE / "live_calls_result.json"

ADAPTER_BY_KIND = {"image": "minimax_image", "video": "minimax_video"}


def main():
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = man["entries"]
    retry = "retry_failed" in sys.argv
    if retry:   # 只重跑上轮 ok=false 的条目，结果落 live_calls_retry.json
        prev = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.exists() else {"rows": []}
        failed_ids = {r["case_id"] for r in prev.get("rows", []) if not r.get("ok")}
        entries = [e for e in entries if e["case_id"] in failed_ids]
        print("retry %d failed: %s" % (len(entries), sorted(failed_ids)), flush=True)
    key = __import__("os").environ.get("MINIMAX_API_KEY")
    if not key:
        print("MINIMAX_API_KEY 未设置——不跑真调。", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(exist_ok=True)
    ledger = R.load_ledger(ROOT / "state" / "media_ledger.json")
    rows = []
    for e in entries:
        adapter = ADAPTER_BY_KIND[e["kind"]]
        t0 = time.time()
        out = R.ask(ledger, e["text"])
        if out.get("status") != "direct":
            rows.append({"case_id": e["case_id"], "ask_status": out.get("status"),
                         "ok": False, "error": "ask_not_direct"})
            continue
        payload = dict(out["payload"], adapter=adapter)
        res = minimax_backend.call_payload(payload, key, cache_dir=OUT_DIR)
        row = {"case_id": e["case_id"], "kind": e["kind"], "recipe": e.get("recipe_id"),
               "adapter": adapter, "ask_ms": round((time.time() - t0) * 1000, 1),
               "ok": bool(res.get("ok")), "cached": bool(res.get("cached")),
               "error": res.get("error"),
               "url": (res.get("url") or res.get("video_url") or "")[:80]}
        # 产物落地（抽样归档，方便人工抽查质量）
        if row["ok"]:
            url = res.get("url") or res.get("video_url")
            ext = ".jpg" if e["kind"] == "image" else ".mp4"
            try:
                dl = adapters.download(url, OUT_DIR / (e["case_id"] + ext))
                row["download"] = dl.get("ok")
                row["bytes"] = dl.get("bytes")
            except Exception as ex:
                row["download"] = False
                row["download_err"] = str(ex)[:120]
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    ok_n = sum(1 for r in rows if r.get("ok"))
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "retry": retry,
               "n_entries": len(entries), "n_ok": ok_n,
               "ok_rate": round(ok_n / len(entries), 4) if entries else None,
               "by_kind": {}, "rows": rows}
    for kind in ("image", "video"):
        sub = [r for r in rows if r.get("kind") == kind]
        summary["by_kind"][kind] = {"n": len(sub),
                                    "ok": sum(1 for r in sub if r.get("ok"))}
    out_path = HERE / ("live_calls_retry.json" if retry else "live_calls_result.json")
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
