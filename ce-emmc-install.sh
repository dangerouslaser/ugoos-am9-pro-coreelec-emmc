#!/bin/bash
# ce-emmc-install.sh — CoreELEC eMMC installer for Ugoos AM9 Pro
#
# Workaround until ceemmc adds support for s6_s905x5_ugoos_am9_pro.
# Must be run from CoreELEC booted off an SD card.
#
# What this does:
#   1. Backs up partition layout, rsv (p28), env (p2), and bootloader_a (p7) to /storage
#   2. Keeps super (p27) — Android system images intact for potential restore
#   3. Deletes rsv (p28) and userdata (p29)
#   4. Creates CE_FLASH (512 MB FAT32) at p28 and CE_STORAGE (remaining space ext4) at p29
#   5. Copies all boot files from the SD card's /flash to CE_FLASH
#   6. Rebuilds cfgload to use disk=LABEL=CE_STORAGE (replaces the dual-boot
#      ceemmc disk=FOLDER=/dev/CE_STORAGE path with standalone label resolution)
#   7. Optionally migrates your current /storage to CE_STORAGE

set -euo pipefail

# ── Flags ─────────────────────────────────────────────────────────────────────

DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help)
            echo "Usage: ce-emmc-install.sh [--dry-run] [--help]"
            echo ""
            echo "  --dry-run  Show all commands without executing destructive operations."
            echo "             Non-destructive reads (parted print, blkid, dd reads) still run."
            echo "  --help     Show this help and exit."
            exit 0
            ;;
    esac
done

# ── Constants ─────────────────────────────────────────────────────────────────

EMMC="/dev/mmcblk0"
SD_FLASH="/flash"
MNT_FLASH="/var/ce_flash"
MNT_STORAGE="/var/ce_storage"
BACKUP_DIR="/storage"

# ── Cleanup trap ──────────────────────────────────────────────────────────────

cleanup() {
    mountpoint -q "$MNT_FLASH"   2>/dev/null && umount "$MNT_FLASH"   || true
    mountpoint -q "$MNT_STORAGE" 2>/dev/null && umount "$MNT_STORAGE" || true
}
trap cleanup EXIT

# ── Colors and output helpers ─────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

log()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
die()    { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}--- $* ---${NC}"; }

# ── Dry-run wrapper ───────────────────────────────────────────────────────────

run() {
    if $DRY_RUN; then
        echo -e "${YELLOW}[DRY-RUN]${NC} $*"
    else
        "$@"
    fi
}

$DRY_RUN && warn "DRY-RUN mode — no changes will be made"

# ── TUI detection ─────────────────────────────────────────────────────────────

USE_TUI=false
if command -v whiptail >/dev/null 2>&1 && [[ -t 0 && -t 1 ]]; then
    _cols=$(tput cols 2>/dev/null || echo 0)
    _rows=$(tput lines 2>/dev/null || echo 0)
    [[ $_cols -ge 60 && $_rows -ge 20 ]] && USE_TUI=true
fi

# tui_confirm_destructive — main confirm dialog (requires typing YES in text mode)
tui_confirm_destructive() {
    local title="$1" msg="$2"
    if $USE_TUI; then
        whiptail --title "$title" --yesno "$msg" 22 72 3>&1 1>&2 2>&3
    else
        echo -e "$msg"
        echo ""
        read -rp "  Type YES to proceed: " _c
        echo ""
        [[ "$_c" == "YES" ]]
    fi
}

# tui_yesno — secondary prompt (default No)
tui_yesno() {
    local title="$1" msg="$2"
    if $USE_TUI; then
        whiptail --title "$title" --defaultno --yesno "$msg" 15 72 3>&1 1>&2 2>&3
    else
        echo -e "$msg"
        read -rp "  Migrate? [y/N]: " _a
        _a="${_a,,}"
        [[ "$_a" == "y" || "$_a" == "yes" ]]
    fi
}

# ── eMMC node helpers ─────────────────────────────────────────────────────────

