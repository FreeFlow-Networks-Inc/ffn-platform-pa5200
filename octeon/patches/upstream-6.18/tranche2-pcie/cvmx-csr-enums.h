/*
 * FFN: minimal extract of the SDK cvmx-csr-enums.h.
 *
 * cvmx-mio-defs.h uses cvmx_uart_iid_t and cvmx_uart_bits_t in its UART
 * register bitfields, and upstream has no cvmx-csr-enums.h at all. Copying the
 * SDK file wholesale does not work: it also defines cvmx_ipd_mode_t,
 * cvmx_pip_port_cfg_mode_t and cvmx_pow_tag_type_t, which upstream already
 * defines in cvmx-ipd.h, cvmx-pip-defs.h and cvmx-pow.h -- 15 redeclaration
 * errors. Carry only what is actually missing.
 */
#ifndef __CVMX_CSR_ENUMS_H__
#define __CVMX_CSR_ENUMS_H__

typedef enum {
	CVMX_UART_BITS5 = 0,
	CVMX_UART_BITS6 = 1,
	CVMX_UART_BITS7 = 2,
	CVMX_UART_BITS8 = 3
} cvmx_uart_bits_t;

typedef enum {
	CVMX_UART_IID_NONE = 1,
	CVMX_UART_IID_RX_ERROR = 6,
	CVMX_UART_IID_RX_DATA = 4,
	CVMX_UART_IID_RX_TIMEOUT = 12,
	CVMX_UART_IID_TX_EMPTY = 2,
	CVMX_UART_IID_MODEM = 0,
	CVMX_UART_IID_BUSY = 7
} cvmx_uart_iid_t;

#endif /* __CVMX_CSR_ENUMS_H__ */
