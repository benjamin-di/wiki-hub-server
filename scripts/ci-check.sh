#!/usr/bin/env bash
# One gate, with a distinct report location for native pipeline collectors.
set -euo pipefail
export UV_PYTHON=3.12
git lfs version
make check JUNIT_XML=reports/junit/check.xml
