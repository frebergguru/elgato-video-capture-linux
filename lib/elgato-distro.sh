#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
# Everything the distributions disagree about, in one place.
#
# install.sh and uninstall.sh are the only things here that touch the system
# outside this directory, and that is exactly where distributions differ: what
# the package manager is called, what the kernel headers package is called,
# whether "sudo" is even installed, whether an unsigned module will load at
# all. Sourced by both, after each has defined msg/warn/die.
#
# Nothing in here installs anything by itself. It answers questions and builds
# commands; the callers decide.

# --- PATH -------------------------------------------------------------------
# modinfo, lsmod, modprobe, depmod and udevadm live in /usr/sbin, which Debian
# and Ubuntu leave off a normal user's PATH. The read-only ones are called
# without root, so without this "modinfo v4l2loopback" fails with "command not
# found" and we conclude the module is unavailable when it is installed.
case ":$PATH:" in
    *:/usr/sbin:*) ;;
    *) PATH="$PATH:/usr/sbin:/sbin" ;;
esac
export PATH

# --- who the machine is -----------------------------------------------------
# Only for saying out loud which machine this is. Nothing branches on it: the
# package manager below is found by looking, which is the one test that keeps
# working on a derivative nobody here has heard of.
DISTRO_NAME="this system"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC2034  # read by install.sh and uninstall.sh
    DISTRO_NAME=$(. /etc/os-release 2>/dev/null \
                  && printf '%s' "${PRETTY_NAME:-${NAME:-this system}}")
fi

# The package manager is decided by which binary is actually here, not by the
# ID: derivatives outnumber their parents, and a "manjaro" or "linuxmint" ID
# would otherwise need its own case. Order matters only where a system has two.
PKG=unknown
for _pm in pacman apt-get dnf5 dnf yum zypper xbps-install emerge apk eopkg; do
    if command -v "$_pm" >/dev/null 2>&1; then PKG=$_pm; break; fi
done
unset _pm

# --- becoming root ----------------------------------------------------------
# sudo is not universal: a stock Debian netinstall has none, and some people run
# doas instead. su works everywhere but asks for the root password every time,
# so say so once rather than surprising them eight prompts in.
SUDO_KIND=none
if [[ $EUID -eq 0 ]]; then
    SUDO_KIND=root
    as_root() { "$@"; }
elif command -v sudo >/dev/null 2>&1; then
    SUDO_KIND=sudo
    as_root() { sudo "$@"; }
elif command -v doas >/dev/null 2>&1; then
    SUDO_KIND=doas
    as_root() { doas "$@"; }
elif command -v su >/dev/null 2>&1; then
    SUDO_KIND=su
    as_root() { local _c; printf -v _c '%q ' "$@"; su -c "$_c"; }
else
    as_root() { return 1; }
fi

# Take the password once, up front, so it is not asked for in the middle of a
# module build. Harmless where there is nothing to cache.
prime_root() {
    case "$SUDO_KIND" in
        sudo) sudo -v 2>/dev/null || true ;;
        su)   warn "no sudo or doas here -- each privileged step will ask for the root password" ;;
        none) die "need root for parts of this, but found no sudo, doas or su" ;;
    esac
}

# --- init system and device permissions -------------------------------------
have_systemd() { [[ -d /run/systemd/system ]]; }

# TAG+="uaccess" in the udev rules is what makes /dev/elgato readable by the
# logged-in user. It is implemented by logind -- systemd's or elogind's. Without
# one, the node stays root:video and group membership is the way in.
have_logind() { command -v loginctl >/dev/null 2>&1 || [[ -d /run/systemd/seats ]]; }

# --- kernel modules ---------------------------------------------------------
# Almost everything is usr-merged now, but /lib/modules is not guaranteed to be
# the symlink; ask rather than assume.
module_root() {
    local kver=${1:-$(uname -r)}
    if [[ -d /lib/modules/$kver ]]; then echo "/lib/modules/$kver"
    else echo "/usr/lib/modules/$kver"; fi
}

# Secure Boot refuses unsigned modules with ENOKEY. Fedora, Ubuntu and openSUSE
# ship with it on by default, so this is the common case, not the exotic one.
secure_boot_enabled() {
    if command -v mokutil >/dev/null 2>&1; then
        mokutil --sb-state 2>/dev/null | grep -qi 'secureboot enabled' && return 0
        return 1
    fi
    local f
    for f in /sys/firmware/efi/efivars/SecureBoot-*; do
        [[ -r $f ]] || continue
        # 4 bytes of EFI variable attributes, then the one byte that matters.
        [[ $(od -An -t u1 -j4 -N1 "$f" 2>/dev/null | tr -d ' ') == 1 ]] && return 0
    done
    return 1
}

# Lockdown is the other half: even signed-by-nobody modules are refused when
# the kernel is locked down, which Secure Boot turns on automatically.
lockdown_mode() {
    local f=/sys/kernel/security/lockdown
    [[ -r $f ]] || { echo none; return; }
    sed -n 's/.*\[\([a-z]*\)\].*/\1/p' "$f" 2>/dev/null || echo none
}

