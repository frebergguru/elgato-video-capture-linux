#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
# Shared helpers for the Elgato Video Capture (cx231xx) setup.
# Sourced by bin/elgato-audio, bin/elgato-doctor and bin/elgato-reset.

ELGATO_CARD_NAME="Elgato Video Capture V2"
ELGATO_USB_ID="0fd9:0037"
ELGATO_AUDIO_PATTERN="alsa_input.usb-Elgato_Video_Capture"

# The v4l2loopback node "elgato-viewer --share" republishes the capture on, so
# that OBS and the viewer can both have the picture. The card itself is
# single-open and cannot be shared; this is the way around that. Must match
# card_label in etc/modprobe.d/v4l2loopback-elgato.conf.
SHARE_CARD_NAME="Elgato Share"
SHARE_DEV_DEFAULT="/dev/elgato-share"

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/elgato"
CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/elgato.conf"

# Colour only on a terminal. These messages are routinely redirected to a file
# or piped into a bug report, and raw escapes there are noise -- install.sh and
# elgato-viewer already guard theirs the same way.
if [[ -t 2 ]]; then
    E_CYA=$'\033[1;36m'; E_YEL=$'\033[1;33m'; E_RED=$'\033[1;31m'; E_OFF=$'\033[0m'
else
    E_CYA=''; E_YEL=''; E_RED=''; E_OFF=''
fi

msg()  { printf '%s::%s %s\n' "$E_CYA" "$E_OFF" "$*" >&2; }
warn() { printf '%s!!%s %s\n' "$E_YEL" "$E_OFF" "$*" >&2; }
die()  { printf '%sxx%s %s\n' "$E_RED" "$E_OFF" "$*" >&2; exit 1; }

# --- device discovery -------------------------------------------------------
# Never hardcode /dev/video4: the number shifts when the built-in webcam
# re-enumerates. Prefer the udev symlink, then by-id, then scan as a last resort.
find_device() {
    if [[ -c /dev/elgato ]]; then
        echo /dev/elgato; return 0
    fi

    local byid
    for byid in /dev/v4l/by-id/usb-Elgato_Video_Capture_*-video-index0; do
        [[ -c $byid ]] && { echo "$byid"; return 0; }
    done

    local dev
    for dev in /dev/video*; do
        [[ -c $dev ]] || continue
        if v4l2-ctl -d "$dev" --info 2>/dev/null | grep -F "$ELGATO_CARD_NAME" >/dev/null; then
            echo "$dev"; return 0
        fi
    done

    return 1
}

# The loopback node, if it is set up. Deliberately does not test for the Video
# Capture capability the way find_device does: with exclusive_caps=1 a
# v4l2loopback node advertises Video Output until something is writing to it,
# and only then becomes a camera.
find_share_device() {
    if [[ -c $SHARE_DEV_DEFAULT ]]; then
        echo "$SHARE_DEV_DEFAULT"; return 0
    fi
    local dev
    for dev in /dev/video*; do
        [[ -c $dev ]] || continue
        if v4l2-ctl -d "$dev" --info 2>/dev/null | grep -F "$SHARE_CARD_NAME" >/dev/null; then
            echo "$dev"; return 0
        fi
    done
    return 1
}

require_device() {
    local dev
    if ! dev=$(find_device); then
        warn "No Elgato Video Capture found."
        if lsusb 2>/dev/null | grep -i "$ELGATO_USB_ID" >/dev/null; then
            warn "The USB device IS present but has no /dev/video node."
            warn "Check: journalctl -k -b | grep cx231xx"
        else
            warn "The USB device is not plugged in (expected ID $ELGATO_USB_ID)."
        fi
        die "Cannot continue."
    fi
    echo "$dev"
}

# --- TV standard ------------------------------------------------------------
# This chip does not implement VIDIOC_QUERYSTD, so the standard cannot simply be
# asked for.
#
# It also cannot be inferred from the input status bits. Those look promising --
# they report "no signal, no hsync lock" right after the driver loads -- but
# testing showed they latch to "ok" for BOTH inputs and BOTH standards once the
# decoder has been touched, even with nothing connected. They are not usable.
#
# So detect from two things that cannot lie, together:
#   1. Frame delivery. A line-rate mismatch stops complete frames coming
#      through at all -- which is exactly the NTSC-vs-PAL question.
#   2. Pixel content. Frames arriving is not proof of signal: on an
#      unconnected input the decoder free-runs and emits rolling noise, and
#      it happens to do so under PAL but not NTSC. Checking delivery alone
#      would therefore report "PAL" for a source that is switched off.

