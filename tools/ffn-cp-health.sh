#!/bin/bash
# FFN OCTEON control-plane health probe.
#
# ffn-octeon.service is Type=oneshot RemainAfterExit=yes, so it reports
# "active" forever -- including when the CP is dead. This is the thing that
# actually knows, and it keeps a timestamped history so "when did the CP go
# down, and did anything reset it" is answerable after the fact.
#
# The probe is PASSIVE: ping, ssh, sysfs and the console log only. It never
# takes /run/ffn-octeon-ctl.lock, never runs oct-remote-*, and never touches
# ffn-dpsh (one shared shell on the DP -- concurrent use wedges it).
#
# Recovery is OFF by default. Restarting ffn-octeon.service RESETS the CP,
# which destroys any in-progress hardware work, so it is opt-in, debounced,
# rate-limited, and skipped entirely while an inhibit file exists.
set -u

CONF=${FFN_CP_HEALTH_CONF:-/etc/ffn-ngfw/cp-health.conf}
STATE=${FFN_CP_HEALTH_STATE:-/var/lib/ffn-ngfw/cp-health.state}
RUNSTATE=/run/ffn-cp-health.state
CONSOLE_LOG=/var/log/ffn-octeon-console.log

CP_ADDR=127.1.1.2
CP_KEY=/root/.ssh/id_ed25519
CP_IFACE=ffnnet0
DOWN_SAMPLES=4            # consecutive DOWN probes before it counts as down
AUTO_RECOVER=no           # yes|no -- restarting the unit resets the CP
RECOVER_MAX_PER_HOUR=2
INHIBIT_FILES="/run/ffn-cp-health.inhibit /etc/ffn-ngfw/cp-health.inhibit"

[ -r "$CONF" ] && . "$CONF"

now=$(date +%s)
mkdir -p "$(dirname "$STATE")"

prev_verdict=UNKNOWN; prev_since=$now; consec_down=0; last_recovery=0; recoveries=""
# shellcheck disable=SC1090
[ -r "$STATE" ] && . "$STATE"

log(){ echo "ffn-cp-health: $*"; }

# ---- probes, cheapest first -------------------------------------------------
# L0: the PCIe transport ring. Advancing rx means the CP's kernel is running
# even if its networking or userland is broken -- the only signal that
# distinguishes "CP wedged" from "CP gone".
l0=fail; rx=0
if [ -r "/sys/class/net/$CP_IFACE/statistics/rx_packets" ]; then
  rx=$(cat "/sys/class/net/$CP_IFACE/statistics/rx_packets")
  carrier=$(cat "/sys/class/net/$CP_IFACE/carrier" 2>/dev/null || echo 0)
  [ "$carrier" = 1 ] && l0=ok
fi
prev_rx=${prev_rx:-0}
[ "$rx" -gt "$prev_rx" ] && l0_moving=yes || l0_moving=no

# Is OUR end of the transport running?  Needed to interpret L0: carrier on a
# virtual PCIe netdev drops when ffn-pcnetd stops HERE, which says nothing about
# the CP.
pcnetd=$(systemctl is-active ffn-pcnetd 2>/dev/null || echo unknown)

# L1: CP kernel networking.
if ping -c2 -W2 -q "$CP_ADDR" >/dev/null 2>&1; then l1=ok; else l1=fail; fi

# L2: CP userland.
if timeout 12 ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=8 -o BatchMode=yes -i "$CP_KEY" "root@$CP_ADDR" \
      'cut -d" " -f1 /proc/uptime' >/tmp/.cph.$$ 2>/dev/null; then
  l2=ok; cp_uptime=$(cat /tmp/.cph.$$)
else
  l2=fail; cp_uptime=
fi
rm -f /tmp/.cph.$$

# The two CP roots offer DIFFERENT shells: the OpenWrt root runs sshd, but the
# 4.9 vendor CentOS root has none; ffn-cpshd starts busybox telnetd on 2323
# ("no sshd here; reach it with ffn-cpsh").  Without this the probe reports a
# perfectly healthy 4.9 CP as DEGRADED forever.
if [ "$l2" = fail ]; then
  if timeout 6 bash -c "exec 3<>/dev/tcp/$CP_ADDR/2323" 2>/dev/null; then
    l2=ok; cp_shell=telnet
  fi
else
  cp_shell=ssh
fi

# L3: death evidence from the console -- but ONLY text written since the last
# time the CP was seen UP.  The log carries 80+ historical panic/reset banners,
# so an unscoped grep would report the box dead forever.
up_console_off=${up_console_off:-0}
con_size=0; deathsig=
if [ -f "$CONSOLE_LOG" ]; then
  con_size=$(stat -c %s "$CONSOLE_LOG")
  con_age=$(( now - $(stat -c %Y "$CONSOLE_LOG") ))
  if [ "$con_size" -gt "$up_console_off" ]; then
    deathsig=$(tail -c +$(( up_console_off + 1 )) "$CONSOLE_LOG" 2>/dev/null \
               | grep -aoiE "Kernel panic|NMI watchdog|Attempted to kill init|soft reset|Reboot in " \
               | head -1)
  fi
