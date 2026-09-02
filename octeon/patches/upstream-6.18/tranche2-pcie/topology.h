/*
 * FFN: OCTEON needs this so that arch/mips/pci/pcie-octeon.c can DEFINE
 * pcibus_to_node() as a function.
 *
 * asm-generic/topology.h has:
 *     #ifndef pcibus_to_node
 *     #define pcibus_to_node(bus) ((void)(bus), -1)
 *     #endif
 *
 * Without the self-define below, that macro is active and then expands inside
 * the function definition in pcie-octeon.c, which fails to parse -- and the
 * error is reported against asm-generic/topology.h, several headers away from
 * the actual cause. The SDK carries this header for the same reason.
 *
 * The SDK's CONFIG_NUMA block is deliberately omitted: it references
 * __node_data and cpu_logical_map in a shape that may not match upstream, and
 * NUMA is off on this platform. Absent it, a NUMA build gets upstream's
 * generic behaviour rather than something subtly wrong.
 */
#ifndef _ASM_MACH_CAVIUM_OCTEON_TOPOLOGY_H
#define _ASM_MACH_CAVIUM_OCTEON_TOPOLOGY_H

struct pci_bus;
int pcibus_to_node(struct pci_bus *bus);
#define pcibus_to_node pcibus_to_node

#include <asm-generic/topology.h>

#endif /* _ASM_MACH_CAVIUM_OCTEON_TOPOLOGY_H */
