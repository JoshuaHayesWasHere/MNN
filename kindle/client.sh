#!/bin/sh
# client.sh: one wake of the Morning Paper on the Kindle.
#
# The bootstrap keeps this file in step with the Press, so a change here
# reaches the Kindle on its next wake with nothing to reinstall. By the time
# it runs, the bootstrap has downloaded the Press's manifest, if the Press
# answered, to $MNN_DIR/manifest (MNN_ONLINE=1). One run:
#
#   1. download what the manifest names and the Kindle lacks: the paper, into
#      KOReader's library, and the screens. Every file is checked against the
#      SHA-256 in the manifest before it is put in place
#   2. tell the Press what happened
#   3. wait for the next wake. The Kindle stays an ordinary Kindle: it is
#      never suspended from here and nothing of Amazon's is stopped. Each
#      time the Kindle goes into its screensaver this draws the screen for
#      the display mode, and each time the Kindle's own power service is
#      about to suspend it asks that service to wake the Kindle by its clock
#      (the RTC) when the next paper is due
#   4. on that wake, bring the Kindle fully awake so the radio comes back
#
# MNN_ONCE=1 (what `bootstrap.sh once` sets) does steps 1 and 2, draws the
# screen straight away, and returns.
# Optional settings can be put in $MNN_DIR/client.conf:
#
#   MNN_LIBRARY   where papers go (default: KOReader's home folder)
#   MNN_KEEP      how many papers to keep on the device (default: 7)
#   MNN_DISPLAY   display mode (default: frontpage)
#
# Written for the Kindle's BusyBox sh.

MNN_DIR="${MNN_DIR:-/mnt/us/mnn}"
[ -n "$PRESS" ] || {
  echo "client: PRESS is not set; run this through bootstrap.sh" >&2
  exit 2
}
# shellcheck source=/dev/null
[ -f "$MNN_DIR/client.conf" ] && . "$MNN_DIR/client.conf"
MNN_KEEP="${MNN_KEEP:-7}"
MNN_DISPLAY="${MNN_DISPLAY:-frontpage}"
KOREADER="${KOREADER:-/mnt/us/koreader}"
# Used when the Press cannot say when to come back.
RETRY=3600
# A wake that leaves something undone is followed by another before the next
# edition. Each of those is a full wake, so they are counted and thinned out.
# While the Press answers: QUICK_TRIES in a row at the interval it names, then
# one every SLOW_RETRY seconds. While it cannot be reached: AWAY_TRIES in a
# row every RETRY seconds, then one every AWAY_RETRY.
QUICK_TRIES=14
SLOW_RETRY=10800
AWAY_TRIES=3
AWAY_RETRY=21600

# --- Talking to the Press ---------------------------------------------------

if command -v curl >/dev/null 2>&1; then
  http_get() { curl -fsS -m "${3:-60}" -o "$2" "$1" 2>/dev/null; }
  http_post() {
    curl -fsS -m 10 -X POST -H "Content-Type: text/plain" --data-binary "$1" \
      "$PRESS/api/log" >/dev/null 2>&1
  }
else
  http_get() { wget -q -T "${3:-60}" -O "$2" "$1" 2>/dev/null; }
  http_post() {
    wget -q -T 10 -O /dev/null --header "Content-Type: text/plain" --post-data "$1" \
      "$PRESS/api/log" >/dev/null 2>&1
  }
fi

# Every line goes to the local log and to the Press. What the Press cannot
# take now waits in unsent.log for the next time it answers.
log() {
  line="kindle: $*"
  echo "$(date '+%Y-%m-%d %H:%M:%S') $line" >>"$MNN_DIR/mnn.log"
  http_post "$line" || echo "$line" >>"$MNN_DIR/unsent.log"
}

# The Press keeps the newest "warning:" line in its status.
warn() { log "warning: $*"; }

# For moments when there is no time to wait on the network, such as just
# before a suspend: logged here and sent on the next wake.
note() {
  line="kindle: $*"
  echo "$(date '+%Y-%m-%d %H:%M:%S') $line" >>"$MNN_DIR/mnn.log"
  echo "$line" >>"$MNN_DIR/unsent.log"
}

flush_unsent() {
  [ -s "$MNN_DIR/unsent.log" ] || return 0
  if http_post "$(tail -n 50 "$MNN_DIR/unsent.log")"; then
    rm -f "$MNN_DIR/unsent.log"
  fi
}

trim_log() {
  [ -f "$MNN_DIR/mnn.log" ] || return 0
  if [ "$(wc -c <"$MNN_DIR/mnn.log")" -gt 200000 ]; then
    tail -n 500 "$MNN_DIR/mnn.log" >"$MNN_DIR/mnn.log.tmp" \
      && mv -f "$MNN_DIR/mnn.log.tmp" "$MNN_DIR/mnn.log"
  fi
}

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

