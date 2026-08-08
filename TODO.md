# Qinyuan_Agent-main 改造 TODO

## 0. 已选择的架构

本 TODO 采用“宿主机 Agent/MCP + bat 管理工具 Sandbox”的架构，替代“把整个 main provider 放入容器”的方案。

```text
qinyuan-bat
    -> 以普通 provider command 启动 Qinyuan_Agent-main（宿主机进程）
    -> stdin 发送一次任务

Qinyuan_Agent-main
    -> Agent 自主判断是否需要 Nmap
    -> 需要时才启动 main 私有的 Nmap MCP Server
    -> MCP 只验证参数并返回 sandbox_command
    -> main 校验 sandbox_command
    -> main 通过 bat 的本地 Sandbox Bridge 请求执行

qinyuan-bat SandboxService
    -> 第一次命令时按需创建本 run 的工具容器
    -> 同一 run 的多次 Nmap 调用复用同一容器
    -> provider run 结束时销毁容器并撤销 bridge token
```

### 固定职责

- `Qinyuan_Agent-main` 是与 Codex、Claude、Kimi 同级的外部 provider，不是 bat 内部 Python service。
- main 拥有 Agent loop、thread、模型调用、MCPManager、Nmap MCP Client/Server 和是否调用 Nmap 的判断。
- main 不拥有 Blackboard、Intent/Execution 调度、Docker runtime 或 Sandbox 生命周期。
- main 不 import bat 内部模块；bat 也不 import `AgentRunner`、`ThreadRepository` 或 MCP 实现。
- Nmap MCP 仅供 main 使用，不注册成 bat、Codex、Claude 或 Kimi 的通用 MCP。
- 首版只迁移 Nmap。Nuclei、FFUF、HTTPX、Subfinder 及其 MCP 均不在本 TODO 范围内。

### 当前基线

- 仓库：`Qinyuan_Agent-main`
- 分支/提交：`main@4aa98ee`
- `main.py -> qsubagent.app.run()` 当前只启动交互式 console。
- `AgentRunner` 已有工具循环、thread lease、取消、恢复、artifact、输出脱敏和重复调用限制。
- `MCP/` 当前只有 `.gitkeep`，尚无 MCPManager 或 Nmap MCP。
- `Tools/powershell.py` 当前可执行宿主机 PowerShell；该能力不能直接用于 bat 的宿主机 provider 模式。

## P0：冻结跨仓库协议

必须先完成协议文档和 fake contract test，再实现真实 Docker/Nmap 链路。

### P0.1 Provider 进程契约

- [ ] 新增非交互式 provider 入口，例如 `python -m qsubagent.provider` 或 `qinyuan-agent`。
- [ ] stdin 只接收 bat 生成的一次性 UTF-8 任务文本，读取至 EOF 后开始执行。
- [ ] main 不要求 bat 调用交互命令、Python API 或内部 service。
- [ ] stdout/stderr 可输出受限进度；stdout 最终只能有一个可解析的 `BLACKBOARD_RESULT:` 结果行。
- [ ] 进程退出码和结构化结果同时表达 `completed | failed | aborted | timeout | outcome_unknown`。
- [ ] 保留当前交互式 console 入口，但 bat 永远不依赖 `/continue`、`/approve`、`/deny` 或 `/exit`。

### P0.2 Run context 契约

bat 为每次 provider invocation 创建受控的 `run_context.json`，main 只读取，不允许模型修改以下字段：

- [ ] `protocol_version`
- [ ] `agent_run_id`
- [ ] `thread_id`
- [ ] `tool_profile`：首版仅 `no_tools | nmap_optional`
- [ ] `authorized_targets`
- [ ] `deadline`
- [ ] `work_dir` / artifact scope
- [ ] `result_contract`
- [ ] `sandbox_endpoint`

bat 通过 `QINYUAN_RUN_CONTEXT_FILE` 传入路径。文件不得包含模型 API key 或 Sandbox bearer token。

### P0.3 Sandbox Bridge 契约

main 通过仅监听 loopback 的 bridge 调用 bat。首版固定请求：

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "tool": "nmap",
  "arguments": ["-sT", "-Pn", "-n", "-p", "80,443", "127.0.0.1"],
  "timeout_seconds": 60
}
```

固定响应：

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "status": "completed",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "timed_out": false,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "artifact_refs": []
}
```

