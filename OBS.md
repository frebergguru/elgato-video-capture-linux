# OBS and the Elgato Video Capture

OBS needs nothing from this repository to get a clean picture. The fix that
matters lives in the kernel module, so once `install.sh` has run, OBS is handed
exactly the same frames `elgato-viewer` gets — if the picture tears in OBS,
check `cat /sys/module/cx231xx/parameters/elgato_htl` before suspecting OBS.

What OBS does need is a dozen settings that are all wrong by default for an
SDTV capture card, and all of them are buried. That is what the setup script is
for.

```
elgato-obs-setup      # with OBS closed
obs                   # the profile and scene collection are already selected
```

## The one rule, and the way round it

**Only one program at a time can own `/dev/elgato`.** `cx231xx` has a single
buffer queue, so whichever program starts streaming first owns the card and the
next one gets `EBUSY`. That is not a bug to work around; it is how the device
works, and no amount of configuration changes it.

| symptom | meaning |
| ------- | ------- |
| the OBS source is black, log says `Failed to open device` | something else has it |
| `elgato-viewer` says `Device or resource busy` | OBS has it |

Who is holding it: `elgato-doctor`, which names the process, or `fuser -v /dev/elgato`.

What the rule does **not** say is that only one program can see the picture.
Capture it once and you can hand the frames to as many readers as you like:

```
elgato-viewer --share        # captures, and republishes on /dev/elgato-share
elgato-obs-setup --share     # points OBS at that node instead of the card
```

`--share` tees the pipeline into a **v4l2loopback** device — a virtual camera
that OBS, a browser, or anything else opens like an ordinary webcam, while the
viewer keeps the card. `install.sh` sets the loopback up; `elgato-doctor` has a
**Sharing** section that says whether it is there and who is using it.

What goes down the loopback is what came off the card: 720x576, YUYV, still
interlaced, still SMPTE 170M limited range — the frames are tapped ahead of the
deinterlacer and nothing relabels them. A reader sees `Field: Interlaced` and
GStreamer negotiates `interlace-mode=interleaved`, so both sides know what they
are looking at and make their own choices: the viewer's `c` key still cycles
deinterlacers, and OBS still does Yadif 2x onto a 960x720 canvas exactly as it
did when it owned the card. None of the settings in the table below change.

Pixel aspect is the one thing V4L2 cannot carry, and it never could — that is
why the 4:3 canvas stretch does the anamorphic correction, sharing or not.

Two things follow from the viewer being the one holding the card:

* **Start the viewer first.** With nothing writing to it the loopback node is
  an *output* device, not a camera, and OBS may not list it at all.
* **Close the viewer and OBS goes black** — but not permanently. The profile
  written by `elgato-obs-setup --share` turns OBS's `auto_reset` off, so OBS
  keeps its handle on the node instead of trying to re-open a device that has
  gone back to being an output, and the picture returns when the viewer does.

Audio never had this problem. PipeWire hands the same capture stream to as many
readers as ask for it, so `elgato-audio`'s loopback, OBS and anything else can
all have the sound at once, sharing or not. That is exactly why `elgato-audio`
uses `pw-loopback` instead of pulling audio into a pipeline.

## Three ways to work

### Both at once

For playing a game while streaming it, with the lowest latency on the hand that
is holding the controller.

```
elgato-viewer --share        # start this first
elgato-obs-setup --share     # once, with OBS closed
obs
```

You watch the viewer's window, which is as direct as this card gets; OBS reads
the same frames off `/dev/elgato-share` and can take its time compositing them.
Neither one is a screen grab of the other, and neither is waiting on the other's
clock.

The cost is one extra copy of every frame — about 21 MB/s of memory bandwidth,
which is nothing — and the ordering rule above.

### OBS owns the device

For recording tapes, archiving, streaming — anything where the recording is the
point. Add the V4L2 source (the script does), and monitor with a projector, not
the preview pane: right-click the preview → **Fullscreen Projector (Preview)**,
or Windowed Projector onto a second screen.

The cost is latency. OBS composites on its own clock and the picture crosses
the GPU and the compositor before you see it; it is comfortably more than
`elgato-viewer`'s and you will feel it on a controller. Fine for a VCR, not for
playing.

