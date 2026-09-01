#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
source "$ROOT/pipeline/fetch_scheduler.sh"

IO_PIDS=(); IO_FILES=(); IO_POLICY=(); IO_DONE=(); IO_FAIL=0
run_io slow.xml required bash -c 'sleep 0.20; exit 0'
run_io fast.xml optional bash -c 'sleep 0.05; exit 7'
run_io required.xml required bash -c 'sleep 0.10; exit 9'
reap_any
[ "${IO_DONE[0]}" = "fast.xml" ]
[ "$IO_FAIL" = "0" ]
reap_any
[ "${IO_DONE[1]}" = "required.xml" ]
[ "$IO_FAIL" = "1" ]
reap_any
[ "${IO_DONE[2]}" = "slow.xml" ]
[ "${#IO_PIDS[@]}" = "0" ]

marker=$(mktemp)
rm -f "$marker"
declare -A FETCH_PID
bash -c "sleep 0.5; touch '$marker'" &
child_pid=$!
FETCH_PID[$child_pid]=cleanup-test
(
  trap cleanup_fetchers EXIT
  exit 1
) || true
sleep 0.1
! kill -0 "$child_pid" 2>/dev/null
sleep 0.6
[ ! -e "$marker" ]

echo "fetch scheduler behavior passed"
