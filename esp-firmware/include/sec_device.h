#ifndef HUEMIXLINK_SEC_DEVICE_H
#define HUEMIXLINK_SEC_DEVICE_H

#include <stddef.h>
#include <stdint.h>
#include "HueMixLink.h"
#include "sec.h"
#include "sec_pairing.h"

void sec_device_init(SecPairingState* st, SecCounter* ctr);
uint16_t sec_device_max_frame(void);
bool sec_device_is_v2(const SecPairingState* st);
void sec_device_on_factory_reset(SecPairingState* st);

// Populate v2 capability TLV + ephemeral X25519 pub_D + nonce_D into a 185-byte v1 HELLO payload.
bool sec_device_prepare_unpaired_hello(SecPairingState* st, uint8_t payload_raw[185], size_t cap_off);

// Encode one outbound v2 frame using K_own and monotonic counter. Returns wire length or 0 if panicked.
size_t sec_device_encode_tx(
    const SecPairingState* st,
    SecCounter* ctr,
    uint8_t pkt_type,
    const uint8_t my_mac[6],
    const uint8_t dst_mac[6],
    uint8_t msg_id,
    const uint8_t* plaintext, size_t pt_len,
    uint8_t* out_buf, size_t out_cap
);

typedef enum {
  SEC_RX_IGNORED = 0,
  SEC_RX_PAIRED_V2,      // Handshake succeeded; caller must save out_home_id & send ack_buf[ack_len]
  SEC_RX_DATA_V2         // Authenticated v2 packet decrypted into pt_out[pt_len] with header hdr_out
} SecRxResult;

SecRxResult sec_device_handle_rx_v2(
    SecPairingState* st,
    SecCounter* ctr,
    const uint8_t my_mac[6],
    const uint8_t* frame, size_t frame_len,
    HueMixLinkHeaderV2* hdr_out,
    uint8_t* pt_out, size_t pt_cap, size_t* pt_len_out,
    uint32_t* out_home_id,
    uint8_t* ack_out, size_t ack_cap, size_t* ack_len_out
);

// Handle decrypted v2 PKT_SYS_CMD CMD_SET_PIN: write-once PIN save + build encrypted ACK frame.
bool sec_device_handle_set_pin_cmd(
    SecPairingState* st,
    SecCounter* ctr,
    const uint8_t my_mac[6],
    const uint8_t* pt, size_t pt_len,
    uint8_t* rsp_out, size_t rsp_cap, size_t* rsp_len_out
);

// Non-blocking serial PIN provisioning for ESP8266 (reads "HML2_PIN:<6digits>\n" when no PIN is set).
void sec_device_poll_serial_pin(SecPairingState* st);

#endif  // HUEMIXLINK_SEC_DEVICE_H
