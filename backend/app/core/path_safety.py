"""Path safety for all storage operations (Brief §5, §22, §25).

Rules:
- Every storage key is a *relative* POSIX-style path under a known root.
- Absolute paths, `..` segments, backslashes and symlink escapes are rejected.
- Users never supply raw filesystem paths — only file ids (Brief §22).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from app.core.errors import PathAccessDeniedError

_FORBIDDEN_SEGMENTS = {"..", "."}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def validate_storage_key(key: str, root: Path) -> Path:
    """Resolve `key` inside `root` and guarantee containment.

    Returns the resolved absolute Path on success.
    Raises PathAccessDeniedError on any traversal / escape attempt.
    """
    if not key or not isinstance(key, str):
        raise PathAccessDeniedError("Empty storage key")

    if "\x00" in key:
        raise PathAccessDeniedError("Invalid storage key")

    if key.startswith("/") or key.startswith("\\") or _WINDOWS_DRIVE.match(key):
        raise PathAccessDeniedError("Absolute paths are not allowed")

    if "\\" in key:
        raise PathAccessDeniedError("Path separators must be '/'")

    pure = PurePosixPath(key)
    for segment in pure.parts:
        if segment in _FORBIDDEN_SEGMENTS or ".." in segment:
            # Strict rule: no segment may contain ".." at all ("...." etc. are
            # rejected too — defense in depth against naive normalizers).
            raise PathAccessDeniedError("Path traversal is not allowed")

    root_resolved = root.expanduser().resolve()
    candidate = (root_resolved / pure).resolve()

    # Containment check (also defeats symlink escape).
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathAccessDeniedError("Path traversal is not allowed")

    return candidate


def safe_filename(name: str, *, max_length: int = 120) -> str:
    """Sanitize a client-supplied filename for use inside a storage key."""
    name = (name or "file").strip()
    # Keep the extension, sanitize the stem.
    stem = Path(name).stem
    suffix = Path(name).suffix[:16]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    return f"{cleaned[:max_length]}{suffix}"
