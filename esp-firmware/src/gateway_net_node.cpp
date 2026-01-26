/* 
  HUEMIXLINK V3 - GATEWAY NET FIRMWARE
  Supports: ESP32
*/

#include "HueMixLink.h"
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

#define PIN_BTN_MAIN  12   
#define PIN_BTN_AUX   13 
#define PIN_LED_WIFI  18
#define PIN_LED_DATA  19
#define PIN_RX        16
#define PIN_TX        17

WiFiUDP udp;
const char* ssidBase = "HueMix Link - ";
char ssidName[32]; 
char server_ip[40] = "192.168.1.1";
int server_port = 7777;
int local_port  = 4210;
WiFiManager wm; 
Ticker wifiTicker; 
HueMixLinkPacket txPkt;  
uint8_t udpBuffer[512];  
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

// --- SERIAL STATE MACHINE ---
enum SerialState { S_IDLE, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint8_t rxRawBuffer[sizeof(HueMixLinkPacket)];

void tickWifiLed() { digitalWrite(PIN_LED_WIFI, !digitalRead(PIN_LED_WIFI)); }
void setWifiLedState(int state) {
  wifiTicker.detach();
  if (state == 0) digitalWrite(PIN_LED_WIFI, LOW);
  else if (state == 1) digitalWrite(PIN_LED_WIFI, nightMode ? LOW : HIGH);
  else if (state == 2) wifiTicker.attach(0.5, tickWifiLed); 
  else if (state == 3) wifiTicker.attach(0.2, tickWifiLed); 
}
void flashDataLED(int times) {
  if (nightMode) return;
  for(int i=0; i<times; i++) {
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
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
void handleSerialPacket(uint8_t* data) {
  memcpy(&txPkt, data, sizeof(HueMixLinkPacket));
  
  // Debug
  Serial.printf("[NET] RX Serial Type 0x%02X\n", txPkt.type);

  if (WiFi.status() == WL_CONNECTED) {
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket));
    udp.endPacket();
    flashDataLED(1);
  } else {
    Serial.println("[NET] WiFi down, cannot forward");
  }
}

// --- PARSER ---
void parseSerialByte(uint8_t b) {
  last_serial_activity = millis();
  
  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) {
        rxState = S_READING;
        rxIndex = 0;
      }
      break;
    case S_READING:
      rxRawBuffer[rxIndex++] = b;
      // Prevent buffer overflow
      if (rxIndex > sizeof(HueMixLinkPacket)) {
        Serial.printf("[NET] Buffer overflow - resync\n");
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
      if (b == SERIAL_END) {
        handleSerialPacket(rxRawBuffer);
        rxState = S_IDLE;
      } else if (b == SERIAL_START) {
        // Possible packet corruption or misalignment, but current byte could be start of next packet
        Serial.printf("[NET] Footer Err (got 0x%02X instead of 0x%02X), resyncing to next packet\n", b, SERIAL_END);
        rxState = S_READING;
        rxIndex = 0;
      } else {
        // Footer mismatch - garbage byte, resync by looking for SERIAL_START
        Serial.printf("[NET] Footer Err (got 0x%02X), discarding packet\n", b);
        rxState = S_IDLE;
      }
      break;
  }
}

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  Serial.printf("[OTA] ABORT: %s\n", reason);
  
  // Notify server about the abort
  HueMixLinkPacket abortPkt;  memset(&abortPkt, 0, sizeof(HueMixLinkPacket));  abortPkt.type = PKT_OTA_ABORT;
  WiFi.macAddress(abortPkt.sourceMAC);
  memset(abortPkt.targetMAC, 0, 6);
  abortPkt.msgID = 0;
  
  // Encode reason as a simple code (0 = device-initiated abort)
  abortPkt.payload.raw[0] = 0;  // reason_code
  
  // Calculate signature (1 byte payload)
  abortPkt.signature = calculateHash(abortPkt.payload.raw, 185, HOME_ID);
  
  // Send abort notification to server
  udp.beginPacket(server_ip, server_port);
  udp.write((uint8_t*)&abortPkt, sizeof(HueMixLinkPacket));
  udp.endPacket();
  
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
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial2.write(SERIAL_END);
    Serial2.flush(); 
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
  
  // Send PKT_OTA_READY
  HueMixLinkPacket ready;
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

