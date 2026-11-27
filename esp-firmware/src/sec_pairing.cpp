#include "sec_pairing.h"
#include <string.h>

#if defined(ESP32) || defined(ESP8266)
  #include <Arduino.h>
  #include <Preferences.h>
#endif

static unsigned long now_ms(void) {
#if defined(ESP32) || defined(ESP8266)
  return millis();
#else
  return 0;
#endif
}

static unsigned long compute_backoff_ms(uint8_t fail_count) {
  // delay = min(2s * 2^fail_count, 10 min = 600,000 ms)
  uint8_t shift = (fail_count > 10) ? 10 : fail_count;
  unsigned long delay_ms = 2000UL << shift;
  return (delay_ms > 600000UL) ? 600000UL : delay_ms;
}

void sec_pairing_load(SecPairingState* st) {
  memset(st, 0, sizeof(*st));
  st->crypto_mode = SEC_MODE_V1;
  st->key_epoch = 1;

#if defined(ESP32) || defined(ESP8266)
  Preferences p;
  if (!p.begin("sec", true)) return;

  st->crypto_mode = p.getUChar("mode", SEC_MODE_V1);
  st->key_epoch = p.getUShort("epoch", 1);
  if (p.getBytesLength("k_own") == 16) {
    p.getBytes("k_own", st->k_own, 16);
  }
  if (p.getBytesLength("k_ack") == 16) {
    p.getBytes("k_ack", st->k_ack, 16);
  }
  String pinStr = p.getString("pin", "");
  if (pinStr.length() == 6) {
    bool all_digits = true;
    for (int i = 0; i < 6; i++) {
      if (pinStr[i] < '0' || pinStr[i] > '9') { all_digits = false; break; }
    }
    if (all_digits) {
      memcpy(st->pin, pinStr.c_str(), 6);
      st->pin[6] = '\0';
      st->has_pin = true;
    }
  }
  p.end();
#endif
}

bool sec_pairing_save(const SecPairingState* st) {
#if defined(ESP32) || defined(ESP8266)
  Preferences p;
  if (!p.begin("sec", false)) return false;
  p.putUChar("mode", st->crypto_mode);
  p.putUShort("epoch", st->key_epoch);
  p.putBytes("k_own", st->k_own, 16);
  p.putBytes("k_ack", st->k_ack, 16);
  p.end();
  return true;
#else
  (void)st;
  return true;
#endif
}

void sec_pairing_clear_keys(SecPairingState* st) {
  st->crypto_mode = SEC_MODE_V1;
  st->key_epoch = 1;
  memset(st->k_own, 0, 16);
  memset(st->k_ack, 0, 16);
  st->handshake_active = false;
#if defined(ESP32) || defined(ESP8266)
  Preferences p;
  if (p.begin("sec", false)) {
    p.remove("mode");
    p.remove("epoch");
    p.remove("k_own");
    p.remove("k_ack");
    // NOTE: "pin" is intentionally preserved across factory resets / OTA!
    p.end();
  }
#endif
}

uint8_t sec_pairing_set_pin(SecPairingState* st, const char pin[6]) {
  if (st->has_pin) {
    return PIN_ACK_ALREADY_SET;
  }
  for (int i = 0; i < 6; i++) {
    if (pin[i] < '0' || pin[i] > '9') {
      return PIN_ACK_INVALID;
    }
  }
  memcpy(st->pin, pin, 6);
  st->pin[6] = '\0';
  st->has_pin = true;
  if (st->crypto_mode == SEC_MODE_V2_UNVERIFIED) {
    st->crypto_mode = SEC_MODE_V2;
  }
#if defined(ESP32) || defined(ESP8266)
  Preferences p;
  if (p.begin("sec", false)) {
    p.putString("pin", st->pin);
    p.putUChar("mode", st->crypto_mode);
    p.end();
  }
#endif
  return PIN_ACK_OK;
}

bool sec_pairing_allow_v1_confirm(const SecPairingState* st) {
  // Q1: Refuse v1 PAIR_CONFIRM if already v2-paired OR if PIN-provisioned
  if (st->crypto_mode != SEC_MODE_V1) return false;
  if (st->has_pin) return false;
  return true;
}

