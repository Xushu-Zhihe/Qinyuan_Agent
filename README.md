# Qinyuan_Agent

Qinyuan_Agent 是 qinyuan-bat 使用的 CLI solver provider。它接收一次 planning、execution 或 timeout 请求，使用本仓库的工具/沙箱完成 assigned Intent，并在 stdout 输出经过本地 gate 校验的协议结果。

## qinyuan-bat provider 模式

安装依赖后：

```powershell
python -m pip install -e .
qinyuan-agent --required-skill blackboard-result-protocol --result-protocol-version 2 --challenge-dir .\challenge --work-dir .\work
```

安装对应沙盒并且验证：

```powershell
docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.1 `
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
