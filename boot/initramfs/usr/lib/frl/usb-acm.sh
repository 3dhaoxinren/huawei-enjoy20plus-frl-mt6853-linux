#!/usr/lib/frl/busybox sh
set -eu
mount -t configfs configfs /sys/kernel/config
G=/sys/kernel/config/usb_gadget/frl_v15
[ ! -e "$G" ] || exit 1
mkdir "$G"
printf 0x1d6b >"$G/idVendor"
printf 0x0104 >"$G/idProduct"
printf 0x0200 >"$G/bcdUSB"
printf 0x0200 >"$G/bcdDevice"
printf 0x02 >"$G/bDeviceClass"
printf 0 >"$G/bDeviceSubClass"
printf 0 >"$G/bDeviceProtocol"
mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409" "$G/functions/acm.usb0"
printf FRL-LINUX-V15 >"$G/strings/0x409/serialnumber"
printf FRL-Linux >"$G/strings/0x409/manufacturer"
printf 'FRL V15 review candidate' >"$G/strings/0x409/product"
printf 'USB serial rescue' >"$G/configs/c.1/strings/0x409/configuration"
printf 250 >"$G/configs/c.1/MaxPower"
ln -s "$G/functions/acm.usb0" "$G/configs/c.1/acm.usb0"
set -- /sys/class/udc/*
[ "$#" -eq 1 ] && [ -d "$1" ] || exit 1
printf '%s' "${1##*/}" >"$G/UDC"
tick=0
while [ ! -r /sys/class/tty/ttyGS0/dev ]; do
    tick=$((tick+1)); [ "$tick" -lt 20 ] || exit 1; sleep 1
done
IFS=: read -r major minor </sys/class/tty/ttyGS0/dev
mknod /dev/ttyGS0 c "$major" "$minor"
chmod 600 /dev/ttyGS0