# The Machine Owner Keys the distributions enrol for their own DKMS and akmods
# builds. If one is here the firmware already trusts it, and signing with it is
# the whole difference between the module loading and ENOKEY.
MOK_PAIRS=(
    "/var/lib/shim-signed/mok/MOK.priv:/var/lib/shim-signed/mok/MOK.der"
    "/var/lib/dkms/mok.key:/var/lib/dkms/mok.pub"
    "/etc/pki/akmods/private/private_key.priv:/etc/pki/akmods/certs/public_key.der"
)

# sign_module KVER FILE... -> prints the certificate used, or fails if there is
# no usable key here (which is not an error; it just means we cannot help).
sign_module() {
    local kver=$1; shift
    local signer; signer="$(module_root "$kver")/build/scripts/sign-file"
    [[ -x $signer ]] || return 1
    local pair key cert f
    for pair in "${MOK_PAIRS[@]}"; do
        key=${pair%%:*}; cert=${pair##*:}
        as_root test -r "$key" 2>/dev/null || continue
        as_root test -r "$cert" 2>/dev/null || continue
        for f in "$@"; do
            as_root "$signer" sha256 "$key" "$cert" "$f" >/dev/null 2>&1 || return 1
        done
        printf '%s\n' "$cert"
        return 0
    done
    return 1
}

# --- package names ----------------------------------------------------------
# pkg_for LOGICAL -> the package(s) providing it here, or nothing if unknown.
# Used both to install and to say what is missing, so the advice and the action
# can never drift apart.
pkg_for() {
    local kver; kver=$(uname -r)
    case "$PKG:$1" in
        pacman:headers)          echo "linux-headers" ;;
        apt-get:headers)         echo "linux-headers-$kver" ;;
        dnf:headers|dnf5:headers|yum:headers) echo "kernel-devel-$kver" ;;
        zypper:headers)          echo "kernel-default-devel" ;;
        xbps-install:headers)    echo "linux-headers" ;;
        emerge:headers)          echo "sys-kernel/linux-headers" ;;
        apk:headers)             echo "linux-lts-dev" ;;
        eopkg:headers)           echo "linux-current-headers" ;;

        pacman:build)            echo "base-devel" ;;
        apt-get:build)           echo "build-essential" ;;
        dnf:build|dnf5:build|yum:build) echo "gcc make" ;;
        zypper:build)            echo "gcc make" ;;
        xbps-install:build)      echo "base-devel" ;;
        emerge:build)            echo "sys-devel/gcc" ;;
        apk:build)               echo "build-base" ;;
        eopkg:build)             echo "-c system.devel" ;;

        pacman:v4l-utils)        echo "v4l-utils" ;;
        apt-get:v4l-utils)       echo "v4l-utils" ;;
        dnf:v4l-utils|dnf5:v4l-utils|yum:v4l-utils) echo "v4l-utils" ;;
        zypper:v4l-utils)        echo "v4l-utils" ;;
        xbps-install:v4l-utils)  echo "v4l-utils" ;;
        emerge:v4l-utils)        echo "media-libs/libv4l" ;;
        apk:v4l-utils)           echo "v4l-utils" ;;
        eopkg:v4l-utils)         echo "v4l-utils" ;;

        pacman:gstreamer)        echo "gstreamer gst-plugins-base gst-plugins-good" ;;
        apt-get:gstreamer)       echo "gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good" ;;
        dnf:gstreamer|dnf5:gstreamer|yum:gstreamer) echo "gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good" ;;
        zypper:gstreamer)        echo "gstreamer gstreamer-plugins-base gstreamer-plugins-good" ;;
        xbps-install:gstreamer)  echo "gstreamer1 gst-plugins-base1 gst-plugins-good1" ;;
        emerge:gstreamer)        echo "media-libs/gstreamer media-plugins/gst-plugins-meta" ;;
        apk:gstreamer)           echo "gstreamer gst-plugins-base gst-plugins-good" ;;
        eopkg:gstreamer)         echo "gstreamer-1.0 gst-plugins-base-1.0 gst-plugins-good-1.0" ;;

        pacman:gst-libav)        echo "gst-libav" ;;
        apt-get:gst-libav)       echo "gstreamer1.0-libav" ;;
        dnf:gst-libav|dnf5:gst-libav|yum:gst-libav) echo "gstreamer1-libav" ;;
        zypper:gst-libav)        echo "gstreamer-plugins-libav" ;;
        xbps-install:gst-libav)  echo "gst-libav" ;;
        emerge:gst-libav)        echo "media-plugins/gst-plugins-libav" ;;
        apk:gst-libav)           echo "gst-libav" ;;
        eopkg:gst-libav)         echo "gst-libav-1.0" ;;

        pacman:pipewire)         echo "pipewire pipewire-audio wireplumber" ;;
        apt-get:pipewire)        echo "pipewire pipewire-pulse wireplumber" ;;
        dnf:pipewire|dnf5:pipewire|yum:pipewire) echo "pipewire pipewire-utils wireplumber" ;;
        zypper:pipewire)         echo "pipewire pipewire-tools wireplumber" ;;
        xbps-install:pipewire)   echo "pipewire wireplumber" ;;
        emerge:pipewire)         echo "media-video/pipewire media-video/wireplumber" ;;
        apk:pipewire)            echo "pipewire wireplumber" ;;
        eopkg:pipewire)          echo "pipewire wireplumber" ;;

        pacman:python)           echo "python" ;;
        *:python)                echo "python3" ;;

        pacman:v4l2loopback)     echo "dkms v4l2loopback-dkms v4l2loopback-utils" ;;
        apt-get:v4l2loopback)    echo "v4l2loopback-dkms v4l2loopback-utils" ;;
        dnf:v4l2loopback|dnf5:v4l2loopback|yum:v4l2loopback) echo "akmod-v4l2loopback" ;;
        zypper:v4l2loopback)     echo "v4l2loopback-kmp-default" ;;
        xbps-install:v4l2loopback) echo "v4l2loopback-dkms" ;;
        emerge:v4l2loopback)     echo "media-video/v4l2loopback" ;;
        apk:v4l2loopback)        echo "v4l2loopback-lts" ;;

        *) return 1 ;;
    esac
}

