/* 
  HUEMIXLINK V3 - LIGHT STRIP FIRMWARE - MODEL 5
  Individual RGB (WS2812B) + Global Warm White PWM (Inverted)
  
  Hardware:
  - ESP32 platform
  - RGB LEDs on pin 27 (WS2812B)
  - Warm White PWM on pin 26 (inverted - LOW = ON, HIGH = OFF)
  - Reset button on pin 23
  
  Features:
  - Smart warm white extraction based on ~3000K color temperature
  - Extracts maximum shared warm white component from all pixels
  - RGB LEDs handle the remaining color information
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <FastLED.h>
#include <Ticker.h>

// --- MODEL ID ---
#define LIGHTSTRIP_MODEL 5

// --- CONFIGURATION ---
#define NUM_LEDS    35 
#define MAX_LEDS    60

// --- PLATFORM SETUP ---
#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #include <Updater.h>
  #include <bearssl/bearssl_hash.h>
  #define LED_PIN        D2
  #define PIN_RESET      D4
  #define WHITE_LED_PIN  D1
  #define ONBOARD_LED    D4
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_wifi.h>
  #include <esp_ota_ops.h>
  #include <esp_partition.h>
  #include "mbedtls/sha256.h"
  #define LED_PIN        27
  #define PIN_RESET      23
  #define WHITE_LED_PIN  26
  #define ONBOARD_LED    2
#endif

// --- STRIP TYPE ---
#define LED_TYPE    WS2812B
#define COLOR_ORDER RGB

// --- WARM WHITE COLOR PROFILE (3000K) ---
// These values represent the RGB equivalent of the warm white LED
// Based on ~3000K color temperature (yellowish/warm tone)
#define WW_R 255
#define WW_G 180
#define WW_B 107

// --- GLOBALS ---
Preferences prefs;
uint32_t HOME_ID = 0; 
uint16_t numLeds = NUM_LEDS;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
int lastSuccessfulGatewayIndex = -1;

CRGB leds[MAX_LEDS];

bool isPaired = false;
unsigned long lastPairRequest = 0;
volatile bool deliveryComplete = false;
volatile bool deliverySuccess = false;

// --- OTA LED BREATHING (using onboard LED) ---
#define LED_PWM_FREQ 5000
#define LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;
bool breathingActive = false;
volatile bool breathingUpdatePending = false;

// --- GAMMA CORRECTION TABLE ---
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

// --- WARM WHITE PWM CONTROL ---
void setWhitePWM(uint8_t val) {
  // val is 0..255
  // Inverted: 0 = OFF (pin HIGH), 255 = FULL ON (pin LOW)
  #ifdef ESP32
    // Use LEDC PWM on ESP32
    static bool pwmConfigured = false;
    if (!pwmConfigured) {
      ledcAttach(WHITE_LED_PIN, LED_PWM_FREQ, LED_PWM_RESOLUTION);
      pwmConfigured = true;
    }
    ledcWrite(WHITE_LED_PIN, 255 - val);  // Inverted
  #else
    // Use analogWrite on ESP8266
    analogWrite(WHITE_LED_PIN, 255 - val);  // Inverted
  #endif
}

// --- WARM WHITE EXTRACTION ALGORITHM ---
// Calculates how much warm white component is shared across all pixels
// Returns the warm white level (0-255)
uint8_t extractWarmWhite(CRGB* pixelData, uint8_t count) {
  if (count == 0) return 0;
  
  // Find the maximum amount of warm white that ALL pixels contain
  uint8_t maxSharedWW = 255;
  
  for(int i = 0; i < count; i++) {
    uint8_t r = pixelData[i].r;
    uint8_t g = pixelData[i].g;
    uint8_t b = pixelData[i].b;
    
    // Calculate how much warm white fits in this pixel
    // by finding the limiting factor (bottleneck)
    uint16_t ww_from_r = ((uint16_t)r * 255) / WW_R;
    uint16_t ww_from_g = ((uint16_t)g * 255) / WW_G;
    uint16_t ww_from_b = ((uint16_t)b * 255) / WW_B;
    
    // The minimum determines how much warm white this pixel can contribute
    uint8_t pixel_ww = min(ww_from_r, min(ww_from_g, ww_from_b));
    
    // Track the minimum across all pixels (shared amount)
    if (pixel_ww < maxSharedWW) {
      maxSharedWW = pixel_ww;
    }
  }
  
  // Now subtract the warm white contribution from each pixel
  for(int i = 0; i < count; i++) {
    uint16_t ww_r = ((uint16_t)maxSharedWW * WW_R) / 255;
    uint16_t ww_g = ((uint16_t)maxSharedWW * WW_G) / 255;
    uint16_t ww_b = ((uint16_t)maxSharedWW * WW_B) / 255;
    
    pixelData[i].r = (pixelData[i].r > ww_r) ? (pixelData[i].r - ww_r) : 0;
    pixelData[i].g = (pixelData[i].g > ww_g) ? (pixelData[i].g - ww_g) : 0;
    pixelData[i].b = (pixelData[i].b > ww_b) ? (pixelData[i].b - ww_b) : 0;
  }
  
  return maxSharedWW;
}

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
  breathingUpdatePending = true;
}

void applyBreathingLed() {
  if (!breathingActive || !breathingUpdatePending) return;
  breathingUpdatePending = false;
  
  #ifdef ESP32
    ledcWrite(ONBOARD_LED, breathingBrightness);
  #else
    analogWrite(ONBOARD_LED, breathingBrightness);
  #endif
}

void startLedBreathing() {
  #ifdef ESP32
    ledcAttach(ONBOARD_LED, LED_PWM_FREQ, LED_PWM_RESOLUTION);
  #else
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
  setWhitePWM(0);  // Turn off white for status colors
  FastLED.show();
}

void flashStatus(CRGB color, int times) {
  for(int i=0; i<times; i++) {
    showStatusColor(color); 
    delay(100);
    showStatusColor(CRGB::Black); 
    delay(100);
  }
}

void saveGateways() { 
  prefs.putBytes("gw", &gateways, sizeof(gateways)); 
  Serial.printf("[GW] Saved %d gateways\n", gateways.count);
}

void addGateway(const uint8_t *mac) {
  for(int i=0; i<gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return; 
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.printf("[GW] Added new gateway: %02X:%02X:%02X:%02X:%02X:%02X\n", 
      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  }
}

// --- FORWARD DECLARATIONS ---
void sendHello();
void abortOta(const char* reason);
void handleOtaNotify(HueMixLinkPacket* pkt);
void handleOtaChunk(HueMixLinkPacket* pkt);
void handleOtaCheckpointReq(HueMixLinkPacket* pkt);
void handleOtaComplete(HueMixLinkPacket* pkt);
void handleOtaAbort(HueMixLinkPacket* pkt);

#if defined(ESP8266)
void OnDataSent(uint8_t *mac_addr, uint8_t status) {
  deliverySuccess = (status == 0);
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
  #else
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
  
  // Send PKT_OTA_READY immediately
  HueMixLinkPacket ready;
  memset(&ready, 0, sizeof(HueMixLinkPacket));
  ready.type = PKT_OTA_READY;
  WiFi.macAddress(ready.sourceMAC);
  memcpy(ready.targetMAC, pkt->sourceMAC, 6);
  ready.payload.otaReady.firmware_size = expected_firmware_size;
  ready.payload.otaReady.battery_mv = 0;
  ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
  
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
  
  startLedBreathing();
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) return;
  
  last_ota_activity = millis();
  
  uint16_t chunk_idx = pkt->payload.otaChunk.chunk_index;
  uint8_t data_len = pkt->payload.otaChunk.data_len;
  
  if (chunk_idx < expected_chunk_index) {
    return;  // Duplicate chunk
  }
  
  if (chunk_idx != expected_chunk_index) {
    Serial.printf("[OTA] Out of order chunk %d (expecting %d)\n", chunk_idx, expected_chunk_index);
    return;
  }
  
  #if defined(ESP32)
    esp_err_t err = esp_ota_write(update_handle, pkt->payload.otaChunk.data, data_len);
    if (err != ESP_OK) {
      Serial.printf("[OTA] Write failed at chunk %d: %d\n", chunk_idx, err);
      abortOta("Write failed");
      return;
    }
    mbedtls_sha256_update(&sha256_ctx, pkt->payload.otaChunk.data, data_len);
  #else
    size_t written = Update.write(pkt->payload.otaChunk.data, data_len);
    if (written != data_len) {
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
  memset(ack.targetMAC, 0, 6);
  ack.msgID = 0;
  ack.payload.otaChunkAck.last_chunk_index = last_chunk_index;
  ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
  
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
  if (otaState != OTA_RECEIVING) return;
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  sendOtaChunkAck(last_chunk);
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  if (otaState != OTA_RECEIVING) return;
  
  Serial.println("[OTA] COMPLETE received, validating...");
  otaState = OTA_VALIDATING;
  
  uint8_t calculated_sha256[32];
  #if defined(ESP32)
    mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
    mbedtls_sha256_free(&sha256_ctx);
  #else
    br_sha256_out(&sha256_ctx, calculated_sha256);
  #endif
  
  if (memcmp(calculated_sha256, expected_sha256, 32) != 0) {
    Serial.println("[OTA] SHA256 MISMATCH!");
    abortOta("SHA256 mismatch");
    return;
  }
  
  Serial.println("[OTA] SHA256 verified!");
  
  #if defined(ESP32)
    esp_err_t err = esp_ota_end(update_handle);
    if (err != ESP_OK) {
      abortOta("End failed");
      return;
    }
    update_handle = 0;
    
    err = esp_ota_set_boot_partition(update_partition);
    if (err != ESP_OK) {
      abortOta("Set boot failed");
      return;
    }
  #else
    if (!Update.end(true)) {
      abortOta("End failed");
      return;
    }
  #endif
  
  if (expected_chunk_index > 0) {
    sendOtaChunkAck(expected_chunk_index - 1);
  }
  
  Serial.println("[OTA] SUCCESS! Rebooting in 2 seconds...");
  otaState = OTA_COMPLETE;
  stopLedBreathing();
  
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
  
  pkt.payload.raw[0] = DEV_LIGHT; 
  pkt.payload.raw[1] = 0; 
  pkt.payload.raw[2] = 0;  // Not RGBW (global white handled separately)
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
  
  // Platform
  #if defined(ESP8266)
    pkt.payload.raw[8] = 1;
  #else
    pkt.payload.raw[8] = 0;
  #endif
  
  // Model ID
  pkt.payload.raw[9] = (uint8_t)(LIGHTSTRIP_MODEL & 0xFF);
  pkt.payload.raw[10] = (uint8_t)((LIGHTSTRIP_MODEL >> 8) & 0xFF);
  
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);

  #if defined(ESP32)
    esp_now_peer_info_t peerInfo = {};
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
  #endif

  if (HOME_ID != 0 && gateways.count > 0) {
    Serial.printf("[HELLO] Sending to %d gateways\n", gateways.count);
    
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
        Serial.printf("[HELLO] Gateway [%d] responded\n", i);
        lastSuccessfulGatewayIndex = i;
        break;
      }
    }
    
    // Move successful gateway to front
    if (lastSuccessfulGatewayIndex > 0) {
      uint8_t tempMac[6];
      memcpy(tempMac, gateways.macs[lastSuccessfulGatewayIndex], 6);
      for(int j = lastSuccessfulGatewayIndex; j > 0; j--) {
        memcpy(gateways.macs[j], gateways.macs[j-1], 6);
      }
      memcpy(gateways.macs[0], tempMac, 6);
      saveGateways();
    }
  } else {
    Serial.println("[HELLO] Broadcasting (unpaired)");
    #if defined(ESP8266)
      if(!esp_now_is_peer_exist(broadcastAddress)) 
        esp_now_add_peer(broadcastAddress, WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
    #else
      memcpy(peerInfo.peer_addr, broadcastAddress, 6);
      if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    #endif
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
  }
}

void processReceivedPacket(HueMixLinkPacket* rx, uint8_t* mac) {
  // OTA HANDLING - Verify signatures
  if (rx->type == PKT_OTA_NOTIFY || rx->type == PKT_OTA_CHUNK || 
      rx->type == PKT_OTA_CHECKPOINT_REQ || rx->type == PKT_OTA_COMPLETE || 
      rx->type == PKT_OTA_ABORT) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.println("[SEC] Invalid OTA signature - rejected");
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

  // PAIRING
  if (rx->type == PKT_PAIR_CONFIRM) {
    if (HOME_ID == 0) {
      uint32_t sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature == sig) {
        HOME_ID = rx->payload.pair.newHomeID;
        prefs.putUInt("hid", HOME_ID);
        addGateway(mac);
        isPaired = true;
        Serial.printf("[PAIR] SUCCESS! ID: 0x%X\n", HOME_ID);
        flashStatus(CRGB::Green, 3);
        sendHello();
      }
    }
  }
  
  // GATEWAY LIST UPDATE
  else if (rx->type == PKT_GW_LIST_UPD) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.println("[SEC] Invalid gateway list signature - rejected");
        return;
      }
    }
    
    if (rx->payload.gwList.count > 0) {
      Serial.printf("[GW] Received list with %d gateways\n", rx->payload.gwList.count);
      
      Payload_GatewayList newList;
      newList.count = 0;
      
      // Preserve existing gateways that are still valid
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
      
      // Add new gateways
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
    }
  }

  // LIGHT DATA PROCESSING WITH WARM WHITE EXTRACTION
  else if (rx->type == PKT_LIGHT_RAW) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.println("[SEC] Invalid light data signature - rejected");
        return;
      }
      
      uint8_t count = rx->payload.light.count;
      uint8_t masterBri = rx->payload.light.brightness;
      
      if (count > MAX_LEDS) count = MAX_LEDS;
      
      // Copy RGB data to temporary buffer
      CRGB tempLeds[MAX_LEDS];
      uint8_t* data = rx->payload.light.data;
      int idx = 0;
      
      for(int i=0; i<count; i++) {
        tempLeds[i].r = data[idx++];
        tempLeds[i].g = data[idx++];
        tempLeds[i].b = data[idx++];
      }
      
      // Extract warm white component (modifies tempLeds in-place)
      uint8_t warmWhiteLevel = extractWarmWhite(tempLeds, count);
      
      // Apply gamma correction and copy to actual LED buffer
      for(int i=0; i<count; i++) {
        leds[i].r = gamma8[tempLeds[i].r];
        leds[i].g = gamma8[tempLeds[i].g];
        leds[i].b = gamma8[tempLeds[i].b];
      }
      
      // Apply master brightness to warm white
      uint16_t finalWhite = ((uint16_t)gamma8[warmWhiteLevel] * masterBri) / 255;
      
      // Set outputs
      FastLED.setBrightness(masterBri);
      FastLED.show();
      setWhitePWM(finalWhite);
      
      // Logging (throttled)
      static uint8_t lastLoggedWhite = 0;
      static unsigned long lastLogTime = 0;
      if (abs(finalWhite - lastLoggedWhite) > 15 || (millis() - lastLogTime > 2000)) {
        Serial.printf("[LIGHT] Count=%d, Bri=%d, WW=%d/%d\n", 
          count, masterBri, warmWhiteLevel, finalWhite);
        lastLoggedWhite = finalWhite;
        lastLogTime = millis();
      }
    }
  }
  
  // SYSTEM COMMANDS
  else if (rx->type == PKT_SYS_CMD) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.println("[SEC] Invalid system command signature - rejected");
        return;
      }
      
      if (rx->payload.sys.cmd == 0xFF) {
        Serial.println("[SYS] Remote factory reset triggered!");
        prefs.clear();
        delay(500);
        ESP.restart();
      }
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
  // ESP8266: Queue packet for processing in main loop
  uint8_t nextHead = (packetQueueHead + 1) % PACKET_QUEUE_SIZE;
  if (nextHead != packetQueueTail) {
    memcpy(&packetQueue[packetQueueHead].packet, data, sizeof(HueMixLinkPacket));
    memcpy(packetQueue[packetQueueHead].mac, mac, 6);
    packetQueue[packetQueueHead].valid = true;
    packetQueueHead = nextHead;
  }
#else
  // ESP32: Process directly
  processReceivedPacket((HueMixLinkPacket*)data, mac);
#endif
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== HUEMIXLINK V3 - MODEL 5 (Warm White) ===");
  
  pinMode(PIN_RESET, INPUT_PULLUP);
  pinMode(WHITE_LED_PIN, OUTPUT);
  setWhitePWM(0);  // Off initially

  prefs.begin("huemixlink", false);
  
  // Load LED count
  numLeds = prefs.getUInt("leds", NUM_LEDS);
  if (numLeds > MAX_LEDS) numLeds = MAX_LEDS;
  
  if (!prefs.isKey("leds")) {
    prefs.putUInt("leds", numLeds);
    Serial.printf("[INIT] Saved default LED count: %d\n", numLeds);
  }
  
  Serial.printf("[INIT] LED Count: %d, Model: %d\n", numLeds, LIGHTSTRIP_MODEL);
  
  // Load pairing data
  HOME_ID = prefs.getUInt("hid", 0);
  size_t gwSize = prefs.getBytes("gw", &gateways, sizeof(gateways));
  if (gwSize != sizeof(gateways)) {
    gateways.count = 0;
  }
  Serial.printf("[INIT] Loaded %d gateways, HOME_ID: 0x%X\n", gateways.count, HOME_ID);
    
  // Initialize FastLED
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, MAX_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(255);
  FastLED.clear();
  FastLED.show();
  Serial.println("[INIT] FastLED initialized");

  // WiFi & ESP-NOW
  WiFi.mode(WIFI_STA);
  
  #if defined(ESP8266)
    if (esp_now_init() != 0) {
      Serial.println("[FATAL] ESP-NOW init failed");
      ESP.restart();
    }
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
    wifi_set_channel(HUEMIXLINK_CHANNEL); 
    esp_now_register_send_cb(OnDataSent);
    esp_now_add_peer(broadcastAddress, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  #else
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    if (esp_now_init() != ESP_OK) {
      Serial.println("[FATAL] ESP-NOW init failed");
      ESP.restart();
    }
    esp_wifi_set_channel(HUEMIXLINK_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = HUEMIXLINK_CHANNEL;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
  #endif

  esp_now_register_recv_cb(OnDataRecv);
  Serial.println("[INIT] ESP-NOW initialized");

  if (HOME_ID != 0) {
    isPaired = true;
    Serial.println("[INIT] Device is PAIRED");
    sendHello();
  } else {
    Serial.println("[INIT] Device is UNPAIRED");
  }
  
  Serial.println("=== READY ===\n");
}

void loop() {
  // Apply LED breathing update (OTA indicator)
  applyBreathingLed();
  
  // Process queued packets (ESP8266)
  #ifdef ESP8266
  while (packetQueueTail != packetQueueHead) {
    if (packetQueue[packetQueueTail].valid) {
      processReceivedPacket(&packetQueue[packetQueueTail].packet, packetQueue[packetQueueTail].mac);
      packetQueue[packetQueueTail].valid = false;
    }
    packetQueueTail = (packetQueueTail + 1) % PACKET_QUEUE_SIZE;
  }
  #endif
  
  // OTA timeout check
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    Serial.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  // Factory reset button
  if (digitalRead(PIN_RESET) == LOW) {
    unsigned long holdStart = millis();
    
    FastLED.setBrightness(100);
    for(int i=0; i<numLeds; i++) leds[i] = CRGB::Red;
    setWhitePWM(0);
    FastLED.show();
    
    while (digitalRead(PIN_RESET) == LOW) {
      if (millis() - holdStart > 5000) {
        Serial.println("[RESET] FACTORY RESET!");
        flashStatus(CRGB::Red, 5);
        
        prefs.remove("hid");
        prefs.remove("gw");
        Serial.println("[RESET] Cleared pairing data");
        
        ESP.restart();
      }
      delay(10);
    }
    
    Serial.println("[RESET] Aborted");
    FastLED.clear();
    setWhitePWM(0);
    FastLED.show();
    sendHello();
  }

  // Unpaired behavior - broadcast periodically
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