bool sec_pairing_fill_hello_tlv(
    SecPairingState* st,
    uint8_t payload_raw[185],
    size_t cap_off,
    uint16_t max_frame
) {
  if (cap_off + V2_HELLO_CAP_LEN > 185) return false;

  // Always advertise v2 capability + PIN flag so the backend never mistakes
  // a backing-off v2 device for a legacy v1 device.
  payload_raw[cap_off]     = PROTO_VERSION_V2;
  payload_raw[cap_off + 1] = (uint8_t)(max_frame & 0xFF);
  payload_raw[cap_off + 2] = (uint8_t)((max_frame >> 8) & 0xFF);
  payload_raw[cap_off + 3] = st->has_pin ? V2_HELLO_FLAG_HAS_PIN : 0x00;

  unsigned long t = now_ms();
  if (st->fail_count > 0 && (long)(t - st->backoff_until_ms) < 0) {
    memset(payload_raw + cap_off + 4, 0, 40);
    return false;  // Still in PIN exponential backoff window
  }

  sec_random_bytes(st->eph_priv, 32);
  sec_x25519_public_from_private(st->eph_pub, st->eph_priv);
  sec_random_bytes(st->nonce_d, 8);
  st->handshake_active = true;
  st->handshake_start_ms = t;

  memcpy(payload_raw + cap_off + 4, st->eph_pub, 32);
  memcpy(payload_raw + cap_off + 36, st->nonce_d, 8);
  return true;
}

static void record_handshake_fail(SecPairingState* st) {
  if (st->fail_count < 20) st->fail_count++;
  st->backoff_until_ms = now_ms() + compute_backoff_ms(st->fail_count);
  st->handshake_active = false;
}