### The viewer owns the device

The fallback for the same job as **Both at once**, when v4l2loopback is not
available — an unbuilt DKMS module after a kernel upgrade, a distribution that
does not package it. Run `elgato-viewer` as usual and capture its *window* in
OBS with **Window Capture (PipeWire)** — the `linux-pipewire` plugin, via the
desktop portal. You keep the viewer's latency for the hand on the controller;
the extra compositor round trip lands in the recording, where nobody is waiting
on it.

What OBS gets here is a screen grab, so it inherits whatever the viewer already
did to the picture — deinterlaced, scaled to the window, resampled by the
compositor. `--share` avoids all of that by handing over the frames themselves,
which is why it is the better route when it is available.

Audio still comes from the PipeWire capture node, exactly as in the other
route. Do not also capture Desktop Audio — see below.

### Neither

```
elgato-viewer --record raw.yuv
```
writes the raw captured frames while you play, for encoding afterwards. No OBS,
no compositor, no encoder competing for the CPU. Raw YUYV is about 21 MB/s, so
keep it short.

## What the script writes

Two plain files, plus a pointer to them:

```
<config>/basic/profiles/<name>/basic.ini    canvas, frame rate, output
<config>/basic/scenes/<name>.json           the sources and the scene
```

`<config>` is `~/.config/obs-studio` for a native OBS and
`~/.var/app/com.obsproject.Studio/config/obs-studio` for the flatpak. Both can
be installed at once and they share nothing; the script picks whichever has
been run and says which, and `--native` / `--flatpak` override it.

**OBS must not be running.** It holds its whole configuration in memory and
writes it out at exit, so anything written underneath it is silently reverted.
The script refuses rather than letting that happen.

### The settings that are not obvious

| setting | value | why |
| ------- | ----- | --- |
| device | `/dev/elgato` | the udev symlink. The video node number moves when the built-in webcam re-enumerates |
| resolution, frame rate | *Leave Unchanged* | the card answers `VIDIOC_ENUM_FRAMESIZES` and `VIDIOC_ENUM_FRAMEINTERVALS` with `EINVAL`. There is nothing else to pick, and the TV standard already fixes the format |
| standard | `0xff` PAL / `0xb000` NTSC | OBS stores the `v4l2_std_id`, not the list index. It has to match an id the driver enumerates |
| pixel format | YUYV 4:2:2 | the only thing this decoder emits |
| colour range | partial | the decoder emits limited-range YUV. "Full" is the classic crushed-blacks, blown-highlights mistake |
| buffering | off | frames are drawn as they land. Turn it on with `--buffering` if the picture stutters under load |
| deinterlacing | Yadif 2x | see below |
| canvas | 960x720, source **stretched** to fill | PAL is 720x576 sampled with 54:59 pixels. OBS assumes square ones, so an aspect-preserving fit renders everybody 11% too tall. A 4:3 canvas plus stretch *is* the anamorphic correction |
| canvas frame rate | 50 (PAL) / 59.94 (NTSC) | one frame per field — the whole point of a 2x deinterlacer |
| Desktop Audio | disabled | it would record the loopback playing the capture to your speakers: this card's own sound a second time, and slightly later |
| recording format | MKV | a killed or crashed OBS leaves a playable MKV. Remux to MP4 afterwards (**File → Remux Recordings**) |

`--dry-run` prints both files without writing anything, which is the quickest
way to see what any of the options does.

## Deinterlacing

The source is interlaced and OBS deinterlaces **per scene item**, from the
source's right-click menu, off by default. The script sets Yadif 2x.

A console emits 50 progressive fields a second; a woven 576-line frame holds
two moments 20 ms apart. A 1x deinterlacer throws one of those moments away and
gives you 25fps. A 2x deinterlacer emits a frame per field, so with the canvas
at 50fps you get genuinely 50fps motion out of a 25fps capture. This is a
bigger visible win than the choice of algorithm.

If motion judders or seems to step backwards, the field order is wrong: flip
**Top Field First** / **Bottom Field First** in the same menu, or re-run with
`--field bottom`. It is much easier to see on a horizontal pan than on a still.

