# 企业知识问答 Agent

帮助员工从合成制度与产品 FAQ 中找到可核对的答案。模型选择检索、改写、读取原文、查证冲突或追问；程序验证引用和来源新鲜度，并以完整原文渲染事实。遇到 30 天与 15 天冲突，不因文档日期较新就擅自选择一个。

**本次结果：60 项工程测试、14/14 组离线场景、14/14 组真实模型场景通过，CLI 与引用审计通过。** 使用百炼 `qwen3.8-flash`，完整验收保留 16 轮 Trace、94 次模型响应；独立核对了 15 条真实引用。所有文档和问题均为合成资料，不代表真实企业制度。

## 评审入口

- [需求与成功标准](docs/01-requirements.md)
- [工具循环、检索和引用约束](docs/02-solution.md)
- [实际结果、限制与改进](docs/03-evaluation.md)
- [AI 使用和个人贡献](docs/04-ai-usage.md)
- [工程复盘](docs/05-retrospective.md)

优先查看：[冲突原文与追问](examples/live/conflict/trace.1.json)、[改写后的相似问题](examples/live/paraphrase/trace.1.json)、[文档从 30 改为 45 后的第二轮](examples/live/document_replacement/trace.2.json)、[读取后原文变化](examples/live/stale_during_run/trace.1.json)。`examples/mock/` 是独立手写策略的离线流程记录，不能代替真实模型证据。

## 环境与快速运行

Python **3.11+**，运行和测试仅使用标准库，无第三方运行依赖。本次本地环境为 Windows / Python 3.13.12。从项目根目录执行：

```powershell
python verify.py
python run.py --mode mock --input examples/mock/conflict/input.json
python retrieval_benchmark.py
python audit_evidence.py --mode mock
```

可选安装 `python -m pip install -e .` 后使用 `enterprise-qa` 命令；直接用 `run.py` 无须安装。默认结果在 `.runtime/output.json`，实际步骤在 `.runtime/trace.json`。本项目只读取知识库，输出本地文件，没有发布或外部消息工具。

## 输入

```json
{
  "request_id": "employee-question-001",
  "question": "那需要提前几天？",
  "history": [
    {"role": "user", "content": "我准备申请年假。"}
  ]
}
```

history 可省略，最多 8 条 user/assistant 消息。历史用于理解指代，不能当作制度证据；历史助手的数字也必须重新查原文。相同追问有“年假”前文时可依据 D03 回答；缺少前文则请求澄清。

## 真实模型

先在本机安全配置密钥。`.env.example` 仅为说明，不会自动加载：

```powershell
# 如果刚写入 Windows 用户环境变量，可这样刷新；不会显示密钥。
$env:DASHSCOPE_API_KEY = [Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY','User')
$env:AGENT_KEY_ENV = 'DASHSCOPE_API_KEY'
$env:AGENT_MODEL = 'qwen3.8-flash'
$env:AGENT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
python run.py --mode live --input examples/live/conflict/input.json

# 下列复跑会实际使用模型配额。
python evaluate.py --mode live --case all --out examples/live --workers 2
python audit_evidence.py --mode live
```

按用户要求固定模型，额度不足返回错误后由用户换模型；批量评估遇到明确额度错误会阻止后续调用，已发出的请求可能仍返回。不自动切换其他模型或 Mock。免费/付费以服务商控制台为准，token 记录不能证明免费计费。

本次完整验收的实际输入为 **203,586 token**、输出 **7,218 token**，合计 **210,804 token**，94 次模型响应。该统计仅覆盖当前完整验收材料，不表示账户总用量或账单。

## 文档、引用与冲突

`data/documents.json` 包含文档 ID、版本日期、范围/优先级和原子片段。默认 D01 写 30 天、D02 写 15 天，未给确定的适用范围与优先级；D03 为年假 FAQ，D04 为离线导出，D05 为密码重置。

模型先获得检索定位器，再用 `read_fragment` 读取原文。最终只能提交决策模式及完整原文引用，没有任意事实回答文本参数。程序检查文档/版本/片段 ID、是否实际读取、完整引文、主题、内容哈希和冲突覆盖，然后生成答案。这保留数字、单位、否定和适用条件，代价是回答偏抽取式，未做开放式摘要。

冲突时须补查适用范围或优先级，仍无法确定则列出各原文并追问。日期不自动产生权威优先级。当前一致的说法不会仅因日期不同而被标成冲突。每次检索和读取从文件加载当前内容，提交与最终返回再检查内容哈希；即使日期没变，旧原文也不能冒充新依据。

## 输出状态

| task_status | 含义 |
|---|---|
| ANSWERED | 已核对原文，可提供抽取式答案 |
| CONFLICT | 原文存在未解决的不同说法，列出引用并确认适用制度 |
| NEEDS_CLARIFICATION | 问题主题或指代不足，先追问 |
| INSUFFICIENT_EVIDENCE | 补查后仍缺直接相关资料 |
| TOOL_FAILURE | 文档工具持续不可用，不提供事实答案 |
| INCOMPLETE | 动作上限、模型失败或来源变化导致没有最终有效答案 |

结果包含 answer、citations、gaps、follow_up_question。run_status=COMPLETED 代表形成了处理结论，可能是冲突或拒答；消费者必须检查 task_status。CLI 已形成结论退出码为 0，未完成或输入错误为 2。

## 检索改进与复现

基础检索为中文字符二元组 BM25；expanded 模式加人工主题和同义表达扩展。8 个预置合成查询的 Hit@1 与平均 Recall@4 均由 **62.5% 提升到 100%**，详见 [实际检索记录](examples/verification/retrieval-benchmark.json)。这些查询与人工词表有关联，不是独立留出集，也不是答案准确率。

替换文档可复制 data 目录后修改，再用 `--data-dir 路径` 重跑同一问题；最终依据来自新原文。自动化变更用例保存了每轮执行前后完整文档快照，便于核对引用。

模拟一次工具失败：

```powershell
python run.py --mode mock --input examples/mock/clear_answer/input.json --fault read_once
```

默认最多 14 次模型动作，最大 100；每次 API 请求超时 45 秒，输出上限 600 token。没有 Agent 框架或向量服务。当前是小型、结构化、闭集语料原型；主题词表和保守冲突比较不是通用语义蕴含证明，也未实现企业权限、文档解析或自动权威裁决。项目与自动化检查入口见 [SUBMISSION.md](SUBMISSION.md) 和 [Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。
