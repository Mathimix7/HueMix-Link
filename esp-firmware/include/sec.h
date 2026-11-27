#ifndef HUEMIXLINK_SEC_H
#define HUEMIXLINK_SEC_H

#include <stddef.h>
#include <stdint.h>
#include "HueMixLink.h"

// ===== Crypto Primitives (ESP32 mbedTLS / ESP8266 BearSSL + RFC 7748 X25519) =====

bool sec_aes_gcm_encrypt(
    const uint8_t key[16],
    const uint8_t nonce[12],
    const uint8_t* aad, size_t aad_len,
    const uint8_t* pt, size_t pt_len,
    uint8_t* ct_out,
    uint8_t tag_out[16]
);

bool sec_aes_gcm_decrypt(
    const uint8_t key[16],
    const uint8_t nonce[12],
    const uint8_t* aad, size_t aad_len,
    const uint8_t* ct, size_t ct_len,
    const uint8_t tag[16],
    uint8_t* pt_out
);

void sec_sha256(const uint8_t* data, size_t len, uint8_t out[32]);

void sec_hmac_sha256(
    const uint8_t* key, size_t key_len,
    const uint8_t* data, size_t data_len,
    uint8_t out[32]
);

void sec_hkdf_sha256(
    const uint8_t* ikm, size_t ikm_len,
    const uint8_t* salt, size_t salt_len,
    const uint8_t* info, size_t info_len,
    uint8_t* out, size_t out_len
);

// RFC 7748 X25519 (32-byte little-endian keys)
void sec_x25519_public_from_private(uint8_t pub_out[32], const uint8_t priv[32]);
bool sec_x25519_shared_secret(uint8_t shared_out[32], const uint8_t priv[32], const uint8_t peer_pub[32]);

void sec_random_bytes(uint8_t* out, size_t len);
uint32_t sec_random_u32(void);
bool sec_ct_equal(const uint8_t* a, const uint8_t* b, size_t len);
uint32_t sec_crc32(const uint8_t* data, size_t len);

// ===== Security v2 Wire Codec =====

void sec_build_nonce(const uint8_t src_mac[6], uint32_t counter, uint16_t key_epoch, uint8_t nonce_out[12]);

// Encodes and encrypts one v2 frame. Enforces N >= 1 and never emits 203 bytes.
// Returns total frame length (38 + N_wire), or 0 on error.
size_t sec_encode_v2(
    uint8_t type,
    const uint8_t src_mac[6],
    const uint8_t dst_mac[6],
    uint8_t msg_id,
    uint32_t counter,
    uint16_t key_epoch,
    const uint8_t* plaintext, size_t pt_len,
    const uint8_t key[16],
    uint8_t* out_buf, size_t out_cap
);

// Decodes and authenticates one v2 frame. Returns true iff tag verifies.
bool sec_decode_v2(
    const uint8_t* frame, size_t frame_len,
    const uint8_t key[16],
    HueMixLinkHeaderV2* hdr_out,
    uint8_t* pt_out, size_t pt_cap,
    size_t* pt_len_out
);

// ===== TX Counter & Nonce Persistence (§7.1) =====

#define SEC_RTC_MAGIC 0x484D4C32UL  // "HML2"

typedef struct {
  uint32_t magic;
  uint32_t counter;
  uint32_t persist_fail;
  uint32_t crc;
} SecRtcBlock;

typedef struct {
  uint32_t (*load_reserved)(void* ctx);
  bool     (*save_and_verify_reserved)(void* ctx, uint32_t value);
  bool     (*load_rtc)(void* ctx, SecRtcBlock* out);
  void     (*save_rtc)(void* ctx, const SecRtcBlock* blk);
  bool     (*is_deep_sleep_wake)(void* ctx);
  uint32_t (*rand_u32)(void* ctx);
  void     (*on_panic)(void* ctx);
} SecCounterOps;

typedef struct {
  uint32_t counter;
  uint32_t reserved;
  uint32_t persist_fail;
  uint32_t reserve_step;  // Default V2_COUNTER_RESERVE (100)
  bool     panicked;
  bool     initialized;
  SecCounterOps ops;
  void*    ctx;
} SecCounter;

void sec_counter_init_with_ops(SecCounter* sc, const SecCounterOps* ops, void* ctx, uint32_t reserve_step);
void sec_counter_init_default(SecCounter* sc);

// Advances counter by 1, persisting a new reserve-ahead block when needed.
// Returns false if panicked (caller MUST NOT transmit).
bool sec_counter_next(SecCounter* sc, uint32_t* out_counter);

// Saves current counter + persist_fail into CRC-protected RTC block before deep sleep.
void sec_counter_prepare_sleep(SecCounter* sc);

#endif  // HUEMIXLINK_SEC_H
