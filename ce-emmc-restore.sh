#!/bin/bash
# ce-emmc-restore.sh — Restore Android partition layout after CoreELEC eMMC install
# Must be run from CoreELEC booted off removable media (SD card or USB stick) —
# anywhere except the eMMC itself, which we're about to repartition.
# Requires backup files created by ce-emmc-install.sh in /storage/emmc-backup
# (or flat in /storage, the layout older installer versions used).

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
# Current installer writes backups to /storage/emmc-backup; older versions
# wrote them flat into /storage. Prefer the subdirectory when it has a set.
if [[ -f "/storage/emmc-backup/partition_layout.txt" ]]; then
    BACKUP_DIR="/storage/emmc-backup"
else
    BACKUP_DIR="/storage"
fi
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

# Tolerate parted's BLKRRPART warning — see ce-emmc-install.sh:run_parted for
# the full explanation. The on-disk write succeeds; partx -u reconciles the
# kernel-side view downstream.
run_parted() {
    if $DRY_RUN; then
        echo -e "${YELLOW}[DRY-RUN]${NC} parted $*"
        return 0
    fi
    local out rc=0
    out=$(parted "$@" 2>&1) || rc=$?
    [[ -n "$out" ]] && printf '%s\n' "$out"
    if (( rc != 0 )); then
        if grep -q "unable to inform the kernel" <<<"$out"; then
            warn "parted: BLKRRPART failed; on-disk write succeeded, partx -u will reconcile"
            return 0
        fi
        return $rc
    fi
    return 0
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

# tui_confirm_destructive — main confirm dialog. Both modes require typing
# YES: a whiptail --yesno alone is a single keypress, which is too little
# deliberateness for an operation that deletes partitions.
tui_confirm_destructive() {
    local title="$1" msg="$2"
    if $USE_TUI; then
        whiptail --title "$title" --yesno "$msg" 22 72 3>&1 1>&2 2>&3 || return 1
        local ans
        ans=$(whiptail --title "$title" --inputbox \
            "Final confirmation — type YES (all caps) to proceed:" 10 60 \
            3>&1 1>&2 2>&3) || return 1
        [[ "$ans" == "YES" ]]
    else
        echo -e "$msg"
        echo ""
        read -rp "  Type YES to proceed: " _c
        echo ""
        [[ "$_c" == "YES" ]]
    fi
}

# ── eMMC node helpers ─────────────────────────────────────────────────────────

# Create /dev nodes for eMMC partitions the kernel already knows about.
# Handles both kernel naming styles (mmcblk0pN from a GPT scan, partition
# label under Amlogic's driver) — same logic as ce-emmc-install.sh.
make_emmc_nodes() {
    for dir in /sys/block/mmcblk0/*/; do
        [[ -f "${dir}partition" ]] || continue
        local partnum devnum maj min
        partnum=$(cat "${dir}partition" 2>/dev/null) || continue
        [[ -n "$partnum" ]] || continue
        [[ -f "${dir}dev" ]] || continue
        read -r devnum < "${dir}dev"
        maj="${devnum%%:*}"
        min="${devnum##*:}"
        mknod "/dev/mmcblk0p${partnum}" b "$maj" "$min" 2>/dev/null || true
        local label
        label=$(basename "$dir")
        if [[ "$label" != mmcblk0p* ]]; then
            mknod "/dev/${label}" b "$maj" "$min" 2>/dev/null || true
        fi
    done
}

# Locate the sysfs directory for eMMC partition N, whichever naming style the
# kernel used. Echoes the directory path (with trailing /) on success.
find_part_sysfs() {
    local want="$1" dir partnum
    for dir in /sys/block/mmcblk0/*/; do
        [[ -f "${dir}partition" ]] || continue
        partnum=$(cat "${dir}partition" 2>/dev/null) || continue
        [[ "$partnum" == "$want" ]] && { echo "$dir"; return 0; }
    done
    return 1
}

# After repartitioning, ask the kernel to re-read the partition table and
# create /dev nodes for the new partitions using sysfs-reported major:minor.
reread_and_make_nodes() {
    local parts=("$@")
    partprobe "$EMMC" 2>/dev/null || true
    command -v partx >/dev/null 2>&1 && partx -u "$EMMC" 2>/dev/null || true

    for part in "${parts[@]}"; do
        local sysfs_dir="" waited=0
        until sysfs_dir=$(find_part_sysfs "$part") || (( waited >= 10 )); do
            sleep 1
            (( waited++ )) || true
        done
        if [[ -n "$sysfs_dir" && -f "${sysfs_dir}dev" ]]; then
            local devnum maj min
            read -r devnum < "${sysfs_dir}dev"
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

# /flash must NOT be on the eMMC we're about to repartition. SD card
# (mmcblk1) and USB stick (sd*) are both fine.
FLASH_SOURCE=$(awk '$2 == "/flash" {print $1}' /proc/mounts 2>/dev/null || true)
[[ -n "$FLASH_SOURCE" ]] || die "Could not determine /flash mount source"
[[ "$FLASH_SOURCE" == *"mmcblk0"* ]] && \
    die "Cannot restore while booted from eMMC — /flash is on '$FLASH_SOURCE'. Boot from SD card or USB and re-run."

case "$FLASH_SOURCE" in
    *mmcblk1*) BOOT_MEDIA="SD card" ;;
    /dev/sd*)  BOOT_MEDIA="USB stick" ;;
    *)         BOOT_MEDIA="removable media" ;;
