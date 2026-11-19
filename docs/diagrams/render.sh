#!/usr/bin/env bash
# Render the architecture diagram to PNG and SVG. Requires Graphviz (`dot`).
set -euo pipefail
cd "$(dirname "$0")"
dot -Tpng -Gdpi=110 architecture.dot -o architecture.png
dot -Tsvg architecture.dot -o architecture.svg
