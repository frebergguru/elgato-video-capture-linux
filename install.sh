#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
# Install the Elgato Video Capture V2 setup.
#
# Six pieces, in dependency order:
#
#   1. the patched cx231xx driver, into /lib/modules/<kernel>/updates/
#   2. /etc/modprobe.d/cx231xx.conf, which turns on elgato_htl=2
#   3. the udev rule: a stable /dev/elgato, and USB autosuspend pinned off
#   4. the WirePlumber rule, so the card never becomes your default microphone
#   5. v4l2loopback, so "elgato-viewer --share" can hand the picture to OBS as
#      well -- the card itself is single-open and always will be
#   6. launcher symlinks in ~/.local/bin
#
# (1) and (2) are what make the picture usable. Without them roughly 60% of
# frames tear: the stock driver never programs the decoder's horizontal
# time-lock loop, so line lengths hunt and every line is sampled at a
# different phase. See README.md.
#
# Run as yourself, not with sudo -- individual steps call sudo as needed, and
# the WirePlumber rule and symlinks belong to your user.

set -uo pipefail

SELF_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
KVER=$(uname -r)
MOD_DIR="/lib/modules/$KVER/updates/cx231xx"
TOOLS=(elgato-viewer elgato-audio elgato-doctor elgato-reset elgato-obs-setup
       elgato-driver)

if [[ -t 1 ]]; then
    G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; O=$'\033[0m'
else
    G=''; Y=''; R=''; O=''
fi
msg()  { printf '%s::%s %s\n' "$G" "$O" "$*"; }
warn() { printf '%s!!%s %s\n' "$Y" "$O" "$*" >&2; }
die()  { printf '%sxx%s %s\n' "$R" "$O" "$*" >&2; exit 1; }

WITH_DRIVER=1
WITH_SHARE=${ELGATO_SKIP_SHARE:+0}; WITH_SHARE=${WITH_SHARE:-1}
while (( $# )); do
    case "$1" in
        --no-driver) WITH_DRIVER=0; shift ;;
        --no-share)  WITH_SHARE=0;  shift ;;
        -h|--help)
            sed -n '/^# Install the/,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;$d'
            echo "usage: ${0##*/} [--no-driver] [--no-share]"
            echo "  --no-driver   skip building and installing the kernel module"
            echo "  --no-share    skip v4l2loopback (elgato-viewer --share will not work)"
            echo "                same as setting ELGATO_SKIP_SHARE=1"
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] && die "run this as yourself, not with sudo -- it calls sudo where needed"

echo
msg "Installing from $SELF_DIR  (kernel $KVER)"
echo

# --- prerequisites ----------------------------------------------------------
for t in v4l2-ctl gst-launch-1.0 pw-loopback; do
    command -v "$t" >/dev/null || warn "$t is missing -- install v4l-utils / gstreamer / pipewire"
done

# Optional, and only for elgato-viewer's r key: the viewer plays perfectly
# without them and says which one is missing if you press r.
gst-inspect-1.0 avenc_ffv1 >/dev/null 2>&1 \
    || warn "gst-libav is missing -- 'elgato-viewer' plays fine, but the r key cannot record"

# Releasing the module needs whatever has the ALSA card open to let go. The
# capture card registers an ALSA device, and WirePlumber holds its control
# node, so an unload fails with "Module cx231xx_alsa is in use" until it stops.
AUDIO_STOPPED=""
release_audio() {
    local unit
    for unit in wireplumber pipewire-pulse pipewire; do
        if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
            systemctl --user stop "$unit" 2>/dev/null && AUDIO_STOPPED="$unit $AUDIO_STOPPED"
        fi
    done
    [[ -n $AUDIO_STOPPED ]] && sleep 1
    return 0
}
restore_audio() {
    local unit
    for unit in pipewire pipewire-pulse wireplumber; do
        [[ " $AUDIO_STOPPED " == *" $unit "* ]] && systemctl --user start "$unit" 2>/dev/null
    done
    AUDIO_STOPPED=""
}

# --- 1. driver --------------------------------------------------------------
if (( WITH_DRIVER )); then
    [[ -d /lib/modules/$KVER/build ]] \
        || die "kernel headers for $KVER are missing (install linux-headers)"

    msg "Building the patched cx231xx driver"
    make -C "/lib/modules/$KVER/build" M="$SELF_DIR/driver/cx231xx" modules >/dev/null \
        || die "driver build failed"

    msg "Installing modules into $MOD_DIR"
    sudo install -d "$MOD_DIR" || die "could not create $MOD_DIR"
    sudo install -m644 "$SELF_DIR/driver/cx231xx/cx231xx.ko" \
                       "$SELF_DIR/driver/cx231xx/cx231xx-alsa.ko" "$MOD_DIR/" \
        || die "could not install the modules"
    sudo depmod -a "$KVER" || warn "depmod failed"
    make -C "/lib/modules/$KVER/build" M="$SELF_DIR/driver/cx231xx" clean >/dev/null 2>&1
else
    msg "Skipping the driver (--no-driver)"
fi

# --- 2. module options ------------------------------------------------------
msg "Installing /etc/modprobe.d/cx231xx.conf (elgato_htl=2)"
sudo install -m644 "$SELF_DIR/etc/modprobe.d/cx231xx.conf" /etc/modprobe.d/cx231xx.conf \
    || warn "could not install the modprobe config"

# --- 3. udev rule -----------------------------------------------------------
msg "Installing the udev rule (/dev/elgato, autosuspend off)"
if sudo install -m644 "$SELF_DIR/etc/70-elgato-video-capture.rules" \
        /etc/udev/rules.d/70-elgato-video-capture.rules; then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=video4linux --subsystem-match=usb