# Create /dev nodes for eMMC partitions the kernel already knows about,
# reading major:minor from sysfs rather than assuming sequential numbering.
make_emmc_nodes() {
    for sysfs_dev in /sys/block/mmcblk0/mmcblk0p*/dev; do
        [[ -f "$sysfs_dev" ]] || continue
        local partname
        partname=$(basename "$(dirname "$sysfs_dev")")
        local devnum maj min
        read -r devnum < "$sysfs_dev"
        maj="${devnum%%:*}"
        min="${devnum##*:}"
        mknod "/dev/${partname}" b "$maj" "$min" 2>/dev/null || true
    done
}

# After repartitioning, ask the kernel to re-read the partition table and
# create /dev nodes for the new partitions using sysfs-reported major:minor.
reread_and_make_nodes() {
    local parts=("$@")
    partprobe "$EMMC" 2>/dev/null || true

    for part in "${parts[@]}"; do
        local sysfs_dev="/sys/block/mmcblk0/mmcblk0p${part}/dev"
        local waited=0
        while [[ ! -f "$sysfs_dev" ]] && (( waited < 10 )); do
            sleep 1
            (( waited++ )) || true
        done
        if [[ -f "$sysfs_dev" ]]; then
            local devnum maj min
            read -r devnum < "$sysfs_dev"
            maj="${devnum%%:*}"
            min="${devnum##*:}"
            rm -f "/dev/mmcblk0p${part}" 2>/dev/null || true
            mknod "/dev/mmcblk0p${part}" b "$maj" "$min"
            log "Device node: /dev/mmcblk0p${part} (${maj}:${min})"
        else
            die "Kernel did not register mmcblk0p${part} — try rebooting and rerunning the script"
        fi
    done
}

# ── Size helpers ──────────────────────────────────────────────────────────────

part_size_mib() {
    parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: -v p="$1" '$1==p{gsub(/MiB/,"",$4); printf "%.0f",$4}'
}

human_mib() { awk -v m="$1" 'BEGIN{if(m>=1024)printf "%.1f GB",m/1024; else printf "%d MB",m}'; }

# ── Preflight ─────────────────────────────────────────────────────────────────

header "Preflight checks"

[[ "$(id -u)" == "0" ]] || die "Must be run as root"

# Board check — verify the AM9 Pro DTB exists and is the active DTB
DTB_DEVICE="${SD_FLASH}/device_trees/s6_s905x5_ugoos_am9_pro.dtb"
DTB_ACTIVE="${SD_FLASH}/dtb.img"

[[ -f "$DTB_DEVICE" ]] || \
    die "AM9 Pro DTB not found at $DTB_DEVICE — is this a Ugoos AM9 Pro?"

HASH_DEVICE=$(md5sum "$DTB_DEVICE" | awk '{print $1}')
HASH_ACTIVE=$(md5sum "$DTB_ACTIVE"  | awk '{print $1}')
[[ "$HASH_DEVICE" == "$HASH_ACTIVE" ]] || \
    die "Active dtb.img doesn't match the AM9 Pro DTB. Check your DTB selection in config.ini."

log "Board: Ugoos AM9 Pro (s6_s905x5_ugoos_am9_pro)"

# Must be booting from SD card
FLASH_SOURCE=$(awk '$2 == "/flash" {print $1}' /proc/mounts 2>/dev/null || true)
[[ "$FLASH_SOURCE" == *"mmcblk1"* ]] || \
    die "Not booting from SD card — /flash is on '${FLASH_SOURCE:-unknown}'. Insert SD card and reboot."

log "Boot source: SD card ($FLASH_SOURCE)"

[[ -b "$EMMC" ]] || die "eMMC not found at $EMMC"
log "eMMC: $EMMC present"

for tool in parted mkfs.fat mkfs.ext4 rsync dd blkid mknod partprobe mountpoint; do
    command -v "$tool" >/dev/null 2>&1 || die "Required tool not found: $tool"
done
log "Required tools: all present"

# Create device nodes from sysfs for partitions the kernel already knows about
make_emmc_nodes

# Check for an existing CoreELEC install before inspecting the Android layout
if blkid "${EMMC}p28" 2>/dev/null | grep -q "CE_FLASH"; then
    die "CoreELEC is already installed (CE_FLASH on p28). Run ce-emmc-restore.sh to restore Android first."
fi
# Also catch old-style install (CE_FLASH was on p27 in v1)
if blkid "${EMMC}p27" 2>/dev/null | grep -q "CE_FLASH"; then
    die "Old-style CoreELEC install detected on p27. Restore Android via USB Burning Tool first."
