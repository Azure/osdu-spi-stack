#!/usr/bin/env bash
# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Drain the fork deploys in flight before a lifecycle run changes the
# environment. Run it after `spi maintenance set`: the flag stops new
# pins, and this waits out the ones already placed. Each poll sweeps the
# ephemeral pins whose owning run has finished (`spi service reset --ephemeral
# --stale-only`), then reads what remains from `spi status --json`. Once no
# ephemeral pin remains, the set must stay empty for --quiescence seconds,
# longer than a pin's path from its deployability check to its post-write
# maintenance recheck, so a pin that read the flag clear has either rolled
# itself back or shows in the scan. Operator pins are not fork deploys and
# never hold the drain.
#
# --window bounds the wait for the set to empty; the quiescence that follows
# runs to its end. Each sweep and status read is killed after --call-timeout,
# so the drain takes at most --window plus --quiescence plus two call timeouts.
# A sweep restores canonicals before it waits on Flux, so a killed wait loses
# nothing and the next poll reads the set empty.
#
# Exit codes:
#   0  no ephemeral pin for the whole quiescence interval
#   1  --window elapsed with ephemeral pins remaining or unreadable, or
#      maintenance is not set
#   2  prerequisite missing or bad argument

set -u
set -o pipefail

WINDOW=900
QUIESCENCE=480
POLL=30
CALL_TIMEOUT=60

usage() {
    cat <<'EOF'
Usage: drain_ephemeral_pins.sh [--window SECONDS] [--quiescence SECONDS]
                               [--poll SECONDS] [--call-timeout SECONDS]
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --window) WINDOW="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
        --quiescence) QUIESCENCE="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
        --poll) POLL="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
        --call-timeout) CALL_TIMEOUT="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for value in "$WINDOW" "$QUIESCENCE" "$POLL" "$CALL_TIMEOUT"; do
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "error: --window, --quiescence, --poll, and --call-timeout take whole seconds, got '$value'" >&2
        exit 2
    fi
done
if [ "$CALL_TIMEOUT" -eq 0 ]; then
    echo "error: --call-timeout must be at least one second" >&2
    exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "error: jq is required but not on PATH" >&2
    exit 2
fi

# GNU timeout signals the command's whole process group, so a killed sweep
# takes its `flux reconcile` child with it.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT=timeout
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT=gtimeout
else
    echo "error: timeout (GNU coreutils) is required but not on PATH" >&2
    exit 2
fi

# The installed release wheel first; `uv run spi` only for a source checkout.
if command -v spi >/dev/null 2>&1; then
    SPI=(spi)
elif command -v uv >/dev/null 2>&1; then
    SPI=(uv run spi)
else
    echo "error: spi is required but not on PATH" >&2
    exit 2
fi

