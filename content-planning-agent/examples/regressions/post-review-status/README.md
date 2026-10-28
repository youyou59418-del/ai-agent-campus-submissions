# 保存前复核节点的旧展示记录

此目录是修正前的历史记录，不属于最终 examples/live/ 验收材料。

原实现把“执行前的初步成本估算”与“脚本生成后的保存前复核”共用展示状态，在脚本未成功时可能出现 estimate_current=SUCCEEDED 或 STALE，而其依赖脚本尚未完成。真实保存工具始终要求成功脚本与生成后的新鲜估算，费用和保存约束未被绕过。

修正后，尚未成功生成脚本时该后置节点保持 PLANNED；脚本成功但估算过期则为 STALE；重新估算后才为 SUCCEEDED。历史摘要中的 passed 来自当时的业务检查，未包含最终新增的成功节点依赖检查。

针对 budget_impossible、deadline_impossible、permanent_failure、resume 的真实回归已重跑，当前输出在 examples/live/ 对应目录。最新 17 份记录已通过增强后的独立证据检查。
