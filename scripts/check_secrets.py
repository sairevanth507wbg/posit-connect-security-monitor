#!/usr/bin/env python
"""Pre-commit secret scanner.

Blocks a commit when staged content looks like a credential, a private key, or
internal infrastructure detail that should not reach a public repository.

Usage::

    python scripts/check_secrets.py             # scan staged changes (pre-commit)
    python scripts/check_secrets.py --all       # scan every tracked file
    python scripts/check_secrets.py FILE...     # scan specific files

Exit codes:
    0  clean
    1  findings - commit should be blocked

This is a safety net, not a guarantee. It cannot detect a secret that looks like
ordinary text. The primary defence is still: never put real values in a tracked
file, and keep them in ``.env`` (git-ignored) or a secret manager.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

#: (rule name, compiled pattern, guidance)
RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----"),
        "A private key must never be committed. Move it outside the repository.",
    ),
    (
        "databricks-pat",
        re.compile(r"\bdapi[0-9a-f]{32}\b"),
        "Databricks personal access token. Rotate it, then load from .env.",
    ),
    (
        "aws-access-key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "AWS access key ID. Rotate it immediately.",
    ),
    (
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
        "GitHub token. Revoke it, then use a credential helper.",
    ),
    (
        "slack-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "Slack token. Revoke it.",
    ),
    (
        "connect-api-key",
        # Posit Connect keys are 32 hex chars, usually after "Key " or an assignment.
        re.compile(
            r"(?i)(?:connect[_-]?api[_-]?key|authorization)\s*[=:]\s*[\"']?"
            r"(?:Key\s+)?([0-9a-zA-Z]{32,})"
        ),
        "Looks like a Posit Connect API key. Revoke it in Connect, then use .env.",
    ),
    (
        "assigned-secret",
        # PASSWORD=..., client_secret: "...", token = '...'
        re.compile(
            r"(?i)\b(password|passwd|secret|client_secret|api_key|apikey|token|"
            r"access_key|private_key)\b\s*[=:]\s*[\"']?([^\s\"'#,;)]{8,})"
        ),
        "Hardcoded credential. Read it from the environment instead.",
    ),
    (
        "postgres-url-with-password",
        re.compile(r"(?i)postgres(?:ql)?(?:\+\w+)?://[^\s:/]+:[^\s@]{3,}@"),
        "Database URL with an inline password. Build it from POSTGRES_* env vars.",
    ),
]

#: Values that are obviously not real, so they do not block a commit.
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"your[_\- ]|replace|example|changeme|placeholder|xxx+|<[^>]*>|\.\.\.|"
    # "test", "test-api-key", "test_password" - any value that announces itself
    # as a fixture. Anchored at the start so a real secret merely *containing*
    # the word is still caught.
    r"test[_\-]?|dummy|sample|fake|mock|stub|redacted|"
    r"secret|password|token|none|null|"
    r"\$\{[^}]*\}|%\([^)]*\)s|\{\{[^}]*\}\}"
    r")"
)

#: Code patterns that merely *reference* a secret rather than contain one.
SAFE_REFERENCE = re.compile(
    r"(?i)(os\.getenv|os\.environ|getenv\(|SecretStr|get_secret_value|"
    r"Field\(|validation_alias|AliasChoices|# noqa|settings\.|self\.|"
    r"description=|help=|assert |raise |import |from )"
)

#: Filenames that must never be committed, regardless of content.
FORBIDDEN_NAMES = re.compile(
    r"(?i)(^|/)(\.env(\.(local|dev|prod|production|staging))?|"
    r"credentials(\.json)?|servers\.json|id_rsa|id_ed25519|.*\.pem|.*\.pfx|"
    r".*\.p12|.*\.keystore)$"
)

#: Allowed despite matching FORBIDDEN_NAMES.
ALLOWED_NAMES = re.compile(r"(?i)(^|/)\.env\.(example|template|sample)$")

#: Never scanned - binary or vendored.
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif",
    ".ico", ".pdf", ".zip", ".gz", ".whl", ".woff", ".woff2", ".ttf", ".docx",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


class Finding:
    def __init__(self, path: str, line_no: int, rule: str, guidance: str) -> None:
        self.path = path
        self.line_no = line_no
        self.rule = rule
        self.guidance = guidance

    def render(self) -> str:
        location = self.path + (":" + str(self.line_no) if self.line_no else "")
        return "  [" + self.rule + "] " + location + "\n      " + self.guidance


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _is_placeholder(value: str) -> bool:
    value = value.strip().strip("\"'")
    if not value:
        return True
    return bool(PLACEHOLDER.match(value))


#: Inline escape hatch for documentation and deliberate examples. Applies to
#: every rule on that line, in any file type (``#``, ``//``, ``<!-- -->``).
IGNORE_MARKER = re.compile(r"check-secrets:\s*ignore")


def scan_line(path: str, line_no: int, line: str) -> List[Finding]:
    """Return findings for one line of text."""
    findings: List[Finding] = []
    if len(line) > 2000:  # minified asset or data blob
        return findings
    if IGNORE_MARKER.search(line):
        return findings

    for rule, pattern, guidance in RULES:
        match = pattern.search(line)
        if not match:
            continue
        # A line that merely reads a secret from the environment is fine.
        if rule in ("assigned-secret", "connect-api-key") and SAFE_REFERENCE.search(line):
            continue
        captured = match.group(match.lastindex) if match.lastindex else match.group(0)
        if rule in ("assigned-secret", "connect-api-key") and _is_placeholder(captured):
            continue
        findings.append(Finding(path, line_no, rule, guidance))
    return findings


def scan_text(path: str, text: str) -> List[Finding]:
    findings: List[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_line(path, line_no, line))
    return findings


def check_filename(path: str) -> List[Finding]:
    normalised = path.replace("\\", "/")
    if ALLOWED_NAMES.search(normalised):
        return []
    if FORBIDDEN_NAMES.search(normalised):
        return [
            Finding(
                path, 0, "forbidden-file",
                "This file type holds credentials and must not be committed. "
                "Add it to .gitignore and run: git rm --cached " + path,
            )
        ]
    return []


def should_skip(path: str) -> bool:
    parts = set(Path(path).parts)
    if parts & SKIP_DIRS:
        return True
    return Path(path).suffix.lower() in SKIP_SUFFIXES


def _git(args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout if result.returncode == 0 else ""


def staged_files() -> List[str]:
    output = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def staged_content(path: str) -> str:
    return _git(["show", ":" + path])


def tracked_files() -> List[str]:
    return [line.strip() for line in _git(["ls-files"]).splitlines() if line.strip()]


def scan(paths: Iterable[str], *, from_index: bool) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        findings.extend(check_filename(path))
        if should_skip(path):
            continue
        if from_index:
            text = staged_content(path)
        else:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        findings.extend(scan_text(path, text))
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for committed secrets.")
    parser.add_argument("paths", nargs="*", help="files to scan (default: staged)")
    parser.add_argument("--all", action="store_true", help="scan every tracked file")
    args = parser.parse_args(argv)

    if args.paths:
        findings = scan(args.paths, from_index=False)
        scanned = len(args.paths)
    elif args.all:
        files = tracked_files()
        findings = scan(files, from_index=False)
        scanned = len(files)
    else:
        files = staged_files()
        findings = scan(files, from_index=True)
        scanned = len(files)

    if not findings:
        print("check_secrets: " + str(scanned) + " file(s) scanned, no secrets found.")
        return 0

    print("", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    print("COMMIT BLOCKED - possible secrets detected", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print("", file=sys.stderr)
    print("If a finding is a deliberate example (docs, tests), add the marker", file=sys.stderr)
    print("'check-secrets: ignore' as a comment on that line.", file=sys.stderr)
    print("To bypass entirely (NOT recommended):", file=sys.stderr)
    print("    git commit --no-verify", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
