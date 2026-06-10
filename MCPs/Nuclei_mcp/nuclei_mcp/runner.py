import shutil
import subprocess
from typing import Any
from pathlib import Path
import yaml

from nuclei_mcp.parser import load_jsonl_text, summarize_findings # type: ignore # 
from nuclei_mcp.config import NUCLEI_COMMAND,DEFAULT_TEMPLATES_DIR


def _tail_text(text: str, limit: int = 20) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]

#检查nulcei是否安装
def check_installation() -> dict[str,Any]:
    nuclei_path = shutil.which(NUCLEI_COMMAND)#定义变量nuclei_path指向nuclei的路径

    #如果nuclei_path是空的，返回下面的json
    if not nuclei_path:
        return {
            "ok": False,
            "nuclei_found": False,
            "message": "nuclei is not installed or not in PATH",
        }
    
    #如果存在，执行下面的指令： 路径 -version，查看版本
    completed = subprocess.run(
        [nuclei_path,"-version"],
        capture_output=True,#捕获输出
        text=True,
        timeout=10,#规定超时时间
        check=False,#出错也不抛出异常
    )

    return {
        "ok": completed.returncode == 0,
        "nuclei_found": True,
        "nuclei_path": nuclei_path,
        "version": completed.stderr or completed.stdout,
        "returncode": completed.returncode, 
    }

#基本的扫描
def run_scan_sync(
    target: str,#传入的目标网站
    template: str = "",#指定使用哪个 nuclei 模板
    timeout_seconds: int = 120, 
) -> dict[str, Any]:
    nuclei_path = shutil.which(NUCLEI_COMMAND)

    #如果没找到
    if not nuclei_path:
        return {
            "ok": False,
            "error": "nuclei is not installed or not in PATH",
        }
    
    #构造执行命令
    command = [
        nuclei_path,
        "-u",
        target,
        "-jsonl",#输出成jsonl
        "-silent",
        "-nc",#禁止彩色输出
    ]

    #如果有模板，采用模板
    if template:
        command.extend(["-t", template])

    #执行扫描
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    #超时异常
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "timeout": True,
            "target": target,
            "command": command,
        }

    findings = load_jsonl_text(completed.stdout)#解析结果组成字典
    summary = summarize_findings(findings,sample_limit=50)#统计扫描结果

    return {
        "ok": completed.returncode == 0,
        "target": target,
        "returncode": completed.returncode,
        "summary": summary,
        "stdout_line_count": len(completed.stdout.splitlines()),
        "stderr_tail": _tail_text(completed.stderr),
        "command": command,
    }

#调用-validate,检查模板是否合法
def validate_template_sync(
        template: str,
        timeout_seconds: int = 60,
) -> dict[str, Any]:
    nuclei_path = shutil.which(NUCLEI_COMMAND)

    #检查是否存在Nuclei
    if not nuclei_path:
        return {
            "ok": False,
            "error": "nuclei is not installed or not in PATH",
        }
    
    command = [
        nuclei_path,
        "-validate",
        "-t",
        template,
        "-silent",
        "-nc",
    ]

    try:
        #尝试运行-validate
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        #处理超时
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "timeout": True,
            "template": template,
            "command": command,
        }
    
    #返回检查结果
    return {
        "ok": completed.returncode == 0,
        "template": template,
        "returncode": completed.returncode,
        "stdout_line_count": len(completed.stdout.splitlines()),
        "stderr_tail": _tail_text(completed.stderr),
        "command": command,
    }

#本地模板目录搜索.yaml模板文件
def list_templates_sync(
    templates_dir: str = "",
    keyword: str = "",
    limit: int = 50,#最多返回50个
) -> dict[str, Any]:
    templates_dir = templates_dir or DEFAULT_TEMPLATES_DIR

    if not templates_dir:
        return {
            "ok": False,
            "error": "templates_dir is required or NUCLEI_TEMPLATES_DIR must be set",
        }

    root = Path(templates_dir)

    if not root.exists() or not root.is_dir():
        return {
            "ok": False,
            "error": f"templates_dir does not exist: {templates_dir}",
        }
    
    keyword = keyword.strip().lower()
    results = []

    for path in root.rglob("*.yaml"):
        if keyword and keyword not in path.name.lower() and keyword not in str(path).lower():
            continue

        results.append(str(path))

        if len(results) >= limit:
            break

    return {
        "ok": True,
        "template_dir": str(root),
        "keyword": keyword,
        "count": len(results),
        "templates": results,
    }

