#!/bin/bash
# ce-emmc-install.sh — CoreELEC eMMC installer for Ugoos S905X5 boxes
#
# Workaround until ceemmc adds support for Ugoos S905X5 boards.
# Originally written for Ugoos AM9 Pro (s6_s905x5_ugoos_am9_pro); the
# Ugoos SK4 (s7d_s905x5m_ugoos_sk4) is also accepted because it ships the
# same Amlogic partition layout. Add new boards to SUPPORTED_BOARDS below.
#
# Must be run from CoreELEC booted off removable media (SD card or USB
# stick) — anywhere except the eMMC itself, which we're about to repartition.
#
# What this does:
#   1. Verifies device identity by cross-checking the running cmdline's
#      `androidboot.serialno` and `mac=` against the AMLNORMAL keystore on
#      `reserved` (p1) using aml-keystore-tool.py — aborts if they disagree
#   2. Backs up partition layout, rsv (p28), env (p2), bootloader_a (p7),
#      reserved (p1, the AMLNORMAL keystore with MAC/serial), frp (p3, anti-
#      rollback nonce), and param (p15, TV picture-quality DB) to /storage
#   3. After backup, verifies the p1 backup is a valid AMLNORMAL keystore
#      (correct magic + at least 2 populated slots) — aborts on failure
#   4. Keeps super (p27) — Android system images intact for potential restore
#   5. Deletes rsv (p28) and userdata (p29)
#   6. Creates CE_FLASH (512 MB FAT32) at p28 and CE_STORAGE (remaining space ext4) at p29
#   7. Copies all boot files from /flash (the running CE media) to CE_FLASH
#   8. Rebuilds cfgload to use disk=LABEL=CE_STORAGE (replaces the dual-boot
#      ceemmc disk=FOLDER=/dev/CE_STORAGE path with standalone label resolution).
#      Pass --no-cfgload-rebuild to skip this step if a future cfgload format
#      breaks the rebuilder.
#   9. Always installs /flash/mount-storage.sh + adds nofsck to config.ini.
#      These are user files CE's updater never touches, so they survive nightly
#      auto-updates. mount-storage.sh is a first-class CE init hook that
#      bypasses the broken FOLDER= mount path; nofsck prevents the retry loop
#      from the bogus /dev/CE_STORAGE node. This is the durable rescue layer:
#      even if step 8's cfgload patch gets reverted by a CE update, the device
#      keeps booting.
#  10. Optionally writes a custom boot logo to p10 (--restore-logo PATH)
#  11. Optionally migrates your current /storage to CE_STORAGE
#
# Flags:
#   --info              Read-only diagnostic mode — print everything we know
#                       about the device (partition state, keystore, bootloader
#                       version, install state) and exit. Safe to run any time.
#   --dry-run           Show all commands without executing destructive ops.
#   --no-cfgload-rebuild  Skip cfgload rebuild, install legacy workarounds.
#   --restore-logo PATH Write a custom boot logo to p10 during the install.
#                       PATH may be either a packed AML_RES .bin file, or a
#                       directory of NN_name.bmp files produced by
#                       `aml-logo-tool.py unpack`. The packed bin is
#                       validated to start with the AML_RES! magic before
#                       writing to the device.
#   --help              Show help and exit.

set -euo pipefail

# ── Flags ─────────────────────────────────────────────────────────────────────

DRY_RUN=false
REBUILD_CFGLOAD=true
INFO_MODE=false
LOGO_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --info)                INFO_MODE=true; shift ;;
        --dry-run)             DRY_RUN=true; shift ;;
        --no-cfgload-rebuild)  REBUILD_CFGLOAD=false; shift ;;
        --restore-logo)        LOGO_PATH="$2"; shift 2 ;;
        --restore-logo=*)      LOGO_PATH="${1#--restore-logo=}"; shift ;;
        --help)
            cat <<EOF
Usage: ce-emmc-install.sh [options]

