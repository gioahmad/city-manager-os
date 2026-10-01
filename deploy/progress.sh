#!/usr/bin/env bash
# Stage completion is measurable; dump duration/total bytes are not known in advance.
progress_step(){
  local complete=$1 total=$2 label=$3 filled empty
  filled=$((complete * 20 / total)); empty=$((20 - filled))
  printf -v filled '%*s' "$filled" ''; printf -v empty '%*s' "$empty" ''
  printf '\n[%s%s] %s/%s stages complete — %s\n' "${filled// /#}" "${empty// /-}" "$complete" "$total" "$label" >&2
}

run_with_progress(){
  local label=$1 size_file=$2 pid started=$SECONDS elapsed bytes status=0
  shift 2
  printf '%s: starting\n' "$label" >&2
  # Explicit stdin forwarding preserves migration/archive pipelines and Python heredocs.
  (trap - ERR; "$@") <&0 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    elapsed=$((SECONDS - started))
    if ((elapsed > 0 && elapsed % 5 == 0)) && kill -0 "$pid" 2>/dev/null; then
      printf '%s: running · %ss elapsed' "$label" "$elapsed" >&2
      if [[ -n "$size_file" && -f "$size_file" ]]; then
        bytes=$(stat -c %s "$size_file")
        printf ' · %s bytes written' "$bytes" >&2
      fi
      printf '\n' >&2
    fi
  done
  wait "$pid" || status=$?
  if ((status == 0)); then
    printf '%s: complete · %ss elapsed\n' "$label" "$((SECONDS - started))" >&2
  else
    printf '%s: FAILED (exit %s) · %ss elapsed\n' "$label" "$status" "$((SECONDS - started))" >&2
  fi
  return "$status"
}