else
    warn "could not install the udev rule; /dev/elgato will not be created"
fi

# --- 4. WirePlumber rule ----------------------------------------------------
WP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
msg "Installing the WirePlumber rule into $WP_DIR"
mkdir -p "$WP_DIR"
install -m644 "$SELF_DIR/etc/wireplumber/51-elgato-not-default.conf" "$WP_DIR/" \
    || warn "could not install the WirePlumber rule"

# --- 5. the sharing loopback ------------------------------------------------
# The capture card is single-open: cx231xx has one vb2_queue, so whoever calls
# STREAMON first owns it and everyone else gets EBUSY. v4l2loopback is how
# "elgato-viewer --share" gets around that -- it captures once and republishes
# the frames on a virtual node OBS can read. Audio never needed this; PipeWire
# already hands the capture stream to every reader.
#
# Nothing here is fatal. This script exists to install the driver, and a missing
# loopback must not take that down with it.
if (( WITH_SHARE )); then
    echo
    if [[ -d /sys/module/v4l2loopback ]] || modinfo v4l2loopback >/dev/null 2>&1; then
        msg "v4l2loopback is already available"
    elif ! command -v pacman >/dev/null; then
        warn "v4l2loopback is missing and this is not an Arch-based system."
        warn "Install it your distribution's way, then re-run with --no-share."
    else
        msg "v4l2loopback is needed for 'elgato-viewer --share' (OBS and the"
        msg "viewer at the same time). It builds against your kernel via DKMS."
        read -r -p ":: Install dkms, v4l2loopback-dkms and v4l2loopback-utils? [Y/n] " reply
        case "${reply,,}" in
            n|no) WITH_SHARE=0; msg "Skipping -- re-run install.sh to add it later" ;;
            *)    sudo pacman -S --needed dkms v4l2loopback-dkms v4l2loopback-utils \
                      || { WITH_SHARE=0; warn "pacman failed; skipping the loopback"; } ;;
        esac
    fi
fi

if (( WITH_SHARE )); then
    msg "Installing the v4l2loopback config (/dev/elgato-share)"
    sudo install -m644 "$SELF_DIR/etc/modprobe.d/v4l2loopback-elgato.conf" \
        /etc/modprobe.d/v4l2loopback-elgato.conf \
        || warn "could not install the v4l2loopback modprobe config"
    sudo install -Dm644 "$SELF_DIR/etc/modules-load.d/v4l2loopback-elgato.conf" \
        /etc/modules-load.d/v4l2loopback-elgato.conf \
        || warn "could not install the modules-load config; it will not load at boot"
    if sudo install -m644 "$SELF_DIR/etc/71-elgato-share.rules" \
            /etc/udev/rules.d/71-elgato-share.rules; then
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=video4linux
    else
        warn "could not install the share udev rule; /dev/elgato-share will not appear"
    fi

    # Reload rather than modprobe: if it was already up with other options
    # (another tool's, or an earlier version of this file) ours would be ignored.
    sudo modprobe -r v4l2loopback 2>/dev/null
    if sudo modprobe v4l2loopback; then
        for _ in 1 2 3 4 5; do [[ -c /dev/elgato-share ]] && break; sleep 0.3; done
        if [[ -c /dev/elgato-share ]]; then
            msg "/dev/elgato-share is ready -- try: elgato-viewer --share"
        else
            warn "the module loaded but /dev/elgato-share did not appear."
            warn "Check: v4l2-ctl --list-devices | grep -A1 'Elgato Share'"
        fi
    else
        warn "modprobe v4l2loopback failed -- 'elgato-viewer --share' will not work"
        warn "If DKMS has not built yet, try: sudo dkms autoinstall"
    fi
fi

# --- 6. launcher symlinks ---------------------------------------------------
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for tool in "${TOOLS[@]}"; do
    ln -sfn "$SELF_DIR/bin/$tool" "$BIN_DIR/$tool"
done
msg "Linked ${TOOLS[*]} into $BIN_DIR"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH -- add it to your shell rc" ;;
esac

# --- 7. load it -------------------------------------------------------------
if (( WITH_DRIVER )); then
    echo
    msg "Loading the driver"
    release_audio
    sudo modprobe -r cx231xx_alsa cx231xx 2>/dev/null
    if lsmod | grep -q '^cx231xx '; then
        warn "could not unload the running cx231xx; reboot to pick up the new one"
    else
        sudo modprobe cx231xx      || warn "modprobe cx231xx failed"
        sudo modprobe cx231xx_alsa || warn "modprobe cx231xx_alsa failed"
    fi
    restore_audio
    sleep 3
fi

# --- 8. verify --------------------------------------------------------------
echo
HTL=/sys/module/cx231xx/parameters/elgato_htl
if [[ -r $HTL ]]; then
    if [[ $(<"$HTL") == 2 ]]; then
        msg "elgato_htl=2 is active -- the horizontal lock fix is on"
    else
        warn "elgato_htl is $(<"$HTL"), expected 2. The picture will tear."
        warn "Check /etc/modprobe.d/cx231xx.conf, then reload the driver."
    fi
else
    warn "cx231xx is not loaded, or is the stock module without the fix."
    (( WITH_DRIVER )) && warn "A reboot will pick up the newly installed one."
fi

[[ -c /dev/elgato ]] && msg "/dev/elgato is live" \
                     || warn "/dev/elgato is missing -- replug the device"

echo
msg "Done. Check it with:   elgato-viewer --verify"
msg "Then play with:        elgato-viewer"
msg "If anything is off:    elgato-doctor"