fi

# Check for expected Android partition layout
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^27:" || \
    die "Partition 27 (super) not found — unexpected layout. Has this already been modified?"
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^28:" || \
    die "Partition 28 (rsv) not found — unexpected layout."
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^29:" || \
    die "Partition 29 (userdata) not found — unexpected layout."

log "Partition layout: 29-partition Android layout confirmed"

# ── Read dynamic partition sizes ──────────────────────────────────────────────

SUPER_SIZE_MIB=$(part_size_mib 27)
RSV_SIZE_MIB=$(part_size_mib 28)
USERDATA_SIZE_MIB=$(part_size_mib 29)
CE_STORAGE_SIZE_MIB=$(( RSV_SIZE_MIB + USERDATA_SIZE_MIB - 512 ))

SUPER_HUMAN=$(human_mib "$SUPER_SIZE_MIB")
RSV_HUMAN=$(human_mib "$RSV_SIZE_MIB")
USERDATA_HUMAN=$(human_mib "$USERDATA_SIZE_MIB")
CE_STORAGE_HUMAN=$(human_mib "$CE_STORAGE_SIZE_MIB")

# ── rsv content check ─────────────────────────────────────────────────────────
#
# Read rsv before the confirm screen so the result can be included in the
# confirm message. The backup happens after the user confirms.

RSV_HAS_DATA=$(dd if="${EMMC}p28" bs=512 count=1 2>/dev/null | tr -d '\0' | wc -c)

# ── Confirm ───────────────────────────────────────────────────────────────────

RSV_NOTE=""
if [[ "$RSV_HAS_DATA" -gt 0 ]]; then
    RSV_NOTE="  [contains data — will be backed up]"
fi

CONFIRM_MSG="\
CoreELEC eMMC Installer — Ugoos AM9 Pro

  KEEP    p27  super       ${SUPER_HUMAN}  (Android system — untouched)

  DELETE  p28  rsv         ${RSV_HUMAN}${RSV_NOTE}
  DELETE  p29  userdata    ${USERDATA_HUMAN}  (encrypted — unrecoverable)

  CREATE  p28  CE_FLASH    512 MB  FAT32  (CoreELEC boot)
  CREATE  p29  CE_STORAGE  ${CE_STORAGE_HUMAN}  ext4   (CoreELEC storage)

Partitions p1–p26 and super (p27) are NOT touched.
boot0/boot1 are hardware write-protected and safe.

Android restore requires Amlogic USB Burning Tool on Windows via the
USB-C OTG port using the official Ugoos factory image."

tui_confirm_destructive "CoreELEC eMMC Installer — Ugoos AM9 Pro" "$CONFIRM_MSG" \
    || { echo "Aborted."; exit 0; }

# ── Backups ───────────────────────────────────────────────────────────────────

header "Backing up critical partitions"

# Save the current partition layout — required by ce-emmc-restore.sh to
# reconstruct the original p28/p29 boundaries exactly.
if ! $DRY_RUN; then
    parted -sm "$EMMC" unit B print > "${BACKUP_DIR}/partition_layout.txt"
    log "Partition layout → ${BACKUP_DIR}/partition_layout.txt"
else
    echo -e "${YELLOW}[DRY-RUN]${NC} parted -sm $EMMC unit B print > ${BACKUP_DIR}/partition_layout.txt"
fi

run dd if="${EMMC}p28" of="${BACKUP_DIR}/rsv_backup.bin" bs=1M status=none
log "rsv (p28) → ${BACKUP_DIR}/rsv_backup.bin"

run dd if="${EMMC}p2" of="${BACKUP_DIR}/env_backup.bin" bs=1M status=none
log "env (p2) → ${BACKUP_DIR}/env_backup.bin"

run dd if="${EMMC}p7" of="${BACKUP_DIR}/bootloader_a_backup.bin" bs=1M status=none
log "bootloader_a (p7) → ${BACKUP_DIR}/bootloader_a_backup.bin"

# ── Repartition ───────────────────────────────────────────────────────────────

header "Repartitioning eMMC"

# Find where p28 (rsv) starts — CE_FLASH will occupy the same starting position
RSV_START_B=$(parted -sm "$EMMC" unit B print 2>/dev/null \
    | awk -F: '/^28:/{gsub(/B/,""); print $2}')
