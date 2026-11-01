#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/bioskop.proto