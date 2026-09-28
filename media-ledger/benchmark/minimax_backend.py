# -*- coding: utf-8 -*-
"""minimax_backend.py — MiniMax（海螺 image-01 / video-01）真调后端，仅供 benchmark 复现。

只服务 benchmark/run_live_calls.py 的成本层验证（BENCHMARK.md 第 9.5 节复现）：
主包 adapters.py 不含此第二后端；本模块随 benchmark 分发，口径与开源报告一致。
实测接口口径（2026-09-25 探测）：POST /v1/image_generation {model,prompt,num_inference_steps,
aspect_ratio?} → data.image_urls[0]；POST /v1/video_generation {model,prompt,
first_frame_image?} → task_id；GET /v1/query/video_generation?task_id= → status
Preparing/Processing/Success/Fail，终态 file.download_url。key 只读 MINIMAX_API_KEY（绝不落盘）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

import adapters  # noqa: E402  复用缓存层/模型名解析/下载工具

BASE = "https://api.minimaxi.com/v1"
TIMEOUT = adapters.TIMEOUT
POLL_INTERVAL = adapters.POLL_INTERVAL
POLL_TIMEOUT = adapters.POLL_TIMEOUT


def _post(path, body, api_key, timeout=TIMEOUT):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, api_key, timeout=30):
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 跨后端模型路由：配方按 Ark 短名写，--adapter 覆盖成 minimax_* 时同步换模型名。
# 注意 MiniMax 对未知 model 静默返回 success+0 图（不报错）——路由缺失=静默空产物，故必须在此换。
_CROSS_BACKEND_MODEL = {
    "doubao-seedream-4-0": "image-01",
    "doubao-seedance-1-0-pro": "video-01",
    "doubao-seedance-1-0-lite": "video-01",
}

_AR_WHITELIST = ("1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2", "21:9")


def _aspect_ratio(size):
    """'1080x1920' → '9:16'。实测 MiniMax image-01 只认有限比例枚举，枚举外
    （如 720x1920→3:8）静默返 0 图——故吸附到长宽比最接近的合法枚举，保持
    横/竖版意图；解析失败返回 None（不传，服务端默认）。"""
    try:
        w, h = (int(x) for x in str(size).lower().split("x"))
    except Exception:
        return None
    g = __import__("math").gcd(w, h)
    ar = "%d:%d" % (w // g, h // g)
    if ar in _AR_WHITELIST:
        return ar
    if w == 0 or h == 0:
        return None
    best = min(_AR_WHITELIST,
               key=lambda a: abs(w / h - int(a.split(":")[0]) / int(a.split(":")[1])))
    return best


def generate_image(payload, api_key):
    body = {"model": adapters.resolve_model(payload["model"]), "prompt": payload["prompt"],
            "num_inference_steps": 20}
    ar = _aspect_ratio(payload.get("size"))
    if ar:
        body["aspect_ratio"] = ar
    out = _post("/image_generation", body, api_key)
    urls = (out.get("data") or {}).get("image_urls") or []
    if not urls:   # MiniMax 对未知 model/非法比例静默返 0 图——空产物按失败处理，不缓存
        return {"ok": False, "error": "no_image_returned",
                "detail": out.get("base_resp"), "model": body["model"],
                "aspect_ratio_used": ar}
    return {"kind": "image", "model": body["model"], "url": urls[0],
            "n_images": len(urls), "aspect_ratio_used": ar}


def create_video(payload, api_key):
    # 剥掉 Seedance 口径的 prompt 后缀指令（--resolution 720p 等）——MiniMax 当普通文字
    text = re.sub(r"\s+--\w+\s+\S+", " ", payload["text"]).strip()
    body = {"model": adapters.resolve_model(payload["model"]), "prompt": text}
    if payload.get("image_url"):
        body["first_frame_image"] = payload["image_url"]
    out = _post("/video_generation", body, api_key)
    return {"kind": "video_task", "model": body["model"], "task_id": out.get("task_id"),
            "base_resp": out.get("base_resp")}


_TERMINAL = ("Success", "success", "Fail", "fail", "Failed")


def poll_video(task_id, api_key, timeout_s=POLL_TIMEOUT, interval_s=POLL_INTERVAL):
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        last = _get("/query/video_generation?task_id=" + task_id, api_key)
        if last.get("status") in _TERMINAL:
            break
        time.sleep(interval_s)
    f = last.get("file") or {}
    url = f.get("download_url")
    file_id = f.get("file_id") or last.get("file_id")
    if not url and file_id:   # 实测口径：Success 无 file.download_url → 文件接口取
        fr = _get("/files/retrieve?file_id=%s" % file_id, api_key)
        url = (fr.get("file") or {}).get("download_url")
    return {"kind": "video", "task_id": task_id, "status": last.get("status"),
            "video_url": url, "file_id": file_id}


def call_payload(payload, api_key=None, cache_dir=None, poll_timeout_s=POLL_TIMEOUT):
    """按 payload['adapter'] 分发 MiniMax 真调（minimax_image / minimax_video）；
    先查缓存（同参数直接复用）。跨后端路由：配方 model（Ark 短名）→ MiniMax 对应模型。"""
    adapter = payload.get("adapter")
    api_key = api_key or os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        return {"ok": False, "error": "no_api_key",
                "message": "未设置 MINIMAX_API_KEY——不跑真调。"}
    payload = dict(payload, model=_CROSS_BACKEND_MODEL.get(payload.get("model"),
                                                           payload.get("model")))
    hit = adapters.cache_get(payload, cache_dir)
    if hit and hit.get("result", {}).get("ok"):
        return dict(hit["result"], cached=True, cache_key=adapters.canon_hash(payload))
    try:
        if adapter == "minimax_image":
            res = {"ok": True, **generate_image(payload, api_key)}
        elif adapter == "minimax_video":
            t = create_video(payload, api_key)
            if not t.get("task_id"):
                return {"ok": False, "error": "no_task_id", "detail": t}
            v = poll_video(t["task_id"], api_key, timeout_s=poll_timeout_s)
            if not v.get("video_url"):
                return {"ok": False, "error": "no_video_url", "detail": v}
            res = {"ok": True, **v}
        else:
            return {"ok": False, "error": "unknown_adapter", "adapter": adapter}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "http_%d" % e.code,
                "detail": e.read().decode("utf-8", "replace")[:500]}
    except Exception as e:   # fail-closed：异常原样上报，不静默
        return {"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}
    if res.get("ok"):
        adapters.cache_put(payload, res, cache_dir)
    return res
