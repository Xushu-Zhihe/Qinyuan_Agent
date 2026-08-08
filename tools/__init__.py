def load_builtin_tools() -> None:
    """Import built-in modules so their tools register once."""

    from . import powershell as _powershell
    from . import read_artifact as _read_artifact
    from . import read_skill as _read_skill

    _ = (_powershell, _read_artifact, _read_skill)


__all__ = ["load_builtin_tools"]
