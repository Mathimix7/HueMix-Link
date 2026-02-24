/* 
  HUEMIXLINK V3 - GATEWAY RADIO FIRMWARE
  Supports: ESP32
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <nvs_flash.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_mac.h>
#include <esp_now.h>
#include <WiFi.h>
#include <Ticker.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include "mbedtls/sha256.h"

Preferences prefs;
uint32_t HOME_ID = 0; 
bool nightMode = false;
bool netNodeHasWiFi = false;  // Tracks if net node has WiFi connectivity

#define PIN_LED_STATUS 19
#define PIN_RX         16
#define PIN_TX         17

// --- STATUS LED STATE MACHINE ---
enum StatusLEDState { STATUS_LED_IDLE, STATUS_LED_ON, STATUS_LED_BRIEF_OFF };
StatusLEDState statusLEDState = STATUS_LED_IDLE;
unsigned long statusLEDTimer = 0;

// --- DATA LED BREATHING (for OTA) ---
#define DATA_LED_PWM_FREQ 5000
#define DATA_LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;

HueMixLinkPacket radioRx;
HueMixLinkPacket radioTx;
Payload_GatewayList activeGateways;

uint8_t lastMsgID = 0;
bool waitingForDelivery = false;
uint8_t lastTargetMAC[6] = {0}; // Store the actual target MAC for delivery reports

volatile bool pktReady = false;
HueMixLinkPacket bufferPkt;

// --- OTA STATE MACHINE ---
enum OtaState { OTA_IDLE, OTA_RECEIVING, OTA_VALIDATING, OTA_COMPLETE };
OtaState otaState = OTA_IDLE;
const esp_partition_t *update_partition = nullptr;
esp_ota_handle_t update_handle = 0;
uint32_t expected_firmware_size = 0;
uint32_t received_bytes = 0;
uint16_t expected_chunk_index = 0;
uint8_t expected_sha256[32];
mbedtls_sha256_context sha256_ctx;
unsigned long last_ota_activity = 0;

// --- STARTUP HANDSHAKE REQUEST TIMEOUT ---
unsigned long startup_time = 0;
bool handshake_request_received = false;
unsigned long last_serial_activity = 0;
#define STARTUP_HANDSHAKE_TIMEOUT 3000  // 3 seconds
#define SERIAL_IDLE_THRESHOLD 50  // Must be idle for 50ms before checking for handshake request

// --- RADIO SERIAL PARSER ---
enum SerialState { S_IDLE, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint8_t rxRawBuffer[sizeof(HueMixLinkPacket)];

// --- OTA BUFFERING CONFIG ---
#define OTA_BUFFER_MAX_CHUNKS 15
#define CHUNK_PAYLOAD_SIZE 185
#define OTA_ACCUMULATOR_SIZE (CHUNK_PAYLOAD_SIZE * OTA_BUFFER_MAX_CHUNKS)
uint8_t otaAccumulator[OTA_ACCUMULATOR_SIZE];
uint16_t otaAccumulatorIndex = 0;

void updateBreathing() {
  breathingBrightness += breathingDirection * 10;
  if (breathingBrightness >= 255) {
    breathingBrightness = 255;
    breathingDirection = -1;
  } else if (breathingBrightness <= 0) {
    breathingBrightness = 0;
    breathingDirection = 1;
  }
  ledcWrite(PIN_LED_STATUS, nightMode ? 0 : breathingBrightness);
}

void startDataLedBreathing() {
  ledcAttach(PIN_LED_STATUS, DATA_LED_PWM_FREQ, DATA_LED_PWM_RESOLUTION);
  breathingBrightness = 0;
  breathingDirection = 1;
  breathingTicker.attach_ms(30, updateBreathing);
}

void stopDataLedBreathing() {
  breathingTicker.detach();
  ledcDetach(PIN_LED_STATUS);
  pinMode(PIN_LED_STATUS, OUTPUT);
  digitalWrite(PIN_LED_STATUS, LOW);
}

void triggerStatusLED() {
  if (nightMode) return;
  
  if (statusLEDState == STATUS_LED_IDLE) {
    // LED is off, turn it on
    digitalWrite(PIN_LED_STATUS, HIGH);
    statusLEDTimer = millis() + LED_ON_DURATION;
    statusLEDState = STATUS_LED_ON;
  } else if (statusLEDState == STATUS_LED_ON) {
    // LED is on, create brief blink-off to show new event
    digitalWrite(PIN_LED_STATUS, LOW);
    statusLEDTimer = millis() + LED_BLINK_OFF_DURATION;
    statusLEDState = STATUS_LED_BRIEF_OFF;
  }
}

void sendSerialHandshake() {
  SerialHandshake h; 
  h.magic = SERIAL_HANDSHAKE;
  esp_read_mac(h.mac, ESP_MAC_WIFI_STA);
  
  // Parse version from build flag (format: "3.7.3")
  #ifdef FIRMWARE_VERSION
    const char* ver = FIRMWARE_VERSION;
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    h.version_major = major;
    h.version_minor = minor;
    h.version_patch = patch;
  #else
    h.version_major = 0;
    h.version_minor = 0;
    h.version_patch = 0;
  #endif
  
  Serial2.write((uint8_t*)&h, sizeof(h));
}

void saveGateways() {
  prefs.putBytes("gw", &activeGateways, sizeof(activeGateways));
  Serial.printf("[RADIO] Saved %d gateways to NVS\n", activeGateways.count);
}

void loadGateways() {
  size_t len = prefs.getBytes("gw", &activeGateways, sizeof(activeGateways));
  if (len == sizeof(activeGateways) && activeGateways.count > 0) {
    Serial.printf("[RADIO] Loaded %d gateways from NVS\n", activeGateways.count);
  } else {
    // No saved gateways, initialize with self
    activeGateways.count = 1;
    esp_read_mac(activeGateways.macs[0], ESP_MAC_WIFI_STA);
    Serial.println("[RADIO] No saved gateways, initialized with self");
  }
}

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  Serial.printf("[OTA] ABORT: %s\n", reason);
  
  // Notify server about the abort via UART to net node
  HueMixLinkPacket abortPkt;
  memset(&abortPkt, 0, sizeof(HueMixLinkPacket));
  abortPkt.type = PKT_OTA_ABORT;
  esp_read_mac(abortPkt.sourceMAC, ESP_MAC_WIFI_STA);
  memset(abortPkt.targetMAC, 0, 6);
  abortPkt.msgID = 0;
  
  // Encode reason as a simple code (0 = device-initiated abort)
  abortPkt.payload.raw[0] = 0;  // reason_code
  
  // Calculate signature (1 byte payload)
  abortPkt.signature = calculateHash(abortPkt.payload.raw, 185, HOME_ID);
  
  // Send abort notification to net node via UART
  Serial2.write(SERIAL_START);
  Serial2.write((uint8_t*)&abortPkt, sizeof(HueMixLinkPacket));
  Serial2.write(SERIAL_END);
  Serial2.flush();  // Wait for transmission to complete
  
  Serial.printf("[OTA] Sent abort notification via UART\n");
  
  // Clean up local state
  if (update_handle) {
    esp_ota_abort(update_handle);
    update_handle = 0;
  }
  
  // Stop DATA LED breathing
  stopDataLedBreathing();
  
  otaState = OTA_IDLE;
  expected_chunk_index = 0;
  otaAccumulatorIndex = 0;
  received_bytes = 0;
}

void handleOtaNotify(HueMixLinkPacket* pkt) {
  // Check if this is for us (our WiFi STA MAC)
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial.println("[OTA] NOTIFY forwarded via ESP-NOW");
    return;
  }
  
  // Security: Verify signature for OTA packet targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.printf("[RADIO] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt->signature);
      Serial.println("[RADIO] Rejected unauthorized OTA packet");
      return;
    }
  }
  
  if (otaState != OTA_IDLE) {
    Serial.println("[OTA] Busy with another update");
    return;
  }
  
  Serial.println("[OTA] NOTIFY received for radio node (self)");
  
  expected_firmware_size = pkt->payload.otaNotify.firmware_size;
  memcpy(expected_sha256, pkt->payload.otaNotify.sha256_hash, 32);
  
  // Initialize update partition
  update_partition = esp_ota_get_next_update_partition(NULL);
  if (!update_partition) {
    Serial.println("[OTA] No update partition available");
    abortOta("No partition");
    return;
  }
  
  Serial.printf("[OTA] Starting update: %u bytes\n", expected_firmware_size);
  Serial.printf("[OTA] Target partition: %s at 0x%x\n", update_partition->label, update_partition->address);
  
  esp_err_t err = esp_ota_begin(update_partition, OTA_SIZE_UNKNOWN, &update_handle);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Begin failed: %d\n", err);
    abortOta("Begin failed");
    return;
  }
  
  mbedtls_sha256_init(&sha256_ctx);
  mbedtls_sha256_starts(&sha256_ctx, 0); // 0 = SHA256 (not SHA224)
  
  otaState = OTA_RECEIVING;
  expected_chunk_index = 0;
  received_bytes = 0;
  last_ota_activity = millis();
    
  // Start DATA LED breathing during OTA
  startDataLedBreathing();
  
  // Send PKT_OTA_READY via UART to net node
  HueMixLinkPacket ready;
  memset(&ready, 0, sizeof(HueMixLinkPacket));
  ready.type = PKT_OTA_READY;
  esp_read_mac(ready.sourceMAC, ESP_MAC_WIFI_STA);
  memcpy(ready.targetMAC, pkt->sourceMAC, 6);
  ready.payload.otaReady.firmware_size = expected_firmware_size;
  ready.payload.otaReady.battery_mv = 0; // Not battery powered
  ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
  
  Serial2.write(SERIAL_START);
  Serial2.write((uint8_t*)&ready, sizeof(HueMixLinkPacket));
  Serial2.write(SERIAL_END);
  Serial2.flush();  // Wait for transmission to complete
  Serial.println("[OTA] Sent READY via UART");
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
    
  if (otaState != OTA_RECEIVING) {
    return;
  }

  // Security: Verify signature for OTA chunk targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.printf("[RADIO] SECURITY: Invalid OTA CHUNK signature\n");
      return;
    }
  }
  
  last_ota_activity = millis();
  
  uint16_t chunk_idx = pkt->payload.otaChunk.chunk_index;
  uint8_t data_len = pkt->payload.otaChunk.data_len;
  
  // Check chunk order: accept current or future chunks, ignore duplicates
  if (chunk_idx < expected_chunk_index) {
    // This is a duplicate chunk we already received - ignore it silently
    Serial.printf("[OTA] Ignoring duplicate chunk %d (already at %d)\n", chunk_idx, expected_chunk_index);
    return;
  }
  
  if (chunk_idx != expected_chunk_index) {
    Serial.printf("[OTA] Ignoring out-of-order chunk %d (expecting %d)\n", chunk_idx, expected_chunk_index);
    return;
  }

  if (otaAccumulatorIndex + data_len <= sizeof(otaAccumulator)) {
    memcpy(&otaAccumulator[otaAccumulatorIndex], pkt->payload.otaChunk.data, data_len);
    otaAccumulatorIndex += data_len;
  } else {
    // If we hit RAM limit before checkpoint, we must write
    esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
    if (err != ESP_OK) {
        abortOta("Flash write failed");
        return;
    }
    otaAccumulatorIndex = 0;
    memcpy(&otaAccumulator[otaAccumulatorIndex], pkt->payload.otaChunk.data, data_len);
    otaAccumulatorIndex += data_len;
  }

  // Update SHA256
  mbedtls_sha256_update(&sha256_ctx, pkt->payload.otaChunk.data, data_len);
  
  received_bytes += data_len;
  expected_chunk_index++;
  
  if (chunk_idx % 50 == 0) {
    Serial.printf("[OTA] Progress: %u / %u bytes (%.1f%%)\n", 
      received_bytes, expected_firmware_size, 
      (received_bytes * 100.0) / expected_firmware_size);
  }
}

void sendOtaChunkAck(uint16_t last_chunk_index) {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_OTA_CHUNK_ACK;
  esp_read_mac(pkt.sourceMAC, ESP_MAC_WIFI_STA);
  memset(pkt.targetMAC, 0, 6); // Server doesn't need target MAC
  pkt.msgID = 0;
  
  pkt.payload.otaChunkAck.last_chunk_index = last_chunk_index;
  
  // Calculate signature (2 bytes for last_chunk_index)
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  
  // Send via UART to net node
  Serial2.write(SERIAL_START);
  Serial2.write((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  Serial2.write(SERIAL_END);
  Serial2.flush();
  
  Serial.printf("[OTA] Sent checkpoint ACK via UART: last_chunk=%d\n", last_chunk_index);
}

void handleOtaCheckpointReq(HueMixLinkPacket* pkt) {
  // Check if this is for us
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[RADIO] SECURITY: Invalid OTA CHECKPOINT_REQ signature");
      return;
    }
  }
  
  if (otaState != OTA_RECEIVING) {
    return;
  }

  if (otaAccumulatorIndex >= 0) {
    Serial.printf("[OTA] Flushing %d bytes at checkpoint\n", otaAccumulatorIndex);
    esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
    if (err != ESP_OK) {
      abortOta("Flash write failed at checkpoint");
      return;
    }
    otaAccumulatorIndex = 0; // Clear RAM buffer
  }
  
  // Respond with last successfully received chunk (expected_chunk_index - 1)
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  sendOtaChunkAck(last_chunk);
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[RADIO] SECURITY: Invalid OTA COMPLETE signature");
      return;
    }
  }
  
  Serial.printf("[OTA] COMPLETE packet received (state=%d)\n", otaState);
  
  if (otaState != OTA_RECEIVING) {
    Serial.printf("[OTA] COMPLETE rejected: wrong state (got %d)\n", otaState);
    return;
  }

  if (otaAccumulatorIndex > 0) {
    Serial.printf("[OTA] Flushing final %d bytes\n", otaAccumulatorIndex);
    esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
    if (err != ESP_OK) {
      abortOta("Final flush failed");
      return;
    }
    otaAccumulatorIndex = 0;
  }

  // Send final checkpoint ACK to confirm all chunks received
  if (expected_chunk_index > 0) {
    sendOtaChunkAck(expected_chunk_index - 1);
  }
  
  Serial.println("[OTA] COMPLETE received, validating...");
  otaState = OTA_VALIDATING;
  
  // Finalize SHA256
  uint8_t calculated_sha256[32];
  mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
  mbedtls_sha256_free(&sha256_ctx);
  
  // Compare SHA256
  if (memcmp(calculated_sha256, expected_sha256, 32) != 0) {
    Serial.println("[OTA] SHA256 MISMATCH!");
    Serial.print("[OTA] Expected: ");
    for(int i=0; i<32; i++) Serial.printf("%02X", expected_sha256[i]);
    Serial.println();
    Serial.print("[OTA] Calculated: ");
    for(int i=0; i<32; i++) Serial.printf("%02X", calculated_sha256[i]);
    Serial.println();
    abortOta("SHA256 mismatch");
    return;
  }
  
  Serial.println("[OTA] SHA256 verified!");
  
  // Finalize OTA
  esp_err_t err = esp_ota_end(update_handle);
  if (err != ESP_OK) {
    Serial.printf("[OTA] End failed: %d\n", err);
    abortOta("End failed");
    return;
  }
  update_handle = 0;
  
  // Set boot partition
  err = esp_ota_set_boot_partition(update_partition);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Set boot partition failed: %d\n", err);
    abortOta("Set boot failed");
    return;
  }
  
  Serial.println("[OTA] UPDATE SUCCESSFUL! Rebooting in 2 seconds...");
  otaState = OTA_COMPLETE;
  
  // Stop breathing effect before flashing LED
  stopDataLedBreathing();
  
  for(int i=0; i<10; i++) {
    digitalWrite(PIN_LED_STATUS, HIGH);
    delay(100);
    digitalWrite(PIN_LED_STATUS, LOW);
    delay(100);
  }
  
  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[RADIO] SECURITY: Invalid OTA ABORT signature");
      return;
    }
  }
  
  Serial.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

// --- HANDLE PACKET FROM NET NODE ---
void handleSerialPacket(uint8_t* data) {
  memcpy(&radioTx, data, sizeof(HueMixLinkPacket));
  
  Serial.printf("[RADIO] Serial RX Type 0x%02X\n", radioTx.type);

  // Handle OTA packets
  if (radioTx.type == PKT_OTA_NOTIFY) {
    handleOtaNotify(&radioTx);
    return;
  } else if (radioTx.type == PKT_OTA_CHUNK) {
    handleOtaChunk(&radioTx);
    return;
  } else if (radioTx.type == PKT_OTA_CHECKPOINT_REQ) {
    handleOtaCheckpointReq(&radioTx);
    return;
  } else if (radioTx.type == PKT_OTA_COMPLETE) {
    handleOtaComplete(&radioTx);
    return;
  } else if (radioTx.type == PKT_OTA_ABORT) {
    handleOtaAbort(&radioTx);
    return;
  }

  if (radioTx.type == PKT_SYS_CMD) {
    // Security: Verify signature for SYS_CMD
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(radioTx.payload.raw, 185, HOME_ID);
      if (radioTx.signature != expected_sig) {
        Serial.printf("[RADIO] SECURITY: Invalid SYS_CMD signature. Expected 0x%08X, got 0x%08X\n", expected_sig, radioTx.signature);
        Serial.println("[RADIO] Rejected unauthorized SYS_CMD");
        return;
      }
    }
    
    if (radioTx.payload.sys.cmd == 1) nightMode = true;
    else if (radioTx.payload.sys.cmd == 2) nightMode = false;
    else if (radioTx.payload.sys.cmd == 3) {
      // WiFi status update from net node
      netNodeHasWiFi = (radioTx.payload.raw[1] == 1);
      Serial.printf("[RADIO] Net node WiFi status: %s\n", netNodeHasWiFi ? "CONNECTED" : "DISCONNECTED");
      return;  // Don't forward this command via ESP-NOW
    }
    else {
      esp_now_peer_info_t peer = {};
      memcpy(peer.peer_addr, radioTx.targetMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL;
      peer.encrypt = false;
      if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
      lastMsgID = radioTx.msgID;
      memcpy(lastTargetMAC, radioTx.targetMAC, 6);
      waitingForDelivery = true;
      esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
    }
  } else if (radioTx.type == PKT_GW_LIST_UPD) {
    activeGateways = radioTx.payload.gwList;
    saveGateways();
    Serial.printf("[RADIO] Updated Gateways List (%d nodes)\n", activeGateways.count);
    
    // Check if this update is targeted to a specific device (not broadcast)
    bool isBroadcast = true;
    for(int i=0; i<6; i++) {
      if (radioTx.targetMAC[i] != 0xFF) {
        isBroadcast = false;
        break;
      }
    }
    
    // If targeted, forward to the device via ESP-NOW
    if (!isBroadcast) {      
      esp_now_peer_info_t peer = {};
      memcpy(peer.peer_addr, radioTx.targetMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL;
      peer.encrypt = false;
      if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
      lastMsgID = radioTx.msgID;
      memcpy(lastTargetMAC, radioTx.targetMAC, 6);
      waitingForDelivery = true;
      esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
    }
  } else if (radioTx.type == PKT_PAIR_CONFIRM) {
    HOME_ID = radioTx.payload.pair.newHomeID;
    prefs.putUInt("hid", HOME_ID);

    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID;
    memcpy(lastTargetMAC, radioTx.targetMAC, 6);
    waitingForDelivery = true;
    esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
  } else {
    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID;
    memcpy(lastTargetMAC, radioTx.targetMAC, 6);
    waitingForDelivery = true;
    esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
  }
}

void parseSerialByte(uint8_t b) {
  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) { 
        rxState = S_READING; 
        rxIndex = 0; 
        last_serial_activity = millis();
      } 
      else if (b == SERIAL_REQ_HANDSHAKE && (millis() - last_serial_activity) > SERIAL_IDLE_THRESHOLD) { 
        handshake_request_received = true;  // Mark that we got a request
        Serial.println("[RADIO] Received handshake request from net node");
        sendSerialHandshake(); 
        last_serial_activity = millis();
      }
      break;
    case S_READING:
      last_serial_activity = millis();
      rxRawBuffer[rxIndex++] = b;
      // Prevent buffer overflow
      if (rxIndex > sizeof(HueMixLinkPacket)) {
        Serial.printf("[RADIO] Buffer overflow - resync\n");
        rxState = S_IDLE;
        rxIndex = 0;
        // Check if current byte is SERIAL_START for next packet
        if (b == SERIAL_START) {
          rxState = S_READING;
          rxIndex = 0;
        }
      } else if (rxIndex >= sizeof(HueMixLinkPacket)) {
        rxState = S_FOOTER;
      }
      break;
    case S_FOOTER:
      last_serial_activity = millis();
      if (b == SERIAL_END) {
        handleSerialPacket(rxRawBuffer);
        rxState = S_IDLE;
      } else if (b == SERIAL_START) {
        // Possible packet corruption, but current byte could be start of next packet
        Serial.printf("[RADIO] Footer Err (got 0x%02X), resyncing to next packet\n", b);
        rxState = S_READING;
        rxIndex = 0;
      } else {
        // Footer mismatch - garbage byte, resync
        Serial.printf("[RADIO] Footer Err (got 0x%02X), discarding packet\n", b);
        rxState = S_IDLE;
      }
      break;
  }
}

void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  if (waitingForDelivery) {
    HueMixLinkPacket rpt; 

    memset(&rpt, 0, sizeof(HueMixLinkPacket));

    rpt.type = PKT_DELIVERY_RPT;
    esp_read_mac(rpt.sourceMAC, ESP_MAC_WIFI_STA);

    rpt.payload.report.originalMsgID = lastMsgID;
    rpt.payload.report.success = (status == ESP_NOW_SEND_SUCCESS);
    memcpy(rpt.payload.report.targetMAC, lastTargetMAC, 6); // Use stored target MAC instead of callback parameter

    rpt.signature = calculateHash(rpt.payload.raw, 185, HOME_ID);

    Serial2.write(SERIAL_START); Serial2.write((uint8_t*)&rpt, sizeof(rpt)); Serial2.write(SERIAL_END);
    Serial2.flush();
    waitingForDelivery = false;
    
    triggerStatusLED();
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if (len != sizeof(HueMixLinkPacket)) return;
  if (pktReady) return; 

  memcpy(&radioRx, data, sizeof(HueMixLinkPacket));
  memcpy(radioRx.sourceMAC, info->src_addr, 6);
  
  // Record RSSI for HELLO and PING_DEVICE responses
  if (radioRx.type == PKT_HELLO) { 
    radioRx.payload.raw[1] = (uint8_t)info->rx_ctrl->rssi; 
  }
  else if (radioRx.type == PKT_PING_DEVICE) {
    // Store RSSI in first byte of payload for Python to read
    radioRx.payload.raw[0] = (uint8_t)info->rx_ctrl->rssi;
  }
  
  memcpy(&bufferPkt, &radioRx, sizeof(HueMixLinkPacket));
  pktReady = true;
  
  triggerStatusLED();
}

void setup() {
  pinMode(PIN_LED_STATUS, OUTPUT);
  Serial.begin(115200); 
  Serial2.setRxBufferSize(8192);
  Serial2.setTxBufferSize(8192);
  Serial2.begin(460800, SERIAL_8N1, PIN_RX, PIN_TX); 
  WiFi.mode(WIFI_STA); 
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);

  // Track startup for handshake timeout
  startup_time = millis();
  last_serial_activity = millis();
  handshake_request_received = false;

  for(int i=0; i<5; i++) { sendSerialHandshake(); delay(100); }
  esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);
  
  // Load saved gateways from NVS, or initialize with self
  loadGateways();
  
  // Ensure self is in the list
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  bool selfInList = false;
  for(int i=0; i<activeGateways.count; i++) {
    if(memcmp(activeGateways.macs[i], myMac, 6) == 0) {
      selfInList = true;
      break;
    }
  }
  if(!selfInList && activeGateways.count < MAX_GATEWAYS) {
    memcpy(activeGateways.macs[activeGateways.count], myMac, 6);
    activeGateways.count++;
    saveGateways();
    Serial.println("[RADIO] Added self to gateway list");
  }
  
  Serial.println("--- RADIO NODE READY ---");
}

void loop() {
  if (!handshake_request_received && (millis() - startup_time) > STARTUP_HANDSHAKE_TIMEOUT) {
    Serial.println("[RADIO] No handshake request from net node, requesting...");
    Serial2.write(SERIAL_REQ_HANDSHAKE);
    Serial2.flush();
    handshake_request_received = true;
  }

  // OTA timeout check
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    Serial.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  if (pktReady) {
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)&bufferPkt, sizeof(bufferPkt));
    Serial2.write(SERIAL_END);
    Serial2.flush();
    
    if (bufferPkt.type == PKT_BTN_EVENT) {
      // Only send ACK if net node has WiFi connectivity
      if (netNodeHasWiFi) {
        HueMixLinkPacket ack; 
        memset(&ack, 0, sizeof(HueMixLinkPacket));
        ack.type = PKT_ACK_TO_BTN;
        esp_read_mac(ack.sourceMAC, ESP_MAC_WIFI_STA);
        memcpy(ack.targetMAC, bufferPkt.sourceMAC, 6);
        ack.msgID = 0;
        ack.payload.gwList = activeGateways;
        
        // Security: Sign the ACK packet with HOME_ID
        ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
        
        esp_now_peer_info_t peer = {}; 
        memcpy(peer.peer_addr, bufferPkt.sourceMAC, 6);
        peer.channel = HUEMIXLINK_CHANNEL; 
        peer.encrypt = false;
        if(!esp_now_is_peer_exist(bufferPkt.sourceMAC)) esp_now_add_peer(&peer);
        esp_now_send(bufferPkt.sourceMAC, (uint8_t*)&ack, sizeof(ack));
      } else {
        Serial.println("[RADIO] Button ACK suppressed - Net node has no WiFi");
      }
    }
    pktReady = false;
  }

  // STATUS LED STATE MACHINE HANDLER
  if (statusLEDState == STATUS_LED_ON && millis() >= statusLEDTimer) {
    digitalWrite(PIN_LED_STATUS, LOW);
    statusLEDState = STATUS_LED_IDLE;
  } else if (statusLEDState == STATUS_LED_BRIEF_OFF && millis() >= statusLEDTimer) {
    digitalWrite(PIN_LED_STATUS, HIGH);
    statusLEDTimer = millis() + LED_ON_DURATION;
    statusLEDState = STATUS_LED_ON;
  }

  if (!waitingForDelivery) {
    while (Serial2.available() > 0) {
      parseSerialByte(Serial2.read());
      if (waitingForDelivery) break;
    }
  }
}
