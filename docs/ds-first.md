# DS-First 行为策略与受控入口

本文件说明 **DS-first**（DeepSeek 优先）工作流：这是一种行为策略加受控入口，**不是**对 Codex 全部编辑的全局技术拦截。规则要求优先选择 DS；`bin/ds_write.py` 和 `bin/run.ps1` 提供受控的单文件交付入口，宿主其它编辑工具仍可调用。

## 分层职责

- **T0 确定性操作**：文件列举、指纹（SHA-256）、备份、原子替换、明文校验、路径边界检查。不调用模型，结果可复现。
- **T1 单文件完整规格**：`bin/ds_write.py` 面向单个目标文件的生成与落盘，输入目标、任务文件、附加输入，产出候选、备份、receipt。
- **T2 DSH 交接**：多文件或需要工具链的改动，使用 `dsh_bridge` 交接；`--execute` 表示把明确任务提交执行；不带该开关时仅提交交接确认。
- **T3 主设计/裁决/验收**：由主代理负责设计意图、冲突裁决与最终验收。脚本永不代替主代理做验收，状态只停在 pending。

## CLI（与规格一致）

参数：
- `--workspace`：工作区根（必需）。
- `--target`：相对路径目标文件（必需）。
- `--task FILE`：任务说明文件（必需，UTF-8，≤32KiB）。
- `--inputs FILE...`：附加输入（可选，合计 ≤128KiB，单文件 ≤2MiB）。
- `--out DIR`：输出目录（必需，必须为新目录，不得位于受保护根）。
- `--execute`：允许调用 DS、写入目标及执行可选检查计划（默认 dry-run）。
- `--check-plan FILE`：检查计划 JSON（可选）。
- `--repair-rounds N`：0..2，默认 1（有界修复，非无限重试）。
- `--timeout-sec S`：15..3600，默认 180（截止时间控制）。

行为要点：
- **默认 dry-run**：不写文件、不调用 API。
- **退出码**：`dry_run`、`generated_pending_review`、`verified_pending_review` 返回 0；其余返回 1。
- 所有非法输入在模型调用或文件变更前失败。
- `--out` 必须为新目录且不在受保护根内。
- 任务路径、检查计划路径、输入均不得位于 `--out` 之下。

## 备份与部分结果

- 首次写入前对原目标做**一次**备份至 `out/original<后缀>`。
- 候选始终保存，修复失败时**保留部分结果**，状态为 `blocked`。
- 原子写入（同目录临时文件 + `os.replace`），不写其它项目文件。

## 检查与状态

- `generated_pending_review` 与 `verified_pending_review` **都属于主代理待验收**，脚本不自动接受。
- 本地检查逐条执行；检查是**调用方授权的命令**，可能写入文件系统，**不是沙箱**。
- 检查前后校验源文件哈希；若检查改动了声明源，判为 needs-attention，不更新 expected。
- 每个检查前要求剩余时间 ≥1 秒，否则 blocked。

## 时限/范围（无金额上限）

DeepSeek 调用不设金额或调用次数上限，改由**截止时间**和**范围**控制：每次生成与检查共享同一结束时间；API 尝试次数 ≤ 1 + repair_rounds。不声称任何节省比例。

## PowerShell 示例

Dry-run（不写、不调 API）：

```powershell
python bin/ds_write.py --workspace C:\work\proj --target src\app.py --task task.txt --out C:\work\runs\r1
```

实际执行需 `--execute`，且检查计划可选：

```powershell
python bin/ds_write.py --workspace C:\work\proj --target src\app.py --task task.txt --inputs notes.md --out C:\work\runs\r2 --execute --check-plan plan.json --timeout-sec 240
```

Wrapper 默认执行，除非带 `-DryRun`：

```powershell
.\bin\run.ps1 "交付单文件" -Workspace C:\work\proj -WriteTarget src\app.py -TaskFile task.txt -ReceiptDir C:\work\runs\r3 -CheckPlan plan.json
.\bin\run.ps1 "交付单文件" -Workspace C:\work\proj -WriteTarget src\app.py -TaskFile task.txt -ReceiptDir C:\work\runs\r4 -DryRun
```

## 简单 JSON 检查计划

```json
{
  "workspace": "C:\\work\\proj",
  "checks": [
    {
      "id": "syntax",
      "argv": ["python", "-c", "import pathlib,sys; p=pathlib.Path(r'src/app.py'); assert p.exists() and p.read_text(encoding='utf-8').strip()"],
      "timeout_sec": 30
    }
  ]
}
```

`python -c` 内联断言用于轻量路径/内容检查；argv 可执行文件须存在，`timeout_sec` 在 1..3600。

## 安全边界

- 检查命令由调用方显式授权，**不是沙箱**；仅在可信环境中运行。
- 密钥保持本地，永不写入任务、输入、receipt 或日志。
- 指纹拒绝符号链接、路径穿越、私有组件与受保护根引用。
- 进程命令不注入 API key。