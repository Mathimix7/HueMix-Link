#include "sec.h"
#include <string.h>

#if defined(ESP32)
  #include <esp_system.h>
  #include <esp_random.h>
  #include "mbedtls/gcm.h"
  #include "mbedtls/sha256.h"
#elif defined(ESP8266)
  #include <Arduino.h>
  #include <bearssl/bearssl_aead.h>
  #include <bearssl/bearssl_block.h>
  #include <bearssl/bearssl_hash.h>
  extern "C" {
    #include "osapi.h"
  }
#endif

// ---------------------------------------------------------------------------
// Constant-time compare & CRC32
// ---------------------------------------------------------------------------

bool sec_ct_equal(const uint8_t* a, const uint8_t* b, size_t len) {
  uint8_t diff = 0;
  for (size_t i = 0; i < len; i++) {
    diff |= (uint8_t)(a[i] ^ b[i]);
  }
  return diff == 0;
}

uint32_t sec_crc32(const uint8_t* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) {
      uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320UL & mask);
    }
  }
  return ~crc;
}

// ---------------------------------------------------------------------------
// RNG
// ---------------------------------------------------------------------------

uint32_t sec_random_u32(void) {
#if defined(ESP32)
  return esp_random();
#elif defined(ESP8266)
  return os_random();
#else
  return 0x12345678UL;
#endif
}

void sec_random_bytes(uint8_t* out, size_t len) {
  size_t i = 0;
  while (i < len) {
    uint32_t r = sec_random_u32();
    size_t n = (len - i < 4) ? (len - i) : 4;
    memcpy(out + i, &r, n);
    i += n;
  }
}

// ---------------------------------------------------------------------------
// SHA-256, HMAC-SHA256, HKDF-SHA256
// ---------------------------------------------------------------------------

void sec_sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
#if defined(ESP32)
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  if (len > 0) mbedtls_sha256_update(&ctx, data, len);
  mbedtls_sha256_finish(&ctx, out);
  mbedtls_sha256_free(&ctx);
#elif defined(ESP8266)
  br_sha256_context ctx;
  br_sha256_init(&ctx);
  if (len > 0) br_sha256_update(&ctx, data, len);
  br_sha256_out(&ctx, out);
#endif
}

static void sha256_two(const uint8_t* a, size_t a_len, const uint8_t* b, size_t b_len, uint8_t out[32]) {
#if defined(ESP32)
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  if (a_len > 0) mbedtls_sha256_update(&ctx, a, a_len);
  if (b_len > 0) mbedtls_sha256_update(&ctx, b, b_len);
  mbedtls_sha256_finish(&ctx, out);
  mbedtls_sha256_free(&ctx);
#elif defined(ESP8266)
  br_sha256_context ctx;
  br_sha256_init(&ctx);
  if (a_len > 0) br_sha256_update(&ctx, a, a_len);
  if (b_len > 0) br_sha256_update(&ctx, b, b_len);
  br_sha256_out(&ctx, out);
#endif
}

void sec_hmac_sha256(
    const uint8_t* key, size_t key_len,
    const uint8_t* data, size_t data_len,
    uint8_t out[32]
) {
  uint8_t k_block[64];
  memset(k_block, 0, sizeof(k_block));
  if (key_len > 64) {
    sec_sha256(key, key_len, k_block);
  } else if (key_len > 0) {
    memcpy(k_block, key, key_len);
  }

  uint8_t ipad[64];
  uint8_t opad[64];
  for (size_t i = 0; i < 64; i++) {
    ipad[i] = k_block[i] ^ 0x36;
    opad[i] = k_block[i] ^ 0x5C;
  }

  uint8_t inner[32];
  sha256_two(ipad, 64, data, data_len, inner);
  sha256_two(opad, 64, inner, 32, out);
}

void sec_hkdf_sha256(
    const uint8_t* ikm, size_t ikm_len,
    const uint8_t* salt, size_t salt_len,
    const uint8_t* info, size_t info_len,
    uint8_t* out, size_t out_len
) {
  // RFC 5869 Extract
  uint8_t zeros[32];
  memset(zeros, 0, sizeof(zeros));
  const uint8_t* actual_salt = (salt != nullptr && salt_len > 0) ? salt : zeros;
  size_t actual_salt_len = (salt != nullptr && salt_len > 0) ? salt_len : 32;

  uint8_t prk[32];
  sec_hmac_sha256(actual_salt, actual_salt_len, ikm, ikm_len, prk);

  // RFC 5869 Expand (supports up to 255 * 32 bytes; HueMix uses 16-32 B)
  uint8_t prev[32];
  size_t prev_len = 0;
  size_t written = 0;
  uint8_t counter = 1;

  // Stack buffer for T(i-1) || info || counter (max info in HueMix is < 128 B)
  uint8_t buf[32 + 160 + 1];
  while (written < out_len) {
    size_t pos = 0;
    if (prev_len > 0) {
      memcpy(buf + pos, prev, prev_len);
      pos += prev_len;
    }
    if (info != nullptr && info_len > 0) {
      size_t copy_info = (info_len <= 160) ? info_len : 160;
      memcpy(buf + pos, info, copy_info);
      pos += copy_info;
    }
    buf[pos++] = counter++;

    uint8_t t[32];
    sec_hmac_sha256(prk, 32, buf, pos, t);
    memcpy(prev, t, 32);
    prev_len = 32;

    size_t take = (out_len - written < 32) ? (out_len - written) : 32;
    memcpy(out + written, t, take);
    written += take;
  }
}

