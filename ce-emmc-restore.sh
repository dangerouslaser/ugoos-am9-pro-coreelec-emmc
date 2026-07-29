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
# the full explanation. The on-disk write succeeds; reread_and_make_nodes
# reconciles the kernel-side view downstream.
#
# IMPORTANT: one parted command per invocation — parted -s stops executing
# its command list at the first commit that fails to reach the kernel, so a
# chained call can silently skip its later commands (install issue #1).
# Follow every destructive invocation with verify_part_absent/present.
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
            warn "parted: BLKRRPART failed; on-disk write succeeded, device nodes will be rebuilt from sysfs"
            return 0
        fi
        return $rc
    fi
    return 0
}

# On-disk GPT row / name for partition N, read fresh from disk via parted —
# reflects reality even when the kernel's partition view is stale. Empty
# output from ondisk_part_row means the partition does not exist on disk.
ondisk_part_row()  { parted -sm "$EMMC" unit B print 2>/dev/null | awk -F: -v p="$1" '$1==p{print; exit}'; }
ondisk_part_name() { ondisk_part_row "$1" | awk -F: '{print $6}'; }

verify_part_absent() {
    $DRY_RUN && return 0
    [[ -z "$(ondisk_part_row "$1")" ]] && return 0
    die "Partition $1 is still present in the on-disk GPT after 'parted rm' —
the kernel likely refused the table update (a partition was in use).
Reboot and re-run ce-emmc-restore.sh; completed steps are skipped on re-run."
}

verify_part_present() {
    $DRY_RUN && return 0
    local have
    have=$(ondisk_part_name "$1")
    [[ "$have" == "$2" ]] && return 0
    die "Partition $1 is '${have:-absent}' in the on-disk GPT, expected '$2' —
'parted mkpart' did not take effect. Reboot and re-run ce-emmc-restore.sh."
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
#
# partprobe is the only table-reread tool CoreELEC ships — util-linux's
# partx/addpart and blockdev/sfdisk/sgdisk are all absent, so the `partx -u`
# fallback this used to attempt was dead code that never ran. When BLKRRPART
# doesn't take, sysfs still reports the new partitions; the loop below reads
# major:minor from there and creates the nodes directly.
reread_and_make_nodes() {
    local parts=("$@")
    partprobe "$EMMC" 2>/dev/null || true

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

# ── Device-identity helpers ───────────────────────────────────────────────────

# Everything that asks "is this thing on the eMMC?" must compare device
# numbers, never name strings. CoreELEC mounts partitions from label-named
# nodes (/dev/CE_FLASH, /dev/CE_STORAGE) which are real block devices carrying
# mmcblk0's major:minor with no "mmcblk0" in the name. Name matching silently
# misses them, defeating both the booted-from-eMMC guard and the unmount sweep.

# major:minor for a path, or empty. stat reports hex; /proc/* is decimal.
dev_majmin() {
    local hexmm
    hexmm=$(stat -c '%t:%T' "$1" 2>/dev/null) || return 0
    [[ -n "$hexmm" && "$hexmm" != ":" ]] || return 0
    printf '%d:%d\n' "0x${hexmm%%:*}" "0x${hexmm##*:}"
}

# Every major:minor belonging to the eMMC — whole disk plus all partitions.
emmc_majmins() { awk '$4 ~ /^mmcblk0/ {print $1":"$2}' /proc/partitions; }

# Kernel device name for a major:minor ("mmcblk0p28"), or empty.
name_for_majmin() { awk -v mm="$1" '$1":"$2 == mm {print $4; exit}' /proc/partitions; }

# Mountpoints whose backing device is on the eMMC, however they were named.
emmc_mountpoints() {
    local known src mnt mm
    known=" $(emmc_majmins | tr '\n' ' ') "
    while read -r src mnt _; do
        [[ -b "$src" ]] || continue
        mm=$(dev_majmin "$src")
        [[ -n "$mm" && "$known" == *" $mm "* ]] && printf '%s\n' "$mnt"
    done < /proc/mounts
}

# ── Preflight ─────────────────────────────────────────────────────────────────

header "Preflight checks"

[[ "$(id -u)" == "0" ]] || die "Must be run as root"

# /flash must NOT be on the eMMC we're about to repartition. SD card
# (mmcblk1) and USB stick (sd*) are both fine.
#
# Match on device numbers, not name strings — /flash is routinely mounted from
# a label-named node (/dev/CE_FLASH) that carries mmcblk0's major:minor with
# no "mmcblk0" in its name, so the old glob left this guard defeated on
# exactly the eMMC-booted boxes it exists to stop.
FLASH_SOURCE=$(awk '$2 == "/flash" {print $1}' /proc/mounts 2>/dev/null || true)
[[ -n "$FLASH_SOURCE" ]] || die "Could not determine /flash mount source"

FLASH_MAJMIN=""
FLASH_REALNAME=""
if [[ -b "$FLASH_SOURCE" ]]; then
    FLASH_MAJMIN=$(dev_majmin "$FLASH_SOURCE")
    [[ -n "$FLASH_MAJMIN" ]] && FLASH_REALNAME=$(name_for_majmin "$FLASH_MAJMIN")
fi

if [[ -n "$FLASH_MAJMIN" ]]; then
    if [[ " $(emmc_majmins | tr '\n' ' ') " == *" ${FLASH_MAJMIN} "* ]]; then
        die "Cannot restore while booted from eMMC — /flash is ${FLASH_SOURCE} (${FLASH_REALNAME:-$FLASH_MAJMIN}), which is on ${EMMC}. Boot from SD card or USB and re-run."
    fi
else
    warn "Could not resolve ${FLASH_SOURCE} to a device number — falling back to name matching"
    [[ "$FLASH_SOURCE" == *"mmcblk0"* ]] && \
        die "Cannot restore while booted from eMMC — /flash is on '$FLASH_SOURCE'. Boot from SD card or USB and re-run."
fi

case "${FLASH_REALNAME:-$FLASH_SOURCE}" in
    *mmcblk1*)      BOOT_MEDIA="SD card" ;;
    sd*|/dev/sd*)   BOOT_MEDIA="USB stick" ;;
    *)              BOOT_MEDIA="removable media" ;;
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

