/* 
  HUEMIXLINK V3 - REMOTE BUTTON FIRMWARE (4-BUTTON)
  Supports: ESP32 only
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <Bounce2.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_adc_cal.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_system.h>
#include "mbedtls/sha256.h"
#include <Ticker.h>

// GPIO pins with pull-down resistors (HIGH = pressed)
#define PIN_BTN0  32
#define PIN_BTN1  33
#define PIN_BTN2  27
#define PIN_BTN3  26
#define PIN_RESET 14
#define PIN_LED   18
#define PIN_BATTERY 35

#define LED_ACTIVE_HIGH HIGH

#define HOLD_TIME     500
#define HOLD_INTERVAL 500
#define SLEEP_TIMEOUT 2000

// EXT1 bitmask for all 4 buttons + reset
#define EXT1_BUTTON_MASK ((1ULL << PIN_BTN0) | (1ULL << PIN_BTN1) | (1ULL << PIN_BTN2) | (1ULL << PIN_BTN3) | (1ULL << PIN_RESET))

// --- LED BREATHING (for OTA) ---
#define LED_PWM_FREQ 5000
#define LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;
bool breathingActive = false;

Preferences prefs;
uint32_t HOME_ID = 0;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Bounce buttons[4];
const uint8_t buttonPins[4] = {PIN_BTN0, PIN_BTN1, PIN_BTN2, PIN_BTN3};

bool wakeupExt1 = false;
int wakeupButtonIndex = -1;
bool wakeupWasReset = false;
unsigned long lastActivityTime = 0;
volatile bool ackReceived = false;

// Per-button state
struct ButtonState {
  bool pressed = false;
  unsigned long holdStartTime = 0;
  unsigned long holdingIntervalUpdate = 0;
  bool isHolding = false;
};

ButtonState buttonStates[4];

unsigned long ledTimer = 0;
bool ledActive = false;
bool homeSetupDone = false;
uint16_t battery_mv = 0;

// --- OTA STATE MACHINE ---
enum OtaState { OTA_IDLE, OTA_WAITING_NOTIFY, OTA_RECEIVING, OTA_VALIDATING, OTA_COMPLETE };
OtaState otaState = OTA_IDLE;
const esp_partition_t *update_partition = nullptr;
esp_ota_handle_t update_handle = 0;
mbedtls_sha256_context sha256_ctx;
uint32_t expected_firmware_size = 0;
uint32_t received_bytes = 0;
uint16_t expected_chunk_index = 0;
uint8_t expected_sha256[32];
unsigned long last_ota_activity = 0;
unsigned long ota_wake_time = 0;
bool ota_mode = false;

// Double-tap detection
unsigned long last_reset_press = 0;
uint8_t reset_tap_count = 0;
#define DOUBLE_TAP_WINDOW 500

esp_now_peer_info_t peerInfo;
esp_adc_cal_characteristics_t adc_chars;

void triggerLed(int duration) {
  if (breathingActive) return;
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
  ledActive = true;
  ledTimer = millis() + duration;
}

void ledBlink(int times, int delayMs) {
  if (breathingActive) return;
  for(int i = 0; i < times; i++) {
    digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
    delay(delayMs);
    digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
    delay(delayMs);
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
  
  if (LED_ACTIVE_HIGH) {
    ledcWrite(PIN_LED, breathingBrightness);
  } else {
    ledcWrite(PIN_LED, 255 - breathingBrightness);
  }
}

void startLedBreathing() {
  ledcAttach(PIN_LED, LED_PWM_FREQ, LED_PWM_RESOLUTION);
  breathingBrightness = 0;
  breathingDirection = 1;
  breathingTicker.attach_ms(30, updateBreathing);
  breathingActive = true;
}

void stopLedBreathing() {
  if (!breathingActive) return;
  breathingTicker.detach();
  ledcDetach(PIN_LED);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  breathingActive = false;
}

void saveGateways() { 
  prefs.putBytes("gw", &gateways, sizeof(gateways)); 
}

void addGateway(const uint8_t *mac) {
  for(int i = 0; i < gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return;
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.println("New Gateway Saved");
  }
}

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  Serial.printf("[OTA] ABORT: %s\n", reason);
  if (update_handle) {
    esp_ota_abort(update_handle);
    update_handle = 0;
  }
  otaState = OTA_IDLE;
  expected_chunk_index = 0;
  received_bytes = 0;
  ota_mode = false;
  stopLedBreathing();
  ledBlink(3, 100);
}

void handleOtaNotify(HueMixLinkPacket* pkt) {
  if (otaState != OTA_WAITING_NOTIFY) {
    Serial.println("[OTA] Not in OTA mode");
    return;
  }
  
  Serial.println("[OTA] NOTIFY received");
  
  expected_firmware_size = pkt->payload.otaNotify.firmware_size;
  memcpy(expected_sha256, pkt->payload.otaNotify.sha256_hash, 32);
  
  update_partition = esp_ota_get_next_update_partition(NULL);
  if (!update_partition) {
    Serial.println("[OTA] No update partition available");
    abortOta("No partition");
    return;
  }
  
  Serial.printf("[OTA] Starting update: %u bytes\n", expected_firmware_size);
  
  esp_err_t err = esp_ota_begin(update_partition, OTA_SIZE_UNKNOWN, &update_handle);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Begin failed: %d\n", err);
    abortOta("Begin failed");
    return;
  }
  
  mbedtls_sha256_init(&sha256_ctx);
  mbedtls_sha256_starts(&sha256_ctx, 0);
  
  otaState = OTA_RECEIVING;
  expected_chunk_index = 0;
  received_bytes = 0;
  last_ota_activity = millis();
  
  Serial.println("[OTA] Ready to receive chunks");
  startLedBreathing();
  
  // Send OTA_READY response with actual firmware size to confirm readiness
  HueMixLinkPacket ready;
  ready.type = PKT_OTA_READY;
  WiFi.macAddress(ready.sourceMAC);
  memset(ready.targetMAC, 0xFF, 6);
  ready.payload.otaReady.firmware_size = expected_firmware_size;
  ready.payload.otaReady.battery_mv = battery_mv;
  ready.signature = calculateHash((uint8_t*)&ready.payload, sizeof(Payload_OtaReady), HOME_ID);
  
  // Try sending to gateways sequentially
  bool sent = false;
  int successfulGatewayIndex = -1;
  
  for(int i = 0; i < gateways.count; i++) {
    if (!esp_now_is_peer_exist(gateways.macs[i])) {
      memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      esp_now_add_peer(&peerInfo);
    }
    
    if (esp_now_send(gateways.macs[i], (uint8_t*)&ready, sizeof(ready)) == ESP_OK) {
      sent = true;
      successfulGatewayIndex = i;
      Serial.printf("[OTA] Sent OTA_READY response via gateway %d\n", i);
      break;
    }
  }
  
  // Move successful gateway to front of list
  if (successfulGatewayIndex > 0) {
    uint8_t tempMac[6];
    memcpy(tempMac, gateways.macs[successfulGatewayIndex], 6);
    for(int j = successfulGatewayIndex; j > 0; j--) {
      memcpy(gateways.macs[j], gateways.macs[j-1], 6);
    }
    memcpy(gateways.macs[0], tempMac, 6);
    saveGateways();
  }
  
  if (!sent) {
    Serial.println("[OTA] Failed to send OTA_READY to any gateway");
  }
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  last_ota_activity = millis();
  ota_wake_time = millis();
  
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
  
  esp_err_t err = esp_ota_write(update_handle, pkt->payload.otaChunk.data, data_len);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Write failed at chunk %d: %d\n", chunk_idx, err);
    abortOta("Write failed");
    return;
  }
  
  mbedtls_sha256_update(&sha256_ctx, pkt->payload.otaChunk.data, data_len);
  
  received_bytes += data_len;
  expected_chunk_index++;
  
  if (chunk_idx % 50 == 0) {
    Serial.printf("[OTA] Progress: %u / %u bytes (%.1f%%)\n", 
      received_bytes, expected_firmware_size, 
      (received_bytes * 100.0) / expected_firmware_size);
    triggerLed(20);
  }
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  Serial.println("[OTA] COMPLETE received, validating...");
  otaState = OTA_VALIDATING;
  
  uint8_t calculated_sha256[32];
  mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
  mbedtls_sha256_free(&sha256_ctx);
  
  if (memcmp(calculated_sha256, expected_sha256, 32) != 0) {
    Serial.println("[OTA] SHA256 MISMATCH!");
    abortOta("SHA256 mismatch");
    return;
  }
  
  Serial.println("[OTA] SHA256 verified!");
  
  esp_err_t err = esp_ota_end(update_handle);
  if (err != ESP_OK) {
    Serial.printf("[OTA] End failed: %d\n", err);
    abortOta("End failed");
    return;
  }
  update_handle = 0;
  
  err = esp_ota_set_boot_partition(update_partition);
  if (err != ESP_OK) {
    Serial.printf("[OTA] Set boot partition failed: %d\n", err);
    abortOta("Set boot failed");
    return;
  }
  
  Serial.println("[OTA] UPDATE SUCCESSFUL! Rebooting...");
  otaState = OTA_COMPLETE;
  
  stopLedBreathing();
  ledBlink(10, 100);
  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  Serial.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

void handleOtaCheckpointReq(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  // Send checkpoint ACK with last successfully received chunk
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  Serial.printf("[OTA] Checkpoint request - last chunk received: %d\n", last_chunk);
  
  // Build OTA_CHUNK_ACK packet
  HueMixLinkPacket ack;
  ack.type = PKT_OTA_CHUNK_ACK;
  WiFi.macAddress(ack.sourceMAC);
  memset(ack.targetMAC, 0, 6);  // Server doesn't need target MAC
  ack.msgID = 0;
  ack.payload.otaChunkAck.last_chunk_index = last_chunk;
  ack.signature = calculateHash((uint8_t*)&ack.payload.otaChunkAck, 2, HOME_ID);
  
  // Try sending to gateways (not to pkt->sourceMAC which is Python server)
  bool sent = false;
  int successfulGatewayIndex = -1;
  
  for(int i = 0; i < gateways.count; i++) {
    if (!esp_now_is_peer_exist(gateways.macs[i])) {
      memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      esp_now_add_peer(&peerInfo);
    }
    
    if (esp_now_send(gateways.macs[i], (uint8_t*)&ack, sizeof(ack)) == ESP_OK) {
      sent = true;
      successfulGatewayIndex = i;
      Serial.printf("[OTA] Sent checkpoint ACK via gateway %d: last_chunk=%d\n", i, last_chunk);
      break;
    }
  }
  
  // Move successful gateway to front of list for future OTA packets
  if (successfulGatewayIndex > 0) {
    uint8_t tempMac[6];
    memcpy(tempMac, gateways.macs[successfulGatewayIndex], 6);
    for(int j = successfulGatewayIndex; j > 0; j--) {
      memcpy(gateways.macs[j], gateways.macs[j-1], 6);
    }
    memcpy(gateways.macs[0], tempMac, 6);
    saveGateways();
  }
  
  if (!sent) {
    Serial.println("[OTA] Failed to send checkpoint ACK to any gateway");
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if (len < sizeof(uint8_t)) return;
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;
  const uint8_t *mac = info->src_addr;

  // OTA Handling
  if (rx->type == PKT_OTA_NOTIFY) {
    handleOtaNotify(rx);
    lastActivityTime = millis();
    ota_wake_time = millis();
    return;
  } else if (rx->type == PKT_OTA_CHUNK) {
    handleOtaChunk(rx);
    lastActivityTime = millis();
    return;
  } else if (rx->type == PKT_OTA_CHECKPOINT_REQ) {
    handleOtaCheckpointReq(rx);
    return;
  } else if (rx->type == PKT_OTA_COMPLETE) {
    handleOtaComplete(rx);
    return;
  } else if (rx->type == PKT_OTA_ABORT) {
    handleOtaAbort(rx);
    return;
  }

  if (rx->type == PKT_PAIR_CONFIRM) {
    if (HOME_ID == 0) {
      uint32_t sig = calculateHash((uint8_t*)&rx->payload, sizeof(Payload_Pairing), 0);
      if (rx->signature == sig) {
        HOME_ID = rx->payload.pair.newHomeID;
        addGateway(mac);
        ackReceived = true;
        Serial.printf("PAIRED! ID: 0x%X\n", HOME_ID);
        lastActivityTime = millis();
        homeSetupDone = true;
      }
    }
  }
  else if (rx->type == PKT_ACK_TO_BTN) {
    ackReceived = true;
    if (rx->payload.gwList.count > 0) {
      // Security: Verify signature to ensure gateway list comes from trusted source with correct HOME_ID
      if (HOME_ID != 0) {
        uint32_t expected_sig = calculateHash((uint8_t*)&rx->payload.gwList, sizeof(Payload_GatewayList), HOME_ID);
        if (rx->signature != expected_sig) {
          Serial.printf("[REMOTE] SECURITY: Invalid signature on gateway list. Expected 0x%08X, got 0x%08X\n", expected_sig, rx->signature);
          Serial.println("[REMOTE] Rejected unauthorized gateway list");
          return;
        }
      }
      
      Serial.printf("[REMOTE] Server sent %d gateways:\n", rx->payload.gwList.count);
      for(int i = 0; i < rx->payload.gwList.count; i++) {
        Serial.printf("  [%d] %02X:%02X:%02X:%02X:%02X:%02X\n", i,
          rx->payload.gwList.macs[i][0], rx->payload.gwList.macs[i][1],
          rx->payload.gwList.macs[i][2], rx->payload.gwList.macs[i][3],
          rx->payload.gwList.macs[i][4], rx->payload.gwList.macs[i][5]);
      }

      // Merge new gateway list while preserving successful order
      Payload_GatewayList newList;
      newList.count = 0;

      // First, add existing gateways that are still in the new list (preserve order)
      for(int i = 0; i < gateways.count && newList.count < MAX_GATEWAYS; i++) {
        bool stillExists = false;
        for(int j = 0; j < rx->payload.gwList.count; j++) {
          if (memcmp(gateways.macs[i], rx->payload.gwList.macs[j], 6) == 0) {
            stillExists = true;
            break;
          }
        }
        if (stillExists) {
          memcpy(newList.macs[newList.count], gateways.macs[i], 6);
          newList.count++;
        }
      }

      // Then, add any new gateways from server that we don't have yet
      for(int i = 0; i < rx->payload.gwList.count && newList.count < MAX_GATEWAYS; i++) {
        bool isNew = true;
        for(int j = 0; j < newList.count; j++) {
          if (memcmp(rx->payload.gwList.macs[i], newList.macs[j], 6) == 0) {
            isNew = false;
            break;
          }
        }
        if (isNew) {
          memcpy(newList.macs[newList.count], rx->payload.gwList.macs[i], 6);
          newList.count++;
        }
      }

      gateways = newList;
      saveGateways();
    }
  }
}

void getBatteryVoltage() {
  // Read calibrated battery voltage from ADC pin 35 with voltage divider
  // Divider: 1MΩ and 270kΩ, ratio = 1300/300 ≈ 4.333
  uint32_t raw = analogRead(PIN_BATTERY);
  uint32_t vout_mv = esp_adc_cal_raw_to_voltage(raw, &adc_chars);
  battery_mv = (vout_mv * 1300) / 300;
}

void sendPacket(uint8_t type, uint8_t action, uint8_t buttonIndex) {
  HueMixLinkPacket pkt;
  pkt.type = type;
  pkt.msgID = 0;
  memset(pkt.targetMAC, 0, 6);
  WiFi.macAddress(pkt.sourceMAC);

  if (type == PKT_BTN_EVENT) {
    pkt.payload.btn.action = action;
    pkt.payload.btn.battery_mv = battery_mv;
    pkt.payload.btn.button_index = buttonIndex;
    
    // Parse firmware version
    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.btn.version_major = major;
      pkt.payload.btn.version_minor = minor;
      pkt.payload.btn.version_patch = patch;
    #else
      pkt.payload.btn.version_major = 0;
      pkt.payload.btn.version_minor = 0;
      pkt.payload.btn.version_patch = 0;
    #endif
    
    // Set platform
    #if defined(ESP8266)
      pkt.payload.btn.platform = 1;
    #else
      pkt.payload.btn.platform = 0;
    #endif
    
    pkt.signature = calculateHash((uint8_t*)&pkt.payload, sizeof(Payload_Button), HOME_ID);
  } else if (type == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_REMOTE;
    pkt.payload.raw[1] = 0;  // Reserved for RSSI (gateway fills this in)
    
    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.raw[2] = major;
      pkt.payload.raw[3] = minor;
      pkt.payload.raw[4] = patch;
      pkt.payload.raw[5] = 0; // build number
    #else
      pkt.payload.raw[2] = 0;
      pkt.payload.raw[3] = 0;
      pkt.payload.raw[4] = 0;
      pkt.payload.raw[5] = 0;
    #endif
    
    // Use HOME_ID for signature if paired, 0 if unpaired
    pkt.signature = calculateHash(pkt.payload.raw, 5, HOME_ID);
  }

  ackReceived = false;
  bool sent = false;
  int successfulGatewayIndex = -1;

  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    Serial.printf("[REMOTE] Trying %d gateways for packet type 0x%02X action %d button %d\n", 
      gateways.count, type, action, buttonIndex);

    for(int i = 0; i < gateways.count; i++) {
      Serial.printf("[REMOTE] Attempt %d/%d: %02X:%02X:%02X:%02X:%02X:%02X\n", i+1, gateways.count,
        gateways.macs[i][0], gateways.macs[i][1], gateways.macs[i][2],
        gateways.macs[i][3], gateways.macs[i][4], gateways.macs[i][5]);

      memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
      if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
        unsigned long w = millis();
        while(millis() - w < 75 && !ackReceived) delay(1);
        if (ackReceived) {
          Serial.println("[REMOTE]   ACK received!");
          sent = true;
          successfulGatewayIndex = i;
          break;
        }
      }
    }

    // Move successful gateway to front of list
    if (successfulGatewayIndex > 0) {
      Serial.printf("[REMOTE] Moving gateway %d to front\n", successfulGatewayIndex);
      uint8_t tempMac[6];
      memcpy(tempMac, gateways.macs[successfulGatewayIndex], 6);
      // Shift all entries before it down by one
      for(int j = successfulGatewayIndex; j > 0; j--) {
        memcpy(gateways.macs[j], gateways.macs[j-1], 6);
      }
      // Place successful gateway at index 0
      memcpy(gateways.macs[0], tempMac, 6);
      saveGateways();
    }

    if (action != ACT_SYNC) {
      if (sent) {
        triggerLed(50);
      } else {
        ledBlink(2, 100);
      }
    }
  }
  // B. UNPAIRED MODE
  else if (HOME_ID == 0) {
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
    unsigned long w = millis();
    while(millis() - w < 500 && !ackReceived) delay(1);
    if(!ackReceived) ledBlink(2, 200);
  }
}

void goToSleep() {
  delay(100);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  Serial.println("Going to sleep with EXT1 wakeup on buttons 0-3 and reset");
  Serial.flush();

  // Enable EXT1 wakeup on all 4 buttons + reset (HIGH level)
  esp_sleep_enable_ext1_wakeup(EXT1_BUTTON_MASK, ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

int getWakeupButtonIndex() {
  uint64_t wakeup_pin_mask = esp_sleep_get_ext1_wakeup_status();
  int pin = __builtin_ctzll(wakeup_pin_mask);
  
  if (pin == PIN_RESET) {
    return -1;
  }
  
  if (pin == PIN_BTN0) return 0;
  if (pin == PIN_BTN1) return 1;
  if (pin == PIN_BTN2) return 2;
  if (pin == PIN_BTN3) return 3;
  
  return -1;
}

void setup() {
  // Initialize all button pins as inputs with pull-down (already on hardware)
  for(int i = 0; i < 4; i++) {
    pinMode(buttonPins[i], INPUT);
  }
  pinMode(PIN_RESET, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  
  // Initialize ADC for battery reading with calibration
  analogSetAttenuation(ADC_2_5db);
  analogSetWidth(12);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_2_5, ADC_WIDTH_BIT_12, 1100, &adc_chars);
  getBatteryVoltage();

  Serial.begin(115200);
  Serial.println("\n--- REMOTE CONTROL WAKE (EXT1) ---");

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));

  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();

  esp_now_register_recv_cb(OnDataRecv);

  // Initialize button debouncing
  for(int i = 0; i < 4; i++) {
    buttons[i].attach(buttonPins[i], INPUT);
    buttons[i].interval(25);
  }

  lastActivityTime = millis();

  // Check wakeup reason
  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  if(wakeupReason == ESP_SLEEP_WAKEUP_EXT1) {
    wakeupExt1 = true;
    wakeupButtonIndex = getWakeupButtonIndex();
    if (wakeupButtonIndex == -1) {
      wakeupWasReset = true;
      Serial.println("Wakeup caused by RESET button");
    } else {
      Serial.printf("Wakeup caused by button %d\n", wakeupButtonIndex);
    }
  } else {
    Serial.println("Cold boot");
    
    // Check if we just rebooted from software reset (e.g., after OTA)
    esp_reset_reason_t reset_reason = esp_reset_reason();
    if (reset_reason == ESP_RST_SW && HOME_ID != 0 && gateways.count > 0) {
      Serial.println("[OTA] Software reset detected - sending HELLO with new version");
      
      // Send HELLO with new version to confirm OTA success
      sendPacket(PKT_HELLO, 0, 0);
    }
    
    ledBlink(5, 100);
    goToSleep();
  }
}

void loop() {
  // OTA timeout check
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    Serial.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  // OTA wake timeout
  if (otaState == OTA_WAITING_NOTIFY && millis() - ota_wake_time > 30000) {
    Serial.println("[OTA] No NOTIFY received within 30s, returning to normal");
    otaState = OTA_IDLE;
    ota_mode = false;
  }

  // Update all buttons
  for(int i = 0; i < 4; i++) {
    buttons[i].update();
  }

  // Handle LED blinking
  if (ledActive) {
    if (millis() > ledTimer) {
      digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
      ledActive = false;
    }
  }

  // Handle reset pin from EXT1 wakeup
  if (wakeupExt1) {
    if (wakeupWasReset) {
      wakeupWasReset = false;
      lastActivityTime = millis();
    }
    else if (wakeupButtonIndex >= 0) {
      if (digitalRead(buttonPins[wakeupButtonIndex]) == LOW) {
        // Button was released during wakeup
        if (HOME_ID == 0) {
          sendPacket(PKT_HELLO, 0, wakeupButtonIndex);
          Serial.printf("Button %d press (pair request)\n", wakeupButtonIndex);
        } else {
          sendPacket(PKT_BTN_EVENT, ACT_CLICK, wakeupButtonIndex);
        }
        buttonStates[wakeupButtonIndex].pressed = false;
      } else {
        // Still holding button, continue to normal state
        buttonStates[wakeupButtonIndex].pressed = true;
        buttonStates[wakeupButtonIndex].holdStartTime = millis();
      }
    }
    wakeupExt1 = false;
    lastActivityTime = millis();
  }

  // Check for button state changes
  for(int i = 0; i < 4; i++) {
    // Button pressed
    if (buttons[i].rose()) {
      buttonStates[i].pressed = true;
      buttonStates[i].holdStartTime = millis();
      lastActivityTime = millis();
    }

    // Button is being held
    if (buttons[i].read() == HIGH && buttonStates[i].pressed && !buttonStates[i].isHolding) {
      if (millis() - buttonStates[i].holdStartTime >= HOLD_TIME) {
        buttonStates[i].isHolding = true;
        if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING, i);
        buttonStates[i].holdingIntervalUpdate = millis();
      }
    }

    // Repeat holding signal while button held
    if (millis() - buttonStates[i].holdingIntervalUpdate >= HOLD_INTERVAL && buttonStates[i].isHolding) {
      if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING, i);
      buttonStates[i].holdingIntervalUpdate = millis();
      lastActivityTime = millis();
    }

    // Button released
    if (buttons[i].fell()) {
      if (buttonStates[i].isHolding) {
        buttonStates[i].isHolding = false;
        if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_RELEASE, i);
        Serial.printf("Button %d holding stopped\n", i);
      } else if (buttonStates[i].pressed) {
        if (HOME_ID == 0) {
          sendPacket(PKT_HELLO, 0, i);
          Serial.printf("Button %d press (pair request)\n", i);
        } else {
          sendPacket(PKT_BTN_EVENT, ACT_CLICK, i);
          Serial.printf("Button %d click\n", i);
        }
      }
      buttonStates[i].pressed = false;
      lastActivityTime = millis();
    }
  }

  // FACTORY RESET using RESET pin (5s hold) OR OTA MODE (double-tap)
  static bool reset_button_was_high = false;
  
  // Detect rising edge (button pressed)
  if (digitalRead(PIN_RESET) == HIGH && !reset_button_was_high) {
    unsigned long now = millis();
    
    // Check if this is within double-tap window
    if (now - last_reset_press < DOUBLE_TAP_WINDOW) {
      reset_tap_count++;
    } else {
      reset_tap_count = 1;
    }
    last_reset_press = now;
  }
  
  // Detect falling edge (button released)
  if (digitalRead(PIN_RESET) == LOW && reset_button_was_high) {
    // Second tap released - enter OTA mode
    if (reset_tap_count >= 2 && !ota_mode) {
      Serial.println("[OTA] Double-tap detected! Entering OTA mode...");
      otaState = OTA_WAITING_NOTIFY;
      ota_mode = true;
      ota_wake_time = millis();
      lastActivityTime = millis();
      ledBlink(3, 200);
      
      // Send OTA_READY packet with firmware_size=0 to announce OTA mode
      HueMixLinkPacket ready;
      ready.type = PKT_OTA_READY;
      WiFi.macAddress(ready.sourceMAC);
      memset(ready.targetMAC, 0xFF, 6);
      ready.payload.otaReady.firmware_size = 0;
      ready.payload.otaReady.battery_mv = battery_mv;
      ready.signature = calculateHash((uint8_t*)&ready.payload, sizeof(Payload_OtaReady), HOME_ID);
      
      // Try sending to gateways sequentially
      bool sent = false;
      int successfulGatewayIndex = -1;
      
      for(int i = 0; i < gateways.count; i++) {
        if (!esp_now_is_peer_exist(gateways.macs[i])) {
          memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
          peerInfo.channel = 0;
          peerInfo.encrypt = false;
          esp_now_add_peer(&peerInfo);
        }
        
        if (esp_now_send(gateways.macs[i], (uint8_t*)&ready, sizeof(ready)) == ESP_OK) {
          sent = true;
          successfulGatewayIndex = i;
          Serial.printf("[OTA] Sent OTA_READY announcement via gateway %d\n", i);
          break;
        }
      }
      
      // Move successful gateway to front of list
      if (successfulGatewayIndex > 0) {
        uint8_t tempMac[6];
        memcpy(tempMac, gateways.macs[successfulGatewayIndex], 6);
        for(int j = successfulGatewayIndex; j > 0; j--) {
          memcpy(gateways.macs[j], gateways.macs[j-1], 6);
        }
        memcpy(gateways.macs[0], tempMac, 6);
        saveGateways();
      }
      
      reset_tap_count = 0;
    }
  }
  
  reset_button_was_high = (digitalRead(PIN_RESET) == HIGH);
  
  if (digitalRead(PIN_RESET) == HIGH && !ota_mode) {
    lastActivityTime = millis();
    unsigned long holdStart = millis();
    while(digitalRead(PIN_RESET) == HIGH) {
      if (millis() - holdStart > 5000) {
        Serial.println("Factory reset initiated...");
        ledBlink(10, 50);
        prefs.clear();
        HOME_ID = 0;
        gateways.count = 0;
        Serial.println("Reset complete, restarting...");
        ESP.restart();
      }
      delay(10);
    }
  }

  if (homeSetupDone) {
    prefs.putUInt("hid", HOME_ID);
    ledBlink(5, 50);
    Serial.println("Paired! Requesting Full Gateway List...");
    sendPacket(PKT_BTN_EVENT, ACT_SYNC, 0);
    lastActivityTime = millis();
    homeSetupDone = false;
  }

  // If been idle long enough, go to sleep (but not in OTA mode)
  if (!ota_mode && millis() - lastActivityTime > SLEEP_TIMEOUT) {
    // Make sure all buttons are released
    bool allReleased = true;
    for(int i = 0; i < 4; i++) {
      if (buttons[i].read() == HIGH) {
        allReleased = false;
        break;
      }
    }
    // Also check reset pin is released
    if (allReleased && digitalRead(PIN_RESET) == LOW) {
      goToSleep();
    }
  }

  delay(5);
}
