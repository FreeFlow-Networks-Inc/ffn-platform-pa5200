/* SPDX-License-Identifier: GPL-2.0-or-later
 * Included by the native packet owner after the aggregate implementation.
 * A decoded original packet re-enters the existing inspection/INPUT/TAP path.
 * This interface does not grant permission to forward or install a flow.
 */
int ffn_packet_fe100(struct ffn_packet *p, const struct ffn_packet_fe100_binding *bindings,
                     unsigned count, uint64_t deadline)
{
    uint32_t sources[16], returns[8]; unsigned used = 0;
    struct sockaddr_storage address; socklen_t size = sizeof(address);
    int64_t now = milliseconds();
    if (!p || count > 8 || (count && (!bindings || now < 0 || deadline <= (uint64_t)now ||
        deadline - (uint64_t)now > 3000)) || (!count && deadline)) { errno = EINVAL; return -1; }
    if (p->aggregate) {
        for (unsigned i = 0; i < p->aggregate->members_count; i++) {
            sources[used++] = p->aggregate->members[i].source;
            if (p->aggregate->members[i].alias) sources[used++] = p->aggregate->members[i].alias;
        }
    } else sources[used++] = p->source;
    for (unsigned i = 0; i < count; i++) {
        const struct ffn_packet_fe100_binding *b = &bindings[i];
        int found = 0;
        if (b->scope.trunk != 24 || !b->scope.return_port || b->scope.return_port == 24 ||
            !b->scope.front || b->scope.front > 24 || !b->scope.zone)
            { errno = EINVAL; return -1; }
        for (unsigned j = 0; j < used; j++)
            if (b->scope.return_port == sources[j]) { errno = EINVAL; return -1; }
        if (p->aggregate) {
            for (unsigned j = 0; j < p->aggregate->members_count; j++) {
                const struct ffn_aggregate_member *m = &p->aggregate->members[j];
                if (b->scope.front == m->front && b->source == m->source) found = 1;
            }
        } else found = b->source == p->source;
        if (!found) { errno = EINVAL; return -1; }
        for (unsigned j = 0; j < i; j++)
            if (bindings[j].scope.return_port == b->scope.return_port &&
                bindings[j].scope.front == b->scope.front && bindings[j].scope.in_lif == b->scope.in_lif)
                { errno = EINVAL; return -1; }
        returns[i] = b->scope.return_port;
    }
    pthread_mutex_lock(&p->control);
    if (p->stopped || (p->started && (!p->pause_requested || !p->paused[0] || !p->paused[1]))) {
        pthread_mutex_unlock(&p->control); errno = EBUSY; return -1;
    }
    if (getsockname(p->rx, (struct sockaddr *)&address, &size) ||
        (address.ss_family == AF_PACKET && packet_filter(p->rx, sources, used,
            p->aggregate ? 3 : 1, returns, count))) {
        int error = errno; pthread_mutex_unlock(&p->control); errno = error; return -1;
    }
    if (count) memcpy(p->fe100, bindings, count * sizeof(*bindings));
    p->fe100_count = count; p->fe100_deadline = deadline;
    pthread_mutex_unlock(&p->control); return 0;
}
int ffn_packet_fe100_stats(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !out || count != 3) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) out[i] = atomic_load_explicit(&p->fe100_stats[i], memory_order_relaxed);
    return 0;
}
static int fe100_receive(struct ffn_packet *p, uint8_t *wire, ssize_t *size)
{
    if (*size < 4 || be16(wire) != 24) return 0;
    int is_return = 0;
    for (unsigned i = 0; i < p->fe100_count; i++)
        if (be16(wire + 2) == p->fe100[i].scope.return_port) is_return = 1;
    if (!is_return) return 0;
    int64_t now = milliseconds();
    if (now < 0 || (uint64_t)now >= p->fe100_deadline) {
        atomic_fetch_add_explicit(&p->fe100_stats[2], 1, memory_order_relaxed); return 1;
    }
    if (*size >= 12 && wire[11] == 19) {
        struct ffn_fe100_counters counters;
        /* The single accounting receiver owns these deltas. Data owners can
         * share the return link, but must neither forward nor double-count
         * valid statistics as malformed software-fallback packets. */
        if (ffn_fe100_counters_decode(wire,(size_t)*size,24,be16(wire+2),&counters)==1)return 1;
    }
    for (unsigned i = 0; i < p->fe100_count; i++) {
        const struct ffn_packet_fe100_binding *b = &p->fe100[i];
        struct ffn_fe100_punt decoded;
        int kind=ffn_fe100_punt_decode(wire, (size_t)*size, &b->scope, &decoded);
        if (!kind || decoded.length > MAX_FRAME) continue;
        if(kind==2)return 1; /* A separate leased control receiver owns this. */
        memmove(wire + 4, decoded.frame, decoded.length);
        wire[2] = (uint8_t)(b->source >> 8); wire[3] = (uint8_t)b->source;
        *size = (ssize_t)decoded.length + 4;
        atomic_fetch_add_explicit(&p->fe100_stats[0], 1, memory_order_relaxed); return 0;
    }
    atomic_fetch_add_explicit(&p->fe100_stats[1], 1, memory_order_relaxed); return 1;
}

