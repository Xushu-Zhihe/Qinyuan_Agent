# Changelog

## Unreleased

- 增加 BLACKBOARD_RESULT v2 vendored Skill/schema/fixtures/validator。
- 增加 `--required-skill`、`--result-protocol-version` 和 `--capabilities --json`。
- provider stdout 增加本地 final gate 和一次 protocol correction；validator 失败不会把未经校验的结果写入 stdout。
