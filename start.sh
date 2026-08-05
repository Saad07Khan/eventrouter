#!/bin/sh
# Free-tier deploy: API and worker are separate entrypoints (app.main /
# app.worker) that scale independently in a real deployment — this script
# co-locates them in one process/container so a single free instance covers
# both. Splitting them back into two services is a config change, not a code
# change: `python -m app.worker` and `uvicorn app.main:app` run unmodified.
set -e

# Bring the schema up to date before anything serves traffic. Idempotent, so
# it is a no-op on every deploy after the first, and `set -e` means a failed
# migration aborts the boot rather than serving against a stale schema.
#
# This lives here rather than in a platform start command because chaining it
# with && there depends on the platform handing the string to a shell, which
# Render does not do reliably: the whole command ends up as one argv entry and
# exits 127. In the script it is just a line, and the container is correct
# wherever it runs.
alembic upgrade head

python -m app.worker &
worker_pid=$!

# Watchdog: co-locating the two means a dead worker is invisible — uvicorn keeps
# serving and /health keeps returning ok while deliveries silently stop forever.
# Taking the whole container down instead lets the platform restart it, and makes
# /health honest by construction: no worker means no container means no response.
#
# $$ is this shell, which `exec` below turns into uvicorn itself — so this is the
# uvicorn PID (1 in a container) without hardcoding 1 and signalling the host's
# init if anyone ever runs this script outside a container.
{
	while kill -0 "$worker_pid" 2>/dev/null; do
		sleep 5
	done
	echo "start.sh: worker exited, shutting down" >&2
	kill -TERM "$$"
} &

# exec replaces the shell with uvicorn so it receives SIGTERM directly from
# Fly during deploys/restarts, instead of the shell swallowing the signal.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