static int control_bindings(const struct ffn_aggregate_member *m,unsigned nm,
    const struct ffn_packet_fe100_binding *b,unsigned nb)
{
    if(!m || !nm || nm>8 || nb>8 || (nb && !b))return 0;
    for(unsigned i=0;i<nm;i++) {
        if(!m[i].front || m[i].front>24 || m[i].source>65535 || m[i].alias>65535 ||
            (m[i].alias && m[i].alias==m[i].source))return 0;
        for(unsigned j=0;j<i;j++)if(m[i].front==m[j].front || m[i].source==m[j].source ||
            (m[i].alias && (m[i].alias==m[j].source || m[i].alias==m[j].alias)) ||
            (m[j].alias && m[i].source==m[j].alias))return 0;
    }
    for(unsigned i=0;i<nb;i++) {
        int owned=0;
        if(b[i].scope.trunk!=24 || !b[i].scope.return_port || b[i].scope.return_port==24 || !b[i].scope.zone)return 0;
        for(unsigned j=0;j<nm;j++) {
            if(b[i].scope.return_port==m[j].source || b[i].scope.return_port==m[j].alias)return 0;
            if(b[i].scope.front==m[j].front && b[i].source==m[j].source)owned=1;
        }
        if(!owned)return 0;
        for(unsigned j=0;j<i;j++)if(b[i].scope.return_port==b[j].scope.return_port &&
            b[i].scope.front==b[j].scope.front && b[i].scope.in_lif==b[j].scope.in_lif)return 0;
    }
    return 1;
}
int ffn_aggregate_control_fe100(int fd,const struct ffn_aggregate_member *m,unsigned nm,
    const struct ffn_packet_fe100_binding *b,unsigned nb,uint64_t deadline)
{
    uint32_t sources[16],returns[8];unsigned count=0;
    struct sockaddr_storage address;socklen_t length=sizeof(address);
    int64_t now=milliseconds();
    if(!control_bindings(m,nm,b,nb) || (nb && (now<0 || deadline<=(uint64_t)now ||
        deadline-(uint64_t)now>3000)) || (!nb && deadline)) {errno=EINVAL;return -1;}
    for(unsigned i=0;i<nm;i++) {
        sources[count++]=m[i].source;
        if(m[i].alias)sources[count++]=m[i].alias;
    }
    for(unsigned i=0;i<nb;i++)returns[i]=b[i].scope.return_port;
    if(getsockname(fd,(struct sockaddr *)&address,&length))return -1;
    return address.ss_family==AF_PACKET?packet_filter(fd,sources,count,2,returns,nb):0;
}
int ffn_aggregate_control_receive(int fd,const struct ffn_aggregate_member *m,unsigned nm,
    const struct ffn_packet_fe100_binding *b,unsigned nb,uint64_t deadline,uint8_t *out,size_t capacity)
{
    uint8_t wire[MAX_FRAME+80];
    if(!out || capacity<18 || !control_bindings(m,nm,b,nb)) {errno=EINVAL;return -1;}
    for(unsigned attempt=0;attempt<64;attempt++) {
        struct sockaddr_ll from={0};socklen_t length=sizeof(from);
        ssize_t n=recvfrom(fd,wire,sizeof(wire),MSG_DONTWAIT|MSG_TRUNC,(void *)&from,&length);
        if(n<0) {if(errno==EINTR)continue;return -1;}
        if((size_t)n>sizeof(wire) || n<18 || from.sll_pkttype==PACKET_OUTGOING || be16(wire)!=24)continue;
        unsigned source=be16(wire+2);int member=0;
        for(unsigned i=0;i<nm;i++)if(source==m[i].source || (m[i].alias && source==m[i].alias)) {
            source=m[i].source;member=1;break;
        }
        if(member) {
            unsigned type=be16(wire+16);
            if(type!=0x8809 && type!=0x88cc)continue;
            if((size_t)n>capacity){errno=EMSGSIZE;return -1;}
            memcpy(out,wire,(size_t)n);out[2]=(uint8_t)(source>>8);out[3]=(uint8_t)source;
            return (int)n;
        }
        int64_t now=milliseconds();
        if(now<0 || (uint64_t)now>=deadline)continue;
        for(unsigned i=0;i<nb;i++) {
            struct ffn_fe100_punt packet;
            if(ffn_fe100_punt_decode(wire,(size_t)n,&b[i].scope,&packet)!=2)continue;
            if(packet.length+4>capacity){errno=EMSGSIZE;return -1;}
            out[0]=0;out[1]=24;out[2]=(uint8_t)(b[i].source>>8);out[3]=(uint8_t)b[i].source;
            memcpy(out+4,packet.frame,packet.length);return (int)packet.length+4;
        }
    }
    errno=EAGAIN;return -1;
}
