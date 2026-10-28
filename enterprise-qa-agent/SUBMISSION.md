# 项目信息

- 选题：企业知识问答 Agent
- 仓库：[youyou59418-del/ai-agent-campus-submissions](https://github.com/youyou59418-del/ai-agent-campus-submissions)
- 项目目录：[enterprise-qa-agent](https://github.com/youyou59418-del/ai-agent-campus-submissions/tree/main/enterprise-qa-agent)
- 评审入口：[README.md](README.md)
- 本地验证：60 项工程测试、14 组离线场景和 14 组真实模式场景通过。
- 自动化检查：[GitHub Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions/workflows/verify.yml)
- AI 辅助范围及人工参与：[docs/04-ai-usage.md](docs/04-ai-usage.md)

## 可复制运行

```powershell
git clone https://github.com/youyou59418-del/ai-agent-campus-submissions.git
cd ai-agent-campus-submissions/enterprise-qa-agent
python verify.py
python run.py --mode mock --input examples/mock/conflict/input.json
```

真实模型运行与环境变量见 README。真实模型 Trace、合成数据和测试均位于本目录。最终提交文本中的 commit SHA 对应合集仓库的完整代码与验证材料。
