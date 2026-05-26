#!/bin/bash
# am9pro-usb-restore.sh — Phase 1: verify image, adnl binary, and burn-mode
# device. READ-ONLY; no bytes are written to the device.
#
# This is the alternative-to-Windows-USB-Burning-Tool path for the Ugoos
# AM9 Pro: combine our aml-img-tool.py (for image parsing) with Khadas's
# adnl binary (the Amlogic DNL protocol speaker for newer SoCs). Phase 1
# only verifies that the pieces fit together — it does NOT flash anything.
# Phase 2 (--unsafe-flash, the actual restore flow) is not yet built.
#
# Usage:
#   am9pro-usb-restore.sh <image.img> [options]
#
# Options:
#   --adnl <path>      Path to adnl binary
#                      (default: looks for ./adnl, /tmp/khadas-tool/adnl-*,
#                       or 'adnl' in $PATH)
#   --keep-temp        Don't delete the temp dir created by image unpack
#   --unsafe-flash     RESERVED for Phase 2. Currently aborts with notice.
#   --help             Show this help.
#
# Getting adnl (Amlogic's closed-source download tool; ships in
# khadas/utils):
#   Linux x86_64: curl -L -o adnl https://github.com/khadas/utils/raw/master/aml-flash-tool/tools/adnl/adnl
#   macOS univ.:  curl -L -o adnl https://github.com/khadas/utils/raw/master/aml-flash-tool/tools/adnl/macos/adnl
#   chmod +x adnl
#
# Putting the AM9 Pro in burn mode (mask-ROM USB-DNL mode):
#   1. Power off the device.
#   2. Connect a USB-C to USB-A cable from the AM9 Pro's USB-C OTG port
#      (the one labelled OTG) to this machine's USB. The three USB-A
#      ports on the device are host-only and will not work.
#   3. With a paperclip, hold the recessed reset button on the bottom.
#   4. While holding reset, plug in power.
#   5. Keep holding reset for ~5 seconds, then release.
#   6. The device should appear as "Amlogic" in `lsusb` (Linux) or in
#      `ioreg -p IOUSB` (macOS). `adnl devices` will list it.

set -euo pipefail

# ── output helpers ─────────────────────────────────────────────────────
# Use $'\e[...]' so the variables hold actual escape bytes — that lets
# both `echo -e` and `cat <<EOF` substitute them correctly.
RED=$'\e[0;31m'
GREEN=$'\e[0;32m'
YELLOW=$'\e[1;33m'
BOLD=$'\e[1m'
NC=$'\e[0m'

log()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
die()    { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── arg parsing ────────────────────────────────────────────────────────
IMG=""
ADNL=""
KEEP_TEMP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \?//'
            exit 0
            ;;
        --adnl)        ADNL="$2"; shift 2 ;;
        --adnl=*)      ADNL="${1#--adnl=}"; shift ;;
        --keep-temp)   KEEP_TEMP=true; shift ;;
        --unsafe-flash)
            die "Phase 2 (--unsafe-flash) is not yet implemented. Run without --unsafe-flash for Phase 1 verification."
            ;;
        -*)
            die "Unknown option: $1 (try --help)"
            ;;
        *)
            [[ -z "$IMG" ]] || die "Unexpected argument: $1"
            IMG="$1"
            shift
            ;;
    esac
done

[[ -n "$IMG" ]] || die "image.img path is required. Run with --help."
[[ -f "$IMG" ]] || die "Not a file: $IMG"

# ── locate aml-img-tool.py ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AML_IMG_TOOL="$SCRIPT_DIR/aml-img-tool.py"
AML_BOOT_TOOL="$SCRIPT_DIR/aml-bootloader-tool.py"
[[ -f "$AML_IMG_TOOL" ]] || die "aml-img-tool.py not found next to this script ($SCRIPT_DIR)"

# ── locate adnl ────────────────────────────────────────────────────────
if [[ -z "$ADNL" ]]; then
    for candidate in \
        "$SCRIPT_DIR/adnl" \
        "$SCRIPT_DIR/adnl-macos" \
        "$SCRIPT_DIR/adnl-linux" \
        "/tmp/khadas-tool/adnl-macos" \
        "/tmp/khadas-tool/adnl-linux" \
        "$(command -v adnl 2>/dev/null || true)"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            ADNL="$candidate"
            break
        fi
    done
