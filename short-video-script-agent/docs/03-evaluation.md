# 实际生成、检查、失败与限制

## 最终结果

Windows / Python 3.13.12：**61 项工程测试通过，0 失败、0 错误；16/16 离线场景、16/16 真实模式场景、CLI 和独立审计通过**。[测试日志](../examples/verification/unit-tests.txt)、[验证摘要](../examples/verification/summary.json)、[离线摘要](../examples/mock/summary.json)、[真实摘要](../examples/live/summary.json)。三组缺参路径不调用模型，其他 13 组实际使用 qwen3.8-flash。

当前材料由全套执行和 retrieval_changed 定向回归组成，共 18 轮记录、97 次成功模型响应，输入 346,905、输出 8,108 token，共 355,013。历史业务回归单独存放，不混入当前通过率；token 记录不表示账户总量或计费归属。

## 场景结果

| 场景 | 实际结果 | 证据 |
|---|---|---|
| 正常 | SCRIPT_READY，17 秒，标题/分镜/旁白/字幕/P01 引用齐全 | [脚本](../examples/live/normal/output.1.script.md)、[Trace](../examples/live/normal/trace.1.json) |
| 缺受众 | NEEDS_INPUT，零调用 | [输出](../examples/live/missing_audience/output.1.json) |
| 缺时长 | NEEDS_INPUT，零调用 | [输出](../examples/live/missing_duration/output.1.json) |
| 多项缺失 | 列明主题/平台/风格，零调用 | [输出](../examples/live/missing_multiple/output.1.json) |
| 无素材 | 改写/补查后 NEEDS_MATERIALS，无批准脚本 | [Trace](../examples/live/no_materials/trace.1.json) |
| 查询改进 | “给画面加字”找到字幕事实，SCRIPT_READY | [Trace](../examples/live/query_rewrite/trace.1.json) |
| 一次检索失败 | 观察错误后恢复，16 秒通过 | [Trace](../examples/live/search_failure_once/trace.1.json) |
| 一次估算失败 | 重试后按真实估算通过，17 秒 | [Trace](../examples/live/estimate_failure_once/trace.1.json) |
| 规则工具持续失败 | TOOL_FAILURE，不交付未经规则检查的脚本 | [输出](../examples/live/rules_permanent_failure/output.1.json) |
| 超时长修正 | D0=38 秒不通过，模型改 D1=19 秒，双检查通过 | [脚本](../examples/live/overlong_repair/output.1.script.md)、[Trace](../examples/live/overlong_repair/trace.1.json) |
| 无法满足时长 | 目标 5 秒，当前表达库最短估计 12 秒，UNFULFILLED | [输出](../examples/live/impossible_duration/output.1.json) |
| 品牌/风格修正 | 初稿专业风格不匹配自然要求，改稿后 21 秒通过 | [Trace](../examples/live/brand_repair/trace.1.json) |
| 要求无依据 80% | REQUESTED_FACT_NOT_SUPPORTED，UNFULFILLED，未批准该承诺 | [Trace](../examples/live/unsupported_claim/trace.1.json) |
| 改目标时长 | 30 秒上限时产出 20 秒；改上限 20 后产出 12 秒，均重新估算和检查 | [第一轮](../examples/live/target_changed/trace.1.json)、[第二轮](../examples/live/target_changed/trace.2.json) |
| 改检索结果 | 有 P01 时 16 秒通过；移除后请求素材，不复用旧引用 | [第一轮](../examples/live/retrieval_changed/trace.1.json)、[第二轮](../examples/live/retrieval_changed/trace.2.json) |
| 动作上限 | STEP_LIMIT / INCOMPLETE，无批准脚本 | [输出](../examples/live/step_limit/output.1.json) |

每轮均有 input、data.before/data.after 快照、实际输出、JSON/Markdown 脚本和 Trace。失败导出是明确的未通过说明；未批准草稿仍可在结果 drafts 与检查记录中核对。

完整真实验收的超时长用例为 38→19 秒，离线策略为 38→28 秒，各自保留实际运行记录。

## 独立核对

`audit_evidence.py` 独立重算每版旁白单位、每镜秒数和总时长；核对目标、素材原句、source_id/fact_id/版本/哈希、实际检索记录、表达库、品牌规则以及同版本的双检查和提交事件。当前 18 轮中有 9 份可交付脚本，35 个已批准分镜、9 条事实引用；18 条时长记录重算通过，0 错误：[真实审计](../examples/verification/live-evidence-audit.json)。离线材料也通过审计，分镜数不同，因为手写策略和真实模型编排不同。

核对的是本合成契约和估算，不是任意自然语言事实证明、TTS 实测或平台审核认证。案例通过表示实际状态符合预期，不表示所有场景都有可交付脚本。

## 问题发现与改进

完整真实场景先已满足业务验收。复核发现 retrieval_changed 第一轮在已有合格 D1 后继续改稿，产生两次风格错误，最后又修正到合格。原记录保留在 [历史回归](../examples/regressions/unnecessary-revisions/README.md)。新增门控：当前已有双检查通过版本时拒绝无必要重写，引导模型提交。定向回归第一轮 6 次动作完成，第二轮按缺素材停止；这是单用例观察，不作为总体效率提升或独立消融结论。

代码复核还发现，检查过程中规则变更时，旧规则结果不应绑定检查结束后的新快照。已让估算和规则检查绑定各自实际读取的数据快照，并新增回归：旧快照通过记录不能批准新规则下的脚本。该时序边界由离线测试验证，没有声称真实批次主动制造过这个并发窗口。

61 项工程测试还覆盖缺参零调用、无素材补查、自动字幕/80% 等错误事实、错主题来源、同日期内容变化、旧检查复用、未知标题/视觉/自由旁白、重复分镜 ID、删除事实、两镜缩时作弊、工具失败、改稿上限、人工导出篡改和失败导出残留。

## 复现与局限

```powershell
python verify.py
python audit_evidence.py --mode mock
python validate_script.py --input examples/live/overlong_repair/input.1.json --script examples/live/overlong_repair/output.1.script.json
# 按 README 设置模型后才重跑真实模式：
python evaluate.py --mode live --case all --out examples/live --workers 2
python audit_evidence.py --mode live
```

没有真实产品视频、TTS、效果数据或品牌官方规则。受限表达库降低自由创意范围，主题词表不覆盖任意主题或复杂复合需求；库外编辑需要人工审核。时长是上限而非目标区间，语速没有通过真实配音校准。当前成功率只对应 16 个合成场景。工程记录、AI 使用范围和验证口径见 04-ai-usage.md。