input_status() {
    local dev=$1
    v4l2-ctl -d "$dev" --get-input 2>/dev/null | sed -n 's/.*(\(.*\)).*/\1/p'
}

# --- capture geometry -------------------------------------------------------
# This chip implements VIDIOC_S_FMT scaling: it will happily hand over 180x144
# if asked, and it STAYS there until something sets it back. A standard change
# does not reset it -- measured.
#
# That makes a measuring pipeline dangerous. GStreamer negotiates downstream
# caps upstream, so a probe that scales to 180x144 for its own convenience has
# that size fixated on v4l2src and written into the device, and every later
# capture inherits it: elgato-viewer --verify then scores the hardware scaler
# instead of the driver, and OBS opens a 180x144 camera. Measuring must never
# reconfigure the card.
#
# So the probes below pin the standard's full frame twice over: once with
# v4l2-ctl before settling, which is also what repairs a card something else
# has already shrunk, and once as explicit source caps, which stops the
# downstream scaler's request from reaching the device at all.
capture_height() {
    case "$(v4l2-ctl -d "$1" --get-standard 2>/dev/null)" in
        *NTSC*|*PAL-M*|*PAL-60*) echo 480 ;;
        *)                       echo 576 ;;
    esac
}

# pin_capture_format DEV -> prints "720 <height>" and leaves the device on it.
# The card only ever samples 720 across, in either standard.
pin_capture_format() {
    local dev=$1 h
    h=$(capture_height "$dev")
    v4l2-ctl -d "$dev" --set-fmt-video=width=720,height="$h" >/dev/null 2>&1
    echo "720 $h"
}

# The chip needs a moment between streaming sessions. Opening it again
# immediately after a previous capture closed fails transiently, with no USB
# error and nothing in the kernel log -- which would otherwise show up as a
# spurious "no signal" during probing.
SETTLE=${ELGATO_SETTLE:-0.8}
settle() { sleep "$SETTLE"; }

# delivers_frames DEV NFRAMES TIMEOUT -> 0 if NFRAMES buffers arrived in time
# Retries once, because a single failure is more often the settle race above
# than a genuine absence of signal.
delivers_frames() {
    local dev=$1 frames=$2 secs=$3 attempt
    for attempt in 1 2; do
        if timeout "$secs" gst-launch-1.0 -q \
            v4l2src device="$dev" io-mode=mmap num-buffers="$frames" \
            ! fakesink sync=false >/dev/null 2>&1; then
            return 0
        fi
        settle
    done
    return 1
}

# has_signal DEV -> 0 if the captured frames look like a real picture
#
# Frames arriving is NOT proof of a signal. With nothing connected the decoder
# emits a constant blanking level at the nominal rate -- measured on this unit
# with elgato_htl=2: every pixel of every frame exactly 35, stdev 0.000. (An
# earlier note here described rolling noise at mean ~22, stdev ~5; that was
# measured before the horizontal-lock fix and no longer holds.) Any real
# picture is far brighter and far more varied, so check the pixels rather than
# trusting the frame count.
SIGNAL_MIN_PEAK=${SIGNAL_MIN_PEAK:-80}
SIGNAL_MIN_STDEV=${SIGNAL_MIN_STDEV:-12}

