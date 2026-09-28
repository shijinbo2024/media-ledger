# MediaLedger 100 倍规模 Benchmark 报告

- 账本版本：media-ledger v1（13 配方 / 70 骨架）
- 生成 seed：20260925（随机可复现）
- 运行方式：进程内 import runtime_media，账本一次加载，逐题调 `ask()`，每题不起子进程
  （配方沙箱 subprocess 为引擎自身设计，原样保留计时）。
- 纪律：零 API 真调、零 key、零 LLM 调用、零网络请求；引擎与 state/ 未做任何改动。
- 全量跑批耗时：849.3 秒；完成时间：2026-09-25 13:48:36

## 1. 总题量与构成

| 构成 | 题量 |
|---|---|
| 总题量 | 14058 |
| 域内（账本骨架 × 参数实例） | 13058 |
| ├ exact 直出形态 | 12518 |
| ├ wrapped（句首/句尾寒暄包裹，走 L2 模糊通道口径） | 540 |
| 账外（FAR 层，全部应诚实回退） | 1000 |

账外构成见第 5 节。域内实例化时每条题的生成参数真值（骨架/配方/slot 填值）随题落盘；
exact 实例均经引擎 `seg_match(strict=True)` 真值校验，wrapped 经 `strict=False` 校验，
保证 ground truth 与模板切槽口径一致（仅校验，不改引擎）。

## 2. 域内命中率（exact vs fuzzy）

| 指标 | 数值 |
|---|---|
| 域内题量 | 13058 |
| 命中（status=direct） | 12528 |
| **域内命中率** | **0.9594** |
| ├ exact（L1 分段锚定，fuzzy=false） | 12518 |
| └ fuzzy（L2 模糊召回+锚定，fuzzy=true） | 10 |
| 未命中（near_miss / fallback / error） | 530 |

说明：exact 实例按骨架逐字措辞生成，理论走 L1；wrapped 变体刻意破坏首尾对齐，
考察 L2 模糊通道的召回与切槽，未中（near_miss）属引擎诚实回退行为，如实计为未命中。

## 3. 判分通过率（含口径说明）

| 指标 | 数值 |
|---|---|
| 命中题 | 12528 |
| 判分通过 | 12528 |
| 判分未过 | 0 |
| **判分通过率** | **1.0**（口径：**全量判分（无抽样）；口径=runtime_media.judge_payload 三段断言，expected=账本配方 fn 按 ground truth 参数进程内独立重算**） |

## 4. 参数提取正确率

| 指标 | 数值 |
|---|---|
| 域内命中题（参与比对） | 12528 |
| 逐 slot 全对 | 12528 |
| **参数提取正确率** | **1.0** |

比对口径：提取 args 与 ground truth slots 逐 slot 经 `norm_q` 归一化后字符串全等。

## 5. FAR：账外泄漏读数

- 账外题量：1000；**泄漏数（status=direct 或带 payload）：0**
- 泄漏判定口径与 tests/test_selftest.py `far_sentinels` 完全一致。
- 账外分型状态分布：

| 分型 | 题量 | 状态分布 |
|---|---|---|
| chitchat | 200 | fallback=200 |
| cross_domain_generate | 300 | fallback=300 |
| injection | 200 | fallback=200 |
| off_topic_tool | 300 | fallback=300 |

泄漏样本（应为空，非空则如实记录，不改引擎不掩盖）：

（无——FAR=0）

## 6. 延迟分位数表（ask() 单题耗时，ms）

| 范围 | n | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|
| 全部题 | 14058 | 60.335 | 65.482 | 72.837 | 92.463 | 455.207 |
| 命中题（direct） | 12528 题，mean 67.5ms，p50 66.0，p90 73.4，p99 93.9，max 455.2 |
| 未命中/账外题 | 1530 题，mean 1.6ms，p50 1.5，p90 1.9，p99 2.6，max 3.2 |

命中延迟主要由引擎配方沙箱 subprocess（python 子进程启动）构成，属引擎自身设计。

## 7. 按配方分布表