bool sec_pairing_handle_confirm_v2(
    SecPairingState* st,
    const uint8_t my_mac[6],
    const uint8_t* frame, size_t frame_len,
    SecCounter* counter,
    uint32_t* out_home_id,
    uint8_t* ack_out, size_t ack_cap,
    size_t* ack_len_out
) {
  if (!st->handshake_active || frame_len != V2_PAIR_CONFIRM_LEN) {
    return false;
  }

  unsigned long t = now_ms();
  // Enforce pairing window BEFORE unwrapping/committing keys to NVS so a late
  // PKT_PAIR_CONFIRM cannot strand the device in a half-paired v2 state.
  if (st->handshake_start_ms != 0 && (unsigned long)(t - st->handshake_start_ms) > 30000UL) {
    st->handshake_active = false;
    return false;
  }
  if (st->fail_count > 0 && (long)(t - st->backoff_until_ms) < 0) {
    return false;
  }

  HueMixLinkHeaderV2 hdr;
  memcpy(&hdr, frame, V2_HEADER_SIZE);
  if (hdr.type != PKT_PAIR_CONFIRM || hdr.version != PROTO_VERSION_V2) {
    return false;
  }
  if (!sec_ct_equal(hdr.targetMAC, my_mac, 6)) {
    return false;
  }

  Payload_PairConfirmV2 body;
  memcpy(&body, frame + V2_HEADER_SIZE, sizeof(body));

  // 1. Compute X25519 shared secret
  uint8_t shared[32];
  if (!sec_x25519_shared_secret(shared, st->eph_priv, body.pub_B)) {
    record_handshake_fail(st);
    return false;
  }

  // 2. Derive K_session = HKDF-SHA256(shared || S, nonce_D || nonce_B, "HueMixLink-pair-v2" || MAC_D || MAC_B || epoch)
  uint8_t ikm[32 + 15];
  size_t ikm_len = 0;
  memcpy(ikm, shared, 32);
  ikm_len = 32;
  if (st->has_pin) {
    memcpy(ikm + 32, "HML2-PIN:", 9);
    memcpy(ikm + 41, st->pin, 6);
    ikm_len = 47;
  } else {
    memcpy(ikm + 32, "HML2-TOFU", 9);
    ikm_len = 41;
  }

  uint8_t salt[16];
  memcpy(salt, st->nonce_d, 8);
  memcpy(salt + 8, body.nonce_B, 8);

  uint8_t info[32];
  memcpy(info, "HueMixLink-pair-v2", 18);
  memcpy(info + 18, my_mac, 6);
  memcpy(info + 24, hdr.sourceMAC, 6);  // backend MAC (00:00:00:00:00:00)
  info[30] = (uint8_t)(body.key_epoch & 0xFF);
  info[31] = (uint8_t)((body.key_epoch >> 8) & 0xFF);

  uint8_t k_session[16];
  sec_hkdf_sha256(ikm, ikm_len, salt, 16, info, sizeof(info), k_session, 16);

  // 3. Build transcript = pub_D(32) || pub_B(32) || nonce_D(8) || nonce_B(8) || MAC_D(6) || MAC_B(6) || epoch(2) = 94 B
  uint8_t transcript[94];
  memcpy(transcript,      st->eph_pub, 32);
  memcpy(transcript + 32, body.pub_B, 32);
  memcpy(transcript + 64, st->nonce_d, 8);
  memcpy(transcript + 72, body.nonce_B, 8);
  memcpy(transcript + 80, my_mac, 6);
  memcpy(transcript + 86, hdr.sourceMAC, 6);
  transcript[92] = (uint8_t)(body.key_epoch & 0xFF);
  transcript[93] = (uint8_t)((body.key_epoch >> 8) & 0xFF);

  // 4. Verify proof_B = HMAC-SHA256(K_session, "backend-ok" || transcript)
  uint8_t proof_input[10 + 94];
  memcpy(proof_input, "backend-ok", 10);
  memcpy(proof_input + 10, transcript, 94);
  uint8_t expected_proof_b[32];
  sec_hmac_sha256(k_session, 16, proof_input, sizeof(proof_input), expected_proof_b);
  if (!sec_ct_equal(expected_proof_b, body.proof_B, 32)) {
    record_handshake_fail(st);
    return false;
  }

  // 5. Unwrap K_own(16) || K_ack(16) || epoch(2) using AES-GCM(K_session, nonce=0, aad=transcript)
  uint8_t zero_nonce[12];
  memset(zero_nonce, 0, sizeof(zero_nonce));
  uint8_t bundle[34];
  if (!sec_aes_gcm_decrypt(k_session, zero_nonce, transcript, sizeof(transcript), body.wrap, 34, body.wrap + 34, bundle)) {
    record_handshake_fail(st);
    return false;
  }

  // 6. Commit keys & clear backoff
  memcpy(st->k_own, bundle, 16);
  memcpy(st->k_ack, bundle + 16, 16);
  st->key_epoch = (uint16_t)bundle[32] | ((uint16_t)bundle[33] << 8);
  st->crypto_mode = st->has_pin ? SEC_MODE_V2 : SEC_MODE_V2_UNVERIFIED;
  st->fail_count = 0;
  st->backoff_until_ms = 0;
  st->handshake_active = false;
  sec_pairing_save(st);

  if (out_home_id != nullptr) {
    *out_home_id = body.newHomeID;
  }

  // 7. Build Flight 3: PKT_PAIR_ACK (0x06) encrypted under K_own containing proof_D (32 B)
  uint8_t proof_d_input[9 + 94];
  memcpy(proof_d_input, "device-ok", 9);
  memcpy(proof_d_input + 9, transcript, 94);
  uint8_t proof_d[32];
  sec_hmac_sha256(k_session, 16, proof_d_input, sizeof(proof_d_input), proof_d);

  uint32_t tx_ctr = 1;
  if (counter != nullptr) {
    if (!sec_counter_next(counter, &tx_ctr)) {
      return false;
    }
  }

  size_t ack_len = sec_encode_v2(
      PKT_PAIR_ACK,
      my_mac,
      hdr.sourceMAC,
      0,
      tx_ctr,
      st->key_epoch,
      proof_d,
      32,
      st->k_own,
      ack_out,
      ack_cap
  );
  if (ack_len == 0) {
    return false;
  }
  if (ack_len_out != nullptr) {
    *ack_len_out = ack_len;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Gateway peer table & PKT_KEY_DISTRIB (0x07)
// ---------------------------------------------------------------------------

void sec_gw_table_init(SecGwPeerTable* tbl) {
  memset(tbl, 0, sizeof(*tbl));
}

const SecGwPeerEntry* sec_gw_table_find(const SecGwPeerTable* tbl, const uint8_t mac[6]) {
  for (int i = 0; i < SEC_GW_MAX_PEERS; i++) {
    if (tbl->entries[i].in_use && memcmp(tbl->entries[i].mac, mac, 6) == 0) {
      return &tbl->entries[i];
    }
  }
  return nullptr;
}

bool sec_gw_handle_key_distrib(SecGwPeerTable* tbl, const uint8_t* plaintext, size_t pt_len) {
  // Plaintext (26 B): action(1) || target_mac(6) || mode(1) || max_frame(2 LE) || k_ack(16)
  if (pt_len < 26) return false;

  uint8_t action = plaintext[0];
  const uint8_t* mac = plaintext + 1;
  uint8_t mode = plaintext[7];
  uint16_t max_frame = (uint16_t)plaintext[8] | ((uint16_t)plaintext[9] << 8);
  const uint8_t* k_ack = plaintext + 10;

  if (action == 0) {
    // Remove entry
    for (int i = 0; i < SEC_GW_MAX_PEERS; i++) {
      if (tbl->entries[i].in_use && memcmp(tbl->entries[i].mac, mac, 6) == 0) {
        memset(&tbl->entries[i], 0, sizeof(SecGwPeerEntry));
        return true;
      }
    }
    return true;
  }

  // Upsert entry
  int free_slot = -1;
  for (int i = 0; i < SEC_GW_MAX_PEERS; i++) {
    if (tbl->entries[i].in_use && memcmp(tbl->entries[i].mac, mac, 6) == 0) {
      free_slot = i;
      break;
    }
    if (!tbl->entries[i].in_use && free_slot < 0) {
      free_slot = i;
    }
  }
  if (free_slot < 0) return false;

  memcpy(tbl->entries[free_slot].mac, mac, 6);
  tbl->entries[free_slot].crypto_mode = mode;
  tbl->entries[free_slot].max_frame = max_frame;
  memcpy(tbl->entries[free_slot].k_ack, k_ack, 16);
  tbl->entries[free_slot].in_use = true;
  return true;
}
