#!/bin/sh
# emmc-boot-diag.sh — read-only diagnostics for "CoreELEC boots from USB/SD
# but not from the eMMC install" (issue #8).
#
# Run it from CoreELEC booted off the USB stick / SD card that did the install:
#
#   curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/scripts/emmc-boot-diag.sh \
#       | sh > /storage/emmc-boot-diag.txt 2>&1
#
# then attach /storage/emmc-boot-diag.txt to the issue.
#
# Nothing is written to the eMMC: the U-Boot env, misc, reserved and GPT are
# only read, and CE_FLASH / CE_STORAGE are mounted read-only under /tmp.
E=/dev/mmcblk0
BACKUP=/storage/emmc-backup

# Block device for eMMC partition number N, found through sysfs so it works
# whatever the kernel named it (mmcblk0pN, or the partition name). Creates a
# node under /tmp if /dev has none. Prints nothing if the kernel has no pN.
pdev() {
    for d in /sys/block/mmcblk0/*/; do
        [ "$(cat "${d}partition" 2>/dev/null)" = "$1" ] || continue
        n=$(basename "$d")
        [ -b "/dev/$n" ] && { echo "/dev/$n"; return; }
        mm=$(cat "${d}dev")
        mknod "/tmp/diag_$n" b "${mm%%:*}" "${mm##*:}" 2>/dev/null
        echo "/tmp/diag_$n"; return
    done
}
ksectors() { for d in /sys/block/mmcblk0/*/; do [ "$(cat "${d}partition" 2>/dev/null)" = "$1" ] && cat "${d}size"; done; }
kname()    { for d in /sys/block/mmcblk0/*/; do [ "$(cat "${d}partition" 2>/dev/null)" = "$1" ] && basename "$d"; done; }
gsectors() { parted -sm $E unit s print 2>/dev/null | awk -F: -v p="$1" '$1==p{gsub(/s/,"",$4); print $4}'; }
gname()    { parted -sm $E unit s print 2>/dev/null | awk -F: -v p="$1" '$1==p{print $6}'; }

envvars() {
    tr '\0' '\n' | grep -E '^(bootcmd|bootfrom[a-z]*|cfgload[a-z]*|ce_on_emmc|bootloader_version|upgrade_step|reboot_mode|bootdelay)='
}
envcrc() {  # U-Boot reads only bank 0 (first 64 KiB); a bad CRC means it falls back to its built-in env
    python3 -c 'import sys,zlib,struct; d=open(sys.argv[1],"rb").read(65536); print("bank0 CRC", "OK" if struct.unpack("<I",d[:4])[0]==zlib.crc32(d[4:]) else "BAD -> U-Boot is using its built-in default env")' "$1"
}
mptinfo() {  # Amlogic MPT at offset 0 of reserved: the kernel prefers it over the GPT
    python3 - "$1" <<'PYEOF'
import struct, sys
d = open(sys.argv[1], 'rb').read(1304)
if d[:4] != b'MPT\0':
    print('none (the kernel uses the GPT)')
    sys.exit(0)
n, csum = struct.unpack('<iI', d[16:24])
if not 0 < n <= 32:
    print(f'PRESENT, entry count {n} out of range (the kernel ignores it)')
    sys.exit(0)
names = [d[24 + 40 * i:40 + 40 * i].split(b'\0')[0].decode('ascii', 'replace') for i in range(n)]
ok = (sum(struct.unpack('<10I', d[24:64])) * n) & 0xffffffff == csum
print(f"PRESENT, {n} entries, {'valid - the kernel uses it instead of the GPT' if ok else 'bad checksum (the kernel ignores it)'}")
print('  ' + ' '.join(f'p{i + 1}={x}' for i, x in enumerate(names) if i + 1 >= 27))
PYEOF
}

echo "== running"
grep PRETTY_NAME /etc/os-release
tr ' ' '\n' </proc/cmdline | grep -E '^(androidboot\.bootloader|boot|disk)='

echo "== usb"
lsusb

echo "== gpt"
parted -sm $E unit MiB print 2>/dev/null | tail -4

echo "== kernel partition view vs GPT"
for p in 28 29; do
    k=$(ksectors $p); g=$(gsectors $p)
    if [ -n "$k" ] && [ "$k" = "$g" ]; then m="match"; else m="MISMATCH"; fi
    echo "p$p  kernel: $(kname $p) $(( ${k:-0} / 2048 )) MiB   GPT: $(gname $p) $(( ${g:-0} / 2048 )) MiB   $m"
done
[ -e /proc/inand ] && echo "/proc/inand present -> the kernel took its partitions from an Amlogic MPT"
RES=$(pdev 1)
echo "MPT in reserved ($RES): $(mptinfo "$RES")"
if [ -f "$BACKUP/reserved_backup.bin" ]; then
    echo "MPT in reserved_backup.bin: $(mptinfo "$BACKUP/reserved_backup.bin")"
fi

ENV=$(pdev 2)
echo "== env live ($ENV)"
envcrc "$ENV"
dd if="$ENV" bs=64k count=1 2>/dev/null | envvars
if [ -f "$BACKUP/env_backup.bin" ]; then
    echo "== env before install ($BACKUP/env_backup.bin) vs live"
    envcrc "$BACKUP/env_backup.bin"
    dd if="$BACKUP/env_backup.bin" bs=64k count=1 2>/dev/null | envvars >/tmp/env.old
    dd if="$ENV" bs=64k count=1 2>/dev/null | envvars >/tmp/env.new
    # busybox has no diff
    grep -vxFf /tmp/env.new /tmp/env.old | sed 's/^/- /'
    grep -vxFf /tmp/env.old /tmp/env.new | sed 's/^/+ /'
    [ "$(md5sum </tmp/env.old)" = "$(md5sum </tmp/env.new)" ] && echo "(identical)"
    rm -f /tmp/env.old /tmp/env.new
fi

echo "== misc/BCB"
dd if="$(pdev 11)" bs=64 count=1 2>/dev/null | xxd | head -4

# Only mount a partition when the kernel's view of it matches the GPT;
# otherwise the device node covers the wrong range of the eMMC.
usable() { [ -n "$(ksectors "$1")" ] && [ "$(ksectors "$1")" = "$(gsectors "$1")" ]; }

echo "== CE_FLASH"
M=/tmp/ce_flash_ro; mkdir -p $M
if ! usable 28; then
    echo "not checked: the kernel's p28 is not the GPT's CE_FLASH (see partition view above)"
elif mount -o ro "$(pdev 28)" $M; then
    ls -la $M
    grep -n '^coreelec=' $M/config.ini
    echo "mount-storage.sh: $(cat $M/mount-storage.sh 2>/dev/null || echo MISSING)"
    for f in kernel.img dtb.img cfgload; do
        echo "$f  eMMC $(md5sum <$M/$f | cut -c1-12)  USB $(md5sum </flash/$f | cut -c1-12)"
    done
    python3 -c 'import sys,zlib,struct; d=open(sys.argv[1],"rb").read(); h=bytearray(d[:64]); c=struct.unpack(">I",h[4:8])[0]; h[4:8]=bytes(4); n=struct.unpack(">I",d[12:16])[0]; print("cfgload hcrc", zlib.crc32(h)==c, "dcrc", zlib.crc32(d[64:64+n])==struct.unpack(">I",d[24:28])[0])' $M/cfgload
    tail -c +73 $M/cfgload | grep -o 'disk=[^ "]*'
    umount $M
fi
rmdir $M 2>/dev/null

echo "== CE_STORAGE"
if ! usable 29; then
    echo "not checked: the kernel's p29 is not the GPT's CE_STORAGE (see partition view above)"
else
    S=$(pdev 29)
    tune2fs -l "$S" 2>/dev/null | grep -E 'Last mounted on|Mount count|Filesystem state'
    M=/tmp/ce_storage_ro; mkdir -p $M
    if [ -f "$BACKUP/partition_layout.txt" ] && mount -o ro "$S" $M; then
        echo "files written after the install (non-empty = an eMMC boot reached userspace):"
        find $M -xdev -type f -newer "$BACKUP/partition_layout.txt" 2>/dev/null | head -15
        umount $M
    fi
    rmdir $M 2>/dev/null
fi
rm -f /tmp/diag_* 2>/dev/null
