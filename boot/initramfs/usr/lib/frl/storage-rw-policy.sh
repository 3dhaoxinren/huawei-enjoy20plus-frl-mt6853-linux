# Only called during early initramfs bootstrap, before any loop mapping.
frl_prepare_storage_rw() {
    [ "$1" = /dev/frl-userdata ] && [ "$2" = /run/frl/storage ] && [ "$3" = /run/frl/storage/frl-linux/rootfs-v3.ext4 ] || return 78
    awk -v p="$2" '$2==p && $3=="f2fs" && $4 ~ /(^|,)ro(,|$)/ {ok++} END{exit ok!=1}' /proc/mounts || return 78
    for backing in /sys/class/block/loop*/loop/backing_file; do
        [ -r "$backing" ] || continue
        [ -z "$(cat "$backing")" ] || { echo 'RW_POLICY_REFUSED: existing loop mapping'; return 78; }
    done
    # The successful ordinary unmount proves no probe/loop still pins this view.
    umount "$2" || return 78
    if ! mount -t f2fs -o rw,background_gc=off,nodiscard "$1" "$2"; then
        echo 'RW_POLICY_FAILED: fresh normal recovery mount failed; no fallback write'
        return 78
    fi
    awk -v p="$2" '$2==p && $3=="f2fs" && $4 ~ /(^|,)rw(,|$)/ && $4 !~ /disable_roll_forward/ {ok++} END{exit ok!=1}' /proc/mounts || return 78
    echo 'FRESH_F2FS_RW_RECOVERY_MOUNT_VERIFIED'
}
frl_validate_rootfs_file() {
    /usr/lib/frl/rootfs-check "$1" "$2" "$3"
}
