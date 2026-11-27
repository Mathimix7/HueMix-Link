#include "sec.h"
#include <string.h>

void sec_build_nonce(const uint8_t src_mac[6], uint32_t counter, uint16_t key_epoch, uint8_t nonce_out[12]) {
  memcpy(nonce_out, src_mac, 6);
  nonce_out[6]  = (uint8_t)(counter & 0xFF);
  nonce_out[7]  = (uint8_t)((counter >> 8) & 0xFF);
  nonce_out[8]  = (uint8_t)((counter >> 16) & 0xFF);
  nonce_out[9]  = (uint8_t)((counter >> 24) & 0xFF);
  nonce_out[10] = (uint8_t)(key_epoch & 0xFF);
  nonce_out[11] = (uint8_t)((key_epoch >> 8) & 0xFF);
}

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
) {
  uint8_t single_zero = 0;
  const uint8_t* src_pt = plaintext;
  size_t wire_pt_len = pt_len;

  if (wire_pt_len == 0) {
    src_pt = &single_zero;
    wire_pt_len = 1;
  }

  // Rule 1 (§4.2): A v2 frame must never be exactly 203 bytes (reserved for v1)
  bool pad_one = ((wire_pt_len + V2_OVERHEAD) == V2_FORBIDDEN_LEN);
  size_t final_pt_len = wire_pt_len + (pad_one ? 1 : 0);
  size_t total_len = final_pt_len + V2_OVERHEAD;

  if (total_len > out_cap) {
    return 0;
  }

  HueMixLinkHeaderV2 hdr;
  memset(&hdr, 0, sizeof(hdr));
  hdr.type = type;
  hdr.version = PROTO_VERSION_V2;
  memcpy(hdr.sourceMAC, src_mac, 6);
  memcpy(hdr.targetMAC, dst_mac, 6);
  hdr.msgID = msg_id;
  hdr.counter = counter;
  hdr.key_epoch = key_epoch;
  hdr.rssi = 0;

  memcpy(out_buf, &hdr, V2_HEADER_SIZE);

  // Copy plaintext into ciphertext slot (encrypt in-place safe for pad_one)
  uint8_t* ct_ptr = out_buf + V2_HEADER_SIZE;
  memcpy(ct_ptr, src_pt, wire_pt_len);
  if (pad_one) {
    ct_ptr[wire_pt_len] = 0x00;
  }

  uint8_t nonce[V2_NONCE_SIZE];
  sec_build_nonce(src_mac, counter, key_epoch, nonce);

  uint8_t* tag_ptr = out_buf + V2_HEADER_SIZE + final_pt_len;
  if (!sec_aes_gcm_encrypt(key, nonce, out_buf, V2_AAD_SIZE, ct_ptr, final_pt_len, ct_ptr, tag_ptr)) {
    return 0;
  }

  return total_len;
}

bool sec_decode_v2(
    const uint8_t* frame, size_t frame_len,
    const uint8_t key[16],
    HueMixLinkHeaderV2* hdr_out,
    uint8_t* pt_out, size_t pt_cap,
    size_t* pt_len_out
) {
  if (frame_len < V2_MIN_FRAME_SIZE) {
    return false;
  }

  HueMixLinkHeaderV2 hdr;
  memcpy(&hdr, frame, V2_HEADER_SIZE);
  if (hdr.version != PROTO_VERSION_V2) {
    return false;
  }

  size_t ct_len = frame_len - V2_OVERHEAD;
  if (ct_len > pt_cap) {
    return false;
  }

  uint8_t nonce[V2_NONCE_SIZE];
  sec_build_nonce(hdr.sourceMAC, hdr.counter, hdr.key_epoch, nonce);

  const uint8_t* ct_ptr = frame + V2_HEADER_SIZE;
  const uint8_t* tag_ptr = frame + V2_HEADER_SIZE + ct_len;

  if (!sec_aes_gcm_decrypt(key, nonce, frame, V2_AAD_SIZE, ct_ptr, ct_len, tag_ptr, pt_out)) {
    return false;
  }

  if (hdr_out != nullptr) {
    *hdr_out = hdr;
  }
  if (pt_len_out != nullptr) {
    *pt_len_out = ct_len;
  }
  return true;
}
