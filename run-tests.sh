#!/usr/bin/env bash
# Run the test suite. Uses coverage when available, plain unittest otherwise.
set -euo pipefail
cd "$(dirname "$0")"

if python3 -m coverage --version >/dev/null 2>&1; then
    PYTHONPATH=tests python3 -m coverage run --source=vitals \
        -m unittest discover -s tests -t tests "$@"
    python3 -m coverage report --show-missing --fail-under=100
else
    echo "coverage not installed - running without it (pip install coverage)"
    PYTHONPATH=tests python3 -m unittest discover -s tests -t tests "$@"
fi
