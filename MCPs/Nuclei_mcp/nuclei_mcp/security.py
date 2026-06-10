from pathlib import Path

from nuclei_mcp.config import MAX_TIMEOUT_SECONDS,SEVERITY_ORDER

def validate_target(target: str) -> str:
    if not isinstance(target,str):
        raise ValueError("target must be a string")
    target = target.strip()

    if not target:
        raise ValueError("target cannot be empty")
    
    if any(char in target for char in "\r\n\t"):
        raise ValueError("target contains unsupported whitespace")
    
    return target

def validate_template(template: str) -> str:
    if not template:
        return ""

    if not isinstance(template,str):
        raise ValueError("template must be a string")
    
    template = template.strip()

    if not template:
        return ""
    
    path = Path(template)
    
    if not path.exists():
        raise ValueError(f"template does not exist: {template}")
    
    return str(path)

def validate_timeout(timeout_seconds: int) -> int:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be an integer")

    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds must be between 1 and 600")

    return timeout_seconds

def validate_severity(severity: str) -> str:
    if not severity:
        return ""
    
    if not isinstance(severity, str):
        raise ValueError("severity must be a string")
    
    severity = severity.strip().lower()

    if severity not in SEVERITY_ORDER:
        raise ValueError(f"invalid severity: {severity}")
    
    return severity

def validate_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags,list):
        raise ValueError("tags must be a list")

    cleaned_tags: list[str] = []

    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tag must be a string")

        tag = tag.strip().lower()

        if not tag:
            continue

        if any(char in tag for char in "\r\n\t, "):
            raise ValueError(f"invalid tag: {tag}")

        cleaned_tags.append(tag)

    if not cleaned_tags:
        raise ValueError("tags cannot be empty")

    return cleaned_tags 

def validate_int_range(
    value: int,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")

    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")

    return value