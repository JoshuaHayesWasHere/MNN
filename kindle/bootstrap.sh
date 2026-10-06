#!/bin/sh
# bootstrap.sh: the one fixed piece of the Morning Paper on the Kindle.
#
# Installed once by install.sh and never updated after that. Everything that
# might need to change lives in the client, which this keeps up to date from
# the Press. On every wake it:
#
#   1. downloads /kindle/manifest from the Press
#   2. if the manifest names a client it does not have, downloads it, checks
#      its SHA-256 against the manifest and that the shell can parse it, keeps
#      the last one that completed a wake as client.prev, and puts the new one
#      in place
#   3. runs the client
#
# If the manifest or the client cannot be had, or fails a check, the last good
# client runs instead and the failure is reported to the Press. A new client
# that fails outright before it has finished one healthy wake is put back to
# client.prev at once, and client.prev does that wake's work. One that returns
# without a complaint has three wakes to finish one. The client does one
# wake's work and returns after sleeping until the next, so this loop is the
# whole schedule.
#
#   sh bootstrap.sh         run for good (what install.sh starts)
#   sh bootstrap.sh once    one cycle now, without sleeping afterwards
#
# What the client can rely on: MNN_DIR and PRESS in its environment,
# MNN_ONLINE=1 when $MNN_DIR/manifest was downloaded on this wake, and
# MNN_ONCE=1 for a single cycle. What it owes in return: delete
# $MNN_DIR/probation once a wake has gone well.
#
# Written for the Kindle's BusyBox sh.

MNN_DIR="${MNN_DIR:-/mnt/us/mnn}"
# Where install.sh recorded the Press address.
# shellcheck source=/dev/null
. "$MNN_DIR/press.conf" || exit 1
[ -n "$PRESS" ] || exit 1
export MNN_DIR PRESS

fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 60 -o "$2" "$1" 2>/dev/null
  else
    wget -q -T 60 -O "$2" "$1" 2>/dev/null
  fi
}

send() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 10 -X POST -H "Content-Type: text/plain" --data-binary "$1" \
      "$PRESS/api/log" >/dev/null 2>&1
  else
    wget -q -T 10 -O /dev/null --header "Content-Type: text/plain" --post-data "$1" \
      "$PRESS/api/log" >/dev/null 2>&1
  fi
}

# Reports go to the Press now if it answers, and otherwise wait in unsent.log
# for the client to send when it next can.
report() {
  line="kindle-bootstrap: $*"
  echo "$(date '+%Y-%m-%d %H:%M:%S') $line" >>"$MNN_DIR/mnn.log"
  send "$line" || echo "$line" >>"$MNN_DIR/unsent.log"
}

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

# A whole manifest starts with "mnn 1" and ends with "end".
get_manifest() {
  new="$MNN_DIR/manifest.new.$$"
  tries=0
  # Just after a wake the radio is still joining the network.
  until fetch "$PRESS/kindle/manifest" "$new" \
    && [ "$(head -n 1 "$new")" = "mnn 1" ] && [ "$(tail -n 1 "$new")" = "end" ]; do
    tries=$((tries + 1))
    if [ "$tries" -ge 6 ]; then
      rm -f "$new"
      return 1
    fi
    sleep 10
  done
  mv -f "$new" "$MNN_DIR/manifest"
}

# Put the previous client back in place of a new one.
#   roll_back <what the new client did>
roll_back() {
  # Remembered, so the same build is not installed again on the next wake.
  sha "$MNN_DIR/client.sh" >"$MNN_DIR/client.bad"
  mv -f "$MNN_DIR/client.prev" "$MNN_DIR/client.sh"
  rm -f "$MNN_DIR/probation"
  report "warning: the new client $1, back to the previous one"
}

# Put back the previous client when a new one has not managed a healthy wake.
check_probation() {
  [ -f "$MNN_DIR/probation" ] || return 0
  count=$(($(cat "$MNN_DIR/probation") + 1))
  if [ "$count" -le 3 ] || [ ! -f "$MNN_DIR/client.prev" ]; then
    echo "$count" >"$MNN_DIR/probation"
    return 0
  fi
  roll_back "did not complete a healthy wake in three"
}

