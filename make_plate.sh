#!/bin/sh
# Runs make_plate.py inside the pinned cadgen/build123d environment (needs uv: https://docs.astral.sh/uv/)
export PYTHONUTF8=1
exec uvx --no-config --managed-python --python 3.13 --from cadgen==0.7.26 --with usd-core==26.11 --with meshoptimizer==0.2.30a0 python "$(dirname "$0")/make_plate.py" "$@"
