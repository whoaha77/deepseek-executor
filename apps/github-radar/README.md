# GitHub 项目发现器

持续发现 GitHub 上有价值的项目，优先科研与效率工具，兼顾 AI、Skill、MCP、开发与新领域探索。项目筛选与状态管理在本地完成，使用 DeepSeek Flash 合批理解公开材料。

**[查看最新报告与历史](reports/README.md)**

**[历史优选 Top 50](reports/top-50.md) · [Codex 部署分析](reports/codex-deployment.md)**

## 历史榜单与 Codex 部署规则

历史 Top 50 从累计分析通过的项目中持续维护，每个仓库使用最新有效分析，只占一个位置；不足50个就显示实际数量，不凑数、不冒充全 GitHub 排名。榜单附入选原因、分数、类型、最近分析时间及每个项目的详细分析。长期历史和榜单不因30天轮次报告清理而消失；分析超过30天会标为待复核，不再作为高价值部署候选。

**用户规则：只提示高价值候选，等用户主动说安装后才部署；不自动安装，不每轮询问。**本查找器没有安装执行入口，不修改 Codex 技能目录、MCP 配置或系统依赖，不运行候选仓库命令。

Flash 为每个项目补充：集成类型、优先级、对 Codex 的具体价值、与现有能力重叠、依赖与费用、权限/风险、部署前下一步核查。高价值提示要求参考分至少75、适配优先级为高且分析未过期；这些门槛不能代替组件核查或用户安装指令。仓库型 Skill 集合需逐组件核查，不能整库盲装；普通应用和参考资料不作为直接部署候选。

