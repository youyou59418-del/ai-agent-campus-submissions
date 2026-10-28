# 内容任务规划 Agent

帮助运营人员在预算和截止时间内完成一份内容活动方案。模型观察工具估算、失败或部分成功结果后决定重规划、替换方案、重试或停止；程序负责硬性约束、真实状态、费用账本和持久化。仅保存本地方案，不发布内容、不支付真实业务资金。

**本次验证：59 项工程测试、15/15 组离线场景、15/15 组真实模式场景通过，CLI 与证据核对通过。** 真实模式使用百炼 `qwen3.8-27b`；三组缺参场景在调用模型前返回，其余 12 组实际调用模型。共保存 17 轮记录，95 次成功模型响应。素材、业务工具、费用与分钟均为合成数据。

## 评审入口

- [需求和成功标准](docs/01-requirements.md)
- [流程、模型决策、状态和费用设计](docs/02-solution.md)
- [实际验证结果与失败修正](docs/03-evaluation.md)
- [AI 使用范围及个人贡献](docs/04-ai-usage.md)
- [工程复盘](docs/05-retrospective.md)

优先查看真实 Trace：[12 超预算后改选 8](examples/live/budget_replan/trace.1.json)、[工具失败后恢复](examples/live/tool_failure/trace.1.json)、[故障断点后的第二轮续跑](examples/live/resume/trace.2.json)。`examples/mock/` 是独立手写策略的离线记录，不能证明真实模型决策。

## 环境与快速运行

Python **3.11+**，运行和测试仅用标准库，无第三方运行依赖。本次本地环境是 Windows / Python 3.13.12。从项目根目录执行：

```powershell
python verify.py
python run.py --mode mock --input examples/mock/budget_replan/input.json
```

`verify.py` 会实际执行工程测试、重建 15 组离线场景并检查 CLI。结果保存在 `examples/verification/`。可选安装 `python -m pip install -e .` 后使用 `content-planner` 命令；直接用 `run.py` 无须安装。

默认输出为 `.runtime/output.json`、`.runtime/trace.json`，SQLite 状态为 `.runtime/plans.sqlite`。重复同一输入和数据库会复用结果；演示不同独立请求可用 `--db .runtime/another.sqlite`。

## 输入、模拟费用和时间

```json
{
  "request_id": "campaign-001",
  "topic": "秋日创作挑战活动",
  "budget": 10,
  "deadline": "2026-11-03T09:30:00+08:00",
  "start_time": "2026-11-03T09:00:00+08:00",
  "initial_strategy": "premium"
}
```

必需业务信息为主题、预算和截止时间。预算或截止时间缺失时返回 `NEEDS_INPUT`，列明字段，不调用模型或业务工具。`request_id` 用于幂等；`initial_strategy` 是待评估的初始草案，默认为 premium。初始草案必须先交给工具估算，随后模型可根据结果选择另一方案或停止。

| 操作 | 模拟费用 | 虚拟耗时 |
|---|---:|---:|
| 主素材检索 | 2 | 2 分钟 |
| 备用检索 | 2 | 3 分钟 |
| 部分检索结果 | 1 | 1 分钟 |
| premium 脚本，至少 2 项素材 | 10 | 6 分钟 |
| economy 脚本，至少 1 项素材 | 6 | 4 分钟 |
| 脚本生成失败 | 1 | 1 分钟 |
| 保存成功或失败 | 0 | 1 分钟 |
| 选方案、估算、结束 | 0 | 0 |

因此完整 premium 费用为 12、耗时 9 分钟；economy 费用为 8、耗时 7 分钟。方案名用于区分成本和依赖，未证明真实生成质量差异。检索无素材仍产生检索费用；失败和未被最终方案使用的步骤费用也不抹掉。

