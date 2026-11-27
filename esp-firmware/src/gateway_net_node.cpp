/* 
  HUEMIXLINK V3 - GATEWAY NET FIRMWARE
  Supports: ESP32
*/

#include "HueMixLink.h"
#include "sec_device.h"
#include <Preferences.h>
#include <nvs_flash.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_mac.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WiFiManager.h> 
#include <Bounce2.h> 
#include <Ticker.h> 
#include <vector>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include "mbedtls/sha256.h"

Preferences prefs;
uint32_t HOME_ID = 0; 
bool nightMode = false;
bool lastWiFiStatus = false;  // Track WiFi status changes

#define PIN_BTN_MAIN  12   
#define PIN_BTN_AUX   13 
#define PIN_LED_WIFI  18
#define PIN_LED_DATA  19
#define PIN_RX        16
#define PIN_TX        17

// --- DATA LED STATE MACHINE ---
enum DataLEDState { DATA_LED_IDLE, DATA_LED_ON, DATA_LED_BRIEF_OFF };
DataLEDState dataLEDState = DATA_LED_IDLE;
unsigned long dataLEDTimer = 0;

WiFiUDP udp;
const char* ssidBase = "HueMix Link - ";
char ssidName[32]; 
char server_ip[40] = "192.168.1.1";
int server_port = 7777;
int local_port  = 4210;
WiFiManager wm; 
Ticker wifiTicker; 
HueMixLinkPacket txPkt;  
#define MAX_FRAME_BUFFER 1500
uint8_t udpBuffer[MAX_FRAME_BUFFER];  
SecPairingState netSecState;
SecCounter netCounter;
uint8_t radioNodeMAC[6] = {0,0,0,0,0,0};
uint8_t radioVersion[3] = {0, 0, 0};  // major, minor, patch

Bounce btnMain = Bounce();
Bounce btnAux = Bounce();
bool buttonPressed = false;
bool isHolding = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingIntervalUpdate = 0;
const int HOLD_TIME = 500;
const int HOLD_INTERVAL = 500;

// --- STARTUP HANDSHAKE REQUEST TIMEOUT ---
unsigned long last_serial_activity = 0;
#define SERIAL_IDLE_THRESHOLD 50  // Must be idle for 50ms before checking for handshake request

// --- DATA LED BREATHING ---
#define DATA_LED_PWM_FREQ 5000
#define DATA_LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;  

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

