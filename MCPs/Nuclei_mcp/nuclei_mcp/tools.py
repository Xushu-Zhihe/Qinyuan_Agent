from typing import Any

from nuclei_mcp.security import (
    validate_target,
    validate_template as validate_template_path,
    validate_timeout,
    validate_severity,
    validate_tags,
    validate_int_range
)
from nuclei_mcp.runner import (
    check_installation, 
    run_scan_sync ,
    validate_template_sync,
    list_templates_sync,
    read_template_info_sync,
    run_scan_by_tags_sync,
    find_template_by_id_sync,
    run_scan_by_template_id_sync
)
from nuclei_mcp.config import DEFAULT_TIMEOUT_SECONDS


def register_tools(mcp: Any) -> None:
    @mcp.tool()
    def check_nuclei_installation() -> dict[str, Any]:
        return check_installation()
    
    @mcp.tool()
    def run_scan(
        target: str,
        template: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        
        target = validate_target(target)
        template = validate_template_path(template)
        timeout_seconds = validate_timeout(timeout_seconds)

        return run_scan_sync(
            target=target,
            template=template,
            timeout_seconds=timeout_seconds,
        )
    
    @mcp.tool()
    def validate_template(
        template: str,
        timeout_seconds:int = 60,
    ) -> dict[str, Any]:
        template = validate_template_path(template)
        timeout_seconds = validate_timeout(timeout_seconds)

        return validate_template_sync(
            template=template,
            timeout_seconds=timeout_seconds,
        )
    
    @mcp.tool()
    def list_templates(
        templates_dir: str = "",
        keyword: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = validate_int_range(limit, "limit", 1, 200)

        return list_templates_sync(
            templates_dir=templates_dir,
            keyword=keyword,
            limit=limit,
        )
    
    @mcp.tool()
    def read_template_info(template: str) -> dict[str, Any]:
        template = validate_template_path(template)
        return read_template_info_sync(template)

    @mcp.tool()
    def run_scan_by_tags(
        target: str,
        tags: list[str],
        severity: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        target = validate_target(target)
        timeout_seconds = validate_timeout(timeout_seconds)

        return run_scan_by_tags_sync(
            target=target,
            tags=validate_tags(tags),
            severity=validate_severity(severity),
            timeout_seconds=timeout_seconds,
        )
    
    @mcp.tool()
    def find_template_by_id(
        template_id: str,
        templates_dir: str = "",
    ) -> dict[str, Any]:
        return find_template_by_id_sync(
            template_id=template_id,
            templates_dir=templates_dir,
        )
    
    @mcp.tool()
    def run_scan_by_template_id(
        target: str,
        template_id: str,
        templates_dir: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        target = validate_target(target)
        timeout_seconds = validate_timeout(timeout_seconds)

        return run_scan_by_template_id_sync(
            target=target,
            template_id=template_id,
            templates_dir=templates_dir,
            timeout_seconds=timeout_seconds,
        )
    