`start_time` 为可选的复现时钟起点；未提供时，在首次创建请求时记录当前北京时间，后续续跑复用。执行按费用表累计**虚拟分钟**，API 等待时间另记 `elapsed_wall_seconds`。这验证合成耗时约束，不代表生产环境真实交付 SLA。金额用整数最小单位存储，输出为两位小数字符串。

## 真实模型运行

在本机安全设置 `DASHSCOPE_API_KEY`，`.env.example` 仅供说明，不会自动加载：

```powershell
# 若刚写入 Windows 用户环境变量，可这样刷新；不会显示密钥。
$env:DASHSCOPE_API_KEY = [Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY','User')
$env:AGENT_KEY_ENV = 'DASHSCOPE_API_KEY'
$env:AGENT_MODEL = 'qwen3.8-27b'
$env:AGENT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
python run.py --mode live --input examples/live/budget_replan/input.json --db .runtime/live.sqlite

# 重新执行全套将实际使用 API 配额。
python evaluate.py --mode live --case all --out examples/live --workers 2
```

按用户要求固定模型，额度不足报告后由用户换；批量评估发现明确额度错误后停止后续调用，已发出的并发请求可能仍返回。不自动切换模型或 Mock。免费/付费归属以服务商控制台为准，token 统计本身不能保证免费。需要限制计费时在百炼控制台启用[免费额度用完即停](https://help.aliyun.com/zh/model-studio/new-free-quota)。

当前完整验收材料输入 **255,859 token**、输出 **3,603 token**，共 **259,462 token**；不含此前探测与旧批次，不等于账户总用量。合成业务费用与真实模型 API 计费是两个独立口径。

## 失败续跑演示

```powershell
# 首轮：注入一次生成失败，并在故障后模拟中断。退出码 2 为预期。
python run.py --mode mock --input examples/mock/resume/input.json --db .runtime/resume.sqlite --fault generate_once --interrupt-after-failure --output .runtime/resume.first.json --trace .runtime/resume.first.trace.json

# 续跑：同一输入、同一数据库；已经消费的一次性故障不会重置。
python run.py --mode mock --input examples/mock/resume/input.json --db .runtime/resume.sqlite --fault generate_once --output .runtime/resume.second.json --trace .runtime/resume.second.trace.json
```

首轮检索成功费用 2、脚本失败费用 1，已花 3。续跑时 premium 需要总费用 13，超过预算 12；模型选择 economy，复用素材，再花 6，以总费用 9 保存。原失败尝试仍为 FAILED，后续成功另记新尝试；没有重复检索和重复收取模拟业务费用。

## 状态和工程约束

`task_plan` 给出依赖和当前步骤状态；`step_attempts` 保存每次实际执行的结果、状态、费用和耗时；`saved_plan` 为经数据库读回确认的活动方案，包含目标/受众和渠道假设、素材清单、脚本、费用与完成时间。假设标明待运营确认，没有编造活动效果。

顶层 `COMPLETED` 需实际保存且模型正常结束；`INCOMPLETE` 提供原因与预算/时间缺口；`NEEDS_INPUT` 请求补充信息。`PARTIAL` 可在满足较低素材依赖时复用，但不会改写为检索完全成功。重试成功不会删除历史 FAILED 尝试。

同请求 ID 不可擅自改变主题、预算、截止时间或初始要求；改变要求应使用新 ID。默认最多 24 次模型动作，最大 100；每个失败业务步骤最多两次实际失败。生成前检查素材依赖和约束，保存前检查脚本成功及新鲜估算。SQLite 事务使结果与费用一起落盘，同一步成功后缓存复用。

```powershell
python audit_evidence.py --mode live
python audit_evidence.py --mode mock
```

证据检查独立核算费用表、账本、步骤依赖、断点历史、预算与截止时间。CLI 成功保存并结束退出码为 0；缺参、未完成或配置错误为 2。

自动化检查与运行状态见 [GitHub Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。项目入口见 [SUBMISSION.md](SUBMISSION.md)。