has_signal() {
    local dev=$1 tmp rc w h
    tmp=$(mktemp -d) || return 1
    read -r w h < <(pin_capture_format "$dev")
    settle

    # The source caps are pinned: without them the 180x144 the scaler wants is
    # negotiated into the device and left there. See pin_capture_format.
    timeout 8 gst-launch-1.0 -q v4l2src device="$dev" io-mode=mmap num-buffers=8 \
        ! video/x-raw,width="$w",height="$h" \
        ! videoconvert ! video/x-raw,format=GRAY8 \
        ! videoscale ! video/x-raw,format=GRAY8,width=180,height=144 \
        ! multifilesink location="$tmp/f%02d.gray" >/dev/null 2>&1

    python3 - "$tmp" "$SIGNAL_MIN_PEAK" "$SIGNAL_MIN_STDEV" <<'PY'
import sys, glob, statistics
d, peak_min, stdev_min = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
files = sorted(glob.glob(d + "/*.gray"))
if not files:
    sys.exit(1)
peak = stdev = 0
for f in files:
    b = open(f, "rb").read()
    if not b:
        continue
    peak = max(peak, max(b))
    stdev = max(stdev, statistics.pstdev(b))
sys.exit(0 if (peak >= peak_min and stdev >= stdev_min) else 1)
PY
    rc=$?
    rm -rf "$tmp"
    # No frames at all exits 1 above, the same as flat frames do: either way
    # there is no picture, which is what this function answers. (A branch here
    # used to remap an exit status of 2 to success; the script above has never
    # produced one, so it never ran.)
    return $rc
}

# probe_standard DEV INPUT -> prints "ntsc" or "pal"
probe_standard() {
    local dev=$1 input=$2 std

    v4l2-ctl -d "$dev" -i "$input" >/dev/null 2>&1

    # Try the last known-good standard first: the common case then needs one
    # probe instead of two, and a wrong-standard probe costs a full timeout.
    local order="ntsc pal"
    if [[ -r $CACHE_DIR/standard ]]; then
        case $(<"$CACHE_DIR/standard") in
            pal)  order="pal ntsc" ;;
            ntsc) order="ntsc pal" ;;
        esac
    fi

    local delivered=""
    for std in $order; do
        v4l2-ctl -d "$dev" -s "$std" >/dev/null 2>&1 || continue
        sleep 0.4                       # let the decoder attempt to lock
        # 20 frames is ~0.67s at NTSC, ~0.80s at PAL; 3s is generous headroom.
        delivers_frames "$dev" 20 3 || continue
        delivered="$std"
        if has_signal "$dev"; then
            msg "Locked as ${std^^}"
            mkdir -p "$CACHE_DIR"
            echo "$std" > "$CACHE_DIR/standard"
            ELGATO_PROBE_OK=1
            echo "$std"
            return 0
        fi
    done

    if [[ -n $delivered ]]; then
        warn "Frames arrive under ${delivered^^} but carry no picture -"
        warn "the decoder is free-running on an unconnected input."
    fi

    # Nothing locked: source off, unplugged, or on the other input.
    local fallback=ntsc
    [[ -r $CACHE_DIR/standard ]] && fallback=$(<"$CACHE_DIR/standard")

    warn "No lock on input $input - is the source powered on and connected to the yellow RCA jack?"
    warn "Falling back to ${fallback^^}. Override with --ntsc or --pal."
    v4l2-ctl -d "$dev" -s "$fallback" >/dev/null 2>&1
    echo "$fallback"
}

# Set to 1 by probe_standard when it has just captured frames successfully, so
# callers can skip a redundant wait_until_streaming loop (which cost ~7s).
ELGATO_PROBE_OK=0

# apply_standard DEV INPUT STD  ('auto' probes, otherwise force)
apply_standard() {
    local dev=$1 input=$2 std=$3
    if [[ $std == auto ]]; then
        probe_standard "$dev" "$input"
    else
        v4l2-ctl -d "$dev" -i "$input" >/dev/null 2>&1
        v4l2-ctl -d "$dev" -s "$std" >/dev/null 2>&1 \
            || die "Failed to set standard '$std'."
        mkdir -p "$CACHE_DIR"; echo "$std" > "$CACHE_DIR/standard"
        echo "$std"
    fi
    settle
}

# --- geometry ---------------------------------------------------------------
# Only YUYV is offered and frame sizes are not enumerable, so geometry is fixed
# by the standard. Output is an integer multiple of the *active* line count
# (240 for NTSC, 288 for PAL) at 4:3, which keeps pixel art sharp.
set_geometry() {
    local std=$1 scale=${2:-3}
    case $std in
        ntsc*|NTSC*)
            WIDTH=720; HEIGHT=480; FRAMERATE='30000/1001'
            OUT_H=$(( 240 * scale ))
            ;;
        pal*|PAL*|secam*)
            WIDTH=720; HEIGHT=576; FRAMERATE='25/1'
            OUT_H=$(( 288 * scale ))
            ;;
        *) die "Unknown standard: $std" ;;
    esac
    OUT_W=$(( OUT_H * 4 / 3 ))
    export WIDTH HEIGHT FRAMERATE OUT_W OUT_H
}

