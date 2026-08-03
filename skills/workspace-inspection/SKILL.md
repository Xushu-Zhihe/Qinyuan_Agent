---
name: workspace-inspection
description: Inspect the PowerShell workspace with bounded, read-only commands.
---

# Workspace Inspection

Use this Skill when the user asks what files are available in the configured
PowerShell workspace.

1. Call `powershell` with `Get-ChildItem -Force`.
2. Prefer `Get-Content` for a known text file and `Select-String` for searches.
3. Do not modify files unless the user explicitly asks for a change.
4. If output is archived, call `read_artifact` for only the required range.