[[ -n "$RSV_START_B" ]] || die "Could not determine start position of partition 28"

# Work in MiB — Android aligns to MiB boundaries
RSV_START_MIB=$((RSV_START_B / 1024 / 1024))
CE_FLASH_END_MIB=$((RSV_START_MIB + 512))

log "Deleting partitions 28 (rsv) and 29 (userdata)..."
run parted -s "$EMMC" rm 29 rm 28

log "Creating CE_FLASH (${RSV_START_MIB}MiB – ${CE_FLASH_END_MIB}MiB)..."
run parted -s "$EMMC" mkpart CE_FLASH fat32 "${RSV_START_MIB}MiB" "${CE_FLASH_END_MIB}MiB"

log "Creating CE_STORAGE (${CE_FLASH_END_MIB}MiB – 100%)..."
run parted -s "$EMMC" mkpart CE_STORAGE ext4 "${CE_FLASH_END_MIB}MiB" "100%"

# Ask the kernel to re-read the partition table, then create device nodes
# using sysfs-reported major:minor numbers (not assumed sequential values)
if ! $DRY_RUN; then
    reread_and_make_nodes 28 29
fi

# ── Format ────────────────────────────────────────────────────────────────────

header "Formatting partitions"

log "Formatting CE_FLASH as FAT32..."
run mkfs.fat -F 32 -n CE_FLASH "${EMMC}p28"

log "Formatting CE_STORAGE as ext4..."
run mkfs.ext4 -q -L CE_STORAGE "${EMMC}p29"

# ── Install boot files ────────────────────────────────────────────────────────

header "Installing boot files"

run mkdir -p "$MNT_FLASH"
run mount "${EMMC}p28" "$MNT_FLASH"

log "Copying files from ${SD_FLASH}..."
run cp -a "${SD_FLASH}/." "${MNT_FLASH}/"
run rm -f "${MNT_FLASH}/fs-resize.log"

# Rebuild cfgload for a standalone install.
#
# CoreELEC's stock cfgload contains a dual-boot path used by ceemmc: when
# ce_on_emmc=yes it sets disk=FOLDER=/dev/CE_STORAGE, which expects
# CoreELEC storage to live as a coreelec_storage/ subfolder inside
# Android's userdata. For a standalone install we want
# disk=LABEL=CE_STORAGE — the partition root, resolved by label.
#
# cfgload is a U-Boot mkimage script container with CRC32 fields in its
# header. Editing it with sed corrupts the CRC and U-Boot silently
# rejects the result, so we rebuild the container with correct CRCs
# after the substitution. mkimage is not installed on CoreELEC, so the
# rebuild is done in Python (the script-image format is short enough to
# pack/unpack with `struct` and `zlib.crc32`).
log "Rebuilding cfgload for standalone install..."
if $DRY_RUN; then
    echo -e "${YELLOW}[DRY-RUN]${NC} rebuild ${MNT_FLASH}/cfgload (FOLDER=/dev/CE_STORAGE → LABEL=CE_STORAGE)"
else
    python3 - "${MNT_FLASH}/cfgload" << 'PYEOF'
import struct
import sys
import time
import zlib

MKIMAGE_MAGIC = 0x27051956
IH_ARCH_ARM64 = 22
IH_TYPE_SCRIPT = 6
HDR_FMT = ">IIIIIIIBBBB"  # 7×u32 + 4×u8 = 32 bytes
HDR_SIZE = 64             # legacy_img_hdr including 32-byte name field
SUBHDR_FMT = ">II"        # [script_size][0_terminator]

OLD = b"disk=FOLDER=/dev/CE_STORAGE"
NEW = b"disk=LABEL=CE_STORAGE"

path = sys.argv[1]

def die(msg):
    print(f"rebuild_cfgload: {msg}", file=sys.stderr)
    sys.exit(1)

with open(path, "rb") as f:
    data = f.read()

if len(data) < HDR_SIZE + 8:
    die(f"{path}: too small to be a mkimage script")

magic, _hcrc, _time, dsize, load, ep, _dcrc, osv, arch, typ, comp = \
    struct.unpack(HDR_FMT, data[:32])
name = data[32:64]

if magic != MKIMAGE_MAGIC:
    die(f"{path}: bad magic 0x{magic:08x}")
