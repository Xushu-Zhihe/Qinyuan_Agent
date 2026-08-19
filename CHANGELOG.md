# Changelog

## Unreleased

- 修复安装后的 Execution 无法导入 `tools`，并将 MCP registry 纳入发行包。
- 增加本地导入、setuptools 包发现和运行资源声明的回归检查；health 现在会预加载 Execution 依赖并检查 MCP server 目录。
- 增加 BLACKBOARD_RESULT v2 vendored Skill/schema/fixtures/validator。
- 增加 `--required-skill`、`--result-protocol-version` 和 `--capabilities --json`。
- provider stdout 增加本地 final gate 和一次 protocol correction；validator 失败不会把未经校验的结果写入 stdout。
