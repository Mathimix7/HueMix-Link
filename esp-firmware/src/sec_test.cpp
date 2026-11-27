/*
  HueMix-Link Security v2 — On-Device Crypto & Counter Self-Test Firmware
  Verifies ESP32 (mbedTLS) and ESP8266 (BearSSL) against Python golden vectors
  in python/tests/vectors/security_v2_vectors.json.
*/

#include <Arduino.h>
#include <string.h>
#include "HueMixLink.h"
#include "sec.h"

static void printHex(const char* label, const uint8_t* data, size_t len) {
  Serial.print(label);
  for (size_t i = 0; i < len; i++) {
    if (data[i] < 16) Serial.print('0');
    Serial.print(data[i], HEX);
  }
  Serial.println();
}

static bool hexToBytes(const char* hex, uint8_t* out, size_t out_len) {
  for (size_t i = 0; i < out_len; i++) {
    char hi = hex[2 * i];
    char lo = hex[2 * i + 1];
    auto nibble = [](char c) -> int {
      if (c >= '0' && c <= '9') return c - '0';
      if (c >= 'a' && c <= 'f') return c - 'a' + 10;
      if (c >= 'A' && c <= 'F') return c - 'A' + 10;
      return -1;
    };
    int h = nibble(hi), l = nibble(lo);
    if (h < 0 || l < 0) return false;
    out[i] = (uint8_t)((h << 4) | l);
  }
  return true;
}

// Simulated backing store for on-device 1000-cycle power-cut counter test
struct SimCtx {
  uint32_t flash_reserved;
  SecRtcBlock rtc;
  bool is_deepsleep;
  bool fail_next_write;
  uint32_t rng_state;
  bool panic_called;
};

static uint32_t sim_load_reserved(void* ctx) { return ((SimCtx*)ctx)->flash_reserved; }
static bool sim_save_reserved(void* ctx, uint32_t v) {
  SimCtx* s = (SimCtx*)ctx;
  if (s->fail_next_write) return false;
  s->flash_reserved = v;
  return true;
}
static bool sim_load_rtc(void* ctx, SecRtcBlock* out) { *out = ((SimCtx*)ctx)->rtc; return true; }
static void sim_save_rtc(void* ctx, const SecRtcBlock* b) { ((SimCtx*)ctx)->rtc = *b; }
static bool sim_is_deepsleep(void* ctx) { return ((SimCtx*)ctx)->is_deepsleep; }
static uint32_t sim_rand(void* ctx) {
  SimCtx* s = (SimCtx*)ctx;
  s->rng_state = s->rng_state * 1664525UL + 1013904223UL;
  return s->rng_state;
}
static void sim_panic(void* ctx) { ((SimCtx*)ctx)->panic_called = true; }