# Download a file the manifest names and put it in place only if it matches
# the manifest's SHA-256. Written beside the target and renamed, so nothing
# ever reads half a file.
#   fetch_verified <name> <sha256> <target>
fetch_verified() {
  tmp="$3.part"
  if http_get "$PRESS/kindle/$1" "$tmp" 180 && [ "$(sha "$tmp")" = "$2" ]; then
    mv -f "$tmp" "$3"
  else
    rm -f "$tmp"
    return 1
  fi
}

# The words of the first manifest line that starts with the given words.
#   manifest_line "screen ready"
manifest_line() {
  sed -n "s/^$1 //p" "$MNN_DIR/manifest" 2>/dev/null | head -n 1
}

# Names from the manifest become paths on the device.
safe_name() {
  case "$1" in
    ''|.*|*[!A-Za-z0-9._-]*) return 1 ;;
  esac
}

# --- The device -------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# Where KOReader's file browser opens, so the paper is the first thing there.
library_dir() {
  if [ -n "$MNN_LIBRARY" ]; then
    echo "$MNN_LIBRARY"
    return
  fi
  home=$(sed -n 's/^ *\["home_dir"\] = "\(.*\)",$/\1/p' "$KOREADER/settings.reader.lua" 2>/dev/null)
  if [ -n "$home" ] && [ -d "$home" ]; then
    echo "$home"
  else
    echo "/mnt/us/documents"
  fi
}

# Draw a PNG over the whole screen with a full, flashing refresh. A full
# FBInk is used where the jailbreak tooling installed one (the copy bundled
# with KOReader may be built without image support); eips is on every Kindle.
draw() {
  for fbink in /mnt/us/libkh/bin/fbink "$KOREADER/fbink"; do
    if [ -x "$fbink" ] && "$fbink" -q -c -f -w -g "file=$1" >/dev/null 2>&1; then
      return 0
    fi
  done
  have eips && eips -f -g "$1" >/dev/null 2>&1
}

powerd_state() { lipc-get-prop com.lab126.powerd state 2>/dev/null; }

waiter=""
# shellcheck disable=SC2329  # called by the trap below
stopped() {
  [ -n "$waiter" ] && kill "$waiter" 2>/dev/null
  exit 0
}
trap stopped TERM INT HUP

# Wait in the background, so a stop request is not held up by it.
nap() {
  sleep "$1" &
  waiter=$!
  wait "$waiter"
  waiter=""
}

# The display belongs on the screen whenever the Kindle is in its screensaver.
# Drawn once per screensaver: the power service announces a suspend several
# times as it counts down.
drawn=""
show_once() {
  [ -n "$drawn" ] && return 0
  show_display
  drawn=1
}

# The Kindle's power service is about to suspend it. Ask for a wake when the
# next paper is due: the service only takes an RTC alarm at this moment.
before_suspend() {
  show_once
  left=$(($1 - $(date +%s)))
  [ "$left" -lt 5 ] && left=5
  if lipc-set-prop -i com.lab126.powerd rtcWakeup "$left" 2>/dev/null; then
    note "going to sleep, RTC wake in $left seconds"
  else
    note "warning: the power service refused an RTC wake in $left seconds"
  fi
}

# Wait until an epoch time, across the Kindle's own suspends. A wake before
# then, by the power button, just carries on waiting.
sleep_until() {
  target=$1
  while :; do
    left=$((target - $(date +%s)))
    [ "$left" -le 0 ] && return 0
    if have lipc-wait-event; then
      # Ends on either event, or after that many seconds awake.
      lipc-wait-event -s "$left" com.lab126.powerd \
        goingToScreenSaver,readyToSuspend,wakeupFromSuspend,outOfScreenSaver \
        >"$MNN_DIR/event" 2>/dev/null &
      waiter=$!
      wait "$waiter"
      waiter=""
      case "$(cat "$MNN_DIR/event" 2>/dev/null)" in
        readyToSuspend*) before_suspend "$target" ;;
        goingToScreenSaver*)
          # A new screensaver, whether or not the end of the last was heard.
          # Let the Kindle's own screensaver finish painting first. On a
          # charger this is the only draw: the Kindle never suspends there.
          drawn=""
          nap 4
          [ "$(powerd_state)" = "screenSaver" ] && show_once
          ;;
        # In use again: the next screensaver gets a fresh draw.
        outOfScreenSaver*) drawn="" ;;
        # Nothing heard with time still to go: the power service could not
        # be listened to. Asked again in a few seconds: not at once, and not
        # so late that the next suspend goes by unheard.
        '')
          left=$((target - $(date +%s)))
          [ "$left" -gt 3 ] && left=3
          [ "$left" -gt 0 ] && nap "$left"
          ;;
      esac
    else
      [ "$left" -gt 60 ] && left=60
      nap "$left"
    fi
  done
}