Skill 是工作流与资源，MCP 是工具连接，CLI/代码库通常属于本机或项目依赖；它们的接入方式不同。依据 [官方 Skills 文档](https://developers.openai.com/plugins/concepts/skills) 和 [Codex MCP 文档](https://developers.openai.com/codex/mcp) 区分类型。本机实际安装状态仍需安装任务中核验，不能仅凭项目名称或 `known` 反馈推断。

榜单和部署分析会在每次正常扫描后自动刷新；这里只生成建议，不会主动安装或弹出逐项安装询问。新候选获得一定的取证优先权，避免一直重复分析相同热门项目。

## 当前行为

- 每轮轮换 6 条主题查询，保存发现来源，所有入库项目立即进入分析队列；不声称遍历全 GitHub。
- GitHub 数字仓库 ID 去重；保存真实时间跨度的 Star 快照，不虚构近 7 天增长。
- Flash 每批最多 6 个项目，取证后立即分析并逐批保存，不设每轮12个的项目限额，返回 JSON 后核对项目集合、字段、数值和逐字引用；未经运行测试的项目明确标注。
- 缓存按材料、兴趣、提示词/schema 版本区分；Star 和 pushed 时间变化不触发全文重读。
- 当前 **DS 金额不限额**（`daily_budget_cny: null`），不设每日/月度硬上限。调用费用及未知项仍记录；如自行配置正数上限，才按北京时间日预算控制。
- 报告分为成熟可用、新兴潜力、值得借鉴；优先科研和效率，每轮最多展示 5 个。无 DS 结果时可列客观候选，明确不作为语义推荐。
- 报告保留最近 **30 天**，且至少保留最新 **10 份**；仅删除清单登记、内容未改动的历史报告。Git 历史不重写。
- 日常自动提交仅限 `reports/`；有暂存的代码修改时拒绝夹带提交。源码迭代独立验证、提交。

## 目录与安装

需要 Windows、Python 3.12+、Git、当前用户的 GitHub 凭证，以及 [deepseek-executor](https://github.com/whoaha77/deepseek-executor) 的 `bin/ask.py`。本查找器没有第三方 Python 包依赖；`requirements.txt` 无需额外安装。

```text
scripts/                查找器完整源码
tests/                  不联网的行为测试
run.ps1                 命令行入口
schedule.ps1            当前用户的定时任务安装和检查入口
config.example.json     不含密钥的配置示例
reports/                版本管理中的报告及入口
config.json             本机配置，不上传
state/                  SQLite、预算与互斥锁，不上传
work/                   原始证据、模型输入输出，不上传
logs/                   调度日志，不上传
backups/                本机备份，不上传
```

本机部署在 `D:\tools\deepseek-executor\apps\github-radar`，默认自动找到上层执行器。单独克隆时，设置环境变量 `DEEPSEEK_EXECUTOR_HOME` 为执行器根目录；不要指向其 bin 子目录。

```powershell
$env:DEEPSEEK_EXECUTOR_HOME = 'D:\tools\deepseek-executor'
Copy-Item config.example.json config.json
.\run.ps1 scan --dry-run
.\run.ps1 scan --no-publish --limit 4
```

执行器按其自己的规则读取 `DEEPSEEK_API_KEY` 或现有本机配置，本项目不保存密钥。GitHub 优先复用 `GH_TOKEN`/`GITHUB_TOKEN`，否则非交互调用 Git Credential Manager。仅查询公开项目，发布到配置的私有仓库；不打印或提交凭证。

已有本机配置时不要覆盖；先备份并比较。仓库默认目标为 `whoaha77/github-radar-reports`，发布前核对 origin、main 分支及远端 private 属性，禁止 force push、reset 或自动丢弃冲突。

## 使用

```powershell
.\run.ps1 scan                         # 扫描、分析、写报告并发布
.\run.ps1 scan --no-model --no-publish # 不调用模型、不发布
.\run.ps1 scan --dry-run               # 展示计划，不请求GitHub/DS、不推送
.\run.ps1 status                      # 最近运行与今日预算占用
.\run.ps1 catalog --no-publish        # 不调用模型，本地重建历史Top50和部署建议
.\run.ps1 library --query 'simulation' # 离线检索全部历史库，包含尚未分析的项目
.\run.ps1 library --status 待分析 --limit 10
.\run.ps1 catalog --enrich --limit 16 # 用Flash补充已有证据的部署分析并发布，记录费用，不按日金额暂停
.\run.ps1 feedback 123456 saved --reason '准备试用'
.\run.ps1 feedback 123456 known --reason '已经使用'
.\run.ps1 feedback 123456 ignore --reason '不适用'
.\run.ps1 feedback 123456 clear
.\run.ps1 cleanup                     # 仅预览可清理报告
.\run.ps1 cleanup --apply             # 按既定保留规则清理；下次报告发布时同步删除
python -m unittest discover -s tests -p 'test_*.py' -v
```

`saved`、`tried_ok` 项目加入后续关注；`known` 在材料变化前不再推荐；`ignore`、`later`、`tried_failed` 暂停筛选，`clear` 恢复。每条报告列出仓库 ID，便于反馈。

## 定时与发布

```powershell
.\schedule.ps1 -Install -DryRun
.\schedule.ps1 -Install
```

任务名 `GitHubRadar-whoaha77`。在当前 Windows 用户登录会话中，每 5 分钟做一次轻量时间检查；新项目搜索只在开机后 30 分钟及随后每 120 分钟的时间窗口执行一次；存在待分析项目时，开机半小时后每次检查继续处理队列，不等下个两小时窗口，正常触发误差不超过约 5 分钟。休眠错过的窗口合并为一次，不补发多轮请求。

该任务不保存 Windows 密码、不要求管理员权限，不唤醒休眠的电脑。**尚未登录 Windows 时无法使用本用户凭证运行；登录后补检查。**采集与分析阶段设置 10 分钟时间预算，发布另有请求超时；任务外层 15 分钟兜底，避免无限等待。进程互斥防止手动与定时运行重叠。

首次部署先审查白名单文件、测试、建立私有远端并提交完整源码；后续扫描仅提交报告目录。源码版本由开发迭代时单独提交。推送失败保留本地文件与提交；远端冲突不会强行覆盖。

## 预算和证据的边界

当前费率根据 [DeepSeek 官方人民币价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing) 于 2026-10-16 核对。预算按 Flash 高峰输入未命中缓存 2 元/百万 tokens、输出 8 元/百万 tokens 的保守上界计算，不将缓存重复计入输入。实际扣费需供应商账单核对；本程序预算不包含其他工具的 DS 调用。

价格超过 30 天未核验时停止新增付费分析，仍可离线查看历史报告；核对官方页后更新本机配置的费率与 `verified_date`。当前不设金额上限；未知调用仍保留估计记录，不因超时算零元。仅在显式启用正数上限时按日控制。

README、Release、Star 及作者描述只是筛选材料。本程序不安装/运行候选仓库，不声称完成安全审计或性能验证。引用行号对应本机固定证据快照，远端后续修改可能改变行号。

首版采用官方仓库搜索和 README/Release 证据，不依赖 find-skills CLI、Trending HTML、复杂多 Agent 或向量数据库。查询与偏好可配置；单轮是有预算的抽样覆盖。推荐质量仍需要结合真实使用反馈逐步验证。

## 全量历史库、经典回溯与分享

`reports/library/` 是累计发现库，包含已分析、待分析、需复核、已忽略、暂缓与试用失败记录。按 GitHub 数字 ID 去重，改名保留旧名称与首次发现时间；JSON 包含已有分析和证据链接，可离线按名称、别名、简介、主题或摘要搜索。它与 Top50 是两种视图，不因报告过期而清除项目。完整模型证据快照和分析版本另保存在本机 SQLite 与 work/ 中，不上传本机凭证或日志。

每轮保留经典回溯和趣味探索来源；经典查询覆盖创建超过两年的项目，不要求近期创建或近期提交。各查询成功后向下一页推进，滚动日期改变不重置页码；受 GitHub 搜索每查询前1000项及本机抽样/时间预算约束，不能保证覆盖所有优质项目。忽略记录继续保留，避免反复推送。对同样材料命中语义缓存时不重复付费，Star变化不使缓存失效。

`reports/discoveries.md` 分享好玩、有趣、有用的项目，老项目也可重新被发现；它不以是否适合 Codex 安装为门槛。Flash 在同批分析中写中文分享和探索点，不另开 GPT 调用；旧分析可复用 DS 摘要配合本地提示。`reports/sharing.json` 是小型发布包，按上次分享时间轮换，不反复占用前三名。尚未运行体验的项目明确标识未知，不把作者说明当实测结果。

发布成功后，本机用 Windows 通知展示最多三个精选的中文摘要，点击打开私有 GitHub 分享页，需要浏览器已登录有权限的账号。仅新分享内容触发，同一项目至少间隔14天；相同内容不重复通知。通知关闭、会话不可用或系统抑制展示时，GitHub页面仍可阅读，程序不会把“提交到Windows”声称为“用户已读”。本机可设置 `desktop_notifications: false` 关闭通知。DS API不能直接向 Codex 聊天发消息，因此没有另设唤醒GPT的聊天定时任务。

通知正文和“查看最新消息”按钮都直接打开 `main` 分支的[最新中文分享页](https://github.com/whoaha77/github-radar-reports/blob/main/reports/discoveries.md)，可收藏这个固定入口。2026-10-17已由用户实际确认收到测试通知且点击正常打开。手动重发测试通知如下，不调用DS，也不改动日常通知去重记录：

```powershell
powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File .\scripts\notify.ps1 -PacketPath .\reports\sharing.json -Test
```

## 执行器与消耗

日常调用链：Windows任务 → 本地Python搜索/分页/去重/缓存 → `bin/ask.py` 的 DeepSeek Flash 文本分析 → 本地校验/历史库/报告 → Git发布 → 本机通知。任务设计、重要取舍和开发验收由主脑负责；技能指令本身不会自动路由，实际分流由上述脚本实现。普通扫描不使用 GPT/OpenAI API，也不依赖 Codex 开着；开发、验收或用户在聊天中追问仍会使用当前助手。

每批DS调用先预留预算，固定Flash、thinking off；单次API请求不自动重试，队列仅对有日志证明的连接重置或HTTP429/5xx最多追加两次安全尝试，每次单独预留预算并保存独立记录；校验精确项目ID、字段、评分与原文引文。超时、截断或校验失败不算成功。已有用量、耗时和估算金额可核查，实际供应商扣费未知；没有同任务GPT基线时不声明节省百分比。不能将低API费用直接解释为Codex订阅消耗的下降。


## 项目到达即分析（v0.4.0）

所有新发现都在本轮交给Flash分析，不再先筛18个、再仅分析12个。积压优先补齐，完成后继续原定频率搜索；分析任务逐批提交到SQLite，时间窗口结束后由下次5分钟检查接续，已经合格的历史项目不反复付费。三个本地线程仅并行读取独立项目的公开README，各用独立SQLite连接；普通队列以README和GitHub元数据为依据，Release在专项取证时可用。

不设DS金额上限；供应商限流、网络和进程时间边界仍有效。只有自行启用正数日上限时，预算不足才按日暂停；价格过期在核验配置更新后恢复；连续网络失败在原请求及最多两次安全重试后暂停，排查后用 `run.ps1 scan --resume-analysis` 恢复。JSON语法错误只额外交给Flash做一次独立格式修复（同样先预留预算、保留原文、不执行模型文字），修复后重新校验全部项目ID与逐字引用；仍不合格才隔离。模型格式或证据不合格的单个项目标为“分析受阻”，保留原因，不自动原样重派；修正规格后可用 `run.ps1 retry-analysis 仓库ID` 重新排队；同批其他合格结果独立校验后保存。列表中的“已分析”表示通过结构与引用校验的材料分析，仍不等于安装测试或安全审计。
