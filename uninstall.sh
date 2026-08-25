#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
# Remove everything install.sh put on the system.
#
# Reverses all six pieces: the launcher symlinks, the WirePlumber rule, the
# udev rules, the v4l2loopback config, /etc/modprobe.d/cx231xx.conf and the
# patched kernel module.
#
# The v4l2loopback *packages* are left installed -- pacman owns them, other
# software may be using them, and removing them is your call, not this
# script's. Only the configuration this package wrote is removed.
#
# Note what removing the module means: the stock in-tree cx231xx has no
# elgato_htl knob, so the decoder's horizontal lock goes unprogrammed and the
# picture tears again. That is the state this package exists to fix.
#
# Nothing outside these paths is touched, and no source file in this directory
# is modified or deleted.

set -uo pipefail

SELF_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
KVER=$(uname -r)
MOD_DIR="/lib/modules/$KVER/updates/cx231xx"
TOOLS=(elgato-viewer elgato-audio elgato-doctor elgato-reset elgato-obs-setup
       elgato-driver)
BIN_DIR="$HOME/.local/bin"
WP_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d/51-elgato-not-default.conf"
UDEV_RULE=/etc/udev/rules.d/70-elgato-video-capture.rules
MODPROBE_CONF=/etc/modprobe.d/cx231xx.conf
SHARE_UDEV_RULE=/etc/udev/rules.d/71-elgato-share.rules
SHARE_MODPROBE=/etc/modprobe.d/v4l2loopback-elgato.conf
SHARE_MODLOAD=/etc/modules-load.d/v4l2loopback-elgato.conf

if [[ -t 1 ]]; then
    G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; O=$'\033[0m'
else
    G=''; Y=''; R=''; O=''
fi
msg()  { printf '%s::%s %s\n' "$G" "$O" "$*"; }
warn() { printf '%s!!%s %s\n' "$Y" "$O" "$*" >&2; }
die()  { printf '%sxx%s %s\n' "$R" "$O" "$*" >&2; exit 1; }

ASSUME_YES=0
KEEP_DRIVER=0
while (( $# )); do
    case "$1" in
        -y|--yes)      ASSUME_YES=1; shift ;;
        --keep-driver) KEEP_DRIVER=1; shift ;;
        -h|--help)
            echo "usage: ${0##*/} [-y] [--keep-driver]"
            echo "  -y, --yes       do not ask for confirmation"
            echo "      --keep-driver  leave the patched module and its options in place"
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] && die "run this as yourself, not with sudo -- it calls sudo where needed"

echo
msg "This will remove:"
FOUND=0
for t in "${TOOLS[@]}"; do
    if [[ -L $BIN_DIR/$t && $(readlink -f "$BIN_DIR/$t") == "$SELF_DIR"/* ]]; then
        echo "    $BIN_DIR/$t"; FOUND=1
    elif [[ -e $BIN_DIR/$t ]]; then
        echo "    ($BIN_DIR/$t points elsewhere -- will be left alone)"
    fi
done
[[ -f $WP_FILE ]]    && echo "    $WP_FILE"
[[ -f $UDEV_RULE ]]  && echo "    $UDEV_RULE"
if (( ! KEEP_DRIVER )); then
    [[ -f $MODPROBE_CONF ]] && echo "    $MODPROBE_CONF"
    [[ -d $MOD_DIR ]]       && echo "    $MOD_DIR  (then reverts to the stock driver)"
fi
echo "    (nothing in $SELF_DIR)"
echo

if (( ! ASSUME_YES )); then
    read -r -p "Proceed? [y/N] " reply
    [[ ${reply,,} == y* ]] || { msg "Nothing done."; exit 0; }
fi

AUDIO_STOPPED=""
release_audio() {
    local unit
    for unit in wireplumber pipewire-pulse pipewire; do
        systemctl --user is-active --quiet "$unit" 2>/dev/null \
            && systemctl --user stop "$unit" 2>/dev/null \
            && AUDIO_STOPPED="$unit $AUDIO_STOPPED"
    done
    [[ -n $AUDIO_STOPPED ]] && sleep 1
    return 0
}
restore_audio() {
    local unit
    for unit in pipewire pipewire-pulse wireplumber; do
        [[ " $AUDIO_STOPPED " == *" $unit "* ]] && systemctl --user start "$unit" 2>/dev/null
    done
}

# --- 1. symlinks ------------------------------------------------------------
for t in "${TOOLS[@]}"; do
    # only remove links that point back into this package
    if [[ -L $BIN_DIR/$t && $(readlink -f "$BIN_DIR/$t") == "$SELF_DIR"/* ]]; then
        rm -f "$BIN_DIR/$t" && msg "removed $BIN_DIR/$t"
    elif [[ -e $BIN_DIR/$t ]]; then
        warn "left $BIN_DIR/$t alone -- it does not point into $SELF_DIR"
    fi
done

# --- 2. WirePlumber rule ----------------------------------------------------
if [[ -f $WP_FILE ]]; then
    rm -f "$WP_FILE" && msg "removed $WP_FILE"
    systemctl --user restart wireplumber 2>/dev/null \
        && msg "restarted wireplumber" \
        || warn "could not restart wireplumber -- log out and back in"
fi

# --- 3. udev rule -----------------------------------------------------------
if [[ -f $UDEV_RULE ]]; then
    sudo rm -f "$UDEV_RULE" && msg "removed $UDEV_RULE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=video4linux --subsystem-match=usb
fi

# --- 4. the sharing loopback ------------------------------------------------
# Only our configuration goes. The module and its packages stay: they are
# ordinary system software that other things may be using.
SHARE_REMOVED=0
for f in "$SHARE_UDEV_RULE" "$SHARE_MODPROBE" "$SHARE_MODLOAD"; do
    [[ -f $f ]] || continue
    sudo rm -f "$f" && msg "removed $f" && SHARE_REMOVED=1
done
if (( SHARE_REMOVED )); then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=video4linux
    if [[ -d /sys/module/v4l2loopback ]]; then
        sudo modprobe -r v4l2loopback 2>/dev/null \
            && msg "unloaded v4l2loopback" \
            || warn "v4l2loopback is in use; it will be gone after a reboot"
    fi
    msg "left the v4l2loopback packages installed -- remove them yourself if"
    msg "nothing else wants them:  sudo pacman -Rns v4l2loopback-dkms v4l2loopback-utils"
fi

# --- 5. driver and its options ----------------------------------------------
if (( KEEP_DRIVER )); then
    msg "Left the patched driver and $MODPROBE_CONF in place (--keep-driver)"
else
    [[ -f $MODPROBE_CONF ]] && sudo rm -f "$MODPROBE_CONF" && msg "removed $MODPROBE_CONF"
    if [[ -d $MOD_DIR ]]; then
        release_audio
        sudo modprobe -r cx231xx_alsa cx231xx 2>/dev/null
        sudo rm -rf "$MOD_DIR" && msg "removed $MOD_DIR"
        sudo depmod -a "$KVER" || warn "depmod failed"
        sudo modprobe cx231xx 2>/dev/null && sudo modprobe cx231xx_alsa 2>/dev/null \
            && msg "reloaded the stock in-tree driver" \
            || warn "could not reload cx231xx -- reboot to finish"
        restore_audio
        echo
        warn "The stock driver has no elgato_htl knob, so the picture will tear"
        warn "again (~60% of frames). Re-run install.sh to undo that."
    fi
fi

echo
msg "Done."