// --- SERIAL STATE MACHINE (length-prefixed: 0xFE | len_lo | len_hi | payload | 0xFD) ---
enum SerialState { S_IDLE, S_LEN_LO, S_LEN_HI, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint16_t rxExpectedLen = 0;
uint8_t rxRawBuffer[MAX_FRAME_BUFFER];

void sendSerialFrame(const uint8_t* data, uint16_t len) {
  if (len < V2_OVERHEAD || len > V2_MAX_FRAME_ESP32) return;
  uint8_t hdr[3] = { SERIAL_START, (uint8_t)(len & 0xFF), (uint8_t)((len >> 8) & 0xFF) };
  Serial2.write(hdr, 3);
  Serial2.write(data, len);
  Serial2.write(SERIAL_END);
  Serial2.flush();
}

void tickWifiLed() { digitalWrite(PIN_LED_WIFI, !digitalRead(PIN_LED_WIFI)); }
void setWifiLedState(int state) {
  wifiTicker.detach();
  if (state == 0) digitalWrite(PIN_LED_WIFI, LOW);
  else if (state == 1) digitalWrite(PIN_LED_WIFI, nightMode ? LOW : HIGH);
  else if (state == 2) wifiTicker.attach(0.5, tickWifiLed); 
  else if (state == 3) wifiTicker.attach(0.2, tickWifiLed); 
}
void flashDataLED() {
  if (nightMode) return;
  
  if (dataLEDState == DATA_LED_IDLE) {
    // LED is off, turn it on
    digitalWrite(PIN_LED_DATA, HIGH);
    dataLEDTimer = millis() + LED_ON_DURATION;
    dataLEDState = DATA_LED_ON;
  } else if (dataLEDState == DATA_LED_ON) {
    // LED is on, create brief blink-off to show new event
    digitalWrite(PIN_LED_DATA, LOW);
    dataLEDTimer = millis() + LED_BLINK_OFF_DURATION;
    dataLEDState = DATA_LED_BRIEF_OFF;
  }
}

void updateBreathing() {
  breathingBrightness += breathingDirection * 10;
  if (breathingBrightness >= 255) {
    breathingBrightness = 255;
    breathingDirection = -1;
  } else if (breathingBrightness <= 0) {
    breathingBrightness = 0;
    breathingDirection = 1;
  }
  ledcWrite(PIN_LED_DATA, nightMode ? 0 : breathingBrightness);
}

void startDataLedBreathing() {
  ledcAttach(PIN_LED_DATA, DATA_LED_PWM_FREQ, DATA_LED_PWM_RESOLUTION);
  breathingBrightness = 0;
  breathingDirection = 1;
  breathingTicker.attach_ms(30, updateBreathing);
}

void stopDataLedBreathing() {
  breathingTicker.detach();
  ledcDetach(PIN_LED_DATA);
  pinMode(PIN_LED_DATA, OUTPUT);
  digitalWrite(PIN_LED_DATA, LOW);
}

// --- HANDLE SERIAL PACKET ---
void handleSerialPacket(uint8_t* data, uint16_t len) {
  Serial.printf("[NET] RX Serial Type 0x%02X (Len %u)\n", data[0], len);

  if (WiFi.status() == WL_CONNECTED) {
    udp.beginPacket(server_ip, server_port);
    udp.write(data, len);
    udp.endPacket();
  } else {
    Serial.println("[NET] WiFi down, cannot forward");
  }
}

// --- PARSER (length-prefixed: 0xFE | len_lo | len_hi | payload | 0xFD) ---
void parseSerialByte(uint8_t b) {
  // Inter-frame idle timeout: reset partial/corrupted frame after >100 ms silence
  if (rxState != S_IDLE && (millis() - last_serial_activity) > 100) {
    Serial.printf("[NET] Serial frame timeout in state %d (got %u/%u B) - resync\n",
                  rxState, rxIndex, rxExpectedLen);
    rxState = S_IDLE;
    rxIndex = 0;
    rxExpectedLen = 0;
  }
  last_serial_activity = millis();
  
  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) {
        rxState = S_LEN_LO;
        rxIndex = 0;
        rxExpectedLen = 0;
      }
      break;
    case S_LEN_LO:
      rxExpectedLen = b;
      rxState = S_LEN_HI;
      break;
    case S_LEN_HI:
      rxExpectedLen |= ((uint16_t)b << 8);
      if (rxExpectedLen < V2_OVERHEAD || rxExpectedLen > V2_MAX_FRAME_ESP32) {
        Serial.printf("[NET] Invalid serial frame len %u - resync\n", rxExpectedLen);
        rxState = (b == SERIAL_START) ? S_LEN_LO : S_IDLE;
        rxIndex = 0;
        rxExpectedLen = 0;
      } else {
        rxIndex = 0;
        rxState = S_READING;
      }
      break;
    case S_READING:
      rxRawBuffer[rxIndex++] = b;
      if (rxIndex >= rxExpectedLen) {
        rxState = S_FOOTER;
      }
      break;
    case S_FOOTER:
      if (b == SERIAL_END) {
        handleSerialPacket(rxRawBuffer, rxExpectedLen);
        rxState = S_IDLE;
      } else if (b == SERIAL_START) {
        Serial.printf("[NET] Footer Err (got 0x%02X instead of 0x%02X), resyncing to next packet\n", b, SERIAL_END);
        rxState = S_LEN_LO;
        rxIndex = 0;
        rxExpectedLen = 0;
      } else {
        Serial.printf("[NET] Footer Err (got 0x%02X), discarding packet\n", b);
        rxState = S_IDLE;
      }
      break;
  }
}

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  Serial.printf("[OTA] ABORT: %s\n", reason);
  
  // Notify server about the abort (dual-stack v1/v2)
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  if (sec_device_is_v2(&netSecState)) {
    uint8_t reasonCode = 0;
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &netSecState, &netCounter, PKT_OTA_ABORT, myWifiMac, backendMac, 0,
        &reasonCode, sizeof(reasonCode), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      udp.beginPacket(server_ip, server_port);
      udp.write(v2Buf, v2Len);
      udp.endPacket();
    }
  } else {
    HueMixLinkPacket abortPkt;
    memset(&abortPkt, 0, sizeof(HueMixLinkPacket));
    abortPkt.type = PKT_OTA_ABORT;
    memcpy(abortPkt.sourceMAC, myWifiMac, 6);
    memset(abortPkt.targetMAC, 0, 6);
    abortPkt.msgID = 0;
    abortPkt.payload.raw[0] = 0;  // reason_code
    abortPkt.signature = calculateHash(abortPkt.payload.raw, 185, HOME_ID);
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&abortPkt, sizeof(HueMixLinkPacket));
    udp.endPacket();
  }
  
  Serial.printf("[OTA] Sent abort notification to server\n");
  
  // Clean up local state
  if (update_handle) {
    esp_ota_abort(update_handle);
    update_handle = 0;
  }
  
  // Stop DATA LED breathing
  stopDataLedBreathing();
  
  otaState = OTA_IDLE;
  expected_chunk_index = 0;
  received_bytes = 0;
}

void handleOtaNotify(HueMixLinkPacket* pkt) {
  // Check if this is for us (WiFi MAC)
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    sendSerialFrame((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature for OTA packet targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.printf("[NET] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt->signature);
      Serial.println("[NET] Rejected unauthorized OTA packet");
      return;
    }
  }
  
  // It's for us - handle OTA for ourselves
  if (otaState != OTA_IDLE) {
    Serial.println("[OTA] Busy with another update");
    return;
  }
  
  Serial.println("[OTA] NOTIFY received for WiFi node (self)");
  
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
  
  last_ota_activity = millis();
  otaState = OTA_RECEIVING;
  expected_chunk_index = 0;
  received_bytes = 0;
  
  // Start DATA LED breathing during OTA
  startDataLedBreathing();
  
  // Send PKT_OTA_READY (dual-stack v1/v2)
  if (sec_device_is_v2(&netSecState)) {
    Payload_OtaReady r = {};
    r.firmware_size = expected_firmware_size;
    r.battery_mv = 0;
    uint8_t myMac[6];
    WiFi.macAddress(myMac);
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &netSecState, &netCounter, PKT_OTA_READY, myMac, backendMac, 0,
        (const uint8_t*)&r, sizeof(r), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      udp.beginPacket(server_ip, server_port);
      udp.write(v2Buf, v2Len);
      udp.endPacket();
      Serial.println("[OTA] Sent v2 READY");
    }
  } else {
    HueMixLinkPacket ready;
    memset(&ready, 0, sizeof(HueMixLinkPacket));
    ready.type = PKT_OTA_READY;
    WiFi.macAddress(ready.sourceMAC);
    memcpy(ready.targetMAC, pkt->sourceMAC, 6);
    ready.payload.otaReady.firmware_size = expected_firmware_size;
    ready.payload.otaReady.battery_mv = 0; // Not battery powered
    ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
    
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&ready, sizeof(HueMixLinkPacket));
    udp.endPacket();
    Serial.println("[OTA] Sent READY");
  }
}

