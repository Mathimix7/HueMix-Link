/* 
  HUEMIXLINK V3 - MOTION SENSOR FIRMWARE
  Supports: ESP32 only
  
  Sleep Strategy:
  - Wake from PIR (EXT1) → Send event → Deep sleep with timer (cooldown)
  - Wake from timer → Re-enable PIR → Deep sleep with EXT1
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
#define PIN_PIR      26   // PIR sensor output (HIGH when motion detected)
#define PIN_RESET    14   // Factory reset button
#define PIN_LED      18   // Status LED
#define PIN_BATTERY  35   // Battery voltage ADC
#define PIN_LDR      34   // LDR light sensor (analog input)
#define PIN_LDR_POWER 33  // LDR power control (HIGH to enable, LOW to save battery)

#define LED_ACTIVE_HIGH HIGH

// Cooldown period - default 60 seconds, configurable via PKT_SYS_CMD
#define DEFAULT_COOLDOWN_SECONDS 60
#define CMD_SET_MOTION_COOLDOWN 0x40  // SysCmd to configure cooldown period

// Wakeup mask for EXT1 (PIR + RESET)
#define EXT1_WAKEUP_MASK ((1ULL << PIN_PIR) | (1ULL << PIN_RESET))

// After sending motion event, delay before sleep (allow network transmission)
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
uint32_t cooldown_seconds = DEFAULT_COOLDOWN_SECONDS;  // Configurable cooldown period

// Wakeup tracking
bool wakeupFromPIR = false;
bool wakeupFromTimer = false;
bool wakeupFromReset = false;
bool sleepWithPIR = true;

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

esp_now_peer_info_t peerInfo;
esp_adc_cal_characteristics_t adc_chars_battery;
esp_adc_cal_characteristics_t adc_chars_ldr;

// --- LED FUNCTIONS ---
void triggerLed(int duration) {
  if (breathingActive) return;
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
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

// --- BATTERY MONITORING ---
void getBatteryVoltage() {  
  uint32_t raw = 0;
  for(int i = 0; i < 10; i++) {
    raw += analogRead(PIN_BATTERY);
    delay(5);
  }
  raw /= 10;
  
  uint32_t voltage = esp_adc_cal_raw_to_voltage(raw, &adc_chars_battery);
  battery_mv = (voltage * 1300) / 300;
}

// --- LDR LIGHT SENSOR ---
void getLightLevel() {  
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
  digitalWrite(PIN_LDR_POWER, LOW);}

// --- PACKET SENDING ---
void sendPacket(uint8_t packetType, uint8_t action) {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(pkt));
  pkt.type = packetType;
  WiFi.macAddress(pkt.sourceMAC);
  memset(pkt.targetMAC, 0xFF, 6);
  pkt.msgID = 0;
  
  if (packetType == PKT_MOTION_EVENT) {
    pkt.payload.motion.action = action;
    pkt.payload.motion.battery_mv = battery_mv;
    pkt.payload.motion.light_level = light_level;
    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.motion.version_major = major;
      pkt.payload.motion.version_minor = minor;
      pkt.payload.motion.version_patch = patch;
    #else
      pkt.payload.motion.version_major = 0;
      pkt.payload.motion.version_minor = 0;
      pkt.payload.motion.version_patch = 0;
    #endif
    
    // Set platform
    #if defined(ESP8266)
      pkt.payload.motion.platform = 1;
    #else
      pkt.payload.motion.platform = 0;
    #endif
    
    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  } else if (packetType == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_MOTION;
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
    
    // Use HOME_ID for signature if paired, 0 if unpaired
    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  }
  
  ackReceived = false;
  bool sent = false;
  int successfulGatewayIndex = -1;
  
  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    for(int i = 0; i < gateways.count; i++) {
      if (!esp_now_is_peer_exist(gateways.macs[i])) {
        memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
        peerInfo.channel = 0;
        peerInfo.encrypt = false;
        esp_now_add_peer(&peerInfo);
      }
      
      if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
        sent = true;
        successfulGatewayIndex = i;
        break;
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
  }
  // B. UNPAIRED MODE
  else if (HOME_ID == 0) {
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
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
          Serial.printf("[MOTION] SECURITY: Invalid signature on gateway list. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt.signature);
          Serial.println("[MOTION] Rejected unauthorized gateway list");
          return;
        }
      }
      
      Serial.printf("[MOTION] Server sent %d gateways:\n", pkt.payload.gwList.count);
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
    if (pkt.payload.sys.cmd == CMD_SET_MOTION_COOLDOWN) {
      // Receive cooldown setting as uint32_t from raw payload bytes
      uint32_t new_cooldown;
      memcpy(&new_cooldown, &pkt.payload.raw[2], sizeof(uint32_t));
      
      // Validate range (1-3600 seconds = 1 second to 1 hour)
      if (new_cooldown >= 5 && new_cooldown <= 60) {
        cooldown_seconds = new_cooldown;
        prefs.putUInt("cooldown", cooldown_seconds);
        Serial.printf("[MOTION] Cooldown period updated to %u seconds\n", cooldown_seconds);
        lastActivityTime = millis();
      } else {
        Serial.printf("[MOTION] Invalid cooldown value: %u (must be 5-60)s\n", new_cooldown);
      }
    }
  }
  else if (pkt.type == PKT_OTA_NOTIFY) {
    // OTA implementation similar to buttons (abbreviated for brevity)
    if (!ota_mode || otaState != OTA_WAITING_NOTIFY) return;
    
    expected_firmware_size = pkt.payload.otaNotify.firmware_size;
    memcpy(expected_sha256, pkt.payload.otaNotify.sha256_hash, 32);
      
    update_partition = esp_ota_get_next_update_partition(NULL);
    if (update_partition == NULL) {
      Serial.println("[OTA] No update partition!");
      return;
    }
      
    esp_err_t err = esp_ota_begin(update_partition, expected_firmware_size, &update_handle);
    if (err != ESP_OK) {
      Serial.printf("[OTA] Begin failed: %d\n", err);
      return;
    }
      
    mbedtls_sha256_init(&sha256_ctx);
    mbedtls_sha256_starts(&sha256_ctx, 0);
    
    received_bytes = 0;
    expected_chunk_index = 0;
    otaState = OTA_RECEIVING;
    last_ota_activity = millis();
    
    startLedBreathing();
    
    // Send OTA_READY with actual firmware size
    HueMixLinkPacket ready;
    memset(&ready, 0, sizeof(ready));
    ready.type = PKT_OTA_READY;
    WiFi.macAddress(ready.sourceMAC);
    memcpy(ready.targetMAC, mac, 6);
    ready.payload.otaReady.firmware_size = expected_firmware_size;
    ready.payload.otaReady.battery_mv = battery_mv;
    ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
    esp_now_send(mac, (uint8_t*)&ready, sizeof(ready));
    
    Serial.printf("[OTA] Ready to receive %u bytes\n", expected_firmware_size);
  }
  else if (pkt.type == PKT_OTA_CHUNK) {
    if (otaState != OTA_RECEIVING) return;
      
    if (pkt.payload.otaChunk.chunk_index == expected_chunk_index) {
      esp_ota_write(update_handle, pkt.payload.otaChunk.data, pkt.payload.otaChunk.data_len);
      mbedtls_sha256_update(&sha256_ctx, pkt.payload.otaChunk.data, pkt.payload.otaChunk.data_len);
      
      received_bytes += pkt.payload.otaChunk.data_len;
      expected_chunk_index++;
      last_ota_activity = millis();
      
      // Send ACK every 10 chunks
      if (expected_chunk_index % 10 == 0) {
        HueMixLinkPacket ack;
        memset(&ack, 0, sizeof(ack));
        ack.type = PKT_OTA_CHUNK_ACK;
        WiFi.macAddress(ack.sourceMAC);
        memcpy(ack.targetMAC, mac, 6);
        ack.payload.otaChunkAck.last_chunk_index = expected_chunk_index - 1;
        ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
        esp_now_send(mac, (uint8_t*)&ack, sizeof(ack));
      }
      
      if (received_bytes >= expected_firmware_size) {
        otaState = OTA_VALIDATING;
      }
    }
  }
  else if (pkt.type == PKT_OTA_COMPLETE) {
    if (otaState == OTA_VALIDATING) {
      uint8_t calculated_sha256[32];
      mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
      
      if (memcmp(calculated_sha256, expected_sha256, 32) == 0) {
        esp_ota_end(update_handle);
        esp_ota_set_boot_partition(update_partition);
        
        stopLedBreathing();
        ledBlink(5, 100);
        
        Serial.println("[OTA] Update successful! Rebooting...");
        delay(500);
        ESP.restart();
      } else {
        Serial.println("[OTA] Hash mismatch!");
        esp_ota_abort(update_handle);
        otaState = OTA_IDLE;
        ota_mode = false;
        stopLedBreathing();
      }
    }
  }
  else if (pkt.type == PKT_OTA_ABORT) {
    if (otaState == OTA_RECEIVING || otaState == OTA_VALIDATING) {
      esp_ota_abort(update_handle);
    }
    otaState = OTA_IDLE;
    ota_mode = false;
    stopLedBreathing();
    Serial.println("[OTA] Aborted by server");
  }
}

// --- SLEEP FUNCTIONS ---
void goToSleepWithTimer() {
  uint64_t cooldown_us = (uint64_t)cooldown_seconds * 1000000ULL;
  Serial.printf("Going to sleep with timer wakeup (%u seconds cooldown)\n", cooldown_seconds);
  Serial.flush();
  
  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  
  // Sleep with TIMER + RESET button (PIR disabled during cooldown)
  esp_sleep_enable_timer_wakeup(cooldown_us);
  esp_sleep_enable_ext1_wakeup((1ULL << PIN_RESET), ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

void goToSleepWithPIR() {
  Serial.println("Going to sleep with PIR wakeup (EXT1)");
  Serial.flush();
  
  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  
  // Sleep with EXT1 wakeup on PIR + RESET (HIGH level)
  esp_sleep_enable_ext1_wakeup(EXT1_WAKEUP_MASK, ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

int getWakeupPin() {
  uint64_t wakeup_pin_mask = esp_sleep_get_ext1_wakeup_status();
  if (wakeup_pin_mask == 0) return -1;
  return __builtin_ctzll(wakeup_pin_mask);
}

// --- SETUP ---
void setup() {
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RESET, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  
  // LDR power control - keep OFF to save battery
  pinMode(PIN_LDR_POWER, OUTPUT);
  digitalWrite(PIN_LDR_POWER, LOW);
  
  // Initialize ADC (12-bit resolution = 0-4095)
  analogSetWidth(12);
  analogSetPinAttenuation(PIN_BATTERY, ADC_2_5db);
  analogSetPinAttenuation(PIN_LDR, ADC_11db);
  
  // Characterize both ADC channels with their respective attenuations
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_2_5, ADC_WIDTH_BIT_12, 1100, &adc_chars_battery);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, 1100, &adc_chars_ldr);
  
  getBatteryVoltage();
  
  Serial.begin(115200);
  Serial.println("\n--- MOTION SENSOR WAKE ---");
  
  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));
  cooldown_seconds = prefs.getUInt("cooldown", DEFAULT_COOLDOWN_SECONDS);
  
  // Check wakeup reason
  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  
  if (wakeupReason == ESP_SLEEP_WAKEUP_EXT1) {
    // Woke from PIR or RESET button
    int wakeupPin = getWakeupPin();
    
    if (wakeupPin == PIN_PIR) {
      wakeupFromPIR = true;
      Serial.println("Wakeup: PIR detected motion");
    } else if (wakeupPin == PIN_RESET) {
      wakeupFromReset = true;
      Serial.println("Wakeup: RESET button");
      // Handle reset button in main loop
    }
    
  } else if (wakeupReason == ESP_SLEEP_WAKEUP_TIMER) {
    // Woke from timer (cooldown finished)
    wakeupFromTimer = true;
    Serial.println("Wakeup: Cooldown timer expired");
    
    // Check if RESET was also pressed during timer wakeup
    int wakeupPin = getWakeupPin();
    if (wakeupPin == PIN_RESET) {
      Serial.println("RESET button also pressed during cooldown");
      // Handle in main loop
    } else {
      // Cooldown finished, re-enable PIR and go back to sleep
      goToSleepWithPIR();
    }
    
  } else {
    // Cold boot
    Serial.println("Cold boot");
  }
  
  // If we reach here, it's cold boot or reset button press
  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();
  
  esp_now_register_recv_cb(OnDataRecv);
  
  lastActivityTime = millis();
  
  Serial.printf("HOME_ID: 0x%08X\n", HOME_ID);
  Serial.printf("Battery: %d mV\n", battery_mv);
  Serial.printf("Gateways: %d\n", gateways.count);
  Serial.printf("Cooldown: %u seconds\n", cooldown_seconds);
}

// --- MAIN LOOP ---
void loop() {
  // Handle PIR motion event sending (from wakeup)
  if (wakeupFromPIR) {
    wakeupFromPIR = false;
    
    // Read light level from LDR sensor
    getLightLevel();
    Serial.printf("Light level: %d\n", light_level);
    
    if (HOME_ID == 0) {
      // Unpaired - send HELLO packet
      sendPacket(PKT_HELLO, 0);
      Serial.println("Motion detected (pair request)");
    } else {
      // Paired - send motion event
      sendPacket(PKT_MOTION_EVENT, ACT_MOTION_DETECTED);
      Serial.println("Motion detected (paired)");
    }
    
    triggerLed(100);
    
    // Wait for transmission to complete
    delay(TX_SETTLE_TIME);
    
    // If pairing just happened, wait for gateway list and save
    if (homeSetupDone) {
      Serial.println("Pairing detected, waiting for gateway list...");
      delay(200); // Give time for gateway list to arrive
      
      prefs.putUInt("hid", HOME_ID);
      ledBlink(5, 50);
      Serial.println("Paired! Requesting Full Gateway List...");
      sendPacket(PKT_MOTION_EVENT, ACT_SYNC);
      delay(100);
      homeSetupDone = false;
    }

    sleepWithPIR = false;
  }
  
  // Handle OTA mode timeout
  if (ota_mode && millis() - ota_wake_time > 300000) {
    Serial.println("[OTA] Timeout, returning to normal mode");
    ota_mode = false;
    otaState = OTA_IDLE;
    stopLedBreathing();
    goToSleepWithPIR();
  }
  
  // Handle RESET button: single press (HELLO), double-tap (OTA), 5s hold (factory reset)
  static bool reset_button_was_high = false;
  static unsigned long reset_press_start = 0;
  
  if (digitalRead(PIN_RESET) == HIGH && !reset_button_was_high) {
    unsigned long now = millis();
    reset_press_start = now;
    
    if (now - last_reset_press < DOUBLE_TAP_WINDOW) {
      reset_tap_count++;
    } else {
      reset_tap_count = 1;
    }
    last_reset_press = now;
  }
  
  if (digitalRead(PIN_RESET) == LOW && reset_button_was_high) {
    unsigned long press_duration = millis() - reset_press_start;
    
    // Check for double-tap (OTA mode)
    if (reset_tap_count >= 2 && !ota_mode) {
      Serial.println("[OTA] Double-tap detected! Entering OTA mode...");
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
      
      for(int i = 0; i < gateways.count; i++) {
        if (!esp_now_is_peer_exist(gateways.macs[i])) {
          memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
          peerInfo.channel = 0;
          peerInfo.encrypt = false;
          esp_now_add_peer(&peerInfo);
        }
        esp_now_send(gateways.macs[i], (uint8_t*)&ready, sizeof(ready));
      }
      
      reset_tap_count = 0;
    }
    // Single press - send HELLO for pairing
    else if (reset_tap_count == 1 && press_duration < 5000 && !ota_mode) {
      Serial.println("Single RESET press - sending pairing request");
      ledBlink(2, 100);
      sendPacket(PKT_HELLO, 0);
      lastActivityTime = millis();
      delay(200); // Wait for potential pairing response
      reset_tap_count = 0;
    }
  }
  
  reset_button_was_high = (digitalRead(PIN_RESET) == HIGH);
  
  // Reset button 5s hold for factory reset
  if (digitalRead(PIN_RESET) == HIGH && !ota_mode) {
    lastActivityTime = millis();
    unsigned long holdStart = millis();
    while(digitalRead(PIN_RESET) == HIGH) {
      if (millis() - holdStart > 5000) {
        Serial.println("Factory reset initiated...");
        ledBlink(10, 50);
        prefs.clear();  // Clears HOME_ID, gateways, and cooldown setting
        HOME_ID = 0;
        gateways.count = 0;
        cooldown_seconds = DEFAULT_COOLDOWN_SECONDS;
        Serial.println("Reset complete, restarting...");
        ESP.restart();
      }
      delay(10);
    }
  }
  
  // Save pairing info if just paired
  if (homeSetupDone) {
    prefs.putUInt("hid", HOME_ID);
    ledBlink(5, 50);
    Serial.println("Paired! Requesting Full Gateway List...");
    sendPacket(PKT_MOTION_EVENT, ACT_SYNC);
    lastActivityTime = millis();
    homeSetupDone = false;
  }
  
  // If not in OTA mode and idle, go to sleep
  if (!ota_mode && millis() - lastActivityTime > 3000) {
    if (sleepWithPIR && HOME_ID != 0) {
      goToSleepWithPIR();
    } else {
      goToSleepWithTimer();
    }
  }
  
  delay(10);
}
