/*
 * FFN: let the SDK gcc 4.7 crt1.o link against a modern glibc.
 *
 * The CP daemons have to be built with the SDK toolchain -- it is the only
 * mips64 big-endian compiler here that has a libc at all (the kernel.org
 * crosstool is nolibc, and no Buildroot host toolchain survives). But its own
 * static glibc is from 2012 and dies on this kernel: ffn_pcnetd built -static
 * took a SIGSEGV inside ptmalloc_init, reached from malloc_hook_ini, i.e. on
 * its first malloc, before a line of its own code ran. Nothing to do with the
 * transport, and /dev/mem itself is fine -- busybox (glibc 2.41, same rootfs)
 * reads 0x29000000 through it without complaint.
 *
 * So link against the rootfs glibc 2.41 instead, which is already proven on
 * this kernel by every other binary in the initramfs. The only obstacle is
 * that glibc 2.34 REMOVED __libc_csu_init and __libc_csu_fini: modern crt1.o
 * passes NULL for them and __libc_start_main runs the init/fini arrays itself.
 * The SDK crt1.o predates that and still references both symbols, so the link
 * fails without these.
 *
 * No-ops are correct here specifically because these daemons are plain C with
 * no __attribute__((constructor)) or ((destructor)) -- checked, zero of either
 * -- so their init_array holds nothing that has to run. If a constructor is
 * ever added to one of them, this file must grow into the real thing: walk
 * __preinit_array, __init_array and __fini_array between their linker-provided
 * start/end symbols. A silently skipped constructor would be a nasty bug, so
 * that is worth remembering rather than rediscovering.
 */

void __libc_csu_init(int argc, char **argv, char **envp);
void __libc_csu_fini(void);

void __libc_csu_init(int argc, char **argv, char **envp)
{
	(void)argc;
	(void)argv;
	(void)envp;
}

void __libc_csu_fini(void)
{
}
