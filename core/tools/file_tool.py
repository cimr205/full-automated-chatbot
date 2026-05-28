"""
File tool — read, write, list files inside the agent's sandbox directory.
Sandbox: /tmp/agent-files  (or FILE_SANDBOX env var)
"""
import os
from pathlib import Path


def _sandbox() -> Path:
    p = Path(os.getenv("FILE_SANDBOX", "/tmp/agent-files"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_path(rel: str) -> Path | None:
    """Resolve path, reject traversal outside sandbox."""
    try:
        resolved = (_sandbox() / rel).resolve()
        if _sandbox().resolve() in resolved.parents or resolved == _sandbox().resolve():
            return resolved
    except Exception:
        pass
    return None


def file_read(path: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"Adgang nægtet: {path}"
    if not p.exists():
        return f"Fil ikke fundet: {path}"
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return content[:6000] + ("…(truncated)" if len(content) > 6000 else "")
    except Exception as e:
        return f"Kunne ikke læse fil: {e}"


def file_write(path: str, content: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"Adgang nægtet: {path}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Fil gemt: {path} ({len(content)} tegn)"
    except Exception as e:
        return f"Kunne ikke skrive fil: {e}"


def file_list(path: str = ".") -> str:
    p = _safe_path(path) or _sandbox()
    if not p.is_dir():
        return f"Mappe ikke fundet: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for e in entries[:50]:
            icon = "📄" if e.is_file() else "📁"
            size = f" ({e.stat().st_size:,} bytes)" if e.is_file() else ""
            lines.append(f"{icon} {e.name}{size}")
        return f"**{p}**\n" + "\n".join(lines) if lines else "Tom mappe."
    except Exception as e:
        return f"Fejl ved listing: {e}"


def file_delete(path: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"Adgang nægtet: {path}"
    try:
        if p.is_file():
            p.unlink()
            return f"Slettet: {path}"
        return f"Ikke en fil: {path}"
    except Exception as e:
        return f"Kunne ikke slette: {e}"
