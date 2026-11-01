PYTHON ?= python

.PHONY: proto test server

proto:
	$(PYTHON) -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/bioskop.proto

test: proto
	$(PYTHON) -m pytest tests/ -v

server: proto
	$(PYTHON) -m server.main --port 50051 --workers 10