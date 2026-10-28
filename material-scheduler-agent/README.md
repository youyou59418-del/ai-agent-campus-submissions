# 素材审核与发布排期 Agent

为内容团队审核素材、检查时段冲突并保存**本地排期草稿**。模型根据工具结果选择补查、替代时段、请求确认或阻断；程序强制保留硬性要求、校验保存结果并防止重复草稿。没有真实发布接口。

> **当前交付状态**：58 项工程测试、13 组离线场景、13 组真实模型场景全部通过，CLI 与证据一致性检查通过。真实模型为百炼 `qwen3.8-27b`，15 轮记录共 80 次成功模型调用。所有输入与业务规则均为合成资料；没有真实发布。自动化检查入口见仓库 Actions。

## 评审入口

1. [需求和成功标准](docs/01-requirements.md)
2. [调用流程、模型决策与工程约束](docs/02-solution.md)
3. [实际验证结果及证据](docs/03-evaluation.md)
4. [AI 使用与个人贡献边界](docs/04-ai-usage.md)
5. [复盘和下一步](docs/05-retrospective.md)

真实模型示例：[正常排期](examples/live/normal/trace.1.json)、[冲突后建议替代时段](examples/live/conflict/trace.1.json)、[缺资料后补查](examples/live/missing/trace.1.json)。同结构的 `examples/mock/` 来自手写策略，仅证明离线流程。修正前的失败案例保留在 `examples/live-before-fix/`。

## 环境与快速运行

Python **3.11+**，运行和测试均只用标准库，无第三方运行依赖；本次验证环境为 Windows / Python 3.13.12。从仓库根目录执行：

```powershell
python verify.py
python run.py --mode mock --input examples/mock/conflict/input.json
```

`verify.py` 运行工程测试、重新生成 13 组离线场景并做 CLI 检查。默认 CLI 输出为 `.runtime/result.json`、`.runtime/trace.json`，持久化草稿为 `.runtime/drafts.sqlite`。再次执行同一命令复用数据库，只保留同一个请求的同一份草稿。演示不同场景时可用 `--db .runtime/another.sqlite` 隔离状态。

可选安装：`python -m pip install -e .`，随后使用 `material-scheduler` 命令，参数与 `run.py` 相同。没有安装依赖的要求时直接使用 `run.py`。

## 真实模型运行

使用 OpenAI 兼容 Chat Completions 的原生 function calling，默认百炼 `qwen3.8-27b`；非流式调用显式设置 `enable_thinking=false`。`.env.example` 仅为说明，程序不会自动读取 `.env`。先在本机安全配置密钥，再执行：

```powershell
# 若密钥刚写入 Windows 用户环境变量，当前窗口可这样刷新；不会显示密钥。
$env:DASHSCOPE_API_KEY = [Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY', 'User')
$env:AGENT_KEY_ENV = 'DASHSCOPE_API_KEY'
$env:AGENT_MODEL = 'qwen3.8-27b'
$env:AGENT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
# 本次用户选择自行管理额度：额度错误即报告，由用户指定下一款模型。
$env:AGENT_QUOTA_POLICY = 'user_managed'
python run.py --mode live --input examples/mock/conflict/input.json --db .runtime/live.sqlite --trace .runtime/live-trace.json

# 重建整套真实模型验收记录；将消耗 API 配额，失败不会自动切换 Mock。
python evaluate.py --mode live --case all --out examples/live --workers 2
```

网络必须可以访问 API。若网络需要代理，可在当前终端配置 `HTTPS_PROXY` 为自己的可用代理地址；项目不修改系统代理。API 不可用或额度不足时返回 `INCOMPLETE / MODEL_FAILURE`，不自动切换 Mock。批量验证检测到额度错误会停止安排后续调用；已发出的并发请求可能仍返回。