# An RTC wake leaves the Kindle half awake: its power service says
# wakeupFromSuspend but not "resuming", the radio stays down, and about a
# minute later it suspends again. Asking the service to wake up properly is
# what a press of the power button does: the radio rejoins the network, and
# the Kindle goes back to sleep by itself when its usual idle time is up.
wake_up() {
  have lipc-set-prop || return 0
  [ "$(powerd_state)" = "active" ] && return 0
  # On a charger the Kindle never suspends, and the radio is still up.
  [ "$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null)" = "CONNECTED" ] && return 0
  lipc-set-prop com.lab126.powerd wakeUp 1 2>/dev/null
}

# --- The paper --------------------------------------------------------------

fetch_paper() {
  # shellcheck disable=SC2046
  set -- $(manifest_line paper)
  name=$1
  want=$2
  case "$name" in
    morning-paper-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].epub) ;;
    *)
      log "the Press has no paper yet"
      return 0
      ;;
  esac

  dir=$(library_dir)
  mkdir -p "$dir"
  target="$dir/$name"
  had=""
  [ -s "$target" ] && had=1
  if [ -n "$had" ] && [ "$(sha "$target")" = "$want" ]; then
    log "already have $name"
  elif fetch_verified "$name" "$want" "$target"; then
    # A different hash for a paper already here means it was printed again.
    if [ -n "$had" ]; then
      log "replaced $name in $dir"
    else
      log "downloaded $name to $dir"
    fi
  else
    warn "download of $name failed or did not match the manifest"
    return 1
  fi

  # Keep the newest few. Counted by name, so a wrong clock deletes nothing.
  # shellcheck disable=SC2012
  ls "$dir"/morning-paper-[0-9]*.epub 2>/dev/null | sort -r | tail -n "+$((MNN_KEEP + 1))" \
    | while read -r old; do
      rm -f "$old"
      rm -rf "${old%.epub}.sdr"
      log "removed ${old##*/}"
    done
  return 0
}

# --- Display modes ----------------------------------------------------------
#
# What is on the screen between reads. The manifest lists the screens the
# Press has drawn:
#
#   screen <role> <time> <refresh> <file> <sha256>
#
# Every wake downloads all of them, so a mode can show any of them later
# without the network. A mode is a function named display_<mode>, chosen by
# MNN_DISPLAY and called by show_once, once per screensaver; it draws from
# what is on the Kindle. Adding a mode means adding a function here and a
# line to the list in show_display: the bootstrap never changes.

fetch_screens() {
  mkdir -p "$MNN_DIR/screens"
  sed -n 's/^screen //p' "$MNN_DIR/manifest" >"$MNN_DIR/screens.list"
  missed=0
  while read -r role _ _ file want; do
    if ! safe_name "$file"; then
      warn "the manifest names a screen that cannot be stored"
      continue
    fi
    path="$MNN_DIR/screens/$file"
    [ "$(sha "$path")" = "$want" ] && continue
    if ! fetch_verified "$file" "$want" "$path"; then
      warn "download of the $role screen failed or did not match the manifest"
      missed=1
    fi
  done <"$MNN_DIR/screens.list"
  # Screens the manifest no longer names belong to earlier editions.
  for old in "$MNN_DIR"/screens/*; do
    [ -f "$old" ] || continue
    grep -q " ${old##*/} " "$MNN_DIR/screens.list" || rm -f "$old"
  done
  return "$missed"
}

# The path of the screen for a role, if it is on the Kindle and whole.
screen_path() {
  # shellcheck disable=SC2046
  set -- $(manifest_line "screen $1")
  safe_name "$3" || return 1
  [ "$(sha "$MNN_DIR/screens/$3")" = "$4" ] || return 1
  echo "$MNN_DIR/screens/$3"
}

display_frontpage() {
  if ! page=$(screen_path ready); then
    note "warning: there is no front page on the Kindle to draw"
    return 1
  fi
  if draw "$page"; then
    note "drew ${page##*/}"
  else
    note "warning: could not draw the front page"
    return 1
  fi
}

display_none() { :; }

show_display() {
  case "$MNN_DISPLAY" in
    frontpage) display_frontpage ;;
    none) display_none ;;
    *) note "warning: unknown display mode $MNN_DISPLAY" ;;
  esac
}

