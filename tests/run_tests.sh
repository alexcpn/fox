#!/bin/bash
# Fox unit test runner — fast (<2s), no LLM required.
# Exit 0 on success, 1 on failure.
# Used by .git/hooks/pre-commit and CI.

set -e
cd "$(dirname "$0")/.."
python3 tests/test_unit.py 2>&1
echo ""
echo "✓ All unit tests passed"
