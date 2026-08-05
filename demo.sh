#!/usr/bin/env bash
# One event, three destinations, three different fates.
#
# The interesting part of this service is what happens when a delivery FAILS,
# and that is invisible from a single happy-path request. So this script stages
# a failure on purpose: one destination that always works, one pointed at a
# host that cannot resolve, and one batched internal sink. Then it watches the
# three delivery rows for the same event diverge.
#
#   ./demo.sh                          # against docker compose on localhost
#   ./demo.sh https://your.onrender.com # against a deploy
#
# Requires: curl, python3 (for JSON parsing — no jq dependency).
set -euo pipefail

BASE="${1:-http://localhost:8000}"
BASE="${BASE%/}"

# Any URL that reliably 200s. Override with a webhook.site inbox to actually
# watch the transformed payload land somewhere you can read.
OK_URL="${WEBHOOK_URL:-https://postman-echo.com/post}"
# .invalid is reserved by RFC 2606 and can never resolve, so this fails every
# time with no dependency on some third-party service staying up.
FAIL_URL="${FAIL_URL:-https://broken.invalid/webhook}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
	B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
	GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'
else
	B=""; DIM=""; R=""; GREEN=""; RED=""; YELLOW=""; BLUE=""
fi

say() { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$BLUE" "$R" "$B" "$*" "$R"; }
die() { printf '%serror:%s %s\n' "$RED" "$R" "$*" >&2; exit 1; }

# Pull one field out of a JSON object on stdin. Beats grep/sed, which break the
# moment a value contains a brace.
jget() { python3 -c 'import json,sys; print(json.load(sys.stdin)["'"$1"'"])'; }

command -v python3 >/dev/null || die "python3 is required (JSON parsing)"
command -v curl >/dev/null || die "curl is required"

step "Checking $BASE"
health="$(curl -fsS --max-time 90 "$BASE/health" 2>/dev/null)" \
	|| die "no response from $BASE/health (a free instance that has spun down can take ~1 min to wake)"
say "  $health"

step "1. Register a source"
src="$(curl -fsS -X POST "$BASE/v1/sources" \
	-H 'Content-Type: application/json' \
	-d '{"name": "demo-webapp"}')"
SOURCE_ID="$(printf '%s' "$src" | jget id)"
WRITE_KEY="$(printf '%s' "$src" | jget write_key)"
say "  source     $SOURCE_ID"
say "  write key  $WRITE_KEY"
say "  ${DIM}shown once and never again — only its sha256 is stored${R}"

mkdest() { # name, json body -> prints destination id
	local label="$1" body="$2" out id
	out="$(curl -fsS -X POST "$BASE/v1/destinations" \
		-H 'Content-Type: application/json' -d "$body")" \
		|| die "could not create the $label destination"
	id="$(printf '%s' "$out" | jget id)"
	printf '%s' "$id"
}

step "2. Point it at three places"
HEALTHY_ID="$(mkdest healthy "$(cat <<JSON
{"source_id": "$SOURCE_ID", "type": "http",
 "filter": "user.*",
 "config": {"url": "$OK_URL"},
 "transform": {"email": "user.email", "plan": "plan"}}
JSON
)")"
say "  ${GREEN}healthy${R}    $HEALTHY_ID  -> $OK_URL"
say "             ${DIM}filter user.* , transform picks email+plan out of the payload${R}"

BROKEN_ID="$(mkdest broken "$(cat <<JSON
{"source_id": "$SOURCE_ID", "type": "http",
 "config": {"url": "$FAIL_URL"}}
JSON
)")"
say "  ${RED}broken${R}     $BROKEN_ID  -> $FAIL_URL"
say "             ${DIM}unresolvable, so this one climbs the retry ladder${R}"

SINK_ID="$(mkdest warehouse "$(cat <<JSON
{"source_id": "$SOURCE_ID", "type": "warehouse", "config": {},
 "batch_size": 2, "batch_window_s": 5}
JSON
)")"
say "  ${BLUE}warehouse${R}  $SINK_ID  -> internal table"
say "             ${DIM}batched: flushes on size 2 OR after a 5s window${R}"

step "3. Send ONE event"
IDEM="demo-$$-$(date +%s)"
track="$(curl -fsS -X POST "$BASE/v1/track" \
	-H "Authorization: Bearer $WRITE_KEY" \
	-H 'Content-Type: application/json' \
	-H "Idempotency-Key: $IDEM" \
	-d '{"type": "user.signed_up",
	     "payload": {"user": {"email": "ada@example.com", "id": 42}, "plan": "pro"}}')"
EVENT_ID="$(printf '%s' "$track" | jget id)"
say "  202 Accepted   event $EVENT_ID"
say "  ${DIM}202, not 200: we have taken responsibility for it, not delivered it${R}"