# All backup files must exist before proceeding
for f in partition_layout.txt rsv_backup.bin env_backup.bin bootloader_a_backup.bin; do
    [[ -f "${BACKUP_DIR}/${f}" ]] || \
        die "Backup file not found: ${BACKUP_DIR}/${f} — run ce-emmc-install.sh first"
done
log "Backup files: all present in ${BACKUP_DIR}"

# CE_FLASH must exist on p28 — confirms this is a CE install to restore from.
# Also accept the partial states a run interrupted mid-repartition leaves
# behind (p28 absent, p28 already recreated under its original Android name,
# or CE_FLASH created but never formatted) — the repartition steps below
# skip whatever is already done, so a re-run resumes cleanly.
P28_ONDISK_NAME=$(ondisk_part_name 28)
P28_ORIG_NAME=$(awk -F: '/^28:/{print $6}' "${BACKUP_DIR}/partition_layout.txt")
if blkid "${EMMC}p28" 2>/dev/null | grep -q "CE_FLASH"; then
    log "CE_FLASH confirmed on p28"
elif [[ -z "$P28_ONDISK_NAME" || "$P28_ONDISK_NAME" == "$P28_ORIG_NAME" || "$P28_ONDISK_NAME" == "CE_FLASH" ]]; then
    warn "p28 is ${P28_ONDISK_NAME:-absent} on disk with no CE_FLASH filesystem — resuming an interrupted install/restore"
else
    die "CE_FLASH not found on p28 — is CoreELEC installed on eMMC?"
fi

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

# Nothing from the eMMC may be mounted while its table is rewritten — an
# in-use partition makes the kernel refuse the update and parted stop early
# (install issue #1). CE's automounter grabs CE_STORAGE/CE_FLASH under
# /media when booted from removable media, so sweep and unmount.
# emmc_mountpoints() matches by device number, so the label-named nodes CE
# actually mounts these from are caught too.
EMMC_MOUNTPOINTS=$(emmc_mountpoints)
if [[ -n "$EMMC_MOUNTPOINTS" ]]; then
    while IFS= read -r mnt; do
        warn "eMMC partition mounted at ${mnt} — unmounting"
        run umount "$mnt" || die "Could not unmount ${mnt} — close whatever is using it (or reboot) and re-run"
    done <<< "$EMMC_MOUNTPOINTS"
fi

# Unmounting isn't always enough. Android's `super` (p27) is a logical-partition
# container: if anything has mapped its sub-partitions, device-mapper holds the
# underlying eMMC partition open and the kernel still refuses the table update,
# with the same stop-early symptom as a live mount. dmsetup is present on
# CoreELEC even though partx et al are not, so sweep for maps backed by this
# disk and tear them down. Nothing we create uses device-mapper, so any map on
# mmcblk0 here is Android-side and safe to remove.
if command -v dmsetup >/dev/null 2>&1; then
    EMMC_MAJOR=$(emmc_majmins | head -1); EMMC_MAJOR="${EMMC_MAJOR%%:*}"
    while IFS= read -r dm; do
        [[ -n "$dm" ]] || continue
        deps=$(dmsetup deps "$dm" 2>/dev/null | tr -d ' ' || true)
        if [[ -n "$EMMC_MAJOR" && "$deps" == *"(${EMMC_MAJOR},"* ]]; then
            warn "device-mapper target '${dm}' is backed by the eMMC — removing"
            run dmsetup remove "$dm" \
                || die "Could not remove device-mapper target '${dm}' — reboot and re-run"
        fi
    done < <(dmsetup ls 2>/dev/null | awk '$1 != "No" {print $1}')
fi

# Delete a CE partition unless it is already gone or already recreated under
# its original Android name (interrupted-restore resume).
restore_rm_ce_part() {
    local part="$1" orig="$2" name
    name=$(ondisk_part_name "$part")
    if [[ -z "$name" ]]; then
        log "p${part} already absent — skipping delete"
    elif [[ "$name" == "$orig" ]]; then
        log "p${part} already restored as ${orig} — skipping delete"
    else
        run_parted -s "$EMMC" rm "$part"
        verify_part_absent "$part"
        log "p${part} (${name}) removed"
    fi
}
restore_rm_ce_part 29 "$P29_NAME"
restore_rm_ce_part 28 "$P28_NAME"

header "Restoring original partitions"
if [[ "$(ondisk_part_name 28)" == "$P28_NAME" ]]; then
    log "p28 already ${P28_NAME} — skipping create"
else
    run_parted -s "$EMMC" mkpart "$P28_NAME" "${P28_START_B}B" "${P28_END_B}B"
    verify_part_present 28 "$P28_NAME"
    log "p28 ${P28_NAME} restored"
fi

if [[ "$(ondisk_part_name 29)" == "$P29_NAME" ]]; then
    log "p29 already ${P29_NAME} — skipping create"
else
    run_parted -s "$EMMC" mkpart "$P29_NAME" "${P29_START_B}B" "${P29_END_B}B"
    verify_part_present 29 "$P29_NAME"
    log "p29 ${P29_NAME} restored (empty — Android will initialize on first boot)"
fi

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