- [ ] 请求体不接受 `agent_run_id`、`sandbox_id`、Docker image、network 或任意 executable path。
- [ ] bearer token 由 bat 单独写入受保护的临时文件，并通过 `QINYUAN_SANDBOX_TOKEN_FILE` 传给 main；禁止放进 CLI 参数、任务 prompt 或日志。
- [ ] main 启动时读取 token 一次并保存在内部对象中；不得放入模型上下文、tool schema、MCP 参数或 artifact。
- [ ] 支持按 `call_id` 取消活动命令；重复 `call_id` 必须幂等或返回明确冲突，不能重复执行 Nmap。
- [ ] main 和 bat 各自保存同版本协议样例和非法样例，并用 contract test 防止独立修改后静默漂移。

### P0 验收

- [ ] fake provider runner 能从 stdin 执行一次任务并生成合法 `BLACKBOARD_RESULT`。
- [ ] fake Sandbox Bridge 能覆盖成功、非零退出、超时、取消、截断和 `outcome_unknown`。
- [ ] 未知字段、错误类型、非法状态、空 `call_id` 和过期 deadline 在任何 MCP/命令启动前失败。
- [ ] main 的 contract tests 不 import qinyuan-bat。

## P1：增加非交互式 Provider CLI

建议新增：

```text
qsubagent/provider.py
qsubagent/provider_models.py
qsubagent/result.py
tests/test_provider_cli.py
```

- [ ] 将 `qsubagent.app` 中 client、settings、repository、registry 的创建拆成可复用 factory，避免复制 console 初始化代码。
- [ ] provider CLI 校验 run context 后创建本 run 独立的 `ThreadRepository`、state 目录和 artifact scope。
- [ ] provider CLI 直接等待一个 turn 完成，不通过 console callback 猜测结束状态。
- [ ] `AgentRunner.continue_turn()` 返回稳定的内部结果对象，provider 层再映射为 `BLACKBOARD_RESULT`。
- [ ] 普通进度与最终结果分流，确保结果行不会被模型自然语言伪造或产生多次。
- [ ] stdin 关闭、SIGTERM、CTRL_BREAK 和父进程退出时中断 turn、取消 bridge 命令、关闭 MCP、释放 lease 后退出。
- [ ] Provider 模式禁止人工 approval 阻塞；需要 approval 的工具直接不可见或 fail closed。
- [ ] 保留现有 `python main.py` 交互体验和已有 thread 恢复行为。

### P1 验收

- [ ] 无交互输入测试可执行完整的一次 fake model turn。
- [ ] 正常、模型鉴权失败、模型超时、协议错误和用户取消都有稳定退出码及结果状态。
- [ ] provider 退出后没有 heartbeat、thread lease 或后台任务残留。

## P2：增加 Bat Provider 工具 Profile

这是宿主机 Agent 架构的安全前提。

- [ ] 在 `AppSettings`/tool loader 中增加显式 profile：`interactive_unrestricted`、`no_tools`、`nmap_optional`。
- [ ] `interactive_unrestricted` 可保留当前 PowerShell，供用户独立启动 main 时使用。
- [ ] bat provider 的 `no_tools` profile 不注册任何可执行宿主机命令的工具。
- [ ] bat provider 的 `nmap_optional` profile 只增加 Nmap proxy 及必要的受管只读工具，不注册 `powershell`。
- [ ] profile 由受控 run context 指定，不能由 prompt、LLM tool call 或 MCP 返回值切换。
- [ ] provider 启动时打印工具名称清单的脱敏摘要，方便审计，但不得打印 schema 中的 secret/default token。
- [ ] 提供机器可读的 profile preflight/health 命令，输出启用工具名和 profile version；bat 可在启动前确认 `nmap_optional` 不包含 `powershell`，输出不得包含配置值或 secret。

### P2 验收

- [ ] `nmap_optional` 下模型看不到 `powershell`。
- [ ] profile preflight 对未知 profile、工具注册漂移和 `nmap_optional` 意外包含宿主机命令工具时返回非零退出码。
- [ ] prompt injection 无法通过工具读取 `config.yaml`、环境变量、API key 或 Sandbox token 文件。
- [ ] 独立交互模式的既有 PowerShell 测试不被破坏。

## P3：迁移并按需启动 Nmap MCP

迁移来源：

```text
qinyuan-legacy/MCPs/Nmap_Mcp/server.py
```

建议目标：

```text
MCP/Nmap_Mcp/server.py
qsubagent/mcp/client.py
qsubagent/mcp/manager.py
qsubagent/mcp/models.py
Tools/nmap.py
tests/test_nmap_mcp.py
tests/test_mcp_manager.py
```