step "4. Send the same Idempotency-Key again"
code="$(curl -fsS -o /tmp/er_dup.$$ -w '%{http_code}' -X POST "$BASE/v1/track" \
	-H "Authorization: Bearer $WRITE_KEY" \
	-H 'Content-Type: application/json' \
	-H "Idempotency-Key: $IDEM" \
	-d '{"type": "user.signed_up",
	     "payload": {"user": {"email": "ada@example.com", "id": 42}, "plan": "pro"}}')"
dup_id="$(jget id < "/tmp/er_dup.$$")"; rm -f "/tmp/er_dup.$$"
if [ "$dup_id" = "$EVENT_ID" ] && [ "$code" = "200" ]; then
	say "  ${GREEN}200 OK${R}         event $dup_id  ${DIM}(same id, no second set of deliveries)${R}"
	say "  ${DIM}200 not 202: 'already had it'. The unique constraint is the referee.${R}"
else
	say "  ${YELLOW}unexpected:${R} status $code, id $dup_id"
fi

step "5. Watch the three deliveries diverge"
say "  ${DIM}polling GET /v1/events/$EVENT_ID — Ctrl-C to stop${R}"
say ""

render='
import json, os, sys
plain = os.environ.get("NO_COLOR")
COL = {} if plain else {"delivered": "\033[32m", "dead": "\033[31m",
                        "pending": "\033[33m", "delivering": "\033[34m"}
OFF = "" if plain else "\033[0m"
DIM = "" if plain else "\033[2m"
names = json.loads(sys.argv[1])
event = json.load(sys.stdin)
head = ("DESTINATION", "TYPE", "STATUS", "TRIES", "LAST ERROR")
print("  %-12s%-11s%-12s%5s   %s" % head)
print("  " + "-" * 66)
for d in sorted(event["deliveries"], key=lambda d: names.get(d["destination_id"], "z")):
    name = names.get(d["destination_id"], d["destination_id"][:10])
    colour = COL.get(d["status"], "")
    err = (d["last_error"] or "")[:28]
    print("  %-12s%-11s%s%-12s%s%5d   %s%s%s" % (
        name, d["destination_type"], colour, d["status"], OFF,
        d["attempts"], DIM, err, OFF))
settled = all(d["status"] in ("delivered", "dead") for d in event["deliveries"])
sys.exit(0 if settled else 7)
'
NAMES="$(python3 -c 'import json,sys; print(json.dumps({sys.argv[1]:"healthy",sys.argv[2]:"broken",sys.argv[3]:"warehouse"}))' \
	"$HEALTHY_ID" "$BROKEN_ID" "$SINK_ID")"

settled=0
for _ in $(seq 1 60); do
	body="$(curl -fsS "$BASE/v1/events/$EVENT_ID")"
	printf '\033[H\033[J' 2>/dev/null || true
	step "5. Watch the three deliveries diverge"
	say "  ${DIM}event $EVENT_ID${R}"
	say ""
	if printf '%s' "$body" | python3 -c "$render" "$NAMES"; then settled=1; break; fi
	sleep 2
done

if [ "$settled" = "1" ]; then
	say ""
	say "  ${B}Everything has settled.${R}"
else
	say ""
	say "  ${YELLOW}Still retrying after 2 minutes.${R}"
	say "  ${DIM}Default retry_base_seconds=10 over 8 attempts takes ~20 min to dead-letter.${R}"
	say "  ${DIM}For a watchable demo set RETRY_BASE_SECONDS=2 and RETRY_MAX_ATTEMPTS=5.${R}"
fi

step "6. What the numbers say"
for pair in "healthy:$HEALTHY_ID" "broken:$BROKEN_ID" "warehouse:$SINK_ID"; do
	name="${pair%%:*}"; id="${pair##*:}"
	printf '  %-11s %s\n' "$name" "$(curl -fsS "$BASE/v1/destinations/$id/stats")"
done

step "7. Fix the outage, replay the dead"
say "  ${DIM}This is what you run after the downstream service comes back.${R}"
replayed="$(curl -fsS -X POST "$BASE/v1/destinations/$BROKEN_ID/replay" | jget replayed)"
say "  moved $replayed dead ${BLUE}broken${R} deliver$( [ "$replayed" = "1" ] && echo y || echo ies ) back to pending"
say "  ${DIM}They will fail again — $FAIL_URL still does not resolve. That is${R}"
say "  ${DIM}the point: replay is a lever you pull once it is genuinely fixed.${R}"

say ""
say "${B}What just happened${R}"
say "  One event produced three delivery rows with independent state."
say "  The broken destination retried and died without touching the other two."
say "  Retry, backoff, dead-lettering and replay are per destination, not per event."
say ""
say "  Full detail:  ${DIM}curl $BASE/v1/events/$EVENT_ID${R}"
say "  Swagger:      ${DIM}$BASE/docs${R}"
say "  Live view:    ${DIM}$BASE/demo${R}"