if typ != IH_TYPE_SCRIPT:
    die(f"{path}: not a script image (type={typ})")
if arch != IH_ARCH_ARM64:
    die(f"{path}: not an arm64 image (arch={arch})")
if len(data) < HDR_SIZE + dsize:
    die(f"{path}: truncated payload")

payload = data[HDR_SIZE:HDR_SIZE + dsize]
script_size, terminator = struct.unpack(SUBHDR_FMT, payload[:8])
if terminator != 0:
    die(f"{path}: expected single-script terminator, got 0x{terminator:08x}")
if len(payload) < 8 + script_size:
    die(f"{path}: script body truncated")

script = payload[8:8 + script_size]
if OLD not in script:
    print(f"rebuild_cfgload: {OLD.decode()!r} not present — already patched",
          file=sys.stderr)
    sys.exit(0)

new_script = script.replace(OLD, NEW)
new_size = len(new_script)
new_payload = struct.pack(SUBHDR_FMT, new_size, 0) + new_script
new_dsize = len(new_payload)
new_dcrc = zlib.crc32(new_payload)
now = int(time.time())

def build_header(hcrc):
    return struct.pack(
        HDR_FMT,
        MKIMAGE_MAGIC, hcrc, now, new_dsize,
        load, ep, new_dcrc, osv, arch, typ, comp,
    ) + name

hcrc = zlib.crc32(build_header(0))
with open(path, "wb") as f:
    f.write(build_header(hcrc) + new_payload)

print(f"rebuild_cfgload: {path}: {len(data)} → {HDR_SIZE + new_dsize} bytes "
      f"(script {script_size} → {new_size})", file=sys.stderr)
PYEOF
fi

run umount "$MNT_FLASH"
log "CE_FLASH ready"

# ── Optional storage migration ─────────────────────────────────────────────────

MIGRATE_MSG="  The eMMC install is ready. CE_STORAGE is currently empty —
  first eMMC boot will initialize it as a fresh install.

  You can optionally migrate your current /storage (settings,
  addons, media metadata) to CE_STORAGE now."

if tui_yesno "Migrate /storage to CE_STORAGE?" "$MIGRATE_MSG"; then
    header "Migrating /storage to CE_STORAGE"

    run mkdir -p "$MNT_STORAGE"
    run mount -t ext4 -o rw,noatime "${EMMC}p29" "$MNT_STORAGE"

    # Check available space before migrating
    STORAGE_USED=$(du -sb /storage 2>/dev/null | awk '{print $1}')
    CE_FREE=$(df -B1 "$MNT_STORAGE" 2>/dev/null | awk 'NR==2{print $4}')
    if (( STORAGE_USED > CE_FREE )); then
        warn "Not enough space: /storage uses $(( STORAGE_USED/1024/1024 )) MB, CE_STORAGE has $(( CE_FREE/1024/1024 )) MB free"
        run umount "$MNT_STORAGE"
        warn "Migration skipped — CE_STORAGE will be initialized fresh on first eMMC boot"
    else
        log "Rsyncing /storage → CE_STORAGE (this may take a few minutes)..."
        run rsync -ax --info=progress2 /storage/ "$MNT_STORAGE/"

        run umount "$MNT_STORAGE"
        log "Migration complete"
    fi
fi

# ── Post-install partition layout ─────────────────────────────────────────────

header "Final eMMC partition layout"
if ! $DRY_RUN; then
    parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: 'NR>2 && /^[0-9]/{gsub(/MiB/,"",$4); printf "  p%-3s %-20s %5.0f MiB\n",$1,$6,$4}'
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Installation complete!${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo ""
echo "  Remove the SD card and reboot. The device will boot"
echo "  CoreELEC from internal eMMC automatically."
echo ""
echo "  On first eMMC boot, SSH host keys are regenerated."
echo "  Clear your old entry before reconnecting:"
echo "    ssh-keygen -R <device-ip>"
echo ""
echo "  Backups saved to ${BACKUP_DIR}:"
echo "    partition_layout.txt  (needed by ce-emmc-restore.sh)"
echo "    rsv_backup.bin"
echo "    env_backup.bin"
echo "    bootloader_a_backup.bin"
echo ""
$DRY_RUN && warn "DRY-RUN complete — no changes were made"
