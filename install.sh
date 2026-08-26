#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
# Install the Elgato Video Capture V2 setup.
#
# Six pieces, in dependency order:
#
#   1. the patched cx231xx driver, into <modules>/updates/cx231xx/
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
# Distribution-independent: the package manager is found by looking for it, not
# by reading an ID, and every difference is confined to lib/elgato-distro.sh.
# Arch, Debian, Ubuntu, Fedora, openSUSE, Void and Alpine are handled end to
# end; Gentoo is named but Portage is left for you to drive. Under Secure Boot
# the module is signed with the machine's existing MOK if there is one, and you
# are told plainly if not.
#
# Run as yourself, not with sudo -- individual steps escalate as needed, and
# the WirePlumber rule and symlinks belong to your user.

set -uo pipefail

SELF_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
KVER=$(uname -r)
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

# shellcheck source=lib/elgato-distro.sh
source "$SELF_DIR/lib/elgato-distro.sh" \
    || die "lib/elgato-distro.sh is missing -- is this a complete checkout?"

MOD_ROOT=$(module_root "$KVER")
MOD_DIR="$MOD_ROOT/updates/cx231xx"
BUILD_DIR="$MOD_ROOT/build"

WITH_DRIVER=1
WITH_SHARE=${ELGATO_SKIP_SHARE:+0}; WITH_SHARE=${WITH_SHARE:-1}
ASSUME_YES=0
while (( $# )); do
    case "$1" in
        --no-driver) WITH_DRIVER=0; shift ;;
        --no-share)  WITH_SHARE=0;  shift ;;
        -y|--yes)    ASSUME_YES=1;  shift ;;
        -h|--help)
            sed -n '/^# Install the/,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;$d'
            echo "usage: ${0##*/} [--no-driver] [--no-share] [-y]"
            echo "  --no-driver   skip building and installing the kernel module"
            echo "  --no-share    skip v4l2loopback (elgato-viewer --share will not work)"
            echo "                same as setting ELGATO_SKIP_SHARE=1"
            echo "  -y, --yes     answer yes to every prompt (for unattended runs)"
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done
(( ASSUME_YES )) && PKG_YES=1

[[ $EUID -eq 0 ]] && die "run this as yourself, not with sudo -- it escalates where needed"

echo
msg "Installing from $SELF_DIR"
msg "$DISTRO_NAME  --  kernel $KVER  --  packages via ${PKG/unknown/an unrecognised package manager}"
echo
prime_root

# --- prerequisites ----------------------------------------------------------
# Nothing here is fatal on its own: the point of the script is the driver, and
# a missing player is something you can install afterwards.
check_tool() { command -v "$1" >/dev/null 2>&1 || advise_missing "$1" "$2"; }
check_tool v4l2-ctl       v4l-utils
check_tool gst-launch-1.0 gstreamer
check_tool pw-loopback    pipewire
check_tool python3        python

# Optional, and only for elgato-viewer's r key: the viewer plays perfectly
# without them and says which one is missing if you press r.
gst-inspect-1.0 avenc_ffv1 >/dev/null 2>&1 \
    || advise_missing "the FFV1 encoder ('elgato-viewer' plays fine, but r cannot record)" gst-libav

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
    if [[ ! -f $BUILD_DIR/Makefile ]]; then
        warn "kernel build tree for $KVER is missing at $BUILD_DIR"
        advise_missing "kernel headers" headers
        die "install them and re-run, or use --no-driver"
    fi
    for t in make cc; do
        command -v "$t" >/dev/null 2>&1 && continue
        [[ $t == cc ]] && command -v gcc >/dev/null 2>&1 && continue
        advise_missing "$t" build
        die "a compiler and make are needed to build the module (or use --no-driver)"
    done

    # Secure Boot rejects unsigned modules outright, and this is on by default
    # on Fedora, Ubuntu and openSUSE. Better to say so before the build than to
    # let modprobe fail with a bare "Key was rejected by service".
    SECURE_BOOT=0
    if secure_boot_enabled; then
        SECURE_BOOT=1
        warn "Secure Boot is enabled: an unsigned module will not load."
        warn "This script signs with an already-enrolled MOK if the machine has one."
    fi
    [[ $(lockdown_mode) != none ]] \
        && warn "kernel lockdown is $(lockdown_mode) -- out-of-tree modules may be refused"

    msg "Building the patched cx231xx driver"
    make -C "$BUILD_DIR" M="$SELF_DIR/driver/cx231xx" modules >/dev/null \
        || die "driver build failed"

    if (( SECURE_BOOT )); then
        if CERT=$(sign_module "$KVER" "$SELF_DIR/driver/cx231xx/cx231xx.ko" \
                                      "$SELF_DIR/driver/cx231xx/cx231xx-alsa.ko"); then
            msg "Signed the modules with $CERT"
        else
            warn "No enrolled signing key found, so the modules stay unsigned."
            warn "They will not load until you enrol one. The short version:"
            warn "  openssl req -new -x509 -newkey rsa:2048 -nodes -days 36500 \\"
            warn "      -keyout MOK.priv -outform DER -out MOK.der -subj '/CN=local module signing/'"
            warn "  $MOD_ROOT/build/scripts/sign-file sha256 MOK.priv MOK.der <module>.ko"
            warn "  sudo mokutil --import MOK.der   # then reboot and enrol it"
            warn "Or turn Secure Boot off in firmware. Everything else here still installs."
        fi
    fi

    msg "Installing modules into $MOD_DIR"
    as_root install -d "$MOD_DIR" || die "could not create $MOD_DIR"
    as_root install -m644 "$SELF_DIR/driver/cx231xx/cx231xx.ko" \
                          "$SELF_DIR/driver/cx231xx/cx231xx-alsa.ko" "$MOD_DIR/" \
        || die "could not install the modules"
    as_root depmod -a "$KVER" || warn "depmod failed"
    make -C "$BUILD_DIR" M="$SELF_DIR/driver/cx231xx" clean >/dev/null 2>&1
