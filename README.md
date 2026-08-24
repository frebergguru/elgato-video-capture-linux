# Elgato Video Capture V2 on Linux

Watch anything on composite or S-Video through an Elgato Video Capture V2
(USB `0fd9:0037`, Conexant cx231xx) — a games console, a VCR, a DVD player, a
camcorder — with low enough latency to play games on, and without the frame
tearing the stock driver produces.

```
./install.sh          # build + install the driver, rules and launchers
elgato-viewer         # play
elgato-viewer --verify   # prove the picture is clean
```

> **This builds and loads an out-of-tree kernel module.** It is unsigned, so it
> taints the kernel and will not load under Secure Boot without signing. The
> module writes decoder registers on one specific USB device; a bad setting can
> leave the capture chip emitting nothing until it is power-cycled
> (`elgato-viewer --reset`). There is no warranty — see LICENSE.

## The problem this fixes

Out of the box roughly **60% of frames are corrupt** — sheared diagonally with
rainbow striping. The cause is not USB, not bandwidth, and not the player:

> The stock cx231xx driver never programs the cx25840 decoder's **horizontal
> time-lock loop**. SAV-to-SAV line lengths hunt instead of holding constant,
> so every line is sampled at a different horizontal phase.

The patched driver programs `HTL_CTRL` and `PLL_CTRL` and runs a lock-acquire
routine, behind `elgato_htl=2`:

| `elgato_htl` | frames corrupt |
| ------------ | -------------- |
| `0` (stock behaviour) | ~60% |
| **`2`** | **0.0%** |

Measured over 750 frames across six consecutive runs, and confirmed by eye.
`install.sh` sets it permanently in `/etc/modprobe.d/cx231xx.conf`.

**The fix lives in the kernel module.** `elgato-viewer` cannot compensate for
it — corrupt frames arrive at exactly 25fps with exactly the right byte count,
carrying data spliced from several moments. If you install with
`--no-driver`, expect the tearing.

## What is here

```
bin/elgato-viewer   the player
bin/elgato-audio    audio loopback (pw-loopback); elgato-viewer starts it
bin/elgato-doctor   end-to-end diagnostics
bin/elgato-reset    power-cycle a wedged capture chip
lib/                shared helpers for the three elgato-* helper scripts
driver/cx231xx/     patched cx231xx source
driver/patches/     every deviation from the stock kernel tree, with rationale
etc/                udev, modprobe and WirePlumber configuration
LICENSE             GPL-2.0-or-later, full text
```

## Playing

```
elgato-viewer                      # composite, 960x720, sound on
elgato-viewer -f --sharp           # fullscreen, nearest-neighbour pixels
elgato-viewer --deinterlace weave  # sharpest for 240p/288p progressive sources
elgato-viewer --ntsc               # or --pal
elgato-viewer --input svideo
elgato-viewer --reset              # power-cycle first
```

### Keys

With the video window focused:

| key | |
| --- | --- |
| `f` | fullscreen on/off |
| `z` / `x` | smaller / larger window |
| `m` | mute/unmute the capture audio — other apps keep playing |
| `h` | show the keys on screen |
| `q` | quit |
| `Esc` | close the help overlay, or quit |

`--help` lists the command-line options.

Keys need `python-gobject`, `gtk4` and `gst-plugin-gtk4`. Without them the viewer
still plays, but there is nothing to press: `gst-launch-1.0` has no keyboard
plumbing, and `waylandsink` does not implement `GstNavigation`, so a key press
never reaches it. Close the window or press Ctrl-C instead.

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

Deinterlacing defaults to `linear`: one field, no temporal lookahead. `yadif`
looks better but costs about a frame of latency, which you can feel on a
controller.

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
colour, a clean picture has flat colour fields. It was validated against
frames inspected by eye (clean 2.6, corrupt 13.5) after three earlier metrics
gave false passes. Validate any replacement the same way.

One caveat worth knowing: the threshold was calibrated on flat-colour graphics
from a games console. Film and tape content — VHS, DVD, camcorder footage —
varies more from line to line by nature, so the figure may read high even on a
healthy capture. Judge by whether the reported chroma spread is *bimodal*
(clean frames clustering low, corrupt ones clustering far above) rather than by
the percentage alone, and raise `THRESH` in the script if your material needs
it.

It also refuses to pass judgement when the input is black: a disconnected or
powered-off source has perfectly coherent chroma and would otherwise score a
clean 0%.

The register constants in `driver/` are specific to this board and are not the
chip's power-on defaults or documented in the cx25840 datasheet. Do not take
them on trust — `--tune` applies each one and scores it, so every claim in this
repository can be checked against your own hardware.

`--tune` runs two interleaved passes. Compare each configuration against its
own figure in the other pass — a difference that does not survive both passes
is drift in the source material, not an effect of the setting. A single
pass is not evidence: the winning configuration looked broken in pass 1.

## If something is wrong

Run `elgato-doctor` first; it checks the whole chain and says which link failed.

| symptom | cause |
| ------- | ----- |
| picture tears, ~60% of frames | `elgato_htl` is not 2 — `elgato-viewer` warns about this |
| no frames, `cannot change alt number` | USB link wedged — `elgato-viewer --reset` |
| frames but no picture | check the yellow RCA at both ends, and `--ntsc`/`--pal` |
| captured audio in voice chat | WirePlumber rule missing — re-run `install.sh` |
| garbled or doubled audio | stray loopbacks — `elgato-audio stop` |

This box wedges its USB link readily (`-EPROTO` on control transfers). Once it
does, everything above it looks broken and only a power cycle clears it;
setting module parameters back to 0 does **not** restore the previous register
values.

## Retired experiments

Kept in `driver/patches/`, defaulting to off, as recorded negative results:

| experiment | result |
| ---------- | ------ |
| `elgato_progressive` | **worse** — 54.4% corrupt vs 12.8% |
| `elgato_fielddebounce` | **cannot work** — no frames delivered at all |
| bulk instead of isochronous | identical; bandwidth starvation excluded |
| `elgato_timing`, `elgato_modectrl` | no improvement |

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

Kernel headers for the running kernel, `v4l-utils`, GStreamer (`base`, `good`
and a Wayland or X11 sink), PipeWire with `pw-loopback`, and `python3` for
`--verify`. The module is unsigned, so it taints the kernel and will not load
under Secure Boot without signing.

## Removing it

```
./uninstall.sh                 # everything, reverts to the stock driver
./uninstall.sh --keep-driver   # rules and launchers only
```

Nothing inside this directory is modified or deleted. Removing the patched
driver brings the tearing back — the stock module has no `elgato_htl`.

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
