# -*- coding: utf-8 -*-
"""mcp_server_media.py — MediaLedger stdio MCP 管道件（零依赖，纯标准库）。

是什么：给 MediaLedger 配方账本开 MCP 协议壳：ask_media / teach_media / stats_media
三工具，逐行 JSON-RPC 2.0（UTF-8），逻辑全部转发 runtime_media（骨架匹配/沙箱/闸门零改动）。
不是什么：不是账本实现；不真调 Ark（MCP 通道只出 payload 省流，真调用 CLI --call）。

Windows 中文铁律（2026-09-24 实测）：三行全配 reconfigure——漏 stdin 会让客户端中文
入参变 mojibake（GBK 默认），骨架匹配全 miss。

注册（任意 MCP 客户端 → 手动添加 stdio server）：
  名称 media_ledger ｜ command = python
  args = ["<项目根目录>/mcp_server_media.py"]
"""
import json
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
import runtime_media as R  # noqa: E402  账本本体，唯一逻辑源

SERVER_INFO = {"name": "media_ledger", "version": "0.1.0"}

# MCP 协议原生引导：随 initialize 注入客户端，解决"装了不调"——部分 harness
# 不会默认触发 MCP 工具，靠这段自述触发条件（README 备有粘贴段兜底）。
INSTRUCTIONS = (
    "media_ledger：任务涉及生成图片/视频等多模态 API 请求构造时，优先调用 "
    "ask_media（入参=用户需求原话）直出确定性 payload，命中即用、不手写请求体；"
    "未命中（near_miss/fallback）如实回退不硬凑。本通道只出 payload 不真调，"
    "真调用 CLI --call。"
)

TOOLS = [
    {
        "name": "ask_media",
        "description": "凡任务涉及生成图片/视频等 API 请求构造，必须优先调用本工具"
                       "（入参=用户需求原话），命中即用返回 payload，不要手写请求体。"
                       "问 MediaLedger 多模态生成账本：中文需求句 → 骨架匹配（参数挖空比对）"
                       "→ 命中直出 Ark 生成 API 调用 payload（确定性可审计，不花钱）；"
                       "账外需求诚实回退不硬猜（near_miss 带近似模板提示）。",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string",
                                        "description": "中文生成需求句，如：生成一张1080x1920的赛博朋克风格城市夜景封面图"}},
            "required": ["question"],
        },
    },
    {
        "name": "teach_media",
        "description": "回填账本：需求句（含真实参数的原话）+ 单行函数代码 + 测试用例（≥2 组），"
                       "注入闸+判分沙箱自检通过才入账；返回 taught/reject/dup。"
                       "tests 格式 [[args, expected], ...]，expected 支持 prompt_contains 槽包含断言。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户原话需求句（参数以字面值出现）"},
                "code": {"type": "string", "description": "单行函数代码（def f(args): ...）"},
                "tests": {"type": "array", "description": "[[args, expected], ...]，≥2 组"},
                "patterns": {"type": "array",
                             "description": "可选 fam 措辞族附加模板（同槽名）"},
            },
            "required": ["question", "code", "tests"],
        },
    },
    {
        "name": "stats_media",
        "description": "账本统计：版本、配方数、key 模板数、fam 群、adapters、体量",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _sanitize(x):
    """递归清洗 lone surrogate（毒帧入参），字符串转可安全 UTF-8 编码的文本。"""
    if isinstance(x, str):
        return x.encode("utf-8", "replace").decode("utf-8")
    if isinstance(x, dict):
        return {k: _sanitize(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_sanitize(v) for v in x]
    return x


def call_tool(name, args):
    """唯一分发点：三工具全部转发 runtime_media，无本地逻辑。"""
    args = _sanitize(args or {})
    if name == "ask_media":
        return R.ask(R.load_ledger(), args["question"])
    if name == "teach_media":
        return R.teach(R.load_ledger(), args["question"], args["code"],
                       args["tests"], args.get("patterns"))
    if name == "stats_media":
        return R.stats(R.load_ledger())
    raise KeyError(f"未知工具 {name}")


def dispatch(msg):
    """单条 JSON-RPC 处理：notification 不回；error 统一 -32603 并带明细。"""
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            out = call_tool(params["name"], params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False, indent=1)}]}}
        if mid is None:      # notification（initialized 等）：不回
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"未知方法 {method}"}}
    except Exception as e:   # fail-closed：异常原样上报，不静默
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def main():
    print("media_ledger MCP 管道件启动（逻辑源 runtime_media，dry 模式只出 payload）",
          file=sys.stderr)
    for line in sys.stdin:               # 逐行读，行=消息
        line = line.strip()
        if not line:
            continue
        try:
            resp = dispatch(json.loads(line))
        except Exception as e:           # 帧解析失败也回错，防对端挂死
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"parse: {e}"}}
        try:
            out_line = json.dumps(resp, ensure_ascii=False) + "\n"
        except Exception:                # 序列化兜底：ASCII 帧必可写
            out_line = json.dumps({"jsonrpc": "2.0", "id": resp.get("id"),
                                   "error": {"code": -32603,
                                             "message": "response serialize failed"}},
                                  ensure_ascii=True) + "\n"
        if resp is not None:
            sys.stdout.write(out_line)
            sys.stdout.flush()


if __name__ == "__main__":
    main()
