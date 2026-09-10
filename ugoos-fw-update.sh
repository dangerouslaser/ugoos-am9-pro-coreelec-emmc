#!/bin/bash
# ugoos-fw-update.sh — update the Ugoos Android firmware in place, from CoreELEC
#
# For Ugoos S905X5 boxes running CoreELEC (from eMMC via ce-emmc-install.sh,
# or from SD/USB). Takes a factory `AM9PRO_X.Y.Z.img` and writes everything
# the Amlogic USB Burning Tool would write to the eMMC — Android partitions,
# the Android DTB in `reserved`, and the bootloader in the eMMC hardware boot
# partitions boot0/boot1 — without USB, Windows, or leaving CoreELEC.
# CE_FLASH / CE_STORAGE and the GPT are never touched.
#
# Why you'd want this: CoreELEC nightlies can require a newer Ugoos firmware
# (e.g. nightly 20260910 needs AM9 Pro firmware 2.2.0 — the new DTB changed
# interrupt trigger types and the running bootloader/BL31 must match).
#
# One-liner, on the box as root (the .img must already be on the box or
# reachable over HTTP — Ugoos publishes firmware on mega.nz, which curl
# can't fetch; download it on a PC first and scp it to /storage):
#
#   curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
#       | bash -s -- --yes /storage/AM9PRO_2.2.0.img
#
# Or from your computer:
#
#   ssh root@<box> 'curl -fsSL <that url> | bash -s -- --yes /storage/AM9PRO_2.2.0.img'
#
# Usage:
#   ugoos-fw-update.sh [options] <IMG-path-or-http(s)-URL>
#
#   --check            Only report what differs between the eMMC and the image
#                      (also the thing to run after the reboot to confirm).
#   --dry-run          Print the write plan and stop. No writes.
#   --yes              Don't ask for confirmation (required when there is no
#                      terminal, e.g. `ssh host 'curl … | bash'`).
#   --no-reboot        Flash but don't reboot afterwards.
#   --no-dtb           Leave the Android DTB slots in `reserved` alone.
#   --no-boot-area     Leave eMMC boot0/boot1 alone. The running bootloader
#                      then stays at the old version — only for experiments.
#   --skip-boot1       Write boot0 only; keep boot1 as the previous bootloader.
#                      Re-run without this flag after a successful reboot.
#   --sha1 HEX         Expected SHA1 of the .img (checked before anything else).
#   --allow-unknown-image
#                      Proceed with an image whose SHA1 isn't in the built-in
#                      list (you verified it yourself).
#   --allow-untested-board
#                      Proceed on a board this tool hasn't been exercised on
#                      (SK4 / SK4 Pro: same partition layout, not yet tested).
#   --force            Flash even if the eMMC already matches the image.
#   --ref REF          Git ref to fetch the tools from (default: main).
#   --tools-dir DIR    Where to keep the tools (default: /storage/.ugoos-fw-update).
#
# What it runs, in order:
#   1. preflight: root, CoreELEC, python3/parted, board id from /flash/dtb.img
#   2. fetch burn/aml-emmc-burn.py + lib/aml_img.py from the repo (or use the
#      copies next to this script when run from a checkout)
#   3. locate/download the .img, verify its SHA1, check the image's SoC
#      family against the board
#   4. aml-emmc-burn.py --verify-only   (what differs; "nothing" → done)
#   5. aml-emmc-burn.py --dry-run       (the plan)
#   6. aml-emmc-burn.py --yes-i-mean-it [--reboot]  under nohup, log in
#      /storage/ugoos-fw-update.log — partitions, then DTB slots, then boot0,
#      then boot1, every write read back and hash-checked
#
# Recovery: a bad boot0 falls back to boot1 (unless you wrote both and both
# are bad); the previous firmware .img can be re-flashed with the same tool
# from CoreELEC on SD/USB; and the Amlogic BootROM's USB burn mode is in mask
# ROM, so the USB Burning Tool (or burn/aml-dnl-burn.py) always works.
#
# Not supported by CoreELEC or Ugoos. Tested on: AM9 Pro, CoreELEC on eMMC,
# firmware 2.1.0 → 2.2.0 (2026-09-10).

set -euo pipefail

REPO_RAW_BASE="https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc"
REF="main"
TOOLS_DIR="/storage/.ugoos-fw-update"
LOG="/storage/ugoos-fw-update.log"
DOWNLOAD_DIR="/storage"

# coreelec-dt-id | board name | tested?
SUPPORTED_BOARDS=(
    "s6_s905x5_ugoos_am9_pro|Ugoos AM9 Pro|tested"
    "s7d_s905x5m_ugoos_sk4|Ugoos SK4 / SK4 Pro|untested"
)