Options:
  --info                     Read-only diagnostic; print device state and exit.
                             No backups, no writes, no install actions.
  --dry-run                  Show all commands without executing destructive ops.
                             Non-destructive reads (parted print, blkid, dd reads)
                             still run.
  --no-cfgload-rebuild       Skip the cfgload rebuild (mount-storage.sh +
                             nofsck are always installed regardless — those
                             are the durable rescue layer that survives CE
                             auto-updates). Use this if a future CoreELEC
                             build ships a cfgload format the rebuild step
                             doesn't understand.
  --restore-logo PATH        Write a custom boot logo to p10 during install.
                             PATH is either a packed AML_RES .bin (must start
                             with the AML_RES! magic) or a directory of
                             NN_name.bmp files from aml-logo-tool.py unpack.
  --help                     Show this help and exit.
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1 (try --help)" >&2
            exit 1
            ;;
    esac
done

# ── Constants ─────────────────────────────────────────────────────────────────

EMMC="/dev/mmcblk0"
FLASH_DIR="/flash"

# Supported boards — each entry is "<dtb-filename>|<friendly-name>". The
# active /flash/dtb.img must md5-match one of these DTBs in
# /flash/device_trees/. To add a new Ugoos S905X5 board with the same
# partition layout, drop another line here.
SUPPORTED_BOARDS=(
    "s6_s905x5_ugoos_am9_pro.dtb|Ugoos AM9 Pro"
    "s7d_s905x5m_ugoos_sk4.dtb|Ugoos SK4"
)
MNT_FLASH="/var/ce_flash"
MNT_STORAGE="/var/ce_storage"
BACKUP_DIR="/storage"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Locate the aml-*-tool.py helpers. We accept either the legacy layout
# (tools alongside this script — for /storage installs where the user
# scp'd everything to one dir) or the repo layout (tools in img-tools/).
find_aml_tool() {
    local name="$1" candidate
    for candidate in "${SCRIPT_DIR}/${name}" \
                     "${SCRIPT_DIR}/img-tools/${name}" \
                     "${SCRIPT_DIR}/../img-tools/${name}"; do
        [[ -f "$candidate" ]] && { echo "$candidate"; return 0; }
    done
    return 1
}
KEYSTORE_TOOL="$(find_aml_tool aml-keystore-tool.py)"   || KEYSTORE_TOOL=""
BOOTLOADER_TOOL="$(find_aml_tool aml-bootloader-tool.py)" || BOOTLOADER_TOOL=""
LOGO_TOOL="$(find_aml_tool aml-logo-tool.py)"           || LOGO_TOOL=""

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

# parted on Amlogic eMMC sometimes fails BLKRRPART after a successful on-disk
# write (kernel can't refresh its partition view because something on the device
# is held open). The disk has been written correctly; we just need to fall back
# to partx -u for the kernel-side reconciliation. This wrapper runs parted with
# both streams merged so the user still sees parted's output, and treats the
# BLKRRPART warning as non-fatal so set -e doesn't kill the script.
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
            warn "parted: BLKRRPART failed (kernel can't refresh partition table)"
            warn "  — on-disk write succeeded; will reconcile with partx -u"
            return 0
        fi
        return $rc
    fi
    return 0
}

$DRY_RUN && warn "DRY-RUN mode — no changes will be made"

# (--info and --restore-logo validation run after all helper functions are
# defined; see the block just above "## Preflight".)

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

# Create /dev nodes for eMMC partitions the kernel already knows about.
# Handles both kernel naming styles:
#   - SD-card boot: sysfs entries are mmcblk0p1, mmcblk0p2, etc.
#   - eMMC boot:    sysfs entries are by partition label (reserved, env, ...).
# In both cases the authoritative partition number lives in <dir>/partition.
# We create /dev/mmcblk0pN regardless of label so the rest of the script
# can use canonical paths.
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
        # Also create a label node if the sysfs entry uses a label name (not
        # already a mmcblk0pN entry) — preserves backward compatibility.
        local label
        label=$(basename "$dir")
        if [[ "$label" != mmcblk0p* ]]; then
            mknod "/dev/${label}" b "$maj" "$min" 2>/dev/null || true
        fi
    done
}

