#!/usr/bin/env bash
# Shared bounded-concurrency helpers for the GitHub runner (Bash 5+).

run_io() {
  local out="$1"; local policy="$2"; shift 2
  "$@" &
  IO_PIDS+=("$!"); IO_FILES+=("$out"); IO_POLICY+=("$policy")
}

reap_any() {
  local done_pid rc idx out policy
  if wait -n -p done_pid "${IO_PIDS[@]}"; then rc=0; else rc=$?; fi
  for idx in "${!IO_PIDS[@]}"; do
    [ "${IO_PIDS[$idx]}" = "$done_pid" ] && break
  done
  out="${IO_FILES[$idx]}"
  policy="${IO_POLICY[$idx]}"
  if [ "$rc" -ne 0 ]; then
    if [ "$policy" = "required" ]; then
      IO_FAIL=1
      echo "required iptv-org grab failed: $out"
    else
      echo "DEGRADED optional iptv-org grab failed: $out"
    fi
  fi
  IO_DONE+=("$out")
  unset 'IO_PIDS[idx]' 'IO_FILES[idx]' 'IO_POLICY[idx]'
  IO_PIDS=("${IO_PIDS[@]}"); IO_FILES=("${IO_FILES[@]}"); IO_POLICY=("${IO_POLICY[@]}")
}

cleanup_fetchers() {
  local pids=("${!FETCH_PID[@]}")
  if [ "${#pids[@]}" -gt 0 ]; then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
