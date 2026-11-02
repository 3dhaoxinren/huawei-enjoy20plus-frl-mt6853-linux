#!/usr/lib/frl/busybox sh
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/frl/bin
# PID1 bounds this complete helper and keeps the independent watchdog alive.
exec 9>/run/frl/server-mode.lock
/usr/bin/flock -w 8 9 || echo MODE_TRANSITION_LOCK_TIMEOUT
for role in power-terminal maintenance apps ssh chrony charge-limit power health fuel mtk-session desktop seatd aptouch hwservicemanager vndservicemanager dhcp wpa radio udev vendor-log; do
    path="/run/frl/service/$role"
    [ -d "$path" ] || continue
    /usr/bin/sv -w 4 exit "$path" || echo "service_stop_incomplete=$role"
done
# Init performs identity-scoped cleanup of any residual users before RAM exec.
/usr/local/sbin/frl-swap stop || echo SWAP_STOP_FAILED
exit 0