如需自动换模型，先在百炼[免费额度页](https://bailian.console.aliyun.com/cn-beijing/costing-balance/free-quota)为每个候选开启“免费额度用完即停”，等待生效，再取消 `AGENT_QUOTA_POLICY`，设置 `AGENT_FREE_ONLY_CONFIRMED=1` 和 `AGENT_MODELS=模型1,模型2`。仅 `AllocationQuota.FreeTierOnly` 触发切换，全部耗尽即停止；限流不会被误判为免费额度耗尽。配置声明无法自行启用或验证控制台开关。自动切换路径仅做离线模拟测试，本次真实验证始终使用同一模型，未耗尽额度。

本次完整真实验收实际输入 **127,369 token**、输出 **4,805 token**，合计 **132,174 token**。这只统计最新套件的 15 份落盘 Trace，不包括此前调试、被覆盖的探测或中断响应，也不表示平台总账单或免费/付费归属。用量原始记录在 Trace 的 `usage`，汇总见[证据核对结果](examples/verification/live-evidence-audit.json)。

## 输入和确认

```json
{
  "request_id": "team-request-001",
  "items": [{
    "material_id": "M01",
    "requested_slot": "2026-11-02T18:00:00+08:00",
    "hard_requirement": true,
    "channel": "xiaoying"
  }]
}
```

合成排期已占用 18:00。程序可保存 19:00 的建议草稿，但原始要求仍是 18:00，草稿为 `PENDING_CONFIRMATION`，`requires_confirmation=true`。用户确认后，在**同一原始请求**的对应项中加入 `"approved_slot": "2026-11-02T19:00:00+08:00"`，再次运行并使用同一数据库，草稿原地更新为 `READY_DRAFT`，ID 保持不变。`approved_slot` 是 CLI 调用者提供的可信用户输入；线上接入必须补身份认证。

请求 ID 与素材 ID 共同构成幂等键。同键更改原定时间、渠道或硬性要求会返回 `IDEMPOTENCY_CONFLICT`；确需修改要求时使用新请求 ID。模型工具没有确认权限参数。

## 输出含义

每项素材包含 `review_status`、带规则编号的 `risk_reasons`、`requested_slot`、`suggested_slot`、`pending_items`、`draft_save_status` 和草稿详情。

| 字段 | 含义 |
|---|---|
| `APPROVED / NEEDS_INFO / BLOCKED / NEEDS_REVIEW` | 审核通过、缺资料、阻断、需人工审核 |
| `READY_DRAFT / PENDING_CONFIRMATION` | 可确认使用的草稿 / 替代时间待确认草稿；均未发布 |
| `SAVED / FAILED / NOT_ATTEMPTED` | 已验证保存 / 保存未确认成功 / 未尝试保存 |
| `NO_AVAILABLE_SLOT` | 当前候选窗口全占用，待用户给出新时间窗口 |
| 顶层 `COMPLETED` | 本次审核处理结束；可能包含缺资料、风险或保存失败，须检查每项结果 |
| 顶层 `INCOMPLETE` | 步数耗尽、模型失败或提前结束导致未完成 |

CLI 退出码：处理链路结束为 0；输入配置错误或未完成为 2。业务保存失败也可能以 0 结束审核流程，消费者必须检查每项的 `outcome` / `draft_save_status`。单轮默认最多 12 次模型动作，可通过 `--max-steps 24` 处理多素材，最大 100。每次网络调用超时 45 秒。

## 可复现验收

```powershell
python -m unittest discover -s tests -v
python evaluate.py --mode mock --case all
python audit_evidence.py --mode live
python run.py --mode mock --input examples/mock/save_failure/input.json --simulate-save-failure --db .runtime/failure.sqlite
```

更换占用时段：复制 `data/` 到自己的目录，编辑 `schedule.json`，用 `--data-dir 路径` 重跑同一请求和数据库。已有建议草稿变成冲突时，Agent 可以查询替代时间；保存工具更新同一份草稿并重新要求确认。自动化版本见 [changed_occupancy](examples/mock/changed_occupancy/) 的两轮记录。

草稿时段按每项 1 小时、北京时间整点处理；查询原定时间及随后 3 小时，另含明确批准的时间。待确认草稿也临时占用时段，以避免同一 SQLite 库内的草稿互相冲突。真实视频内容识别、跨系统排期事务、身份系统和真实发布不在本次范围内。

自动化检查不使用真实密钥；运行状态见 [GitHub Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)，项目入口见 [SUBMISSION.md](SUBMISSION.md)。