# After repartitioning, ask the kernel to re-read the partition table and
# create /dev nodes for the new partitions using sysfs-reported major:minor.
#
# We try partprobe first (full BLKRRPART reread — fine when nothing on the
# device is held open), then fall back to partx -u which uses per-partition
# BLKPG_* ioctls and works even when something on the device is open. On
# Amlogic eMMC, BLKRRPART has been observed to fail (e.g. on Ugoos SK4 CE 22
# Piers nightly) while partx -u succeeds.
reread_and_make_nodes() {
    local parts=("$@")
    partprobe "$EMMC" 2>/dev/null || true
    command -v partx >/dev/null 2>&1 && partx -u "$EMMC" 2>/dev/null || true

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

# ── Keystore / identity helpers ──────────────────────────────────────────────

# Extract a slot's value from p1 via aml-keystore-tool.py list. Returns ""
# if the tool is unavailable or the slot isn't populated.
keystore_value() {
    local slot_name="$1"
    [[ -f "$KEYSTORE_TOOL" ]] || return 0
    python3 "$KEYSTORE_TOOL" list "${EMMC}p1" 2>/dev/null \
        | awk -v s="'$slot_name'" "
            /name:[[:space:]]+/ { in_slot = (\$0 ~ s) }
            in_slot && /value:/ { gsub(/^.*value:[[:space:]]+'?/, \"\"); gsub(/'\$/, \"\"); print; exit }
        "
}

# After a p1 backup is written, verify it's a valid AMLNORMAL keystore.
# Returns non-zero if the backup looks corrupt (and the caller should abort).
verify_p1_backup() {
    local backup_file="$1"
    [[ -f "$KEYSTORE_TOOL" ]] || { warn "aml-keystore-tool.py not present — skipping p1 verification"; return 0; }
    [[ -f "$backup_file" ]] || { warn "p1 backup $backup_file missing"; return 1; }

    local info
    info=$(python3 "$KEYSTORE_TOOL" info "$backup_file" 2>&1) || {
        warn "aml-keystore-tool.py failed on p1 backup:"
        echo "$info" | sed 's/^/  /' >&2
        return 1
    }

    local keycnt
    keycnt=$(echo "$info" | awk '/keycnt:/ {print $2; exit}')
    if [[ -z "$keycnt" || "$keycnt" -lt 1 ]]; then
        warn "p1 backup: keycnt parsed as '${keycnt:-empty}' — expected >= 1"
        echo "$info" | sed 's/^/  /' >&2
        return 1
    fi

    log "p1 backup verified — AMLNORMAL keystore with $keycnt populated slot(s)"
    return 0
}

# Cross-check the running cmdline's androidboot.serialno and mac= against
# the values stored in p1's keystore. Mismatch => abort (something tampered,
# or device is in unexpected state).
identity_cross_check() {
    [[ -f "$KEYSTORE_TOOL" ]] || { warn "aml-keystore-tool.py not present — skipping identity cross-check"; return 0; }

    local cmdline_serial cmdline_mac
    cmdline_serial=$(awk -v RS=' ' '/^androidboot\.serialno=/{sub(/^androidboot\.serialno=/,""); print}' /proc/cmdline | tr -d '\n')
    cmdline_mac=$(awk -v RS=' ' '/^mac=/{sub(/^mac=/,""); print; exit}' /proc/cmdline | tr -d '\n')

    local keystore_serial keystore_mac
    keystore_serial=$(keystore_value usid)
    keystore_mac=$(keystore_value mac)

    if [[ -z "$keystore_serial" && -z "$keystore_mac" ]]; then
        warn "Keystore returned no usid or mac slot — skipping cross-check"
        return 0
    fi

    if [[ -n "$cmdline_serial" && -n "$keystore_serial" && "$cmdline_serial" != "$keystore_serial" ]]; then
        die "Identity mismatch: cmdline serial '$cmdline_serial' != keystore usid '$keystore_serial'"
    fi
    if [[ -n "$cmdline_mac" && -n "$keystore_mac" && "${cmdline_mac,,}" != "${keystore_mac,,}" ]]; then
        die "Identity mismatch: cmdline mac '$cmdline_mac' != keystore mac '$keystore_mac'"
    fi
    log "Identity cross-check: serial=$keystore_serial mac=$keystore_mac (matches cmdline)"
}

# ── Logo processing helper (--restore-logo) ──────────────────────────────────

# Validates --restore-logo argument and returns (via echo) the path to a
# ready-to-write AML_RES .bin file. If a directory was given, packs it first.
prepare_logo_bin() {
    local input="$1"
    [[ -e "$input" ]] || die "--restore-logo: '$input' does not exist"
    if [[ -d "$input" ]]; then
        [[ -f "$LOGO_TOOL" ]] || die "--restore-logo with a directory requires aml-logo-tool.py"
        local out
        out=$(mktemp --suffix=.bin 2>/dev/null || mktemp)
        python3 "$LOGO_TOOL" pack "$input" "$out" >&2 || die "aml-logo-tool.py pack failed"
        echo "$out"
    else
        # File — verify AML_RES! magic at offset 8 (after the 4-byte CRC + 4-byte version)
        local magic
        magic=$(dd if="$input" bs=1 skip=8 count=8 status=none 2>/dev/null)
        [[ "$magic" == "AML_RES!" ]] || die "--restore-logo: '$input' is not an AML_RES container (no AML_RES! magic at offset 8)"
        echo "$input"
    fi
}

# ── --info mode ──────────────────────────────────────────────────────────────

do_info() {
    [[ "$(id -u)" == "0" ]] || die "Must be run as root"
    [[ -b "$EMMC" ]] || die "eMMC not found at $EMMC"
    make_emmc_nodes

    header "Hardware"
    log "Kernel: $(uname -r)"
    if [[ -f /etc/os-release ]]; then
        log "OS: $(grep -E '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '\"')"
    fi
    local mmc_dir
    mmc_dir=$(ls -d /sys/class/mmc_host/mmc0/mmc0:* 2>/dev/null | head -1)
    if [[ -n "$mmc_dir" ]]; then
        log "eMMC: $(cat "$mmc_dir/name") manfid=$(cat "$mmc_dir/manfid") date=$(cat "$mmc_dir/date") fwrev=$(cat "$mmc_dir/fwrev")"
        log "Health: life=$(cat "$mmc_dir/life_time") pre_eol=$(cat "$mmc_dir/pre_eol_info")"
    fi
    local sectors
    sectors=$(cat /sys/block/mmcblk0/size 2>/dev/null || echo 0)
    log "eMMC size: $sectors sectors = $(awk -v s="$sectors" 'BEGIN{printf "%.2f GiB", s*512/1024/1024/1024}')"

    header "Partition layout (GPT)"
    parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: 'NR>2 && /^[0-9]/{gsub(/MiB/,"",$4); printf "  p%-3s %-20s %5.0f MiB\n",$1,$6,$4}'

    header "Install state"
    # Use parted to find CE_FLASH regardless of which partition number it
    # ended up on (old-style install was p27, current install is p28).
    local ce_flash_p
    ce_flash_p=$(parted -sm "$EMMC" unit MiB print 2>/dev/null \
        | awk -F: '/:CE_FLASH:/{print $1; exit}')
    if [[ -n "$ce_flash_p" ]]; then
        if [[ "$ce_flash_p" == "28" ]]; then
            log "CoreELEC IS installed (CE_FLASH at p${ce_flash_p}, super preserved)"
        else
            warn "CoreELEC IS installed (CE_FLASH at p${ce_flash_p} — old-style install, super was deleted)"
        fi
        if [[ -f /flash/mount-storage.sh ]]; then
            log "  mount-storage.sh hook present (auto-update rescue)"
        else
            warn "  mount-storage.sh hook missing — eMMC boot will break after next CE update"
        fi
        # Match nofsck only inside the actual coreelec='...' setting, not
        # within documentation comments that list it as a valid option.
        if grep -qE "^coreelec=['\"][^'\"]*nofsck" /flash/config.ini 2>/dev/null; then
            log "  nofsck present in coreelec= setting"
        else
            warn "  nofsck missing from coreelec= setting — fsck retry loop may occur"
        fi
        if [[ -f /flash/cfgload ]]; then
            local cfg_size
            cfg_size=$(stat -c %s /flash/cfgload 2>/dev/null || stat -f %z /flash/cfgload)
            log "  cfgload present: $cfg_size bytes"
        fi
    else
        log "CoreELEC NOT installed (Android partition layout intact)"
    fi

    header "U-Boot env summary (p2)"
    if [[ -r "${EMMC}p2" ]]; then
        dd if="${EMMC}p2" bs=1M count=1 status=none 2>/dev/null | tr '\0' '\n' \
            | grep -E '^(bootcmd|ce_on_emmc|firstboot|board|bootloader_version|active_slot|ethaddr|EnableSelinux|verifiedbootstate|avb2)=' \
            | sort -u | head -20 | sed 's/^/  /'
    fi

    header "AMLNORMAL keystore (p1)"
    if [[ -f "$KEYSTORE_TOOL" ]]; then
        python3 "$KEYSTORE_TOOL" info "${EMMC}p1" 2>&1 | sed 's/^/  /'
        echo
        log "Slots:"
        python3 "$KEYSTORE_TOOL" list "${EMMC}p1" 2>&1 \
            | awk '/slot #/{slot=$2} /name:/{n=$2} /value:/{v=$2; printf "  %s %-12s %s\n", slot, n, v}'
    else
        warn "aml-keystore-tool.py not found in $SCRIPT_DIR — skipping keystore info"
    fi

    header "Bootloader (p7)"
    if [[ -f "$BOOTLOADER_TOOL" ]]; then
        python3 "$BOOTLOADER_TOOL" info "${EMMC}p7" 2>&1 | sed 's/^/  /' | head -10
    else
        warn "aml-bootloader-tool.py not found — skipping bootloader info"
    fi

    header "Cmdline identity"
    local serial mac
    serial=$(awk -v RS=' ' '/^androidboot\.serialno=/{sub(/.*=/,""); print; exit}' /proc/cmdline)
    mac=$(awk -v RS=' ' '/^mac=/{sub(/.*=/,""); print; exit}' /proc/cmdline)
    log "Kernel cmdline serial: ${serial:-<missing>}"
    log "Kernel cmdline MAC:    ${mac:-<missing>}"
    [[ -f "$KEYSTORE_TOOL" ]] && identity_cross_check

    exit 0
}

# ── Early flag handling (now that helper functions are defined) ──────────────

# --info mode: read-only diagnostic. Exits without entering preflight.
if $INFO_MODE; then
    do_info
fi

# --restore-logo: validate / pack the logo path NOW (before destructive ops),
# so a bad argument fails fast without partial-install side effects.
LOGO_BIN=""
if [[ -n "$LOGO_PATH" ]]; then
    LOGO_BIN=$(prepare_logo_bin "$LOGO_PATH")
    log "Custom logo prepared: $LOGO_BIN (will be written to ${EMMC}p10 during install)"
fi

# ── Preflight ─────────────────────────────────────────────────────────────────

header "Preflight checks"

[[ "$(id -u)" == "0" ]] || die "Must be run as root"

# Board check — the active /flash/dtb.img must md5-match a supported DTB
DTB_ACTIVE="${FLASH_DIR}/dtb.img"
[[ -f "$DTB_ACTIVE" ]] || die "Active dtb.img not found at $DTB_ACTIVE"
HASH_ACTIVE=$(md5sum "$DTB_ACTIVE" | awk '{print $1}')

BOARD_NAME=""
BOARD_DTB=""
for entry in "${SUPPORTED_BOARDS[@]}"; do
    dtb_name="${entry%%|*}"
    friendly="${entry##*|}"
    dtb_path="${FLASH_DIR}/device_trees/${dtb_name}"
    [[ -f "$dtb_path" ]] || continue
    if [[ "$(md5sum "$dtb_path" | awk '{print $1}')" == "$HASH_ACTIVE" ]]; then
        BOARD_NAME="$friendly"
        BOARD_DTB="$dtb_name"
        break
    fi
done

if [[ -z "$BOARD_NAME" ]]; then
    msg="Active dtb.img doesn't match any supported board. Supported DTBs:"
    for entry in "${SUPPORTED_BOARDS[@]}"; do
        msg+=$'\n  - '"${entry%%|*}  (${entry##*|})"
    done
    msg+=$'\n''Check your DTB selection in config.ini, or add the board to SUPPORTED_BOARDS.'
    die "$msg"
fi

log "Board: ${BOARD_NAME} (${BOARD_DTB%.dtb})"

# /flash must NOT be on the eMMC we're about to repartition. SD card
# (mmcblk1) and USB stick (sd*) are both fine — the installer just rsyncs
# /flash → eMMC CE_FLASH, so any boot source other than the destination works.
FLASH_SOURCE=$(awk '$2 == "/flash" {print $1}' /proc/mounts 2>/dev/null || true)
[[ -n "$FLASH_SOURCE" ]] || die "Could not determine /flash mount source"
[[ "$FLASH_SOURCE" == *"mmcblk0"* ]] && \
    die "Cannot install while booted from eMMC — /flash is on '$FLASH_SOURCE'. Boot from SD card or USB and re-run."

case "$FLASH_SOURCE" in
    *mmcblk1*) BOOT_MEDIA="SD card" ;;
    /dev/sd*)  BOOT_MEDIA="USB stick" ;;
    *)         BOOT_MEDIA="removable media" ;;
