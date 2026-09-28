# MediaLedger — 省流多模态生成 MCP

[English](README_EN.md) | 中文

> 中文生成需求句 → 骨架匹配（零 LLM）→ 直出生成 API 调用 payload。
> 命中秒出、确定性、可判分；账外诚实回退，绝不硬猜（FAR=0）。

## 特性

- **零依赖**：纯 Python 标准库（3.8+），克隆即用
- **骨架匹配**：措辞逐字、参数挖空；未中走 bigram 模糊召回，歧义平局不硬猜
- **两档执行**：默认 dry 只出 payload（不花钱）；`--call` 真调 Ark 生成 API
  （key 只读环境变量 `ARK_API_KEY`，绝不落盘）；同参数结果自动缓存复用
- **teach 教账**：注入闸 + 沙箱自检 + 原子入账，秒级生效
- **自测网页**：`index.html` 纯静态双击即用（内嵌 JS 引擎，见下）

## 快速开始

```bash
python runtime_media.py --ask "生成一张1080x1920的赛博朋克风格城市夜景封面图"   # dry 出 payload
python runtime_media.py --ask "画一个卡通的橘猫方形头像" --call               # 真调（需 ARK_API_KEY）
python runtime_media.py --teach-file my_recipe.json                          # 教账（格式见 --help）
python runtime_media.py --list | --stats | --selftest                        # 列表 / 统计 / 验收自测
start index.html   # Windows；macOS: open index.html —— 纯静态自测面板
```

## MCP 部署

零依赖 stdio server，任意 MCP 客户端可接。客户端配置文件（如 Claude Desktop 的
`claude_desktop_config.json`）加：

```json
{
  "mcpServers": {
    "media_ledger": {
      "command": "python",
      "args": ["/path/to/media-ledger/mcp_server_media.py"]
    }
  }
}
```

其他客户端（Cline / Cursor 等）手动添加 stdio server：`command = python`，
`args = ["/path/to/media-ledger/mcp_server_media.py"]`。

连接后 `tools/list` 可见三工具：

| 工具 | 入参 | 行为 |
|---|---|---|
| `ask_media` | `question` | dry 出 payload（确定性、可审计，不花钱） |
| `teach_media` | `question, code, tests, patterns?` | 教账：注入闸+判分自检通过才入账 |
| `stats_media` | – | 账本统计 |

MCP 通道只出 payload 省流；真调用 CLI `--call`。Windows 中文已内置 UTF-8 reconfigure。

### 让 Agent 主动调用

MCP 工具由模型自主决定是否调用，部分 harness 不会默认触发。本 server 已带协议级
`instructions` 与工具描述触发条件；需要强制工作流时，把下面一段贴进
`CLAUDE.md` / `AGENTS.md` / `.cursorrules` 即可：

```text
凡任务涉及生成图片/视频的 API 请求构造：先调用 MCP 工具 media_ledger 的
ask_media（入参=用户需求原话），命中即用返回的 payload，不要手写请求体；
未命中（near_miss/fallback）则如实说明，再按常规方式处理。ask_media 只出
payload 不真调；真调用 CLI：python runtime_media.py --ask "..." --call。
```

## 自测网页（纯静态）

双击 `index.html` 直接运行，无需 server、无需联网：

- **① 验收自测**：内嵌 JS 引擎实时计算（种子命中+判分 / 账外回退 / 参数泛化）；
  teach 闸门与 MCP 冒烟属 CLI 专属判据，页面记 SKIP
- **② API 本地自测**：填一个通用 API Key 即可浏览器直连真调一次（若被 CORS 跨域拦截，
  请改用 CLI `--call`）；Key 只在本页内存，绝不写盘

## 账本：可生成、可增长

账本（`state/media_ledger.json`）不是写死的配置，而是随使用生长的数据：

- **可生成**：配方模板不用手写——teach 一句带真实参数的需求原话 + 一个单行函数 +
  ≥2 组断言用例，模板骨架自动归纳生成，判分自检 + 注入闸通过后秒级入账；
  从空账本即可自举（教一条、命中一条）。
- **可增长**：随时教新配方扩容；老配方可持续追加措辞族（同配方不同说法都能命中）；
  账本为纯 JSON、原子写入，可版本化、可迁移、可回滚。

入口：CLI `python runtime_media.py --teach-file my_recipe.json` / `--add-pattern-file`；
MCP `teach_media` 工具同口径。

## 诚实边界

- 省的是编排 token 与废生成重试，**不替代生成模型本身**；判分只保证 payload 构造正确
- 骨架匹配措辞敏感，泛化靠措辞族养账 + 模糊召回（有阈值），超出即诚实回退
- `ARK_API_KEY` 只从环境变量读，绝不落盘/入账/入缓存

## 目录

```
media-ledger/
  runtime_media.py     运行时（骨架匹配/沙箱/teach 闸/selftest CLI）
  mcp_server_media.py  MCP stdio server（ask_media/teach_media/stats_media）
  adapters.py          Ark adapter + 参数哈希缓存层（urllib；其他多模态生成模型可按同口径扩展）
  index.html           自测网页（纯静态，双击即用）
  state/media_ledger.json  配方账（13 条种子 + teach 增量）
  tests/test_selftest.py   验收判据（--selftest 全绿）
  benchmark/           100 倍规模 benchmark（生成器/跑批器/真调抽样 + 报告）
```

## License

MIT