static void runAllTests() {
  int passed = 0;
  int failed = 0;

  auto check = [&](const char* name, bool ok) {
    Serial.printf("[%s] %s\n", ok ? "PASS" : "FAIL", name);
    if (ok) passed++; else failed++;
  };

  Serial.println("\n=== HueMix-Link Security v2 On-Device Vector Suite ===");
#if defined(ESP32)
  Serial.println("Platform: ESP32 (mbedTLS)");
#elif defined(ESP8266)
  Serial.println("Platform: ESP8266 (BearSSL)");
#endif

  // 1. HKDF K_own & K_ack
  uint8_t hmk[16], exp_k_own[16], exp_k_ack[16], got[16];
  hexToBytes("000102030405060708090a0b0c0d0e0f", hmk, 16);
  hexToBytes("b9da743de67c3f0fd738bc9576877761", exp_k_own, 16);
  hexToBytes("9c52316eaaa9b04568c317ece5b6ee56", exp_k_ack, 16);

  const uint8_t dev_mac[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x11};
  const uint8_t backend_mac[6] = {0, 0, 0, 0, 0, 0};

  uint8_t info_own[14] = {'o','w','n','-','v','2', 0x01, 0x00, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x11};
  sec_hkdf_sha256(hmk, 16, nullptr, 0, info_own, sizeof(info_own), got, 16);
  check("HKDF K_own matches Python golden vector", sec_ct_equal(got, exp_k_own, 16));

  uint8_t info_ack[14] = {'a','c','k','-','v','2', 0x01, 0x00, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x11};
  sec_hkdf_sha256(hmk, 16, nullptr, 0, info_ack, sizeof(info_ack), got, 16);
  check("HKDF K_ack matches Python golden vector", sec_ct_equal(got, exp_k_ack, 16));

  // 2. RFC 7748 X25519
  uint8_t a_priv[32], a_pub[32], exp_a_pub[32], b_pub[32], shared[32], exp_shared[32];
  hexToBytes("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a", a_priv, 32);
  hexToBytes("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a", exp_a_pub, 32);
  hexToBytes("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f", b_pub, 32);
  hexToBytes("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742", exp_shared, 32);

  sec_x25519_public_from_private(a_pub, a_priv);
  check("X25519 public key matches RFC 7748", sec_ct_equal(a_pub, exp_a_pub, 32));

  bool dh_ok = sec_x25519_shared_secret(shared, a_priv, b_pub);
  check("X25519 shared secret matches RFC 7748", dh_ok && sec_ct_equal(shared, exp_shared, 32));

  // 3. Pairing K_session
  uint8_t ikm_pair[32 + 15];
  memcpy(ikm_pair, shared, 32);
  memcpy(ikm_pair + 32, "HML2-PIN:482913", 15);
  uint8_t salt_pair[16];
  hexToBytes("112233445566778899aabbccddeeff00", salt_pair, 16);
  // Build exact info = "HueMixLink-pair-v2" (18 B) || MAC_D (6 B) || MAC_B (6 B) || epoch (2 B) = 32 B
  uint8_t info_pair_exact[32];
  memcpy(info_pair_exact, "HueMixLink-pair-v2", 18);
  memcpy(info_pair_exact + 18, dev_mac, 6);
  memcpy(info_pair_exact + 24, backend_mac, 6);
  info_pair_exact[30] = 0x01;
  info_pair_exact[31] = 0x00;

  uint8_t exp_k_sess[16];
  hexToBytes("f1cf30507e7927278977b6d8151f203a", exp_k_sess, 16);
  sec_hkdf_sha256(ikm_pair, sizeof(ikm_pair), salt_pair, sizeof(salt_pair), info_pair_exact, sizeof(info_pair_exact), got, 16);
  check("Pairing K_session matches Python golden vector", sec_ct_equal(got, exp_k_sess, 16));

  // 4. Full v2 packet encode/decode vs Python wire bytes
  uint8_t pt[9], exp_wire[47], wire[256];
  hexToBytes("016e0fff0400000000", pt, 9);
  hexToBytes("1102aabbccddee11000000000000072a00000001000012d7c57cb4af2fe7f8046e57cbc1d7c68b76b90761edf39b7e", exp_wire, 47);

  size_t wlen = sec_encode_v2(PKT_BTN_EVENT, dev_mac, backend_mac, 7, 42, 1, pt, 9, exp_k_own, wire, sizeof(wire));
  check("v2 encode matches Python golden wire frame byte-for-byte", wlen == 47 && sec_ct_equal(wire, exp_wire, 47));
  if (wlen != 47 || !sec_ct_equal(wire, exp_wire, 47)) {
    printHex("  got: ", wire, wlen);
    printHex("  exp: ", exp_wire, 47);
  }

  // 5. RSSI byte-21 mutation tolerated, authenticated byte tamper rejected
  wire[21] = (uint8_t)(-55);
  HueMixLinkHeaderV2 hdr;
  uint8_t dec_pt[64];
  size_t dec_len = 0;
  bool dec_ok = sec_decode_v2(wire, wlen, exp_k_own, &hdr, dec_pt, sizeof(dec_pt), &dec_len);
  check("v2 decode tolerates gateway RSSI mutation (offset 21)", dec_ok && dec_len == 9 && sec_ct_equal(dec_pt, pt, 9));

  wire[15] ^= 0x01;  // flip counter bit
  bool tamper_ok = !sec_decode_v2(wire, wlen, exp_k_own, &hdr, dec_pt, sizeof(dec_pt), &dec_len);
  check("v2 decode rejects single-bit header tamper", tamper_ok);

  // 6. Never emits 203-byte frame (N=165 -> 204 B)
  uint8_t pt165[165];
  memset(pt165, 0xAB, sizeof(pt165));
  size_t len165 = sec_encode_v2(PKT_BTN_EVENT, dev_mac, backend_mac, 1, 5, 1, pt165, 165, exp_k_own, wire, sizeof(wire));
  check("v2 encode avoids forbidden 203-byte length (emits 204 B)", len165 == 204);

  // 7. Counter 1000-cycle randomized reset/power-cut monotonicity + panic test
  SimCtx sim;
  memset(&sim, 0, sizeof(sim));
  sim.rng_state = 0xDEADBEEFUL;
  SecCounterOps ops = {sim_load_reserved, sim_save_reserved, sim_load_rtc, sim_save_rtc, sim_is_deepsleep, sim_rand, sim_panic};
  SecCounter sc;
  sec_counter_init_with_ops(&sc, &ops, &sim, 100);

  uint32_t max_seen = 0;
  bool monotonic = true;
  for (int cycle = 0; cycle < 1000 && monotonic; cycle++) {
    int tx_count = (int)(sim_rand(&sim) % 25) + 1;
    for (int t = 0; t < tx_count; t++) {
      uint32_t c = 0;
      if (!sec_counter_next(&sc, &c) || c <= max_seen) {
        monotonic = false;
        break;
      }
      max_seen = c;
    }
    uint32_t mode = sim_rand(&sim) % 3;
    if (mode == 0) {
      // Clean deep sleep wake
      sec_counter_prepare_sleep(&sc);
      sim.is_deepsleep = true;
    } else if (mode == 1) {
      // Corrupted RTC after brownout
      sec_counter_prepare_sleep(&sc);
      sim.rtc.counter ^= 0xFF;
      sim.is_deepsleep = true;
    } else {
      // Cold boot / battery pull
      sim.is_deepsleep = false;
    }
    sec_counter_init_with_ops(&sc, &ops, &sim, 100);
  }
  check("Counter monotonicity across 1000 simulated resets/power-cuts", monotonic);

  // Panic after 3 consecutive write failures
  sim.fail_next_write = true;
  bool halted = false;
  for (int i = 0; i < 400; i++) {
    uint32_t c = 0;
    bool ok = sec_counter_next(&sc, &c);
    if (sc.panicked && sim.panic_called && !ok) {
      halted = true;
      break;
    }
  }
  check("Counter enters SECURITY_PANIC after 3 failed flash writes", halted);

  Serial.printf("=== RESULT: %d passed, %d failed ===\n", passed, failed);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  runAllTests();
}

void loop() {
  if (Serial.available()) {
    while (Serial.available()) Serial.read();
    runAllTests();
  }
  delay(50);
}