# --- One wake ---------------------------------------------------------------

battery() {
  level=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
  [ -n "$level" ] && log "battery $level%"
}

# One more wake in a row that left something undone. The count is kept in a
# file, since every wake is a new run of this script, and printed.
#   count_wake <file>
count_wake() {
  count=$(cat "$MNN_DIR/$1" 2>/dev/null)
  case "$count" in
    ''|*[!0-9]*) count=0 ;;
  esac
  count=$((count + 1))
  echo "$count" >"$MNN_DIR/$1"
  echo "$count"
}

wake() {
  NEXT_WAKE=$RETRY
  trim_log
  if [ "$MNN_ONLINE" != "1" ]; then
    # The bootstrap has already reported this; keep what is on the Kindle.
    [ "$(count_wake unreached)" -gt "$AWAY_TRIES" ] && NEXT_WAKE=$AWAY_RETRY
    return 0
  fi
  flush_unsent
  battery

  # shellcheck disable=SC2046
  set -- $(manifest_line edition)
  edition=$1
  state=$2
  # shellcheck disable=SC2046
  set -- $(manifest_line next)
  due_in=$3
  retry=$5
  case "$due_in" in
    ''|*[!0-9]*) ;;
    *) NEXT_WAKE=$due_in ;;
  esac
  case "$retry" in
    ''|*[!0-9]*) retry=$RETRY ;;
  esac

  undone=""
  fetch_paper || undone=1
  case "$state" in
    late) log "today's edition is not out, keeping $edition" ;;
    missing) log "the Press has not printed a paper yet" ;;
  esac
  [ "$state" = "fresh" ] || undone=1
  fetch_screens || undone=1

  if [ -z "$undone" ]; then
    rm -f "$MNN_DIR/retries" "$MNN_DIR/unreached"
  else
    pause=$retry
    [ "$(count_wake retries)" -gt "$QUICK_TRIES" ] && pause=$SLOW_RETRY
    # Past its edition time the Press names its retry interval as the next
    # wake. Any other time is when a paper is due, and is not slept through.
    [ "$due_in" = "$retry" ] && NEXT_WAKE=$pause
    [ "$pause" -lt "$NEXT_WAKE" ] && NEXT_WAKE=$pause
  fi

  start_script
  check_updates
  return 0
}

# Nothing here survives a restart of the Kindle. This leaves a script in
# KOReader's home folder that starts it again: long-press it, then Execute.
start_script() {
  script="$(library_dir)/Start MNN.sh"
  [ -f "$script" ] && return 0
  cat >"$script" <<EOF
#!/bin/sh
# Starts the Morning Paper after the Kindle has been restarted.
# In KOReader: long-press this file, then choose Execute.
if command -v setsid >/dev/null 2>&1; then
  setsid sh "$MNN_DIR/bootstrap.sh" >/dev/null 2>&1 </dev/null &
else
  nohup sh "$MNN_DIR/bootstrap.sh" >/dev/null 2>&1 </dev/null &
fi
EOF
}

# The jailbreak blocks firmware updates by renaming the updater. One that got
# through could remove the jailbreak, so say so if they are not blocked.
check_updates() {
  have lipc-get-prop || return 0
  if [ -e /usr/bin/otaupd ] || [ -e /usr/bin/otav3 ]; then
    warn "firmware updates are not blocked on this Kindle"
  fi
}

wake
# A wake that got this far, whatever the Press had to offer, tells the
# bootstrap this client can be trusted with the next one. Failures on the way
# are reported as warnings; the exit status is only for a broken client.
rm -f "$MNN_DIR/probation"
if [ -n "$MNN_ONCE" ]; then
  show_display
  flush_unsent
  exit 0
fi

# A one-off wake time for trying the schedule: seconds from now, used once.
if [ -f "$MNN_DIR/wake-in" ]; then
  once=$(cat "$MNN_DIR/wake-in")
  rm -f "$MNN_DIR/wake-in"
  case "$once" in
    ''|*[!0-9]*) ;;
    *) NEXT_WAKE=$once ;;
  esac
fi
# No sooner than the bootstrap would start the next cycle: after a shorter
# one it pauses for this long, with nothing listening to the power service.
[ "$NEXT_WAKE" -lt 300 ] && NEXT_WAKE=300

log "sleeping for $NEXT_WAKE seconds"
# A paper that arrives while the Kindle sits in its screensaver goes straight
# onto the screen.
[ "$(powerd_state)" = "screenSaver" ] && show_once
sleep_until $(($(date +%s) + NEXT_WAKE))
wake_up
exit 0
# end of script