void handleOtaChunkRaw(uint16_t chunk_idx, const uint8_t* chunk_data, uint16_t data_len) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  last_ota_activity = millis();
  if (chunk_idx < expected_chunk_index) {
    return;
  }
  if (chunk_idx != expected_chunk_index) {
    Serial.printf("[OTA] Ignoring out-of-order chunk %d (expecting %d)\n", chunk_idx, expected_chunk_index);
    return;
  }
  esp_err_t err = esp_ota_write(update_handle, chunk_data, data_len);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Write failed at chunk %d: %d\n", chunk_idx, err);
    abortOta("Write failed");
    return;
  }
  mbedtls_sha256_update(&sha256_ctx, chunk_data, data_len);
  received_bytes += data_len;
  expected_chunk_index++;
  if (chunk_idx % 50 == 0) {
    Serial.printf("[OTA] Progress: %u / %u bytes (%.1f%%)\n",
      received_bytes, expected_firmware_size,
      (received_bytes * 100.0) / expected_firmware_size);
  }
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  // Check if this chunk is for us
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    sendSerialFrame((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }

  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  // Security: Verify signature for OTA chunk targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.printf("[NET] SECURITY: Invalid OTA CHUNK signature\n");
      return;
    }
  }
  
  handleOtaChunkRaw(pkt->payload.otaChunk.chunk_index, pkt->payload.otaChunk.data, pkt->payload.otaChunk.data_len);
}