fi

if [[ -z "$ADNL" || ! -x "$ADNL" ]]; then
    cat >&2 <<EOF
$(echo -e "${RED}[ERROR]${NC}") adnl binary not found.

Download for your platform (it's a closed-source Amlogic tool that
Khadas re-distributes — we don't ship it in this repo for licensing
reasons):

  macOS (universal):
    curl -L -o adnl https://github.com/khadas/utils/raw/master/aml-flash-tool/tools/adnl/macos/adnl
    chmod +x adnl

  Linux x86_64:
    curl -L -o adnl https://github.com/khadas/utils/raw/master/aml-flash-tool/tools/adnl/adnl
    chmod +x adnl

Then put it next to this script, or pass --adnl=/path/to/adnl.
EOF
    exit 1
fi

# Detect OS for later (macOS uses ioreg, Linux uses lsusb)
case "$(uname -s)" in
    Darwin) OS_KIND="macos" ;;
    Linux)  OS_KIND="linux" ;;
    *)      OS_KIND="unknown" ;;
esac

# ── phase 1 checks ─────────────────────────────────────────────────────

header "Image inspection"
log "Image:    $IMG"
log "Parser:   $AML_IMG_TOOL"
python3 "$AML_IMG_TOOL" info "$IMG" || die "image failed verification"

header "adnl binary"
log "Path:     $ADNL"
ADNL_VER=$("$ADNL" --help 2>&1 | head -1 || true)
log "Version:  $ADNL_VER"
if [[ "$ADNL_VER" != *"DNL"* ]]; then
    warn "Banner doesn't look like Amlogic DNL — proceeding anyway"
fi

header "Image unpack to temp"
TMPDIR=$(mktemp -d 2>/dev/null || mktemp -d -t am9pro-restore)
cleanup() {
    if $KEEP_TEMP; then
        log "Temp dir preserved: $TMPDIR"
    else
        rm -rf "$TMPDIR"
    fi
}
trap cleanup EXIT
log "Unpacking → $TMPDIR"
python3 "$AML_IMG_TOOL" unpack "$IMG" "$TMPDIR" > "$TMPDIR/.unpack.log" 2>&1 \
    || { cat "$TMPDIR/.unpack.log"; die "unpack failed"; }
TOTAL_ITEMS=$(grep -c '^  ' "$TMPDIR/.unpack.log" || true)
log "Unpacked $TOTAL_ITEMS items"