else
    msg "Skipping the driver (--no-driver)"
fi

# --- 2. module options ------------------------------------------------------
msg "Installing /etc/modprobe.d/cx231xx.conf (elgato_htl=2)"
as_root install -Dm644 "$SELF_DIR/etc/modprobe.d/cx231xx.conf" /etc/modprobe.d/cx231xx.conf \
    || warn "could not install the modprobe config"

# --- 3. udev rule -----------------------------------------------------------
msg "Installing the udev rule (/dev/elgato, autosuspend off)"
if as_root install -Dm644 "$SELF_DIR/etc/70-elgato-video-capture.rules" \
        /etc/udev/rules.d/70-elgato-video-capture.rules; then
    as_root udevadm control --reload-rules
    as_root udevadm trigger --subsystem-match=video4linux --subsystem-match=usb
else
    warn "could not install the udev rule; /dev/elgato will not be created"
fi

# TAG+="uaccess" is what hands the node to whoever is logged in, and it is
# logind that implements it. Without one the node stays root:video, so on a
# systemd-free system group membership is the way in.
if ! have_logind; then
    warn "no logind here, so the rule's uaccess tag does nothing."
    warn "Join the video group instead, then log out and back in:"
    warn "    sudo usermod -aG video $(id -un)"
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
    elif ! LOOPBACK_PKGS=$(pkg_for v4l2loopback); then
        WITH_SHARE=0
        warn "v4l2loopback is missing, and its package name on $DISTRO_NAME is not known here."
        warn "Install it your distribution's way and re-run, or use --no-share."
    else
        msg "v4l2loopback is needed for 'elgato-viewer --share' (OBS and the"
        msg "viewer at the same time). It builds against your kernel."
        NOTE=$(pkg_note v4l2loopback) && warn "$NOTE"

        reply=n
        if (( ASSUME_YES )); then
            reply=y
        elif [[ -t 0 ]]; then
            read -r -p ":: Install $LOOPBACK_PKGS? [Y/n] " reply
            reply=${reply:-y}
        else
            warn "not running on a terminal and no --yes given, so not installing packages"
        fi

        case "${reply,,}" in
            y|yes)
                # shellcheck disable=SC2086
                pkg_install $LOOPBACK_PKGS
                case $? in
                    0) ;;
                    2) WITH_SHARE=0
                       warn "no automatic install for $PKG; install $LOOPBACK_PKGS yourself" ;;
                    *) WITH_SHARE=0
                       warn "the package install failed; skipping the loopback" ;;
                esac ;;
            *)  WITH_SHARE=0; msg "Skipping -- re-run install.sh to add it later" ;;
        esac
    fi
fi

if (( WITH_SHARE )); then
    msg "Installing the v4l2loopback config (/dev/elgato-share)"
    as_root install -Dm644 "$SELF_DIR/etc/modprobe.d/v4l2loopback-elgato.conf" \
        /etc/modprobe.d/v4l2loopback-elgato.conf \
        || warn "could not install the v4l2loopback modprobe config"
    if as_root install -Dm644 "$SELF_DIR/etc/modules-load.d/v4l2loopback-elgato.conf" \
            /etc/modules-load.d/v4l2loopback-elgato.conf; then
        have_systemd || warn "modules-load.d is a systemd thing; load v4l2loopback at boot your init's way"
    else
        warn "could not install the modules-load config; it will not load at boot"
    fi
    if as_root install -Dm644 "$SELF_DIR/etc/71-elgato-share.rules" \
            /etc/udev/rules.d/71-elgato-share.rules; then
        as_root udevadm control --reload-rules
        as_root udevadm trigger --subsystem-match=video4linux
    else
        warn "could not install the share udev rule; /dev/elgato-share will not appear"
    fi

    # Reload rather than modprobe: if it was already up with other options
    # (another tool's, or an earlier version of this file) ours would be ignored.
    as_root modprobe -r v4l2loopback 2>/dev/null
    if as_root modprobe v4l2loopback; then
        for _ in 1 2 3 4 5; do [[ -c /dev/elgato-share ]] && break; sleep 0.3; done
        if [[ -c /dev/elgato-share ]]; then
            msg "/dev/elgato-share is ready -- try: elgato-viewer --share"
        else
            warn "the module loaded but /dev/elgato-share did not appear."
            warn "Check: v4l2-ctl --list-devices | grep -A1 'Elgato Share'"
        fi
    else
        warn "modprobe v4l2loopback failed -- 'elgato-viewer --share' will not work"
        case "$PKG" in
            dnf|dnf5|yum) warn "If akmods has not built it yet, try: sudo akmods --force" ;;
            *)            warn "If DKMS has not built it yet, try: sudo dkms autoinstall" ;;
        esac
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
    as_root modprobe -r cx231xx_alsa cx231xx 2>/dev/null
    if lsmod | grep -q '^cx231xx '; then
        warn "could not unload the running cx231xx; reboot to pick up the new one"
    else
        as_root modprobe cx231xx      || warn "modprobe cx231xx failed"
        as_root modprobe cx231xx_alsa || warn "modprobe cx231xx_alsa failed"
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
