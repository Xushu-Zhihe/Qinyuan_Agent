from operator import truediv
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

# 创建一个MCP Server实例，这个名字作为 MCP Server的显示实例，不等于工具名。
# json_response=True 表示工具返回 dict时， FastMcp会更适合以 JSON结构返回 client
mcp = FastMCP("Burp_Suite-mcp-server",json_response = True)

def _get_allowed_hosts() -> list[str]:
    """
    读取允许访问的目标 host 列表。

    这个函数用于做安全边界限制，避免 Agent 随便请求任意目标。
    环境变量 MCP_BURP_ALLOWED_HOSTS 示例：
        127.0.0.1,localhost,example.com,*.example.org

    如果没有设置环境变量，默认只允许：
        127.0.0.1
        localhost
    """
    # 把 “127.0.0.1，localhost”拆成 ["127.0.0.1","localhost"], strip()去掉空格
    raw = os.getenv("MCP_BURP_ALLOWED_HOSTS","127.0.0.1,localhost")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]

def _is_host_allowed(host:str) -> bool:
    """
    判断某个 host是否在允许列表中，支持两种匹配方式：
    1.精确匹配：
        allowed = "example.com"
        host = "example.com"
    2.统配子域名：
        allowed = "*.example.com"
        host = "test.example.com"
    """
    host = host.lower()
    allowed_hosts = _get_allowed_hosts()

    for allowed in allowed_hosts:
        if allowed == host:
            return True
        
        if allowed.startswith("*.") and host.endswith(allowed[1:]):
            return True

    return False 

def _validate_target_url(target_url: str) -> urllib.parse.ParseResult:
    """
    校验目标 URL是否合法，并确认它在授权范围内。
    返回值是 urllib.parse.ParseResult, 里面包含:
        scheme:http/https
        hostname:主机名
        path:路径
        query:查询参数
        等等
    """
    parsed = urllib.parse.urlparse(target_url)
    # 只允许 Http/Https,
    # 不允许file://、ftp://、gopher:// 等协议，避免 SSRF和本地文件读取风险
    if parsed.scheme not in {"http","https"}:
        raise ValueError("target_url must start with http:// or https://")

    # URL必须包含 hostname
    # 例如 "http://abc"就是不合法的
    if not parsed.hostname:
        raise ValueError("target_url must contain a hostname")
    
    # 检查目标 host是否在 MCP_BURP_ALLOWED_HOSTS允许范围内
    if not _is_host_allowed(parsed.hostname):
        raise ValueError(
            f"target host {parsed.hostname!r} is not allowed by MCP_BURP_ALLOWED_HOSTS"
        )

    return parsed

@mcp.tool(
    title = "Send HTTP Request Through Burp Proxy",
    description = (
        "Send one authorized HTTP request through Burp Suite Proxy. "
        "Use this tool during web reconnaissance or manual verification when the agent "
        "needs Burp Suite to capture the request in HTTP history. "
        "Burp Suite must already be running with a proxy listener, usually at 127.0.0.1:8080. "
        "The target host must be allowed by MCP_BURP_ALLOWED_HOSTS. "
        "Returns HTTP status, response headers, response body preview, proxy URL, and error details."
    ),
)
def send_http_request_through_burp(
    target_url:str,
    method:str = "GET",
    headers:dict[str,str] | None = None,
    body:str | None = None,
    proxy_url:str = "http://127.0.0.1:8080",
    timeout_seconds:int = 30,
    verify_tls:bool = False,
    max_response_bytes:int = 200_000,
) -> dict[str,Any]:

    _validate_target_url(target_url) # 先校验目标 URL,确保协议和 host都符合限制
    method = method.upper().strip() # 规范化 HTTP方法
    # 只允许常见的 http 方法，避免 Agent构建奇怪方法导致不可控
    if method not in {"GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"}:
        raise ValueError("method must be one of GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS")

    # 复制 headers，避免修改调用者传入的 dict
    request_headers = dict(headers or {})

    # 如果用户没有提供 User-agent，则加一个默认值，方便在 Burp history中识别来源
    request_headers.setdefault("User-Agent","Qinyuan-Agent-Burp-Mcp")

    # urllib需要 bytes类型的请求体，如果 body是None，就表示没有请求体
    data = body.encode("utf-8") if body is not None else None

    # 构造 Http请求对象
    request = urllib.request.Request(
        url=target_url,
        data=data,
        headers=request_headers,
        method=method,
    )

    # 配置代理， http和 https都走 Burp Proxy
    proxy_handler = urllib.request.ProxyHandler({
        "http":proxy_url,
        "https":proxy_url,
    })
    # urllib opener的处理器列表
    handlers: list[Any] = [proxy_handler]

    # 若不校验 TLS,就创建一个不校验证书的 Https handler
    if not verify_tls:
        tls_context = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=tls_context))
    # build_opener会生成一个带代理配置的 opener，后面的请求都会通过 opener发出
    opener = urllib.request.build_opener(*handlers)

    try:
        # 发送请求，如果 Burp没启动，或代理不可达，则进入 URLError
        with opener.open(request,timeout=timeout_seconds) as response:
            raw_body = response.read(max_response_bytes +1)
            truncated = len(raw_body) > max_response_bytes
            # 如果超过最大长度，就截断
            raw_body = raw_body[:max_response_bytes]

            return{
                "ok":200 <=response.status<400,
                "target_url": target_url,
                "method": method,
                "proxy_url": proxy_url,
                "status_code": response.status,
                "reason": response.reason,
                "headers": dict(response.headers.items()),
                "body": raw_body.decode("utf-8", errors="replace"),
                "body_truncated": truncated,
            }
    except urllib.error.HTTPError as exc:
        # HTTPError 表示服务器返回了 4xx/5xx。
        # 注意：这仍然说明请求发出去了，只是 HTTP 状态码不成功。
        raw_body = exc.read(max_response_bytes + 1)
        truncated = len(raw_body) > max_response_bytes
        raw_body = raw_body[:max_response_bytes]

        return {
            "ok": False,
            "target_url": target_url,
            "method": method,
            "proxy_url": proxy_url,
            "status_code": exc.code,
            "reason": exc.reason,
            "headers": dict(exc.headers.items()),
            "body": raw_body.decode("utf-8", errors="replace"),
            "body_truncated": truncated,
        } 
    except urllib.error.URLError as exc:
        # URLError 通常表示网络层失败：
        # 1.Burp Proxy未启动 2.proxy_url写错 3.DNS失败 4.目标不可达
        return{
            "ok":False,
            "target_url":target_url,
            "method":method,
            "proxy_url":proxy_url,
            "error":str(exc.reason),
        }   
    except TimeoutError:
        # 请求超过 timeout_seconds，返回 timeout 标记。
        return {
            "ok": False,
            "target_url": target_url,
            "method": method,
            "proxy_url": proxy_url,
            "timeout": True,
        } 
    
def main() -> None:
    mcp.run()

if __name__=="__main__":
    main()