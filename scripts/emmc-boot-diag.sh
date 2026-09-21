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
# Nothing is written to the eMMC: the U-Boot env, misc and GPT are only read,
# and CE_FLASH / CE_STORAGE are mounted read-only under /tmp.
E=/dev/mmcblk0
BACKUP=/storage/emmc-backup

node() {  # GPT partition name -> block device
    for d in "/dev/$1" "$(blkid -t PARTLABEL="$1" -o device 2>/dev/null | head -1)"; do
        [ -b "$d" ] && { echo "$d"; return; }
    done
}
envvars() {
    tr '\0' '\n' | grep -E '^(bootcmd|bootfrom[a-z]*|cfgload[a-z]*|ce_on_emmc|bootloader_version|upgrade_step|reboot_mode|bootdelay)='
}
envcrc() {  # U-Boot reads only bank 0 (first 64 KiB); a bad CRC means it falls back to its built-in env
    python3 -c 'import sys,zlib,struct; d=open(sys.argv[1],"rb").read(65536); print("bank0 CRC", "OK" if struct.unpack("<I",d[:4])[0]==zlib.crc32(d[4:]) else "BAD -> U-Boot is using its built-in default env")' "$1"
}

echo "== running"
grep PRETTY_NAME /etc/os-release
tr ' ' '\n' </proc/cmdline | grep -E '^(androidboot\.bootloader|boot|disk)='

echo "== usb"
lsusb

echo "== gpt"
parted -sm $E unit MiB print 2>/dev/null | tail -4

ENV=$(node env)
echo "== env live ($ENV)"
envcrc "$ENV"
dd if="$ENV" bs=64k count=1 2>/dev/null | envvars
if [ -f "$BACKUP/env_backup.bin" ]; then
    echo "== env before install ($BACKUP/env_backup.bin), diff vs live"
    envcrc "$BACKUP/env_backup.bin"
    dd if="$BACKUP/env_backup.bin" bs=64k count=1 2>/dev/null | envvars >/tmp/env.old
    dd if="$ENV" bs=64k count=1 2>/dev/null | envvars >/tmp/env.new
    diff /tmp/env.old /tmp/env.new && echo "(identical)"
    rm -f /tmp/env.old /tmp/env.new
fi

echo "== misc/BCB"
dd if="$(node misc)" bs=64 count=1 2>/dev/null | xxd | head -4

echo "== CE_FLASH"
M=/tmp/ce_flash_ro; mkdir -p $M
if mount -o ro "$(node CE_FLASH)" $M; then
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
S=$(node CE_STORAGE)
tune2fs -l "$S" 2>/dev/null | grep -E 'Last mounted on|Mount count|Filesystem state'
M=/tmp/ce_storage_ro; mkdir -p $M
if [ -f "$BACKUP/partition_layout.txt" ] && mount -o ro "$S" $M; then
    echo "files written after the install (non-empty = an eMMC boot reached userspace):"
    find $M -xdev -type f -newer "$BACKUP/partition_layout.txt" 2>/dev/null | head -15
    umount $M
fi
rmdir $M 2>/dev/null
