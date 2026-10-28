# 预期、实际结果、失败和限制

## 当前结果

Windows / Python 3.13.12 下，**59 项工程测试通过，0 失败、0 错误；15/15 组离线场景通过；CLI 检查通过**。证据：[原始测试日志](../examples/verification/unit-tests.txt)、[验证摘要](../examples/verification/summary.json)、[离线摘要](../examples/mock/summary.json)。

使用百炼 **qwen3.8-27b** 完成 **15/15 组真实模式验收**。其中 3 组缺参路径在模型调用前返回；其余 12 组由模型实际执行。共 17 轮记录，95 次成功模型响应；当前完整验收材料输入 255,859、输出 3,603 token，合计 259,462。不包括探测及旧批次，也不等于账户总用量或免费计费判断。

独立检查了 17 份 Trace 的费用表、账本、依赖、断点前缀、预算与时间，0 错误：[真实证据核对](../examples/verification/live-evidence-audit.json)。离线记录同样核对通过。自动化检查结果见 [Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。

## 场景和实际输出

每个场景目录包含输入、实际输出、Trace 与检查摘要。下表为真实模式结果；同名 examples/mock/ 为独立离线结果。

| 场景 | 预期与实际 | 证据 |
|---|---|---|
| 正常 | COMPLETED，premium，总费用 12，虚拟 9 分钟 | [输出](../examples/live/normal/output.1.json)、[Trace](../examples/live/normal/trace.1.json) |
| 缺预算 | NEEDS_INPUT，明确 budget，0 调用、0 费用 | [记录](../examples/live/missing_budget/trace.1.json) |
| 缺截止时间 | NEEDS_INPUT，明确 deadline，0 调用、0 费用 | [记录](../examples/live/missing_deadline/trace.1.json) |
| 两项都缺 | 同时列出两项，未执行工具 | [记录](../examples/live/missing_both/trace.1.json) |
| 工具一次失败 | 保留检索，生成 FAILED 后模型重试，最终 premium，总费用 13 | [输出](../examples/live/tool_failure/output.1.json)、[Trace](../examples/live/tool_failure/trace.1.json) |
| 预算超限 | 预算 10，工具先估算 12 不可行，模型选 economy，再估算 8 并保存 | [Trace](../examples/live/budget_replan/trace.1.json) |
| 无可行预算 | 预算 5，最低还差 3；INCOMPLETE，实际费用 0 | [输出](../examples/live/budget_impossible/output.1.json) |
| 截止时间促使改选 | 仅有 8 分钟，premium 不可行，economy 7 分钟完成，费用 8 | [Trace](../examples/live/deadline_replan/trace.1.json) |
| 无可行时间 | 仅有 4 分钟，最短仍差 3；INCOMPLETE，费用 0 | [输出](../examples/live/deadline_impossible/output.1.json) |
| 部分成功 | 主检索 PARTIAL 保留；备用检索因预算约束被拒后，模型选 economy，复用子结果，总费用 7 | [Trace](../examples/live/partial_success/trace.1.json) |
| 持续生成失败 | premium/economy 失败记录保留，INCOMPLETE，总费用 6，无方案 | [输出](../examples/live/permanent_failure/output.1.json) |
| 重复执行 | 两轮同方案 ID、同账本、同费用 12；第二轮未重复业务步骤 | [第一轮](../examples/live/duplicate/output.1.json)、[第二轮](../examples/live/duplicate/output.2.json) |
| 故障续跑 | 第一轮已花 3 后中断；第二轮复用检索、改 economy，总费用 9，旧尝试完整保留 | [第一轮](../examples/live/resume/trace.1.json)、[第二轮](../examples/live/resume/trace.2.json) |
| 保存持续失败 | 脚本成功仍保留，但 NOT_SAVED / INCOMPLETE，费用 12，不虚报成功 | [输出](../examples/live/save_failure/output.1.json) |
| 步骤上限 | 只允许一次动作，STEP_LIMIT / INCOMPLETE，实际业务费用 0 | [输出](../examples/live/step_limit/output.1.json) |

续跑用例的首轮 INTERRUPTED_AFTER_FAILURE 是明确的实验中断，不是模型主动选择的完成状态；第二轮真实模型观察断点后重新规划。未将该首轮算作成功方案。UUID 为实际生成值；跨次新实验可以不同，同一次重复执行必须一致。

## 发现的问题与修改

初版真实模式 14/15 组通过。预算场景最终费用为 8 且成功保存，但模型从已给出的费用信息直接选择 economy，缺少“工具先返回 12，再重规划为 8”的观察路径。没有违反预算，但不满足该用例要求的决策证据。保留了 [原 Trace](../examples/regressions/early-budget-selection/trace.1.json) 和 [初版摘要](../examples/regressions/initial-suite-summary.json)。

随后把 initial_strategy 明确为输入中的待评估初始草案：先选该草案并工具估算，才能改选；模型仍自行决定替代策略、执行、重试或停止。新增了对应工程回归测试，并全套真实重测，15/15 通过。没有放宽测试来掩盖缺失证据，也没有把固定条件分支写成模型日志。

最终任务依赖核对又发现，脚本执行前的初步估算被展示成了“脚本后的费用复核已完成”，尽管实际保存仍严格要求生成后的新鲜估算。修正了这个展示状态，并增加前后阶段的工程测试与“成功节点的依赖必须满足”的证据检查。对预算不可行、时间不可行、持续失败及续跑四组进行了定向真实回归，全部通过；旧记录保留在 [状态展示回归说明](../examples/regressions/post-review-status/README.md)。当前完整材料是全套重测与这四组定向回归的集合，17 份当前记录均通过最后的独立核对。

## 测试的有效范围

59 项工程测试包含精确金额与非法输入、缺参零调用、预算/时间不可行停止、依赖检查、新鲜估算、失败费用、部分结果、断点恢复、历史不可变、重复结果复用、同 ID 需求冲突、并发检索/生成/保存不重复收费、SQLite 故障回滚，以及越权价格/预算参数、未知发布工具、提前结束和 API 失败不降级 Mock。

适配器的构造响应测试只是协议和脱敏检查，不代替真实模型。独立证据检查重算费用表和每步约束，不只是检查汇总的 passed 字段。15 个合成场景不是生产统计或泛化成功率。

## 复跑与限制

```powershell
python verify.py
python audit_evidence.py --mode mock
# 按 README 配好 API 环境后，才执行实际消耗配额的复跑：
python evaluate.py --mode live --case all --out examples/live --workers 2
python audit_evidence.py --mode live
```

当前为本地合成素材和模板脚本，未评价真实创意质量或活动效果。时间为费用表的虚拟分钟，非真实网络时延承诺。计划模板只有两种，且没有任意 DAG 搜索。真实外部工具的已发生费用不能用 SQLite 回滚，后续接入需对账与幂等。CLI 调用者被信任，尚无多用户认证。工程记录、AI 使用范围和验证口径见 04-ai-usage.md。
