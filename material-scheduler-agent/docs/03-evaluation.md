# 预期、实际结果、失败与限制

## 本次结果

本地环境为 Windows、Python 3.13.12。`python verify.py` 的实际结果：**58 项工程测试通过，0 失败、0 错误；13/13 组离线场景通过；CLI 检查通过**。真实 `qwen3.8-27b` **13/13 组场景通过**，包含 15 轮执行、80 次成功模型响应。原始证据：[测试日志](../examples/verification/unit-tests.txt)、[验证摘要](../examples/verification/summary.json)、[离线摘要](../examples/mock/summary.json)、[真实摘要](../examples/live/summary.json)。自动化检查结果见 [Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。

本次实测模型为 `qwen3.8-27b`，接口为百炼 OpenAI 兼容接口，显式关闭思考模式。使用用户管理额度策略，额度不足返回失败后由用户更换模型。全套期间未发生额度耗尽或真实模型切换，不声称已验证免费额度归属。最新套件输入 127,369 token、输出 4,805 token，合计 132,174；历史调试与中断响应不包含在此统计中。

独立执行 `python audit_evidence.py --mode live`，核对 15 份 Trace 的模式、动作上限、后续动作、审核/查询/保存前置条件、原始时间与确认边界、保存成功依据和 API 用量，结果为 0 错误：[证据核对结果](../examples/verification/live-evidence-audit.json)。离线记录同样核对通过。该检查验证记录的一致性，不等于生产环境效果评估。

## 失败、修正与重测


首次 qwen3.8-27b 批次发现：模型误以为必须先拿到用户同意才能保存建议草稿，在冲突与重复提交场景直接 finish，最终为 INCOMPLETE。证据：[修正前冲突](../examples/live-before-fix/conflict/trace.1.json)、[修正前重复提交](../examples/live-before-fix/duplicate/trace.2.json)。

修正了提示词中“已授权保存待确认建议草稿”的表达，并为 finish 增加未完成项检查，返回 FINISH_REJECTED 后仍由模型选择具体动作。随后全套真实重测 13/13 通过。没有把固定分支伪装成模型决策；审核、时间和保存动作仍由模型发起，确认与写入约束由工具执行。原失败批次保留，不与当前通过率混算。

## 六项必验与扩展验证

下表链接为**实际真实模型输出**。同名 `examples/mock/` 为独立的离线记录，不能混算通过率。每个目录包含输入、初始占用表、实际输出、步骤/参数/工具返回/后续动作/简要依据。UUID 为实际生成，重复运行可能不同；同一次重复提交的两轮必须相同。

| 用例 | 预期 | 实际结果与证据 |
|---|---|---|
| 正常 | 保存 17:00 草稿 | DRAFT_SAVED、READY_DRAFT，[输出](../examples/live/normal/output.1.json) / [Trace](../examples/live/normal/trace.1.json) |
| 信息缺失 | 补查后仍缺标题，不保存 | NEEDS_INFO、待补 title，[输出](../examples/live/missing/output.1.json) / [Trace](../examples/live/missing/trace.1.json) |
| 版权风险 | 阻断，不保存 | BLOCKED、R02，[输出](../examples/live/blocked/output.1.json) / [Trace](../examples/live/blocked/trace.1.json) |
| 人工审核风险 | 不明确授权不能排期 | NEEDS_REVIEW、R03，[输出](../examples/live/risk_review/output.1.json) / [Trace](../examples/live/risk_review/trace.1.json) |
| 时间冲突 | 18:00 占用，19:00 仅为待确认建议 | PENDING_CONFIRMATION，保留原要求，[输出](../examples/live/conflict/output.1.json) / [Trace](../examples/live/conflict/trace.1.json) |
| 重复提交 | 同键两次只一份草稿 | 草稿 ID 相同、行数 1，[第一轮](../examples/live/duplicate/output.1.json) / [第二轮](../examples/live/duplicate/output.2.json) / [摘要](../examples/live/summary.json) |
| 保存失败 | 不虚报成功 | SAVE_FAILED、FAILED、行数 0，[输出](../examples/live/save_failure/output.1.json) / [Trace](../examples/live/save_failure/trace.1.json) |
| 补查成功 | 补齐标题后重新审核并保存 | NEEDS_INFO → APPROVED → DRAFT_SAVED，[Trace](../examples/live/supplement/trace.1.json) |
| 用户明确确认 | 原请求附 approved_slot 才可通过替代时间确认 | READY_DRAFT、approval_source=user_request，[输出](../examples/live/confirmed/output.1.json) |
| 占用变化后重提 | 旧建议 19:00 新增占用，改建议且不新增草稿 | 同 ID 改为 20:00、待确认、行数 1，[第一轮](../examples/live/changed_occupancy/output.1.json) / [第二轮](../examples/live/changed_occupancy/output.2.json) / [变更占用](../examples/live/changed_occupancy/schedule.changed.json) |
| 步数上限 | 返回明确未完成 | INCOMPLETE / STEP_LIMIT，[输出](../examples/live/step_limit/output.1.json) |
| 全部候选占用 | 返回待调整时间窗口 | NO_AVAILABLE_SLOT，[输出](../examples/live/no_slots/output.1.json) |
| 多素材 | 同渠道草稿不互相占同一时间 | 生成两个不同时间的草稿，[输出](../examples/live/multi/output.1.json) |

## 工程检查的有效范围

58 项测试覆盖正常与异常分支、相同/不同请求的并发保存、确认后原地更新、保留已记录确认、防止批准错误时段、旧草稿有效时不漂移、占用改变后重排、保存前二次检查、相邻区间不冲突，以及模型跳过审核、伪造确认参数、访问其他素材、请求 publish、提前 finish、未知工具反复执行等情况。还覆盖免费额度路由、额度错误停止其他工作线程、错误脱敏和 Qwen 非流式参数。

适配器测试使用构造的 HTTP 响应验证协议解析与错误脱敏，不等同于真实模型测试。并发和输入校验测试不使用真实模型，便于离线重现。

## 如何复跑真实验收

按 README 设置 `DASHSCOPE_API_KEY`、`qwen3.8-27b` 与额度策略，再执行（会使用实际 API 配额）：

```powershell
python evaluate.py --mode live --case all --out examples/live --workers 2
```

检查 summary 的每项 passed 与对应 Trace。当前已提供正常、冲突、缺资料等多条真实模型路径。模型可能选择不同但合理的工具次序；验收以硬性要求、确认状态、实际存储和风险处置为主。复跑后如有新失败，应如实更新结果并保留历史记录。无需为了证明路由而主动消耗完免费额度；切换逻辑的模拟测试与真实业务链路结果分开记录。

## 已知限制

没有真实视频安全识别或真实运营数据；固定时长为 1 小时，时区限 +08:00，候选窗口为原定及后 3 小时；待确认草稿没有过期回收。JSON 现有排期与 SQLite 不构成跨系统事务。CLI 用户身份被信任，线上确认需身份与权限校验。13 个合成场景不是生产统计或泛化效果评估。自动切换的控制台保护状态依赖调用者确认，程序不能从 token 用量推断免费或付费。
