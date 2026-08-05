#!/bin/sh
# Free-tier deploy: API and worker are separate entrypoints (app.main /
# app.worker) that scale independently in a real deployment — this script
# co-locates them in one process/container so a single free instance covers
# both. Splitting them back into two services is a config change, not a code
# change: `python -m app.worker` and `uvicorn app.main:app` run unmodified.
set -e

python -m app.worker &
worker_pid=$!

# Watchdog: co-locating the two means a dead worker is invisible — uvicorn keeps
# serving and /health keeps returning ok while deliveries silently stop forever.
# Signalling PID 1 (uvicorn, after the exec below) takes the container down so
# the platform restarts it, which also makes /health honest by construction:
# no worker means no container means no response at all.
{
	while kill -0 "$worker_pid" 2>/dev/null; do
		sleep 5
	done
	echo "start.sh: worker exited, shutting down container" >&2
	kill -TERM 1
} &

# exec replaces the shell with uvicorn so it receives SIGTERM directly from
# Fly during deploys/restarts, instead of the shell swallowing the signal.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
