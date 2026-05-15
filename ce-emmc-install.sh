#!/bin/bash
# ce-emmc-install.sh — CoreELEC eMMC installer for Ugoos AM9 Pro
#
# Workaround until ceemmc adds support for s6_s905x5_ugoos_am9_pro.
# Must be run from CoreELEC booted off an SD card.
#
# What this does:
#   1. Backs up env (p2) and bootloader_a (p7) to /storage
#   2. Deletes unused Android partitions: super (p27), rsv (p28), userdata (p29)
#   3. Creates CE_FLASH (512 MB FAT32) and CE_STORAGE (remaining ~57.9 GB ext4)
#   4. Copies all boot files from the SD card's /flash to CE_FLASH
#   5. Installs mount-storage.sh hook (bypasses broken FOLDER= device node mechanism)
#   6. Adds nofsck to config.ini (avoids 10s boot delay from phantom fsck)
#   7. Optionally migrates your current /storage to CE_STORAGE

set -euo pipefail

EMMC="/dev/mmcblk0"
SD_FLASH="/flash"
MNT_FLASH="/var/ce_flash"
MNT_STORAGE="/var/ce_storage"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

log()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
die()    { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}--- $* ---${NC}"; }

# ── Preflight ────────────────────────────────────────────────────────────────

header "Preflight checks"

# Must run as root
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
FLASH_SOURCE=$(findmnt -n -o SOURCE "$SD_FLASH" 2>/dev/null || true)
[[ "$FLASH_SOURCE" == *"mmcblk1"* ]] || \
    die "Not booting from SD card — /flash is on '${FLASH_SOURCE:-unknown}'. Insert SD card and reboot."

log "Boot source: SD card ($FLASH_SOURCE)"

# eMMC must be present
[[ -b "$EMMC" ]] || die "eMMC not found at $EMMC"
log "eMMC: $EMMC present"

# Required tools
for tool in parted mkfs.fat mkfs.ext4 rsync dd blkid mknod findmnt; do
    command -v "$tool" >/dev/null 2>&1 || die "Required tool not found: $tool"
done
log "Required tools: all present"

# Create device nodes so we can inspect the current partition layout
for i in $(seq 1 29); do
    mknod "/dev/mmcblk0p${i}" b 179 "$i" 2>/dev/null || true
done

# Check for expected Android partition layout (p27=super, p28=rsv, p29=userdata)
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^27:" || \
    die "Partition 27 not found — unexpected layout. Has this already been modified?"
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^28:" || \
    die "Partition 28 not found — unexpected layout."
parted -sm "$EMMC" unit B print 2>/dev/null | grep -q "^29:" || \
    die "Partition 29 not found — unexpected layout."

# Abort if CE_FLASH already exists
if blkid /dev/mmcblk0p27 2>/dev/null | grep -q "CE_FLASH"; then
    die "CE_FLASH already found on eMMC — CoreELEC appears to already be installed."
fi

log "Partition layout: 29-partition Android layout confirmed"

# ── Confirm ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  CoreELEC eMMC Installer — Ugoos AM9 Pro${NC}"
echo -e "${BOLD}════════════════════════════════════════════════════${NC}"
echo ""
echo "  The following changes will be made to the eMMC:"
echo ""
echo "    DELETE  p27  super      3.1 GB   (Android system — empty)"
echo "    DELETE  p28  rsv         64 MB   (reserved — empty)"
echo "    DELETE  p29  userdata   54.4 GB  (encrypted remnant)"
echo ""
echo "    CREATE  p27  CE_FLASH   512 MB   FAT32  (boot partition)"
echo "    CREATE  p28  CE_STORAGE ~57.9 GB ext4   (storage)"
echo ""
echo "  Partitions p1–p26 are NOT touched."
echo "  boot0/boot1 are hardware write-protected and are safe."
echo ""
warn "This cannot be undone. The userdata partition is encrypted"
warn "and its contents cannot be recovered regardless."
echo ""
read -rp "  Type YES to proceed: " confirm
echo ""
[[ "$confirm" == "YES" ]] || { echo "Aborted."; exit 0; }

# ── Backups ──────────────────────────────────────────────────────────────────

header "Backing up critical partitions"

dd if=/dev/mmcblk0p2 of=/storage/env_backup.bin bs=1M status=none
log "env (p2) → /storage/env_backup.bin"

dd if=/dev/mmcblk0p7 of=/storage/bootloader_a_backup.bin bs=1M status=none
log "bootloader_a (p7) → /storage/bootloader_a_backup.bin"

# ── Repartition ──────────────────────────────────────────────────────────────

header "Repartitioning eMMC"

# Find where p27 (super) starts — CE_FLASH will start at the same position
SUPER_START_B=$(parted -sm "$EMMC" unit B print 2>/dev/null \
    | awk -F: '/^27:/{gsub(/B/,""); print $2}')