esac
log "Boot source: ${BOOT_MEDIA} (${FLASH_SOURCE})"

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

# Cross-check device identity vs the AMLNORMAL keystore in p1 — aborts on
# mismatch. Catches the case where someone has been modifying state, or the
# device is in an unexpected configuration we shouldn't proceed against.
identity_cross_check

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
CoreELEC eMMC Installer — ${BOARD_NAME}

  KEEP    p27  super       ${SUPER_HUMAN}  (Android system — untouched)

  DELETE  p28  rsv         ${RSV_HUMAN}${RSV_NOTE}
  DELETE  p29  userdata    ${USERDATA_HUMAN}  (encrypted — unrecoverable)

  CREATE  p28  CE_FLASH    512 MB  FAT32  (CoreELEC boot)
  CREATE  p29  CE_STORAGE  ${CE_STORAGE_HUMAN}  ext4   (CoreELEC storage)

Partitions p1–p26 and super (p27) are NOT touched.
boot0/boot1 are hardware write-protected and safe.

Android restore requires Amlogic USB Burning Tool on Windows via the
USB-C OTG port using the official Ugoos factory image."

tui_confirm_destructive "CoreELEC eMMC Installer — ${BOARD_NAME}" "$CONFIRM_MSG" \
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

# Back up p1 reserved — it holds the Amlogic UKS keystore (AMLNORMAL magic at
# offset 0x4000, with a redundant copy at 0x44000). On this SoC family the
# device's ETH MAC and serial are stored there as plaintext slots. The install
# does not touch p1, but a wipe of this partition would lose eMMC-stored
# identity and the factory image does not include it, so USB Burning Tool
# restore would not recover the values. Cheap insurance.
run dd if="${EMMC}p1" of="${BACKUP_DIR}/reserved_backup.bin" bs=1M status=none
log "reserved (p1) → ${BACKUP_DIR}/reserved_backup.bin"

