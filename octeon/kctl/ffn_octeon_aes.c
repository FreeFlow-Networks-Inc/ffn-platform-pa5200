// SPDX-License-Identifier: GPL-2.0-only
/* Linux Crypto API bridge to the owner's OCTEON SDK COP2 AES instructions.
 * Build generates ffn_sdk_crypto_ops.h from local cvmx-asm.h; no SDK copy is
 * stored here. The kernel's exported COP2 save/restore helpers protect task
 * state and preemption. Does not take ownership of PKI/SSO/PKO or DMA engines.
 */
#define CVMX_ENABLE_CSR_ADDRESS_CHECKING 0
#include <linux/module.h>
#include <linux/crypto.h>
#include <crypto/internal/cipher.h>
#include <linux/string.h>
#include <asm/processor.h>
#include <asm/octeon/crypto.h>
#include <asm/octeon/octeon.h>
#include "ffn_sdk_crypto_ops.h"

struct ffn_aes_ctx { u64 key[4]; unsigned int length; };

static void ffn_block(const struct ffn_aes_ctx *ctx, u8 *dst, const u8 *src, bool decrypt)
{
    struct octeon_cop2_state saved;
    unsigned long flags;
    u64 in[2], out[2];
    memcpy(in, src, sizeof(in));
    flags = octeon_crypto_enable(&saved);
    CVMX_MT_AES_KEY(ctx->key[0], 0);
    CVMX_MT_AES_KEY(ctx->key[1], 1);
    CVMX_MT_AES_KEY(ctx->key[2], 2);
    CVMX_MT_AES_KEY(ctx->key[3], 3);
    CVMX_MT_AES_KEYLENGTH(ctx->length / 8 - 1);
    if (decrypt) {
        CVMX_MT_AES_DEC0(in[0]);
        CVMX_MT_AES_DEC1(in[1]);
    } else {
        CVMX_MT_AES_ENC0(in[0]);
        CVMX_MT_AES_ENC1(in[1]);
    }
    CVMX_MF_AES_RESULT(out[0], 0);
    CVMX_MF_AES_RESULT(out[1], 1);
    octeon_crypto_disable(&saved, flags);
    memcpy(dst, out, sizeof(out));
    memzero_explicit(&saved, sizeof(saved));
    memzero_explicit(in, sizeof(in));
    memzero_explicit(out, sizeof(out));
}

static int ffn_setkey(struct crypto_tfm *tfm, const u8 *key, unsigned int length)
{
    struct ffn_aes_ctx *ctx = crypto_tfm_ctx(tfm);
    if (length != 16 && length != 24 && length != 32) return -EINVAL;
    memset(ctx, 0, sizeof(*ctx));
    memcpy(ctx->key, key, length);
    ctx->length = length;
    return 0;
}

static void ffn_encrypt(struct crypto_tfm *tfm, u8 *dst, const u8 *src)
{ ffn_block(crypto_tfm_ctx(tfm), dst, src, false); }
static void ffn_decrypt(struct crypto_tfm *tfm, u8 *dst, const u8 *src)
{ ffn_block(crypto_tfm_ctx(tfm), dst, src, true); }

static struct crypto_alg ffn_alg = {
    .cra_name = "aes",
    .cra_driver_name = "aes-ffn-octeon",
    .cra_priority = 300,
    .cra_flags = CRYPTO_ALG_TYPE_CIPHER,
    .cra_blocksize = 16,
    .cra_ctxsize = sizeof(struct ffn_aes_ctx),
    .cra_module = THIS_MODULE,
    .cra_cipher = { .cia_min_keysize = 16, .cia_max_keysize = 32,
                    .cia_setkey = ffn_setkey, .cia_encrypt = ffn_encrypt,
                    .cia_decrypt = ffn_decrypt },
};

static int __init ffn_init(void)
{
    static const u8 expected[3][16] = {
        {0x69,0xc4,0xe0,0xd8,0x6a,0x7b,0x04,0x30,0xd8,0xcd,0xb7,0x80,0x70,0xb4,0xc5,0x5a},
        {0xdd,0xa9,0x7c,0xa4,0x86,0x4c,0xdf,0xe0,0x6e,0xaf,0x70,0xa0,0xec,0x0d,0x71,0x91},
        {0x8e,0xa2,0xb7,0xca,0x51,0x67,0x45,0xbf,0xea,0xfc,0x49,0x90,0x4b,0x49,0x60,0x89}
    };
    struct ffn_aes_ctx ctx;
    u8 key[32], plain[16], cipher[16], decoded[16];
    unsigned int i, n;
    if (!octeon_has_crypto()) return -ENODEV;
    for (i = 0; i < sizeof(key); i++) key[i] = i;
    for (i = 0; i < sizeof(plain); i++) plain[i] = i * 0x11;
    for (n = 0; n < 3; n++) {
        memset(&ctx, 0, sizeof(ctx));
        ctx.length = 16 + n * 8;
        memcpy(ctx.key, key, ctx.length);
        ffn_block(&ctx, cipher, plain, false);
        ffn_block(&ctx, decoded, cipher, true);
        if (memcmp(cipher, expected[n], 16) || memcmp(decoded, plain, 16)) {
            pr_err("ffn-octeon-aes: hardware known-answer test failed (%u bits)\n", ctx.length * 8);
            return -EINVAL;
        }
    }
    pr_info("ffn-octeon-aes: SDK COP2 AES-128/192/256 encrypt/decrypt self-tests passed\n");
    return crypto_register_alg(&ffn_alg);
}
static void __exit ffn_exit(void) { crypto_unregister_alg(&ffn_alg); }
module_init(ffn_init);
module_exit(ffn_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN OCTEON SDK COP2 AES Crypto API adapter");
MODULE_ALIAS_CRYPTO("aes-ffn-octeon");