[[ -n "$SUPER_START_B" ]] || die "Could not determine start position of partition 27"

# Work in MiB — Android aligns to MiB boundaries
SUPER_START_MIB=$((SUPER_START_B / 1024 / 1024))
CE_FLASH_END_MIB=$((SUPER_START_MIB + 512))

log "Deleting partitions 27 (super), 28 (rsv), 29 (userdata)..."
parted -s "$EMMC" rm 29 rm 28 rm 27

log "Creating CE_FLASH (${SUPER_START_MIB}MiB – ${CE_FLASH_END_MIB}MiB)..."
parted -s "$EMMC" mkpart CE_FLASH fat32 "${SUPER_START_MIB}MiB" "${CE_FLASH_END_MIB}MiB"

log "Creating CE_STORAGE (${CE_FLASH_END_MIB}MiB – 100%)..."
parted -s "$EMMC" mkpart CE_STORAGE ext4 "${CE_FLASH_END_MIB}MiB" "100%"

sleep 1

# Recreate device nodes with correct minor numbers
rm -f /dev/mmcblk0p27 /dev/mmcblk0p28 /dev/mmcblk0p29 2>/dev/null || true
mknod /dev/mmcblk0p27 b 179 27
mknod /dev/mmcblk0p28 b 179 28

# Verify labels were set correctly
blkid /dev/mmcblk0p27 >/dev/null 2>&1 || true  # blkid may exit non-zero before format

# ── Format ───────────────────────────────────────────────────────────────────

header "Formatting partitions"

log "Formatting CE_FLASH as FAT32..."
mkfs.fat -F 32 -n CE_FLASH /dev/mmcblk0p27

log "Formatting CE_STORAGE as ext4..."
mkfs.ext4 -q -L CE_STORAGE /dev/mmcblk0p28

# ── Install boot files ───────────────────────────────────────────────────────

header "Installing boot files"

mkdir -p "$MNT_FLASH"
mount /dev/mmcblk0p27 "$MNT_FLASH"

log "Copying files from ${SD_FLASH}..."
# Exclude fs-resize.log (SD-specific) and any stale mount-storage.sh
cp -a "${SD_FLASH}/." "${MNT_FLASH}/"
rm -f "${MNT_FLASH}/fs-resize.log"

# Install mount-storage.sh hook.
# The initrd sources this file instead of the normal mount_part() logic,
# bypassing the FOLDER=/dev/CE_STORAGE mechanism which requires a device node
# that the initrd never creates (no udev rules in CoreELEC initrd).
log "Installing mount-storage.sh hook..."
cat > "${MNT_FLASH}/mount-storage.sh" << 'EOF'
mount -t ext4 -o rw,noatime LABEL=CE_STORAGE /storage
EOF

# Add nofsck to config.ini.
# With cfgload unmodified, the kernel cmdline still contains
# disk=FOLDER=/dev/CE_STORAGE. The initrd adds /dev/CE_STORAGE to its fsck
# list, then retries 20 times at 0.5s each when the device node never appears.
# nofsck skips this entirely.
log "Updating config.ini (adding nofsck)..."
if grep -q "^coreelec=" "${MNT_FLASH}/config.ini" 2>/dev/null; then
    # Add nofsck to existing coreelec line if not already there
    if ! grep -q "nofsck" "${MNT_FLASH}/config.ini"; then
        sed -i "s/coreelec='\(.*\)'/coreelec='\1 nofsck'/" "${MNT_FLASH}/config.ini"
    fi
else
    echo "coreelec='quiet nofsck'" >> "${MNT_FLASH}/config.ini"
fi

umount "$MNT_FLASH"
log "CE_FLASH ready"

# ── Optional storage migration ────────────────────────────────────────────────

echo ""
echo "  The eMMC install is ready. CE_STORAGE is currently empty —"
echo "  first eMMC boot will initialize it as a fresh install."
echo ""
echo "  You can optionally migrate your current /storage (settings,"
echo "  addons, media metadata) to CE_STORAGE now."
echo ""
read -rp "  Migrate /storage to CE_STORAGE? [YES/no]: " migrate_ans
echo ""

if [[ "${migrate_ans}" == "YES" ]]; then
    header "Migrating /storage to CE_STORAGE"

    mkdir -p "$MNT_STORAGE"
    mount -t ext4 -o rw,noatime /dev/mmcblk0p28 "$MNT_STORAGE"

    log "Rsyncing /storage → CE_STORAGE (this may take a few minutes)..."
    rsync -ax --info=progress2 /storage/ "$MNT_STORAGE/"

    umount "$MNT_STORAGE"
    log "Migration complete"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

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
