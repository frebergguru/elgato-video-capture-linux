#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Hypnotize
"""What a recording of this card *is*, in one place.

Two programs write these files: the viewer's r key, which hangs a branch off a
tee while a window plays (libexec/elgato-player.py), and elgato-record, which
runs the same chain with nothing attached to a screen
(libexec/elgato-recorder.py). Only the ends differ -- one is fed by a tee and
lives inside a Bin so it can be detached mid-session, the other is fed by
v4l2src and lives as long as the process. Everything between those ends is
here.

It is here rather than written out twice because each decision below is the
answer to a specific measured failure -- the Y42B pin, the I420 pin, PCM rather
than FLAC, offset-to-zero -- and a second copy is a second place for one of
them to quietly stop being true.

Nothing in this module imports GTK, and nothing may: elgato-record has to run
over SSH with no display at all.

Note: gst-python is not required, and this deliberately avoids the constructs
that need it (Gst.Fraction, Gst.ValueArray, multi-argument Bin.add). Caps are
built from strings.
"""

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


# Package hints for the encoders, so a missing plugin says what to install
# rather than "no element avenc_ffv1".
CODEC_PKG = {"ffv1": ("avenc_ffv1", "gst-libav"),
             "h264": ("x264enc", "gst-plugins-ugly")}

CODECS = tuple(CODEC_PKG)

# Measured on 400 frames of real motion from this capture card:
#   yadif    comb 0.31  v-detail 86.0   latency 5.1ms
#   greedyh  comb 0.61  v-detail 86.3   latency 1.7ms
#   linear   comb 1.04  v-detail 73.6   latency 0.3ms
# yadif is best on picture and costs 5ms, so it is the default; the others are
# here for anyone who would rather have the last few milliseconds. The numbers
# are GstDeinterlaceMethods values.
DEINTERLACERS = [("yadif", 10), ("greedyh", 1), ("linear", 4)]

# A branch that errors does not fail alone -- the flow error travels back
# through the tee and v4l2src posts "internal data stream error", which stops
# the picture as well. On a recording the one realistic way that happens is a
# full disk, so it is headed off instead of handled: refuse to start with less
# than the first figure free, and stop cleanly on the way down to the second
# rather than let filesink discover ENOSPC.
RECORD_MIN_FREE = 1 << 30          # 1 GB to start
RECORD_STOP_FREE = 512 << 20       # 512 MB to keep going

# Roughly what a second of recording costs on disk, for warning about a
# capture that will not fit. FFV1 is the figure measured on console output
# (4.5 MB/s); noisy tape compresses worse, which is why the warning is a
# warning. h264 at quantizer 18 is about a twentieth of that.
BYTES_PER_SECOND = {"ffv1": 4.5 * (1 << 20), "h264": 0.25 * (1 << 20)}


class BranchError(Exception):
    """A chain could not be built or attached. Never fatal to the viewer: the
    picture keeps playing and the reason goes on screen."""


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


def missing_encoder(codec):
    """The (element, package) a codec needs, if this machine has not got it."""
    element, package = CODEC_PKG[codec]
    if Gst.ElementFactory.find(element) is None:
        return element, package
    return None


def video_chain(codec, field_order, deint_method=None):
    """Raw capture buffers in, encoded video out, ready for the muxer.

    Deliberately does NOT include a queue at the head: what belongs there
    depends on who is feeding it. The viewer's branch leaks, because a stalled
    disk must never stall the live picture; elgato-record's does not, because a
    leak is a hole in the middle of an archive and there is no picture to
    protect.
    """
    if deint_method is None:
        deint_method = DEINTERLACERS[0][1]

    chain = [
        # The card says "interlaced" but not which field comes first, so
        # ffprobe reports field_order=unknown and a player has to guess --
        # guess wrong and motion steps backwards. Say it here. Top field
        # first is what elgato-obs-setup already defaults to for this
        # hardware; bottom is the escape hatch, same as the OBS tool's
        # --field. The h264 arm below is deinterlaced and so carries none of
        # this into the file, but the deinterlacer reads the same field order
        # to decide which field is which -- so it is set for both.
        make("capssetter", caps=Gst.Caps.from_string(
            "video/x-raw,field-order=(string)%s" % field_order)),
    ]
    if codec == "ffv1":
        chain += [
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
        chain += [
            make("deinterlace", mode=1, method=deint_method),
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
    return chain


def audio_chain(node, rate, channels, codec, av_offset_ms=0):
    """The capture sound, ready for the muxer. Returns (elements, source).

    The source is returned because ending a recording means sending EOS to it
    by hand: it is a live source, and without that the muxer waits for an audio
    track that never ends.
    """
    src = make("pulsesrc", device=node, provide_clock=False)
    chain = [
        src,
        make("queue", max_size_time=2 * Gst.SECOND, leaky=2),
        make("audioconvert"),
        make("audioresample"),
        # Pin the format to what the node really is. Left to negotiate
        # freely against a flexible sink, pulsesrc settles on its own
        # defaults rather than the device's -- measured: 44100 mono off a
        # node that was 48000 stereo, which is a resample and a lost
        # channel, silently. The figures come from PipeWire via
        # elgato-audio, so a mono feed is recorded as mono rather than
        # forced to stereo.
        caps_filter("audio/x-raw,format=S16LE,rate=%d,channels=%d"
                    % (rate, channels)),
    ]
    # Shift the audio track against the picture, if asked. Video is stamped
    # when the USB buffer completes and audio when PipeWire hands it over, and
    # those two paths do not have the same latency -- the residue is a constant
    # offset, which this cancels. It goes on the queue's src pad rather than
    # the source's: a pad offset moves the segment, and moving a live source's
    # segment is what went wrong when the viewer's branch tried to rebase
    # itself to zero. Downstream of the queue there is no such edge to fall
    # off, and a few hundred milliseconds against a running time measured in
    # minutes is nowhere near one.
    if av_offset_ms:
        chain[1].get_static_pad("src").set_offset(av_offset_ms * Gst.MSECOND)
    if codec != "ffv1":
        chain.append(make("opusenc"))
    # Raw PCM, deliberately, beside lossless video.
    #
    # flacenc would halve the size and is also lossless, but it rewrites its
    # stream header at end-of-stream, and a branch that is detached the moment
    # EOS lands does not give the muxer time to take the new one: measured,
    # every recording came out with an undecodable audio track ("invalid sync
    # code") and a container duration of 2**64-1 nanoseconds. PCM has nothing
    # to finalise. Against FFV1's ~10 MB/s the extra 190 KB/s does not
    # register.
    return chain, src


def mux_and_sink(path, writing_app):
    """The Matroska muxer and the file it writes. Returns (mux, sink)."""
    mux = make("matroskamux",
               writing_app=writing_app,
               # FFV1 is all-intra, so every frame is a keyframe and the
               # default index interval of 0 writes a cue entry for each
               # one -- 90,000 of them in an hour. One a second seeks just
               # as well and costs nothing.
               min_index_interval=Gst.SECOND,
               # This is what rebases a file to zero. It matters for the
               # viewer, whose branch is attached to a pipeline that may have
               # been running for an hour and whose buffers therefore carry an
               # hour of running time; for elgato-record, where the pipeline
               # starts with the recording, it has nothing to do and costs
               # nothing. matroskamux rebases every stream by the earliest one,
               # so it does video and audio together and leaves the offset
               # between them untouched. The viewer's Branch.attach
               # deliberately does NOT use pad offsets for this -- see the note
               # there.
               offset_to_zero=True)
    sink = make("filesink",
                **{"location": path, "sync": False, "async": False})
    return mux, sink