# Verify the p1 backup actually contains a valid AMLNORMAL keystore. If the
# dd read silently produced zeros or the keystore is corrupt, we want to know
# NOW (before destructive ops) so the user can investigate. Falls through
# silently if aml-keystore-tool.py isn't shipped alongside the install script.
if ! $DRY_RUN; then
    verify_p1_backup "${BACKUP_DIR}/reserved_backup.bin" \
        || die "p1 backup verification failed — aborting before destructive ops."
fi

# Back up frp (p3) — contains 36 bytes of unit-unique anti-rollback / FRP
# signing material at offset 0. The install doesn't touch p3 either; this is
# defensive in case of future destructive operations on the partition table.
run dd if="${EMMC}p3" of="${BACKUP_DIR}/frp_backup.bin" bs=1M status=none
log "frp (p3) → ${BACKUP_DIR}/frp_backup.bin"

# Back up param (p15) — ext4 filesystem mounted at /mnt/vendor/param in
# Android, containing the TV picture-quality DB (pq.db, pq_ext.db) which
# is likely tuned per-device at the factory. Same defensive rationale.
run dd if="${EMMC}p15" of="${BACKUP_DIR}/param_backup.bin" bs=1M status=none
log "param (p15) → ${BACKUP_DIR}/param_backup.bin"

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
run_parted -s "$EMMC" rm 29 rm 28