Yadif here is the same algorithm the viewer defaults to, measured on this card
at 0.31 combing against 1.04 for `linear` — see the table in README.md.

## Audio

Use **Audio Input Capture (PulseAudio)** and pick
`alsa_input.usb-Elgato_Video_Capture…` by name.

- **Not** ALSA Input Capture. Only one client may open the raw ALSA device, and
  `elgato-audio` has it. Two things fighting over `hw:N,0` is the "garbled
  audio" symptom.
- **Not** "Default". The WirePlumber rule in `etc/` deliberately gives this
  node `priority.session = 100` so it never becomes your system microphone —
  the whole point being that you do not transmit the capture into voice chat.
  It will therefore never be the default OBS picks up either.
- Leave the source's monitoring on **Monitor Off**. `elgato-audio`'s loopback is
  already playing the capture to your speakers at 15 ms; monitoring it through
  OBS as well gives you both, a few milliseconds apart. Pick one — the loopback
  is the lower-latency of the two, and it keeps working when OBS is closed.

Monitoring through the loopback while watching the OBS preview means the sound
arrives before the picture. That is the preview being slow, not the recording:
inside the file, OBS timestamps both itself and they line up. If a finished
recording is consistently out, correct it once in **Advanced Audio Properties →
Sync Offset**.

## Recording

The profile records with x264 at the "High Quality" preset into MKV. SD
material at 25 or 50fps needs very little bitrate — this is not where to spend
effort. If you want a specific codec or hardware encoding, switch **Settings →
Output** to Advanced; nothing else in the setup depends on it.

## If something is wrong

| symptom | cause |
| ------- | ----- |
| source black, `Failed to open device` in the log | `elgato-viewer`, `elgato-doctor` or another OBS has the device |
| source black, device opens fine | no signal — check the yellow RCA, and the standard (`--pal` / `--ntsc`) |
| `Selected video format not supported` | resolution or frame rate is not *Leave Unchanged* |
| picture tears with rainbow striping | `elgato_htl` is not 2. `cat /sys/module/cx231xx/parameters/elgato_htl` |
| everybody is too tall | the scene item is aspect-fitted rather than stretched to a 4:3 canvas |
| motion combs | deinterlacing is off on the scene item |
| motion judders in 2x | wrong field order |
| audio doubled or echoing | Desktop Audio is capturing the loopback, or OBS monitoring is on as well as the loopback, or there are stray loopbacks (`elgato-audio stop`) |
| no Elgato source in the audio list | the loopback or the card is gone — `elgato-doctor` |
| the settings vanished | OBS was running when `elgato-obs-setup` wrote them |
| sharing: no `/dev/elgato-share` | v4l2loopback is not loaded — `elgato-doctor`, then `sudo modprobe v4l2loopback` |
| sharing: OBS does not list the share node | nothing is writing to it yet — start `elgato-viewer --share` first |
| sharing: `elgato-viewer` says the share device is already being written to | another producer has it; the message names the process |
| sharing: `is not a output device`, nothing is using the node | something set `keep_format` on it (`v4l2loopback-ctl set-caps` does). `elgato-viewer --share` now clears it for you; by hand it is `v4l2-ctl -d /dev/elgato-share --set-ctrl keep_format=0` |
| sharing: OBS loses the source for good when the viewer stops | the profile predates `--share` — re-run `elgato-obs-setup --share --force` to get `auto_reset` off |

**Opening Properties on the video source is not free.** OBS's device dropdown
enumerates `/dev/videoN` only, so it cannot show `/dev/elgato` as the selected
entry. Press Cancel, not OK, unless you actually mean to change something —
OK-ing it can write whatever the dropdown happens to be showing. If you would
rather have a device that appears in the list, run
`elgato-obs-setup --device /dev/video4 --force`, and re-run it when the number
moves.

### Flatpak

The flatpak needs `devices=all` (its default) to see `/dev/video*`, and the
PipeWire socket for audio and for window capture. Both are in the default
manifest; `flatpak info --show-permissions com.obsproject.Studio` confirms it.
The `/dev/elgato` symlink is visible inside the sandbox.
