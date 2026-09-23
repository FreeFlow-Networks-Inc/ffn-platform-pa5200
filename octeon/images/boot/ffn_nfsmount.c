/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Kernel text NFS API; no dynamic dependency on the filesystem being mounted. */
#include <stdio.h>
#include <sys/mount.h>
int main(int argc, char **argv)
{
    if (argc != 4) {
        fprintf(stderr, "usage: ffn_nfsmount server:/export /target options\n");
        return 2;
    }
    if (mount(argv[1], argv[2], "nfs", 0, argv[3])) {
        perror("NFS mount");
        return 1;
    }
    return 0;
}