# SHA1 of factory images we have run this on (or parsed). Add yours here.
KNOWN_IMAGES=(
    "a0b9adc543788205de03b1a04cfba88be1dc2300|AM9PRO_2.2.0.img|s6"
    "5c0920b3f9081e084e3370525d056411a7284847|AM9PRO_2.1.0.img|s6"
    "8b5734fe70bd7168914ae1e7880ae6ab25a026b9|AM9PRO_2.0.9.img|s6"
)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header() { echo; echo -e "${GREEN}== $* ==${NC}"; }

usage() { sed -n '2,70p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

MODE="flash"
ASSUME_YES=0
REBOOT=1
FORCE=0
ALLOW_UNKNOWN_IMAGE=0
ALLOW_UNTESTED_BOARD=0
EXPECT_SHA1=""
EXTRA_FLAGS=()
IMG_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)        MODE="check" ;;
        --dry-run)      MODE="dry-run" ;;
        --yes)          ASSUME_YES=1 ;;
        --no-reboot)    REBOOT=0 ;;
        --no-dtb|--no-boot-area|--skip-boot1) EXTRA_FLAGS+=("$1") ;;
        --sha1)         EXPECT_SHA1="${2:-}"; shift ;;
        --allow-unknown-image)  ALLOW_UNKNOWN_IMAGE=1 ;;
        --allow-untested-board) ALLOW_UNTESTED_BOARD=1 ;;
        --force)        FORCE=1 ;;
        --ref)          REF="${2:-}"; shift ;;
        --tools-dir)    TOOLS_DIR="${2:-}"; shift ;;
        -h|--help)      usage 0 ;;
        -*)             die "Unknown option: $1 (try --help)" ;;
        *)              [[ -z "$IMG_ARG" ]] || die "Only one image argument allowed"; IMG_ARG="$1" ;;
    esac
    shift
done
[[ -n "$IMG_ARG" ]] || { echo "Missing image argument." >&2; usage 1; }

# ── preflight ────────────────────────────────────────────────────────────────
header "Preflight"
[[ "$(id -u)" == "0" ]] || die "Must be run as root"
grep -qs '^ID="coreelec"' /etc/os-release || die "This only runs on CoreELEC"
for cmd in python3 parted curl sha1sum; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required tool missing: $cmd"
done
[[ -b /dev/mmcblk0 ]] || die "No eMMC at /dev/mmcblk0"
[[ -d /sys/block/mmcblk0boot0 ]] || die "No eMMC hardware boot partitions (mmcblk0boot0) — unexpected board"

# Board id: the `coreelec-dt-id` root property of the live DTB (same method
# as ce-emmc-install.sh — stable across CE's runtime dtb.img rewrites).
fdt_root_prop() {
    python3 - "$1" "$2" <<'PYEOF' 2>/dev/null || true
import struct, sys
path, want = sys.argv[1], sys.argv[2]
blob = open(path, 'rb').read()
if len(blob) < 40 or struct.unpack('>I', blob[:4])[0] != 0xd00dfeed:
    sys.exit(1)
off_struct, off_strings = struct.unpack('>2I', blob[8:16])
size_strings = struct.unpack('>I', blob[32:36])[0]
strings = blob[off_strings:off_strings + size_strings]
p, depth = off_struct, 0
while p + 4 <= len(blob):
    (tok,) = struct.unpack('>I', blob[p:p + 4]); p += 4
    if tok == 1:
        depth += 1
        p = (blob.index(b'\0', p) + 1 + 3) & ~3
    elif tok == 2:
        depth -= 1
        if depth <= 0:
            break
    elif tok == 3:
        ln, name_off = struct.unpack('>2I', blob[p:p + 8]); p += 8
        val = blob[p:p + ln]
        p = (p + ln + 3) & ~3
        if depth == 1:
            name = strings[name_off:strings.index(b'\0', name_off)].decode()
            if name == want:
                print(val.split(b'\0')[0].decode('utf-8', 'replace'))
                break
    elif tok == 4:
        continue
    else:
        break
PYEOF
}

DT_ID="$(fdt_root_prop /flash/dtb.img coreelec-dt-id)"
[[ -n "$DT_ID" ]] || die "Could not read coreelec-dt-id from /flash/dtb.img"
BOARD_NAME=""; BOARD_TESTED=""
for entry in "${SUPPORTED_BOARDS[@]}"; do
    IFS='|' read -r id name tested <<<"$entry"
    if [[ "$DT_ID" == "$id" ]]; then BOARD_NAME="$name"; BOARD_TESTED="$tested"; fi
done
[[ -n "$BOARD_NAME" ]] || die "Board '$DT_ID' is not in SUPPORTED_BOARDS"
if [[ "$BOARD_TESTED" != "tested" && $ALLOW_UNTESTED_BOARD -eq 0 ]]; then
    die "$BOARD_NAME ($DT_ID) has not been exercised with this tool. Re-run with --allow-untested-board if you accept that."
