#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
"""Windowed player for elgato-viewer, with keyboard control.

Why this exists rather than gst-launch-1.0:

  * gst-launch has no keyboard plumbing at all -- there is no input option, and
    no way to set a property at runtime. Nothing interactive can be built on it.
  * waylandsink does not implement GstNavigation, so even with a controlling
    process it can never deliver a key press from its own window.
  * gtksink/gtkglsink, which would, are not built on Arch/Manjaro.

gtk4paintablesink hands us the video as a GdkPaintable, so the window is an
ordinary Gtk.Window: keys, fullscreen and resizing are GTK's problem, not
GStreamer's.

Resizing renegotiates the pipeline's output capsfilter instead of restarting
anything, so a size change costs no frames. Scaling stays in videoscale with the
method the caller chose, which is what keeps --sharp genuinely
nearest-neighbour rather than letting GTK resample it.

Note: gst-python is not required, and this deliberately avoids the constructs
that need it (Gst.Fraction, Gst.ValueArray, multi-argument Bin.add). Caps are
built from strings.
"""

import argparse
import re
import signal
import subprocess
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gst, Gtk  # noqa: E402

# GLib.unix_signal_add moved to GLibUnix and warns on newer PyGObject; keep
# working on both rather than printing a deprecation notice over the output.
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402
    _unix_signal_add = GLibUnix.signal_add
except (ValueError, ImportError):
    _unix_signal_add = GLib.unix_signal_add

# 4:3 rungs. z steps down, x steps up; a custom --size snaps to the nearest.
LADDER = [
    (640, 480), (800, 600), (960, 720), (1120, 840),
    (1280, 960), (1440, 1080), (1600, 1200), (1920, 1440),
]

TOAST_MS = 1500

CSS = b"""
.toast {
  background-color: rgba(0,0,0,0.78);
  color: #ffffff;
  padding: 8px 14px;
  margin: 18px;
  border-radius: 6px;
  font-size: 15pt;
}
.help {
  background-color: rgba(0,0,0,0.86);
  color: #ffffff;
  padding: 20px 28px;
  border-radius: 10px;
  font-family: monospace;
  font-size: 12pt;
}
"""

# The helper writes to a terminal, so its messages carry colour escapes and a
# "!!" marker. A GTK label would render those literally.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean(text):
    return ANSI.sub("", text).lstrip("!:* ").strip()


# Measured on 400 frames of real motion from this capture card:
#   yadif    comb 0.31  v-detail 86.0   latency 5.1ms
#   greedyh  comb 0.61  v-detail 86.3   latency 1.7ms
#   linear   comb 1.04  v-detail 73.6   latency 0.3ms
# yadif is best on picture and costs 5ms, so it is the default; the others are
# here for anyone who would rather have the last few milliseconds.
DEINTERLACERS = [("yadif", 10), ("greedyh", 1), ("linear", 4)]

KEYS = [
    ("f", "fullscreen on/off"),
    ("z", "smaller window"),
    ("x", "larger window"),
    ("c", "cycle deinterlacer (picture quality)"),
    ("m", "mute/unmute the capture audio"),
    ("h", "this help"),
    ("q", "quit"),
    ("Esc", "close help, or quit"),
]


