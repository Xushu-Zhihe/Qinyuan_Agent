#!/usr/bin/env python
"""测试脚本 - 验证 MCP 服务器能正常工作"""
import subprocess
import sys
import time

print("=== 测试 Dirsearch MCP 服务器 ===")
print("当前时间:", time.strftime("%Y-%m-%d %H:%M:%S"))
print()

# 测试命令
test_input = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

print("发送请求:", test_input[:50], "...")
print()

# 使用管道方式测试
process = subprocess.Popen(
    [sys.executable, "C:\\Users\\15131\\Desktop\\Dirsearch_Mcp\\src\\server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# 发送请求
stdout, stderr = process.communicate(input=test_input + "\n", timeout=10)

print("=== 服务器响应 ===")
print(stdout)

if stderr:
    print("=== 错误输出 ===")
    print(stderr)

print("=== 测试结果 ===")
if process.returncode == 0 and "jsonrpc" in stdout:
    print("[OK] 成功！MCP 服务器运行正常！")
else:
    print("[FAIL] 失败")
    
print("进程退出码:", process.returncode)
print()
input("按 Enter 键退出...")