fmt_mmss() { printf '%02d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }

# Prints "<count>" then one line per ephemeral pin, or fails on a payload that
# does not carry the maintenance flag and the pin map.
ephemeral_pins() {
    jq -er '
        if (.maintenance | type) == "boolean" and (.images.pins | type) == "object"
        then
            (.images.pins | to_entries | map(select(.value.ephemeral == true))) as $pins
            | ($pins | length), ($pins[] | "  \(.key): run \(.value.runId) of \(.value.sourceRepo)")
        else error("no pin state") end
    '
}

START=$SECONDS
EMPTY_SINCE=-1

echo "drain_ephemeral_pins · window $(fmt_mmss "$WINDOW") · quiescence $(fmt_mmss "$QUIESCENCE") · poll ${POLL}s"

while :; do
    problem=""
    count=""

    # The sweep's command panels show what it changes; its final stdout line
    # is the JSON outcome.
    sweep_exit=0
    sweep_out=$("$TIMEOUT" "$CALL_TIMEOUT" "${SPI[@]}" service reset --ephemeral --stale-only --json) \
        || sweep_exit=$?
    outcome=$(tail -n 1 <<<"$sweep_out")
    sed '$d' <<<"$sweep_out"
    stamp="[$(fmt_mmss $(( SECONDS - START ))) / $(fmt_mmss "$WINDOW")]"
    if [ "$sweep_exit" -eq 124 ]; then
        problem="the stale-pin sweep ran past $(fmt_mmss "$CALL_TIMEOUT") and was stopped"
    elif [ "$sweep_exit" -ne 0 ]; then
        detail=$(jq -r '.detail // empty' <<<"$outcome" 2>/dev/null) || detail=""
        problem="the stale-pin sweep failed (exit $sweep_exit)${detail:+: $detail}"
    else
        swept=$(jq -r '(.swept // []) | join(", ")' <<<"$outcome" 2>/dev/null) || swept=""
        if [ -n "$swept" ]; then
            echo "$stamp swept pins whose owning run finished: $swept"
        fi
    fi

    if [ -z "$problem" ]; then
        read_at=$(( SECONDS - START ))
        status_exit=0
        status=$("$TIMEOUT" "$CALL_TIMEOUT" "${SPI[@]}" status --json) || status_exit=$?
        # Exit 2 is "not deployable", which maintenance always is.
        if [ "$status_exit" -eq 124 ]; then
            problem="spi status --json ran past $(fmt_mmss "$CALL_TIMEOUT") and was stopped"
        elif [ "$status_exit" -ne 0 ] && [ "$status_exit" -ne 2 ]; then
            problem="spi status --json failed (exit $status_exit)"
        elif ! pins=$(ephemeral_pins <<<"$status" 2>/dev/null); then
            problem="spi status --json carried no maintenance flag or pin state"
        elif [ "$(jq -r .maintenance <<<"$status")" != "true" ]; then
            echo "ERROR: maintenance is not set, so new fork deploys are still admitted; set it with 'spi maintenance set' before draining" >&2
            exit 1
        else
            count=$(head -n 1 <<<"$pins")
        fi
    fi

    elapsed=$(( SECONDS - START ))
    stamp="[$(fmt_mmss "$elapsed") / $(fmt_mmss "$WINDOW")]"

    if [ "$count" = "0" ]; then
        # The interval runs from after the read that first saw the set empty
        # to before the latest read, so a slow call never lengthens it.
        if [ "$EMPTY_SINCE" -lt 0 ]; then
            EMPTY_SINCE=$elapsed
            held=0
            echo "$stamp no ephemeral pins; holding $(fmt_mmss "$QUIESCENCE") of quiescence"
        else
            held=$(( read_at - EMPTY_SINCE ))
        fi
        if [ "$held" -ge "$QUIESCENCE" ]; then
            echo "✓ drained: no ephemeral pin for $(fmt_mmss "$held") at $(fmt_mmss "$elapsed")"
            exit 0
        fi
        next_deadline=$(( EMPTY_SINCE + QUIESCENCE ))
    else
        if [ "$EMPTY_SINCE" -ge 0 ]; then
            echo "$stamp the pin set is no longer provably empty; quiescence restarts once it is"
        fi
        EMPTY_SINCE=-1
        if [ -n "$problem" ]; then
            echo "$stamp $problem" >&2
        else
            echo "$stamp waiting on $count ephemeral pin(s) whose owning run has not finished:"
            tail -n +2 <<<"$pins"
        fi
        if [ "$elapsed" -ge "$WINDOW" ]; then
            echo "ERROR: drain window of $(fmt_mmss "$WINDOW") elapsed before every fork deploy finished; maintenance stays set" >&2
            if [ -n "$problem" ]; then
                echo "last problem: $problem" >&2
            else
                tail -n +2 <<<"$pins" >&2
            fi
            exit 1
        fi
        next_deadline=$WINDOW
    fi

    # The last poll lands on the deadline rather than up to a poll past it.
    wait_for=$(( next_deadline - elapsed ))
    [ "$wait_for" -gt "$POLL" ] && wait_for=$POLL
    [ "$wait_for" -lt 0 ] && wait_for=0
    sleep "$wait_for"
done