update_client() {
  want=$(sed -n 's/^client client\.sh \([0-9a-f]\{64\}\) [0-9]*$/\1/p' "$MNN_DIR/manifest")
  if [ -z "$want" ]; then
    report "warning: the manifest names no client, running the last good copy"
    return 1
  fi
  [ -f "$MNN_DIR/client.sh" ] && [ "$(sha "$MNN_DIR/client.sh")" = "$want" ] && return 0
  [ "$(cat "$MNN_DIR/client.bad" 2>/dev/null)" = "$want" ] && return 0

  new="$MNN_DIR/client.new.$$"
  if ! fetch "$PRESS/kindle/client.sh" "$new"; then
    rm -f "$new"
    report "warning: could not download the client, running the last good copy"
    return 1
  fi
  if [ "$(sha "$new")" != "$want" ]; then
    rm -f "$new"
    report "warning: the downloaded client does not match the manifest, running the last good copy"
    return 1
  fi
  if ! sh -n "$new" 2>/dev/null; then
    rm -f "$new"
    report "warning: the downloaded client does not parse, running the last good copy"
    return 1
  fi
  if [ -f "$MNN_DIR/client.sh" ]; then
    # A client still on probation has completed no wake, so client.prev stays
    # the last one that did.
    [ -f "$MNN_DIR/probation" ] || cp -f "$MNN_DIR/client.sh" "$MNN_DIR/client.prev"
    echo 0 >"$MNN_DIR/probation"
  fi
  mv -f "$new" "$MNN_DIR/client.sh"
  rm -f "$MNN_DIR/client.bad"
  report "installed client $(echo "$want" | cut -c1-12)"
}

child=""
stop() {
  [ -n "$child" ] && kill "$child" 2>/dev/null
  # A stop request waits for the download in hand, and by then a newer
  # bootstrap may have written the file.
  [ "$(cat "$MNN_DIR/bootstrap.pid" 2>/dev/null)" = "$$" ] && rm -f "$MNN_DIR/bootstrap.pid"
  exit 0
}

# In the background, so a stop request is acted on at once instead of waiting
# out the client's sleep.
run_client() {
  sh "$MNN_DIR/client.sh" &
  child=$!
  wait "$child"
  status=$?
  child=""
  return "$status"
}

cycle() {
  check_probation
  if get_manifest; then
    MNN_ONLINE=1
    update_client
  else
    MNN_ONLINE=0
    report "warning: could not download the manifest from $PRESS, running the last good client"
  fi
  export MNN_ONLINE
  if [ ! -f "$MNN_DIR/client.sh" ]; then
    report "warning: there is no client to run"
    return 1
  fi
  run_client && return 0
  # A new client that fails outright is put back now and the previous one
  # does this wake's work. Left for a later wake, the Kindle could go to
  # sleep with no client running to ask for that wake.
  if [ -f "$MNN_DIR/probation" ] && [ -f "$MNN_DIR/client.prev" ]; then
    roll_back "exited with status $status before completing a wake"
    run_client && return 0
  fi
  report "warning: the client exited with status $status"
  return "$status"
}

if [ "${1:-}" = "once" ]; then
  MNN_ONCE=1
  export MNN_ONCE
  cycle
  exit $?
fi

# One bootstrap at a time: starting it twice must not run two schedules.
old=$(cat "$MNN_DIR/bootstrap.pid" 2>/dev/null)
case "$old" in
  ''|*[!0-9]*) ;;
  *)
    if [ "$old" != "$$" ] && [ -r "/proc/$old/cmdline" ] && tr '\0' ' ' <"/proc/$old/cmdline" | grep -q "bootstrap.sh"; then
      exit 0
    fi
    ;;
esac
trap stop TERM INT HUP
echo $$ >"$MNN_DIR/bootstrap.pid"
while :; do
  started=$(date +%s)
  cycle
  # A client that comes straight back must not be run in a tight loop.
  if [ $(($(date +%s) - started)) -lt 300 ]; then
    sleep 300 &
    child=$!
    wait "$child"
    child=""
  fi
done
# end of script
