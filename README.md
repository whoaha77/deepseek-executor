# DeepSeek Executor

给 Codex 配套的轻量执行工具：确定性任务留给本地脚本，明确文本任务交给 DeepSeek Flash，需要工具的小工作包交给受监管 worker，主脑负责设计和验收。

支持 Windows 10/11、PowerShell 5.1+、Python 3.10+。文件 worker 另需 Codex CLI，并要求当前 DeepSeek API 账户支持配置中的 `deepseek-flash` 与 Responses 协议；模型可用性以你的账户和服务端为准。本项目不提供 API 密钥或账户。

## 安装

1. 下载仓库 ZIP 或克隆仓库，解压到普通项目目录。
2. 在当前 PowerShell 会话设置自己的密钥，再运行配置步骤：

```powershell
$env:DEEPSEEK_API_KEY = "填你自己的密钥"
python -I install.py --configure
```

配置会写入本机 `worker-home/config.toml`，该文件被 Git 忽略；不要上传或转发它。T1 也可只使用环境变量。安装程序改已有文件前先备份，不启动模型、不注册计划任务、不覆盖全局 AGENTS.md。

3. 可选安装 Codex 技能：`python -I install.py --install-skill`。技能中的路径会自动替换为本机解压位置。直接运行 `python -I install.py` 只预览路径。

## 使用

```powershell
# 本地工具，无模型调用
python -I bin/toolbox.py list
# Python 代码按符号读取；不会执行源码
python -I bin/code_context.py find --root bin --query load_key --limit 3
# 纯文本调用（会产生你的 DeepSeek API 费用）
python -I bin/ask.py "把这句话改短：这是一个示例。"
# 看路由，不执行
powershell -NoProfile -ExecutionPolicy Bypass -File bin/run.ps1 "运行测试" -DryRun
# 看预算，不调用模型
powershell -NoProfile -ExecutionPolicy Bypass -File bin/delegate.ps1 -Spec "检查规格" -Workspace . -BudgetProfile standard -DryRun
# 看计划，不执行；加 --execute 才运行
python -I bin/team.py --plan examples/plan.json
```

一个 worker 只接一个目标；实现和本地验证分开计时，共同受团队总截止约束。预算档位是估计起点，可以按任务覆盖并说明理由，不自动续时。主脑检查实际 diff 后再开启后续付费阶段。详见 [预算与阶段](docs/budgets.md)。

需要读取较大 Python 文件或多份历史说明时，可使用 [本地上下文工具](docs/local-context.md)：先符号定位/索引检索，再取必要原文。索引仅收显式指定的 Markdown/TXT，普通小文件直接读取即可。工具不启动后台服务，也不调用模型；原文变化后拒绝使用过期摘录。

## 验证

```powershell
python -X utf8 -m unittest discover -s tests -v
```

测试使用合成数据、mock 和本地子进程，不读取你的密钥，不发模型请求。公开包未在其他人的真实 API 账户上进行兼容性验证。通过测试不等于已证明减少 Codex 订阅消耗；API 费用、缓存和订阅额度分开看待。

## 内容与边界

- `bin/`：T0/T1/T2、预算、监管、评审和记录工具。
- `worker-home/config.example.toml`：新建配置模板，不含实际密钥。
- `codex-skill/`：可选技能模板；`install.py` 写入本机路径。
- `examples/`：只读可预检示例；不会默认启动模型。
- `tests/`：可离线运行的回归测试。

不包含作者的会话、研究数据、运行日志、历史账本、备份或私人全局配置。监管是事后事件监测，不是文件权限沙箱；文件改动仍需检查。敏感路径筛选不能代替调用者审阅源码里嵌入的信息。

当前未指定开源许可证；公开可见不等于已授予任意再分发许可。
