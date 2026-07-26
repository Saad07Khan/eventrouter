#!/bin/sh
# Free-tier deploy: API and worker are separate entrypoints (app.main /
# app.worker) that scale independently in a real deployment — this script
# co-locates them in one process/container so a single free instance covers
# both. Splitting them back into two services is a config change, not a code
# change: `python -m app.worker` and `uvicorn app.main:app` run unmodified.
set -e

python -m app.worker &

# exec replaces the shell with uvicorn so it receives SIGTERM directly from
# Fly during deploys/restarts, instead of the shell swallowing the signal.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
