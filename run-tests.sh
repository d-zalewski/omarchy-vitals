#!/usr/bin/env bash
# Run the test suite.
#
#   ./run-tests.sh                    all tests, with coverage if available
#   ./run-tests.sh gpu                only tests whose name matches "gpu"
#   ./run-tests.sh -v                 verbose
#   ./run-tests.sh --no-coverage      skip the coverage gate
#   ./run-tests.sh --setup            create .venv with coverage, then run
#
# Tests need no dependencies. Coverage is optional and only used for the gate.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'

# ARGS is expanded as "${ARGS[@]+...}" below: bash 3.2 (still the /bin/bash on
# macOS) treats "${ARGS[@]}" on an empty array as an unset variable under
# `set -u` and aborts before a single test runs.
PATTERN=""; USE_COVERAGE=1; ARGS=()
for a in "$@"; do
    case "$a" in
        --no-coverage) USE_COVERAGE=0 ;;
        --setup)
            echo "${BOLD}Creating .venv with coverage...${RESET}"
            python3 -m venv .venv && .venv/bin/pip install --quiet --upgrade pip coverage
            echo "${GREEN}Done.${RESET} Re-run ./run-tests.sh"
            exit 0 ;;
        -v|--verbose) ARGS+=("-v") ;;
        -*) ARGS+=("$a") ;;
        *) PATTERN="$a" ;;
    esac
done

# Prefer a local .venv, then whatever python3 has, then no coverage at all.
PY=python3
if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; fi
if [[ $USE_COVERAGE -eq 1 ]] && ! "$PY" -m coverage --version >/dev/null 2>&1; then
    USE_COVERAGE=0
    COVERAGE_HINT=1
fi

# -k filters by test name (method or class), which is what people expect:
#   ./run-tests.sh gpu          -> every test with "gpu" in its name
#   ./run-tests.sh Graphics     -> the TestGraphics* classes
# A file-name filter would miss graphics tests living in test_checks_hardware.py.
DISCOVER=(-m unittest discover -s tests -t tests)
[[ -n "$PATTERN" ]] && DISCOVER+=(-k "$PATTERN")

if [[ $USE_COVERAGE -eq 1 ]]; then
    PYTHONPATH=tests "$PY" -m coverage run --source=vitals "${DISCOVER[@]}" ${ARGS[@]+"${ARGS[@]}"}
    echo
    if PYTHONPATH=tests "$PY" -m coverage report --show-missing --fail-under=100; then
        echo "${GREEN}${BOLD}Coverage at 100%.${RESET}"
    else
        echo "${YELLOW}${BOLD}Coverage below 100%.${RESET} Lines listed above are untested."
        echo "${DIM}Every branch needs a test, including failure and skip paths.${RESET}"
        exit 1
    fi
else
    PYTHONPATH=tests "$PY" "${DISCOVER[@]}" ${ARGS[@]+"${ARGS[@]}"}
    if [[ -n "${COVERAGE_HINT:-}" ]]; then
        echo
        echo "${DIM}Tests passed. For the coverage gate: ./run-tests.sh --setup${RESET}"
    fi
fi
