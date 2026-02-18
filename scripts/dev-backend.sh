#!/bin/bash
cd "$(dirname "$0")/../backend"
uv run uvicorn main:app --host 127.0.0.1 --port 8008 --reload
