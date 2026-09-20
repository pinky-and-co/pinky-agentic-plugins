#!/usr/bin/env bash
# test_bulk_clean_gone.sh — tests that --quiet flag works correctly

set -euo pipefail

TMPDIR="${TMPDIR:-/tmp}"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bulk_clean_gone.sh"

# Setup: create remote and local repos
REMOTE="${TEST_ROOT}/remote.git"
LOCAL="${TEST_ROOT}/local"

git init --bare "$REMOTE" >/dev/null 2>&1
git clone "$REMOTE" "$LOCAL" >/dev/null 2>&1

cd "$LOCAL"
git config user.email "test@test.com"
git config user.name "Test"
echo "test" > file.txt
git add file.txt
git commit -m "test" >/dev/null 2>&1
git push -u origin main >/dev/null 2>&1

# Create a branch that will be [gone]
git checkout -b feature/gone >/dev/null 2>&1
echo "feature" > feature.txt
git add feature.txt
git commit -m "feature" >/dev/null 2>&1
git push -u origin feature/gone >/dev/null 2>&1

# Delete the remote branch
git -C "$REMOTE" update-ref -d refs/heads/feature/gone >/dev/null 2>&1

# Test 1: script runs without error in quiet mode
output=$("$SCRIPT_PATH" --root "$TEST_ROOT" --quiet 2>&1) || exit_code=$?
if [ "${exit_code:-0}" -eq 0 ]; then
    echo "PASS: --quiet flag does not cause error"
else
    echo "FAIL: --quiet flag caused exit code $exit_code"
    exit 1
fi

# Test 2: script runs without error in normal mode
output=$("$SCRIPT_PATH" --root "$TEST_ROOT" 2>&1) || exit_code=$?
if [ "${exit_code:-0}" -eq 0 ]; then
    echo "PASS: normal mode does not cause error"
else
    echo "FAIL: normal mode caused exit code $exit_code"
    exit 1
fi

echo "All tests passed"
