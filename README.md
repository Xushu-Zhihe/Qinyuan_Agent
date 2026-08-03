# Q-subagent

Q-subagent 是面向钦原黑板架构的 Windows 本地子 Agent。它使用 Responses API，提供 SQLite 崩溃恢复、工具循环预算、大输出归档、PowerShell 进程树中断、本地 Skills 和显式审批模式。

> `workspace/` 只是 PowerShell 的默认工作目录，不是安全隔离边界。PowerShell 保留完整能力；真正的进程、网络、注册表和文件系统隔离必须由调用 Q-subagent 的外层沙箱提供。

## 项目结构

```text
qsubagent/
  agent.py                    回合编排、预算、重试和中断
  approval.py                 PowerShell 审批请求与决定
  compaction.py               上下文估算与本地压缩
  config.py                   严格配置模型
  runtime/
    database.py               SQLite schema 和迁移入口
    storage.py                事务状态仓库与 JSONL 审计导出
    recovery.py               线程租约与崩溃接管
    artifacts.py              大输出归档和分段读取
    process.py                异步子进程与 Windows Job Object
  security/redaction.py       日志脱敏
tools/
  powershell.py               完整 PowerShell 执行
  read_artifact.py            artifact 范围读取
  read_skill.py               Skills 发现与读取
skills/workspace-inspection/  可实际发现的示例 Skill
tests/                        单元、故障恢复和可选真实测试
workspace/                    PowerShell 默认工作目录
```

## 安装与凭据

```powershell
python -m pip install -r requirements.txt
$env:OPENAI_API_KEY = "你的 API Key"
```

API Key 从 `model.api_key_env` 指定的环境变量读取，不写入被 Git 跟踪的配置文件。然后在 `config.yaml` 填写 `model.name`，兼容服务还需填写 `model.base_url`。

所有布尔值必须使用 YAML 的 `true` 或 `false`；字符串形式的 `"false"` 会被拒绝。超时、轮数和容量配置也会在启动时严格校验。

## 运行

```powershell
python main.py
```

命令行可覆盖 PowerShell 执行模式：

```powershell
python main.py --execution-mode unrestricted
python main.py --execution-mode approval
```

- `unrestricted`：原样执行模型提交的完整 PowerShell 命令，适合已经处于正式外层沙箱的环境。
- `approval`：执行前显示最终命令并创建唯一审批 ID；没有审批通道时默认拒绝。

控制台命令：

- `/interrupt`：取消模型流、审批等待和工具任务，并通过 Windows Job Object 回收 PowerShell 进程树。
- `/continue`：继续因硬预算、重复调用或超时而暂停的同一回合，并开启新的工具预算窗口。
- `/approve <id>`、`/deny <id>`：决定待处理的 PowerShell 审批。
- `/exit`：先中断活动回合，再退出。

## 状态与恢复

权威状态位于 `var/threads/state.sqlite3`。数据库包含 `turns`、`responses`、`tool_calls`、`tool_results`、`compactions`、`events` 和 `leases` 等表。

每次模型 Response、待执行工具调用和回合指针在同一 SQLite 事务中提交。工具调用遵循：

```text
pending -> running -> completed
                   -> failed
                   -> aborted
                   -> outcome_unknown
```

- PowerShell 执行前先提交 `running`。
- 已提交的 `completed` 调用不会再次执行。
- 进程崩溃遗留的 `running` 调用恢复为 `outcome_unknown`，不会自动重试。
- 每个 `thread_id` 只有一个写入租约；持有租约的 PID 已退出时，新实例可立即接管。
- 旧 `state.json` 和 `events.jsonl` 会在首次打开时幂等导入，原文件保留。

`var/threads/<thread_id>/events.jsonl` 仅是经脱敏、会轮转的可读审计导出，不再是恢复依据。

## 预算与错误策略

默认软工具轮数为 16、硬工具轮数为 64、相同且结果不变的调用上限为 3、回合总超时为 600 秒。达到软上限会保存检查点并检查是否需要压缩；达到硬上限或重复调用上限会暂停，并允许 `/continue`。

模型连接由 OpenAI 客户端和回合层双重限制：401/403 立即停止；429 和明确的 5xx 只按 `model_max_retries` 做有限指数退避；超时和模糊网络错误不会自动切换 `previous_response_id` 模式。PowerShell 超时或中断后不会自动重试。

## 大输出与 artifact

PowerShell stdout/stderr 以 64 KiB 块流式读取，完整内容写入：

```text
var/artifacts/<thread_id>/<turn_id>/<call_id>.stdout.log
var/artifacts/<thread_id>/<turn_id>/<call_id>.stderr.log
```

模型只接收退出码、总字节数、截断标记、头尾片段和 artifact 引用。使用 `read_artifact` 可按字节或行读取所需范围。单文件、单线程容量和保留天数由 `artifacts` 配置控制。

## Responses 上下文

- 服务支持时使用 `previous_response_id` 发送增量输入。
- `prompt_cache_key`、服务端压缩和流式输出均按兼容服务的真实能力显式开启。
- 本地重放从 SQLite 最近压缩点之后读取，不扫描完整 JSONL。
- 压缩摘要以普通 `user` 历史数据重放，不提升为 `system` 指令。
- PowerShell 和其他工具默认串行执行，避免任意副作用并发。

## Skills

内置工具链为 `list_skills -> read_skill/read_skill_resource -> 实际工具调用`。项目包含 `workspace-inspection` 示例 Skill。损坏的单个 Skill 会在目录中标记为 invalid，不影响其他 Skill；重名会明确报错；正文和资源均有大小限制。

## 验证

普通测试：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q qsubagent tools skills tests main.py
```

受限沙箱可能禁止异步子进程管道，相应测试会明确跳过。应在允许创建本地进程的环境再次运行，用于验证父子 PowerShell 进程树、审批执行和完整 Skill 工具链。

真实 Responses API 测试默认跳过，不会用 Fake Client 冒充真实验收：

```powershell
$env:QSUBAGENT_RUN_REAL_API = "1"
$env:OPENAI_API_KEY = "你的 API Key"
$env:QSUBAGENT_MODEL = "模型名称"
$env:QSUBAGENT_BASE_URL = "兼容服务地址"  # 使用官方地址时可不设
python -m unittest tests.test_real_api -v
```

该测试执行“发现 Skill -> 读取 Skill -> 多轮工具 -> 中断 -> 重启 -> 恢复 -> 完成任务”。只有它在目标服务和真实凭据下通过后，才能宣称真实模型完整验收完成。