fi
BOARD_SOC="${DT_ID%%_*}"              # s6 / s7d
CUR_BL="$(tr ' ' '\n' </proc/cmdline | sed -n 's/^androidboot.bootloader=//p')"
log "Board: $BOARD_NAME ($DT_ID, $BOARD_TESTED)"
log "CoreELEC: $(sed -n 's/^VERSION=//p' /etc/os-release | tr -d '"')"
log "Running bootloader: ${CUR_BL:-unknown}"

# ── tools ────────────────────────────────────────────────────────────────────
header "Tools"
mkdir -p "$TOOLS_DIR"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SELF_DIR" && -f "$SELF_DIR/burn/aml-emmc-burn.py" && -f "$SELF_DIR/lib/aml_img.py" ]]; then
    cp "$SELF_DIR/burn/aml-emmc-burn.py" "$SELF_DIR/lib/aml_img.py" "$TOOLS_DIR/"
    log "Using tools from local checkout: $SELF_DIR"
else
    for f in burn/aml-emmc-burn.py lib/aml_img.py; do
        curl -fsSL --retry 3 -o "$TOOLS_DIR/$(basename "$f").tmp" "$REPO_RAW_BASE/$REF/$f" \
            || die "Could not download $f from $REPO_RAW_BASE/$REF"
        mv "$TOOLS_DIR/$(basename "$f").tmp" "$TOOLS_DIR/$(basename "$f")"
    done
    log "Fetched aml-emmc-burn.py + aml_img.py (ref: $REF)"
fi
python3 -m py_compile "$TOOLS_DIR/aml-emmc-burn.py" "$TOOLS_DIR/aml_img.py" || die "Downloaded tools do not compile"
BURN="$TOOLS_DIR/aml-emmc-burn.py"

# ── image ────────────────────────────────────────────────────────────────────
header "Image"
IMG=""
if [[ "$IMG_ARG" =~ ^https?:// ]]; then
    IMG="$DOWNLOAD_DIR/$(basename "${IMG_ARG%%\?*}")"
    [[ "$IMG" == *.img ]] || IMG="$IMG.img"
    if [[ -f "$IMG" ]]; then
        log "Already downloaded: $IMG (resuming if incomplete)"
    fi
    size="$(curl -fsSLI "$IMG_ARG" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="content-length:"{s=$2} END{print s}')"
    if [[ -n "${size:-}" ]]; then
        free_kb="$(df -Pk "$DOWNLOAD_DIR" | awk 'NR==2{print $4}')"
        have_kb=$(( $(stat -c %s "$IMG" 2>/dev/null || echo 0) / 1024 ))
        need_kb=$(( size / 1024 - have_kb + 65536 ))
        [[ "$free_kb" -gt "$need_kb" ]] || die "Not enough space in $DOWNLOAD_DIR ($free_kb KB free, need ~$need_kb KB)"
    fi
    log "Downloading $IMG_ARG → $IMG"
    curl -fL --retry 3 -C - -o "$IMG" "$IMG_ARG" || die "Download failed"
else
    IMG="$IMG_ARG"
    [[ -f "$IMG" ]] || die "Image not found: $IMG"
fi

log "Hashing $IMG …"
IMG_SHA1="$(sha1sum "$IMG" | cut -d' ' -f1)"
IMG_KNOWN=""; IMG_SOC=""
for entry in "${KNOWN_IMAGES[@]}"; do
    IFS='|' read -r h n soc <<<"$entry"
    if [[ "$h" == "$IMG_SHA1" ]]; then IMG_KNOWN="$n"; IMG_SOC="$soc"; fi
done
if [[ -n "$EXPECT_SHA1" && "${EXPECT_SHA1,,}" != "$IMG_SHA1" ]]; then
    die "SHA1 mismatch: image is $IMG_SHA1, expected $EXPECT_SHA1"
fi
if [[ -n "$IMG_KNOWN" ]]; then
    log "Image: $IMG_KNOWN (sha1 $IMG_SHA1, known good)"
elif [[ -n "$EXPECT_SHA1" ]]; then
    log "Image sha1 $IMG_SHA1 matches --sha1"
elif [[ $ALLOW_UNKNOWN_IMAGE -eq 1 ]]; then
    warn "Image sha1 $IMG_SHA1 is not in the known list — proceeding because --allow-unknown-image"
else
    die "Image sha1 $IMG_SHA1 is not in this script's known list. Verify it against Ugoos' download, then re-run with --sha1 $IMG_SHA1 (or --allow-unknown-image)."
fi

# The bootloader's @AMLBOOT manifest names the SoC family ("S6-s905x5-…",
# "S7D-…"); it must agree with the board we're on.
IMG_BOARD_ID="$(python3 - "$TOOLS_DIR" "$IMG" <<'PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
from aml_img import AmlogicImage
img = AmlogicImage(sys.argv[2])
for name in ("bootloader", "bootloader_a"):
    try:
        it = img.find(name, "PARTITION")
    except KeyError:
        continue
    blob = bytes(img.read_at(it, 0, it.size))
    i = blob.find(b"@AMLBOOT")
    if i >= 0:
        print(blob[i + 12:i + 32].rstrip(b"\0").decode("ascii", "replace"))
    break
PYEOF
)"
[[ -n "$IMG_BOARD_ID" ]] || die "Could not read the @AMLBOOT board id from the image — is this an Amlogic factory .img?"
IMG_SOC_FROM_BL="$(echo "${IMG_BOARD_ID%%-*}" | tr 'A-Z' 'a-z')"
[[ "$IMG_SOC_FROM_BL" == "$BOARD_SOC" ]] || die "Image is for SoC '$IMG_SOC_FROM_BL' ($IMG_BOARD_ID) but this board is '$BOARD_SOC' ($DT_ID)"
if [[ -n "$IMG_SOC" && "$IMG_SOC" != "$BOARD_SOC" ]]; then
    die "Known image $IMG_KNOWN is for '$IMG_SOC' boards, this is '$BOARD_SOC'"
fi
log "Image bootloader: $IMG_BOARD_ID (SoC family matches board)"

# ── compare ──────────────────────────────────────────────────────────────────
header "Current eMMC vs image"
set +e
python3 "$BURN" "$IMG" --verify-only "${EXTRA_FLAGS[@]}"
VERIFY_RC=$?
set -e
case "$VERIFY_RC" in
    0) log "eMMC already matches this image everywhere."
       if [[ "$MODE" == "check" ]]; then exit 0; fi
       if [[ $FORCE -eq 0 ]]; then log "Nothing to do (use --force to rewrite anyway)."; exit 0; fi ;;
    1) log "Differences found — an update is needed." ;;
    2) warn "Some items have no hash in the image; they'll be written blind." ;;
    *) die "verify-only failed (rc=$VERIFY_RC)" ;;
