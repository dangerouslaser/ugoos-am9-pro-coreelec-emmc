#!/bin/bash
# ce-emmc-restore.sh — Restore Android partition layout after CoreELEC eMMC install
# Must be run from CoreELEC booted from SD card.
# Requires backup files created by ce-emmc-install.sh in /storage.

set -euo pipefail

# ── Flags ─────────────────────────────────────────────────────────────────────

DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help)
            echo "Usage: ce-emmc-restore.sh [--dry-run] [--help]"
            echo ""
            echo "  --dry-run  Show all commands without executing destructive operations."
            echo "             Non-destructive reads (parted print, blkid) still run."
            echo "  --help     Show this help and exit."
            exit 0
            ;;
    esac
done

# ── Constants ─────────────────────────────────────────────────────────────────

EMMC="/dev/mmcblk0"
BACKUP_DIR="/storage"
MNT_FLASH="/var/ce_flash"
MNT_STORAGE="/var/ce_storage"

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

# tui_msg — informational message box (no response required)
tui_msg() {
    local title="$1" msg="$2"
    if $USE_TUI; then
        whiptail --title "$title" --msgbox "$msg" 15 72
    else
        echo -e "$msg"
    fi
}

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

# ── Preflight ─────────────────────────────────────────────────────────────────

header "Preflight checks"

[[ "$(id -u)" == "0" ]] || die "Must be run as root"

# Must be booting from SD card
FLASH_SOURCE=$(awk '$2 == "/flash" {print $1}' /proc/mounts 2>/dev/null || true)
[[ "$FLASH_SOURCE" == *"mmcblk1"* ]] || \
    die "Not booting from SD card — /flash is on '${FLASH_SOURCE:-unknown}'. Insert SD card and reboot."

log "Boot source: SD card ($FLASH_SOURCE)"

[[ -b "$EMMC" ]] || die "eMMC not found at $EMMC"
log "eMMC: $EMMC present"

for tool in parted dd blkid mknod partprobe mountpoint; do
    command -v "$tool" >/dev/null 2>&1 || die "Required tool not found: $tool"
done
log "Required tools: all present"

# Create device nodes from sysfs for partitions the kernel already knows about
make_emmc_nodes

# CE_FLASH must exist on p28 — confirms this is a CE install to restore from
if ! blkid "${EMMC}p28" 2>/dev/null | grep -q "CE_FLASH"; then
    die "CE_FLASH not found on p28 — is CoreELEC installed on eMMC?"
fi
log "CE_FLASH confirmed on p28"

# All backup files must exist before proceeding
for f in partition_layout.txt rsv_backup.bin env_backup.bin bootloader_a_backup.bin; do
    [[ -f "${BACKUP_DIR}/${f}" ]] || \
        die "Backup file not found: ${BACKUP_DIR}/${f} — run ce-emmc-install.sh first"
done
log "Backup files: all present in ${BACKUP_DIR}"

# ── Parse original partition layout ───────────────────────────────────────────

header "Reading original partition layout"

P28_START_B=$(awk -F: '/^28:/{gsub(/B/,"",$2); print $2}' "${BACKUP_DIR}/partition_layout.txt")
P28_END_B=$(awk   -F: '/^28:/{gsub(/B/,"",$3); print $3}' "${BACKUP_DIR}/partition_layout.txt")
P28_NAME=$(awk    -F: '/^28:/{print $6}'                   "${BACKUP_DIR}/partition_layout.txt")
P29_START_B=$(awk -F: '/^29:/{gsub(/B/,"",$2); print $2}' "${BACKUP_DIR}/partition_layout.txt")
P29_END_B=$(awk   -F: '/^29:/{gsub(/B/,"",$3); print $3}' "${BACKUP_DIR}/partition_layout.txt")
P29_NAME=$(awk    -F: '/^29:/{print $6}'                   "${BACKUP_DIR}/partition_layout.txt")

[[ -n "$P28_START_B" ]] || die "Could not parse p28 start offset from partition_layout.txt"
[[ -n "$P28_END_B"   ]] || die "Could not parse p28 end offset from partition_layout.txt"
[[ -n "$P28_NAME"    ]] || die "Could not parse p28 name from partition_layout.txt"
[[ -n "$P29_START_B" ]] || die "Could not parse p29 start offset from partition_layout.txt"
[[ -n "$P29_END_B"   ]] || die "Could not parse p29 end offset from partition_layout.txt"
[[ -n "$P29_NAME"    ]] || die "Could not parse p29 name from partition_layout.txt"

log "p28 original: ${P28_NAME} (${P28_START_B}B – ${P28_END_B}B)"
log "p29 original: ${P29_NAME} (${P29_START_B}B – ${P29_END_B}B)"

# ── Confirm ───────────────────────────────────────────────────────────────────

CONFIRM_MSG="\
This will restore the original Android partition layout on ${EMMC}:

  DELETE  p28  CE_FLASH    (CoreELEC boot partition)
  DELETE  p29  CE_STORAGE  (CoreELEC storage — ALL DATA WILL BE LOST)

  RESTORE p28  ${P28_NAME}   (from rsv_backup.bin)
  RESTORE p29  ${P29_NAME}   (empty — Android will reinitialize on first boot)
  RESTORE      env            (from env_backup.bin)
  RESTORE      bootloader_a   (from bootloader_a_backup.bin)

  super (p27) is untouched — Android system images are intact.
  Partitions p1–p26 are NOT touched.

After restore, boot Android via USB Burning Tool or by removing the SD card.
WARNING: All CoreELEC data on CE_STORAGE will be permanently lost."

tui_confirm_destructive "Restore Android — Ugoos AM9 Pro" "$CONFIRM_MSG" \
    || { echo "Aborted."; exit 0; }

# ── Restore ───────────────────────────────────────────────────────────────────

header "Removing CoreELEC partitions"
run parted -s "$EMMC" rm 29 rm 28
log "CE_STORAGE (p29) and CE_FLASH (p28) removed"

header "Restoring original partitions"
run parted -s "$EMMC" mkpart "$P28_NAME" "${P28_START_B}B" "${P28_END_B}B"
log "p28 ${P28_NAME} restored"

run parted -s "$EMMC" mkpart "$P29_NAME" "${P29_START_B}B" "${P29_END_B}B"
log "p29 ${P29_NAME} restored (empty — Android will initialize on first boot)"

if ! $DRY_RUN; then
    reread_and_make_nodes 28 29
fi

header "Restoring partition contents"
run dd if="${BACKUP_DIR}/rsv_backup.bin"         of="${EMMC}p28" bs=1M status=none
log "rsv (p28) restored"

run dd if="${BACKUP_DIR}/env_backup.bin"          of="${EMMC}p2"  bs=1M status=none
log "env (p2) restored"

run dd if="${BACKUP_DIR}/bootloader_a_backup.bin" of="${EMMC}p7"  bs=1M status=none
log "bootloader_a (p7) restored"

# ── Post-restore partition layout ─────────────────────────────────────────────

header "Final eMMC partition layout"
if ! $DRY_RUN; then
    parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: 'NR>2 && /^[0-9]/{gsub(/MiB/,"",$4); printf "  p%-3s %-20s %5.0f MiB\n",$1,$6,$4}'
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}Android partition layout restored.${NC}"
echo ""
echo "  Remove the SD card and reboot to boot Android."
echo "  On first boot Android will reinitialize the userdata partition."
echo ""
echo "  To verify restore before rebooting, the SD card can be left in —"
echo "  CoreELEC will boot from SD while Android waits on eMMC."
echo ""
$DRY_RUN && warn "DRY-RUN complete — no changes were made"
