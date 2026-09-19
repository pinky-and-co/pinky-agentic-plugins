#!/usr/bin/env python3
"""Hook to validate plugin manifests with `claude plugin validate --strict`.

This pre-commit hook enumerates every `plugins/*/.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` under the repo and runs the CLI's own validator on each,
so a schema regression (unknown field, wrong `dependencies` shape) fails the commit instead
of surfacing only at `claude plugin install` time.

`validate --strict` checks manifest *form*, never dependency *resolvability* — a
`dependencies: ["does-not-exist@nowhere"]` entry passes this gate.

Usage:
    python3 validate_plugins_strict.py   # no args -> scan the whole repo
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def find_plugin_manifests() -> list[Path]:
    """Return every `plugins/*/.claude-plugin/plugin.json` under the repo."""
    return sorted(REPO_ROOT.glob("plugins/*/.claude-plugin/plugin.json"))


def find_marketplace_manifests() -> list[Path]:
    """Return every `.claude-plugin/marketplace.json` under the repo."""
    return sorted(REPO_ROOT.glob("**/.claude-plugin/marketplace.json"))


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of running `claude plugin validate --strict` on one manifest."""

    passed: bool
    output: str


def validate(path: Path) -> ValidationResult:
    """Run strict validation on a plugin manifest."""
    result = subprocess.run(
        ["claude", "plugin", "validate", "--strict", str(path)],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    return ValidationResult(passed=result.returncode == 0, output=output)


def main() -> int:
    """Validate plugin + marketplace manifests, print results per path."""
    manifests = find_plugin_manifests() + find_marketplace_manifests()
    if not manifests:
        print("No plugin.json / marketplace.json found — nothing to validate.")
        return 0

    try:
        results = [(path, validate(path)) for path in manifests]
    except FileNotFoundError:
        print("`claude` CLI not found on PATH — cannot validate manifests.")
        return 1

    failed = [path for path, result in results if not result.passed]
    for path, result in results:
        mark = "OK" if result.passed else "FAIL"
        print(f"[{mark}] {path.relative_to(REPO_ROOT)}")
        if not result.passed:
            print(result.output)

    if failed:
        print(f"{len(failed)}/{len(manifests)} manifest(s) failed --strict validation.")
        return 1

    print(f"{len(manifests)} manifest(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
