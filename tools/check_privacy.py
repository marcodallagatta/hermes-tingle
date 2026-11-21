#!/usr/bin/env python3
"""Privacy gate for Hermes Tingle: refuse to publish secrets or personal data.

Scans tracked files (or, with --staged/--diff, only the lines being committed)
for credential formats, non-placeholder email addresses, phone numbers and
internal markers. This is defense in depth: a clean result does not prove a
change is safe to publish, so still read the diff before every push.

Exit code 1 with file:line findings when anything matches.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

SECRET_PATTERNS = [
    ("GitHub token", re.compile(r"(?:github_pat|ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("generic API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w+])\+[1-9]\d{7,14}\b")
MARKERS = (".DO-NOT-PUBLISH", "FEATURE-TRACKER")


def _local_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """Operator-local forbidden literals (the user's name, handles, addresses).

    Lives in tools/privacy_patterns.local - GITIGNORED, one literal per line -
    so the forbidden personal strings themselves never enter the tracked tree
    or its history. Each line becomes a case-insensitive substring scan."""
    import pathlib
    local = pathlib.Path(__file__).with_name("privacy_patterns.local")
    patterns = []  # type: list[tuple[str, re.Pattern]]
    if local.is_file():
        for line in local.read_text(encoding="utf-8").splitlines():
            literal = line.strip()
            if literal and not literal.startswith("#"):
                patterns.append(("operator-declared private literal",
                                 re.compile(re.escape(literal), re.IGNORECASE)))
    return patterns

# Placeholder addresses that are safe in synthetic fixtures.
ALLOWED_EMAIL_SUFFIXES = (".invalid", ".test", ".localhost", "@localhost")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}


def _email_allowed(address: str) -> bool:
    local, _, domain = address.partition("@")
    domain = domain.casefold()
    if domain in ALLOWED_EMAIL_DOMAINS or any(domain.endswith(s) for s in ALLOWED_EMAIL_SUFFIXES):
        return True
    return any(address.endswith(s) for s in ALLOWED_EMAIL_SUFFIXES)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return [line for line in out if line.strip()]


def _added_lines() -> list[tuple[str, int, str]]:
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color", "--diff-filter=AM"],
        check=True, capture_output=True, text=True,
    ).stdout
    findings, path, new_line = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line = int(match.group(1)) - 1 if match else 0
        elif path and line.startswith("+") and not line.startswith("+++"):
            new_line += 1
            findings.append((path, new_line, line[1:]))
    return findings


def _scan(lines: list[tuple[str, int, str]]) -> list[str]:
    problems = []
    for path, lineno, text in lines:
        for label, pattern in SECRET_PATTERNS + _local_patterns():
            if pattern.search(text):
                problems.append(f"{path}:{lineno}: possible {label}")
        if path not in {".gitignore", "tools/check_privacy.py"}:  # these name the markers to keep them out
            for marker in MARKERS:
                if marker in text:
                    problems.append(f"{path}:{lineno}: internal marker {marker!r} must not be published")
        for match in EMAIL_RE.finditer(text):
            if not _email_allowed(match.group(0)):
                problems.append(f"{path}:{lineno}: non-placeholder email address")
        if PHONE_RE.search(text):
            problems.append(f"{path}:{lineno}: possible phone number")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan only lines staged for commit")
    args = parser.parse_args()

    if args.staged:
        lines = _added_lines()
    else:
        lines = []
        for path in _tracked_files():
            try:
                with open(path, encoding="utf-8") as handle:
                    for lineno, text in enumerate(handle, 1):
                        lines.append((path, lineno, text))
            except (OSError, UnicodeDecodeError):
                continue

    problems = _scan(lines)
    if problems:
        print("Privacy gate found potential secrets or personal data:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Privacy gate: no secrets or personal data patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