# Where a package is not in the default repositories, saying so beats watching
# the package manager fail with "no match found".
pkg_note() {
    case "$PKG:$1" in
        pacman:headers)
            echo "match it to your kernel: linux-lts-headers, linux-zen-headers, linux6xx-headers" ;;
        dnf:v4l2loopback|dnf5:v4l2loopback|yum:v4l2loopback)
            echo "this lives in RPM Fusion (free); enable that repository first" ;;
        zypper:v4l2loopback|zypper:gst-libav)
            echo "this lives in Packman; add that repository first" ;;
        dnf:gst-libav|dnf5:gst-libav|yum:gst-libav)
            echo "this lives in RPM Fusion (free); enable that repository first" ;;
        *) return 1 ;;
    esac
}

# --- installing -------------------------------------------------------------
# Set to 1 by a caller that has already asked the user, so the package manager
# does not ask the same question again.
PKG_YES=${PKG_YES:-0}
_APT_UPDATED=0

# pkg_install PACKAGE... -> whatever the package manager returned; 2 if there is
# no way to do it here, so callers can tell "failed" from "not supported".
pkg_install() {
    (( $# )) || return 0
    local -a cmd
    case "$PKG" in
        pacman)
            cmd=(pacman -S --needed); (( PKG_YES )) && cmd+=(--noconfirm) ;;
        apt-get)
            # A container or a long-untouched install has stale lists, and
            # apt-get then fails on packages that are perfectly available.
            if (( ! _APT_UPDATED )); then
                as_root apt-get update >/dev/null 2>&1 && _APT_UPDATED=1
            fi
            cmd=(apt-get install); (( PKG_YES )) && cmd+=(-y) ;;
        dnf|dnf5|yum)
            cmd=("$PKG" install); (( PKG_YES )) && cmd+=(-y) ;;
        zypper)
            cmd=(zypper); (( PKG_YES )) && cmd+=(--non-interactive); cmd+=(install) ;;
        xbps-install)
            cmd=(xbps-install -S); (( PKG_YES )) && cmd+=(-y) ;;
        apk)
            cmd=(apk add) ;;
        eopkg)
            cmd=(eopkg install); (( PKG_YES )) && cmd+=(-y) ;;
        emerge)
            # Portage builds from source and wants to be driven by its owner.
            warn "Portage installs are yours to run:  sudo emerge --ask $*"
            return 2 ;;
        *)
            return 2 ;;
    esac
    as_root "${cmd[@]}" "$@"
}

# pkg_remove_hint PACKAGE... -> the command that would remove them, for advice
# only. Nothing here ever removes a package.
pkg_remove_hint() {
    case "$PKG" in
        pacman)       echo "sudo pacman -Rns $*" ;;
        apt-get)      echo "sudo apt-get purge $*" ;;
        dnf|dnf5|yum) echo "sudo $PKG remove $*" ;;
        zypper)       echo "sudo zypper remove $*" ;;
        xbps-install) echo "sudo xbps-remove -R $*" ;;
        apk)          echo "sudo apk del $*" ;;
        eopkg)        echo "sudo eopkg remove $*" ;;
        emerge)       echo "sudo emerge --deselect $*" ;;
        *)            echo "your package manager's removal command for: $*" ;;
    esac
}

# advise_missing TOOL LOGICAL -> one warning naming the package for this distro
advise_missing() {
    local tool=$1 logical=$2 pkgs note
    if pkgs=$(pkg_for "$logical"); then
        warn "$tool is missing -- install: $pkgs"
    else
        warn "$tool is missing -- install your distribution's $logical package"
    fi
    note=$(pkg_note "$logical") && warn "    ($note)"
    return 0
}