- [ ] 保留 legacy Nmap MCP 的职责：验证 hostname/IP/CIDR、端口数量和 timeout，只返回 `sandbox_command`。
- [ ] MCP Server 禁止 `subprocess`、PowerShell、Docker SDK、Docker socket 和宿主机 Nmap fallback。
- [ ] `authorized_targets` 从本 run 的受控 context 注入，不依赖所有 Agent 共享的全局可变配置。
- [ ] 为模型静态注册 `nmap_tcp_scan` proxy schema，使 Agent 在 MCP 尚未启动时也能选择该工具。
- [ ] 只有首次执行 `nmap_tcp_scan` proxy 时才启动本 run 私有的 MCP stdio 子进程。
- [ ] 静态 proxy schema 与 MCP Server schema 加入一致性测试，防止参数漂移。
- [ ] MCP 可执行文件和 `cwd` 只从 registry-relative 配置解析，不接受模型提供的路径。
- [ ] MCP 启动、初始化、call、shutdown 分层超时；启动失败不重试执行 Nmap。
- [ ] MCP stdio 必须是严格 JSON-RPC；stderr 有界记录，协议 stdout 不混入普通日志。
- [ ] main 退出时只关闭已经启动的 MCP；从未调用 Nmap 的 run 不创建任何 MCP 子进程。

### P3 验收

- [ ] 无 Nmap 回合完成后，进程表和日志中没有 Nmap MCP 子进程。
- [ ] 同一 run 多次 Nmap 调用只启动一个 MCP runtime。
- [ ] 不同 run 的 MCP runtime、authorized targets 和 thread 状态不串线。
- [ ] 未授权目标、命令注入式目标、非法端口范围和非法 timeout 不产生 bridge 请求。

## P4：实现 Sandbox Bridge Client 与 Nmap Proxy

建议新增：

```text
qsubagent/sandbox/contracts.py
qsubagent/sandbox/client.py
qsubagent/sandbox/errors.py
qsubagent/sandbox/auth.py
tests/test_sandbox_client.py
tests/test_nmap_proxy.py
```

- [ ] `NmapProxy` 调用 MCP 获得 `sandbox_command`，再次进行结构和通用命令安全校验，再调用 bridge。
- [ ] 只允许命令名 `nmap`；拒绝绝对/相对路径、shell 字符串、未知字段、NUL、非字符串参数和超长 argv。
- [ ] main 不解释 Docker、容器名、sandbox ID、network 或 image。
- [ ] bridge client 固定 loopback scheme/host，不允许 prompt 或 MCP 覆盖 endpoint。
- [ ] bearer token 只进入 Authorization header；异常、重试、trace 和 redaction 后的日志均不得输出 token。
- [ ] transport timeout 必须大于命令 timeout，并包含取消及响应读取余量。
- [ ] 将 bridge 响应严格映射为现有 `ToolResult` 状态，保留 exit code、截断标记、artifact 引用和不确定结果。
- [ ] Nmap proxy 标记为具有网络副作用；恢复时不得自动重放已开始但未确认的扫描。
- [ ] bridge 不可用时 fail closed，不回退到宿主机 Nmap、PowerShell 或本地 subprocess。

### P4 验收

- [ ] fake MCP -> real proxy -> fake bridge 完成一次合法扫描计划。
- [ ] MCP 返回 `curl`、命令路径、附加 shell 或未知字段时，bridge 尚未收到请求即被拒绝。
- [ ] bridge 401/403、断连、超时和结果协议错误均映射为稳定 `ToolResult`。
- [ ] 模型不能通过 tool arguments 选择另一个 Agent 的 run 或 Sandbox。

## P5：取消、恢复、Artifact 与结果语义

- [ ] 每个 Nmap 调用使用模型 `call_id` 派生的稳定 idempotency key。
- [ ] provider 取消时先请求 bridge 取消活动 command，再终止 MCP；bat 最终仍负责销毁工具容器。
- [ ] 明确区分：命令未开始为 `aborted`，确认非零退出为 `failed`，确认超时为 `timeout`，已开始但失联为 `outcome_unknown`。
- [ ] `ThreadRepository.recover_interrupted()` 不重放 `running/outcome_unknown` Nmap。
- [ ] 大输出只把受限 head/tail 放进模型上下文；完整内容只能通过受管 artifact 引用访问。
- [ ] artifact 路径必须位于本 run scope，拒绝 bridge 返回的任意宿主机路径。
- [ ] 最终 `BLACKBOARD_RESULT` 汇总 tool call、状态、持续时间、exit code、truncation、artifact 和 uncertain outcome。

