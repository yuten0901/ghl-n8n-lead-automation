"""Scan the working tree for committed credentials.

Runs in CI and is worth running before any push. It is not a replacement for a
real scanner (gitleaks, trufflehog) and does not pretend to be: it targets the
specific shapes this project could plausibly leak, and it fails loudly on them.

    python scripts/scan_secrets.py

Two rules keep it honest rather than decorative:

* **It must not be satisfiable by deleting the placeholders.** The demo values
  are allow-listed by exact string, so replacing one with a real key trips it.
* **It checks .env is not tracked**, which is the leak that actually happens -
  far more often than a key pasted into source.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".mypy_cache",
    "htmlcov",
}
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".sqlite3", ".lock"}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("OpenAI project key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GHL private integration token", re.compile(r"\bpit-[0-9a-f]{8}-[0-9a-f-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    (
        "populated secret assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|access[_-]?token|password|passwd|signing[_-]?secret)"
            r"\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']"
        ),
    ),
]

# Exact strings that are deliberately in the tree. Listing them by value rather
# than by a loose pattern means a real key cannot hide behind the allow-list.
ALLOWED = {
    "whsec_test_value_not_a_real_secret",
    "test_token_not_a_real_secret",
    "demo_token_not_a_real_secret",
    "the_wrong_secret",
    "whsec_a_different_secret",
    "REPLACE_WITH_YOUR_CREDENTIAL_ID",
    "REPLACE_WITH_ERROR_WORKFLOW_ID",
    "CHANGE_ME",
    "loc_DEMO0000000000000000",
    "pipe_DEMO000000000000000",
    "cal_DEMO0000000000000000",
    "leadops:CHANGE_ME@localhost",
    "Cj0KCQjw_DEMO_gclid_value",
}


def is_allowed(match: re.Match[str]) -> bool:
    text = match.group(0)
    captured = match.groups()[-1] if match.groups() else ""
    return any(allowed in text or (captured and allowed in captured) for allowed in ALLOWED)


def files_to_scan() -> list[pathlib.Path]:
    """Prefer git's view of the tree - it is exactly what would be pushed."""
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\n")
        paths = [REPO / line for line in listing if line.strip()]
        if paths:
            return [p for p in paths if p.is_file() and p.suffix not in SKIP_SUFFIXES]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return [
        path
        for path in REPO.rglob("*")
        if path.is_file()
        and path.suffix not in SKIP_SUFFIXES
        and not SKIP_DIRS & set(path.relative_to(REPO).parts)
    ]


def check_env_not_tracked() -> list[str]:
    """The leak that actually happens: a real .env committed by accident."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", ".env", "*.env", "**/.env"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [f"{name} is tracked by git; it must be gitignored" for name in tracked]


def main() -> int:
    findings: list[str] = check_env_not_tracked()

    for path in files_to_scan():
        if path.name == pathlib.Path(__file__).name:
            continue  # this file necessarily contains the patterns
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            for label, pattern in PATTERNS:
                match = pattern.search(line)
                if match and not is_allowed(match):
                    relative = path.relative_to(REPO)
                    findings.append(f"{relative}:{line_number}: possible {label}")

    if findings:
        print("Potential secrets found:\n")
        for finding in findings:
            print(f"  {finding}")
        print(
            "\nIf one is a false positive, add the exact literal to ALLOWED in "
            "scripts/scan_secrets.py - never loosen the pattern."
        )
        return 1

    print(f"No secrets found ({len(files_to_scan())} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
