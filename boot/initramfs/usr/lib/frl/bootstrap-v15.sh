#!/usr/lib/frl/busybox sh
set -eu
export PATH=/usr/lib/frl/bin:/usr/sbin:/usr/bin:/sbin:/bin
BB=/usr/lib/frl/busybox
"$BB" mkdir -p /usr/lib/frl/bin
"$BB" --install -s /usr/lib/frl/bin
mkdir -p /dev/pts /dev/shm /dev/input /dev/dri /sys/kernel/config /run/frl/storage
mount -t devpts -o gid=5,mode=0620,ptmxmode=0666 devpts /dev/pts
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm
ln -s /dev/pts/ptmx /dev/ptmx
for item in 'zero 1 5' 'random 1 8' 'urandom 1 9' 'tty 5 0'; do
    set -- $item
    mknod "/dev/$1" c "$2" "$3"
    chmod 666 "/dev/$1"
done
ln -s /proc/self/fd /dev/fd
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr

# Serial is brought up before any persistent-storage access or deployment gate.
"$BB" sh /usr/lib/frl/usb-acm.sh
"$BB" sh /usr/lib/frl/serial-console.sh </dev/null >>/run/frl/log/serial.log 2>&1 &
echo "FRL_V15_RECOVERY_SERIAL_STARTED; touch /tmp/frl-keepalive retains watchdog"

partition_node() {
    want=$1
    out=$2
    matched=0
    for event in /sys/class/block/*/uevent; do
        [ -r "$event" ] || continue
        grep -qx "PARTNAME=$want" "$event" || continue
        [ "$matched" -eq 0 ] || return 1
        sysnode=${event%/uevent}/dev
        IFS=: read -r major minor <"$sysnode"
        mknod "$out" b "$major" "$minor"
        matched=1
    done
    [ "$matched" -eq 1 ]
}
partition_node misc /dev/frl-misc || echo 'misc node unavailable; BCB preserved'
partition_node userdata /dev/frl-userdata || exit 78
STORE=/run/frl/storage
mount -t f2fs -o ro,norecovery /dev/frl-userdata "$STORE" || exit 78
FLAG=$STORE/frl-linux/v15-deployed.flag
IMAGE=$STORE/frl-linux/rootfs-v3.ext4
[ -d "$STORE/frl-linux" ] && [ ! -L "$STORE/frl-linux" ] || exit 78
[ -f "$FLAG" ] && [ ! -L "$FLAG" ] && [ -f "$IMAGE" ] && [ ! -L "$IMAGE" ] || {
    echo 'STAGING_REQUIRED: new image/explicit v15-deployed.flag absent; no handoff'
    exit 78
}
[ "$(readlink -f "$IMAGE")" = "$IMAGE" ] || exit 78
[ "$(stat -c %h "$IMAGE")" = 1 ] && [ "$(stat -c %s "$IMAGE")" = 4294967296 ] || exit 78
# The deployment flag is data, never shell code. Recover F2FS before validating
# its current image/flag contents; the readonly probe may reflect an old checkpoint.
. /usr/lib/frl/storage-rw-policy.sh
frl_prepare_storage_rw /dev/frl-userdata "$STORE" "$IMAGE" || exit 78
{
    IFS= read -r signature
    IFS= read -r filename
    IFS= read -r uuid
    IFS= read -r checksum
    IFS= read -r phase
    if IFS= read -r extra; then exit 78; fi
} <"$FLAG"
[ "$signature" = FRL_V15_IMAGE_V2 ] && [ "$filename" = rootfs-v3.ext4 ] || exit 78
case "$uuid:$checksum" in *[!0-9a-f:]*) exit 78;; esac
[ "${#uuid}" -eq 32 ] && [ "${#checksum}" -eq 64 ] || exit 78
case "$phase" in staged|accepted) :;; *) exit 78;; esac
[ -f "$IMAGE" ] && [ ! -L "$IMAGE" ] && [ "$(stat -c %h "$IMAGE")" = 1 ] || exit 78
if [ "$phase" = staged ]; then
    actual=$(sha256sum "$IMAGE")
    [ "${actual%% *}" = "$checksum" ] || { echo 'staged image hash mismatch'; exit 78; }
fi
# Accepted images are mutable OS filesystems. UUID/geometry/features stay pinned;
# normal ext4 journal recovery is allowed, not a stale whole-image hash comparison.
frl_validate_rootfs_file "$IMAGE" "$uuid" "$phase" || exit 78
LOOP=
for dev in /sys/class/block/loop*; do
    [ -r "$dev/dev" ] || continue
    if [ -r "$dev/loop/backing_file" ]; then
        [ -z "$(cat "$dev/loop/backing_file")" ] || continue
    fi
    name=${dev##*/}
    IFS=: read -r major minor <"$dev/dev"
    [ -b "/dev/$name" ] || mknod "/dev/$name" b "$major" "$minor"
    if losetup "/dev/$name" "$IMAGE"; then LOOP=/dev/$name; break; fi
done
[ -n "$LOOP" ] || { echo 'no free loop attachment; recovery mode'; exit 78; }
printf '%s\n' "$LOOP" >/run/frl/control/root-loop
mount -t ext4 -o rw,noatime,noacl "$LOOP" /newroot || exit 78
[ "$(cat /newroot/etc/frl/v15-root-staged)" = FRL_V15_PAYLOAD_1 ] || exit 78
[ -x /newroot/sbin/frl-init ] && [ -x /newroot/usr/lib/frl/busybox ] || exit 78
[ -r /newroot/usr/lib/frl/serial-console.sh ] || exit 78
if [ "$phase" = staged ]; then
    (cd /newroot && sha256sum -c etc/frl/root-payload.sha256) || exit 78
fi
for d in dev proc sys run tmp; do [ -d "/newroot/$d" ] && [ ! -L "/newroot/$d" ] || exit 78; done
# Update only the reviewed serial handoff files before PID1 changes root.
# The source bind is read-only and is always ordinarily unmounted before handoff.
[ -d /newroot/mnt ] && [ ! -L /newroot/mnt ] || exit 78
serial_stage=/newroot/mnt/frl-initramfs-stage
[ ! -e "$serial_stage" ] && [ ! -L "$serial_stage" ] || exit 78
mkdir -m 700 "$serial_stage" || exit 78
serial_stage_status=0
if mount -o bind /usr/lib/frl "$serial_stage"; then
    if mount -o remount,bind,ro "$serial_stage"; then
        chroot /newroot /usr/bin/python3 /mnt/frl-initramfs-stage/stage-serial-root-update.py || serial_stage_status=$?
    else
        serial_stage_status=78
    fi
    umount "$serial_stage" || { echo 'serial stage bind unmount failed'; exit 78; }
else
    serial_stage_status=78
fi
rmdir "$serial_stage" || exit 78
[ "$serial_stage_status" -eq 0 ] || { echo 'serial stage update refused'; exit 78; }
unset serial_stage serial_stage_status

echo 'FRL_V15_BOOTSTRAP_ROOT_GATE_OK (not GUI/network readiness)'
exit 0
