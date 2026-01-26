/* 
  HUEMIXLINK V3 - LIGHT STRIP FIRMWARE
  Supports: ESP32 & ESP8266
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <FastLED.h>
#include <Ticker.h>

// --- CONFIGURATION ---
#define NUM_LEDS    35 // Max 60
#define MAX_LEDS    60

// --- SELECT YOUR STRIP TYPE HERE ---

// Option 1: Standard RGB
#define IS_RGBW  false
#define COLOR_ORDER GRB
#define LED_TYPE WS2812B

// Option 2: RGBW Strip
// #define IS_RGBW  true
// #define LED_TYPE SK6812
// #define COLOR_ORDER GRB 

// --- PLATFORM SETUP ---
#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #include <Updater.h>
  #include <bearssl/bearssl_hash.h>
  #define LED_PIN     D2
  #define PIN_RESET   D1
  #define ONBOARD_LED D4
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_wifi.h>
  #include <esp_ota_ops.h>
  #include <esp_partition.h>
  #include "mbedtls/sha256.h"
  #define LED_PIN     16  
  #define PIN_RESET   27
  #define ONBOARD_LED 2
#endif

// --- MODEL ID ---
// Each firmware variant has a unique model ID for identification
// Model 1: ESP32, RGB, GRB, WS2812B
// Model 2: ESP32, RGBW, GRB, SK6812
// Model 3: ESP8266, RGB, GRB, WS2812B
// Model 4: ESP8266, RGBW, GRB, SK6812
#if defined(ESP8266)
  #if IS_RGBW
    #define MODEL_ID 4
  #else
    #define MODEL_ID 3
  #endif
#else
  #if IS_RGBW
    #define MODEL_ID 2
  #else
    #define MODEL_ID 1
  #endif
#endif

// --- GLOBALS ---
Preferences prefs;
uint32_t HOME_ID = 0; 
uint16_t numLeds = NUM_LEDS;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
int lastSuccessfulGatewayIndex = -1;

// --- OTA LED BREATHING (using onboard LED) ---
#define LED_PWM_FREQ 5000
#define LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;
bool breathingActive = false;
volatile bool breathingUpdatePending = false;  // Flag for interrupt-safe LED update

const uint8_t gamma8[] = {
    0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,
    0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  1,  1,  1,  1,
    1,  1,  1,  1,  1,  1,  1,  1,  1,  2,  2,  2,  2,  2,  2,  2,
    2,  3,  3,  3,  3,  3,  3,  3,  4,  4,  4,  4,  4,  5,  5,  5,
    5,  6,  6,  6,  6,  7,  7,  7,  7,  8,  8,  8,  9,  9,  9, 10,
   10, 10, 11, 11, 11, 12, 12, 13, 13, 13, 14, 14, 15, 15, 16, 16,
   17, 17, 18, 18, 19, 19, 20, 20, 21, 21, 22, 22, 23, 24, 24, 25,
   25, 26, 27, 27, 28, 29, 29, 30, 31, 32, 32, 33, 34, 35, 35, 36,
   37, 38, 39, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 50,
   51, 52, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 66, 67, 68,
   69, 70, 72, 73, 74, 75, 77, 78, 79, 81, 82, 83, 85, 86, 87, 89,
   90, 92, 93, 95, 96, 98, 99,101,102,104,105,107,109,110,112,114,
  115,117,119,120,122,124,126,127,129,131,133,135,137,138,140,142,
  144,146,148,150,152,154,156,158,160,162,164,167,169,171,173,175,
  177,180,182,184,186,189,191,193,196,198,200,203,205,208,210,213,
  215,218,220,223,225,228,231,233,236,239,241,244,247,249,252,255 
};

struct CRGBW {
  union {
    struct {
      uint8_t g;
      uint8_t r;
      uint8_t b;
      uint8_t w;
    };
    uint8_t raw[4];
  };

  CRGBW(){}
  CRGBW(uint8_t _r, uint8_t _g, uint8_t _b, uint8_t _w) {
    r = _r; g = _g; b = _b; w = _w;
  }
  
  // Quick assign from CRGB
  inline void operator = (const CRGB& c) {
    r = c.r; g = c.g; b = c.b; w = 0;
  }
};


#if IS_RGBW
CRGBW leds[MAX_LEDS];
#define NUM_LEDS_BUFFER ((MAX_LEDS * 4) + 2) / 3
CRGB *ledsAsRGB = (CRGB*)leds;
#else
CRGB leds[MAX_LEDS];
#endif

bool isPaired = false;
unsigned long lastPairRequest = 0;
volatile bool deliveryComplete = false;
volatile bool deliverySuccess = false;

CRGBW rgbToRgbw(const CRGB &rgb) {
  // 1. Get the maximum brightness of the requested color
  uint8_t maxVal = max(rgb.r, max(rgb.g, rgb.b));
  
  // 2. If it's black, return black immediately
  if (maxVal == 0) return CRGBW(0,0,0,0);

  // 3. Calculate Saturation (0 = White, 255 = Fully Colorful)
  // We use FastLED's built-in saturation math logic here
  uint8_t minVal = min(rgb.r, min(rgb.g, rgb.b));
  uint8_t saturation = 255 - ((minVal * 255) / maxVal);

  // 4. LOGIC: 
  // If Saturation is High (> 200), keep White LED OFF (Pure Color).
  // If Saturation is Low, mix in the White LED.
  
  if (saturation >= 200) {
      // Deep rich color: Don't use the White channel at all.
      return CRGBW(rgb.r, rgb.g, rgb.b, 0); 
  } 
  else {
      // Pastel or White: Move the "common" part to the White LED
      // But scale it down slightly (0.8) because W LEDs are usually overpowering
      uint8_t whiteAmt = (uint8_t)(minVal * 0.8); 
      return CRGBW(rgb.r - whiteAmt, rgb.g - whiteAmt, rgb.b - whiteAmt, whiteAmt);
  }
}

// --- OTA STATE MACHINE ---
enum OtaState { OTA_IDLE, OTA_RECEIVING, OTA_VALIDATING, OTA_COMPLETE };
OtaState otaState = OTA_IDLE;
#if defined(ESP32)
  const esp_partition_t *update_partition = nullptr;
  esp_ota_handle_t update_handle = 0;
  mbedtls_sha256_context sha256_ctx;
#else
  br_sha256_context sha256_ctx;
#endif
uint32_t expected_firmware_size = 0;
uint32_t received_bytes = 0;
uint16_t expected_chunk_index = 0;
uint8_t expected_sha256[32];
unsigned long last_ota_activity = 0;

// Packet queue for ESP8266 interrupt-safe processing
#ifdef ESP8266
  #define PACKET_QUEUE_SIZE 10
  struct PacketQueueItem {
    HueMixLinkPacket packet;
    uint8_t mac[6];
    bool valid;
  };
  PacketQueueItem packetQueue[PACKET_QUEUE_SIZE];
  volatile uint8_t packetQueueHead = 0;
  volatile uint8_t packetQueueTail = 0;
#endif

// --- OTA LED INDICATOR (PWM breathing on onboard LED) ---
void updateBreathing() {
  breathingBrightness += breathingDirection * 10;
  if (breathingBrightness >= 255) {
    breathingBrightness = 255;
    breathingDirection = -1;
  } else if (breathingBrightness <= 0) {
    breathingBrightness = 0;
    breathingDirection = 1;
  }
  
  // Set flag to update LED in main loop (interrupt-safe for ESP8266)
  breathingUpdatePending = true;
}

void applyBreathingLed() {
  // This function is called from main loop, not interrupt
  if (!breathingActive || !breathingUpdatePending) return;
  breathingUpdatePending = false;
  
  #ifdef ESP32
    ledcWrite(ONBOARD_LED, breathingBrightness);
  #else
    // ESP8266: analogWrite is safe from main loop
    analogWrite(ONBOARD_LED, breathingBrightness);
  #endif
}

void startLedBreathing() {
  #ifdef ESP32
    ledcAttach(ONBOARD_LED, LED_PWM_FREQ, LED_PWM_RESOLUTION);
  #else
    // ESP8266: Set PWM frequency and range
    analogWriteFreq(LED_PWM_FREQ);
    analogWriteRange(255);
    pinMode(ONBOARD_LED, OUTPUT);
  #endif
  breathingBrightness = 0;
  breathingDirection = 1;
  breathingTicker.attach_ms(30, updateBreathing);
  breathingActive = true;
}

void stopLedBreathing() {
  if (!breathingActive) return;
  breathingTicker.detach();
  #ifdef ESP32
    ledcDetach(ONBOARD_LED);
  #endif
  pinMode(ONBOARD_LED, OUTPUT);
  digitalWrite(ONBOARD_LED, LOW);
  breathingActive = false;
}

// --- LED FEEDBACK ---
void showStatusColor(CRGB color) {
  for(int i=0; i<numLeds; i++) leds[i] = color;
  FastLED.show();
}

void flashStatus(CRGB color, int times) {
  for(int i=0; i<times; i++) {
    showStatusColor(color); delay(100);
    showStatusColor(CRGB::Black); delay(100);
  }
}

void saveGateways() { 
  prefs.putBytes("gw", &gateways, sizeof(gateways)); 
  Serial.printf("Saved %d gateways\n", gateways.count);
}

void addGateway(const uint8_t *mac) {
  for(int i=0; i<gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return; 
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.println("New Gateway Added");
  }
}


// --- FORWARD DECLARATIONS ---
void sendHello();

#if defined(ESP8266)
void OnDataSent(uint8_t *mac_addr, uint8_t status) {
  deliverySuccess = (status == 0); // 0 means success on ESP8266
  deliveryComplete = true;
}
#else
void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  deliverySuccess = (status == ESP_NOW_SEND_SUCCESS);
  deliveryComplete = true;
}
#endif

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  Serial.printf("[OTA] ABORT: %s\n", reason);
  stopLedBreathing();
  #if defined(ESP32)
    if (update_handle) {
      esp_ota_abort(update_handle);
      update_handle = 0;
    }
  #else
    Update.end();
  #endif
  otaState = OTA_IDLE;
  expected_chunk_index = 0;
  received_bytes = 0;
  flashStatus(CRGB::Red, 3);
  
  // Send HELLO to restore color state after showing error
  if (HOME_ID != 0) {
    sendHello();
    Serial.println("[OTA] Sent HELLO to restore colors");
  }
}

void handleOtaNotify(HueMixLinkPacket* pkt) {
  if (otaState != OTA_IDLE) {
    Serial.println("[OTA] Busy with another update");
    return;
  }
  
  Serial.println("[OTA] NOTIFY received");
  
  expected_firmware_size = pkt->payload.otaNotify.firmware_size;
  memcpy(expected_sha256, pkt->payload.otaNotify.sha256_hash, 32);
  
  #if defined(ESP32)
    // ESP32: Initialize update partition
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
  #else
    // ESP8266: Initialize Update library
    Serial.printf("[OTA] Starting update: %u bytes\n", expected_firmware_size);
    
    if (!Update.begin(expected_firmware_size)) {
      Serial.printf("[OTA] Begin failed: %s\n", Update.getErrorString().c_str());
      abortOta("Begin failed");
      return;
    }
    
    br_sha256_init(&sha256_ctx);
  #endif
  
  last_ota_activity = millis();
  otaState = OTA_RECEIVING;
  expected_chunk_index = 0;
  received_bytes = 0;
  
  Serial.printf("[OTA] Set last_ota_activity to %lu\n", last_ota_activity);
  
  // Send PKT_OTA_READY immediately
  HueMixLinkPacket ready;
  memset(&ready, 0, sizeof(HueMixLinkPacket));
  ready.type = PKT_OTA_READY;
  WiFi.macAddress(ready.sourceMAC);
  memcpy(ready.targetMAC, pkt->sourceMAC, 6);
  ready.payload.otaReady.firmware_size = expected_firmware_size;
  ready.payload.otaReady.battery_mv = 0; // Not battery powered
  ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
  
  // Send to first gateway
  if (gateways.count > 0) {
    #if defined(ESP32)
      esp_now_peer_info_t peerInfo = {};
      memcpy(peerInfo.peer_addr, gateways.macs[0], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      if (!esp_now_is_peer_exist(gateways.macs[0])) {
        esp_now_add_peer(&peerInfo);
      }
      esp_now_send(gateways.macs[0], (uint8_t*)&ready, sizeof(ready));
    #else
      if (!esp_now_is_peer_exist(gateways.macs[0])) {
        esp_now_add_peer(gateways.macs[0], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      }
      esp_now_send(gateways.macs[0], (uint8_t*)&ready, sizeof(ready));
    #endif
    Serial.println("[OTA] Sent READY");
  }
  
  // Start onboard LED blinking to indicate OTA in progress
  startLedBreathing();
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
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
  
  #if defined(ESP32)
    // ESP32: Write to partition
    esp_err_t err = esp_ota_write(update_handle, pkt->payload.otaChunk.data, data_len);
    if (err != ESP_OK) {
      Serial.printf("[OTA] Write failed at chunk %d: %d\n", chunk_idx, err);
      abortOta("Write failed");
      return;
    }
    mbedtls_sha256_update(&sha256_ctx, pkt->payload.otaChunk.data, data_len);
  #else
    // ESP8266: Write via Update library
    size_t written = Update.write(pkt->payload.otaChunk.data, data_len);
    if (written != data_len) {
      Serial.printf("[OTA] Write failed at chunk %d: wrote %d of %d\n", chunk_idx, written, data_len);
      abortOta("Write failed");
      return;
    }
    br_sha256_update(&sha256_ctx, pkt->payload.otaChunk.data, data_len);
  #endif
  
  received_bytes += data_len;
  expected_chunk_index++;
  
  if (chunk_idx % 50 == 0) {
    Serial.printf("[OTA] Progress: %u / %u bytes (%.1f%%)\n", 
      received_bytes, expected_firmware_size, 
      (received_bytes * 100.0) / expected_firmware_size);
  }
}

void sendOtaChunkAck(uint16_t last_chunk_index) {
  HueMixLinkPacket ack;
  memset(&ack, 0, sizeof(HueMixLinkPacket));
  ack.type = PKT_OTA_CHUNK_ACK;
  WiFi.macAddress(ack.sourceMAC);
  memset(ack.targetMAC, 0, 6); // Server doesn't need target MAC
  ack.msgID = 0;
  
  ack.payload.otaChunkAck.last_chunk_index = last_chunk_index;
  ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
  
  // Send to first gateway
  if (gateways.count > 0) {
    #if defined(ESP32)
      esp_now_peer_info_t peerInfo = {};
      memcpy(peerInfo.peer_addr, gateways.macs[0], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      if (!esp_now_is_peer_exist(gateways.macs[0])) {
        esp_now_add_peer(&peerInfo);
      }
      esp_now_send(gateways.macs[0], (uint8_t*)&ack, sizeof(ack));
    #else
      if (!esp_now_is_peer_exist(gateways.macs[0])) {
        esp_now_add_peer(gateways.macs[0], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      }
      esp_now_send(gateways.macs[0], (uint8_t*)&ack, sizeof(ack));
    #endif
    Serial.printf("[OTA] Sent checkpoint ACK: last_chunk=%d\n", last_chunk_index);
  }
}

void handleOtaCheckpointReq(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  // Respond with last successfully received chunk (expected_chunk_index - 1)
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  sendOtaChunkAck(last_chunk);
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  
  Serial.println("[OTA] COMPLETE received, validating...");
  otaState = OTA_VALIDATING;
  
  // Finalize SHA256
  uint8_t calculated_sha256[32];
  #if defined(ESP32)
    mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
    mbedtls_sha256_free(&sha256_ctx);
  #else
    br_sha256_out(&sha256_ctx, calculated_sha256);
  #endif
  
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
  
  #if defined(ESP32)
    // ESP32: Finalize OTA
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
  #else
    // ESP8266: Finalize Update
    if (!Update.end(true)) {
      Serial.printf("[OTA] End failed: %s\n", Update.getErrorString().c_str());
      abortOta("End failed");
      return;
    }
  #endif
  
  // Send final checkpoint ACK
  if (expected_chunk_index > 0) {
    sendOtaChunkAck(expected_chunk_index - 1);
  }
  
  Serial.println("[OTA] UPDATE SUCCESSFUL! Rebooting in 2 seconds...");
  otaState = OTA_COMPLETE;
  
  stopLedBreathing();
  
  // Flash onboard LED to indicate success (avoid FastLED)
  for (int i = 0; i < 10; i++) {
    digitalWrite(ONBOARD_LED, HIGH);
    delay(100);
    digitalWrite(ONBOARD_LED, LOW);
    delay(100);
  }

  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  Serial.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

// --- SEND HELLO ---
void sendHello() {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  
  // Payload: [Type, RSSI_Hole, RGBW_Flag, LED_Count_H, LED_Count_L, version_major, version_minor, version_patch, platform, model_id_low, model_id_high]
  pkt.payload.raw[0] = DEV_LIGHT; 
  pkt.payload.raw[1] = 0; 
  pkt.payload.raw[2] = IS_RGBW ? 1 : 0;
  pkt.payload.raw[3] = (uint8_t)((numLeds >> 8) & 0xFF);
  pkt.payload.raw[4] = (uint8_t)(numLeds & 0xFF);
  
  // Version info
  #ifdef FIRMWARE_VERSION
    const char* ver = FIRMWARE_VERSION;
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    pkt.payload.raw[5] = major;
    pkt.payload.raw[6] = minor;
    pkt.payload.raw[7] = patch;
  #else
    pkt.payload.raw[5] = 0;
    pkt.payload.raw[6] = 0;
    pkt.payload.raw[7] = 0;
  #endif
  
  // Platform: 0=ESP32, 1=ESP8266
  #if defined(ESP8266)
    pkt.payload.raw[8] = 1;
  #else
    pkt.payload.raw[8] = 0;
  #endif
  
  // Model ID (2 bytes, little-endian): identifies exact firmware variant (LED type, color order, RGBW)
  pkt.payload.raw[9] = (uint8_t)(MODEL_ID & 0xFF);        // Low byte
  pkt.payload.raw[10] = (uint8_t)((MODEL_ID >> 8) & 0xFF); // High byte
  
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);

  #if defined(ESP32)
    esp_now_peer_info_t peerInfo = {};
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
  #endif

  bool sent = false;
  
  // A. PAIRED MODE - Try known gateways
  if (HOME_ID != 0 && gateways.count > 0) {
    Serial.printf("[LIGHT] Sending HELLO to %d gateways\n", gateways.count);
    
    for(int i=0; i<gateways.count; i++) {
      #if defined(ESP8266)
        if(!esp_now_is_peer_exist(gateways.macs[i])) 
          esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      #else
        memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
      #endif

      deliveryComplete = false;
      deliverySuccess = false;

      esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt));

      unsigned long startWait = millis();
      while(!deliveryComplete && (millis() - startWait < 200)) {
        delay(1); 
      }

      if (deliverySuccess) {
        Serial.printf("  Gateway [%d] responded. Connection Established.\n", i);
        lastSuccessfulGatewayIndex = i;
        break; // <--- STOP LOOP, we found a working gateway
      } else {
        Serial.printf("  Gateway [%d] timed out. Trying next...\n", i);
      }
    }
    
    // Move successful gateway to front if not already there
    if (lastSuccessfulGatewayIndex > 0) {
      Serial.printf("[LIGHT] Moving gateway %d to front\n", lastSuccessfulGatewayIndex);
      uint8_t tempMac[6];
      memcpy(tempMac, gateways.macs[lastSuccessfulGatewayIndex], 6);
      for(int j = lastSuccessfulGatewayIndex; j > 0; j--) {
        memcpy(gateways.macs[j], gateways.macs[j-1], 6);
      }
      memcpy(gateways.macs[0], tempMac, 6);
      saveGateways();
    }
  }
  
  // B. UNPAIRED MODE - Broadcast
  else {
    Serial.println("[LIGHT] Broadcasting HELLO (unpaired)");
    #if defined(ESP8266)
      if(!esp_now_is_peer_exist(broadcastAddress)) 
        esp_now_add_peer(broadcastAddress, WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
    #else
      memcpy(peerInfo.peer_addr, broadcastAddress, 6);
      if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    #endif
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
    sent = true;
  }
  
  // Visual Blip (Blue)
  if (sent) flashStatus(CRGB::Blue, 1);
}

void processReceivedPacket(HueMixLinkPacket* rx, uint8_t* mac) {
  // 0. OTA HANDLING
  // Security: Verify all OTA packets before processing
  if (rx->type == PKT_OTA_NOTIFY || rx->type == PKT_OTA_CHUNK || 
      rx->type == PKT_OTA_CHECKPOINT_REQ || rx->type == PKT_OTA_COMPLETE || 
      rx->type == PKT_OTA_ABORT) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.printf("[LIGHT] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, rx->signature);
        Serial.println("[LIGHT] Rejected unauthorized OTA packet");
        return;
      }
    }
  }
  
  if (rx->type == PKT_OTA_NOTIFY) {
    handleOtaNotify(rx);
    return;
  } else if (rx->type == PKT_OTA_CHUNK) {
    handleOtaChunk(rx);
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

  // 1. PAIRING
  if (rx->type == PKT_PAIR_CONFIRM) {
    if (HOME_ID == 0) {
      uint32_t sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature == sig) {
        HOME_ID = rx->payload.pair.newHomeID;
        prefs.putUInt("hid", HOME_ID);
        addGateway(mac);
        isPaired = true;
        Serial.printf("PAIRED! ID: 0x%X\n", HOME_ID);
        flashStatus(CRGB::Green, 3);
        sendHello();
      }
    }
  }
  
  // 1b. GATEWAY LIST UPDATE
  else if (rx->type == PKT_GW_LIST_UPD) {
    // Security: Verify signature to ensure gateway list comes from trusted source with correct HOME_ID
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.printf("[LIGHT] SECURITY: Invalid signature on gateway list update. Expected 0x%08X, got 0x%08X\n", expected_sig, rx->signature);
        Serial.println("[LIGHT] Rejected unauthorized gateway list update");
        return;
      }
    }
    
    if (rx->payload.gwList.count > 0) {
      Serial.printf("[LIGHT] Received gateway list with %d gateways:\n", rx->payload.gwList.count);
      for(int i=0; i<rx->payload.gwList.count; i++) {
        Serial.printf("  [%d] %02X:%02X:%02X:%02X:%02X:%02X\n", i,
          rx->payload.gwList.macs[i][0], rx->payload.gwList.macs[i][1],
          rx->payload.gwList.macs[i][2], rx->payload.gwList.macs[i][3],
          rx->payload.gwList.macs[i][4], rx->payload.gwList.macs[i][5]);
      }
      
      // Merge new gateway list while preserving successful order
      Payload_GatewayList newList;
      newList.count = 0;
      
      // First, add existing gateways that are still in the new list (preserve order)
      for(int i=0; i<gateways.count && newList.count < MAX_GATEWAYS; i++) {
        bool stillExists = false;
        for(int j=0; j<rx->payload.gwList.count; j++) {
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
      for(int i=0; i<rx->payload.gwList.count && newList.count < MAX_GATEWAYS; i++) {
        bool isNew = true;
        for(int j=0; j<newList.count; j++) {
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
      Serial.printf("[LIGHT] Gateway list updated, now have %d gateways (will try first: %02X:%02X:..:%02X)\n",
        gateways.count,
        gateways.count > 0 ? gateways.macs[0][0] : 0,
        gateways.count > 0 ? gateways.macs[0][1] : 0,
        gateways.count > 0 ? gateways.macs[0][5] : 0);
    }
  }

  // 2. LIGHT DATA
  else if (rx->type == PKT_LIGHT_RAW) {
    if (HOME_ID != 0) {
      uint32_t sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      
      if (rx->signature == sig) {
         uint8_t count = rx->payload.light.count;
         uint8_t bri   = rx->payload.light.brightness;

         if (bri > 255) bri = 255;
         if (bri < 5) bri = 5;
         if (count > MAX_LEDS) count = MAX_LEDS;

         FastLED.setBrightness(bri);

         uint8_t* d = rx->payload.light.data;
         int idx = 0;
         
         for(int i=0; i<count; i++) {
            uint8_t rawR = d[idx];
            uint8_t rawG = d[idx+1];
            uint8_t rawB = d[idx+2];
            CRGB rgb(gamma8[rawR], gamma8[rawG], gamma8[rawB]);

            #if IS_RGBW
              leds[i] = rgbToRgbw(rgb);
            #else
              leds[i] = rgb;
            #endif
            idx += 3; 
         }
         FastLED.show();
      }
    }
  }
  
  // 3. SYSTEM RESET (Remote)
  else if (rx->type == PKT_SYS_CMD) {
    Serial.printf("[LIGHT] Received PKT_SYS_CMD from %02X:%02X:%02X:%02X:%02X:%02X\n", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    if (rx->signature == calculateHash(rx->payload.raw, 185, HOME_ID)) {
        // Factory Reset
        if (rx->payload.sys.cmd == 0xFF) {
          // Selective reset: only clear pairing data
          prefs.remove("hid");
          prefs.remove("gw");
          Serial.println("[RESET] Remote factory reset triggered");
          ESP.restart();
        }
        // Update Configured Length
        else if (rx->payload.sys.cmd == 0x50) {
          numLeds = rx->payload.sys.value;
          prefs.putUInt("leds", numLeds);
          Serial.printf("[LIGHT] Updated LED count to %d\n", numLeds);
          flashStatus(CRGB::White, 1);
        }
     }
  }
  
  // 4. PING DEVICE - Respond so gateway can record RSSI
  else if (rx->type == PKT_PING_DEVICE) {
     Serial.printf("[LIGHT] Received PKT_PING_DEVICE from %02X:%02X:%02X:%02X:%02X:%02X\n",
       mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
     
     uint32_t expected = calculateHash(rx->payload.raw, 185, HOME_ID);
     Serial.printf("[LIGHT] Signature check: rx=0x%08X expected=0x%08X\n", rx->signature, expected);
     
     if (rx->signature == expected) {
         Serial.println("[LIGHT] Signature valid, sending response");
         // Send response back to the gateway that sent the ping
         HueMixLinkPacket pong;
         memset(&pong, 0, sizeof(HueMixLinkPacket));
         pong.type = PKT_PING_DEVICE;
         WiFi.macAddress(pong.sourceMAC);
         memset(pong.payload.raw, 0, sizeof(pong.payload.raw));
         pong.signature = calculateHash(pong.payload.raw, 185, HOME_ID);
         
         // Send back to the sender (gateway)
         #if defined(ESP8266)
           if (!esp_now_is_peer_exist(mac)) {
             esp_now_add_peer(mac, WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
           }
           int result = esp_now_send(mac, (uint8_t*)&pong, sizeof(pong));
           Serial.printf("[LIGHT] Ping response sent, result=%d\n", result);
         #else
           esp_now_peer_info_t peerInfo = {};
           memcpy(peerInfo.peer_addr, mac, 6);
           peerInfo.channel = 0;
           peerInfo.encrypt = false;
           if (!esp_now_is_peer_exist(mac)) esp_now_add_peer(&peerInfo);
           
           esp_err_t result = esp_now_send(mac, (uint8_t*)&pong, sizeof(pong));
           Serial.printf("[LIGHT] Ping response sent, result=%d\n", result);
         #endif
     } else {
       Serial.println("[LIGHT] Signature invalid, ignoring ping");
     }
  }
}

#if defined(ESP8266)
void OnDataRecv(uint8_t *mac_addr, uint8_t *data, uint8_t len) {
#else
void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
#endif
  if (len < sizeof(HueMixLinkPacket)) return;
  
  #if defined(ESP8266)
    const uint8_t *mac = mac_addr;
  #else
    uint8_t *mac = (uint8_t *)info->src_addr;
  #endif

#ifdef ESP8266
  // ESP8266: Queue packet for processing in main loop (interrupt-safe)
  uint8_t nextHead = (packetQueueHead + 1) % PACKET_QUEUE_SIZE;
  if (nextHead != packetQueueTail) {  // Queue not full
    memcpy(&packetQueue[packetQueueHead].packet, data, sizeof(HueMixLinkPacket));
    memcpy(packetQueue[packetQueueHead].mac, mac, 6);
    packetQueue[packetQueueHead].valid = true;
    packetQueueHead = nextHead;
  }
#else
  // ESP32: Process directly (interrupt handler is more capable)
  processReceivedPacket((HueMixLinkPacket*)data, mac);
#endif
}

void fillSolid(CRGB color) {
  for(int i=0; i<numLeds; i++) {
    #if IS_RGBW
      leds[i] = rgbToRgbw(color);
    #else
      leds[i] = color;
    #endif
  }
  FastLED.show();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RESET, INPUT_PULLUP); 

  prefs.begin("huemixlink", false);
  
  // Load LED count from EEPROM (can be changed remotely)
  numLeds = prefs.getUInt("leds", NUM_LEDS);
  if (numLeds > MAX_LEDS) numLeds = MAX_LEDS;
  
  // Save default on first boot if not already saved
  if (!prefs.isKey("leds")) {
    prefs.putUInt("leds", numLeds);
    Serial.printf("[CONFIG] Saved default LED count: %d\n", numLeds);
  }
  
  Serial.printf("[CONFIG] LED Count: %d, Model ID: %d\n", numLeds, MODEL_ID);
  
  // Load pairing data
  HOME_ID = prefs.getUInt("hid", 0);
  size_t gwSize = prefs.getBytes("gw", &gateways, sizeof(gateways));
  if (gwSize != sizeof(gateways)) {
    gateways.count = 0;
  }
  Serial.printf("Loaded %d gateways from memory\n", gateways.count);
    
  // Initialize FastLED with compile-time configuration
  #if IS_RGBW
    uint16_t virtualLeds = (MAX_LEDS * 4 + 2) / 3;
    FastLED.addLeds<LED_TYPE, LED_PIN, RGB>(ledsAsRGB, virtualLeds).setCorrection(TypicalLEDStrip);
  #else
    FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, MAX_LEDS).setCorrection(TypicalLEDStrip);
  #endif

  FastLED.setBrightness(255); 
  fillSolid(CRGB::Black);
  FastLED.show();

  // WiFi
  WiFi.mode(WIFI_STA);
  #if defined(ESP8266)
    if (esp_now_init() != 0) ESP.restart();
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
    wifi_set_channel(HUEMIXLINK_CHANNEL); 
    esp_now_register_send_cb(OnDataSent);
    esp_now_add_peer(broadcastAddress, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  #else
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    if (esp_now_init() != ESP_OK) ESP.restart();
    esp_wifi_set_channel(HUEMIXLINK_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = HUEMIXLINK_CHANNEL;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
  #endif

  esp_now_register_recv_cb(OnDataRecv);

  if (HOME_ID != 0) {
    isPaired = true;
    Serial.printf("--- LIGHT READY (ID: 0x%X) ---\n", HOME_ID);
    sendHello();
  } else {
    Serial.println("--- LIGHT UNPAIRED ---");
  }
}

void loop() {
  // Apply LED breathing update (interrupt-safe for ESP8266)
  applyBreathingLed();
  
  // --- PROCESS QUEUED PACKETS (ESP8266 interrupt-safe) ---
  #ifdef ESP8266
  while (packetQueueTail != packetQueueHead) {
    if (packetQueue[packetQueueTail].valid) {
      processReceivedPacket(&packetQueue[packetQueueTail].packet, packetQueue[packetQueueTail].mac);
      packetQueue[packetQueueTail].valid = false;
    }
    packetQueueTail = (packetQueueTail + 1) % PACKET_QUEUE_SIZE;
  }
  #endif
  
  // --- OTA TIMEOUT CHECK ---
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    Serial.printf("[OTA] Timeout check: millis=%lu, last_ota_activity=%lu, diff=%lu\n", millis(), last_ota_activity, millis() - last_ota_activity);
    Serial.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  // --- FACTORY RESET LOGIC ---
  if (digitalRead(PIN_RESET) == LOW) {
    unsigned long holdStart = millis();
    
    // Show visual warning (Red)
    FastLED.setBrightness(100);
    fillSolid(CRGB::Red);
    FastLED.show();
    
    while (digitalRead(PIN_RESET) == LOW) {
      if (millis() - holdStart > 5000) {
        Serial.println("FACTORY RESET!");
        flashStatus(CRGB::Red, 5);
        
        // Selective reset: only clear pairing data
        prefs.remove("hid");   // Clear HOME_ID
        prefs.remove("gw");    // Clear gateways
        // Keep: "leds" (configurable LED count)
        Serial.println("[RESET] Cleared pairing data, LED count preserved");
        
        ESP.restart();
      }
      delay(10);
    }
    
    Serial.println("Reset Aborted. Requesting State...");
    fillSolid(CRGB::Black); FastLED.show();
    sendHello();
  }

  // --- UNPAIRED BEHAVIOR ---
  if (!isPaired) {
    if (millis() - lastPairRequest > 5000) {
      sendHello();
      lastPairRequest = millis();
    }
  }
  
  #if defined(ESP8266)
    yield();
  #endif
}