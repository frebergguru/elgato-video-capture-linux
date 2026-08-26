# Elgato Video Capture V2 on Linux

Watch anything on composite or S-Video through an Elgato Video Capture V2
(USB `0fd9:0037`, Conexant cx231xx) — a games console, a VCR, a DVD player, a
camcorder — with low enough latency to play games on, and without the frame
tearing the stock driver produces.

```
./install.sh             # build + install the driver, rules and launchers
elgato-viewer            # play
elgato-viewer --verify   # measure frame integrity (read its caveats below)
elgato-record -t 2h      # capture a tape, no display needed
```

`./install.sh --no-driver` skips building the kernel module (rules and
launchers only), `--no-share` skips v4l2loopback, which only `--share` needs
(`ELGATO_SKIP_SHARE=1` does the same), and `-y` answers every prompt, for an
unattended run.

> **This builds and loads an out-of-tree kernel module.** It taints the kernel,
> and under Secure Boot it has to be signed to load at all — `install.sh` signs
> it with an enrolled Machine Owner Key when the machine has one, and prints the
> recipe to enrol your own when it does not. The module writes decoder registers
> on one specific USB device; a bad setting can leave the capture chip emitting
> nothing until it is power-cycled (`elgato-viewer --reset`). There is no
> warranty — see LICENSE.

## The problem this fixes

Out of the box roughly **60% of frames are corrupt** — sheared diagonally with
rainbow striping. The cause is not USB, not bandwidth, and not the player:

> The stock cx231xx driver never programs the cx25840 decoder's **horizontal
> time-lock loop**. SAV-to-SAV line lengths hunt instead of holding constant,
> so every line is sampled at a different horizontal phase.

The patched driver programs `HTL_CTRL` and `PLL_CTRL`, behind `elgato_htl=2`:

| `elgato_htl` | frames corrupt |
| ------------ | -------------- |
| `0` (stock behaviour) | ~60% |
| **`2`** | **0.0%** |

Measured over 750 frames across six consecutive runs, and confirmed by eye.
`install.sh` sets it permanently in `/etc/modprobe.d/cx231xx.conf`.

To see this for yourself rather than take the table on trust, run
`tools/htl-ab` from a terminal. It power-cycles the card before each setting,
shows the decoder registers each one actually produced, and opens the live
picture so you can look at it. The power cycle is the part that matters:
writing `elgato_htl=0` back does **not** un-program `HTL_CTRL`, it only stops
the driver writing it, so a hand-rolled A/B without one shows every setting as
perfect once any of them has locked the decoder. The tool runs `elgato_htl=0`
first as a positive control — if the picture does not tear there, your source
does not provoke the fault and no comparison after it means anything.

`elgato_htl=1` and `elgato_htl=2` are the same thing, incidentally: the
lock-acquire routine `2` was meant to add cannot run and could not contribute
if it did. See `cx231xx_elgato_v2_acquire_lock()` in
`driver/cx231xx/cx231xx-avcore.c`.

**The fix lives in the kernel module.** `elgato-viewer` cannot compensate for
it — corrupt frames arrive at exactly 25fps with exactly the right byte count,
carrying data spliced from several moments. If you install with
`--no-driver`, expect the tearing.

## What is here

```
bin/elgato-viewer   the player
bin/elgato-record   record without a display -- over SSH, unattended
bin/elgato-audio    audio loopback (pw-loopback); elgato-viewer starts it
bin/elgato-doctor   end-to-end diagnostics
bin/elgato-reset    power-cycle a wedged capture chip
bin/elgato-obs-setup  write an OBS profile and scene collection for the card
bin/elgato-driver   load the locally built module for this boot only
libexec/elgato-player.py  the viewer's window and keys, incl. r and s
libexec/elgato-recorder.py  the headless recording engine, no GTK
libexec/elgato_recording.py  what a recording IS -- shared by both of those
lib/elgato-common.sh  shared helpers for the elgato-* helper scripts
lib/elgato-distro.sh  package names, root, module paths, per distribution
tools/htl-ab        A/B the elgato_htl knob honestly -- not installed, needs root
driver/cx231xx/     patched cx231xx source
driver/patches/     every deviation from the stock kernel tree, with rationale
etc/                udev, modprobe, modules-load and WirePlumber configuration
install.sh          build and install all of the above; uninstall.sh undoes it
LICENSE             GPL-2.0-or-later, full text
OBS.md              using OBS with this card, and alongside elgato-viewer
```