class Player:
    def __init__(self, args):
        self.args = args
        self.exit_code = 0
        self.toast_id = 0
        self.help_open = False
        self.saved_caps = None      # rung to restore when leaving fullscreen

        Gst.init(None)
        self.pipeline = Gst.parse_launch(args.pipeline)
        self.sink = self.pipeline.get_by_name("vsink")
        self.capsfilter = self.pipeline.get_by_name("outcaps")
        self.deint = self.pipeline.get_by_name("deint")
        self.di_idx = 0
        if self.deint is not None:
            cur = self.deint.get_property("method")
            for i, (_n, v) in enumerate(DEINTERLACERS):
                if v == cur:
                    self.di_idx = i
        if self.sink is None:
            raise RuntimeError("pipeline has no element named 'vsink'")

        # Fetch the paintable here, not in build(). An exception raised inside
        # GTK's activate handler is printed and swallowed, leaving an app with
        # no window and no way out -- it has to fail before the loop starts.
        try:
            self.paintable = self.sink.get_property("paintable")
        except TypeError:
            raise RuntimeError(
                "'vsink' is a %s, which has no paintable -- expected "
                "gtk4paintablesink" % self.sink.__class__.__name__)
        if self.paintable is None:
            raise RuntimeError("'vsink' returned no paintable")

        self.idx = self._nearest_rung(args.width, args.height)

    # ---------------------------------------------------------------- setup --
    def _nearest_rung(self, w, h):
        return min(range(len(LADDER)), key=lambda i: abs(LADDER[i][0] - w))

    def build(self, app):
        self.window = Gtk.ApplicationWindow(application=app)
        self.window.set_title(self.args.title)
        self.window.set_default_size(self.args.width, self.args.height)

        picture = Gtk.Picture()
        picture.set_paintable(self.paintable)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)

        self.overlay = Gtk.Overlay()
        self.overlay.set_child(picture)

        self.toast = Gtk.Label(label="")
        self.toast.add_css_class("toast")
        self.toast.set_halign(Gtk.Align.START)
        self.toast.set_valign(Gtk.Align.END)
        self.toast.set_visible(False)
        self.overlay.add_overlay(self.toast)

        self.help = Gtk.Label()
        self.help.add_css_class("help")
        self.help.set_halign(Gtk.Align.CENTER)
        self.help.set_valign(Gtk.Align.CENTER)
        self.help.set_visible(False)
        self.help.set_justify(Gtk.Justification.LEFT)
        self.overlay.add_overlay(self.help)

        self.window.set_child(self.overlay)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS.decode())
        except AttributeError:          # GTK < 4.12
            provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.window.add_controller(keys)
        self.window.connect("close-request", self.on_close)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_error)
        bus.connect("message::eos", self.on_eos)

        # Quit cleanly when elgato-viewer's cleanup trap signals us. Python's
        # own signal handling does not run while blocked inside the GLib loop,
        # so without this the caller waits out its grace period and resorts to
        # SIGKILL -- which works, but tears the pipeline down mid-frame.
        for sig in (signal.SIGINT, signal.SIGTERM):
            _unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.on_signal)

        self.window.present()
        if self.args.fullscreen:
            self.window.fullscreen()
        self.pipeline.set_state(Gst.State.PLAYING)

    def on_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    # ----------------------------------------------------------------- keys --
    def on_key(self, _ctrl, keyval, _code, _state):
        name = Gdk.keyval_name(keyval)
        if name in ("f", "F"):
            self.toggle_fullscreen()
        elif name in ("z", "Z"):
            self.step(-1)
        elif name in ("x", "X"):
            self.step(+1)
        elif name in ("c", "C"):
            self.cycle_deinterlace()
        elif name in ("m", "M"):
            self.toggle_mute()
        elif name in ("h", "H"):
            self.toggle_help()
        elif name in ("q", "Q"):
            self.quit()
        elif name == "Escape":
            if self.help_open:
                self.toggle_help()
            else:
                self.quit()
        else:
            return False
        return True

    def toggle_fullscreen(self):
        if self.window.is_fullscreen():
            self.window.unfullscreen()
            if self.saved_caps is not None:
                self.set_caps(*self.saved_caps)
                self.saved_caps = None
            self.show_toast("windowed")
        else:
            # Scale in videoscale to the monitor rather than letting GTK stretch
            # the paintable, so --sharp stays sharp when it matters most.
            self.saved_caps = LADDER[self.idx]
            geo = self.monitor_geometry()
            if geo:
                w = min(geo[0], (geo[1] * 4) // 3)
                self.set_caps(w - w % 2, (w // 4 * 3) - ((w // 4 * 3) % 2))
            self.window.fullscreen()
            self.show_toast("fullscreen")

    def monitor_geometry(self):
        try:
            surface = self.window.get_surface()
            monitor = self.window.get_display().get_monitor_at_surface(surface)
            r = monitor.get_geometry()
            return (r.width, r.height)
        except Exception:
            return None

    def step(self, delta):
        if self.window.is_fullscreen():
            self.show_toast("resize only in windowed mode")
            return
        new = self.idx + delta
        if new < 0 or new >= len(LADDER):
            self.show_toast("%dx%d (limit)" % LADDER[self.idx])
            return
        self.idx = new
        w, h = LADDER[self.idx]
        self.set_caps(w, h)
        self.window.set_default_size(w, h)
        self.show_toast("%dx%d" % (w, h))

    def set_caps(self, w, h):
        if self.capsfilter is None:
            return
        self.capsfilter.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,width=%d,height=%d,pixel-aspect-ratio=(fraction)1/1"
            % (w, h)))

    def cycle_deinterlace(self):
        if self.deint is None:
            self.show_toast("no deinterlacer in this pipeline")
            return
        self.di_idx = (self.di_idx + 1) % len(DEINTERLACERS)
        nm, val = DEINTERLACERS[self.di_idx]
        # The property is not documented as changeable in PLAYING, but it is --
        # verified by setting it mid-stream and reading it back.
        self.deint.set_property("method", val)
        got = self.deint.get_property("method")
        self.show_toast("deinterlace: %s" % nm if got == val
                        else "deinterlace unchanged (%s refused)" % nm)

    def toggle_mute(self):
        if not self.args.audio_helper:
            self.show_toast("no audio helper")
            return
        try:
            r = subprocess.run([self.args.audio_helper, "mute-toggle"],
                               capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as e:
            self.show_toast("mute failed: %s" % e)
            return
        state = r.stdout.strip()
        if r.returncode != 0 or state not in ("muted", "unmuted"):
            # Say what went wrong rather than claim a state we did not reach.
            detail = r.stderr.strip().splitlines()
            self.show_toast(clean(detail[-1]) if detail else "mute failed")
            return
        self.show_toast("audio " + state)

    def toggle_help(self):
        self.help_open = not self.help_open
        if self.help_open:
            self.help.set_text(self.help_text())
        self.help.set_visible(self.help_open)

    def help_text(self):
        rows = "\n".join("  %-5s %s" % (k, d) for k, d in KEYS)
        w, h = LADDER[self.idx]
        di = DEINTERLACERS[self.di_idx][0] if self.deint is not None else "off"
        return ("elgato-viewer\n\n%s\n\n  size    %dx%d\n  deint   %s\n"
                "  device  %s\n  input   %s"
                % (rows, w, h, di, self.args.device or "?", self.args.input or "?"))

    # ----------------------------------------------------------------- toast --
    def show_toast(self, text):
        self.toast.set_text(text)
        self.toast.set_visible(True)
        if self.toast_id:
            GLib.source_remove(self.toast_id)
        self.toast_id = GLib.timeout_add(TOAST_MS, self.hide_toast)

    def hide_toast(self):
        self.toast.set_visible(False)
        self.toast_id = 0
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------ bus --
    def on_error(self, _bus, message):
        err, debug = message.parse_error()
        print("elgato-player: %s" % err.message, file=sys.stderr)
        if debug:
            print(debug, file=sys.stderr)
        self.exit_code = 1
        self.quit()

    def on_eos(self, _bus, _message):
        # The capture is live; EOS means the source went away.
        print("elgato-player: stream ended", file=sys.stderr)
        self.exit_code = 1
        self.quit()

    def on_close(self, _window):
        self.quit()
        return True

    def quit(self):
        if self.toast_id:
            GLib.source_remove(self.toast_id)
            self.toast_id = 0
        self.pipeline.set_state(Gst.State.NULL)
        # Audio is stopped by the caller's cleanup trap, never from here --
        # elgato-viewer owns the loopback's lifetime.
        self.app.quit()


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--pipeline", required=True)
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--audio-helper", default="")
    p.add_argument("--title", default="Elgato Video Capture")
    p.add_argument("--device", default="")
    p.add_argument("--input", default="")
    args = p.parse_args()

    try:
        player = Player(args)
    except Exception as e:
        print("elgato-player: %s" % e, file=sys.stderr)
        return 1

    app = Gtk.Application(application_id="org.hypnotize.ElgatoViewer")
    player.app = app
    app.connect("activate", player.build)
    app.run([])
    return player.exit_code


if __name__ == "__main__":
    sys.exit(main())