// ---------------------------------------------------------------------------
// AES-128-GCM
// ---------------------------------------------------------------------------

bool sec_aes_gcm_encrypt(
    const uint8_t key[16],
    const uint8_t nonce[12],
    const uint8_t* aad, size_t aad_len,
    const uint8_t* pt, size_t pt_len,
    uint8_t* ct_out,
    uint8_t tag_out[16]
) {
#if defined(ESP32)
  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  int ret = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, key, 128);
  if (ret == 0) {
    ret = mbedtls_gcm_crypt_and_tag(
        &gcm, MBEDTLS_GCM_ENCRYPT,
        pt_len, nonce, 12,
        aad, aad_len,
        pt, ct_out,
        16, tag_out);
  }
  mbedtls_gcm_free(&gcm);
  return ret == 0;
#elif defined(ESP8266)
  br_aes_ct_ctr_keys kc;
  br_gcm_context gcm;
  br_aes_ct_ctr_init(&kc, key, 16);
  br_gcm_init(&gcm, &kc.vtable, br_ghash_ctmul32);
  br_gcm_reset(&gcm, nonce, 12);
  if (aad != nullptr && aad_len > 0) {
    br_gcm_aad_inject(&gcm, aad, aad_len);
  }
  br_gcm_flip(&gcm);
  if (pt_len > 0) {
    if (ct_out != pt) memcpy(ct_out, pt, pt_len);
    br_gcm_run(&gcm, 1, ct_out, pt_len);
  }
  br_gcm_get_tag(&gcm, tag_out);
  return true;
#else
  (void)key; (void)nonce; (void)aad; (void)aad_len; (void)pt; (void)pt_len; (void)ct_out; (void)tag_out;
  return false;
#endif
}

bool sec_aes_gcm_decrypt(
    const uint8_t key[16],
    const uint8_t nonce[12],
    const uint8_t* aad, size_t aad_len,
    const uint8_t* ct, size_t ct_len,
    const uint8_t tag[16],
    uint8_t* pt_out
) {
#if defined(ESP32)
  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  int ret = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, key, 128);
  if (ret == 0) {
    ret = mbedtls_gcm_auth_decrypt(
        &gcm, ct_len, nonce, 12,
        aad, aad_len,
        tag, 16,
        ct, pt_out);
  }
  mbedtls_gcm_free(&gcm);
  return ret == 0;
#elif defined(ESP8266)
  br_aes_ct_ctr_keys kc;
  br_gcm_context gcm;
  br_aes_ct_ctr_init(&kc, key, 16);
  br_gcm_init(&gcm, &kc.vtable, br_ghash_ctmul32);
  br_gcm_reset(&gcm, nonce, 12);
  if (aad != nullptr && aad_len > 0) {
    br_gcm_aad_inject(&gcm, aad, aad_len);
  }
  br_gcm_flip(&gcm);
  if (ct_len > 0) {
    if (pt_out != ct) memcpy(pt_out, ct, ct_len);
    br_gcm_run(&gcm, 0, pt_out, ct_len);
  }
  if (br_gcm_check_tag(&gcm, tag) != 1) {
    if (ct_len > 0) memset(pt_out, 0, ct_len);
    return false;
  }
  return true;
#else
  (void)key; (void)nonce; (void)aad; (void)aad_len; (void)ct; (void)ct_len; (void)tag; (void)pt_out;
  return false;
#endif
}

// ---------------------------------------------------------------------------
// RFC 7748 X25519 (constant-time Montgomery ladder, identical on ESP32/ESP8266)
// ---------------------------------------------------------------------------

typedef int64_t gf[16];

static void fe_set(gf o, int64_t v) {
  for (int i = 0; i < 16; i++) o[i] = 0;
  o[0] = v;
}

static void fe_carry(gf o) {
  for (int i = 0; i < 16; i++) {
    o[i] += (1LL << 16);
    int64_t c = o[i] >> 16;
    o[(i + 1) * (i < 15)] += c - 1 + 37 * (c - 1) * (i == 15);
    o[i] -= c << 16;
  }
}