# --- audio node -------------------------------------------------------------
# Resolve by pattern rather than by the unit's serial number so a replacement
# box still works.
#
# This must match a real Audio/Source *Node*, not just the pattern appearing
# somewhere in pw-dump. When the USB link wedges, the Source node disappears but
# the Device object survives and still carries the name -- a looser match then
# reports success for a node nothing can connect to.
find_audio_node() {
    pw-dump 2>/dev/null | python3 -c '
import json, sys
pattern = sys.argv[1]
try:
    objs = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for o in objs:
    if o.get("type") != "PipeWire:Interface:Node":
        continue
    props = (o.get("info") or {}).get("props") or {}
    if props.get("media.class") != "Audio/Source":
        continue
    name = props.get("node.name", "")
    if name.startswith(pattern):
        print(name)
        break
' "$ELGATO_AUDIO_PATTERN" 2>/dev/null
}

# The node's sample rate and channel count, as PipeWire reports them. Falls
# back to what this card always is when a property is absent -- a suspended
# node does not always publish its format, and a sane default beats no answer.
audio_node_prop() {
    pw-dump 2>/dev/null | python3 -c '
import json, sys
name, key, fallback = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    objs = json.load(sys.stdin)
except Exception:
    print(fallback); sys.exit(0)
for o in objs:
    if o.get("type") != "PipeWire:Interface:Node":
        continue
    props = (o.get("info") or {}).get("props") or {}
    if props.get("node.name") == name:
        value = str(props.get(key, "")).strip()
        print(value if value.isdigit() else fallback)
        break
else:
    print(fallback)
' "$1" "$2" "$3" 2>/dev/null || echo "$3"
}

audio_node_rate()     { audio_node_prop "$1" audio.rate 48000; }
audio_node_channels() { audio_node_prop "$1" audio.channels 2; }

# what_feeds NODENAME -> names of the nodes actually linked into it
what_feeds() {
    pw-dump 2>/dev/null | python3 -c '
import json, sys
target = sys.argv[1]
try:
    objs = json.load(sys.stdin)
except Exception:
    sys.exit(1)
nodes, links = {}, []
for o in objs:
    t = o.get("type", "")
    info = o.get("info") or {}
    if t == "PipeWire:Interface:Node":
        nodes[o["id"]] = (info.get("props") or {}).get("node.name", "")
    elif t == "PipeWire:Interface:Link":
        p = info.get("props") or {}
        links.append((p.get("link.output.node"), p.get("link.input.node")))
seen = []
for out_id, in_id in links:
    if nodes.get(in_id) == target:
        n = nodes.get(out_id, "?")
        if n not in seen:
            seen.append(n)
print("\\n".join(seen))
' "$1" 2>/dev/null
}

# --- picture controls -------------------------------------------------------
# Optional user overrides, e.g.  saturation=72
apply_controls() {
    local dev=$1
    [[ -r $CONFIG_FILE ]] || return 0
    local line ctrl
    while IFS= read -r line; do
        line=${line%%#*}; line=${line// /}
        [[ -z $line ]] && continue
        case $line in
            brightness=*|contrast=*|saturation=*|hue=*|volume=*)
                ctrl=$line
                v4l2-ctl -d "$dev" -c "$ctrl" 2>/dev/null \
                    && msg "control $ctrl" \
                    || warn "could not set control $ctrl"
                ;;
        esac
    done < "$CONFIG_FILE"
}

# --- USB link health --------------------------------------------------------
# Count -EPROTO errors only since the device last enumerated. awk resets the
# counter at each enumeration, so a replug clears the slate; counting for the
# whole boot would keep reporting errors that a replug already fixed.
usb_errors_since_plug() {
    journalctl -k -b -o short-unix 2>/dev/null | awk '
        /New USB device found, idVendor=0fd9, idProduct=0037/ { seen=1; count=0; next }
        seen && /cx231xx.*(-71|error=-71)/ { count++ }
        END { print count+0 }
    '
}