void handleOtaChunk(HueMixLinkPacket* pkt) {
  // Check if this chunk is for us
  uint8_t myWifiMac[6];
  WiFi.macAddress(myWifiMac);
  
  if (memcmp(pkt->targetMAC, myWifiMac, 6) != 0) {
    // Not for us, forward to radio node via UART
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial2.write(SERIAL_END);
    Serial2.flush();  // Wait for transmission to complete
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
  
  // Write chunk to partition
  esp_err_t err = esp_ota_write(update_handle, pkt->payload.otaChunk.data, data_len);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Write failed at chunk %d: %d\n", chunk_idx, err);
    abortOta("Write failed");
    return;
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
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial2.write(SERIAL_END);
    Serial2.flush();  // Wait for transmission to complete
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
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial2.write(SERIAL_END);
    Serial2.flush();  // Wait for transmission to complete
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
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)pkt, sizeof(HueMixLinkPacket));
    Serial2.write(SERIAL_END);
    Serial2.flush();  // Wait for transmission to complete
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
  
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  udp.beginPacket(server_ip, server_port);
  udp.write((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  udp.endPacket();
  flashDataLED(1);
}

void performFactoryReset() {
  Serial.println("FACTORY RESET TRIGGERED!");
  wifiTicker.detach();
  for(int i=0; i<5; i++) { 
    digitalWrite(PIN_LED_WIFI, HIGH); digitalWrite(PIN_LED_DATA, HIGH); delay(100); 
    digitalWrite(PIN_LED_WIFI, LOW); digitalWrite(PIN_LED_DATA, LOW); delay(100);
  }
  wm.resetSettings(); nvs_flash_erase(); nvs_flash_init(); prefs.clear();
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
  
  last_serial_activity = millis();
  sendSerialHandshake();
  flashDataLED(2);

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
  
  btnPkt.signature = calculateHash(btnPkt.payload.raw, 185, HOME_ID);
  if (WiFi.status() == WL_CONNECTED) {
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&btnPkt, sizeof(HueMixLinkPacket));
    udp.endPacket();
    flashDataLED(1);
  }
}

void loop() {
  btnMain.update(); btnAux.update();
  if (btnAux.read() == LOW && btnAux.currentDuration() > 5000) performFactoryReset();
  
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastReconnect = 0;
    digitalWrite(PIN_LED_WIFI, LOW); wifiTicker.detach(); 
    if (millis() - lastReconnect > 10000) { Serial.println("WiFi lost..."); WiFi.disconnect(); WiFi.reconnect(); lastReconnect = millis(); }
  } else { setWifiLedState(1); }

  // 1. UDP IN
  if (WiFi.status() == WL_CONNECTED) {
    int pSize = udp.parsePacket();
    if (pSize) {
      // Read partial packet from UDP
      int len = udp.read(udpBuffer, 512);
      if (len > 0) {
        memset(&txPkt, 0, sizeof(HueMixLinkPacket));
        memcpy(&txPkt, udpBuffer, len);
        
        Serial.printf("[NET] UDP Recv Type 0x%02X (Len %d)\n", txPkt.type, len);

        // Handle OTA packets
        if (txPkt.type == PKT_OTA_NOTIFY) {
          handleOtaNotify(&txPkt);
        } else if (txPkt.type == PKT_OTA_CHUNK) {
          handleOtaChunk(&txPkt);
        } else if (txPkt.type == PKT_OTA_CHECKPOINT_REQ) {
          handleOtaCheckpointReq(&txPkt);
        } else if (txPkt.type == PKT_OTA_COMPLETE) {
          handleOtaComplete(&txPkt);
        } else if (txPkt.type == PKT_OTA_ABORT) {
          handleOtaAbort(&txPkt);
        } else if (txPkt.type == PKT_PAIR_CONFIRM) {
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
          
          Serial2.write(SERIAL_START); 
          Serial2.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket)); 
          Serial2.write(SERIAL_END);
          Serial2.flush();
          flashDataLED(1);
        } else if (txPkt.type == PKT_SYS_CMD) {
          if (txPkt.signature == calculateHash(txPkt.payload.raw, 185, HOME_ID)) {
            bool oldMode = nightMode;
            if (txPkt.payload.sys.cmd == 1) nightMode = true;
            if (txPkt.payload.sys.cmd == 2) nightMode = false;
            
            if (oldMode != nightMode) {
              Serial.printf("Night Mode: %s\n", nightMode ? "ON" : "OFF");
              setWifiLedState(1);
            }
            
            // Forward to radio node
            Serial2.write(SERIAL_START); 
            Serial2.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket)); 
            Serial2.write(SERIAL_END);
            Serial2.flush();
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
          
          // Respond with uptime
          HueMixLinkPacket pong;
          memset(&pong, 0, sizeof(HueMixLinkPacket));
          pong.type = PKT_PING;
          WiFi.macAddress(pong.sourceMAC);
          uint32_t uptime_seconds = millis() / 1000;
          memcpy(pong.payload.raw, &uptime_seconds, sizeof(uint32_t));
          pong.signature = calculateHash(pong.payload.raw, 185, HOME_ID);
          
          udp.beginPacket(server_ip, server_port);
          udp.write((uint8_t*)&pong, sizeof(HueMixLinkPacket));
          udp.endPacket();
          Serial.printf("[NET] PING Response: Uptime %d seconds\n", uptime_seconds);
          flashDataLED(1);
        } else {
          // Forward other packet types to radio node
          Serial2.write(SERIAL_START); 
          Serial2.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket)); 
          Serial2.write(SERIAL_END);
          Serial2.flush();
          flashDataLED(1);
        }
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
}
