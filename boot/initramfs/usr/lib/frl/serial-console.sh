#!/usr/lib/frl/busybox sh
set -eu
BB=/usr/lib/frl/busybox
export PATH=/usr/lib/frl/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=vt100
stat_line=$("$BB" cat "/proc/$$/stat")
set -- ${stat_line##*) }
shift 19
controller_start=$1
printf '%s %s\n' "$$" "$controller_start" >"/run/frl/control/serial-controller.pid.$$"
"$BB" mv -f "/run/frl/control/serial-controller.pid.$$" /run/frl/control/serial-controller.pid
while :; do
    # Controller never opens the USB tty. Worker publishes identity before open.
    /usr/lib/frl/serial-session "$$" "$controller_start" &
    session=$!
    if [ "${FRL_SERIAL_READY_FD:-}" = 3 ]; then
        exec 3>&-
        unset FRL_SERIAL_READY_FD
        # A failed first handoff must EOF the ready pipe, not silently retry.
        wait "$session" || exit 1
    else
        wait "$session" || :
    fi
    "$BB" sleep 1
done
