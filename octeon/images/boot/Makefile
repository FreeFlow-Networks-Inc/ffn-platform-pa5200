CROSS ?= mips64-linux-gnuabi64-
CC = $(CROSS)gcc
PROGRAMS = ffn_dpagent2 ffn-systemd-handoff ffn_nfsmount
all: $(PROGRAMS)
%: %.c
	$(CC) -O2 -Wall -Wextra -EB -mabi=64 -static -o $@ $<
	$(CROSS)strip $@
clean:
	rm -f $(PROGRAMS)
.PHONY: all clean