# --- audio level ------------------------------------------------------------
# Print the RMS level of the capture input in dBFS, or nothing on failure.
# Useful as a cross-check when video is missing: audio and video share one
# cable bundle, so sound arriving while the picture does not narrows the fault
# to the yellow video conductor.
audio_level() {
    local node tmp
    node=$(find_audio_node) || return 1
    [[ -n $node ]] || return 1
    tmp=$(mktemp -d) || return 1

    ( timeout 6 pw-cat --record --target "$node" "$tmp/a.wav" >/dev/null 2>&1 ) &
    local pid=$!
    sleep 2.5
    kill $pid 2>/dev/null; wait $pid 2>/dev/null

    python3 - "$tmp/a.wav" 2>/dev/null <<'PY'
import sys, wave, math
try:
    w = wave.open(sys.argv[1], "rb")
    n, sw = w.getnframes(), w.getsampwidth()
    if not n:
        raise ValueError
    raw = w.readframes(n)
except Exception:
    sys.exit(1)
step = sw
total = count = 0
for i in range(0, len(raw) - step + 1, step * 16):     # sample every 16th frame
    v = int.from_bytes(raw[i:i+step], "little", signed=True)
    total += v * v
    count += 1
if not count:
    sys.exit(1)
rms = math.sqrt(total / count)
full = 2 ** (sw * 8 - 1)
print(f"{20 * math.log10(max(rms, 1) / full):.1f}")
PY
    local rc=$?
    rm -rf "$tmp"
    return $rc
}

# --- module reload ----------------------------------------------------------
# WirePlumber keeps /dev/snd/controlC<n> open, which pins cx231xx_alsa and so
# pins cx231xx. It has to be stopped before the module can be unloaded.
WP_WAS_RUNNING=0

release_device() {
    "$(dirname "${BASH_SOURCE[0]}")/../bin/elgato-audio" stop >/dev/null 2>&1
    if systemctl --user is-active --quiet wireplumber 2>/dev/null; then
        WP_WAS_RUNNING=1
        msg "Stopping wireplumber (it holds the ALSA control device)"
        systemctl --user stop wireplumber
        local i
        for i in $(seq 1 20); do
            fuser /dev/snd/controlC* >/dev/null 2>&1 || break
            sleep 0.25
        done
    fi
}

restore_wireplumber() {
    if (( WP_WAS_RUNNING )); then
        systemctl --user start wireplumber 2>/dev/null
        WP_WAS_RUNNING=0
        msg "Restarted wireplumber"
    fi
}

