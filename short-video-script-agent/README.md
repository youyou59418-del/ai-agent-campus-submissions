# 短视频脚本 Agent

帮助创作者在事实和时长约束下得到可编辑脚本。模型编排标题、分镜、旁白表达和停留时间，观察时长/品牌检查后修改或停止。产品事实完整引用实际检索到的素材；没有数据时不会批准“效率提升 80%”或“自动生成字幕”等新增结论。

**本次结果：61 项工程测试、16/16 组离线场景、16/16 组真实模式场景、CLI 和独立证据核对通过。** 三组缺参路径不调用模型，其他 13 组使用 `qwen3.8-flash`。当前验收材料共 18 轮记录，批准 9 份脚本，独立核对 35 个分镜、9 条事实引用。

本题采用**受限结构化编排**：模型选择合成表达库中的中性句式，并组合完整素材事实、视觉模板和分镜时间。它可以改编排与时间，未实现任意自由文案的语义事实证明。表达库是开发时预置的合成资料，不代表真实品牌方或平台审批。

## 评审入口

- [需求与验收](docs/01-requirements.md)
- [工具循环、事实和检查绑定](docs/02-solution.md)
- [实际生成、检查与限制](docs/03-evaluation.md)
- [AI 使用与个人贡献](docs/04-ai-usage.md)
- [复盘](docs/05-retrospective.md)

直接查看真实成品：[19 秒脚本 Markdown](examples/live/overlong_repair/output.1.script.md)、[可编辑 JSON](examples/live/overlong_repair/output.1.script.json)、[38→19 秒修正 Trace](examples/live/overlong_repair/trace.1.json)。离线策略在同类用例中为 38→28 秒，两份记录独立保存，不把 Mock 当作模型效果。

## 环境与运行

Python **3.11+**，运行和测试仅依赖标准库。本次环境为 Windows / Python 3.13.12。从项目根目录执行：

```powershell
python verify.py
python run.py --mode mock --input examples/mock/overlong_repair/input.1.json
python audit_evidence.py --mode mock
```

可选 `python -m pip install -e .` 后使用 `short-video-script` 命令；直接 `run.py` 无须安装。默认文件：

- `.runtime/output.json`：状态、风险、所有草稿及实际检查结果。
- `.runtime/output.script.json`：当前通过检查的可编辑脚本。
- `.runtime/output.script.md`：标题、分镜、旁白/字幕、秒数和来源表。
- `.runtime/trace.json`：模型动作、参数、真实工具结果与后续动作。

本次无通过脚本时，公开脚本导出会改为“未通过”的说明，不遗留上次成品冒充本次成功。未批准初稿只在结果/Trace 的 drafts 中保留。没有视频生成、真实发布或线上服务。

## 输入

```json
{
  "request_id": "creator-001",
  "topic": "字幕功能介绍",
  "audience": "新手创作者",
  "platform": "抖音",
  "target_seconds": 30,
  "style": "自然"
}
```

主题、受众、平台、目标时长、风格均必需。缺字段返回 NEEDS_INPUT，不调用模型。平台支持抖音/小红书/B站，风格支持自然/轻松/专业。受众与平台保留为原始创作 brief，未验证平台传播效果或受众喜好优化。

`target_seconds` 按**最大允许估算时长**解释，不要求填满。估算不超过目标且满足规则即可；如果业务要求接近某个时长区间，需要后续扩展下限和误差校准。可选 initial_draft 用于检查并修改已有草稿，形状见 overlong_repair 输入；可选 required_claims 表示不可默默省略的事实要求，没有来源时明确无法满足。

## 真实模型

先在本机安全配置密钥，`.env.example` 不会自动加载：

```powershell
# 如刚写入 Windows 用户环境变量，可刷新，不会显示密钥。
$env:DASHSCOPE_API_KEY = [Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY','User')
$env:AGENT_KEY_ENV = 'DASHSCOPE_API_KEY'
$env:AGENT_MODEL = 'qwen3.8-flash'
$env:AGENT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
python run.py --mode live --input examples/live/overlong_repair/input.1.json

# 下列重跑会实际使用 API 配额。
python evaluate.py --mode live --case all --out examples/live --workers 2
python audit_evidence.py --mode live
```

模型固定为用户指定项，额度不足停止后由用户更换；不自动切换模型或 Mock。批量验证通过共享停止信号阻止后续配额错误调用，已发出的并发请求可能仍返回。token 记录不代表免费计费或账户账单。

当前完整验收材料（全套执行及一组定向回归）为 **97 次响应，输入 346,905、输出 8,108 token，共 355,013**。历史业务回归单独存放，不混入当前通过率；token 记录不表示账户总量或计费归属。

## 事实与时长

默认 P01 只有“支持字幕。”，没有自动生成或效果百分比信息。产品事实分镜必须使用 speech_id=FACT，并完整匹配实际检索返回的 source_id、fact_id 和 source_quote。其他旁白、标题、视觉由合成表达库 ID 编排，模型不能填入任意新产品承诺。

估算对每个分镜计算：

`预计秒数 = max(ceil(旁白单位数 / 每秒单位数), hold_seconds, 最小分镜秒数)`

再逐镜求和。默认汉字每字一单位、连续英文/数字词每词两单位、每秒四单位、每镜至少两秒。它是确定性启发式，不是实测 TTS 或视频长度；标题视为画面文字，不另作旁白估算。

每版草稿有独立 ID 和内容哈希；估算和品牌检查均绑定该版本和实际读取的数据快照。新稿不能复用旧检查，来源或规则变化会使记录失效。通过两项检查后交付；系统阻止已有合格版本时继续无必要改稿。

## 修改后复核

可以在 Markdown/JSON 中编辑；库外自由改写不再属于自动事实保证范围，需人工审核。检查 JSON 导出：

```powershell
python validate_script.py --input examples/live/overlong_repair/input.1.json --script examples/live/overlong_repair/output.1.script.json
```

修改结构化初稿中的表达 ID、素材事实或停留时间后，可以作为 initial_draft 重新运行 Agent。扩展表达库须自行进行事实和品牌审阅，不能把修改库文件当成已经验证新承诺。

task_status=SCRIPT_READY 才有可交付 script；其余为 NEEDS_INPUT、NEEDS_MATERIALS、TOOL_FAILURE、UNFULFILLED 或 INCOMPLETE。退出码只有 SCRIPT_READY 为 0，其余为 2。默认最多 18 次模型动作、4 份草稿；达到上限明确未完成。项目与自动化检查入口见 [SUBMISSION.md](SUBMISSION.md) 和 [Actions](https://github.com/youyou59418-del/ai-agent-campus-submissions/actions)。
