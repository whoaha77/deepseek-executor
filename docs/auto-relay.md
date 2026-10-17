# 自动限额接力（显式装载任务）

这是独立的本地监管程序。装载一个明确任务后，Codex额度耗尽时自动把该任务交给官方DeepSeek Harness，在隔离副本执行；你可以在DSH侧边栏对应会话继续聊天。无需耗尽后再由Codex启动程序。原项目不自动写入或合并。

前提：已按[dsh-relay.md](dsh-relay.md)安装并启动DSH；Codex CLI已登录自己的账户且支持`app-server --stdio`。只读查询走官方`account/rateLimits/read`，不调用推理、不读取或导出auth.json。参考[官方App Server文档](https://developers.openai.com/codex/app-server)。本机共享daemon端口连接失败，独立stdio查询已实测可用；它返回原桌面聊天`notLoaded`，不能据此判断桌面已停止。

## 装载和运行

任务文件必须写清一个可独立完成的目标、必要输入、输出和本地验收要求。显式文件清单最多16个，已有文件上限2MiB且须UTF-8，声明新文件可以不存在。这些限制来自现有交接证据边界，不是DS金额/调用次数限额。只复制列出的文件；大型项目依赖未装载时可能无法构建，DSH应报告阻塞，不能自行扩大目录范围。

```powershell
# 将THREAD_UUID换成要交回的真实Codex聊天UUID
python -X utf8 -I bin/auto_relay.py arm --workspace D:/my-project --task work/task.md --files src/example.py tests/test_example.py --thread-id THREAD_UUID
# 用上一步返回的job_id；默认每60秒查询，无模型轮询
python -X utf8 -I bin/auto_relay.py daemon --job job-HEX
python -X utf8 -I bin/auto_relay.py status --job job-HEX
python -X utf8 -I bin/auto_relay.py stop --job job-HEX
```

实际job_id格式为`job-`加32位十六进制。监管进程隐藏运行，`process_started`只证明已启动进程，后续状态见job.json及daemon.log；重复运行由OS进程锁拒绝。没有注册开机启动或系统定时任务。电脑关机、DSH停止或监管进程被结束都会影响接力，重启前检查状态。前台排查可用`run --job ... --interval 60 --once`；`--state`是全局参数，须放子命令前。

可选`arm --rollout <显式JSONL>`读取这个聊天的最新token_count额度元数据作为查询不可用时的后备。装载时核对首条session_meta的聊天ID，不导出对话。只读最后128KiB/200条，信号须比装载时间新且在180秒内；普通聊天文字、HTTP429、断网或旧日志都不当作额度耗尽。内部日志格式变化会降为unknown，不猜测执行。

## 触发与交回

连续两次明确额度耗尽、相隔至少5秒才派单，正常默认轮询60秒；未知额度不会触发或假定恢复。优先匹配指定额度桶，过期窗口、错误桶、格式不明均拒绝推断。模型仅`deepseek-official/deepseek-flash`、默认High。

执行使用装载时冻结的任务文本及文件副本，不是当前聊天的内部上下文。副本在`state/auto-relay/jobs/<id>/workspace`，保留原文件基线哈希。原目录后来有改动，会在交回时列出；不会自动覆盖。任务文本被更改则中止自动派单。副本避免脚本本身同时改原文件，**不是操作系统文件沙箱**；DSH权限仍受官方运行配置及任务约定约束。不要并发给同一副本下发冲突任务。

阶段：armed → dispatching → dsh_ack → submit_pending → dsh_active → dsh_ready → return_ready。两个远端写入步骤先落状态，响应丢失或进程中断进入needs_attention，不盲目重派。额度未知、普通只读元数据暂时失败会保留阶段等待。停止监管不终止已经开始的DSH任务，也不撤销它的文件访问权限。

DSH会话无运行任务、无排队消息，连续空闲至少10秒后自动导出公开交接记录。它只是某个时刻的结果，不代表任务成功，`main_agent_acceptance`始终pending；DSH之后仍可继续聊天。Codex额度重新可用后生成`交回通知.md`，请把该路径交给Codex验收，核实差异、测试与原目录冲突，再决定合并。

`arm --auto-resume`额外授权在额度恢复后，对装载的同一个Codex聊天执行一次官方`codex queue --thread ...`。请求前保存尝试标记；失败或结果不明不重试。`queue_accepted`仅表示CLI接受排队，不等于桌面已开始执行。本机最初共享控制端口失败，后续实测向原聊天排队返回0成功；仍不把CLI接受等同于UI执行。默认需显式开启；结果也可从本机通知文件取回。它不创建新的Codex聊天、不自动消耗重置机会、不购买额度。

## 验证和隐私

本机已完成模拟耗尽/恢复信号驱动的真实后台进程与DSH文件执行测试，原目录未变、回传成功。真实账户只读额度查询已通过。尚未故意耗尽账户进行真实服务端限额端到端测试，亦未证明本机原桌面聊天能被自动唤醒。不能把模拟信号测试表述成真实额度耗尽测试。

`--test-signals`仅用于独立测试任务：装载后用`inject --job ... --signal exhausted|available|unknown`写入持久测试信号，后台照常运行。普通任务拒绝注入。此模式不会读取真实账户额度，不能用于证明真实限额识别。

本机state、日志、任务文本和聊天不进入公开分享包。常见密钥签名检查不是全面的数据脱敏器，装载前应确认任务和显式输入可发送给DS。DS没有金额或调用次数硬上限；接口失败、证据完整性、工作范围和主脑验收仍然有效。