| 配方 | 题数 | 命中数 | exact 命中 | fuzzy 命中 | 判分通过 | 平均延迟 ms |
|---|---|---|---|---|---|---|
| r001 | 1104 | 1074 | 1074 | 0 | 1074 | 65.7 |
| r002 | 1134 | 1074 | 1074 | 0 | 1074 | 64.3 |
| r003 | 1104 | 1074 | 1074 | 0 | 1074 | 65.7 |
| r004 | 923 | 883 | 883 | 0 | 883 | 64.4 |
| r005 | 935 | 895 | 895 | 0 | 895 | 64.3 |
| r006 | 935 | 895 | 895 | 0 | 895 | 64.7 |
| r007 | 945 | 895 | 895 | 0 | 895 | 64.3 |
| r008 | 945 | 895 | 895 | 0 | 895 | 63.9 |
| r009 | 945 | 895 | 895 | 0 | 895 | 63.8 |
| r010 | 1134 | 1084 | 1074 | 10 | 1084 | 64.5 |
| r011 | 925 | 895 | 895 | 0 | 895 | 65.5 |
| r012 | 1104 | 1074 | 1074 | 0 | 1074 | 65.9 |
| r013 | 925 | 895 | 895 | 0 | 895 | 65.6 |

## 8. 异常清单（fail-closed，如实记录，引擎不改）

（无）

## 9. 真调抽样清单（live_call_manifest.json）

- 规模：30 条图 + 3 条视频，共 33 条；覆盖配方 r001、r002、r003、r004、r005、r006、r007、r008、r009、r010、r011、r012、r013（13/13）。
- adapter 意图覆盖：{"ark_image": 30, "ark_video": 3, "minimax_image": 2, "minimax_video": 2}。
- 清单头部声明：本清单供人工带 key 真调；benchmark 自身零真调。
- 每条含：原始需求句 + 完整 payload + recipe_id + 对应 benchmark_full.jsonl 的 case_id。

## 9.5 真调抽样结果（live_calls_result.json + live_calls_retry.json，2026-09-25）

- 28 条（25 图 + 3 视频），全走 MiniMax 后端（`--adapter minimax_image/minimax_video` 覆盖），key 只读环境变量
- **图 25/25 全过**：首轮 19/25，6 张失败根因=aspect_ratio 枚举外被服务端**静默吞图**（如 720x1920→"3:8" 不在 MiniMax 合法枚举内，返回 success+0 图）；白名单吸附修复后 retry 6/6 全部救回（修复随包于 `benchmark/minimax_backend.py`，附 `aspect_ratio_used` 审计字段）
- **视频 1/3**：r008 出片；2 条失败=服务端 Token Plan 用量上限（base_resp status_code 2067，非 payload 构造缺陷；其中 r010 另含生成器假示例 URL 的数据局限，如实声明）
- 修复后综合成功口径：**26/28 = 92.9%**；服务端静默失败（success+空产物）是真实 API 的主要风险面，fail-closed 空产物拦截与该修复即为此设计
- 产物归档：live_cache/（抽样图片/视频文件，供人工抽查质量）
- 说明：真调原始产物与响应文件（含账号相关的下载 URL）未随开源包分发，
  数字以本节为准；按第 10 节命令 + `run_live_calls.py` 可完整复现

## 10. 对照组：不用账本直接跑（LLM 编排基线）

- 与主 benchmark 同题库确定性抽样 350 题（exact 200 + wrapped 50 + 账外 100）的对照测量：
  无账本时每题需向 LLM 发送「API 编排系统提示（估算 518 token）+ 需求句」并生成 payload。
- **token：对照组合计约 210,355 token（估算口径 ±30%），账本路径 0 token，编排层节省 100%**。
- **时间：账本实测 p50 66.1 ms（域内命中）/ 1.3 ms（账外回退）**；对照组延迟为参数化假设
  （p50 1500 ms），按假设节省 95.6% / 99.9%——量级结论（毫秒级 vs 秒级）稳定成立。
- 详见 [CONTROL_GROUP.md](CONTROL_GROUP.md)（含口径、假设标注与复现命令）。

## 11. 复现方式

```bash
python benchmark/gen_benchmark.py    # seed=20260925 生成题目
python benchmark/run_benchmark.py    # 进程内全量跑批并产出本报告
python runtime_media.py --selftest --out tests/selftest_result.json
# 真调抽样（可选，需 MINIMAX_API_KEY）：python benchmark/run_live_calls.py
```