#阅读模板
def read_template_info_sync(template: str) -> dict[str, Any]:
    path = Path(template)

    if not path.exists():
        return {
            "ok": False,
            "error": f"template does not exist: {template}",
        }
    
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            data = yaml.safe_load(file) or {}
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "template": template,
        }
    
    info = data.get("info", {})
    if not isinstance(info,dict):
        info = {}

    return {
        "ok": True,
        "template": str(path),
        "id": data.get("id"),
        "name": info.get("name"),
        "severity": info.get("severity"),
        "tags": info.get("tags"),
    }

def run_scan_by_tags_sync(
    target: str,
    tags: list[str],
    severity: str = "",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    nuclei_path = shutil.which(NUCLEI_COMMAND)

    if not nuclei_path:
        return {
            "ok": False,
            "error": "nuclei is not installed or not in PATH",
        }
    
    command = [
        nuclei_path,
        "-u",
        target,
        "-jsonl",
        "-silent",
        "-nc",
    ]

    for tag in tags:
        command.extend(["-tags", tag])

    if severity:
        command.extend(["-s", severity])

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
            "timeout": True,
            "target": target,
            "tags": tags,
            "severity": severity,
            "command": command,
        }
    
    findings = load_jsonl_text(completed.stdout)
    summary = summarize_findings(findings, sample_limit=50)

    return {
        "ok": completed.returncode == 0,
        "target": target,
        "tags": tags,
        "severity": severity,
        "returncode": completed.returncode,
        "summary": summary,
        "stdout_line_count": len(completed.stdout.splitlines()),
        "stderr_tail": _tail_text(completed.stderr),
        "command": command,
    }

def find_template_by_id_sync(
    template_id: str,
    templates_dir: str = "",
) -> dict[str, Any]:
    templates_dir = templates_dir or DEFAULT_TEMPLATES_DIR

    if not templates_dir:
        return {
            "ok": False,
            "error": "templates_dir is required or NUCLEI_TEMPLATES_DIR must be set",
        }

    root = Path(templates_dir)

    if not root.exists() or not root.is_dir():
        return {
            "ok": False,
            "error": f"templates_dir does not exist: {templates_dir}",
        }
    
    template_id = template_id.strip().lower()

    if not template_id:
        return {
            "ok": False,
            "error": "template_id cannot be empty",
        }
    
    expected_names = {
        f"{template_id}.yaml",
        f"{template_id}.yml",
    }

    for path in root.rglob("*.yaml"):
        if not path.is_file():
            continue

        if path.name.lower() not in expected_names:
            continue

        info_result = read_template_info_sync(str(path))

        if not info_result.get("ok"):
            return {
                "ok": True,
                "template_id": template_id,
                "template": str(path),
            }

        return {
            "ok": True,
            "template_id": template_id,
            "template": str(path),
            "name": info_result.get("name"),
            "severity": info_result.get("severity"),
            "tags": info_result.get("tags"),
        }
    
    return {
        "ok": False,
        "template_id": template_id,
        "error": f"template not found: {template_id}",
    }

def run_scan_by_template_id_sync(
    target: str,
    template_id: str,
    templates_dir: str = "",
    timeout_seconds: int = 120,
) -> dict[str,Any]:
    template_result = find_template_by_id_sync(
        template_id=template_id,
        templates_dir=templates_dir,
    )

    if not template_result.get("ok"):
        return {
            "ok": False,
            "target": target,
            "template_id": template_id,
            "error": template_result.get("error", "template not found"),
        }
    
    template = template_result.get("template")
    if not template:
        return {
            "ok": False,
            "target": target,
            "template_id": template_id,
            "error": "template path missing from find_template_by_id result",
        }

    scan_result = run_scan_sync(
        target=target,
        template=template,
        timeout_seconds=timeout_seconds,
    )

    scan_result["template_id"] = template_id
    scan_result["template"] = template
    return scan_result