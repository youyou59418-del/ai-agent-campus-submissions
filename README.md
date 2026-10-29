# 第 1 题：素材审核与发布排期 Agent

[项目 README](material-scheduler-agent/README.md) · [GitHub Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions/workflows/verify.yml)

本仓库提交第 1 题，实现素材审核、时段冲突处理、待确认草稿、重复请求幂等及保存失败处理。所有代码、合成资料、可执行测试、实际输入输出和 Trace 均位于同一仓库。

## 评审入口

- [环境、安装与运行](material-scheduler-agent/README.md)
- [需求和成功标准](material-scheduler-agent/docs/01-requirements.md)
- [动态决策与工程约束](material-scheduler-agent/docs/02-solution.md)
- [实际验证结果](material-scheduler-agent/docs/03-evaluation.md)
- [AI 辅助范围与人工参与](material-scheduler-agent/docs/04-ai-usage.md)
- [工程复盘](material-scheduler-agent/docs/05-retrospective.md)

## 一键运行

Python 3.11+，运行和测试仅使用标准库。

```powershell
git clone https://github.com/youyou59418-del/ai-agent-campus-submissions.git
cd ai-agent-campus-submissions
python verify_all.py
cd material-scheduler-agent
python run.py --mode mock --input examples/mock/conflict/input.json
```

## 验证

58 项工程测试、13 组离线场景及 13 组真实模型场景通过。真实模式实测 qwen3.8-27b，当前材料包含 15 轮记录，实际工具选择、参数、观察、后续动作和简要依据均保存在 examples/live/。

一键验证与 CI 重新执行离线测试并静态审计已有真实模型记录，不使用密钥或发起新的模型请求。GitHub Actions 使用 Python 3.11 / 3.13，实时结果见工作流页面。

## 数据与边界

实现、合成资料、测试与文档使用 AI 编程工具辅助；人工参与需求、模型与环境配置、交付要求确认。完整记录见项目内 docs/04-ai-usage.md。

资料和规则为合成数据，只保存本地草稿，不接真实发布。替代硬性时段需确认，保存失败明确返回失败。Mock 仅证明流程，真实决策以实际 Trace 为依据；生产能力与已知限制在评估文档中说明。
