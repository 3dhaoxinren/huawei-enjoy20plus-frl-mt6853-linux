#!/usr/lib/frl/busybox sh
# Entered only after the worker acquired ctty without stealing and set foreground.
unset FRL_SERIAL_READY_FD
echo 'FRL_V15_SERIAL: diagnostic root shell; watchdog remains armed'
echo 'Keepalive: touch /tmp/frl-keepalive; exit: touch /tmp/frl-request-exit'
echo 'No automatic GUI/network readiness or watchdog disarm'
exec /usr/lib/frl/busybox sh -i