fi
[ -n "$deathsig" ] && l3=dead || l3=quiet

# L4: is the CP not draining the H2O ring?  The MP produces into it and the CP
# consumes; ring-full drops climbing means the CP's driver has stopped running.
# This is positive evidence of a dead kernel, independent of the IP stack.
txdrop=0
[ -r "/sys/class/net/$CP_IFACE/statistics/tx_dropped" ] && \
  txdrop=$(cat "/sys/class/net/$CP_IFACE/statistics/tx_dropped")
prev_txdrop=${prev_txdrop:-0}
[ "$txdrop" -gt "$prev_txdrop" ] && l4=stalled || l4=ok

# ---- verdict ----------------------------------------------------------------
# DOWN requires POSITIVE evidence of death.  Absence of a reply is not proof:
# an idle but healthy CP with a broken ffnnet0 looks identical to a dead one
# from here, and resetting it on that basis destroys working state.  That case
# is reported as UNREACHABLE and never auto-recovered.
if [ "$l2" = ok ]; then
  verdict=UP
elif [ "$l1" = ok ]; then
  verdict=DEGRADED                       # kernel + net up, userland not answering
elif [ "$l3" = dead ] || [ "$l4" = stalled ]; then
  verdict=DOWN                           # positive evidence of death
elif [ "$l0" = fail ] && [ "$pcnetd" = active ]; then
  verdict=DOWN                           # carrier gone while OUR end is up
elif [ "$l0" = fail ]; then
  verdict=UNREACHABLE                    # our own transport is down, not the CP
else
  verdict=UNREACHABLE                    # cannot tell from the MP
fi

# remember where the console log stood when the CP was last healthy
[ "$verdict" = UP ] && up_console_off=$con_size

case "$verdict" in
  DOWN)        consec_down=$(( consec_down + 1 )) ;;
  UNREACHABLE) consec_unreach=$(( ${consec_unreach:-0} + 1 )); consec_down=0 ;;
  *)           consec_down=0; consec_unreach=0 ;;
esac

# ---- report state transitions ----------------------------------------------
if [ "$verdict" != "$prev_verdict" ]; then
  held=$(( now - prev_since ))
  log "CP $prev_verdict -> $verdict after ${held}s (l0=$l0(pcnetd=$pcnetd) l1=$l1 l2=$l2(${cp_shell:-none}) l3=$l3 l4=$l4 rx=$rx${deathsig:+ saw=\"$deathsig\"}${cp_uptime:+ cp_uptime=${cp_uptime}s}${con_age:+ console_age=${con_age}s})"
  prev_since=$now
fi

# ---- recovery: opt-in, debounced, rate-limited, inhibitable ------------------
recover=no; why=
if [ "$consec_down" -ge "$DOWN_SAMPLES" ]; then
  inhibited=
  for f in $INHIBIT_FILES; do [ -f "$f" ] && inhibited="$f"; done
  # keep only recovery timestamps inside the last hour
  kept=; for t in $recoveries; do [ $(( now - t )) -lt 3600 ] && kept="$kept $t"; done
  recoveries=$(echo $kept)
  n=$(set -- $recoveries; echo $#)
  if [ -n "$inhibited" ]; then       why="inhibited by $inhibited"
  elif [ "$AUTO_RECOVER" != yes ]; then why="AUTO_RECOVER=no"
  elif [ "$n" -ge "$RECOVER_MAX_PER_HOUR" ]; then why="rate limit ($n in the last hour)"
  else recover=yes; fi
  [ "$recover" = no ] && log "CP down for $consec_down probes; NOT recovering: $why"
fi

if [ "$recover" = yes ]; then
  log "CP down for $consec_down probes; RESTARTING ffn-octeon.service (this resets the CP)"
  systemctl stop ffn-pcnetd 2>/dev/null || true
  systemctl restart ffn-octeon.service && log "restart issued" || log "restart FAILED rc=$?"
  last_recovery=$now; recoveries="$recoveries $now"; consec_down=0
fi

# ---- persist ----------------------------------------------------------------
cat > "$STATE" <<EOF2
prev_verdict=$verdict
prev_since=$prev_since
prev_rx=$rx
prev_txdrop=$txdrop
up_console_off=$up_console_off
consec_down=$consec_down
consec_unreach=${consec_unreach:-0}
last_recovery=$last_recovery
recoveries="$recoveries"
EOF2
cat > "$RUNSTATE" <<EOF2
verdict=$verdict since=$prev_since checked=$now
l0=$l0 l0_moving=$l0_moving l1=$l1 l2=$l2 l3=$l3 shell=${cp_shell:-none}
rx=$rx txdrop=$txdrop cp_uptime=${cp_uptime:-} console_age=${con_age:-}
deathsig=${deathsig:-none} consec_unreach=${consec_unreach:-0}
pcnetd=$pcnetd
consec_down=$consec_down auto_recover=$AUTO_RECOVER last_recovery=$last_recovery
EOF2

[ "${1:-}" = --status ] && cat "$RUNSTATE"
[ "$verdict" = UP ] && exit 0 || exit 1
