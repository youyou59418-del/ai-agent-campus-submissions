# AI Agent 校招项目

[自动化检查](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions/workflows/verify.yml)

四份独立可运行的项目实现，覆盖业务工具、真实模型动作循环、可执行测试及实际输入输出。每题在各自目录内保留 README、src、tests、examples、data、docs、依赖文件和环境变量示例。

## 评审导航

| 选题 | 项目入口 | 核心验证 | 工程测试 | 离线 / 真实模式场景 |
|---|---|---|---:|---|
| 素材审核与发布排期 Agent | [material-scheduler-agent](material-scheduler-agent/README.md) | 风险审核、时间冲突、确认边界与草稿幂等 | 58 | 13 / 13 |
| 内容任务规划 Agent | [content-planning-agent](content-planning-agent/README.md) | 预算与截止时间、失败费用、部分成功与故障续跑 | 59 | 15 / 15 |
| 企业知识问答 Agent | [enterprise-qa-agent](enterprise-qa-agent/README.md) | 检索改进、完整原文引用、冲突查证与资料不足处理 | 60 | 14 / 14 |
| 短视频脚本 Agent | [short-video-script-agent](short-video-script-agent/README.md) | 事实来源、分镜时长、品牌检查与版本修正 | 61 | 16 / 16 |

## 环境与一键验证

Python 3.11+，运行和测试仅使用标准库，无第三方运行依赖。

```powershell
git clone https://github.com/youyou59418-del/ai-agent-campus-submissions.git
cd ai-agent-campus-submissions
python verify_all.py
```

也可在任一项目目录运行 python verify.py。真实模型材料为已实际执行的 Trace；一键验证与 CI 仅重新执行离线测试并静态核对现有真实记录，不发起新的模型请求或使用密钥。

## 验证口径

四题共 238 项工程测试、58 组离线场景和 58 组真实模式场景。真实模式中内容规划与脚本项目各有三组缺参前置校验，未调用模型。各题动态决策、正常与异常路径、真实模型用量、失败修正及已知限制均在 docs/03-evaluation.md 与 examples/ 内记录。

## AI 辅助与工程边界

实现、合成资料、测试与文档使用 AI 编程工具辅助；人工参与需求、模型与环境配置和交付要求。每题 docs/04-ai-usage.md 如实记录使用范围和人工参与。Mock 不代表真实模型效果；所有业务资料均为合成。脚本项目使用受限表达库，知识问答使用抽取式引用，预算和时长均有明确的模拟或估算口径。

自动化检查采用 Python 3.11 / 3.13，逐题运行离线测试和证据审计。CI 的实时状态以 Actions 为准。
