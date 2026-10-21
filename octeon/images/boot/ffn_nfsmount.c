/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Kernel text NFS API; no dynamic dependency on the filesystem being mounted.
 *
 *   ffn_nfsmount server:/export /target options        mount the root
 *   ffn_nfsmount route SERVER via GATEWAY dev IFNAME     reach a server beyond the link
 *
 * The second form installs a host route to SERVER through GATEWAY on IFNAME
 * with the interface's own IPv4 address as the preferred source. The planes
 * number their PCIe links in 127/8, a range the kernel treats as host scope:
 * without the explicit source a packet routed through the gateway leaves with
 * source 0.0.0.0 and no server can answer it (seen 2026-10-21 when the DP
 * tried the MP's NFS server through the CP). The initramfs busybox carries no
 * `ip` applet, so this is the route installer. Exit 1 while the interface has
 * no address yet (the transport is still coming up): the caller retries. */
#include <arpa/inet.h>
#include <errno.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <unistd.h>

static int usage(void)
{
    fprintf(stderr, "usage: ffn_nfsmount server:/export /target options\n"
                    "       ffn_nfsmount route SERVER via GATEWAY dev IFNAME\n");
    return 2;
}

static int interface_address(const char *name, struct in_addr *out, int *index)
{
    struct ifreq request;
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    memset(&request, 0, sizeof request);
    snprintf(request.ifr_name, IFNAMSIZ, "%s", name);
    if (ioctl(fd, SIOCGIFINDEX, &request)) { close(fd); return -1; }
    *index = request.ifr_ifindex;
    if (ioctl(fd, SIOCGIFADDR, &request)) { close(fd); return -1; }
    *out = ((struct sockaddr_in *)&request.ifr_addr)->sin_addr;
    close(fd);
    return 0;
}

static void attribute(struct nlmsghdr *message, int type, const void *data, size_t length)
{
    struct rtattr *entry = (struct rtattr *)((char *)message + NLMSG_ALIGN(message->nlmsg_len));
    entry->rta_type = (unsigned short)type;
    entry->rta_len = (unsigned short)RTA_LENGTH(length);
    memcpy(RTA_DATA(entry), data, length);
    message->nlmsg_len = NLMSG_ALIGN(message->nlmsg_len) + RTA_ALIGN(entry->rta_len);
}

/* RTM_NEWROUTE SERVER/32 via GATEWAY dev IFNAME src <IFNAME's address>, replacing any earlier one. */
static int route(const char *server, const char *gateway, const char *ifname)
{
    struct { struct nlmsghdr header; struct rtmsg route; char attributes[256]; } request;
    struct { struct nlmsghdr header; struct nlmsgerr error; char tail[256]; } reply;
    struct in_addr destination, via, source;
    struct sockaddr_nl kernel = { .nl_family = AF_NETLINK };
    int index, fd;
    ssize_t n;
    if (inet_pton(AF_INET, server, &destination) != 1 || inet_pton(AF_INET, gateway, &via) != 1) return usage();
    if (interface_address(ifname, &source, &index)) {
        fprintf(stderr, "route: %s has no IPv4 address yet (%s)\n", ifname, strerror(errno));
        return 1;
    }
    memset(&request, 0, sizeof request);
    request.header.nlmsg_len = NLMSG_LENGTH(sizeof request.route);
    request.header.nlmsg_type = RTM_NEWROUTE;
    request.header.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_REPLACE;
    request.header.nlmsg_seq = 1;
    request.route.rtm_family = AF_INET;
    request.route.rtm_dst_len = 32;
    request.route.rtm_table = RT_TABLE_MAIN;
    request.route.rtm_protocol = RTPROT_BOOT;
    request.route.rtm_scope = RT_SCOPE_UNIVERSE;
    request.route.rtm_type = RTN_UNICAST;
    attribute(&request.header, RTA_DST, &destination, sizeof destination);
    attribute(&request.header, RTA_GATEWAY, &via, sizeof via);
    attribute(&request.header, RTA_PREFSRC, &source, sizeof source);
    attribute(&request.header, RTA_OIF, &index, sizeof index);
    fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE);
    if (fd < 0) { perror("netlink"); return 1; }
    if (sendto(fd, &request, request.header.nlmsg_len, 0, (struct sockaddr *)&kernel, sizeof kernel) < 0) { perror("route request"); close(fd); return 1; }
    n = recv(fd, &reply, sizeof reply, 0);
    close(fd);
    if (n < (ssize_t)NLMSG_LENGTH(sizeof reply.error) || reply.header.nlmsg_type != NLMSG_ERROR) { fprintf(stderr, "route: no acknowledgement\n"); return 1; }
    if (reply.error.error) { fprintf(stderr, "route: %s\n", strerror(-reply.error.error)); return 1; }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 7 && !strcmp(argv[1], "route") && !strcmp(argv[3], "via") && !strcmp(argv[5], "dev")) return route(argv[2], argv[4], argv[6]);
    if (argc != 4) return usage();
    if (mount(argv[1], argv[2], "nfs", 0, argv[3])) {
        perror("NFS mount");
        return 1;
    }
    return 0;
}