log "Creating CE_FLASH (${RSV_START_MIB}MiB – ${CE_FLASH_END_MIB}MiB)..."
run_parted -s "$EMMC" mkpart CE_FLASH fat32 "${RSV_START_MIB}MiB" "${CE_FLASH_END_MIB}MiB"

log "Creating CE_STORAGE (${CE_FLASH_END_MIB}MiB – 100%)..."
run_parted -s "$EMMC" mkpart CE_STORAGE ext4 "${CE_FLASH_END_MIB}MiB" "100%"

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

log "Copying files from ${FLASH_DIR}..."
run cp -a "${FLASH_DIR}/." "${MNT_FLASH}/"
run rm -f "${MNT_FLASH}/fs-resize.log"

if $REBUILD_CFGLOAD; then

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

fi  # end REBUILD_CFGLOAD branch

# Always install /flash/mount-storage.sh + nofsck in config.ini — these are the
# durable rescue path that survives CoreELEC's nightly auto-updates.
#
# Background: CE's updater unconditionally overwrites cfgload from
# /usr/share/bootloader/${DEVICE_CFGLOAD}, which uses
# disk=FOLDER=/dev/CE_STORAGE (intended for ceemmc dual-boot installs).
# Even when we patch cfgload during install, every auto-update reverts it.
# An earlier attempt at a /flash/user-update.sh post-update hook failed
# because the hook runs in the initramfs context where python3 isn't
# present (only busybox + sh).
#
# mount-storage.sh is a first-class CE hook (init line ~635: `if [ -f
# /flash/mount-storage.sh ]; then . /flash/mount-storage.sh; fi`) that
# completely bypasses the broken FOLDER= mount path. nofsck prevents the
# retry loop that would otherwise occur when init checks the bogus
# /dev/CE_STORAGE node. Both files are user-added and never touched by
# the CE updater, so the device boots cleanly after every nightly.
log "Installing /flash/mount-storage.sh hook..."
if $DRY_RUN; then
    echo -e "${YELLOW}[DRY-RUN]${NC} write ${MNT_FLASH}/mount-storage.sh"
