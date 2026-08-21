# Qinyuan_Agent

Qinyuan_Agent 是 qinyuan-bat 使用的 CLI solver provider。它接收一次 planning、execution 或 timeout 请求，使用本仓库的工具/沙箱完成 assigned Intent，并在 stdout 输出经过本地 gate 校验的协议结果。

## qinyuan-bat provider 模式

安装依赖后：

```powershell
python -m pip install -e .
python -I -c "from tools import create_agent_tool_registry; from MCPs.Mcp_client import load_registry; print('qinyuan-agent imports: ok')"
qinyuan-agent --required-skill blackboard-result-protocol --result-protocol-version 2 --challenge-dir .\challenge --work-dir .\work
```

When `--challenge-dir` is present, the first sandbox execution must use
`shell_run_bash` by itself to list `/challenge` and run `file` on every supplied
file before any other analysis or network action.

Android inputs can be decompiled with the registered `jadx.decompile_android`
MCP tool. It runs the pinned JADX CLI inside the sandbox and writes output only
to a `/work` subdirectory.

For project-owned sandboxes, qinyuan-bat also supplies
`--sandbox-owner-id <stable-owner>` and `--sandbox-work-dir <stable-workspace>`.
Those short-lived provider processes detach without deleting the sandbox. The
project owner releases it explicitly with
`qinyuan-agent --release-sandbox --sandbox-owner-id <stable-owner>` after the
solver project reaches its terminal state. Calls without these lifecycle
arguments retain the original per-process cleanup behavior.

Sandbox commands have two timeout layers. The command plan's
`timeout_seconds` is enforced inside the container, while the host stops
waiting after that limit plus a 10-second grace period. A host timeout or
provider cancellation marks the sandbox unusable and stops it first. The
normal lifecycle removes it only after Docker has closed the command output
stream, avoiding a concurrent `remove(force=True)` against a live runc task.
`QINYUAN_SANDBOX_EXECUTION_GRACE_SECONDS` changes the host grace period and
`QINYUAN_SANDBOX_INTERRUPT_TIMEOUT_SECONDS` limits each cleanup wait; both
must be positive numbers.

Docker control operations use a separate five-second read timeout; only the
command output stream keeps the longer 315-second timeout. Configure these
with `QINYUAN_DOCKER_CONTROL_TIMEOUT_SECONDS` and
`QINYUAN_DOCKER_API_TIMEOUT_SECONDS`. `qinyuan-agent --health` checks Docker,
the sandbox image, and MCP resources without creating a disposable container.

`-I` 会从当前工作目录之外验证已安装包，避免源码目录碰巧可见而掩盖缺失的运行包或资源。`qinyuan-agent --health` 也会加载 Execution 所需模块，并检查启用的 MCP server 目录。

安装对应沙盒并且验证：

```powershell
docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.3 `
  .
qinyuan-agent --health
```

```powershell
docker context use desktop-linux
docker version
docker info
qinyuan-agent --health
```

`--required-skill` 会确定性加载 `Skills/blackboard-result-protocol/SKILL.md`，`--result-protocol-version 2` 固定结果协议版本。最终 `BLACKBOARD_RESULT` 在写入 stdout 前由 vendored validator 校验；首次失败会对同一 Intent 追加一次 protocol correction，第二次失败才返回非零并把诊断留在 stderr。

能力和依赖健康检查：

```powershell
qinyuan-agent --capabilities --json
qinyuan-agent --health
```

`--capabilities --json` 报告 required Skill、result protocol/schema、fixture digest 和本地验证能力。`Skills/blackboard-result-protocol/` 是从 qinyuan-bat canonical 资产同步的独立 vendor，不依赖 sibling checkout。

当前 provider 模式只提交文本协议结果；artifact/文件上传、controller 角色和自动 session 恢复由 qinyuan-bat 负责或明确不属于首版。
