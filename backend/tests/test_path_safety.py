"""Path traversal protection tests (Brief §5, §22, §25)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import PathAccessDeniedError
from app.core.path_safety import safe_filename, validate_storage_key


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "exports").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    "evil",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "exports/../../secrets.txt",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "C:\\Windows\\system32\\config",
        "/qbit-data/../../home/user/.ssh/id_rsa",
        "....//....//etc/passwd",
        "a/\x00b",
    ],
)
def test_traversal_attempts_blocked(root: Path, evil: str):
    with pytest.raises(PathAccessDeniedError):
        validate_storage_key(evil, root)


def test_dotdot_in_middle_blocked(root: Path):
    with pytest.raises(PathAccessDeniedError):
        validate_storage_key("exports/../..//x", root)


def test_valid_key_resolves_inside_root(root: Path):
    resolved = validate_storage_key("exports/2026/09/report.csv", root)
    assert resolved.is_relative_to(root.resolve())


def test_symlink_escape_blocked(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = root / "exports" / "link.txt"
    link.symlink_to(outside)

    # The symlink resolves OUTSIDE the root -> must be rejected.
    with pytest.raises(PathAccessDeniedError):
        validate_storage_key("exports/link.txt", root)

    # Traversal towards the outside file is rejected as well.
    with pytest.raises(PathAccessDeniedError):
        validate_storage_key("exports/../../outside.txt", root)


def test_safe_filename_sanitizes():
    assert safe_filename("../../evil name?.txt") != "../../evil name?.txt"
    assert safe_filename("../../evil name?.txt").endswith(".txt")
    assert "/" not in safe_filename("a/b/c.txt")
    assert safe_filename("") == "file"
    assert safe_filename("report-2026 final.xlsx") == "report-2026_final.xlsx"


def test_unicode_and_long_names():
    cleaned = safe_filename("résumé café.pdf")
    assert cleaned.endswith(".pdf")
    assert len(safe_filename("x" * 500)) <= 136
