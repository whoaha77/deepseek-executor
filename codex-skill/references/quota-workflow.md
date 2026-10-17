# 减少Codex往返的命令

## 批量本地验收

写一个显式计划JSON：`{"workspace":"D:/project","checks":[{"id":"unit","argv":["python","-m","unittest","discover","-s","tests"],"timeout_sec":120}]}`。

`python -X utf8 -I bin/check_bundle.py --plan work/checks.json --out work/check-result-NEW`

一次调用等待所有独立检查；每项都有本地stdout/stderr日志和summary.json。标准输出只有状态、数量、失败ID，不回传整份日志。失败不会重试、不调用模型；主脑先定位失败项，再把有效证据交DS在原范围内修复。检查没有依赖图；有依赖使用team本地DAG。argv由调用者授权，脚本不是沙箱，检查本身可能改工作区；不要运行未经审查的第三方计划。

## 复用发布流程

通过验收的白名单导出manifest需明确main_agent_acceptance=accepted，acceptance.json需绑定ZIP哈希。不要把本机其他窗口的未审查修改夹入导出。

`python -I bin/prepare_release.py --export releases/EXPORT --out work/PUBLISH-NEW --repository https://github.com/OWNER/REPO --tag vX.Y.Z`

它只克隆、核对、暂存并保存publication.diff和plan.json。先审实际diff，再执行：

`python -I bin/publish_release.py --plan work/PUBLISH-NEW/plan.json --acceptance work/acceptance.json --title "具体变更" --body-file work/release-notes.md --check`

去掉--check才正常提交、推送和发布；--check会fetch及读取账号/仓库信息，但不commit/push/release。复用Git已有认证、不打印密钥，核对账号、远端基线、文件哈希、公开下载。不重试不明发布结果，不强推；失败先看publication.json，不重跑去覆盖已有提交或release。本工具用于已经授权的发布，不是默认授权。

## 验收与上下文

不承诺固定节省比例。主要看Codex接管、工具往返和返工是否减少。短交接只需目标、约束、文件索引、未完成事项与验证结果；不要复制全部聊天。相关项目状态按需读，未验证的渲染、真实限额恢复等明确保留未知。
