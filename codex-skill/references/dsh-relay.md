# 在官方 DSH 中继续对话

本功能提供手动接力：Codex保存明确任务，创建官方DeepSeek Harness的Web会话；你随后在DSH中继续聊天、修改代码，最后导出公开交接记录供Codex验收。不依赖Codex聊天保持运行，但需要本机DSH进程和电脑继续运行。自动识别额度耗尽、自动唤醒原Codex聊天尚未实现。

## 安装和打开

官方项目：https://github.com/deepseek-ai/deepseek-harness 。当前验证的npm版本是`@deepseek-ai/dsh@0.2.0-rc.2`，程序固定版本，未自动追随最新预览版。Node.js本机验证为24.15.0；Windows Python 3.10+，仅桥接代码使用标准库。

在本项目根目录执行一次官方运行时安装（第三方依赖不进入分享ZIP）：

```powershell
npm.cmd install --prefix state/dsh/runtime --registry https://registry.npmjs.org --no-audit --no-fund --save-exact @deepseek-ai/dsh@0.2.0-rc.2
```

使用自己的`DEEPSEEK_API_KEY`环境变量或执行器既有本机配置，然后双击根目录`启动DSH.cmd`。等价命令：

```powershell
python -X utf8 -I bin/dsh_start.py --open
python -X utf8 -I bin/dsh_bridge.py status
```

服务只使用`127.0.0.1`，默认3088端口；占用时不会杀掉其他程序。可用`dsh_start.py --home <目录> --port <端口>`指定独立DSH配置。第一次选择的home保存在本机state，之后保留；本项目作者机器的home在`D:/codex/dsh-relay`。不要同时让两个服务进程写同一个home。

启动器复用已认证的现有服务，不随Codex轮次结束而停止；未注册开机启动或定时任务。每次服务重启会产生新的本机登录URL，启动器自动打开，不把其中凭据写进普通输出。若普通地址显示需要认证，再运行启动器即可。Web界面和后端为官方DSH，桥接程序没有另造聊天模型。

## 交给 DSH

先写一份明确的脱敏任务文件，说明现状、完成标准和未完成事项，再声明需要跟踪的文件：

```powershell
python -X utf8 -I bin/dsh_bridge.py handoff --workspace . --task work/handoff.md --files bin/example.py tests/test_example.py --title "Codex项目接力"
```

命令输出`handoff_id`、`session_id`和本机记录位置。在DSH侧边栏选择该标题；独立cwd的会话可能在“未分组”下面。首次消息只要求确认接收，不立即修改文件，随后你直接在DSH界面继续发任务。默认固定`deepseek-official/deepseek-flash`，推理High；可以用`--effort off/low/high/max`选择。不会自动切换Pro。

DS费用不设调用次数或金额硬上限。模型上下文、服务端输出长度、网络超时及失败检查仍然存在；它们是接口和可靠性边界，不是费用配额。无限额不意味着自动扩任务范围、无限重试或允许修改所有文件。

只读取显式任务文件和声明文件的哈希，不自动扫描聊天、密钥、科研原始数据或全目录。常见密钥签名和敏感文件名会被拒绝，但不保证识别所有敏感信息；任务文件必须先脱敏。声明文件上限16个用于可核对的工作包；较大工作按明确目标交接，不把路径清单误当文件沙箱。

## 交回 Codex

停止在该DSH会话派新任务，待其完成后执行：

```powershell
python -X utf8 -I bin/dsh_bridge.py return --id <handoff_id>
```

程序拒绝导出仍运行、仍有排队消息或读取中变化的会话。报告包含真正用户发言、助手公开文本、声明文件的前后哈希和变化、provider报告的用量。内部运行上下文、技能注入、系统消息、推理及工具参数/结果不会导出；分页不完整则失败，不冒充完整交接。

输出在`state/dsh/handoffs/<id>/packets/return-*.md/.json`，把Markdown路径交给Codex继续。原DSH会话保留，报告标记`main_agent_acceptance=pending`。这不自动唤醒Codex、不撤销DSH权限、不提供OS文件锁，也不监控未声明文件；两边同时修改仍需通过操作约定避免。回交后若要再让DSH写代码，先明确新的单一写者。

## 本地材料与验证

`state/dsh/`包含运行时、启动凭据日志和交接原文，只留本机；`home`包含DSH会话与设置。两者都不能上传。公开包仅包含桥接源码、合成测试和文档。

已在作者机器完成两轮记忆对话，以及DSH真实创建/读取测试文件后导出差异的双向验证。测试目录与实际项目分开。没有通过故意耗尽Codex额度测试自动切换，也没有验证断电恢复、所有插件、全部第三方版本或无人监管长任务。
