#ifndef HUEMIXLINK_SEC_PAIRING_H
#define HUEMIXLINK_SEC_PAIRING_H

#include <stddef.h>
#include <stdint.h>
#include "HueMixLink.h"
#include "sec.h"

#define SEC_MODE_V1            0
#define SEC_MODE_V2_UNVERIFIED 1
#define SEC_MODE_V2            2

#define SEC_GW_MAX_PEERS       128

typedef struct {
  uint8_t  crypto_mode;      // SEC_MODE_V1 / V2_UNVERIFIED / V2
  uint16_t key_epoch;
  uint8_t  k_own[16];
  uint8_t  k_ack[16];
  bool     has_pin;
  char     pin[7];           // 6 ASCII digits + NUL

  // Active ephemeral ECDH handshake state (RAM only)
  bool     handshake_active;
  uint8_t  eph_priv[32];
  uint8_t  eph_pub[32];
  uint8_t  nonce_d[8];
  unsigned long handshake_start_ms;

  // Exponential backoff state (mandatory 6-digit PIN online-guessing mitigation)
  uint8_t  fail_count;
  unsigned long backoff_until_ms;
} SecPairingState;

typedef struct {
  uint8_t  mac[6];
  uint8_t  crypto_mode;
  uint16_t max_frame;
  uint8_t  k_ack[16];
  bool     in_use;
} SecGwPeerEntry;

typedef struct {
  SecGwPeerEntry entries[SEC_GW_MAX_PEERS];
} SecGwPeerTable;

// Load persisted v2 keys/PIN/mode from NVS namespace "sec".
void sec_pairing_load(SecPairingState* st);

// Persist v2 keys/mode/epoch to NVS namespace "sec".
bool sec_pairing_save(const SecPairingState* st);

// Clear v2 pairing keys (e.g. factory reset), preserving provisioned PIN!
void sec_pairing_clear_keys(SecPairingState* st);

// Write-once 6-digit PIN provisioning over an authenticated v2 channel.
// Returns PIN_ACK_OK, PIN_ACK_ALREADY_SET, or PIN_ACK_INVALID.
uint8_t sec_pairing_set_pin(SecPairingState* st, const char pin[6]);

// Populate capability TLV + ephemeral ECDH pub_D + nonce_D into a 185-byte v1 HELLO payload.
// Returns false if currently in PIN exponential backoff window.
bool sec_pairing_fill_hello_tlv(
    SecPairingState* st,
    uint8_t payload_raw[185],
    size_t cap_off,
    uint16_t max_frame
);

// Check whether a v1 PKT_PAIR_CONFIRM is allowed (Q1: false if already v2 or if has_pin).
bool sec_pairing_allow_v1_confirm(const SecPairingState* st);

// Handle incoming 151-byte v2 PKT_PAIR_CONFIRM.
// On success: verifies proof_B, unwraps K_own/K_ack/epoch, saves to NVS, builds 70-byte
// encrypted PKT_PAIR_ACK into ack_out, and returns true.
// On failure: increments fail_count and applies exponential backoff min(2s * 2^fail, 600s).
bool sec_pairing_handle_confirm_v2(
    SecPairingState* st,
    const uint8_t my_mac[6],
    const uint8_t* frame, size_t frame_len,
    SecCounter* counter,
    uint32_t* out_home_id,
    uint8_t* ack_out, size_t ack_cap,
    size_t* ack_len_out
);

// Gateway peer table & PKT_KEY_DISTRIB (0x07) handler
void sec_gw_table_init(SecGwPeerTable* tbl);
const SecGwPeerEntry* sec_gw_table_find(const SecGwPeerTable* tbl, const uint8_t mac[6]);
bool sec_gw_handle_key_distrib(
    SecGwPeerTable* tbl,
    const uint8_t* plaintext, size_t pt_len
);

#endif  // HUEMIXLINK_SEC_PAIRING_H