esac
log "Boot source: ${BOOT_MEDIA} (${FLASH_SOURCE})"

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

After restore, boot Android via USB Burning Tool or by removing the ${BOOT_MEDIA}.
WARNING: All CoreELEC data on CE_STORAGE will be permanently lost."

tui_confirm_destructive "Restore Android" "$CONFIRM_MSG" \
    || { echo "Aborted."; exit 0; }

# ── Restore ───────────────────────────────────────────────────────────────────

header "Removing CoreELEC partitions"
run_parted -s "$EMMC" rm 29 rm 28
log "CE_STORAGE (p29) and CE_FLASH (p28) removed"

header "Restoring original partitions"
run_parted -s "$EMMC" mkpart "$P28_NAME" "${P28_START_B}B" "${P28_END_B}B"
log "p28 ${P28_NAME} restored"

run_parted -s "$EMMC" mkpart "$P29_NAME" "${P29_START_B}B" "${P29_END_B}B"
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

# Optionally restore frp (p3) and param (p15) if the install script backed
# them up. These partitions weren't touched by the install (so this is a
# defensive write of the same content the device already has), but the
# symmetry is correct: if a backup exists, write it back.
if [[ -f "${BACKUP_DIR}/frp_backup.bin" ]]; then
    run dd if="${BACKUP_DIR}/frp_backup.bin" of="${EMMC}p3" bs=1M status=none
    log "frp (p3) restored from backup"
else
    warn "frp_backup.bin not present (older install) — skipping p3 restore"
fi

if [[ -f "${BACKUP_DIR}/param_backup.bin" ]]; then
    run dd if="${BACKUP_DIR}/param_backup.bin" of="${EMMC}p15" bs=1M status=none
    log "param (p15) restored from backup"
else
    warn "param_backup.bin not present (older install) — skipping p15 restore"
fi

if [[ -f "${BACKUP_DIR}/bootloader_b_backup.bin" ]]; then
    BL_B_PART=$(parted -sm "$EMMC" unit B print 2>/dev/null \
        | awk -F: '$6=="bootloader_b"{print $1; exit}')
    if [[ -n "$BL_B_PART" ]]; then
        run dd if="${BACKUP_DIR}/bootloader_b_backup.bin" of="${EMMC}p${BL_B_PART}" bs=1M status=none
        log "bootloader_b (p${BL_B_PART}) restored from backup"
    else
        warn "bootloader_b_backup.bin present but no bootloader_b partition found — skipping"
    fi
fi

# ── Post-restore partition layout ─────────────────────────────────────────────

header "Final eMMC partition layout"
if ! $DRY_RUN; then
    parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: 'NR>2 && /^[0-9]/{gsub(/MiB/,"",$4); printf "  p%-3s %-20s %5.0f MiB\n",$1,$6,$4}'
fi

# ── Done ──────────────────────────────────────────────────────────────────────

# Flush the restored partition contents before the user pulls the boot media.
run sync

echo ""
echo -e "${GREEN}${BOLD}Android partition layout restored.${NC}"
echo ""
echo "  Remove the ${BOOT_MEDIA} and reboot to boot Android."
echo "  On first boot Android will reinitialize the userdata partition."
echo ""
echo "  To verify restore before rebooting, the ${BOOT_MEDIA} can be left in —"
echo "  CoreELEC will boot from it while Android waits on eMMC."
echo ""
$DRY_RUN && warn "DRY-RUN complete — no changes were made"