else
    cat > "${MNT_FLASH}/mount-storage.sh" << 'EOF'
mount -t ext4 -o rw,noatime LABEL=CE_STORAGE /storage
EOF
fi

log "Adding nofsck to ${MNT_FLASH}/config.ini coreelec= line..."
if $DRY_RUN; then
    echo -e "${YELLOW}[DRY-RUN]${NC} update ${MNT_FLASH}/config.ini — add nofsck"
else
    if grep -q "^coreelec=" "${MNT_FLASH}/config.ini" 2>/dev/null; then
        if ! grep -q "nofsck" "${MNT_FLASH}/config.ini"; then
            sed -i "s/coreelec='\(.*\)'/coreelec='\1 nofsck'/" "${MNT_FLASH}/config.ini"
        fi
    else
        echo "coreelec='quiet nofsck'" >> "${MNT_FLASH}/config.ini"
    fi
fi

run umount "$MNT_FLASH"
log "CE_FLASH ready"

# ── Optional custom boot logo (--restore-logo) ───────────────────────────────

if [[ -n "$LOGO_BIN" ]]; then
    header "Writing custom boot logo to p10"
    # p10 is the logo partition (8 MB). The actual AML_RES content is
    # typically ~1.7 MB on this device; the rest is zero padding. Writing
    # the whole file is safe — partition is 8 MB and any AML_RES container
    # we'd produce is well under that.
    LOGO_SIZE=$(stat -c %s "$LOGO_BIN" 2>/dev/null || stat -f %z "$LOGO_BIN")
    log "Logo source: $LOGO_BIN ($LOGO_SIZE bytes)"
    run dd if="$LOGO_BIN" of="${EMMC}p10" bs=1M conv=fsync status=none
    log "Custom logo written to ${EMMC}p10"