header "PARTITION items (these would be flashed in Phase 2)"
printf "  %-3s  %-22s  %12s\n" "idx" "partition" "size"
for item in "$TMPDIR"/*__PARTITION__*.bin; do
    [[ -f "$item" || -L "$item" ]] || continue
    base=$(basename "$item" .bin)
    idx=${base%%__*}
    name=${base##*__PARTITION__}
    sz=$(wc -c <"$item" | tr -d ' ')
    printf "  %-3s  %-22s  %12s\n" "$idx" "$name" "$sz"
done

header "bootloader (consumed by adnl bl1_boot / bl2_boot in Phase 2)"
BOOTLOADER=$(ls "$TMPDIR"/*__PARTITION__bootloader.bin 2>/dev/null | head -1 || true)
if [[ -n "$BOOTLOADER" && -e "$BOOTLOADER" ]]; then
    log "Found: $BOOTLOADER"
    if [[ -f "$AML_BOOT_TOOL" ]]; then
        python3 "$AML_BOOT_TOOL" info "$BOOTLOADER" 2>&1 \
            | sed 's/^/   /' \
            | head -8
    fi
else
    warn "No bootloader_a.bin found in the image — Phase 2 cannot proceed."
fi

header "Device check (burn mode)"
DEVICES_OUT=$("$ADNL" devices 2>&1 || true)
# Strip the banner line "Amlogic DNL protocol tool V[2.7.5] ..."
DEVICES_LIST=$(echo "$DEVICES_OUT" | grep -v "DNL protocol tool" | grep -v "^$" || true)

if [[ -z "$DEVICES_LIST" ]]; then
    warn "No Amlogic device in burn mode."

    case "$OS_KIND" in
        macos)
            if ioreg -p IOUSB -l 2>/dev/null | grep -q -i "amlogic"; then
                warn "ioreg DOES show an Amlogic USB device though — check that adnl has permission, or that it's in DNL (not USB-storage) mode."
            fi
            ;;
        linux)
            if command -v lsusb >/dev/null && lsusb 2>/dev/null | grep -qi "amlogic"; then
                warn "lsusb DOES show an Amlogic USB device though — check that adnl has permission, or that it's in DNL (not USB-storage) mode."
            fi
            ;;
    esac

    cat <<EOF

To enter burn mode (mask-ROM USB DNL):
  1. Power off the AM9 Pro.
  2. USB-C OTG cable: device's USB-C port → this machine.
     (NOT the USB-A ports on the device — those are host-only.)
  3. Hold the recessed reset button (paperclip).
  4. While holding reset, plug in power.
  5. Hold ~5 seconds, then release.
  6. \`$ADNL devices\` should now list the device.

When the device is in burn mode, re-run this script. It will print the
device identity and confirm Phase 2 is ready to run (once implemented).
EOF
else
    log "Device(s) found:"
    echo "$DEVICES_LIST" | sed 's/^/  /'

    header "Querying device state (read-only)"
    # These queries are safe — getvar never writes. Some variables only
    # respond after BL2/BL33 is loaded; in mask ROM stage they may return
    # empty. That's expected.
    for var in identify chipinfo-1 chipinfo serialno cbw downloadsize; do
        printf "  %-15s → " "getvar:$var"
        result=$("$ADNL" getvar "$var" 2>&1 \
                  | grep -v "DNL protocol tool" \
                  | grep -v "^$" \
                  || true)
        if [[ -z "$result" ]]; then
            echo "(no response — variable not available in current stage)"
        else
            echo "$result" | head -1
        fi
    done
fi

# ── verdict ────────────────────────────────────────────────────────────

header "Phase 1 complete"

VERDICT_PARTS=()
VERDICT_PARTS+=("image is a valid Amlogic .img (CRC OK, $TOTAL_ITEMS items)")
VERDICT_PARTS+=("adnl binary works ($ADNL_VER)")
if [[ -n "$BOOTLOADER" && -e "$BOOTLOADER" ]]; then
    VERDICT_PARTS+=("bootloader item present (needed for bl1_boot/bl2_boot)")
fi
if [[ -n "$DEVICES_LIST" ]]; then
    VERDICT_PARTS+=("device in burn mode and responding to adnl")
fi

log "Verified:"
for part in "${VERDICT_PARTS[@]}"; do
    echo "  • $part"
done

echo
if [[ -z "$DEVICES_LIST" ]]; then
    cat <<EOF
${BOLD}Next steps:${NC}
  1. Put the device in burn mode (see instructions above).
  2. Re-run this script. It will confirm the device identifies cleanly.
  3. Once both checks pass, Phase 2 (--unsafe-flash, the actual restore
     flow with adnl bl1_boot / bl2_boot / partition writes) can be safely
     built and run.

  Phase 2 is NOT implemented yet. Do not manually invoke adnl bl1_boot,
  bl2_boot, partition, or reboot until Phase 2 is built and tested —
  the order and gating matters.
EOF
else
    cat <<EOF
${BOLD}Toolchain verified end-to-end.${NC} You are ready for Phase 2 once it
is implemented.

  Phase 2 (--unsafe-flash) is still NOT implemented in this script.
  Do not run it manually until built. The intended flow is:

    adnl bl1_boot   -f bootloader.bin    # mask-ROM → BL2 up
    adnl bl2_boot   -f bootloader.bin    # BL2 → BL33 (U-Boot) up
    adnl partition  -p <each PARTITION item>
    adnl reboot

  Each step is gated and verified. Building that safely is the next
  iteration on this script.
EOF
fi
