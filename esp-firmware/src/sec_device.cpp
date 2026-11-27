#include "sec_device.h"
#include <string.h>

#if defined(ESP32)
  #include <esp_now.h>
#endif

void sec_device_init(SecPairingState* st, SecCounter* ctr) {
  sec_pairing_load(st);
  sec_counter_init_default(ctr);
}

uint16_t sec_device_max_frame(void) {
#if defined(ESP32)
  uint32_t ver = 0;
  if (esp_now_get_version(&ver) == ESP_OK && ver >= 2) {
    return V2_MAX_FRAME_ESP32;
  }
  return V2_MAX_FRAME_ESP8266;
#else
  return V2_MAX_FRAME_ESP8266;
#endif
}

bool sec_device_is_v2(const SecPairingState* st) {
  return st->crypto_mode != SEC_MODE_V1;
}

void sec_device_on_factory_reset(SecPairingState* st) {
  sec_pairing_clear_keys(st);
}

bool sec_device_prepare_unpaired_hello(SecPairingState* st, uint8_t payload_raw[185], size_t cap_off) {
  return sec_pairing_fill_hello_tlv(st, payload_raw, cap_off, sec_device_max_frame());
}

size_t sec_device_encode_tx(
    const SecPairingState* st,
    SecCounter* ctr,
    uint8_t pkt_type,
    const uint8_t my_mac[6],
    const uint8_t dst_mac[6],
    uint8_t msg_id,
    const uint8_t* plaintext, size_t pt_len,
    uint8_t* out_buf, size_t out_cap
) {
  if (!sec_device_is_v2(st)) return 0;
  uint32_t next_ctr = 0;
  if (!sec_counter_next(ctr, &next_ctr)) {
    return 0;  // SECURITY_PANIC or unpersisted counter — refuse TX
  }
  return sec_encode_v2(
      pkt_type, my_mac, dst_mac, msg_id, next_ctr, st->key_epoch,
      plaintext, pt_len, st->k_own, out_buf, out_cap
  );
}

SecRxResult sec_device_handle_rx_v2(
    SecPairingState* st,
    SecCounter* ctr,
    const uint8_t my_mac[6],
    const uint8_t* frame, size_t frame_len,
    HueMixLinkHeaderV2* hdr_out,
    uint8_t* pt_out, size_t pt_cap, size_t* pt_len_out,
    uint32_t* out_home_id,
    uint8_t* ack_out, size_t ack_cap, size_t* ack_len_out
) {
  if (frame_len < V2_MIN_FRAME_SIZE || frame[1] != PROTO_VERSION_V2) {
    return SEC_RX_IGNORED;
  }

  // 1. Flight 2 pairing handshake (151-byte PKT_PAIR_CONFIRM)
  if (frame[0] == PKT_PAIR_CONFIRM && frame_len == V2_PAIR_CONFIRM_LEN) {
    if (sec_pairing_handle_confirm_v2(st, my_mac, frame, frame_len, ctr, out_home_id, ack_out, ack_cap, ack_len_out)) {
      return SEC_RX_PAIRED_V2;
    }
    return SEC_RX_IGNORED;
  }

  // 2. Authenticated v2 data packet (must already be v2-paired)
  if (!sec_device_is_v2(st)) {
    return SEC_RX_IGNORED;
  }

  const uint8_t* key = (frame[0] == PKT_ACK_TO_BTN) ? st->k_ack : st->k_own;
  if (sec_decode_v2(frame, frame_len, key, hdr_out, pt_out, pt_cap, pt_len_out)) {
    return SEC_RX_DATA_V2;
  }
  return SEC_RX_IGNORED;
}

bool sec_device_handle_set_pin_cmd(
    SecPairingState* st,
    SecCounter* ctr,
    const uint8_t my_mac[6],
    const uint8_t* pt, size_t pt_len,
    uint8_t* rsp_out, size_t rsp_cap, size_t* rsp_len_out
) {
  if (!sec_device_is_v2(st) || pt_len < 8 || pt[0] != CMD_SET_PIN) {
    return false;
  }
  uint8_t status = sec_pairing_set_pin(st, (const char*)(pt + 2));
  const uint8_t backend_mac[6] = {0, 0, 0, 0, 0, 0};
  uint8_t ack_pt[2] = { CMD_SET_PIN_ACK, status };
  size_t len = sec_device_encode_tx(
      st, ctr, PKT_SYS_CMD, my_mac, backend_mac, 0,
      ack_pt, sizeof(ack_pt), rsp_out, rsp_cap
  );
  if (len == 0) return false;
  if (rsp_len_out != nullptr) *rsp_len_out = len;
  return true;
}

void sec_device_poll_serial_pin(SecPairingState* st) {
#if defined(ESP8266)
  if (st == nullptr || st->has_pin) return;
  static char lineBuf[32];
  static uint8_t lineIdx = 0;
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineIdx] = '\0';
      if (lineIdx == 15 && strncmp(lineBuf, "HML2_PIN:", 9) == 0) {
        uint8_t status = sec_pairing_set_pin(st, lineBuf + 9);
        if (status == PIN_ACK_OK || status == PIN_ACK_ALREADY_SET) {
          Serial.println("HML2_PIN_OK");
        } else {
          Serial.println("HML2_PIN_ERR");
        }
      }
      lineIdx = 0;
    } else if (lineIdx < sizeof(lineBuf) - 1) {
      lineBuf[lineIdx++] = c;
    } else {
      lineIdx = 0;
    }
  }
#else
  (void)st;
#endif
}
