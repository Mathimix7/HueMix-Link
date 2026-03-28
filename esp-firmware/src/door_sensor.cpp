/* 
  HUEMIXLINK V3 - DOOR SENSOR FIRMWARE
  Supports: ESP32 only

  Sleep Strategy:
  - Wake from reed state transition (EXT0) → Send OPEN/CLOSED event
  - Always keep RESET button wake via EXT1
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_adc_cal.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_system.h>
#include "mbedtls/sha256.h"
#include <Ticker.h>

// GPIO pins
#define PIN_REED     26   // NC reed input: HIGH=door open (magnet far), LOW=door closed (magnet near)
#define PIN_RESET    14   // Factory reset button
#define PIN_LED      18   // Status LED
#define PIN_BATTERY  35   // Battery voltage ADC
#define PIN_LDR      34   // LDR light sensor (analog input)
#define PIN_LDR_POWER 33  // LDR power control (HIGH to enable, LOW to save battery)

#define LED_ACTIVE_HIGH HIGH
#define DOOR_OPEN_LEVEL HIGH
#define DOOR_CLOSED_LEVEL LOW

// Debounce period for reed sampling after wakeup
#define REED_DEBOUNCE_MS 15

// After sending door event, delay before sleep (allow network transmission)
#define TX_SETTLE_TIME 100

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

unsigned long lastActivityTime = 0;
volatile bool ackReceived = false;
bool homeSetupDone = false;
uint16_t battery_mv = 0;
uint8_t light_level = 0;

RTC_DATA_ATTR uint8_t lastReedLevel = 0xFF;
uint8_t pendingDoorAction = ACT_SYNC;

// Wakeup tracking
bool wakeupFromDoor = false;
bool wakeupFromReset = false;

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

// Double-tap detection for OTA mode
unsigned long last_reset_press = 0;
uint8_t reset_tap_count = 0;
#define DOUBLE_TAP_WINDOW 1000

// Pending single press (wait to see if double-tap comes)
unsigned long pending_single_press_time = 0;
bool pending_single_press = false;
#define SINGLE_PRESS_DELAY 500  // Wait 500ms after first tap before acting

esp_now_peer_info_t peerInfo;
esp_adc_cal_characteristics_t adc_chars_battery;
esp_adc_cal_characteristics_t adc_chars_ldr;

// --- LED FUNCTIONS ---
void triggerLed(int duration) {
  if (breathingActive) return;
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
  delay(duration);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  lastActivityTime = millis();
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

// --- GATEWAY MANAGEMENT ---
void saveGateways() { 
  prefs.putBytes("gw", &gateways, sizeof(gateways)); 
}

void addGateway(const uint8_t *mac) {
  for(int i=0; i<gateways.count; i++) {
    if(memcmp(gateways.macs[i], mac, 6) == 0) return;
  }
  if(gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count++], mac, 6);
    saveGateways();
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
  memset(&ready, 0, sizeof(HueMixLinkPacket));
  ready.type = PKT_OTA_READY;
  WiFi.macAddress(ready.sourceMAC);
  memset(ready.targetMAC, 0xFF, 6);
  ready.payload.otaReady.firmware_size = expected_firmware_size;
  ready.payload.otaReady.battery_mv = battery_mv;
  ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
  
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
  memset(&ack, 0, sizeof(HueMixLinkPacket));
  ack.type = PKT_OTA_CHUNK_ACK;
  WiFi.macAddress(ack.sourceMAC);
  memset(ack.targetMAC, 0, 6);  // Server doesn't need target MAC
  ack.msgID = 0;
  ack.payload.otaChunkAck.last_chunk_index = last_chunk;
  ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
  
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

// --- BATTERY MONITORING ---
void getBatteryVoltage() {
  // Ensure attenuation is set for this pin before reading
  analogSetPinAttenuation(PIN_BATTERY, ADC_2_5db);
  
  uint32_t raw = 0;
  for(int i = 0; i < 10; i++) {
    raw += analogRead(PIN_BATTERY);
    delay(5);
  }
  raw /= 10;
  
  uint32_t voltage = esp_adc_cal_raw_to_voltage(raw, &adc_chars_battery);
  battery_mv = (voltage * 1300) / 300;
  
  Serial.printf("[BATTERY] Raw ADC: %lu, ADC voltage: %lu mV, Calculated battery: %u mV\n", raw, voltage, battery_mv);
}

uint8_t readReedLevelStable() {
  uint8_t initial = digitalRead(PIN_REED);
  delay(REED_DEBOUNCE_MS);
  uint8_t second = digitalRead(PIN_REED);
  if (initial == second) return second;
  delay(REED_DEBOUNCE_MS);
  return digitalRead(PIN_REED);
}

// --- LDR LIGHT SENSOR ---
void getLightLevel() {
  // Ensure attenuation is set for this pin before reading
  analogSetPinAttenuation(PIN_LDR, ADC_11db);
  
  // Power on LDR sensor
  digitalWrite(PIN_LDR_POWER, HIGH);
  delay(10);  // Allow sensor to stabilize
  
  uint32_t raw = 0;
  const uint32_t light_min_mv = 150;
  const uint32_t light_max_mv = 2400;

  for (int i = 0; i < 10; i++) {
    raw += analogRead(PIN_LDR);
    delay(5);
  }
  raw /= 10;

  uint32_t voltage = esp_adc_cal_raw_to_voltage(raw, &adc_chars_ldr);
  if (voltage < light_min_mv) voltage = light_min_mv;
  if (voltage > light_max_mv) voltage = light_max_mv;

  float normalized = (float)(voltage - light_min_mv) / (light_max_mv - light_min_mv);
  float logScaled = log10(1 + 9 * normalized) / log10(10);

  light_level = 10 - roundf(logScaled * 10.0f);
  Serial.printf("Voltage: %u mV, LightLevel: %d\n", voltage, light_level);  
  // Power off LDR sensor to save battery
  digitalWrite(PIN_LDR_POWER, LOW);
}

uint8_t actionFromReedLevel(uint8_t reedLevel) {
  return (reedLevel == DOOR_OPEN_LEVEL) ? ACT_DOOR_OPENED : ACT_DOOR_CLOSED;
}

// --- PACKET SENDING ---
void sendPacket(uint8_t packetType, uint8_t action) {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = packetType;
  pkt.msgID = 0;
  memset(pkt.targetMAC, 0, 6);
  WiFi.macAddress(pkt.sourceMAC);

  if (packetType == PKT_DOOR_EVENT) {
    pkt.payload.door.action = action;
    pkt.payload.door.battery_mv = battery_mv;
    pkt.payload.door.light_level = light_level;

    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.door.version_major = major;
      pkt.payload.door.version_minor = minor;
      pkt.payload.door.version_patch = patch;
    #else
      pkt.payload.door.version_major = 0;
      pkt.payload.door.version_minor = 0;
      pkt.payload.door.version_patch = 0;
    #endif

    #if defined(ESP8266)
      pkt.payload.door.platform = 1;
    #else
      pkt.payload.door.platform = 0;
    #endif

    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  } else if (packetType == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_DOOR;
    pkt.payload.raw[1] = 0;  // Reserved for RSSI (gateway fills this in)

    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.raw[2] = major;
      pkt.payload.raw[3] = minor;
      pkt.payload.raw[4] = patch;
    #else
      pkt.payload.raw[2] = 0;
      pkt.payload.raw[3] = 0;
      pkt.payload.raw[4] = 0;
    #endif

    #if defined(ESP8266)
      pkt.payload.raw[5] = 1;
    #else
      pkt.payload.raw[5] = 0;
    #endif

    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  }

  ackReceived = false;
  bool sent = false;
  int successfulGatewayIndex = -1;

  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    for(int i = 0; i < gateways.count; i++) {
      Serial.printf("[DOOR] Attempt %d/%d: %02X:%02X:%02X:%02X:%02X:%02X\n", i+1, gateways.count,
        gateways.macs[i][0], gateways.macs[i][1], gateways.macs[i][2],
        gateways.macs[i][3], gateways.macs[i][4], gateways.macs[i][5]);

      if (!esp_now_is_peer_exist(gateways.macs[i])) {
        memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
        peerInfo.channel = 0;
        peerInfo.encrypt = false;
        esp_now_add_peer(&peerInfo);
      }
      
      if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
        unsigned long w = millis();
        while(millis() - w < 75 && !ackReceived) delay(1);
        if (ackReceived) {
          Serial.println("[DOOR]   ACK received!");
          sent = true;
          successfulGatewayIndex = i;
          break;
        }
      }
    }
    
    // Move successful gateway to front
    if (successfulGatewayIndex > 0) {
      uint8_t tempMac[6];
      memcpy(tempMac, gateways.macs[successfulGatewayIndex], 6);
      for(int j = successfulGatewayIndex; j > 0; j--) {
        memcpy(gateways.macs[j], gateways.macs[j-1], 6);
      }
      memcpy(gateways.macs[0], tempMac, 6);
      saveGateways();
    }

    if (action != ACT_SYNC) {
      if (sent) {
        triggerLed(100);
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

    if (packetType == PKT_HELLO) {
      // Use the same retry + short ACK wait strategy for pairing HELLO.
      Serial.printf("[DOOR][HELLO] Broadcast");
      if (esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
        unsigned long w = millis();
        while(millis() - w < 500 && !ackReceived) delay(1);
        if(!ackReceived) ledBlink(2, 200);
      }
    }
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if(len != sizeof(HueMixLinkPacket)) return;
  
  HueMixLinkPacket pkt;
  memcpy(&pkt, data, sizeof(pkt));
  const uint8_t *mac = info->src_addr;
  
  uint32_t expectedSig = calculateHash(pkt.payload.raw, 185, HOME_ID);
  if(pkt.signature != expectedSig && HOME_ID != 0) return;
  
  lastActivityTime = millis();
  
  if (pkt.type == PKT_PAIR_CONFIRM) {
    if(HOME_ID == 0) {
      uint32_t sig = calculateHash(pkt.payload.raw, 185, 0);
      if (pkt.signature == sig) {
        HOME_ID = pkt.payload.pair.newHomeID;
        addGateway(mac);
        ackReceived = true;
        Serial.printf("PAIRED! ID: 0x%X\n", HOME_ID);
        lastActivityTime = millis();
        homeSetupDone = true;
      }
    }
  }
  else if (pkt.type == PKT_ACK_TO_BTN) {
    ackReceived = true;
    if (pkt.payload.gwList.count > 0) {
      // Security: Verify signature to ensure gateway list comes from trusted source with correct HOME_ID
      if (HOME_ID != 0) {
        uint32_t expected_sig = calculateHash(pkt.payload.raw, 185, HOME_ID);
        if (pkt.signature != expected_sig) {
          Serial.printf("[DOOR] SECURITY: Invalid signature on gateway list. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt.signature);
          Serial.println("[DOOR] Rejected unauthorized gateway list");
          return;
        }
      }
      
      Serial.printf("[DOOR] Server sent %d gateways:\n", pkt.payload.gwList.count);
      for(int i = 0; i < pkt.payload.gwList.count; i++) {
        Serial.printf("  [%d] %02X:%02X:%02X:%02X:%02X:%02X\n", i,
          pkt.payload.gwList.macs[i][0], pkt.payload.gwList.macs[i][1],
          pkt.payload.gwList.macs[i][2], pkt.payload.gwList.macs[i][3],
          pkt.payload.gwList.macs[i][4], pkt.payload.gwList.macs[i][5]);
      }

      // Merge new gateway list while preserving successful order
      Payload_GatewayList newList;
      newList.count = 0;

      // First, add existing gateways that are still in the new list (preserve order)
      for(int i = 0; i < gateways.count && newList.count < MAX_GATEWAYS; i++) {
        bool stillExists = false;
        for(int j = 0; j < pkt.payload.gwList.count; j++) {
          if (memcmp(gateways.macs[i], pkt.payload.gwList.macs[j], 6) == 0) {
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
      for(int i = 0; i < pkt.payload.gwList.count && newList.count < MAX_GATEWAYS; i++) {
        bool isNew = true;
        for(int j = 0; j < newList.count; j++) {
          if (memcmp(pkt.payload.gwList.macs[i], newList.macs[j], 6) == 0) {
            isNew = false;
            break;
          }
        }
        if (isNew) {
          memcpy(newList.macs[newList.count], pkt.payload.gwList.macs[i], 6);
          newList.count++;
        }
      }

      gateways = newList;
      saveGateways();
    }
  }
  else if (pkt.type == PKT_GW_LIST_UPD) {
    memcpy(&gateways, &pkt.payload.gwList, sizeof(Payload_GatewayList));
    saveGateways();
    Serial.printf("[GW] Gateway list updated: %d gateways\n", gateways.count);
  }
  else if (pkt.type == PKT_SYS_CMD) {
  }
  // OTA Handling
  // Security: Verify all OTA packets before processing
  if (pkt.type == PKT_OTA_NOTIFY || pkt.type == PKT_OTA_CHUNK || 
      pkt.type == PKT_OTA_CHECKPOINT_REQ || pkt.type == PKT_OTA_COMPLETE || 
      pkt.type == PKT_OTA_ABORT) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(pkt.payload.raw, 185, HOME_ID);
      if (pkt.signature != expected_sig) {
        Serial.printf("[DOOR] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt.signature);
        Serial.println("[DOOR] Rejected unauthorized OTA packet");
        return;
      }
    }
  }
  
  if (pkt.type == PKT_OTA_NOTIFY) {
    handleOtaNotify(&pkt);
    lastActivityTime = millis();
    ota_wake_time = millis();
    return;
  } 
  else if (pkt.type == PKT_OTA_CHUNK) {
    handleOtaChunk(&pkt);
    lastActivityTime = millis();
    return;
  } 
  else if (pkt.type == PKT_OTA_CHECKPOINT_REQ) {
    handleOtaCheckpointReq(&pkt);
    return;
  } 
  else if (pkt.type == PKT_OTA_COMPLETE) {
    handleOtaComplete(&pkt);
    return;
  } 
  else if (pkt.type == PKT_OTA_ABORT) {
    handleOtaAbort(&pkt);
    return;
  }
}

// --- SLEEP FUNCTIONS ---
void goToSleepDoor() {
  uint8_t reedLevel = readReedLevelStable();
  lastReedLevel = reedLevel;

  // NC reed wiring in this project:
  // HIGH = door open (magnet far), LOW = door closed (magnet near)
  // Configure EXT0 to wake on the opposite level so we only wake on a real transition.
  int ext0WakeLevel = (reedLevel == DOOR_OPEN_LEVEL) ? 0 : 1;
  const char* currentState = (reedLevel == DOOR_OPEN_LEVEL) ? "OPEN" : "CLOSED";
  const char* nextWakeState = (ext0WakeLevel == 1) ? "OPEN" : "CLOSED";

  Serial.printf("Going to sleep (door=%s), EXT0 wakes on door=%s\n", currentState, nextWakeState);
  Serial.flush();

  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();

  esp_sleep_enable_ext0_wakeup((gpio_num_t)PIN_REED, ext0WakeLevel);
  // Keep EXT1 wake for reset button exactly as requested.
  esp_sleep_enable_ext1_wakeup((1ULL << PIN_RESET), ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

int getWakeupPin() {
  uint64_t wakeup_pin_mask = esp_sleep_get_ext1_wakeup_status();
  if (wakeup_pin_mask == 0) return -1;
  return __builtin_ctzll(wakeup_pin_mask);
}

// --- SETUP ---
void setup() {
  // Initialize Serial FIRST so we can see debug output
  Serial.begin(115200);
  Serial.println("\n--- DOOR SENSOR WAKE ---");
  
  pinMode(PIN_REED, INPUT);
  pinMode(PIN_RESET, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  analogRead(PIN_BATTERY);  // Dummy read to initialize ADC
  analogRead(PIN_LDR);      // Dummy read to initialize ADC
  
  // LDR power control - keep OFF to save battery
  pinMode(PIN_LDR_POWER, OUTPUT);
  digitalWrite(PIN_LDR_POWER, LOW);
  
  // Initialize ADC (12-bit resolution = 0-4095)
  analogSetWidth(12);
  
  // Set pin attenuations BEFORE characterization and reading
  analogSetPinAttenuation(PIN_BATTERY, ADC_2_5db);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_2_5, ADC_WIDTH_BIT_12, 1100, &adc_chars_battery);

  analogSetPinAttenuation(PIN_LDR, ADC_11db);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, 1100, &adc_chars_ldr);
  
  getBatteryVoltage();
  Serial.println("[SETUP] ADC initialization complete");
  
  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));
  if (lastReedLevel == 0xFF) {
    lastReedLevel = readReedLevelStable();
  }

  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();
  
  esp_now_register_recv_cb(OnDataRecv);
  
  // Initialize peer info structure
  memset(&peerInfo, 0, sizeof(peerInfo));
  
  lastActivityTime = millis();
  
  // Check wakeup reason
  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  
  if (wakeupReason == ESP_SLEEP_WAKEUP_EXT0) {
    wakeupFromDoor = true;
    Serial.println("Wakeup: Reed transition (EXT0)");
  } else if (wakeupReason == ESP_SLEEP_WAKEUP_EXT1) {
    // Woke from RESET button
    int wakeupPin = getWakeupPin();

    if (wakeupPin == PIN_RESET) {
      wakeupFromReset = true;
      Serial.println("Wakeup: RESET button");
      // Handle reset button in main loop
    }
  } else {
    // Cold boot
    Serial.println("Cold boot");
    
    // Check if we just rebooted from software reset (e.g., after OTA)
    esp_reset_reason_t reset_reason = esp_reset_reason();
    if (reset_reason == ESP_RST_SW && HOME_ID != 0 && gateways.count > 0) {
      Serial.println("[OTA] Software reset detected - sending HELLO with new version");
      delay(100);
      sendPacket(PKT_HELLO, 0);
      delay(200);
    } 
    ledBlink(5, 100);
    goToSleepDoor();
  }
  
  Serial.printf("HOME_ID: 0x%08X\n", HOME_ID);
  Serial.printf("Battery: %d mV\n", battery_mv);
  Serial.printf("Gateways: %d\n", gateways.count);
  Serial.printf("Door state: %s\n", (lastReedLevel == DOOR_OPEN_LEVEL) ? "OPEN" : "CLOSED");
}

void loop() {
  if (wakeupFromReset) {
    wakeupFromReset = false;
    if (digitalRead(PIN_RESET) == LOW && !ota_mode) {
      unsigned long now = millis();
      reset_tap_count = 1;
      last_reset_press = now;
      pending_single_press = true;
      pending_single_press_time = now;
      lastActivityTime = now;
      Serial.println("[RESET] Wake-from-reset tap latched, waiting for possible second tap...");
    }
  }

  // Handle reed state transition event (from EXT0 wakeup)
  if (wakeupFromDoor) {
    wakeupFromDoor = false;

    // Read light level from LDR sensor
    getLightLevel();
    Serial.printf("Light level: %d\n", light_level);

    uint8_t currentReedLevel = readReedLevelStable();
    if (currentReedLevel != lastReedLevel) {
      pendingDoorAction = actionFromReedLevel(currentReedLevel);
      lastReedLevel = currentReedLevel;

      if (HOME_ID == 0) {
        // Unpaired - send HELLO packet on wakeup to allow pairing.
        sendPacket(PKT_HELLO, 0);
        Serial.println("Door transition (pair request)");
      } else {
        // Paired - send door opened/closed event.
        sendPacket(PKT_DOOR_EVENT, pendingDoorAction);
        Serial.printf("Door transition (paired): %s\n", pendingDoorAction == ACT_DOOR_OPENED ? "OPENED" : "CLOSED");
      }

      // Wait for transmission to complete
      delay(TX_SETTLE_TIME);

      // If pairing just happened, wait for gateway list and save
      if (homeSetupDone) {
        Serial.println("Pairing detected, waiting for gateway list...");
        delay(200); // Give time for gateway list to arrive

        prefs.putUInt("hid", HOME_ID);
        ledBlink(5, 50);
        Serial.println("Paired! Requesting Full Gateway List...");
        sendPacket(PKT_DOOR_EVENT, ACT_SYNC);
        delay(100);
        homeSetupDone = false;
      }
    } else {
      Serial.println("[DOOR] Wakeup without stable state change (debounced)");
    }
  }
  
  // Handle OTA mode timeouts
  if (ota_mode) {
    if (otaState == OTA_WAITING_NOTIFY) {
      // Waiting for NOTIFY packet - timeout after 30 seconds
      if (millis() - ota_wake_time > 30000) {
        Serial.println("[OTA] No NOTIFY received within 30s, returning to normal");
        abortOta("NOTIFY timeout");
        goToSleepDoor();
      }
    } else if (otaState == OTA_RECEIVING) {
      // Actively receiving chunks - timeout if no activity for 30 seconds
      if (millis() - last_ota_activity > 30000) {
        Serial.println("[OTA] No chunk received for 30s, aborting");
        abortOta("Chunk timeout");
        goToSleepDoor();
      }
      // Overall OTA timeout - 10 minutes from start
      if (millis() - ota_wake_time > 600000) {
        Serial.println("[OTA] Overall timeout (10 min), aborting");
        abortOta("Overall timeout");
        goToSleepDoor();
      }
    }
  }
  
  // Handle RESET button: single press (HELLO), double-tap (OTA), 5s hold (factory reset)
  static bool reset_button_was_high = false;
  static unsigned long reset_press_start = 0;
  
  if (digitalRead(PIN_RESET) == HIGH && !reset_button_was_high) {
    unsigned long now = millis();
    reset_press_start = now;
    
    if (now - last_reset_press < DOUBLE_TAP_WINDOW) {
      reset_tap_count++;
      // Cap at 2 to prevent counter from running away
      if (reset_tap_count > 2) reset_tap_count = 2;
      Serial.printf("[RESET] Tap detected! Count: %d\n", reset_tap_count);
      
      // Cancel pending single press if second tap detected
      if (reset_tap_count >= 2 && pending_single_press) {
        Serial.println("[RESET] Second tap detected, cancelling pending single press");
        pending_single_press = false;
      }
    } else {
      reset_tap_count = 1;
      Serial.println("[RESET] First tap detected");
    }
    last_reset_press = now;
  }
  
  if (digitalRead(PIN_RESET) == LOW && reset_button_was_high) {
    unsigned long press_duration = millis() - reset_press_start;
    
    Serial.printf("[RESET] Button released (count=%d, duration=%lums, ota_mode=%d)\n", 
                  reset_tap_count, press_duration, ota_mode);
    
    // Check for double-tap (OTA mode) - only if not already in OTA mode
    if (reset_tap_count >= 2 && !ota_mode) {
        Serial.println("[OTA] Double-tap detected! Entering OTA mode...");
        pending_single_press = false;  // Cancel any pending single press
        otaState = OTA_WAITING_NOTIFY;
        ota_mode = true;
        ota_wake_time = millis();
        lastActivityTime = millis();
        ledBlink(3, 200);
        
        // Send OTA_READY announcement
        HueMixLinkPacket ready;
        memset(&ready, 0, sizeof(ready));
        ready.type = PKT_OTA_READY;
        WiFi.macAddress(ready.sourceMAC);
        memset(ready.targetMAC, 0xFF, 6);
        ready.payload.otaReady.firmware_size = 0;
        ready.payload.otaReady.battery_mv = battery_mv;
        ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
        
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
        
        if (!sent) {
          Serial.println("[OTA] Failed to send OTA_READY to any gateway");
        }
        
        reset_tap_count = 0;
    }
    // Single press - set pending (wait to see if second tap comes)
    else if (reset_tap_count == 1 && press_duration < 5000 && !pending_single_press) {
      Serial.println("[RESET] Single tap released, waiting to confirm (no second tap)...");
      pending_single_press = true;
      pending_single_press_time = millis();
      lastActivityTime = millis();
    }
  }
  
  reset_button_was_high = (digitalRead(PIN_RESET) == HIGH);
  
  // Check if pending single press should be activated (no second tap came)
  if (HOME_ID == 0 && pending_single_press && millis() - pending_single_press_time >= SINGLE_PRESS_DELAY) {
    Serial.println("[RESET] No second tap detected, sending pairing request");
    pending_single_press = false;
    sendPacket(PKT_HELLO, 0);
    lastActivityTime = millis();
    delay(200); // Wait for potential pairing response
    reset_tap_count = 0;
  }
  
  // Reset button 5s hold for factory reset (check on rising edge only to avoid updating lastActivityTime every loop)
  if (digitalRead(PIN_RESET) == HIGH && !ota_mode) {
    unsigned long holdStart = millis();
    while(digitalRead(PIN_RESET) == HIGH) {
      if (millis() - holdStart > 5000) {
        Serial.println("Factory reset initiated...");
        ledBlink(10, 50);
        prefs.clear();
        HOME_ID = 0;
        gateways.count = 0;
        lastReedLevel = readReedLevelStable();
        Serial.println("Reset complete, restarting...");
        ESP.restart();
      }
      delay(10);
    }
    // Only update activity time after checking for 5s hold
    lastActivityTime = millis();
  }
  
  // Save pairing info if just paired
  if (homeSetupDone) {
    prefs.putUInt("hid", HOME_ID);
    ledBlink(5, 50);
    Serial.println("Paired! Requesting Full Gateway List...");
    sendPacket(PKT_DOOR_EVENT, ACT_SYNC);
    lastActivityTime = millis();
    homeSetupDone = false;
  }
  
  // If not in OTA mode and idle, go to sleep
  if (!ota_mode && millis() - lastActivityTime > 3000) {
    goToSleepDoor();
  }
  
  delay(10);
}