device_holders() {
    local node pid
    for node in /dev/video* /dev/vbi* /dev/snd/controlC*; do
        ls -l /proc/[0-9]*/fd/* 2>/dev/null | grep -F "$node" | awk '{print $9}' \
            | sed 's|/proc/\([0-9]*\)/fd/.*|\1|' | sort -u | while read -r pid; do
                [[ -r /proc/$pid/comm ]] && echo "    $(<"/proc/$pid/comm") (pid $pid) -> $node"
            done
    done
}

# reload_cx231xx [modparams...]
reload_cx231xx() {
    release_device
    sudo modprobe -r cx231xx_alsa 2>/dev/null
    if ! sudo modprobe -r cx231xx 2>/dev/null; then
        warn "Could not unload cx231xx. Still held by:"
        device_holders
        restore_wireplumber
        return 1
    fi
    sudo modprobe cx231xx "$@" || { restore_wireplumber; return 1; }
    sudo modprobe cx231xx_alsa 2>/dev/null

    msg "Waiting for the device to re-register and reload firmware..."
    local i
    for i in $(seq 1 30); do
        sleep 0.5
        [[ -c /dev/elgato ]] && break
    done
    restore_wireplumber
    settle
    wait_until_streaming || warn "device node present but not streaming yet"
    [[ -c /dev/elgato ]]
}

# After a module reload the device node appears well before the chip can
# actually stream: the decoder firmware upload takes a couple of seconds more.
# Poll with real capture attempts rather than guessing a sleep, so callers stop
# reporting a spurious "no frames captured".
wait_until_streaming() {
    local dev tries=${1:-12} i
    for i in $(seq 1 "$tries"); do
        dev=$(find_device) || { sleep 1; continue; }
        if timeout 6 gst-launch-1.0 -q v4l2src device="$dev" io-mode=mmap \
             num-buffers=2 ! fakesink sync=false >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# --- device power cycle ------------------------------------------------------
# Some register experiments (notably the PLL at 0x108/0x110/0x8f8) leave the
# decoder in a state that a module reload does NOT clear: setting the module
# parameter back to 0 simply stops writing them, it does not restore the old
# values. The chip then emits a mathematically flat blanking level -- mean 35,
# std 0.00 -- which looks like "no signal" but is a hung decoder.
#
# The only reliable recovery is a USB port reset, which power-cycles the device
# and makes the firmware reload from scratch.
usb_reset_elgato() {
    local dev
    dev=$(lsusb | sed -n 's/Bus \([0-9]*\) Device \([0-9]*\): ID 0fd9:0037.*/\/dev\/bus\/usb\/\1\/\2/p' | head -1)
    [[ -n $dev ]] || { warn "Elgato not found on the USB bus"; return 1; }
    msg "Resetting the capture device over USB (needs root)"
    sudo usbreset 0fd9:0037 >/dev/null 2>&1 || sudo usbreset "$dev" >/dev/null 2>&1 || {
        warn "usbreset failed - unplug and replug the device"
        return 1
    }
    local i
    for i in $(seq 1 30); do
        sleep 0.5
        [[ -c /dev/elgato ]] && break
    done
    settle
    wait_until_streaming 15 >/dev/null 2>&1
    return 0
}

# Is the decoder emitting a flat blanking level rather than real video?
#
# CAUTION: this no longer distinguishes what it was written to distinguish. It
# assumed a genuine loss of signal still shows noise, so that a mathematically
# flat frame could only mean a wedged decoder. With elgato_htl=2 an
# unconnected input is flat too -- measured, every pixel exactly 35 and stdev
# 0.000, which is the same signature the wedged-decoder note in
# usb_reset_elgato describes. A healthy card with nothing plugged into it now
# answers "hung". Nothing calls this; do not start without a test that can
# actually tell the two apart.
decoder_is_hung() {
    local dev=$1 tmp rc w h
    tmp=$(mktemp -d) || return 1
    read -r w h < <(pin_capture_format "$dev")
    settle
    # Source caps pinned, and the scaling asked for separately: letting the one
    # capsfilter do both would push 360x288 into the device. See
    # pin_capture_format.
    timeout 10 gst-launch-1.0 -q v4l2src device="$dev" io-mode=mmap num-buffers=4 \
        ! video/x-raw,width="$w",height="$h" \
        ! videoconvert ! video/x-raw,format=GRAY8 \
        ! videoscale ! video/x-raw,format=GRAY8,width=360,height=288 \
        ! multifilesink location="$tmp/f%02d.gray" >/dev/null 2>&1
    python3 - "$tmp" <<'PY'
import sys, glob, statistics
fs = sorted(glob.glob(sys.argv[1] + "/*.gray"))
if not fs:
    sys.exit(2)
flat = 0
for f in fs:
    b = open(f, "rb").read()
    if b and statistics.pstdev(b) < 0.5:
        flat += 1
sys.exit(0 if flat == len(fs) else 1)
PY
    rc=$?
    rm -rf "$tmp"
    return $rc
}

# --- screen size -------------------------------------------------------------
# Largest 4:3 rectangle that fits the primary monitor, for --fullscreen.
screen_geometry() {
    local geo w h
    geo=$(qdbus6 org.kde.KWin /KWin supportInformation 2>/dev/null \
          | sed -n 's/^Geometry: [0-9]*,[0-9]*,\([0-9]*\)x\([0-9]*\).*/\1 \2/p' | head -1)
    read -r w h <<< "${geo:-1920 1080}"
    [[ -n $w && -n $h ]] || { w=1920; h=1080; }
    # fit 4:3 inside w x h
    if (( w * 3 > h * 4 )); then
        echo "$(( h * 4 / 3 )) $h"
    else
        echo "$w $(( w * 3 / 4 ))"
    fi
}