void sendOtaChunkAck(uint16_t last_chunk_index) {
  if (sec_device_is_v2(&netSecState)) {
    Payload_OtaChunkAck ack = {};
    ack.last_chunk_index = last_chunk_index;
    uint8_t myMac[6];
    WiFi.macAddress(myMac);
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &netSecState, &netCounter, PKT_OTA_CHUNK_ACK, myMac, backendMac, 0,
        (const uint8_t*)&ack, sizeof(ack), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      udp.beginPacket(server_ip, server_port);
      udp.write(v2Buf, v2Len);
      udp.endPacket();
      Serial.printf("[OTA] Sent v2 checkpoint ACK: last_chunk=%d\n", last_chunk_index);
    }
    return;
  }

  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_OTA_CHUNK_ACK;
  WiFi.macAddress(pkt.sourceMAC);
  memset(pkt.targetMAC, 0, 6); // Server doesn't need target MAC
  pkt.msgID = 0;
  
  pkt.payload.otaChunkAck.last_chunk_index = last_chunk_index;
  
  // Calculate signature (2 bytes for last_chunk_index)
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  
  udp.beginPacket(server_ip, server_port);
  udp.write((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  udp.endPacket();
  
  Serial.printf("[OTA] Sent checkpoint ACK: last_chunk=%d\n", last_chunk_index);
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  // Check if this is for us
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  Serial.printf("[OTA] COMPLETE packet received (state=%d)\n", otaState);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    Serial.println("[OTA] COMPLETE not for us, forwarding to radio");
    sendSerialFrame((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[NET] SECURITY: Invalid OTA COMPLETE signature");
      return;
    }
  }
  
  if (otaState != OTA_RECEIVING) {
    Serial.printf("[OTA] COMPLETE rejected: wrong state (got %d)\n", otaState);
    return;
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
  
  for(int i=0; i<10; i++) {
    digitalWrite(PIN_LED_WIFI, HIGH);
    digitalWrite(PIN_LED_DATA, HIGH);
    delay(100);
    digitalWrite(PIN_LED_WIFI, LOW);
    digitalWrite(PIN_LED_DATA, LOW);
    delay(100);
  }
  
  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  // Check if this is for us
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    sendSerialFrame((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[NET] SECURITY: Invalid OTA ABORT signature");
      return;
    }
  }
  
  Serial.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

void handleOtaCheckpointReq(HueMixLinkPacket* pkt) {
  // Check if this is for us
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    sendSerialFrame((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      Serial.println("[NET] SECURITY: Invalid OTA CHECKPOINT_REQ signature");
      return;
    }
  }
  
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  // Respond with last successfully received chunk (expected_chunk_index - 1)
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  sendOtaChunkAck(last_chunk);
}

void sendSerialHandshake() {
  Serial.println("[NET] Requesting handshake from radio node...");
  
  while(Serial2.available()) Serial2.read();
  
  unsigned long startWait = millis();
  unsigned long lastReq = 0;
  bool radioReady = false;
  
  while(millis() - startWait < 5000) { 
    if (millis() - lastReq > 200) { Serial2.write(SERIAL_REQ_HANDSHAKE); lastReq = millis(); }
    if(Serial2.available() >= sizeof(SerialHandshake)) {
      if(Serial2.peek() == SERIAL_HANDSHAKE) {
        SerialHandshake h; Serial2.readBytes((uint8_t*)&h, sizeof(h));
        bool v = false; for(int k=0;k<6;k++) if(h.mac[k]!=0) v=true;
        if(v) { 
          memcpy(radioNodeMAC, h.mac, 6);
          radioVersion[0] = h.version_major;
          radioVersion[1] = h.version_minor; 
          radioVersion[2] = h.version_patch;
          radioReady = true; 
          break; 
        }
      } else { Serial2.read(); }
    }
    delay(10);
  }
  
  if(radioReady) { 
    Serial.print("[NET] Handshake OK! Radio MAC: ");
    for(int i=0; i<6; i++) Serial.printf("%02X", radioNodeMAC[i]);
    Serial.printf(" Version: %d.%d.%d\n", radioVersion[0], radioVersion[1], radioVersion[2]);
  } else { 
    Serial.println("[NET] Handshake FAILED (No response from radio)"); 
  }
}

void sendGatewayHello() {
  if (WiFi.status() != WL_CONNECTED) return;
  // Serial.println("Sending Hello...");
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  pkt.payload.raw[0] = DEV_GATEWAY; 
  memcpy(&pkt.payload.raw[1], radioNodeMAC, 6);
  
  // Parse version from build flag (format: "3.7.3") for NET NODE
  #ifdef FIRMWARE_VERSION
    const char* ver = FIRMWARE_VERSION;
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    pkt.payload.raw[7] = major;   // Net node major
    pkt.payload.raw[8] = minor;   // Net node minor
    pkt.payload.raw[9] = patch;   // Net node patch
    pkt.payload.raw[10] = 0;      // Net node build number
  #else
    pkt.payload.raw[7] = 0;
    pkt.payload.raw[8] = 0;
    pkt.payload.raw[9] = 0;
    pkt.payload.raw[10] = 0;
  #endif
  
  // Add RADIO NODE version (from handshake)
  pkt.payload.raw[11] = radioVersion[0];  // Radio node major
  pkt.payload.raw[12] = radioVersion[1];  // Radio node minor
  pkt.payload.raw[13] = radioVersion[2];  // Radio node patch
  pkt.payload.raw[14] = 0;                // Radio node build number

  if (!sec_device_is_v2(&netSecState)) {
    sec_pairing_fill_hello_tlv(&netSecState, pkt.payload.raw, 15, V2_MAX_FRAME_ESP32);
  } else {
    pkt.payload.raw[15] = PROTO_VERSION_V2;
    pkt.payload.raw[16] = (uint8_t)(V2_MAX_FRAME_ESP32 & 0xFF);
    pkt.payload.raw[17] = (uint8_t)((V2_MAX_FRAME_ESP32 >> 8) & 0xFF);
  }
  
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  udp.beginPacket(server_ip, server_port);
  udp.write((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  udp.endPacket();
  flashDataLED();
}

void sendWiFiStatusToRadio(bool connected) {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_SYS_CMD;
  WiFi.macAddress(pkt.sourceMAC);
  pkt.payload.sys.cmd = 3;  // WiFi status command
  pkt.payload.raw[1] = connected ? 1 : 0;
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  
  sendSerialFrame((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  
  Serial.printf("[NET] Sent WiFi status to radio: %s\n", connected ? "CONNECTED" : "DISCONNECTED");
}

void performFactoryReset() {
  Serial.println("FACTORY RESET TRIGGERED!");
  // Tell the paired radio node over UART to factory-reset its Security v2 keys & HOME_ID too
  uint8_t zeroMac[6] = {0, 0, 0, 0, 0, 0};
  if (memcmp(radioNodeMAC, zeroMac, 6) != 0) {
    HueMixLinkPacket rstPkt;
    memset(&rstPkt, 0, sizeof(HueMixLinkPacket));
    rstPkt.type = PKT_SYS_CMD;
    WiFi.macAddress(rstPkt.sourceMAC);
    memcpy(rstPkt.targetMAC, radioNodeMAC, 6);
    rstPkt.payload.sys.cmd = CMD_FACTORY_RESET;
    rstPkt.signature = calculateHash(rstPkt.payload.raw, 185, HOME_ID);
    sendSerialFrame((uint8_t*)&rstPkt, sizeof(HueMixLinkPacket));
    delay(150);
  }
  // Clear pairing/v2 keys while preserving write-once PIN in NVS namespace "sec"
  sec_device_on_factory_reset(&netSecState);
  wifiTicker.detach();
  for(int i=0; i<5; i++) { 
    digitalWrite(PIN_LED_WIFI, HIGH); digitalWrite(PIN_LED_DATA, HIGH); delay(100); 
    digitalWrite(PIN_LED_WIFI, LOW); digitalWrite(PIN_LED_DATA, LOW); delay(100);
  }
  wm.resetSettings();
  prefs.clear();
  Serial.println("Rebooting..."); ESP.restart();
}

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); delay(500);
  pinMode(PIN_LED_WIFI, OUTPUT); pinMode(PIN_LED_DATA, OUTPUT);
  setWifiLedState(2); 
  btnMain.attach(PIN_BTN_MAIN, INPUT_PULLUP); btnMain.interval(25);
  btnAux.attach(PIN_BTN_AUX, INPUT_PULLUP); btnAux.interval(25);
  Serial.begin(115200); 
  
  Serial2.setRxBufferSize(8192);
  Serial2.setTxBufferSize(8192);
  Serial2.begin(460800, SERIAL_8N1, PIN_RX, PIN_TX); 
  
  Serial.println("\n--- BOOTING NET NODE ---");

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);
  sec_device_init(&netSecState, &netCounter);
  
  last_serial_activity = millis();
  sendSerialHandshake();
  for(int i=0; i<2; i++) {
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
  }

  WiFiManagerParameter custom_ip("server", "Server IP", "", 40);
  char portStr[6]; itoa(server_port, portStr, 10);
  WiFiManagerParameter custom_port("port", "Server Port", portStr, 6);
  wm.addParameter(&custom_ip); wm.addParameter(&custom_port);
  std::vector<const char *> wm_menu = {"wifi", "exit"}; wm.setMenu(wm_menu);
  uint32_t chipId = (uint32_t)ESP.getEfuseMac();
  sprintf(ssidName, "%s%08X", ssidBase, chipId);
  wm.setAPCallback([](WiFiManager *myWiFiManager) { setWifiLedState(3); Serial.println("Entered Config Mode"); });

  bool hasSavedConfig = (prefs.getString("srv_ip", "").length() > 0);
  if (!hasSavedConfig) {
    Serial.println("No config -> Opening Portal");
    wm.setConnectTimeout(180);
    if (!wm.autoConnect(ssidName, "HueMixLink")) { ESP.restart(); }
  } else {
    Serial.println("Connecting...");
    wm.setEnableConfigPortal(false); wm.setConnectTimeout(20);
    if (!wm.autoConnect(ssidName, "HueMixLink")) {
      Serial.println("Connection Failed! Retrying...");
      setWifiLedState(2); 
      unsigned long la = millis();
      while (WiFi.status() != WL_CONNECTED) {
        btnAux.update();
        if (btnAux.read() == LOW && btnAux.currentDuration() > 5000) performFactoryReset();
        if (millis() - la > 10000) { Serial.print("."); WiFi.disconnect(); WiFi.reconnect(); la = millis(); }
        delay(10);
      }
    }
  }

  String sip = custom_ip.getValue();
  if (sip.length() > 0) {
    strcpy(server_ip, custom_ip.getValue()); server_port = atoi(custom_port.getValue());
    prefs.putString("srv_ip", server_ip); prefs.putInt("srv_port", server_port);
  } else {
    String s = prefs.getString("srv_ip"); s.toCharArray(server_ip, 40); server_port = prefs.getInt("srv_port");
  }

  udp.begin(local_port);
  setWifiLedState(1);
  Serial.println("\n--- OPERATIONAL ---");
  Serial.printf("Server: %s:%d\n", server_ip, server_port);
  sendGatewayHello();
  
  // Send initial WiFi status to radio node
  lastWiFiStatus = true;
  sendWiFiStatusToRadio(true);
}

void sendBtnEvent(uint8_t action) {
  HueMixLinkPacket btnPkt;
  btnPkt.type = PKT_BTN_EVENT;
  WiFi.macAddress(btnPkt.sourceMAC);
  btnPkt.payload.btn.action = action;
  btnPkt.payload.btn.battery_mv = 0;
  btnPkt.payload.btn.button_index = -1;  // Gateway button acts like normal button
  
  // Parse firmware version
  #ifdef FIRMWARE_VERSION
    const char* ver = FIRMWARE_VERSION;
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    btnPkt.payload.btn.version_major = major;
    btnPkt.payload.btn.version_minor = minor;
    btnPkt.payload.btn.version_patch = patch;
  #else
    btnPkt.payload.btn.version_major = 0;
    btnPkt.payload.btn.version_minor = 0;
    btnPkt.payload.btn.version_patch = 0;
  #endif
  
  // Gateway is always ESP32
  btnPkt.payload.btn.platform = 0;
  
  // Button count: 0 for gateway button (not applicable)
  btnPkt.payload.btn.button_count = 0;
  
  btnPkt.signature = calculateHash(btnPkt.payload.raw, 185, HOME_ID);
  if (WiFi.status() == WL_CONNECTED) {
    if (sec_device_is_v2(&netSecState)) {
      const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
      uint8_t v2Wire[128];
      size_t v2Len = sec_device_encode_tx(
          &netSecState, &netCounter, PKT_BTN_EVENT, btnPkt.sourceMAC, backendMac, 0,
          (const uint8_t*)&btnPkt.payload.btn, sizeof(Payload_Button),
          v2Wire, sizeof(v2Wire)
      );
      if (v2Len > 0) {
        udp.beginPacket(server_ip, server_port);
        udp.write(v2Wire, v2Len);
        udp.endPacket();
        flashDataLED();
      }
    } else {
      udp.beginPacket(server_ip, server_port);
      udp.write((uint8_t*)&btnPkt, sizeof(HueMixLinkPacket));
      udp.endPacket();
      flashDataLED();
    }
  }
}

void loop() {
  btnMain.update(); btnAux.update();
  if (btnAux.read() == LOW && btnAux.currentDuration() > 5000) performFactoryReset();
  
  // Track WiFi status changes and notify radio node
  bool currentWiFiStatus = (WiFi.status() == WL_CONNECTED);
  if (currentWiFiStatus != lastWiFiStatus) {
    lastWiFiStatus = currentWiFiStatus;
    sendWiFiStatusToRadio(currentWiFiStatus);
  }
  
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastReconnect = 0;
    digitalWrite(PIN_LED_WIFI, LOW); wifiTicker.detach(); 
    if (millis() - lastReconnect > 10000) { Serial.println("WiFi lost..."); WiFi.disconnect(); WiFi.reconnect(); lastReconnect = millis(); }
  } else { setWifiLedState(1); }

  // 1. UDP IN
  if (WiFi.status() == WL_CONNECTED) {
    int pSize = udp.parsePacket();
    if (pSize) {
      int len = udp.read(udpBuffer, sizeof(udpBuffer));
      if (len > 0) {
        flashDataLED();
        Serial.printf("[NET] UDP Recv Type 0x%02X (Len %d)\n", udpBuffer[0], len);

        // Variable-length Security v2 frame (never 203 B)
        if (len != (int)sizeof(HueMixLinkPacket)) {
          if (len >= V2_MIN_FRAME_SIZE && len <= V2_MAX_FRAME_ESP32 && udpBuffer[1] == PROTO_VERSION_V2) {
            uint8_t myWifiMac[6];
            WiFi.macAddress(myWifiMac);
            HueMixLinkHeaderV2 hdr;
            memcpy(&hdr, udpBuffer, V2_HEADER_SIZE);

            // Net-node targeted v2 PKT_PAIR_CONFIRM (151 B): install K_own[wifi_mac] and send PKT_PAIR_ACK
            if (hdr.type == PKT_PAIR_CONFIRM && len == V2_PAIR_CONFIRM_LEN && memcmp(hdr.targetMAC, myWifiMac, 6) == 0) {
              uint32_t newHomeId = 0;
              uint8_t ackBuf[96];
              size_t ackLen = 0;
              if (sec_pairing_handle_confirm_v2(&netSecState, myWifiMac, udpBuffer, (size_t)len, &netCounter, &newHomeId, ackBuf, sizeof(ackBuf), &ackLen)) {
                if (newHomeId != 0 && newHomeId != HOME_ID) {
                  HOME_ID = newHomeId;
                  prefs.putUInt("hid", HOME_ID);
                }
                // Send PKT_PAIR_ACK twice over UDP so a single dropped packet cannot leave
                // Python thinking the net node is still v1 while NVS has committed v2.
                for (int attempt = 0; attempt < 2; attempt++) {
                  udp.beginPacket(server_ip, server_port);
                  udp.write(ackBuf, ackLen);
                  udp.endPacket();
                  if (attempt == 0) delay(150);
                }
                Serial.printf("[NET] PAIRED v2! K_own[wifi_mac] installed (epoch=%u)\n", netSecState.key_epoch);
              }
            } else if (hdr.type == PKT_PING && memcmp(hdr.targetMAC, myWifiMac, 6) == 0 && sec_device_is_v2(&netSecState)) {
              uint8_t pt[32];
              size_t ptLen = 0;
              if (sec_decode_v2(udpBuffer, (size_t)len, netSecState.k_own, nullptr, pt, sizeof(pt), &ptLen)) {
                uint32_t uptime_seconds = millis() / 1000;
                const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
                uint8_t pongBuf[96];
                size_t pongLen = sec_device_encode_tx(
                    &netSecState, &netCounter, PKT_PING, myWifiMac, backendMac, hdr.msgID,
                    (const uint8_t*)&uptime_seconds, sizeof(uptime_seconds),
                    pongBuf, sizeof(pongBuf)
                );
                if (pongLen > 0) {
                  udp.beginPacket(server_ip, server_port);
                  udp.write(pongBuf, pongLen);
                  udp.endPacket();
                  Serial.printf("[NET] PING v2 Response: Uptime %u seconds\n", uptime_seconds);
                }
              }
            } else if (hdr.type == PKT_SYS_CMD && memcmp(hdr.targetMAC, myWifiMac, 6) == 0 && sec_device_is_v2(&netSecState)) {
              uint8_t pt[64];
              size_t ptLen = 0;
              if (sec_decode_v2(udpBuffer, (size_t)len, netSecState.k_own, nullptr, pt, sizeof(pt), &ptLen) && ptLen >= 1) {
                uint8_t rspBuf[96];
                size_t rspLen = 0;
                if (sec_device_handle_set_pin_cmd(&netSecState, &netCounter, myWifiMac, pt, ptLen, rspBuf, sizeof(rspBuf), &rspLen)) {
                  udp.beginPacket(server_ip, server_port);
                  udp.write(rspBuf, rspLen);
                  udp.endPacket();
                  Serial.println("[NET] Processed v2 CMD_SET_PIN and sent ACK");
                } else if (pt[0] == 1 || pt[0] == 2) {
                  bool oldMode = nightMode;
                  nightMode = (pt[0] == 1);
                  if (oldMode != nightMode) {
                    Serial.printf("[NET] v2 Night Mode: %s\n", nightMode ? "ON" : "OFF");
                    setWifiLedState(1);
                  }
                }
              }
            } else if ((hdr.type == PKT_OTA_NOTIFY || hdr.type == PKT_OTA_CHUNK ||
                        hdr.type == PKT_OTA_CHECKPOINT_REQ || hdr.type == PKT_OTA_COMPLETE ||
                        hdr.type == PKT_OTA_ABORT) &&
                       memcmp(hdr.targetMAC, myWifiMac, 6) == 0 && sec_device_is_v2(&netSecState)) {
              static uint8_t otaPt[1440];
              size_t ptLen = 0;
              if (sec_decode_v2(udpBuffer, (size_t)len, netSecState.k_own, nullptr, otaPt, sizeof(otaPt), &ptLen)) {
                if (hdr.type == PKT_OTA_CHUNK && ptLen >= sizeof(Payload_OtaChunkV2Header)) {
                  Payload_OtaChunkV2Header ch;
                  memcpy(&ch, otaPt, sizeof(ch));
                  if (sizeof(ch) + ch.data_len <= ptLen) {
                    handleOtaChunkRaw(ch.chunk_index, otaPt + sizeof(ch), ch.data_len);
                  }
                } else {
                  HueMixLinkPacket synth;
                  memset(&synth, 0, sizeof(synth));
                  synth.type = hdr.type;
                  memcpy(synth.sourceMAC, hdr.sourceMAC, 6);
                  memcpy(synth.targetMAC, myWifiMac, 6);
                  synth.msgID = hdr.msgID;
                  size_t cp = (ptLen > sizeof(synth.payload.raw)) ? sizeof(synth.payload.raw) : ptLen;
                  memcpy(synth.payload.raw, otaPt, cp);
                  synth.signature = calculateHash(synth.payload.raw, 185, HOME_ID);
                  if (hdr.type == PKT_OTA_NOTIFY) handleOtaNotify(&synth);
                  else if (hdr.type == PKT_OTA_CHECKPOINT_REQ) handleOtaCheckpointReq(&synth);
                  else if (hdr.type == PKT_OTA_COMPLETE) handleOtaComplete(&synth);
                  else if (hdr.type == PKT_OTA_ABORT) handleOtaAbort(&synth);
                }
              }
            } else {
              // Relay all other v2 frames verbatim to radio node
              sendSerialFrame(udpBuffer, (uint16_t)len);
            }
          }
          continue_udp_done:;
        } else {
        memset(&txPkt, 0, sizeof(HueMixLinkPacket));
        memcpy(&txPkt, udpBuffer, sizeof(HueMixLinkPacket));

        uint8_t myWifiMacV1[6];
        WiFi.macAddress(myWifiMacV1);
        bool targetIsSelfV1 = (memcmp(txPkt.targetMAC, myWifiMacV1, 6) == 0);

        // Handle OTA packets (anti-downgrade: reject v1 OTA targeted to self once v2-paired)
        if (txPkt.type == PKT_OTA_NOTIFY || txPkt.type == PKT_OTA_CHUNK ||
            txPkt.type == PKT_OTA_CHECKPOINT_REQ || txPkt.type == PKT_OTA_COMPLETE ||
            txPkt.type == PKT_OTA_ABORT) {
          if (targetIsSelfV1 && sec_device_is_v2(&netSecState)) {
            Serial.printf("[NET] SECURITY: Rejected v1 OTA packet (type=0x%02X) while v2-paired\n", txPkt.type);
          } else if (txPkt.type == PKT_OTA_NOTIFY) {
            handleOtaNotify(&txPkt);
          } else if (txPkt.type == PKT_OTA_CHUNK) {
            handleOtaChunk(&txPkt);
          } else if (txPkt.type == PKT_OTA_CHECKPOINT_REQ) {
            handleOtaCheckpointReq(&txPkt);
          } else if (txPkt.type == PKT_OTA_COMPLETE) {
            handleOtaComplete(&txPkt);
          } else if (txPkt.type == PKT_OTA_ABORT) {
            handleOtaAbort(&txPkt);
          }
        } else if (txPkt.type == PKT_PAIR_CONFIRM) {
          if (sec_pairing_allow_v1_confirm(&netSecState)) {
            uint32_t incomingSig = txPkt.signature;
            uint32_t expectedSig = calculateHash(txPkt.payload.raw, 185, 0);
            if (incomingSig == expectedSig) {
              uint32_t newID = txPkt.payload.pair.newHomeID;
              if (newID != HOME_ID && newID != 0) { 
                Serial.printf("!!! UPDATING HOME ID: 0x%X !!!\n", newID);
                HOME_ID = newID; prefs.putUInt("hid", HOME_ID);
                digitalWrite(PIN_LED_DATA, HIGH); delay(1000); digitalWrite(PIN_LED_DATA, LOW);
                sendGatewayHello();
              } else if (newID == HOME_ID) {
                Serial.println("Re-sending Gateway Hello...");
                sendGatewayHello();
              }
            }
          } else if (targetIsSelfV1) {
            Serial.println("[NET] SECURITY: Rejected v1 PKT_PAIR_CONFIRM while v2-paired");
          }
          
          // Forward to radio node for downstream legacy v1 device pairing
          sendSerialFrame((uint8_t*)&txPkt, sizeof(HueMixLinkPacket));
        } else if (txPkt.type == PKT_SYS_CMD) {
          if (txPkt.signature == calculateHash(txPkt.payload.raw, 185, HOME_ID)) {
            uint8_t myWifiMac[6];
            WiFi.macAddress(myWifiMac);
            if (txPkt.payload.sys.cmd == CMD_UPGRADE_TO_V2 && memcmp(txPkt.targetMAC, myWifiMac, 6) == 0) {
              if (sec_device_is_v2(&netSecState)) {
                Serial.println("[NET] Clearing stale v2 keys for re-pairing (CMD_UPGRADE_TO_V2)");
                sec_pairing_clear_keys(&netSecState);
              }
              Serial.println("[NET] Triggering in-place v2 upgrade HELLO");
              sendGatewayHello();
            } else {
              bool oldMode = nightMode;
              if (txPkt.payload.sys.cmd == 1) nightMode = true;
              if (txPkt.payload.sys.cmd == 2) nightMode = false;
              
              if (oldMode != nightMode) {
                Serial.printf("Night Mode: %s\n", nightMode ? "ON" : "OFF");
                setWifiLedState(1);
              }
              
              // Forward to radio node
              sendSerialFrame((uint8_t*)&txPkt, sizeof(HueMixLinkPacket));
            }
          }
        } else if (txPkt.type == PKT_PING) {
          // Security: Verify signature for PING
          if (HOME_ID != 0) {
            uint32_t expected_sig = calculateHash(txPkt.payload.raw, 185, HOME_ID);
            if (txPkt.signature != expected_sig) {
              Serial.printf("[NET] SECURITY: Invalid PING signature. Expected 0x%08X, got 0x%08X\n", expected_sig, txPkt.signature);
              return;
            }
          }
          
          // Respond with uptime (encrypted v2 if paired as v2, else v1)
          uint32_t uptime_seconds = millis() / 1000;
          if (sec_device_is_v2(&netSecState)) {
            uint8_t myWifiMac[6];
            WiFi.macAddress(myWifiMac);
            const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
            uint8_t pongBuf[96];
            size_t pongLen = sec_device_encode_tx(
                &netSecState, &netCounter, PKT_PING, myWifiMac, backendMac, txPkt.msgID,
                (const uint8_t*)&uptime_seconds, sizeof(uptime_seconds),
                pongBuf, sizeof(pongBuf)
            );
            if (pongLen > 0) {
              udp.beginPacket(server_ip, server_port);
              udp.write(pongBuf, pongLen);
              udp.endPacket();
              Serial.printf("[NET] PING v2 Response: Uptime %u seconds\n", uptime_seconds);
            }
          } else {
            HueMixLinkPacket pong;
            memset(&pong, 0, sizeof(HueMixLinkPacket));
            pong.type = PKT_PING;
            WiFi.macAddress(pong.sourceMAC);
            memcpy(pong.payload.raw, &uptime_seconds, sizeof(uint32_t));
            pong.signature = calculateHash(pong.payload.raw, 185, HOME_ID);
            
            udp.beginPacket(server_ip, server_port);
            udp.write((uint8_t*)&pong, sizeof(HueMixLinkPacket));
            udp.endPacket();
            Serial.printf("[NET] PING Response: Uptime %d seconds\n", uptime_seconds);
          }
        } else {
          // Forward other packet types to radio node
          sendSerialFrame((uint8_t*)&txPkt, sizeof(HueMixLinkPacket));
        }
        } // end v1 203-byte branch
      }
    }
  }

  // 2. SERIAL IN - Check for handshake requests from radio node, then parse framed packets
  while (Serial2.available() > 0) {
    uint8_t b = Serial2.peek();
    
    // Check for raw handshake request from radio node (only when idle for 50ms+ to avoid false positives)
    if (b == SERIAL_REQ_HANDSHAKE && (millis() - last_serial_activity) > SERIAL_IDLE_THRESHOLD) {
      Serial2.read();  // Consume the byte
      Serial.println("[NET] Received handshake request from radio node");
      sendSerialHandshake();
      
      // After sending handshake, send HELLO to notify server of radio node's current version
      delay(100);
      sendWiFiStatusToRadio(lastWiFiStatus);
      sendGatewayHello();
    } else {
      // Process as regular framed packet
      parseSerialByte(Serial2.read());
    }
  }

  // 3. OTA TIMEOUT CHECK
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    Serial.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  // 4. BUTTON HANDLING
  if (btnMain.fell()) {
    buttonHoldStartTime = millis();
    buttonPressed = true;
  }

  if (btnMain.read() == LOW && buttonPressed && !isHolding) {
    if (millis() - buttonHoldStartTime >= HOLD_TIME) {
      isHolding = true;
      sendBtnEvent(ACT_HOLDING);
      holdingIntervalUpdate = millis();
    }
  }

  if (millis() - holdingIntervalUpdate >= HOLD_INTERVAL && isHolding) {
    sendBtnEvent(ACT_HOLDING);
    holdingIntervalUpdate = millis();
  }

  if (btnMain.rose()) {
    if (isHolding) {
      isHolding = false;
      sendBtnEvent(ACT_RELEASE);
    } else {
      sendBtnEvent(ACT_CLICK);
    }
    buttonPressed = false;
  }

  // 5. DATA LED STATE MACHINE HANDLER
  if (dataLEDState == DATA_LED_ON && millis() >= dataLEDTimer) {
    digitalWrite(PIN_LED_DATA, LOW);
    dataLEDState = DATA_LED_IDLE;
  } else if (dataLEDState == DATA_LED_BRIEF_OFF && millis() >= dataLEDTimer) {
    digitalWrite(PIN_LED_DATA, HIGH);
    dataLEDTimer = millis() + LED_ON_DURATION;
    dataLEDState = DATA_LED_ON;
  }
}