## Playing

```
elgato-viewer                      # composite, 960x720, sound on
elgato-viewer -f --sharp           # fullscreen, nearest-neighbour pixels
elgato-viewer --deinterlace linear # lowest latency, at the cost of combing
elgato-viewer --ntsc               # or --pal
elgato-viewer --input svideo
elgato-viewer --reset              # power-cycle first
elgato-viewer --share              # also publish to OBS (see below)
elgato-viewer --record-codec h264  # r then records small files, not lossless
```

### Keys

With the video window focused:

| key | |
| --- | --- |
| `f` | fullscreen on/off |
| `z` / `x` | smaller / larger window |
| `c` | cycle the deinterlacer — yadif / greedyh / linear |
| `m` | mute/unmute the capture audio — other apps keep playing |
| `r` | start/stop recording — see [Recording](#recording) |
| `s` | save a screenshot — a PNG in `~/elgato`, deinterlaced and 768x576 |
| `h` | show the keys on screen |
| `q` | quit |
| `Esc` | close the help overlay, or quit |

### Options

`elgato-viewer --help` prints this same list.

| option | |
| ------ | --- |
| `-i`, `--input WHICH` | `composite` (default) \| `svideo` \| `0` \| `1` |
| `--pal` / `--ntsc` | force the TV standard (default: leave the card on whatever it is) |
| `-f`, `--fullscreen` | start full screen |
| `-s`, `--size WxH` | window size (default 960x720, 4:3) |
| `--sharp` | nearest-neighbour scaling; crisp pixels, no blur |
| `--deinterlace M` | `yadif` (default) \| `greedyh` \| `linear` \| `weave` \| `none` — see [Deinterlacing](#deinterlacing) |
| `--no-audio` | do not start the audio loopback |
| `--latency MS` | audio loopback latency (default 15) |
| `--share [DEV]` | also publish on a v4l2loopback device (default `/dev/elgato-share`) so OBS can read it too |
| `--record FILE` | also write the raw captured frames to `FILE`, about 21 MB/s — for analysis; for a file you can play, press `r` |
| `--record-dir DIR` | where `r` writes recordings (default `~/elgato`, made on first use) |
| `--shot-dir DIR` | where `s` writes screenshots (default `~/elgato`) |
| `--record-codec C` | `ffv1` (default) \| `h264` — see [Recording](#recording) |
| `--no-record-audio` | leave the capture audio out of recordings |
| `--record-av-offset MS` | move the recorded audio against the picture, positive is later (default 0) — see [Recording](#recording) |
| `--field-order F` | `top` (default) \| `bottom` — which field of a recorded frame comes first |
| `--reset` | power-cycle the capture box first (runs `elgato-reset`) |
| `--check` | run the pre-flight checks and exit — add `--share` to check that path too |
| `--verify [SECS]` | measure frame integrity and exit (default 6s) |
| `--quality [SECS]` | measure picture quality — crosstalk, sharpness, detail, noise (default 8s) |
| `--tune` | sweep the driver's `elgato_*` knobs and score each. Needs root |
| `-d`, `--device PATH` | V4L2 node (default: autodetect, preferring `/dev/elgato`) |
| `-h`, `--help` | this |

`--verify` and `--quality` answer different questions: integrity is whether the
driver assembled the frame correctly, quality is what the picture looks like
once it did.

### Recording

`r` starts and stops a recording; `s` writes a single frame. Both land in
`~/elgato`, named for the time they were taken, and both can be pointed
elsewhere with `--record-dir` / `--shot-dir`. The window shows `● REC 0:23`
with the file size while a recording runs.

Both tap the picture **ahead of the deinterlacer**, off the same tee `--share`
uses, so what is written is what the card delivered — not what the window
happens to be showing. Resizing the window, or cycling the deinterlacer with
`c`, changes neither.

A recording is a Matroska file, and by default it holds the frames exactly as
they arrived:

| | |
| --- | --- |
| video | FFV1, lossless, 720x576 still interlaced, 4:2:2 — bit-for-bit the capture |
| audio | uncompressed PCM at the rate and channel count the card is actually sending |
| size | 4.5 MB/s measured on console output — about 16 GB an hour. Noisy tape compresses worse; budget up to twice that |

That is the format to archive a tape in: nothing is thrown away, and the
deinterlacing can be decided later, once, with as much time as it takes. It
costs about 0.8 of one core to encode — measured on 720x576 of pure noise,
which is the worst thing this codec can be handed, with the cost of generating
those frames subtracted. It threads across slices, so on a modern machine that
is roughly eight times faster than real time and the picture never waits.

Measured on a real capture: 48 seconds of NES output came to 1200 frames in
47.99s — 25.005 fps, not a frame dropped — and 215 MB, with no USB errors
logged while it ran.

`--record-codec h264` is the other trade: the picture is deinterlaced to 50fps
and encoded at quantizer 18, which is hard to fault by eye and about a
twentieth of the size. Use it when the recording is something to watch rather
than something to keep.

MKV rather than MPG because `.mpg` means an MPEG-2 program stream — lossy,
4:2:0, and unable to carry FFV1 at all.

Screenshots are PNG: lossless, and written at 768x576 (640x480 on NTSC), which
is the capture with its non-square samples corrected rather than the window
scaled up. They are deinterlaced with whatever method `c` last selected, so a
still matches what you were looking at.

#### Without a display

`r` is a feature of a window: it needs GTK4, `gtk4paintablesink` and a
compositor before it can write a byte. Archiving a tape is the job you would
least like to need a desktop session for, so `elgato-record` is that same
recording with the window taken out.

```
elgato-record                  # until stopped
elgato-record -t 2h            # a whole tape, then finish cleanly
elgato-record --check          # device, picture, sound, disk -- then exit
elgato-record status           # from any other shell
elgato-record stop             # finishes the file properly
```

It writes the same file `r` does, because it is the same code: the encoders,
the muxer and the audio all come from `libexec/elgato_recording.py`, which both
the viewer and this share. Everything in [Recording](#recording) above — FFV1
by default, `--codec h264` for something to watch, the field order, the
lip-sync correction — applies unchanged.

| option | |
| ------ | --- |
| `-o`, `--output FILE` | where to write (default `~/elgato/elgato-<date>-<time>.mkv`) |
| `--record-dir DIR` | where that default name lands |
| `-t`, `--duration T` | record for `90`, `90s`, `45m`, `2h` or `1:30:00`, then finish |
| `--codec C` | `ffv1` (default) \| `h264` — same trade as `--record-codec` |
| `--no-audio` | leave the capture sound out of the file |
| `--av-offset MS` | bake in a lip-sync correction, as `--record-av-offset` does |
| `--field-order F` | `top` (default) \| `bottom` |
| `-i`, `--input WHICH` | `composite` (default) \| `svideo` \| `0` \| `1` |
| `--pal` / `--ntsc` | force the TV standard |
| `-d`, `--device PATH` | V4L2 node (default: autodetect, preferring `/dev/elgato`) |
| `--reset` | power-cycle the capture box first |
| `--check` | run the preflight and exit without recording |
| `-q`, `--quiet` | no progress line; messages and the final report only |
| `--debug` | print GStreamer's debug detail on an error |

**Stopping it is the part that matters.** matroskamux writes its index and
duration when the stream *ends*, so a recording has to be finished rather than
killed — an MKV that never got that has no duration and seeks badly. Ctrl-C,
`elgato-record stop`, SIGTERM and SIGHUP all finish it properly, which means a
dropped SSH connection costs nothing. Measured: about half a second from the
signal to a closed file. `kill -9` is the one way to lose the index.

`stop` sends SIGTERM rather than SIGINT deliberately. A shell without job
control — a script, `nohup`, `systemd-run` — sets SIGINT to `SIG_IGN` in its
background children, and a signal that was ignored on entry cannot be trapped
afterwards. Measured: `SigIgn: 0000000000000007` in `/proc/PID/status`, the
trap silently not installed, and `stop` doing nothing at all.

Because nobody is watching the picture, it says what it found before it starts
and accounts for every frame when it finishes:

```
$ elgato-record -t 20
:: Device: /dev/elgato
:: Input: Composite   Standard: PAL   720x576 @ 25fps
:: Picture: present
:: Audio: alsa_input.usb-Elgato_Video_Capture_…stereo-fallback (48000Hz, 2ch)
:: Disk: 775.3 GB free, about 90 MB needed for 0:20
:: Recording to /home/you/elgato/elgato-20260826-205135.mkv
● REC 0:19  42 MB  2.2 MB/s  481 frames  775.2 GB free
:: Saved /home/you/elgato/elgato-20260826-205135.mkv  (0:20, 45 MB)
:: 506 frames in 20.20s -- 25.006 fps, none lost
```

That last line is a claim about *capture*, not about picture: this card
delivers corrupt frames at exactly the right rate, so a full frame count proves
the driver kept up and nothing more. `elgato-viewer --verify` measures the
other half and needs no display either — worth running before committing to a
two-hour capture, with the caveats in [Measuring](#measuring).

The frame count is counted where the frames arrive from the card, and the queue
below it does not leak. That is the one deliberate difference from the viewer's
recording: `r` leaks, because a stalled disk must never stall the live picture,
but here there is no picture to protect and a leak would be a hole in the
middle of an archive.

Two things go wrong over SSH and nowhere else, and both are checked before it
starts:

- **The device is there but unreadable.** The udev rule hands it over with
  `TAG+="uaccess"`, which is an ACL for whoever is logged in *at the screen*. An
  SSH session is not, so on a machine with nobody at the keyboard the node
  exists and cannot be opened. `sudo usermod -aG video $USER` is the fix.
- **There is no sound.** PipeWire is a user service; with no session running
  there is nothing to read the audio from. The recording goes ahead silently
  and says so, and `sudo loginctl enable-linger $USER` is what starts those
  services without a login.

Only one program can open the card, so a running viewer blocks a recording and
the other way round. If you want both, the viewer will lend you the picture:

```
elgato-viewer --share                    # captures, republishes on /dev/elgato-share
elgato-record -d /dev/elgato-share       # record what it is showing
```

For something long, run it under `tmux` or as a transient unit, and come back
to it later:

```
systemd-run --user --unit=tape elgato-record -t 3h
elgato-record status
elgato-record stop
```

#### If the sound sits wrong

The picture and the sound reach this machine by different routes — one through
the USB video path, one through ALSA and PipeWire — and each is timestamped
when it arrives rather than when it happened. What survives that is a constant
offset, the same in every recording, and `--record-av-offset` cancels it.

Finding your number takes one recording of something with a sharp sound you can
see happen:

```
mpv ~/elgato/elgato-….mkv     # then Ctrl-+ / Ctrl-- until it looks right
```

mpv prints `Audio delay: +0.040` on screen as you adjust. Multiply by 1000 and
pass it in — `--record-av-offset 40` — and every recording after that is
corrected as it is written. Measured: the correction lands within a
millisecond of what you ask for.

It is worth saying what this is *not* for. The two streams do not drift apart:
each is timestamped on the same pipeline clock, so the relationship between
sample and frame holds for the length of the recording. Only the fixed head
start is in question, and only your ears can settle it. Leave it at 0 until
something looks wrong.

The first recording of a session may begin with a few tens of milliseconds of
silence, because PipeWire has to wake the capture source. That is a late start,
not an offset, and it corrects itself on the next one.

Recordings are finished properly when you quit — the window waits for the
muxer to write its index, which is what the difference between a file with a
duration and a truncated one comes down to. Measured at about 100ms.

### With OBS

`elgato-obs-setup` writes an OBS profile and scene collection with the settings
this card needs — the TV standard, the anamorphic 4:3 correction, 2x
deinterlacing at 50fps, and audio taken from PipeWire rather than the raw ALSA
device.

Only one program can own the capture node at a time — `cx231xx` has a single
buffer queue — but that does not have to mean choosing between OBS and the
viewer:

```
elgato-viewer --share        # captures, and republishes on /dev/elgato-share
elgato-obs-setup --share     # once, with OBS closed
```

`--share` tees the pipeline into a v4l2loopback device, so OBS reads the frames
as an ordinary camera while the viewer keeps the card. They go out exactly as
captured — still interlaced, still 720x576 — so each side deinterlaces and
scales its own way, and nothing about the OBS settings changes. Start the viewer
first: with nothing writing to it the node is not yet a camera.

Sound needed none of this. PipeWire already hands the capture audio to every
reader that asks, which is why `elgato-audio` uses `pw-loopback`.

[OBS.md](OBS.md) explains all four ways to work and why each setting is what it
is.

Keys need `python-gobject`, `gtk4` and `gst-plugin-gtk4` (Arch names — see
[Requirements](#requirements)). Without them the viewer still plays, but there
is nothing to press: `gst-launch-1.0` has no keyboard plumbing, and
`waylandsink` does not implement `GstNavigation`, so a key press never reaches
it. Close the window or press Ctrl-C instead.

### How it stays stable

Three choices do the work, and they are worth keeping if you modify it:

- **Audio and video travel separately.** Video goes V4L2 → GStreamer → screen;
  audio goes ALSA → PipeWire → speakers via `elgato-audio`. Putting audio in the
  GStreamer pipeline would make video wait on a clock derived from a different
  crystal than the USB capture clock. That drift shows up as periodic stalls
  and creeping lip-sync over a long session.
- **The sink runs `sync=false`.** Frames are drawn on arrival rather than held
  until their timestamp is due. The card is already the clock, so there is no
  second timeline to sync to — the delay would buy nothing.
- **The queue is short and `leaky=downstream`.** If the compositor stalls, old
  frames are dropped instead of accumulating, so latency stays flat.

### Deinterlacing

Defaults to **yadif**, measured over 400 frames of real motion from this card:

| method | combing | vertical detail | latency |
| ------ | ------- | --------------- | ------- |
| raw (none) | 2.46 | 90.7 | — |
| **yadif** | **0.31** | **86.0** | 5.1 ms |
| greedyh | 0.61 | 86.3 | 1.7 ms |
| linear | 1.04 | 73.6 | 0.3 ms |

The console emits 50 progressive fields per second, so a woven 576-line frame
holds two moments 20 ms apart. That combs only when things move, which is why it
is easy to miss on a paused screen. `linear` leaves three times more of it than
yadif *and* discards about 17% of the vertical detail.

The latency worry that once justified `linear` did not survive measurement:
yadif costs 5 ms, not the frame it is often assumed to. Press `c` while playing
to cycle the three and judge for yourself — the difference is a motion artefact
and barely visible in a screenshot.

## The other tools

`elgato-viewer` and `elgato-obs-setup` take options; the rest take verbs.
`elgato-record` takes both — options to start a recording, verbs to act on one
that is already running. `elgato-doctor` and `elgato-reset` take nothing at all.

```
elgato-record [options]                     # record; see Recording headless
elgato-record stop                          # finish the running recording properly
elgato-record status                        # what is recording, how long, how big

elgato-audio start|stop|restart|status      # the pw-loopback that carries the sound
elgato-audio mute|unmute|mute-toggle        # what the viewer's m key calls
elgato-audio record-node                    # the capture source's PipeWire node,
                                            #   as "NAME RATE CHANNELS"

elgato-driver local [param=value ...]       # insmod the locally built module, this boot only
elgato-driver stock                         # back to the distro module, immediately
elgato-driver status                        # which one is loaded, by srcversion

elgato-doctor                               # end-to-end diagnostics
elgato-reset                                # power-cycle a wedged capture chip
```

`elgato-viewer` starts and stops the audio loopback for you; you only need
`elgato-audio` directly to clear a stray one, or to check what is running.
`record-node` is the one verb that is not about the loopback: it reports the
capture source itself, which is what the viewer's `r` key records from and
what anything else reading the audio directly would want. The rate and channel
count come with it because this card is stereo or mono depending on what is fed
into it, and a reader that guesses gets it wrong.

Four environment variables override defaults:

| variable | |
| -------- | --- |
| `ELGATO_VIEWER_SINK` | force a GStreamer video sink instead of autodetecting one |
| `ELGATO_AUDIO_LATENCY_MS` | audio loopback latency, same as `--latency` (default 15) |
| `ELGATO_SETTLE` | seconds to wait after touching the hardware (default 0.8) |
| `ELGATO_SKIP_SHARE` | set to 1 to make `install.sh` skip v4l2loopback, same as `--no-share` |

## Measuring

**Do not judge this device by frame rate or dropped-frame counts.** Corrupt
frames arrive at exactly 25fps with exactly the right byte count; every
delivery metric reports a perfect stream while the picture is unwatchable.

```
elgato-viewer --verify      # % of frames corrupt
                            #   exit 0 clean, 2 corrupt, 3 no picture at all
sudo elgato-viewer --tune   # sweep the driver's elgato_* knobs
```

`--verify` measures chroma coherence between lines: corrupt frames stripe in
colour, a clean picture has flat colour fields. It was validated against frames
inspected by eye — clean scored 2.6, corrupt 13.5 — on flat-colour graphics
from a games console, after three earlier metrics gave false passes. Validate
any replacement the same way.

**A pass is not proof.** The figure is a frame *average* tested against an
absolute threshold, and the scale of the statistic depends on the content while
the threshold does not, so damage confined to part of a dark picture is diluted
away. Measured, on a console title screen: `elgato_htl=0` scored **0.0%
corrupt** while PNG stills of that very capture were sheared diagonally with
the text duplicated at a horizontal offset — 20 frames out of 20, across two
independent power cycles. `--verify` says this next to every verdict it passes.
Look at the picture, or save a still with `s`, before believing a clean score;
`tools/htl-ab` exists because of exactly this.

It reads high in the other direction too. The threshold was calibrated on
flat-colour graphics, and film and tape content — VHS, DVD, camcorder footage —
varies more from line to line by nature, so the percentage may look bad on a
healthy capture. Judge by whether the reported chroma spread is *bimodal*
(clean frames clustering low, corrupt ones clustering far above) rather than by
the percentage alone, and raise `THRESH` in the script if your material needs
it.

Beside the mean it reports the worst 5% of samples, which is where the damage
actually shows. That number is **uncalibrated on purpose** and no verdict rests
on it: a known-good capture measures about 14 on it, read from raw YUYV, so the
mean's threshold of 9 emphatically does not transfer. Use it to compare two
runs of your own, nothing more. (Computing it from PNG stills instead makes it
look like a clean separator — 2.5 good against 20.9 torn — but that is
GStreamer's YUYV-to-RGB conversion interpolating chroma and smoothing exactly
the differences being measured.)

The threshold and the percentage are deliberately left where they are: every
measurement in this repository was taken with them, and redefining them
silently would invalidate the lot.

It refuses to pass judgement when the input is black: a disconnected or
powered-off source has perfectly coherent chroma and would otherwise score a
clean 0%.

To try a driver change without installing it, `elgato-driver local` inserts the
freshly built `driver/cx231xx/*.ko` with `insmod` for this boot only — nothing
goes into `/lib/modules`, so a reboot (or `elgato-driver stock`) puts the distro
module back. Module parameters are passed straight through, which is what makes
an edit-build-measure loop quick:

```
make -C /lib/modules/$(uname -r)/build M=$PWD/driver/cx231xx modules
elgato-driver local elgato_htl=2   # load it, this boot only
elgato-viewer --verify             # score it
elgato-driver status               # local build or distro? compares srcversion
elgato-driver stock                # back to the distro module
```

The register constants in `driver/` are specific to this board and are not the
chip's power-on defaults or documented in the cx25840 datasheet. Do not take
them on trust — `--tune` applies each one and scores it, so every claim in this
repository can be checked against your own hardware.

`--tune` runs two interleaved passes. Compare each configuration against its
own figure in the other pass — a difference that does not survive both passes
is drift in the source material, not an effect of the setting. A single
pass is not evidence: the winning configuration looked broken in pass 1.

**The sweep still measures the wrong thing, and the passes do not fix it.** It
applies a configuration by writing the module parameter and re-setting the
standard, which cannot undo what an earlier configuration already wrote to the
decoder — the same persistence `elgato-reset` exists for. So once any
configuration has locked the decoder, every later one inherits the lock and
scores as though it had earned it: measured, with `elgato_htl=2` applied first,
going back to `elgato_htl=0` still reported 0.0% corrupt, twice. Interleaving
guards against drift; this is one-way contamination, which it does not catch.
To rank configurations honestly, power-cycle between each — `elgato-reset` then
`elgato-viewer --verify`, once per configuration — which is what `tools/htl-ab`
does for `elgato_htl`. And read the blind spot above before reading any row:
a low figure is not evidence that a configuration is good.

## If something is wrong

Run `elgato-doctor` first; it checks the whole chain and says which link failed.

| symptom | cause |
| ------- | ----- |
| picture tears, ~60% of frames | `elgato_htl` is not 2 — `elgato-viewer` warns about this |
| the picture is visibly torn but `--verify` says 0.0% | that is its known blind spot, not a healthy capture — see [Measuring](#measuring) |
| no frames, `cannot change alt number` | USB link wedged — `elgato-viewer --reset` |
| frames but no picture | check the yellow RCA at both ends, and `--ntsc`/`--pal` |
| captured audio in voice chat | WirePlumber rule missing — re-run `install.sh` |
| `elgato-record` cannot open the device over SSH | `uaccess` grants an ACL to the seat session, not to you — `sudo usermod -aG video $USER` |
| a recording came out silent | no PipeWire in that session — `sudo loginctl enable-linger $USER` |
| a recording has no duration and seeks badly | it was killed rather than stopped — use `elgato-record stop` |
| garbled or doubled audio | stray loopbacks — `elgato-audio stop` |
| `--share` will not start, or OBS shows nothing | `elgato-doctor` has a Sharing section; the failure modes are listed in [OBS.md](OBS.md) |

This box wedges its USB link readily (`-EPROTO` on control transfers). Once it
does, everything above it looks broken and only a power cycle clears it;
setting module parameters back to 0 does **not** restore the previous register
values.

## Retired experiments

Kept in the tree as recorded negative results — the module parameters are in
`driver/patches/` and default to off, and the dead routine is left where it is
with the reasoning above it:

| experiment | result |
| ---------- | ------ |
| `elgato_progressive` | **worse** — 54.4% corrupt vs 12.8% |
| `elgato_fielddebounce` | **cannot work** — no frames delivered at all |
| bulk instead of isochronous | identical; bandwidth starvation excluded |
| `elgato_timing`, `elgato_modectrl` | no improvement |
| the lock-acquire routine `elgato_htl=2` was meant to add | **unreachable, and could not help** — see `cx231xx-avcore.c` |

Also excluded by measurement: USB packet loss (`err=0 eproto=0` over 2M
packets) and false `FF 00 00` SAV codes in the payload (no `0x00` or `0xFF`
bytes exist in it at all).

## Driver provenance

`driver/cx231xx/` is the mainline Linux `cx231xx` driver, taken verbatim from
the stable tree at **6.18.45**, with every deviation kept as a separate patch
in `driver/patches/` so it is always obvious what was changed and why. The
original SPDX tags and Conexant / Mauro Carvalho Chehab copyright notices are
intact; those files remain GPL-2.0-or-later and are not this project's work.

Rebasing onto another kernel means re-applying `driver/patches/*.patch` to that
kernel's `cx231xx` sources. Patches 0005 and 0006 are retired and need not be
applied — see above.

## Requirements

Kernel headers for the running kernel, a C compiler and `make` to build the
module against them, `v4l-utils`, GStreamer (`base`, `good` and a Wayland or
X11 sink), PipeWire with `pw-loopback`, and `python3` — which `--verify`,
`--quality`, the viewer's window and `elgato-record` all use. Being
out-of-tree, the module taints the kernel whatever else is true of it, and
under Secure Boot it has to be signed — see [Distributions](#distributions).

Package names in this file are Arch's, since that is what it was written on.
They differ elsewhere: `gst-libav` is `gstreamer1.0-libav` on Debian and
`gstreamer1-libav` on Fedora, and so on. Rather than translating this list by
hand, run `install.sh` — it checks for each tool and names the package for the
distribution you are actually on, including which extra repository it needs
where that applies.

Recording — the `r` key and `elgato-record` alike — additionally needs
`gst-libav`, for the FFV1 encoder, and the `h264` codec needs
`gst-plugins-ugly` for x264. Neither is required to watch: without them
everything else works and both say which package is missing. `s` needs nothing
beyond `good`.

`elgato-record` needs `python-gobject` (`python3-gi` on Debian) but **not**
GTK, which is what lets it run over SSH; the viewer's keys need both. Nothing
in the headless path imports GTK, and a display is never opened — tested with
`DISPLAY` and `WAYLAND_DISPLAY` unset.

`--share` additionally needs the `v4l2loopback` module; `install.sh` offers to
install and configure it under whatever name your distribution uses, and
`install.sh --no-share` skips it. Nothing else depends on it. On Arch and
Debian it arrives as `v4l2loopback-dkms`; on Fedora it is `akmod-v4l2loopback`
from RPM Fusion, and on openSUSE `v4l2loopback-kmp-default` — `install.sh` says
so if the repository it lives in is not enabled. `v4l2loopback-utils`, where
there is one, is not required; it only adds inspection tools such as
`v4l2loopback-ctl query`. Note that DKMS rebuilds the module against each new
kernel only if that kernel's headers are installed.

### Distributions

`install.sh` and `uninstall.sh` are not tied to a distribution. They find the
package manager by looking for it rather than by reading an ID, so derivatives
work without being named — this was developed on Manjaro, which needs no case
of its own — and every package name, the way to become root, and the module
directory are decided at run time in `lib/elgato-distro.sh`.

Arch, Debian, Ubuntu, Fedora, openSUSE, Void and Alpine are handled end to end,
and were tested that way: both scripts run, install through uninstall, in a
container of each. Gentoo is recognised and its package names are known, but
Portage is left for you to drive — `install.sh` prints the `emerge` line rather
than running it. On anything else everything that does not need a package
manager still installs, and you are told what could not be named for you.

Two things are worth knowing up front:

- **Secure Boot.** Fedora, Ubuntu and openSUSE ship with it on, and it refuses
  unsigned modules. `install.sh` signs the build with the machine's existing
  Machine Owner Key if DKMS or akmods has already enrolled one, and otherwise
  prints the `openssl` / `mokutil` recipe to enrol your own. Everything that is
  not the kernel module installs either way.
- **No systemd.** The udev rule hands the device over with `TAG+="uaccess"`,
  which is logind's doing. Without logind, `install.sh` says so and points you
  at the `video` group instead.

Run them as yourself, never with `sudo` — both refuse to run as root, and
escalate only the individual steps that need it. `sudo`, `doas` and plain `su`
all work. `-y` answers every prompt, for unattended installs.

## Removing it

```
./uninstall.sh                 # everything, reverts to the stock driver
./uninstall.sh --keep-driver   # rules and launchers only
```

`-y` / `--yes` skips the confirmation prompt.

Nothing inside this directory is modified or deleted. Removing the patched
driver brings the tearing back — the stock module has no `elgato_htl`. The
v4l2loopback configuration is removed and the module unloaded, but the
v4l2loopback package itself is left alone: your package manager owns it and
other software may be using it. `uninstall.sh` prints the removal command for
your distribution if you want it gone.

## Credits

The reverse engineering that identified `HTL_CTRL` and `PLL_CTRL`, the
diagnosis of the frame corruption, `elgato-viewer`, the measurement tooling and
the driver instrumentation were written by **Claude** (Anthropic), working with
the author across several sessions. The author directed the work, ran every
privileged step, and made the calls that mattered — including the two-pass
sweep that separated the real fix from measurement noise.

Copyright in this project's own code is held by Hypnotize; machine-generated
material is not itself copyrightable, so the licence grant below rests on the
author's rights in the work as a whole.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE) for the full text.

The `cx231xx` sources under `driver/` are GPL-2.0-or-later kernel code and
carry their original copyright notices; this project's own files carry
`SPDX-License-Identifier: GPL-2.0-or-later`.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.

## Trademarks

"Elgato" is a trademark of Corsair Gaming, Inc. This project is not affiliated
with, endorsed by, or supported by Corsair or Elgato. The name is used only to
identify the hardware this software works with.
