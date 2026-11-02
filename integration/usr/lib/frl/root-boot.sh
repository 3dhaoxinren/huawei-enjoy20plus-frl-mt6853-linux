#!/usr/lib/frl/busybox sh
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/frl/bin
BB=/usr/lib/frl/busybox
"$BB" mkdir -p /usr/lib/frl/bin
"$BB" --install -s /usr/lib/frl/bin
[ -d /run/frl/control ] && [ -c /dev/ttyGS0 ]
ln -sf /run/frl/control/keepalive /tmp/frl-keepalive
ln -sf /run/frl/control/request-exit /tmp/frl-request-exit
/usr/bin/python3 /usr/lib/frl/prepare-native.py
/usr/bin/python3 /usr/lib/frl/device-nodes.py >/run/frl/log/device-nodes.json 2>/run/frl/log/device-nodes.err || echo DEVICE_NODES_PREPARE_FAILED >>/run/frl/log/root-boot.log
/usr/bin/python3 /usr/lib/frl/backlight.py apply >/run/frl/log/backlight.json 2>/run/frl/log/backlight.err || echo BACKLIGHT_PREPARE_FAILED >>/run/frl/log/root-boot.log
/usr/bin/python3 /usr/lib/frl/claim-mutable-root.py
/usr/local/sbin/frl-performance apply >/run/frl/log/performance-boot.json 2>/run/frl/log/performance-boot.err || { echo PERFORMANCE_POLICY_FAILED >>/run/frl/log/root-boot.log || :; }
/usr/local/sbin/frl-swap apply >/run/frl/log/swap-boot.json 2>/run/frl/log/swap-boot.err || { echo SWAP_PREPARE_FAILED >>/run/frl/log/root-boot.log || :; }
/usr/sbin/ip link set lo up
/usr/sbin/ip addr replace 127.0.0.1/8 dev lo
mkdir -p /run/frl/resolver /var/log/frl/services
chmod 755 /run/frl /run/frl/resolver
[ -e /run/frl/resolver/resolv.conf ] || : >/run/frl/resolver/resolv.conf
chmod 644 /run/frl/resolver/resolv.conf
[ -L /etc/resolv.conf ] && [ "$(readlink /etc/resolv.conf)" = /run/frl/resolver/resolv.conf ]
# Failure remains explicit, but independent services can still expose a repair UI.
touch_status=0
/usr/lib/frl/touch/prepare.sh || touch_status=$?
echo "touch_prepare_exit=$touch_status" >>/run/frl/log/root-boot.log
while IFS= read -r name; do
    case "$name" in ''|'#'*) continue;; vendor-log|udev|seatd|desktop|radio|wpa|dhcp|hwservicemanager|vndservicemanager|aptouch|health|fuel|mtk-session|power|charge-limit|ssh|chrony|apps|maintenance|power-terminal) :;; *) exit 78;; esac
    [ -x "/etc/frl/sv/$name/run" ]
    live="/run/frl/service/$name"
    mkdir -p "$live/log" "/var/log/frl/services/$name"
    ln -sf "/etc/frl/sv/$name/run" "$live/run"
    if [ -x "/etc/frl/sv/$name/check" ]; then ln -sf "/etc/frl/sv/$name/check" "$live/check"; fi
    printf 's2097152\nn2\n' >"/var/log/frl/services/$name/config"
    printf '#!/bin/sh\nexec /usr/bin/svlogd -tt /var/log/frl/services/%s\n' "$name" >"$live/log/run"
    chmod 755 "$live/log/run"
done </etc/frl/enabled-services
/usr/local/sbin/frl-app boot >/run/frl/log/apps-boot.log 2>&1 || { echo APPS_PREPARE_FAILED >>/run/frl/log/root-boot.log || :; }
/usr/local/sbin/frl-server boot >/run/frl/log/server-boot.json 2>/run/frl/log/server-boot.err || { echo SERVER_MODE_PREPARE_FAILED >>/run/frl/log/root-boot.log || :; }
[ "$touch_status" -eq 0 ]
echo ROOT_FILESYSTEM_AND_SERVICE_LAYOUT_PREPARED >>/run/frl/log/root-boot.log
