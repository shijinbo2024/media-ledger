# -*- coding: utf-8 -*-
"""adapters.py — Ark（火山引擎方舟）生成 API adapter + 缓存层（零依赖，urllib 实现）。

是什么：把配方 payload 真正打成 Ark HTTP 调用：
  ark_image（doubao-seedream，OpenAI 兼容）：POST /api/v3/images/generations → data[0].url
  ark_video（doubao-seedance，异步任务）：
    POST /api/v3/contents/generations/tasks  body {model, content:[{type:"text",text:"<prompt> --resolution 720p --duration 5 --ratio 9:16 --watermark false"}(+{type:"image_url",image_url:{url}})]}
    → 返回 id；GET /api/v3/contents/generations/tasks/{id} 轮询 status，
      succeeded 后 content.video_url（参数为 seedance-1-0 系的 prompt 后缀指令口径）。

纪律：ARK_API_KEY 只从环境变量读——绝不落盘、绝不入账、绝不入缓存文件。
缓存：canon_hash(payload)（参数规范哈希）→ state/cache/{hash}.json，同参数复用结果（连生成费都省）。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://ark.cn-beijing.volces.com/api/v3"
IMG_PATH = "/images/generations"
VID_TASK_PATH = "/contents/generations/tasks"
TIMEOUT = 120
POLL_INTERVAL = 5
POLL_TIMEOUT = 600

# ---------------- 模型名解析（短名 → 现行全名） ----------------
# Ark 模型 id 带日期后缀且随版本演进——配方 payload 保持人类可读短名（稳定、判分断言友好），
# 调用层解析到现行全名；带 image_url 的 lite 级视频调用自动切 i2v（图生视频）变体。
# 未登记的短名（如用户自建接入点 ep-xxxx）原样透传。
_MODEL_ALIAS = {
    "doubao-seedream-4-0": "doubao-seedream-4-0-250828",
    "doubao-seedance-1-0-pro": "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1-0-lite": "doubao-seedance-1-0-lite-t2v-250428",
}
_MODEL_ALIAS_I2V = {
    "doubao-seedance-1-0-lite": "doubao-seedance-1-0-lite-i2v-250428",
}


def resolve_model(name, has_image=False):
    """短名→现行全名；i2v 场景优先查 i2v 映射。"""
    if has_image and name in _MODEL_ALIAS_I2V:
        return _MODEL_ALIAS_I2V[name]
    return _MODEL_ALIAS.get(name, name)


def _post(path, body, api_key, timeout=TIMEOUT):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, api_key, timeout=30):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 缓存层 ----------------

def canon_hash(payload) -> str:
    """payload 规范化 sha256 前 16 hex（键排序 + ensure_ascii，确定性缓存键）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def cache_get(payload, cache_dir):
    if not cache_dir:
        return None
    p = Path(cache_dir) / (canon_hash(payload) + ".json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def cache_put(payload, result, cache_dir):
    if not cache_dir:
        return
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / (canon_hash(payload) + ".json")
    p.write_text(json.dumps({"payload": payload, "result": result,
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                            ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- 图片：doubao-seedream ----------------

def generate_image(payload, api_key):
    """POST /images/generations（OpenAI 兼容）。payload: {model, prompt, size, response_format?}"""
    model = resolve_model(payload["model"])
    body = {"model": model, "prompt": payload["prompt"],
            "size": payload.get("size", "1024x1024"),
            "response_format": payload.get("response_format", "url")}
    if payload.get("seed") is not None:
        body["seed"] = payload["seed"]
    out = _post(IMG_PATH, body, api_key)
    item = (out.get("data") or [{}])[0]
    return {"kind": "image", "model": body["model"], "size": body["size"],
            "url": item.get("url"), "b64_len": len(item.get("b64_json") or ""),
            "raw_keys": sorted(out.keys())}


# ---------------- 视频：doubao-seedance（异步任务） ----------------

def _video_body(payload):
    """payload → Ark content 数组：text 后缀指令条 + 可选首帧图 image_url 条。"""
    content = [{"type": "text", "text": payload["text"]}]
    if payload.get("image_url"):
        content.append({"type": "image_url", "image_url": {"url": payload["image_url"]}})
    model = resolve_model(payload["model"], has_image=bool(payload.get("image_url")))
    return {"model": model, "content": content}


def create_video_task(payload, api_key):
    body = _video_body(payload)
    out = _post(VID_TASK_PATH, body, api_key)
    return {"kind": "video_task", "model": body["model"],
            "task_id": out.get("id"), "status": out.get("status")}


def poll_video_task(task_id, api_key, timeout_s=POLL_TIMEOUT, interval_s=POLL_INTERVAL):
    """轮询到终态。返回 {kind, task_id, status, video_url?}。"""
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        last = _get(VID_TASK_PATH + "/" + task_id, api_key)
        status = last.get("status")
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(interval_s)
    content = (last.get("content") or {})
    return {"kind": "video", "task_id": task_id, "status": last.get("status"),
            "video_url": content.get("video_url"),
            "last_error": (last.get("error") or {}).get("message") if last.get("error") else None}


# ---------------- 统一分发 ----------------

def call_payload(payload, api_key=None, cache_dir=None, poll_timeout_s=POLL_TIMEOUT):
    """按 payload['adapter'] 分发真调；先查缓存（同参数直接复用，省生成费）。
    key 纪律：key 只从入参/环境变量读取，绝不落盘、绝不入账、绝不入缓存文件。
    其他多模态生成模型后端可按同口径扩展：实现生成函数后在分发处加一支。"""
    adapter = payload.get("adapter")
    api_key = api_key or os.environ.get("ARK_API_KEY")
    if not api_key:
        return {"ok": False, "error": "no_api_key",
                "message": "未设置 ARK_API_KEY——只 dry 出 payload，不花钱。"}
    hit = cache_get(payload, cache_dir)
    if hit and hit.get("result", {}).get("ok"):
        return dict(hit["result"], cached=True, cache_key=canon_hash(payload))
    try:
        if adapter == "ark_image":
            res = {"ok": True, **generate_image(payload, api_key)}
        elif adapter == "ark_video":
            t = create_video_task(payload, api_key)
            if not t.get("task_id"):
                return {"ok": False, "error": "no_task_id", "detail": t}
            v = poll_video_task(t["task_id"], api_key, timeout_s=poll_timeout_s)
            if not v.get("video_url"):   # 终态无产物（Fail/空）不算成功，不缓存
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
        cache_put(payload, res, cache_dir)
    return res


def download(url, path):
    """产物落地（图片/视频文件）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return {"ok": True, "path": str(path), "bytes": path.stat().st_size}
