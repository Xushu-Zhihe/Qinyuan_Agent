import shutil
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp =FastMCP("amass-mcp-server",json_response=True)

@mcp.tool(
    title="Check Amass",
    description=(
        "Check whether amass is installed and available in PATH."
        "Returns the executable path and version output."
    ),
)

def check_amass()->dict[str,Any]:
    amass_path=shutil.which("amass")

    if not amass_path:
        return{
            "ok":False,
            "installed":False,
            "message":"amass is not installed or not present in PATH",
        }
    
    completed=subprocess.run(
        [amass_path,"-version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    return{
        "ok":completed.returncode==0,
        "installed":True,
        "path":amass_path,
        "returncode":completed.returncode,
        "stdout":completed.stdout,
        "stderr":completed.stderr,
        "command":[amass_path,"-version"],
    }

@mcp.tool(
    title="Amass Passive Domain Scan",
    description=(
        "Run amass passive enumeration against a single domain. "
        "Input domain should be a root domain such as example.com. "
        "The tool runs: amass enum -passive -d domain. "
        "Passive mode mainly uses public data sources and does not actively probe the target."
    ),
)

def run_amass_passive_scan(
    domain:str,
    timeout_seconds:int=180,
)->dict[str,Any]:
    amass_path=shutil.which("amass")

    if not amass_path:
        raise RuntimeError("amass is not installed or present in PATH")
    
    command=[
        amass_path,
        "enum",
        "-passive",
        "-d",
        domain,
    ]

    try:
        completed=subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return{
            "ok": False,
            "domain": domain,
            "timeout": True,
            "timeout_seconds": timeout_seconds,
            "command": command,
        }
    
    subdomains=[]
    lines=completed.stdout.splitlines()

    for i in range(len(lines)):
        line=lines[i].strip()
        if line!="":
            subdomains.append(line)

    return{
        "ok": completed.returncode == 0,
        "mode": "passive",
        "domain": domain,
        "count": len(subdomains),
        "subdomains": subdomains,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }

mcp.tool(
    title="Amass Domain Scan",
    description=(
        "Run amass enumeration against a single domain. "
        "Input domain should be a root domain such as example.com. "
        "The tool runs: amass enum -d domain. "
        "This may perform a broader enumeration than passive mode."
    ),
)

def run_amass_scan(
        domain:str,
        timeout_seconds:int=300,
)->dict[str,Any]:
    amass_path = shutil.which("amass")

    if not amass_path:
        raise RuntimeError("amass is not installed or present in PATH")
    
    command = [
        amass_path,
        "enum",
        "-d",
        domain,
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "domain": domain,
            "timeout": True,
            "timeout_seconds": timeout_seconds,
            "command": command,
        }

    subdomains = []
    lines = completed.stdout.splitlines()

    for i in range(len(lines)):
        line = lines[i].strip()
        if line != "":
            subdomains.append(line)

    return {
        "ok": completed.returncode == 0,
        "mode": "normal",
        "domain": domain,
        "count": len(subdomains),
        "subdomains": subdomains,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }

@mcp.tool(
    title="Amass Batch Domain Scan",
    description=(
        "Run amass passive enumeration against multiple domains. "
        "Input domains should be a list of root domains such as ['example.com', 'test.com']. "
        "For safety, this tool scans each domain in passive mode by default."
    ),
)

def run_amass_batch_scan(
    domains: list[str],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    amass_path = shutil.which("amass")

    if not amass_path:
        raise RuntimeError("amass is not installed or present in PATH")

    clean_domains = []

    for i in range(len(domains)):
        domain = domains[i].strip()
        if domain != "":
            clean_domains.append(domain)

    if len(clean_domains) == 0:
        return {
            "ok": False,
            "message": "domains list is empty",
            "domains": domains,
        }

    all_results = []
    all_subdomains = []

    for i in range(len(clean_domains)):
        domain = clean_domains[i]

        command = [
            amass_path,
            "enum",
            "-passive",
            "-d",
            domain,
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            all_results.append({
                "ok": False,
                "domain": domain,
                "timeout": True,
                "timeout_seconds": timeout_seconds,
                "command": command,
            })
            continue

        subdomains = []
        lines = completed.stdout.splitlines()

        for j in range(len(lines)):
            line = lines[j].strip()
            if line != "":
                subdomains.append(line)
                all_subdomains.append(line)

        all_results.append({
            "ok": completed.returncode == 0,
            "domain": domain,
            "count": len(subdomains),
            "subdomains": subdomains,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "command": command,
        })

    return {
        "ok": True,
        "mode": "batch_passive",
        "domains": clean_domains,
        "domain_count": len(clean_domains),
        "total_count": len(all_subdomains),
        "subdomains": all_subdomains,
        "results": all_results,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()