static void fe_cswap(gf p, gf q, int b) {
  int64_t c = ~(int64_t)(b - 1);
  for (int i = 0; i < 16; i++) {
    int64_t t = c & (p[i] ^ q[i]);
    p[i] ^= t;
    q[i] ^= t;
  }
}

static void fe_unpack(gf o, const uint8_t n[32]) {
  for (int i = 0; i < 16; i++) {
    o[i] = (int64_t)n[2 * i] + ((int64_t)n[2 * i + 1] << 8);
  }
  o[15] &= 0x7FFF;
}

static void fe_pack(uint8_t o[32], const gf n) {
  gf t, m;
  for (int i = 0; i < 16; i++) t[i] = n[i];
  fe_carry(t);
  fe_carry(t);
  fe_carry(t);
  for (int j = 0; j < 2; j++) {
    m[0] = t[0] - 0xFFED;
    for (int i = 1; i < 15; i++) {
      m[i] = t[i] - 0xFFFF - ((m[i - 1] >> 16) & 1);
      m[i - 1] &= 0xFFFF;
    }
    m[15] = t[15] - 0x7FFF - ((m[14] >> 16) & 1);
    int b = (int)((m[15] >> 16) & 1);
    m[14] &= 0xFFFF;
    fe_cswap(t, m, 1 - b);
  }
  for (int i = 0; i < 16; i++) {
    o[2 * i] = (uint8_t)(t[i] & 0xFF);
    o[2 * i + 1] = (uint8_t)(t[i] >> 8);
  }
}

static void fe_add(gf o, const gf a, const gf b) {
  for (int i = 0; i < 16; i++) o[i] = a[i] + b[i];
}

static void fe_sub(gf o, const gf a, const gf b) {
  for (int i = 0; i < 16; i++) o[i] = a[i] - b[i];
}

static void fe_mul(gf o, const gf a, const gf b) {
  int64_t t[31];
  for (int i = 0; i < 31; i++) t[i] = 0;
  for (int i = 0; i < 16; i++) {
    for (int j = 0; j < 16; j++) {
      t[i + j] += a[i] * b[j];
    }
  }
  for (int i = 0; i < 15; i++) t[i] += 38 * t[i + 16];
  for (int i = 0; i < 16; i++) o[i] = t[i];
  fe_carry(o);
  fe_carry(o);
}

static void fe_sq(gf o, const gf a) {
  fe_mul(o, a, a);
}

static void fe_inv(gf o, const gf i) {
  gf c;
  for (int a = 0; a < 16; a++) c[a] = i[a];
  for (int a = 253; a >= 0; a--) {
    fe_sq(c, c);
    if (a != 2 && a != 4) fe_mul(c, c, i);
  }
  for (int a = 0; a < 16; a++) o[a] = c[a];
}

static void x25519_scalarmult(uint8_t q[32], const uint8_t n[32], const uint8_t p[32]) {
  uint8_t z[32];
  memcpy(z, n, 32);
  z[31] = (uint8_t)((z[31] & 127) | 64);
  z[0] &= 248;

  gf x, a, b, c, d, e, f;
  static const gf _121665 = {0xDB41, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};

  fe_unpack(x, p);
  for (int i = 0; i < 16; i++) {
    b[i] = x[i];
    d[i] = a[i] = c[i] = 0;
  }
  a[0] = d[0] = 1;

  for (int i = 254; i >= 0; --i) {
    int r = (z[i >> 3] >> (i & 7)) & 1;
    fe_cswap(a, b, r);
    fe_cswap(c, d, r);
    fe_add(e, a, c);
    fe_sub(a, a, c);
    fe_add(c, b, d);
    fe_sub(b, b, d);
    fe_sq(d, e);
    fe_sq(f, a);
    fe_mul(a, c, a);
    fe_mul(c, b, e);
    fe_add(e, a, c);
    fe_sub(a, a, c);
    fe_sq(b, a);
    fe_sub(c, d, f);
    fe_mul(a, c, _121665);
    fe_add(a, a, d);
    fe_mul(c, c, a);
    fe_mul(a, d, f);
    fe_mul(d, b, x);
    fe_sq(b, e);
    fe_cswap(a, b, r);
    fe_cswap(c, d, r);
  }
  fe_inv(c, c);
  fe_mul(a, a, c);
  fe_pack(q, a);
}

void sec_x25519_public_from_private(uint8_t pub_out[32], const uint8_t priv[32]) {
  static const uint8_t basepoint[32] = {9};
  x25519_scalarmult(pub_out, priv, basepoint);
}

bool sec_x25519_shared_secret(uint8_t shared_out[32], const uint8_t priv[32], const uint8_t peer_pub[32]) {
  x25519_scalarmult(shared_out, priv, peer_pub);
  uint8_t acc = 0;
  for (int i = 0; i < 32; i++) acc |= shared_out[i];
  return acc != 0;  // Reject low-order / all-zero shared secret (RFC 7748 §6.1)
}
