# Out-of-tree build of the cx231xx driver.
#
# Sources are verbatim from the stable kernel tree at the tag matching the
# running kernel; any deviation lives in a patch under ../patches so it is
# always obvious what was changed and why.
#
# cx231xx-dvb is deliberately not built: this device has no demodulator
# (tuner_type = TUNER_ABSENT) and skipping it avoids the dvb-frontend deps.

obj-m += cx231xx.o
cx231xx-y := cx231xx-video.o cx231xx-i2c.o cx231xx-cards.o cx231xx-core.o \
             cx231xx-avcore.o cx231xx-417.o cx231xx-pcb-cfg.o cx231xx-vbi.o
cx231xx-$(CONFIG_VIDEO_CX231XX_RC) += cx231xx-input.o

obj-m += cx231xx-alsa.o
cx231xx-alsa-objs := cx231xx-audio.o

ccflags-y += -I $(srctree)/drivers/media/tuners
ccflags-y += -I $(srctree)/drivers/media/dvb-frontends

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
