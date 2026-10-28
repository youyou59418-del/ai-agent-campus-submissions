# 实际结果、验证范围和限制

## 本次结果

Windows / Python 3.13.12 下：**60 项工程测试，0 失败、0 错误；离线 14/14；真实模型 14/14；CLI 检查通过**。对应 [测试日志](../examples/verification/unit-tests.txt)、[验证摘要](../examples/verification/summary.json)、[离线摘要](../examples/mock/summary.json)、[真实摘要](../examples/live/summary.json)。自动化检查结果见 [Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。

完整真实验收使用 qwen3.8-flash，16 轮记录、94 次模型响应，输入 203,586、输出 7,218 token，合计 210,804。该统计不等于账户总用量或免费计费判断。

## 六项必验与扩展

| 场景 | 实际结果 | 原始记录 |
|---|---|---|
| 明确答案 | ANSWERED，D03 原文为至少提前 3 个工作日 | [输出](../examples/live/clear_answer/output.1.json)、[Trace](../examples/live/clear_answer/trace.1.json) |
| 文档冲突 | CONFLICT，列出 D01 的 30 天、D02 的 15 天，补查并请求确认适用制度 | [Trace](../examples/live/conflict/trace.1.json) |
| 资料外问题 | 补查后 INSUFFICIENT_EVIDENCE，未回答天气或编造引用 | [输出](../examples/live/outside_corpus/output.1.json) |
| 改写问题 | “票据最迟啥时候交”经查询改进，在单制度数据中答 30 天 | [Trace](../examples/live/paraphrase/trace.1.json) |
| 上下文追问 | 有年假前文答 3 个工作日，无前文 NEEDS_CLARIFICATION | [有前文](../examples/live/context_followup/trace.1.json)、[无前文](../examples/live/context_missing/trace.1.json) |
| 工具失败 | 一次搜索/读取失败后恢复；持续读取失败时 TOOL_FAILURE，无事实引用 | [搜索恢复](../examples/live/search_failure_once/trace.1.json)、[读取恢复](../examples/live/read_failure_once/trace.1.json)、[持续失败](../examples/live/permanent_failure/trace.1.json) |
| 单份制度 | 只有 D01，无冲突时可依据原文答 30 天 | [输出](../examples/live/single_policy/output.1.json) |
| 替换原文 | 同问题、同日期，D01 原文 30→45，答案和引用同步更新 | [第一轮](../examples/live/document_replacement/trace.1.json)、[第二轮](../examples/live/document_replacement/trace.2.json) |
| 冲突消失 | 第二轮 D02 改为 30 后，CONFLICT→ANSWERED，不因版本不同编造冲突 | [第一轮](../examples/live/conflict_disappears/trace.1.json)、[第二轮](../examples/live/conflict_disappears/trace.2.json) |
| 读取后变更 | 3→5 个工作日，旧引用被拒，重读后答 5 | [Trace](../examples/live/stale_during_run/trace.1.json) |
| 动作上限 | 一次动作后 STEP_LIMIT / INCOMPLETE，事实答案和引用为空 | [输出](../examples/live/step_limit/output.1.json) |

每个场景保留输入、实际输出、Trace、检查摘要，以及每轮文档 before/after 完整快照。替换测试故意保持日期不变，验证内容新鲜度而非仅依赖日期。

## 独立引用审计

`audit_evidence.py` 不调用模型，独立对照场景文档快照：验证是否实际检索和读取、ID/版本/片段、完整原句、当前内容哈希、控制主题、冲突说法覆盖及最终抽取式模板。真实 16 份 Trace、15 条引用均通过，0 错误：[真实引用核对](../examples/verification/live-evidence-audit.json)。离线 16 份记录同样通过，但引用数为 14，因为一致文档时 Mock 仅选一份，真实模型有时选择两份。

这是闭集语料和抽取式回答的来源/支持检查，不是任意自然语言答案的通用蕴含证明。

## 检索改进

在 8 个预置合成查询上，literal BM25 的 Hit@1 和平均 Recall@4 均为 0.625，人工同义扩展后均为 1.0：[逐查询结果](../examples/verification/retrieval-benchmark.json)。票据、没网导报告、口令忘了等表达在基础检索中无正确命中，扩展后找到相应主题。

该词表与查询集人工设计，不能称为独立留出集或生产泛化指标，也不能把检索命中率当成最终答案准确率。未实现或对比向量检索、Reranker，未给它们编造结果。

## 问题发现与验证改进

离线与首次完整真实场景均通过。进一步检查发现，若在答案获校验后、最终返回前新增另一份冲突文档，仅核对原引用可能漏掉新冲突。最终增加了整体语料哈希检查和对应回归：即使 D01 引文未变，新增 D02 也会使旧结论失效，返回 SOURCE_CHANGED / INCOMPLETE。这条时序边界通过离线回归验证；实际真实场景没有声称人为制造了这个特定并发窗口。当前真实快照的整体哈希亦通过独立核对。

60 项工程测试还覆盖错误文档/版本、数字篡改、截取短句、主题错配、未读取引用、重复引用、未覆盖冲突、较新日期不能裁决、历史助手非证据、备注不可作为政策、同日期来源修改、文件消失、API 失败不降级 Mock 及额度错误停止后续调用。

## 复现与限制

```powershell
python verify.py
python retrieval_benchmark.py
python audit_evidence.py --mode mock
# 按 README 配置后执行会实际消耗配额的真实重跑：
python evaluate.py --mode live --case all --out examples/live --workers 2
python audit_evidence.py --mode live
```

资料为五篇合成文档、四个控制主题；人工关系词和保守文本比较可能误判复杂问题。抽取式回答限制了自由总结能力。没有企业权限、真实性签名、PDF/OCR、规模化索引或自动权威裁决。当前样例不能证明真实员工场景的全面正确率。工程记录、AI 使用范围和验证口径见 04-ai-usage.md。