### P5 验收

- [ ] 在 MCP 规划前、bridge 请求前、容器执行中和结果返回后四个阶段取消均有确定测试。
- [ ] provider 崩溃重启后不会再次扫描已经开始的目标。
- [ ] 超大 stdout/stderr 不导致 main 内存无界增长或 Blackboard 结果膨胀。

## P6：API Key 与进程环境隔离

- [ ] 模型 API key 只供 main 的模型 client 使用，不写入 run context、MCP 参数、bridge token 文件、日志或 artifact。
- [ ] 启动 Nmap MCP 时构造最小环境变量白名单，不继承 `OPENAI_API_KEY`、自定义模型 key、Docker 配置或宿主机凭据。
- [ ] provider profile 不提供读取任意环境变量/文件的工具。
- [ ] `redact_text`/`redact_data` 覆盖模型 key、bridge token、Authorization header 和带凭据 URL。
- [ ] crash report、配置错误和 health check 只显示 secret 名称，不显示值。
- [ ] README 明确：工具 Sandbox 不保护宿主机 main；安全性依赖 provider profile 禁止宿主机命令工具。
- [ ] 后续可接宿主机 LLM Gateway，用短期 run token 替代 main 直接持有长期模型 key；该 Gateway 不是首版 Nmap 迁移的阻塞项。

### P6 验收

- [ ] 恶意 prompt、恶意 MCP stderr、bridge 错误和异常堆栈中均找不到测试 secret。
- [ ] MCP 子进程环境快照不包含模型 API key 和 Sandbox bearer token。
- [ ] secret scanner 覆盖 stdout、stderr、event log、artifact 和 `BLACKBOARD_RESULT`。

## P7：测试、文档和发布顺序

- [ ] 将当前 `Tools/` 目录与源码 `import tools` 的大小写统一；在 Windows 上使用可被 Git 正确记录的两步重命名，避免 Linux import 失败。
- [ ] 更新 `README.md`：provider CLI、stdin/stdout、run context、tool profile、按需 Nmap 和 bat bridge 前提。
- [ ] 更新 `config.yaml` 示例：MCP 路径/超时、bridge timeout、provider profile；不提交真实 key。
- [ ] 增加 Windows 进程树取消测试和 Linux 大小写敏感 import 测试。
- [ ] 增加 deterministic fake model、fake MCP、fake bridge；单元测试不能要求 Docker。
- [ ] 增加可选的 bat bridge 集成测试，只有显式环境开关时才依赖 qinyuan-bat。
- [ ] 记录实际测得的无工具启动时间、首次 Nmap MCP 启动时间、bridge 往返时间和内存峰值。

实施顺序：

1. [ ] P0：协议与 fake contract test。
2. [ ] P1-P2：provider CLI 和安全工具 profile。
3. [ ] P3：Nmap MCP 与懒启动 manager。
4. [ ] P4：Sandbox Bridge Client 和 Nmap proxy。
5. [ ] P5-P6：取消、恢复、artifact 和 secret 防护。
6. [ ] P7：跨仓库集成证据与文档。

## 最终验收

- [ ] bat 能像调用 Codex/Claude 一样启动 main、写入 stdin、收集输出和退出码。
- [ ] main 不调用 Nmap 时不启动 MCP，也不请求创建工具 Sandbox。
- [ ] main 调用多次 Nmap 时复用同一 run 的 MCP runtime 和 bat 工具 Sandbox。
- [ ] main 无 Docker socket、Docker API、宿主机 Nmap fallback 或 bat 内部 import。
- [ ] bat provider 模式看不到宿主机 PowerShell 工具。
- [ ] 模型 API key 和 bridge token 不进入 MCP、工具容器、模型上下文、日志或 Blackboard。
- [ ] 所有协议失败、取消和清理失败都有可审计且不夸大的终态。

## 本轮实施证据

- [x] 已新增 `qsubagent.provider` 非交互入口、`qsubagent.sandbox` v1 契约与 loopback client。
- [x] 已新增 `MCP/Nmap_Mcp` 严格 JSON-RPC server、lazy manager、Nmap planner/proxy 与 profile preflight。
- [x] `tools/` 已完成大小写迁移；`nmap_optional` 不注册 PowerShell。
- [x] main deterministic tests 与跨仓 fake bridge smoke 已通过；真实 Docker、真实 Nmap 和真实模型仍需显式 opt-in 验收。
