#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
"""Record the Elgato Video Capture with nothing attached to a screen.

The engine behind bin/elgato-record. It is the viewer's r key with the window
taken away: the encoders, the muxer and the audio all come from
libexec/elgato_recording.py, which libexec/elgato-player.py uses too, so the
files the two produce are the same.

What is different is what feeds it and what ends it.

  * Feeding. The viewer taps a tee that is already running, so its branch
    starts with a leaky queue -- a stalled disk must never stall the live
    picture. Here v4l2src is at the top and there is no picture to protect, so
    the queue does NOT leak: back-pressure reaching the source is better than a
    hole in the middle of an archive.

  * Ending. A recording is finished, not stopped. matroskamux writes its index
    and duration when the stream ends, and a file that never got that is a file
    with no duration that seeks badly -- so every way out of here (a signal,
    --duration expiring, the disk filling) goes through the same path: push EOS
    in at the sources, wait for it to come out at the file sink, and only then
    take the pipeline to NULL. Measured at about 100ms for a 15-second FFV1
    file; the timeout below is a backstop, not a budget.

SIGHUP is handled beside SIGINT and SIGTERM on purpose. This is the tool you
run over SSH, and a connection that drops mid-tape must leave a playable file.

No GTK is imported here, and none may be: this has to run with neither DISPLAY
nor WAYLAND_DISPLAY set.

Note: gst-python is not required, and this deliberately avoids the constructs
that need it. Caps are built from strings.
"""

import argparse
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from elgato_recording import (  # noqa: E402
    BranchError, RECORD_STOP_FREE, audio_chain, caps_filter, make,
    missing_encoder, mux_and_sink, video_chain)

# GLib.unix_signal_add moved to GLibUnix and warns on newer PyGObject; keep
# working on both rather than printing a deprecation notice over the output.
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402
    _unix_signal_add = GLibUnix.signal_add
except (ValueError, ImportError):
    _unix_signal_add = GLib.unix_signal_add

# How long to wait for EOS to travel the pipeline before giving up on it and
# taking the file as it stands. See the module docstring: 100ms is typical.
EOS_TIMEOUT_MS = 15000

# Consecutive one-second ticks with the file not growing before saying so. The
# recording is not abandoned -- a source that has gone quiet is not the same as
# a broken one -- but silence about it is the worst answer.
STALL_TICKS = 5

TTY = sys.stderr.isatty()
if TTY:
    C_CYA, C_YEL, C_RED, C_OFF = ("\033[1;36m", "\033[1;33m", "\033[1;31m",
                                  "\033[0m")
else:
    C_CYA = C_YEL = C_RED = C_OFF = ""


class Out:
    """stderr, shared between a self-erasing progress line and real messages.

    The progress line is only ever drawn on a terminal. Anything else -- a log
    file, a systemd journal, a pipe -- gets whole lines at a slow cadence
    instead, because a carriage return in a log is just noise.
    """

    # How often a non-terminal gets a progress line. A terminal is redrawn
    # every second because the line erases itself; a log file, a journal or a
    # pipe would otherwise collect 3,600 of them an hour for a recording nobody
    # is watching.
    LOG_EVERY_S = 60

    def __init__(self, quiet):
        self.quiet = quiet
        self.dirty = False          # a progress line is on screen, unfinished
        self.last_logged = 0.0

    def _clear(self):
        if self.dirty:
            sys.stderr.write("\r\033[K")
            self.dirty = False

    def line(self, marker, colour, text):
        self._clear()
        sys.stderr.write("%s%s%s %s\n" % (colour, marker, C_OFF, text))
        sys.stderr.flush()

    def msg(self, text):
        self.line("::", C_CYA, text)

    def warn(self, text):
        self.line("!!", C_YEL, text)

    def err(self, text):
        self.line("xx", C_RED, text)

    def progress(self, text):
        if self.quiet:
            return
        if TTY:
            sys.stderr.write("\r\033[K%s" % text)
            sys.stderr.flush()
            self.dirty = True
            return
        now = time.monotonic()
        if self.last_logged and now - self.last_logged < self.LOG_EVERY_S:
            return
        self.last_logged = now
        self.msg(text)

    def done(self):
        self._clear()