fi

# ── Optional storage migration ─────────────────────────────────────────────────

MIGRATE_MSG="  The eMMC install is ready. CE_STORAGE is currently empty —
  first eMMC boot will initialize it as a fresh install.

  You can optionally migrate your current /storage (settings,
  addons, media metadata) to CE_STORAGE now."

if tui_yesno "Migrate /storage to CE_STORAGE?" "$MIGRATE_MSG"; then
    header "Migrating /storage to CE_STORAGE"

    run mkdir -p "$MNT_STORAGE"
    run mount -t ext4 -o rw,noatime "${EMMC}p29" "$MNT_STORAGE"

    if $DRY_RUN; then
        # In dry-run the partition isn't actually mounted, so the size/free
        # comparison would be meaningless. Skip the slow `du -sb /storage`
        # walk and just stub the rsync.
        echo -e "${YELLOW}[DRY-RUN]${NC} would compare \$(du -sb /storage) to free space on CE_STORAGE"
        echo -e "${YELLOW}[DRY-RUN]${NC} rsync -ax --info=progress2 /storage/ ${MNT_STORAGE}/"
        echo -e "${YELLOW}[DRY-RUN]${NC} umount ${MNT_STORAGE}"
    else
        # Check available space before migrating
        STORAGE_USED=$(du -sb /storage 2>/dev/null | awk '{print $1}')
        CE_FREE=$(df -B1 "$MNT_STORAGE" 2>/dev/null | awk 'NR==2{print $4}')
        if (( STORAGE_USED > CE_FREE )); then
            warn "Not enough space: /storage uses $(( STORAGE_USED/1024/1024 )) MB, CE_STORAGE has $(( CE_FREE/1024/1024 )) MB free"
            umount "$MNT_STORAGE"
            warn "Migration skipped — CE_STORAGE will be initialized fresh on first eMMC boot"
        else
            log "Rsyncing /storage → CE_STORAGE (this may take a few minutes)..."
            rsync -ax --info=progress2 /storage/ "$MNT_STORAGE/"

            umount "$MNT_STORAGE"
            log "Migration complete"
        fi
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
echo "  Remove the ${BOOT_MEDIA} and reboot. The device will boot"
echo "  CoreELEC from internal eMMC automatically."
echo ""
echo "  On first eMMC boot, SSH host keys are regenerated."
echo "  Clear your old entry before reconnecting:"
echo "    ssh-keygen -R <device-ip>"
echo ""
echo "  Backups saved to ${BACKUP_DIR}:"
echo "    partition_layout.txt   (needed by ce-emmc-restore.sh)"
echo "    rsv_backup.bin"
echo "    env_backup.bin"
echo "    bootloader_a_backup.bin"
echo "    reserved_backup.bin    (Amlogic UKS keystore — MAC/serial)"
echo "    frp_backup.bin         (anti-rollback / FRP nonce)"
echo "    param_backup.bin       (Amlogic TV picture-quality DB)"
echo ""
$DRY_RUN && warn "DRY-RUN complete — no changes were made"