esac
[[ "$MODE" == "check" ]] && exit "$VERIFY_RC"

header "Plan"
python3 "$BURN" "$IMG" --dry-run "${EXTRA_FLAGS[@]}"
[[ "$MODE" == "dry-run" ]] && exit 0

# ── confirm ──────────────────────────────────────────────────────────────────
echo
echo "About to write firmware to the eMMC of this $BOARD_NAME:"
echo "  image:        $IMG"
echo "  bootloader:   ${CUR_BL:-?}  →  $IMG_BOARD_ID"
echo "  steps:        Android partitions → DTB slots → boot0 → boot1$( [[ $REBOOT -eq 1 ]] && echo ' → reboot' )"
[[ ${#EXTRA_FLAGS[@]} -gt 0 ]] && echo "  flags:        ${EXTRA_FLAGS[*]}"
echo "  log:          $LOG"
echo "CE_FLASH / CE_STORAGE / GPT are not touched. Every write is read back and hash-checked."
echo
if [[ $ASSUME_YES -eq 0 ]]; then
    # /dev/tty exists even in a non-interactive ssh session; opening it is the real test.
    if ( : </dev/tty ) 2>/dev/null; then
        read -r -p "Type YES to continue: " answer </dev/tty || answer=""
        [[ "$answer" == "YES" ]] || die "Aborted."
    else
        die "No terminal for confirmation — re-run with --yes"
    fi
fi

# ── flash ────────────────────────────────────────────────────────────────────
header "Flashing"
FLAGS=(--yes-i-mean-it "${EXTRA_FLAGS[@]}")
[[ $REBOOT -eq 1 ]] && FLAGS+=(--reboot)
{
    echo "=== $(date '+%F %T') ugoos-fw-update.sh: $BOARD_NAME ($DT_ID) image=$IMG sha1=$IMG_SHA1 flags=${FLAGS[*]}"
} >>"$LOG"
# nohup + wait: the writes finish even if this SSH session drops mid-way.
nohup python3 "$BURN" "$IMG" "${FLAGS[@]}" >>"$LOG" 2>&1 &
FLASH_PID=$!
set +e
wait "$FLASH_PID"
FLASH_RC=$?
set -e
sync
tail -n 40 "$LOG"
if [[ $FLASH_RC -ne 0 ]]; then
    die "Flash FAILED (rc=$FLASH_RC). See $LOG. Do not power off until you have read it: the boot area is written last, so a failure before it leaves the previous bootloader in place."
fi
echo
if [[ $REBOOT -eq 1 ]]; then
    log "Done — rebooting. After it comes back, confirm with:"
else
    log "Done — reboot when convenient, then confirm with:"
fi
echo "    tr ' ' '\\n' </proc/cmdline | grep androidboot.bootloader"
echo "    python3 $BURN $IMG --verify-only"
