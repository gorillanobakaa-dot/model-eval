#!/bin/bash
# Launch a sweep detached, and record its real PID.
#
# Do NOT wait on this with `pgrep -f run_eval.py`. A waiting shell's own command
# line contains that string, so pgrep matches itself and the wait deadlocks
# forever while looking exactly like a running job. That cost 79 minutes once:
# five waiters each blocked on their own existence, and the sweep they were
# guarding never launched at all.
#
# Check progress by the artifact instead, which is what actually proves work
# happened:
#     ls -1 results/ | grep -c trap        # result files appearing
#     tail -f sweep.log                    # live log
#     kill -0 "$(cat sweep.pid)"           # is it alive
set -euo pipefail
cd "$(dirname "$0")"
setsid nohup python3 run_eval.py "$@" > sweep.log 2>&1 &
echo $! > sweep.pid
sleep 2
if kill -0 "$(cat sweep.pid)" 2>/dev/null; then
    echo "sweep pid $(cat sweep.pid) running; log: $(pwd)/sweep.log"
else
    echo "sweep died immediately - check sweep.log:"; cat sweep.log; exit 1
fi
