#!/bin/sh
# install.sh: put the Morning Paper on a jailbroken Kindle.
#
# Run it once, from KOReader's terminal or over SSH, straight from the Press:
#
#   curl http://<press-address>:<port>/kindle/install.sh | sh
#   wget -q -O - http://<press-address>:<port>/kindle/install.sh | sh
#
# The Press fills in its own address below as it serves this file, so there is
# nothing to edit. Everything is installed in /mnt/us/mnn. Running it again is
# safe: it replaces the bootstrap and restarts it.
#
# Written for the Kindle's BusyBox sh.

PRESS="@PRESS@"
MNN_DIR="${MNN_DIR:-/mnt/us/mnn}"
END_MARK="# end of script"

fail() {
  echo "install: $*" >&2
  exit 1
}

fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 60 -o "$2" "$1"
  else
    wget -q -T 60 -O "$2" "$1"
  fi
}

case "$PRESS" in
  http://*|https://*) ;;
  *) fail "this file must be downloaded from the Press, which fills in its address" ;;
esac

mkdir -p "$MNN_DIR" || fail "cannot create $MNN_DIR"

new="$MNN_DIR/bootstrap.new"
fetch "$PRESS/kindle/bootstrap.sh" "$new" || fail "cannot download the bootstrap from $PRESS"
if [ "$(tail -n 1 "$new")" != "$END_MARK" ] || ! sh -n "$new"; then
  rm -f "$new"
  fail "the bootstrap from $PRESS is incomplete"
fi

# Stop a bootstrap left by an earlier install. The pid is only trusted if it
# still belongs to a bootstrap.
if [ -f "$MNN_DIR/bootstrap.pid" ]; then
  old=$(cat "$MNN_DIR/bootstrap.pid")
  case "$old" in
    ''|*[!0-9]*) ;;
    *)
      if [ -r "/proc/$old/cmdline" ] && tr '\0' ' ' <"/proc/$old/cmdline" | grep -q "bootstrap.sh"; then
        kill "$old" 2>/dev/null
        # It finishes the download it is in before it stops, which can take
        # a minute. Two bootstraps must not run side by side.
        echo "Stopping the Morning Paper that is already running."
        waited=0
        while [ -d "/proc/$old" ] && [ "$waited" -lt 90 ]; do
          sleep 1
          waited=$((waited + 1))
        done
      fi
      ;;
  esac
  rm -f "$MNN_DIR/bootstrap.pid"
fi

mv -f "$new" "$MNN_DIR/bootstrap.sh" || fail "cannot write $MNN_DIR/bootstrap.sh"
echo "PRESS=\"$PRESS\"" >"$MNN_DIR/press.conf"

# Detached from this terminal, so it outlives the shell that installed it.
if command -v setsid >/dev/null 2>&1; then
  setsid sh "$MNN_DIR/bootstrap.sh" >/dev/null 2>&1 </dev/null &
else
  nohup sh "$MNN_DIR/bootstrap.sh" >/dev/null 2>&1 </dev/null &
fi

echo "Morning Paper installed in $MNN_DIR, reading from $PRESS."
echo "It is fetching the newest paper now. Log: $MNN_DIR/mnn.log"
# end of script