def mmss(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, (seconds // 60) % 60,
                                 seconds % 60)
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


def free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return -1                  # unknown: do not stand in the way


class Recorder:
    def __init__(self, args):
        self.args = args
        self.out = Out(args.quiet)
        self.exit_code = 0
        self.stopping = False
        self.reason = ""
        self.frames = 0
        self.first_frame = None     # monotonic, when the card's first buffer
        self.last_frame = None      #   arrived, and its most recent
        self.fps = args.fps
        self.started = 0.0
        self.last_size = 0
        self.flat_ticks = 0
        self.stalled = False
        self.timers = []
        self.audio_src = None
        self.loop = GLib.MainLoop()

        Gst.init(None)
        self.pipeline = Gst.Pipeline.new("elgato-record")
        self.build()

    # ------------------------------------------------------------- pipeline --
    def build(self):
        a = self.args
        src = make("v4l2src", device=a.device, io_mode=2)   # 2 = mmap

        # Pin the capture geometry as source caps.
        #
        # This chip implements S_FMT scaling and STAYS wherever it was last
        # asked to be -- a standard change does not reset it. GStreamer
        # negotiates downstream caps upstream, so anything below that fancies a
        # different size would have that written into the device and left
        # there. Nothing here asks for one, but an archive that silently came
        # out 180x144 is the worst version of that bug, so say the size out
        # loud. bin/elgato-record has already pinned the same geometry with
        # v4l2-ctl, which is what repairs a card something else shrank.
        #
        # The frame rate is deliberately NOT pinned: the card decides it from
        # the standard, and asking for the wrong one is a negotiation failure
        # rather than a correction.
        size = caps_filter("video/x-raw,width=%d,height=%d" % (a.width,
                                                               a.height))

        # Four seconds, and NOT leaky -- see the module docstring.
        queue = make("queue", max_size_buffers=0, max_size_bytes=0,
                     max_size_time=4 * Gst.SECOND, leaky=0)

        # The 4:3 correction. 720 samples across is a 4:3 picture made of
        # non-square samples, and this is the tag that says so, so the file
        # displays correctly in anything that opens it. In the viewer this sits
        # ahead of the tee (bin/elgato-viewer), which is why its recordings
        # carry it; here there is no upstream to inherit it from.
        #
        # capssetter rather than a capsfilter because it overrides the tag
        # without asking the source to renegotiate: v4l2src reports whatever
        # the driver says about pixel aspect, and demanding a different value
        # would fail rather than relabel.
        par = make("capssetter", caps=Gst.Caps.from_string(
            "video/x-raw,pixel-aspect-ratio=(fraction)%s" % a.par))

        video = [src, size, queue, par] + video_chain(a.codec, a.field_order)
        mux, sink = mux_and_sink(a.output, "elgato-record")

        audio = []
        if a.audio_node:
            try:
                audio, self.audio_src = audio_chain(
                    a.audio_node, a.audio_rate, a.audio_channels, a.codec,
                    a.av_offset)
            except BranchError as exc:
                audio, self.audio_src = [], None
                self.out.warn("recording without sound: %s" % exc)

        # chain_into wants a bin; the pipeline is one.
        for el in video + audio + [mux, sink]:
            self.pipeline.add(el)
        for a_el, b_el in zip(video, video[1:]):
            if not a_el.link(b_el):
                raise BranchError("cannot link %s to %s"
                                  % (a_el.get_factory().get_name(),
                                     b_el.get_factory().get_name()))
        for a_el, b_el in zip(audio, audio[1:]):
            if not a_el.link(b_el):
                raise BranchError("cannot link %s to %s"
                                  % (a_el.get_factory().get_name(),
                                     b_el.get_factory().get_name()))
        if not video[-1].link(mux):
            raise BranchError("matroskamux refused the video track")
        if audio and not audio[-1].link(mux):
            raise BranchError("matroskamux refused the audio track")
        if not mux.link(sink):
            raise BranchError("cannot link matroskamux to the file")

        # Count frames where they arrive from the card, not where they are
        # written: nothing downstream of the queue may drop one (it does not
        # leak, and encoders do not discard), so a shortfall here is the driver
        # losing frames and nothing else. That is the number worth reporting --
        # this card delivers corrupt frames at exactly the right rate, so a
        # frame count is a claim about capture, not about picture quality.
        queue.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                               self._count)
        self.sink = sink

    def _count(self, pad, _info):
        now = time.monotonic()
        if self.first_frame is None:
            self.first_frame = now
            # The negotiated rate beats the one we were told: the card decides
            # it from the standard it is actually locked to.
            caps = pad.get_current_caps()
            if caps is not None and caps.get_size():
                ok, num, den = caps.get_structure(0).get_fraction("framerate")
                if ok and den:
                    self.fps = float(num) / den
        self.last_frame = now
        self.frames += 1
        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------ run --
    def run(self):
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_error)
        bus.connect("message::warning", self.on_warning)
        bus.connect("message::eos", self.on_eos)

        for sig, why in ((signal.SIGINT, "interrupted"),
                         (signal.SIGTERM, "terminated"),
                         # A dropped SSH connection. Finish the file rather
                         # than leave one with no index behind.
                         (signal.SIGHUP, "connection closed")):
            _unix_signal_add(GLib.PRIORITY_HIGH, sig, self.on_signal, why)

        if self.pipeline.set_state(Gst.State.PLAYING) == \
                Gst.StateChangeReturn.FAILURE:
            self.out.err("the pipeline would not start")
            return 1
        # Live sources go async; give the state change a moment to fail loudly
        # here rather than leaving a "recording" message over a dead pipeline.
        change, _state, _pending = self.pipeline.get_state(5 * Gst.SECOND)
        if change == Gst.StateChangeReturn.FAILURE:
            self.finish_state()
            return 1

        self.started = time.monotonic()
        self.out.msg("Recording to %s" % self.args.output)
        if self.args.duration:
            self.out.msg("Will stop after %s (or on Ctrl-C, or "
                         "'elgato-record stop')" % mmss(self.args.duration))
            self.timers.append(GLib.timeout_add_seconds(
                self.args.duration, self.on_duration))
        else:
            self.out.msg("Stop it with Ctrl-C, or 'elgato-record stop'")
        self.timers.append(GLib.timeout_add_seconds(1, self.tick))

        self.loop.run()
        self.finish_state()
        self.report()
        return self.exit_code

    def finish_state(self):
        self.pipeline.set_state(Gst.State.NULL)

    # ----------------------------------------------------------------- tick --
    def tick(self):
        if self.stopping:
            return GLib.SOURCE_REMOVE
        size = file_size(self.args.output)
        elapsed = time.monotonic() - self.started

        free = free_bytes(os.path.dirname(self.args.output) or ".")
        if 0 <= free < RECORD_STOP_FREE:
            self.stop("the disk is nearly full (%s left)" % human(free))
            return GLib.SOURCE_REMOVE

        # A file that has stopped growing means the card stopped delivering.
        # Say so once: the recording is still running, and a source that was
        # switched off for a minute is not a reason to end a tape.
        if size == self.last_size:
            self.flat_ticks += 1
            if self.flat_ticks == STALL_TICKS and not self.stalled:
                self.stalled = True
                self.out.warn("no data written for %ds -- is the source still "
                              "playing? (still recording)" % STALL_TICKS)
        else:
            if self.stalled:
                self.out.msg("data is flowing again")
                self.stalled = False
            self.flat_ticks = 0
        self.last_size = size

        rate = size / elapsed / (1 << 20) if elapsed > 0 else 0
        self.out.progress(
            "● REC %s  %s  %.1f MB/s  %d frames  %s free"
            % (mmss(elapsed), human(size), rate, self.frames,
               human(free) if free >= 0 else "?"))
        return GLib.SOURCE_CONTINUE

    def on_duration(self):
        self.stop("the %s asked for is up" % mmss(self.args.duration))
        return GLib.SOURCE_REMOVE

    def on_signal(self, why):
        self.stop(why)
        return GLib.SOURCE_CONTINUE      # a second Ctrl-C must still be heard

    # ----------------------------------------------------------------- stop --
    def stop(self, reason):
        if self.stopping:
            # Somebody is impatient. Say what is happening rather than appear
            # to ignore them -- the wait is matroskamux writing the index, and
            # it is the difference between a file with a duration and one
            # without.
            self.out.warn("already finishing -- writing the index, a moment")
            return
        self.stopping = True
        self.reason = reason
        for t in self.timers:
            GLib.source_remove(t)
        self.timers = []
        self.out.done()
        self.out.msg("Finishing: %s" % reason)

        # EOS at the sources, which is what makes the muxer write its index.
        # Sent to the pipeline, which hands it to every source it contains --
        # including pulsesrc, whose track the muxer would otherwise sit waiting
        # for.
        self.pipeline.send_event(Gst.Event.new_eos())
        self.timers.append(GLib.timeout_add(EOS_TIMEOUT_MS, self.on_eos_late))

    def on_eos(self, _bus, _message):
        self.quit()

    def on_eos_late(self):
        self.out.warn("the muxer did not finish within %ds -- the file may be "
                      "missing its index" % (EOS_TIMEOUT_MS // 1000))
        self.exit_code = 1
        self.quit()
        return GLib.SOURCE_REMOVE

    def quit(self):
        for t in self.timers:
            GLib.source_remove(t)
        self.timers = []
        if self.loop.is_running():
            self.loop.quit()

    # ------------------------------------------------------------------ bus --
    def on_error(self, _bus, message):
        err, debug = message.parse_error()
        self.out.err(err.message)
        if debug and self.args.debug:
            self.out.err(debug)
        self.exit_code = 1
        if self.stopping:
            self.quit()
            return
        # Try to close the file properly even now: an error at the source (the
        # box being unplugged, say) leaves everything already written perfectly
        # good, and it is only worth having if the index gets written.
        self.stop("the pipeline failed -- saving what was captured")

    def on_warning(self, _bus, message):
        err, _debug = message.parse_warning()
        self.out.warn(err.message)

    # --------------------------------------------------------------- report --
    def report(self):
        self.out.done()
        size = file_size(self.args.output)
        if self.frames == 0:
            self.out.err("no frames were captured -- %s is empty"
                         % self.args.output)
            self.exit_code = self.exit_code or 1
            return

        # Measure the span the card actually covered, first buffer to last,
        # rather than the process's lifetime: the startup and the EOS handshake
        # are real seconds but no frames were expected in them, and counting
        # them would report losses that did not happen.
        span = (self.last_frame - self.first_frame) if self.frames > 1 else 0.0
        expected = int(round(span * self.fps)) + 1
        lost = expected - self.frames
        rate = (self.frames - 1) / span if span > 0 else 0.0

        self.out.msg("Saved %s  (%s, %s)"
                     % (self.args.output, mmss(span), human(size)))
        # Two or three frames is the rounding on a rate that is 30000/1001 as
        # often as it is 25, not a capture fault.
        if lost > 3:
            self.out.warn("%d frames in %.2fs -- %.3f fps against %.3f "
                          "expected, so about %d frames were lost"
                          % (self.frames, span, rate, self.fps, lost))
            self.out.warn("that is the driver not delivering, not the encoder:"
                          " check  journalctl -k -b | grep cx231xx")
            self.exit_code = self.exit_code or 2
        else:
            self.out.msg("%d frames in %.2fs -- %.3f fps, none lost"
                         % (self.frames, span, rate))


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--device", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--codec", default="ffv1", choices=("ffv1", "h264"))
    p.add_argument("--duration", type=int, default=0)
    p.add_argument("--width", type=int, default=720)
    p.add_argument("--height", type=int, default=576)
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--par", default="16/15")
    p.add_argument("--field-order", default="top-field-first",
                   choices=("top-field-first", "bottom-field-first"))
    # The PipeWire node the capture audio arrives on, resolved by
    # bin/elgato-record. Empty means record without sound rather than refuse to
    # record.
    p.add_argument("--audio-node", default="")
    p.add_argument("--audio-rate", type=int, default=48000)
    p.add_argument("--audio-channels", type=int, default=2)
    p.add_argument("--av-offset", type=int, default=0)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    # Before any factory lookup: Gst.ElementFactory.find() answers from the
    # plugin registry, and an uninitialised GStreamer has no registry to answer
    # from -- it reports every element missing, including the ones that are
    # installed. Gst.init is idempotent, so Recorder calling it again is free.
    Gst.init(None)

    missing = missing_encoder(args.codec)
    if missing is not None:
        print("elgato-recorder: %s recording needs %s (%s)"
              % ((args.codec,) + missing), file=sys.stderr)
        return 1

    try:
        recorder = Recorder(args)
    except (BranchError, GLib.Error) as e:
        print("elgato-recorder: %s" % e, file=sys.stderr)
        return 1
    return recorder.run()


if __name__ == "__main__":
    sys.exit(main())
