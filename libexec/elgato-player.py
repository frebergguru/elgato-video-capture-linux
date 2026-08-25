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

The r and s keys add and remove a branch on the tee that elgato-viewer puts
ahead of the deinterlacer, while the pipeline keeps playing -- see Branch, where
the awkward part (finishing a muxed file without stalling the picture) is
explained.

Note: gst-python is not required, and this deliberately avoids the constructs
that need it (Gst.Fraction, Gst.ValueArray, multi-argument Bin.add). Caps are
built from strings.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

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
.rec {
  background-color: rgba(0,0,0,0.78);
  color: #ff6b6b;
  padding: 6px 12px;
  margin: 18px;
  border-radius: 6px;
  font-size: 13pt;
}
"""

# The helper writes to a terminal, so its messages carry colour escapes and a
# "!!" marker. A GTK label would render those literally.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean(text):
    return ANSI.sub("", text).lstrip("!:* ").strip()


def mmss(seconds):
    seconds = int(seconds)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def human(size):
    if size >= 1 << 30:
        return "%.1f GB" % (size / float(1 << 30))
    return "%d MB" % (size >> 20)


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def in_pipeline(obj, pipeline):
    while obj is not None:
        if obj is pipeline:
            return True
        obj = obj.get_parent()
    return False


def free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return -1                  # unknown: do not stand in the way


# A branch that errors does not fail alone -- the flow error travels back
# through the tee and v4l2src posts "internal data stream error", which stops
# the picture as well. On a recording the one realistic way that happens is a
# full disk, so it is headed off instead of handled: refuse to start with less
# than the first figure free, and stop cleanly on the way down to the second
# rather than let filesink discover ENOSPC.
RECORD_MIN_FREE = 1 << 30          # 1 GB to start
RECORD_STOP_FREE = 512 << 20       # 512 MB to keep going


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
    ("r", "start/stop recording"),
    ("s", "save a screenshot"),
    ("h", "this help"),
    ("q", "quit"),
    ("Esc", "close help, or quit"),
]

# How long to wait for a branch to finish its EOS handshake before giving up on
# it. Stopping a recording has to travel the length of the branch and let
# matroskamux write its index: measured at 103ms for a 15-second FFV1 file, so
# five seconds is not a budget, it is a backstop -- it exists so that a wedged
# branch cannot leave the window with a REC indicator that will never go out.
BRANCH_EOS_TIMEOUT_MS = 5000

# How long to leave a branch alone after its EOS has been seen at the sink.
#
# A pad probe runs *before* the element it is watching handles the event, so
# seeing EOS at the file sink's pad means the sink is about to write its last
# bytes and close, not that it has. Tearing the branch down on that signal
# raced the close: about one screenshot in four, taken while several others
# were in flight, ended as "Error while writing to file" out of
# gst_file_sink_event and a zero-byte PNG. Waiting removes the overlap
# entirely -- the branch is already unlinked and gated by this point, so
# nothing arrives during the wait and the only cost is that a screenshot
# branch lives a fifth of a second longer than it needs to.
BRANCH_SETTLE_MS = 200


# Package hints for the encoders, so a missing plugin says what to install
# rather than "no element avenc_ffv1".
CODEC_PKG = {"ffv1": ("avenc_ffv1", "gst-libav"),
             "h264": ("x264enc", "gst-plugins-ugly")}


class BranchError(Exception):
    """A branch could not be built or attached. Never fatal: the picture keeps
    playing and the reason goes on screen."""


def make(factory, **props):
    el = Gst.ElementFactory.make(factory, None)
    if el is None:
        raise BranchError("this GStreamer has no '%s'" % factory)
    for k, v in props.items():
        el.set_property(k.replace("_", "-"), v)
    return el


def caps_filter(text):
    return make("capsfilter", caps=Gst.Caps.from_string(text))


def chain_into(container, elements):
    """Add elements to a bin in order and link them into a chain."""
    for el in elements:
        container.add(el)
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise BranchError("cannot link %s to %s"
                              % (a.get_factory().get_name(),
                                 b.get_factory().get_name()))


def request_pad(element, template):
    # request_pad_simple arrived in GStreamer 1.20 and deprecated
    # get_request_pad; take whichever this install has.
    fn = getattr(element, "request_pad_simple", None)
    if fn is None:
        fn = element.get_request_pad
    return fn(template)


class Branch:
    """A tap that is added to the running pipeline and taken out again.

    Recording and screenshots are the same problem: hang something off the tee
    the pipeline already has, let it run, then detach it without disturbing the
    picture. The awkward half is detaching. A file sink can be unlinked at any
    time, but a *muxer* has to be told the stream ended -- EOS is what makes
    matroskamux go back and write its index and duration -- and that message
    has to travel the length of the branch before the elements may be freed.

    So stopping is a handshake, not a call: block the tee pad on an idle probe
    (which fires between buffers, so nothing is ever cut in half), unlink,
    push EOS in at the top, wait for a probe on the sink's pad to see it come
    out of the bottom, and only then -- after a pause, because that probe runs
    before the sink has finished with the event -- take the bin out.

    A screenshot is the same thing with a gate on the end: the first frame out
    of the branch is kept, the rest are dropped, and the stop is triggered from
    there. It is worth saying why it is not simpler. pngenc has a snapshot mode
    that ends the stream by itself after one frame, which would need no gate at
    all -- but it does that by returning GST_FLOW_EOS *upstream*, the tee makes
    that EOS sticky, and every other branch on it dies too. Nothing may hand
    this tee an EOS from below.

    Everything that touches the pipeline's topology happens on the main loop:
    probe callbacks run on a streaming thread, and adding or removing elements
    from there deadlocks.
    """

    def __init__(self, player, name, container, sink, audio_src=None,
                 done_cb=None, oneshot=False, gate_pad=None):
        self.player = player
        self.name = name
        self.bin = container
        self.sink = sink
        self.audio_src = audio_src
        self.done_cb = done_cb
        self.oneshot = oneshot
        # Where the one-shot gate sits. At the *end* of the branch, not the
        # start: gating the input would hand the deinterlacer a single frame,
        # and yadif needs the field after the one it is producing, so it would
        # emit nothing at all. Measured -- a screenshot taken during a
        # recording wrote a zero-byte file. Let the whole branch run and take
        # the first thing that comes out of it instead.
        self.gate_pad = gate_pad
        self.ghost = container.get_static_pad("sink")
        self.tee_pad = None
        self.unlinked = False
        self.finished = False
        self.got_frame = False
        self.timeout_id = 0

    # -------------------------------------------------------------- attach --
    def attach(self):
        tee = self.player.tee
        self.tee_pad = request_pad(tee, "src_%u")
        if self.tee_pad is None:
            raise BranchError("the pipeline's tee would not give up a pad")

        # Nothing here shifts timestamps. The viewer may have been playing for
        # an hour before r was pressed, so the buffers arriving at this branch
        # carry an hour of running time and a file written from them would open
        # with an hour of nothing -- but the fix belongs in the muxer, not
        # here. matroskamux offset-to-zero rebases every stream by the earliest
        # one, which brings the file back to zero *and* leaves the offset
        # between video and audio exactly as it was.
        #
        # Pad offsets were tried first and are the wrong tool: shifting a live
        # source's segment by the running time at the moment it is added lands
        # its first buffers fractionally before zero, the running time wraps,
        # and matroskamux writes a duration of 2**64-1 ns over an audio track
        # that will not decode. Measured, twice.

        try:
            if not self.player.pipeline.add(self.bin):
                raise BranchError("the pipeline would not take the %s branch"
                                  % self.name)
            # Arm the EOS probe before anything can flow: a screenshot
            # branch lives about a tenth of a second, and its EOS must not
            # arrive before there is something watching for it.
            self.sink.get_static_pad("sink").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_sink_event)
            if self.oneshot:
                # One frame out, then the gate shuts and the branch stops
                # itself.
                #
                # It is tempting to let pngenc snapshot=true do this -- it
                # emits EOS after a frame and needs no gate at all. Measured:
                # that also returns GST_FLOW_EOS *upstream*, the tee makes EOS
                # sticky on its sink pad, and every other branch is torn down
                # with it. A screenshot taken while recording ended the
                # recording. Nothing may ever hand this tee an EOS from below,
                # so the frames are dropped here instead and the stop comes
                # from us, exactly as it does for a recording.
                gate = self.gate_pad if self.gate_pad is not None \
                    else self.ghost
                gate.add_probe(Gst.PadProbeType.BUFFER, self._gate)
                # And if nothing ever comes out -- a starved deinterlacer, a
                # source that stopped -- give up rather than leave the branch
                # attached for the rest of the session.
                self.timeout_id = GLib.timeout_add(BRANCH_EOS_TIMEOUT_MS,
                                                   self._timed_out)
            self.bin.sync_state_with_parent()
            if self.tee_pad.link(self.ghost) != Gst.PadLinkReturn.OK:
                raise BranchError("cannot link the tee to the %s branch"
                                  % self.name)
        except Exception:
            self.abandon()
            raise

    def abandon(self):
        """Undo a half-built attach. Only for the failure path."""
        self.finished = True
        try:
            self.bin.set_state(Gst.State.NULL)
            self.player.pipeline.remove(self.bin)
        except Exception:
            pass
        if self.tee_pad is not None:
            self.player.tee.release_request_pad(self.tee_pad)
            self.tee_pad = None

    def _gate(self, _pad, _info):
        if self.got_frame:
            return Gst.PadProbeReturn.DROP
        self.got_frame = True
        GLib.idle_add(self._stop_from_gate)
        return Gst.PadProbeReturn.OK

    def _stop_from_gate(self):
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = 0
        self.stop()
        return GLib.SOURCE_REMOVE

    # ---------------------------------------------------------------- stop --
    def stop(self, done_cb=None):
        if self.finished or self.unlinked:
            return
        if done_cb is not None:
            self.done_cb = done_cb
        self.timeout_id = GLib.timeout_add(BRANCH_EOS_TIMEOUT_MS,
                                           self._timed_out)
        self.tee_pad.add_probe(Gst.PadProbeType.IDLE, self._on_idle_stop)

    def _on_idle_stop(self, pad, _info):
        pad.unlink(self.ghost)
        self.unlinked = True
        self.ghost.send_event(Gst.Event.new_eos())
        if self.audio_src is not None:
            # A live source is ended by sending EOS to the element, which
            # basesrc turns into a stop plus an EOS of its own downstream.
            # Without it the muxer waits for an audio track that never ends.
            self.audio_src.send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.REMOVE

    # -------------------------------------------------------------- finish --
    def _on_sink_event(self, _pad, info):
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.EOS:
            GLib.timeout_add(BRANCH_SETTLE_MS, self._finish)
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.PASS

    def _finish(self):
        if self.finished:
            return GLib.SOURCE_REMOVE
        if self.unlinked:
            self._teardown()
        else:
            # Ended by itself, so it is still attached; take it off the tee on
            # an idle probe rather than from here, where an unlink would race
            # a buffer being pushed into it.
            self.tee_pad.add_probe(Gst.PadProbeType.IDLE, self._on_idle_finish)
        return GLib.SOURCE_REMOVE

    def _on_idle_finish(self, pad, _info):
        pad.unlink(self.ghost)
        self.unlinked = True
        GLib.idle_add(self._teardown)
        return Gst.PadProbeReturn.REMOVE

    def _timed_out(self):
        self.timeout_id = 0
        self._teardown()
        return GLib.SOURCE_REMOVE

    def _teardown(self):
        if self.finished:
            return GLib.SOURCE_REMOVE
        self.finished = True
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = 0
        if self.tee_pad is not None and self.ghost.is_linked():
            self.tee_pad.unlink(self.ghost)
        self.bin.set_state(Gst.State.NULL)
        self.player.pipeline.remove(self.bin)
        if self.tee_pad is not None:
            self.player.tee.release_request_pad(self.tee_pad)
            self.tee_pad = None
        if self.done_cb is not None:
            self.done_cb(self)
        return GLib.SOURCE_REMOVE

    def owns(self, obj):
        """Whether a bus message came from inside this branch."""
        while obj is not None:
            if obj is self.bin:
                return True
            obj = obj.get_parent()
        return False


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
        # r and s hang their branches off this. elgato-viewer always puts it
        # in, ahead of the deinterlacer, so what is recorded or saved is what
        # the card delivered rather than what this window happens to be
        # showing.
        self.tee = self.pipeline.get_by_name("tap")
        self.scale = self.pipeline.get_by_name("vscale")
        self.record = None          # the live recording Branch, or None
        self.record_path = ""
        self.record_start = 0.0
        self.rec_timer = 0
        self.branches = []          # every branch currently attached
        # Bin names have to be unique among a pipeline's children, and two
        # screenshots a tenth of a second apart really do overlap -- the second
        # add fails and the branch cannot be linked. Number them.
        self.branch_seq = 0
        self.quit_pending = False
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

        # Its own label rather than a toast: a toast is a notification and
        # hides itself after a second and a half, and the one thing you must be
        # able to check at any moment is whether this is still recording.
        self.rec_label = Gtk.Label(label="")
        self.rec_label.add_css_class("rec")
        self.rec_label.set_halign(Gtk.Align.END)
        self.rec_label.set_valign(Gtk.Align.START)
        self.rec_label.set_visible(False)
        self.overlay.add_overlay(self.rec_label)

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
        elif name in ("r", "R"):
            self.toggle_record()
        elif name in ("s", "S"):
            self.take_shot()
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

    # -------------------------------------------------------------- capture --
    #
    # r and s both hang a branch off the tee that elgato-viewer puts ahead of
    # the deinterlacer, so both see the frames as the card delivered them --
    # interlaced, 720 samples across -- rather than whatever this window has
    # been resized to. What each does with them differs: a recording keeps them
    # exactly as they are, a screenshot deinterlaces and squares the samples up,
    # because one is an archive and the other is a picture to look at.

    def capture_path(self, directory, ext):
        directory = os.path.expanduser(directory)
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, "elgato-%s.%s" % (stamp, ext))
        n = 2
        while os.path.exists(path):
            path = os.path.join(directory, "elgato-%s-%d.%s" % (stamp, n, ext))
            n += 1
        return path

    def deint_method(self):
        # Match what is on screen. Somebody who cycled to greedyh with c is
        # telling us which trade-off they want; a still or an h264 recording
        # that quietly used something else would not be what they were looking
        # at when they pressed the key.
        if self.deint is not None:
            return self.deint.get_property("method")
        return DEINTERLACERS[0][1]

    def scale_method(self):
        # Likewise --sharp: nearest-neighbour on screen, nearest-neighbour in
        # the file.
        if self.scale is not None:
            return self.scale.get_property("method")
        return 1                                  # bilinear, videoscale default

    def branch_name(self, kind):
        self.branch_seq += 1
        return "%s-%d" % (kind, self.branch_seq)

    def forget_branch(self, branch):
        if branch in self.branches:
            self.branches.remove(branch)

    # ------------------------------------------------------------ recording --
    def toggle_record(self):
        if self.record is not None:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        if self.tee is None:
            self.show_toast("this pipeline has no tee to record from")
            return
        codec = self.args.record_codec
        element, package = CODEC_PKG[codec]
        if Gst.ElementFactory.find(element) is None:
            self.show_toast("%s recording needs %s (%s)"
                            % (codec, element, package))
            return
        try:
            path = self.capture_path(self.args.record_dir, "mkv")
        except OSError as exc:
            self.show_toast("cannot record: %s" % exc)
            return

        free = free_bytes(os.path.dirname(path))
        if 0 <= free < RECORD_MIN_FREE:
            self.show_toast("only %s free -- not starting a recording"
                            % human(free))
            return

        try:
            container, sink, audio_src = self.build_record_bin(path, codec)
        except (BranchError, OSError) as exc:
            self.show_toast("cannot record: %s" % exc)
            return

        branch = Branch(self, "record", container, sink, audio_src=audio_src)
        try:
            branch.attach()
        except BranchError as exc:
            self.show_toast("cannot record: %s" % exc)
            return

        self.branches.append(branch)
        self.record = branch
        self.record_path = path
        self.record_start = time.monotonic()
        self.rec_label.set_visible(True)
        self.update_rec()
        self.rec_timer = GLib.timeout_add_seconds(1, self.update_rec)
        sound = "with sound" if audio_src is not None else "no sound"
        self.show_toast("recording %s  (%s, %s)"
                        % (os.path.basename(path), codec, sound))

    def build_record_bin(self, path, codec):
        container = Gst.Bin.new(self.branch_name("record"))

        # Deep and time-bounded rather than the two or three buffers the live
        # taps use. Leaking is still the last resort -- a stalled disk must
        # never stall the picture -- but a leak here drops frames out of the
        # middle of the recording, so give the encoder two whole seconds of
        # slack to catch up in before it comes to that.
        video = [
            make("queue", max_size_buffers=0, max_size_bytes=0,
                 max_size_time=2 * Gst.SECOND, leaky=2),
            # The card says "interlaced" but not which field comes first, so
            # ffprobe reports field_order=unknown and a player has to guess --
            # guess wrong and motion steps backwards. Say it here. Top field
            # first is what elgato-obs-setup already defaults to for this
            # hardware; --field-order bottom is the escape hatch, same as the
            # OBS tool's --field.
            make("capssetter", caps=Gst.Caps.from_string(
                "video/x-raw,field-order=(string)%s" % self.args.field_order)),
        ]
        if codec == "ffv1":
            video += [
                make("videoconvert"),
                # Pin 4:2:2. avenc_ffv1 advertises I420 first, so a bare
                # videoconvert negotiates it and throws away half the chroma --
                # a lossless codec quietly fed a lossy conversion. YUY2 to Y42B
                # is a repack of the same samples and loses nothing.
                caps_filter("video/x-raw,format=Y42B"),
                # Measured on 720x576 snow, the worst case this codec can be
                # handed: 10x real time, about 0.65 of one core at 25fps.
                make("avenc_ffv1", threads=0, slices=16, coder=-2, context=1),
            ]
        else:
            video += [
                make("deinterlace", mode=1, method=self.deint_method()),
                make("videoconvert"),
                # 4:2:0, explicitly. Left alone, x264enc negotiates 4:4:4 off
                # this 4:2:2 source and produces High 4:4:4 Predictive, which
                # is larger and which most players and every hardware decoder
                # refuse. The whole point of this mode is a file that opens
                # anywhere.
                caps_filter("video/x-raw,format=I420"),
                make("x264enc", **{"pass": 4, "quantizer": 18,
                                   "speed-preset": 5}),
            ]

        mux = make("matroskamux",
                   writing_app="elgato-viewer",
                   # FFV1 is all-intra, so every frame is a keyframe and the
                   # default index interval of 0 writes a cue entry for each
                   # one -- 90,000 of them in an hour. One a second seeks just
                   # as well and costs nothing.
                   min_index_interval=Gst.SECOND,
                   # The pad offset in Branch.attach already brings the branch
                   # back to near zero; this takes off the remaining few
                   # milliseconds, and does it to video and audio together so
                   # their relative offset is untouched.
                   offset_to_zero=True)
        sink = make("filesink",
                    **{"location": path, "sync": False, "async": False})

        # Audio is built before anything joins the bin, so that a missing
        # encoder or an absent capture source costs the sound and not the
        # recording.
        audio = []
        audio_src = None
        if self.args.audio_node:
            try:
                audio_src = make("pulsesrc", device=self.args.audio_node,
                                 provide_clock=False)
                audio = [
                    audio_src,
                    make("queue", max_size_time=2 * Gst.SECOND, leaky=2),
                    make("audioconvert"),
                    make("audioresample"),
                    # Pin the format to what the node really is. Left to
                    # negotiate freely against a flexible sink, pulsesrc
                    # settles on its own defaults rather than the device's --
                    # measured: 44100 mono off a node that was 48000 stereo,
                    # which is a resample and a lost channel, silently. The
                    # figures come from PipeWire via elgato-audio, so a mono
                    # feed is recorded as mono rather than forced to stereo.
                    caps_filter("audio/x-raw,format=S16LE,rate=%d,channels=%d"
                                % (self.args.audio_rate,
                                   self.args.audio_channels)),
                ]
                if codec == "ffv1":
                    # Raw PCM, deliberately, beside lossless video.
                    #
                    # flacenc would halve the size and is also lossless, but it
                    # rewrites its stream header at end-of-stream, and a branch
                    # that is detached the moment EOS lands does not give the
                    # muxer time to take the new one: measured, every recording
                    # came out with an undecodable audio track ("invalid sync
                    # code") and a container duration of 2**64-1 nanoseconds.
                    # PCM has nothing to finalise. Against FFV1's ~10 MB/s the
                    # extra 190 KB/s does not register.
                    pass
                else:
                    audio.append(make("opusenc"))
            except BranchError as exc:
                audio, audio_src = [], None
                self.show_toast("recording without sound: %s" % exc)

        chain_into(container, video)
        container.add(mux)
        container.add(sink)
        if not video[-1].link(mux):
            raise BranchError("matroskamux refused the video track")
        if not mux.link(sink):
            raise BranchError("cannot link matroskamux to the file")
        if audio:
            chain_into(container, audio)
            if not audio[-1].link(mux):
                raise BranchError("matroskamux refused the audio track")
        container.add_pad(
            Gst.GhostPad.new("sink", video[0].get_static_pad("sink")))
        return container, sink, audio_src

    def stop_record(self, then=None, reason=""):
        branch = self.record
        if branch is None:
            if then is not None:
                then()
            return
        self.record = None
        if self.rec_timer:
            GLib.source_remove(self.rec_timer)
            self.rec_timer = 0
        self.rec_label.set_text("\u25cf saving...")
        elapsed = time.monotonic() - self.record_start
        path = self.record_path

        def done(finished):
            self.forget_branch(finished)
            self.rec_label.set_visible(False)
            self.show_toast("saved %s  (%s, %s)%s"
                            % (os.path.basename(path), mmss(elapsed),
                               human(file_size(path)), reason))
            if then is not None:
                then()

        branch.stop(done)

    def update_rec(self):
        if self.record is None:
            return GLib.SOURCE_REMOVE
        free = free_bytes(os.path.dirname(self.record_path))
        if 0 <= free < RECORD_STOP_FREE:
            self.stop_record(reason="  -- the disk is nearly full")
            return GLib.SOURCE_REMOVE
        self.rec_label.set_text(
            "\u25cf REC  %s   %s" % (mmss(time.monotonic() - self.record_start),
                                     human(file_size(self.record_path))))
        return GLib.SOURCE_CONTINUE

    # ----------------------------------------------------------- screenshot --
    def take_shot(self):
        if self.tee is None:
            self.show_toast("this pipeline has no tee to grab from")
            return
        try:
            path = self.capture_path(self.args.shot_dir, "png")
            container, sink = self.build_shot_bin(path)
        except (BranchError, OSError) as exc:
            self.show_toast("screenshot failed: %s" % exc)
            return

        def done(finished):
            self.forget_branch(finished)
            if file_size(path) > 0:
                self.show_toast("saved %s" % os.path.basename(path))
            else:
                self.show_toast("screenshot caught no frame")

        branch = Branch(self, "shot", container, sink, done_cb=done,
                        oneshot=True,
                        gate_pad=sink.get_static_pad("sink"))
        try:
            branch.attach()
        except BranchError as exc:
            self.show_toast("screenshot failed: %s" % exc)
            return
        self.branches.append(branch)

    def build_shot_bin(self, path):
        w, h = self.args.shot_size
        elements = [
            make("queue", max_size_buffers=4, leaky=2),
            make("deinterlace", mode=1, method=self.deint_method()),
            make("videoconvert"),
            make("videoscale", method=self.scale_method()),
            # Two corrections in one filter. 720 non-square samples really are
            # 768 square ones on PAL and 640 on NTSC, and correcting here
            # rather than saving 720 wide means the file is right in anything
            # that opens it, with no aspect metadata to be ignored. RGB rather
            # than the RGBA videoconvert would otherwise settle on: the capture
            # has no transparency, and an alpha plane is a third of the file
            # for nothing.
            caps_filter("video/x-raw,format=RGB,width=%d,height=%d,"
                        "pixel-aspect-ratio=(fraction)1/1" % (w, h)),
            # Deliberately NOT snapshot=true -- see Branch.attach for what that
            # does to everything else hanging off the tee.
            make("pngenc", compression_level=9),
            make("filesink",
                 **{"location": path, "sync": False, "async": False}),
        ]
        container = Gst.Bin.new(self.branch_name("shot"))
        chain_into(container, elements)
        container.add_pad(
            Gst.GhostPad.new("sink", elements[0].get_static_pad("sink")))
        return container, elements[-1]

    def toggle_help(self):
        self.help_open = not self.help_open
        if self.help_open:
            self.help.set_text(self.help_text())
        self.help.set_visible(self.help_open)

    def help_text(self):
        rows = "\n".join("  %-5s %s" % (k, d) for k, d in KEYS)
        w, h = LADDER[self.idx]
        di = DEINTERLACERS[self.di_idx][0] if self.deint is not None else "off"
        rec = "%s%s -> %s" % (self.args.record_codec,
                              "" if self.args.audio_node else " (no sound)",
                              self.args.record_dir)
        return ("elgato-viewer\n\n%s\n\n  size    %dx%d\n  deint   %s\n"
                "  record  %s\n  shots   %dx%d -> %s\n"
                "  device  %s\n  input   %s"
                % (rows, w, h, di, rec,
                   self.args.shot_size[0], self.args.shot_size[1],
                   self.args.shot_dir,
                   self.args.device or "?", self.args.input or "?"))

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

        # A failing capture branch -- a full disk, almost always -- must not
        # take the picture down with it. Walk the message's source up to see
        # whether it came from inside one, and if it did, lose only that.
        for branch in list(self.branches):
            if not branch.owns(message.src):
                continue
            if branch is self.record:
                self.stop_record()
                self.show_toast("recording stopped: %s" % err.message)
            else:
                branch.stop()
                self.show_toast("screenshot failed: %s" % err.message)
            print("elgato-player: %s" % err.message, file=sys.stderr)
            return

        # A branch that has already been detached is no longer anybody's
        # child, so nothing above it leads back to the pipeline. Whatever it
        # has to complain about on its way out, it is not the live path and
        # must not end the session.
        if message.src is not None and not in_pipeline(message.src,
                                                       self.pipeline):
            print("elgato-player: %s" % err.message, file=sys.stderr)
            self.show_toast(clean(err.message))
            return

        print("elgato-player: %s" % err.message, file=sys.stderr)
        if debug:
            print(debug, file=sys.stderr)
        self.exit_code = 1
        self.quit()

    def on_eos(self, _bus, message):
        # Only the pipeline's own EOS means the capture went away. A branch
        # being taken out posts its own, and quitting on that would end the
        # session every time a screenshot finished.
        if message.src is not self.pipeline:
            return
        # The capture is live; EOS means the source went away.
        print("elgato-player: stream ended", file=sys.stderr)
        self.exit_code = 1
        self.quit()

    def on_close(self, _window):
        self.quit()
        return True

    def quit(self):
        # An unfinished MKV is a truncated one: matroskamux writes its index and
        # duration when the stream ends, so the file has to be closed properly
        # before the pipeline goes away. Wait for that, then really go.
        if self.quit_pending:
            return
        if self.record is not None:
            self.quit_pending = True
            self.show_toast("finishing the recording...")
            self.stop_record(then=self.really_quit)
            return
        self.really_quit()

    def really_quit(self):
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
    p.add_argument("--record-dir", default="~/elgato")
    p.add_argument("--shot-dir", default="~/elgato")
    p.add_argument("--record-codec", default="ffv1", choices=("ffv1", "h264"))
    # The PipeWire node the capture audio arrives on, resolved by elgato-viewer
    # (which asks elgato-audio, the one file that knows). Empty means record
    # without sound rather than refuse to record.
    p.add_argument("--audio-node", default="")
    # What the node actually is, read from PipeWire by elgato-audio rather than
    # assumed: this card is stereo or mono depending on what is plugged into
    # it.
    p.add_argument("--audio-rate", type=int, default=48000)
    p.add_argument("--audio-channels", type=int, default=2)
    p.add_argument("--shot-size", default="768x576")
    p.add_argument("--field-order", default="top-field-first",
                   choices=("top-field-first", "bottom-field-first"))
    args = p.parse_args()

    m = re.match(r"^(\d+)x(\d+)$", args.shot_size)
    if not m:
        print("elgato-player: --shot-size must look like 768x576",
              file=sys.stderr)
        return 1
    args.shot_size = (int(m.group(1)), int(m.group(2)))

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
