## Httpx MCP 工具说明
### 概述
Httpx MCP 是基于 Model Context Protocol (MCP) 标准实现的 HTTP 探测工具插件，为 QinYuan Agent 提供目标存活探测和信息收集能力。

### 功能特性
工具函数 功能描述 run_httpx_probe 对单个目标URL/IP执行HTTP探测 run_httpx_batch 从文件批量读取目标并执行探测

### 使用方法
单目标探测：

批量探测：

### 新增技术


### 依赖要求
- Python 3.11+
- mcp[cli] >= 1.9.0
- httpx 命令行工具
安装 httpx：

```
go install -v github.com/
projectdiscovery/httpx/cmd/
httpx@latest
```