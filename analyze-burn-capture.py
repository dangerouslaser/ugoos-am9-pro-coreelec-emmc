#!/usr/bin/env python3
"""Extract the Amlogic USB DNL protocol command/response sequence from a
USBPcap capture of a USB Burning Tool flash session.

Strategy: walk every bulk transfer, decode payloads that look like ASCII
fastboot commands ('getvar:*', 'download:*', 'OKAY*', 'DATA*', etc.), and
emit a chronological transcript with sizes. Large binary chunks (the image
data) are summarized, not dumped.
"""
import sys
import subprocess

PCAP = sys.argv[1] if len(sys.argv) > 1 else \
    "captures/burn-2.1.0-windows-full.pcapng"

# Pull the relevant fields from every bulk transfer in order.
fields = [
    "frame.number",
    "frame.time_relative",
    "usb.device_address",
    "usb.endpoint_address.direction",
    "usb.data_len",
    "usb.capdata",
]
cmd = ["tshark", "-r", PCAP,
       "-Y", "usb.transfer_type == 0x03",
       "-T", "fields"] + [a for f in fields for a in ("-e", f)]

p = subprocess.run(cmd, capture_output=True, check=True)
lines = p.stdout.decode("utf-8", errors="replace").splitlines()

PRINTABLE = set(range(0x20, 0x7f))


def decode(hex_data):
    if not hex_data:
        return None
    raw = bytes.fromhex(hex_data.replace(":", ""))
    # Treat as ASCII fastboot only if every byte is printable.
    if all(b in PRINTABLE for b in raw):
        return raw.decode("ascii")
    return None


cmd_counts = {}
prev_dev = None
print(f"{'time':>9}  {'frm':>6}  {'dev':>3}  {'dir':>3}  {'len':>5}  payload")
print("-" * 80)
events = []
for ln in lines:
    parts = ln.split("\t")
    if len(parts) < 6:
        continue
    frm, t, dev, direction, length, capdata = parts
    if not length:
        continue
    length = int(length)
    direction = "IN " if direction == "1" else "OUT"
    text = decode(capdata) if length > 0 else ""
    events.append((float(t), int(frm), dev, direction, length, text))

# Find all unique ASCII commands sent by host (OUT direction).
print("\n=== unique ASCII strings on OUT endpoint ===")
out_strings = {}
for t, frm, dev, d, ln, text in events:
    if d == "OUT" and text:
        key = text.split(":")[0] if ":" in text else text
        out_strings.setdefault(key, []).append(text)
for k in sorted(out_strings):
    examples = sorted(set(out_strings[k]))
    print(f"  {k:>20}  ({len(out_strings[k]):>5} times, {len(examples)} unique)")
    for ex in examples[:4]:
        print(f"      → {ex!r}")
    if len(examples) > 4:
        print(f"      … and {len(examples) - 4} more")

# Find all unique ASCII response prefixes on IN endpoint.
print("\n=== unique ASCII response prefixes on IN endpoint ===")
in_prefix = {}
for t, frm, dev, d, ln, text in events:
    if d == "IN " and text:
        # Fastboot responses use 4-byte prefixes: OKAY / FAIL / INFO / DATA
        prefix = text[:4]
        in_prefix.setdefault(prefix, []).append(text)
for k in sorted(in_prefix):
    examples = sorted(set(in_prefix[k]))
    print(f"  {k!r:>10}  ({len(in_prefix[k])} times, {len(examples)} unique)")
    for ex in examples[:3]:
        print(f"      ← {ex!r}")

# Chronological transcript of the first 80 ASCII command/response events.
print("\n=== first 80 ASCII command/response events ===")
shown = 0
for t, frm, dev, d, ln, text in events:
    if not text:
        continue
    print(f"{t:>9.3f}  {frm:>6}  {dev:>3}  {d}  {ln:>5}  {text!r}")
    shown += 1
    if shown >= 80:
        break

# Stage transitions: device address changes.
print("\n=== device address timeline (each = a re-enumeration) ===")
last = None
for t, frm, dev, d, ln, text in events:
    if dev != last:
        print(f"  t={t:>8.2f}  device address → {dev}")
        last = dev
