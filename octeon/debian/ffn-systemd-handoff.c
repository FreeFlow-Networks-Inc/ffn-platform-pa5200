#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mount.h>
#include <sys/wait.h>
#include <errno.h>

/* Statically linked: this code and the transport must survive NFS failures.
 * The caller bind-mounts the initramfs at newroot/oldroot before entering.
 * Never delete the initramfs; the recovery agent and transports live there.
 */
int main(int argc, char **argv)
{
    int moved = 0;
    if (argc != 2 || argv[1][0] != '/' || !strcmp(argv[1], "/")) {
        fprintf(stderr, "usage: ffn-systemd-handoff /mounted-new-root\n");
        return 2;
    }
    if (getpid() != 1) {
        fprintf(stderr, "refusing root handoff outside PID 1\n");
        return 2;
    }
    if (chdir(argv[1]) || access("sbin/init", X_OK) ||
        access("oldroot/bin/busybox", X_OK)) {
        perror("validate staged root");
        goto recover;
    }
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) ||
        mount(".", "/", NULL, MS_MOVE, NULL)) {
        perror("move root mount");
        goto recover;
    }
    moved = 1;
    if (!chroot(".") && !chdir("/")) {
        execl("/sbin/init", "init", (char *)NULL);
        perror("exec Debian init");
    }
    /* The move committed. Recover a console from RAM if exec fails; never
     * return from PID 1 and never fetch the recovery shell through NFS. */
recover:
    if (moved && chdir("/oldroot") == 0) { chroot("."); }
    chdir("/");
    for (;;) {
        pid_t child = fork();
        if (child == 0) {
            execl("/bin/busybox", "busybox", "sh", "-i", (char *)NULL);
            _exit(127);
        }
        if (child > 0) while (waitpid(child, NULL, 0) < 0 && errno == EINTR) {}
        sleep(2);
    }
}
