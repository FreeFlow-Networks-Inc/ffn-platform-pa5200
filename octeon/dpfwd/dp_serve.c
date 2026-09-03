/* dp_serve -- run the real DP command loop over an mmap'd file.
 *
 * Exists so the Python MP client (ffn_dpring.py) can be tested against the
 * actual C implementation rather than against a second model of the wire
 * format. Two independent implementations agreeing in separate unit tests
 * proves nothing about interop; this closes that gap.
 *
 *   dp_serve <region-file> <seconds>
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "ffn_dp_oct.h"

/* minimal io ops -- this harness never forwards a packet. Signatures taken
 * from struct dp_io_ops in ffn_dp_oct.h, not guessed. */
static int nop_init(void *a) { (void)a; return DP_OK; }
static void nop_fini(void *a) { (void)a; }
static int nop_rx(void *a, struct dp_pkt *b, int max)
{
    (void)a; (void)b; (void)max; return 0;
}
static int nop_tx(void *a, struct dp_pkt *b, int n)
{
    (void)a; (void)b; return n;
}
static void nop_pkt(void *a, struct dp_pkt *p) { (void)a; (void)p; }

static const struct dp_io_ops SERVE_IO = {
    .name = "file-serve",
    .init = nop_init,
    .fini = nop_fini,
    .rx = nop_rx,
    .tx = nop_tx,
    .to_local = nop_pkt,
    .to_offload = nop_pkt,
    .free_pkt = nop_pkt,
};

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <region-file> <seconds>\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    double secs = atof(argv[2]);

    int fd = open(path, O_RDWR);
    if (fd < 0) { perror("open"); return 1; }
    struct stat st;
    if (fstat(fd, &st)) { perror("fstat"); return 1; }
    size_t sz = (size_t)st.st_size;
    void *region = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (region == MAP_FAILED) { perror("mmap"); return 1; }

    struct dp_ctx ctx;
    if (dp_init(&ctx, &SERVE_IO, NULL, 1024) != DP_OK) {
        fprintf(stderr, "dp_init failed\n");
        return 1;
    }
    /* fresh=1: lay down the header, rings and caps the MP will read */
    if (dp_region_attach(&ctx, region, sz, 1) != DP_OK) {
        fprintf(stderr, "dp_region_attach failed\n");
        return 1;
    }
    printf("serving %s (%zu bytes) for %.1fs\n", path, sz, secs);
    fflush(stdout);

    struct timespec ts = { 0, 2 * 1000 * 1000 };   /* 2 ms */
    double waited = 0.0;
    int total = 0;
    while (waited < secs && !ctx.stop) {
        int n = dp_service_commands(&ctx);
        total += n;
        nanosleep(&ts, NULL);
        waited += 0.002;
    }
    printf("handled %d command(s)\n", total);
    msync(region, sz, MS_SYNC);
    return 0;
}
