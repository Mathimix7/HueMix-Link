/* 
  HUEMIXLINK V3 - NORMAL BUTTON FIRMWARE
  Supports: ESP32 & ESP8266
  DEPRECATED - This firmware is no longer recommended for use with ESP32 Normal Button board V3 or later. Please use the new "Remote Button" firmware which supports both single and multiple button configurations.
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <Bounce2.h>
#include <Ticker.h>

#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #include <Updater.h>
  #include <bearssl/bearssl_hash.h>
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_ota_ops.h>
  #include <esp_partition.h>
  #include "mbedtls/sha256.h"
#endif

#if defined(ESP8266)
  #define PIN_BTN  D2
  #define PIN_AUX  D1
  #define PIN_LED  D4
  #define LED_ACTIVE_HIGH LOW
#else  // ESP32
  #define PIN_BTN  12
  #define PIN_AUX  13
  #define PIN_LED  18
  #define LED_ACTIVE_HIGH HIGH
#endif

#define HOLD_TIME     500
#define HOLD_INTERVAL 500
#define SLEEP_TIMEOUT 2000 

Preferences prefs;
uint32_t HOME_ID = 0; 
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Bounce button;
Bounce auxButton;

bool wakeupExt0 = false; 
volatile bool ackReceived = false;
unsigned long lastActivityTime = 0;
unsigned long lastHoldSend = 0;
bool isHolding = false;
unsigned long btnPressTime = 0;
bool btnState = HIGH; 
bool homeSetupDone = false;

unsigned long ledTimer = 0;
bool ledActive = false;

// --- OTA STATE MACHINE ---
enum OtaState { OTA_IDLE, OTA_WAITING_NOTIFY, OTA_RECEIVING, OTA_VALIDATING, OTA_COMPLETE };
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
unsigned long ota_wake_time = 0;
bool ota_mode = false;

// LED breathing (for OTA)
#define LED_PWM_FREQ 5000
#define LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;
bool breathingActive = false;
volatile bool breathingUpdatePending = false;  // Flag for interrupt-safe LED update

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

// Double-tap detection
unsigned long last_reset_press = 0;
uint8_t reset_tap_count = 0;
#define DOUBLE_TAP_WINDOW 1000

#if defined(ESP32)
  esp_now_peer_info_t peerInfo;
#endif

void triggerLed(int duration) {
  if (breathingActive) {
    return;
  }
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
  ledActive = true;
  ledTimer = millis() + duration;
}

void ledBlink(int times, int delayMs) {
  if (breathingActive) {
    return;
  }
  for(int i=0; i<times; i++) {
    digitalWrite(PIN_LED, LED_ACTIVE_HIGH); delay(delayMs);
    digitalWrite(PIN_LED, !LED_ACTIVE_HIGH); delay(delayMs);
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
  
  // Set flag to update LED in main loop (interrupt-safe for ESP8266)
  breathingUpdatePending = true;
}

void applyBreathingLed() {
  // This function is called from main loop, not interrupt
  if (!breathingActive || !breathingUpdatePending) return;
  breathingUpdatePending = false;
  
  #ifdef ESP32
    if (LED_ACTIVE_HIGH) {
      ledcWrite(PIN_LED, breathingBrightness);
    } else {
      ledcWrite(PIN_LED, 255 - breathingBrightness);
    }
  #else
    if (LED_ACTIVE_HIGH) {
      analogWrite(PIN_LED, breathingBrightness);
    } else {
      analogWrite(PIN_LED, 255 - breathingBrightness);
    }
  #endif

}

void startLedBreathing() {
  #ifdef ESP32
    ledcAttach(PIN_LED, LED_PWM_FREQ, LED_PWM_RESOLUTION);
  #else
    // ESP8266: Set PWM frequency and range
    analogWriteFreq(LED_PWM_FREQ);
    analogWriteRange(255);
    pinMode(PIN_LED, OUTPUT);
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
    ledcDetach(PIN_LED);
  #endif
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  breathingActive = false;
}

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
  ota_mode = false;
  ledBlink(3, 100); // Error indication
}

void saveGateways() { prefs.putBytes("gw", &gateways, sizeof(gateways)); }


void handleOtaNotify(HueMixLinkPacket* pkt) {
  if (otaState != OTA_WAITING_NOTIFY) {
    Serial.println("[OTA] Not in OTA mode");
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
    
    esp_err_t err = esp_ota_begin(update_partition, OTA_SIZE_UNKNOWN, &update_handle);
    if (err != ESP_OK) {
      Serial.printf("[OTA] Begin failed: %d\n", err);
      abortOta("Begin failed");
      return;
    }
    
    mbedtls_sha256_init(&sha256_ctx);
    mbedtls_sha256_starts(&sha256_ctx, 0);
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
  ready.payload.otaReady.battery_mv = 0;
  ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
  
  // Try sending to gateways sequentially
  bool sent = false;
  int successfulGatewayIndex = -1;
  
  for(int i = 0; i < gateways.count; i++) {
    #if defined(ESP8266)
      if(!esp_now_is_peer_exist(gateways.macs[i])) {
        esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      }
      if (esp_now_send(gateways.macs[i], (uint8_t*)&ready, sizeof(ready)) == 0) {
        sent = true;
        successfulGatewayIndex = i;
        Serial.printf("[OTA] Sent OTA_READY response via gateway %d\n", i);
        break;
      }
    #else
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
    #endif
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
  ota_wake_time = millis(); // Extend wake time
  
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
      Serial.printf("[OTA] Write failed at chunk %d\n", chunk_idx);
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
    triggerLed(20); // Visual feedback
  }
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
    #if defined(ESP8266)
      if(!esp_now_is_peer_exist(gateways.macs[i])) {
        esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      }
      if (esp_now_send(gateways.macs[i], (uint8_t*)&ack, sizeof(ack)) == 0) {
        sent = true;
        successfulGatewayIndex = i;
        Serial.printf("[OTA] Sent checkpoint ACK via gateway %d: last_chunk=%d\n", i, last_chunk);
        break;
      }
    #else
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
    #endif
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
    abortOta("SHA256 mismatch");
    return;
  }
  
  Serial.println("[OTA] SHA256 verified!");
  
  #if defined(ESP32)
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
  #else
    if (!Update.end(true)) {
      Serial.printf("[OTA] End failed: %s\n", Update.getErrorString().c_str());
      abortOta("End failed");
      return;
    }
  #endif
  
  Serial.println("[OTA] UPDATE SUCCESSFUL! Rebooting...");
  otaState = OTA_COMPLETE;
  
  stopLedBreathing();
  ledBlink(10, 100); // Success indication
  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  Serial.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

void addGateway(const uint8_t *mac) {
  for(int i=0; i<gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return; 
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.println("New Gateway Saved");
  }
}

void processReceivedPacket(HueMixLinkPacket *rx, const uint8_t *mac) {
  // OTA Handling
  // Security: Verify all OTA packets before processing
  if (rx->type == PKT_OTA_NOTIFY || rx->type == PKT_OTA_CHUNK || 
      rx->type == PKT_OTA_CHECKPOINT_REQ || rx->type == PKT_OTA_COMPLETE || 
      rx->type == PKT_OTA_ABORT) {
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
      if (rx->signature != expected_sig) {
        Serial.printf("[BTN] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, rx->signature);
        Serial.println("[BTN] Rejected unauthorized OTA packet");
        return;
      }
    }
  }
  
  if (rx->type == PKT_OTA_NOTIFY) {
    handleOtaNotify(rx);
    lastActivityTime = millis();
    ota_wake_time = millis(); // Keep awake for OTA
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
      uint32_t sig = calculateHash(rx->payload.raw, 185, 0);
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
         uint32_t expected_sig = calculateHash(rx->payload.raw, 185, HOME_ID);
         if (rx->signature != expected_sig) {
           Serial.printf("[BTN] SECURITY: Invalid signature on gateway list. Expected 0x%08X, got 0x%08X\n", expected_sig, rx->signature);
           Serial.println("[BTN] Rejected unauthorized gateway list");
           return;
         }
       }
       
       Serial.printf("[BTN] Server sent %d gateways:\n", rx->payload.gwList.count);
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
    const uint8_t *mac = info->src_addr;
  #endif

#ifdef ESP8266
  // Exception: PKT_ACK_TO_BTN is processed immediately in interrupt for fast response
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;
  if (rx->type == PKT_ACK_TO_BTN) {
    processReceivedPacket(rx, mac);
    return;
  }

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

void sendPacket(uint8_t type, uint8_t action) {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = type;
  WiFi.macAddress(pkt.sourceMAC);
  
  if (type == PKT_BTN_EVENT) {
    pkt.payload.btn.action = action;
    pkt.payload.btn.battery_mv = 0;
    pkt.payload.btn.button_index = -1; // Normal button always sends -1
    
    // Add version information
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
    
    // Platform indicator: 0=ESP32, 1=ESP8266
    #if defined(ESP8266)
      pkt.payload.btn.platform = 1;
    #else
      pkt.payload.btn.platform = 0;
    #endif
    
    // Button count: 0 for normal buttons (not applicable)
    pkt.payload.btn.button_count = 0;
    
    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  } else if (type == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_BUTTON;
    pkt.payload.raw[1] = 0; // RSSI placeholder (gateway fills this)
    
    // Parse version from build flag (format: "3.7.3")
    #ifdef FIRMWARE_VERSION
      const char* ver = FIRMWARE_VERSION;
      uint8_t major = 0, minor = 0, patch = 0;
      sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
      pkt.payload.raw[2] = major;
      pkt.payload.raw[3] = minor;
      pkt.payload.raw[4] = patch;
      // Build byte indicates platform: 0=ESP32, 1=ESP8266
      #if defined(ESP8266)
        pkt.payload.raw[5] = 1; // ESP8266
      #else
        pkt.payload.raw[5] = 0; // ESP32
      #endif
    #else
      pkt.payload.raw[2] = 0;
      pkt.payload.raw[3] = 0;
      pkt.payload.raw[4] = 0;
      #if defined(ESP8266)
        pkt.payload.raw[5] = 1; // ESP8266
      #else
        pkt.payload.raw[5] = 0; // ESP32
      #endif
    #endif
    

    pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID != 0 ? HOME_ID : 0);
  }
  
  ackReceived = false;
  bool sent = false;
  int successfulGatewayIndex = -1;

  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    Serial.printf("[BTN] Trying %d gateways for packet type 0x%02X action %d\n", gateways.count, type, action);
    
    for(int i=0; i<gateways.count; i++) {
      Serial.printf("[BTN] Attempt %d/%d: %02X:%02X:%02X:%02X:%02X:%02X\n", i+1, gateways.count,
        gateways.macs[i][0], gateways.macs[i][1], gateways.macs[i][2],
        gateways.macs[i][3], gateways.macs[i][4], gateways.macs[i][5]);
      
      #if defined(ESP8266)
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
        if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == 0) {
          unsigned long w = millis();
          while(millis() - w < 150 && !ackReceived) delay(1);
          if (ackReceived) { 
            Serial.println("[BTN]   ACK received!");
            sent = true; successfulGatewayIndex = i; break; 
          }
        }
      #else
        memcpy(peerInfo.peer_addr,gateways.macs[i],6);
        peerInfo.channel=0;
        peerInfo.encrypt=false;
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
        if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
          unsigned long w = millis();
          while(millis() - w < 75 && !ackReceived) delay(1);
          if (ackReceived) { 
            sent = true; successfulGatewayIndex = i; break; 
          }
        }
      #endif
    }
    
    // Move successful gateway to front of list
    if (successfulGatewayIndex > 0) {
      Serial.printf("[BTN] Moving gateway %d to front\n", successfulGatewayIndex);
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
    #if defined(ESP8266)
      if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(broadcastAddress, WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
    #else
      memcpy(peerInfo.peer_addr, broadcastAddress, 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    #endif
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
    unsigned long w = millis();
    while(millis() - w < 500 && !ackReceived) delay(1);
    if(!ackReceived) ledBlink(2, 200); 
  }
}

#if defined(ESP32)
void goToSleep() {
  delay(100);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  Serial.println("Going to sleep now");
  Serial.flush();

  esp_sleep_enable_ext0_wakeup((gpio_num_t)PIN_BTN, 0); 
  esp_deep_sleep_start();
}
#endif

void setup() {
  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_AUX, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  
  Serial.begin(115200);
  Serial.println("\n--- BUTTON WAKE ---");

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));

  WiFi.mode(WIFI_STA);
  #if defined(ESP32) 
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    if (esp_now_init() != ESP_OK) ESP.restart();
  #else 
    if(esp_now_init() != 0) ESP.restart();
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
  #endif

  esp_now_register_recv_cb(OnDataRecv);

  button.attach(PIN_BTN, INPUT_PULLUP);
  button.interval(25); 
  auxButton.attach(PIN_AUX, INPUT_PULLUP);
  auxButton.interval(25);

  lastActivityTime = millis();

  #if defined(ESP32)
    esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
    if(wakeupReason == ESP_SLEEP_WAKEUP_EXT0) {
        wakeupExt0 = true;
        Serial.println("Wakeup caused by Button");
    } else {
        // Check if we just rebooted from OTA update
        esp_reset_reason_t reset_reason = esp_reset_reason();
        if (reset_reason == ESP_RST_SW) {
          Serial.println("[OTA] Detected software reset (post-OTA reboot)");
          if (HOME_ID != 0) {
            sendPacket(PKT_HELLO, 0); // Send HELLO with new version
            Serial.println("[OTA] Sent post-OTA HELLO with new version");
            delay(500); // Give time for transmission
          }
        }
        
        Serial.println("Cold boot");
        ledBlink(5, 100);
        goToSleep();
    }
  #else
    // ESP8266: Check if we just rebooted from OTA update
    String reset_reason = ESP.getResetReason();
    Serial.printf("ESP8266 Reset Reason: %s\n", reset_reason.c_str());
    
    if (reset_reason == "Software/System restart" || reset_reason.indexOf("Software") >= 0) {
      Serial.println("[OTA] Detected software reset (post-OTA reboot)");
      if (HOME_ID != 0) {
        delay(500); // Give ESP8266 time to stabilize after reset
        sendPacket(PKT_HELLO, 0); // Send HELLO with new version
        Serial.println("[OTA] Sent post-OTA HELLO with new version");
        delay(500); // Give time for transmission
      }
    }
    
    Serial.println("ESP8266 ready");
    ledBlink(3, 100);
  #endif
}

bool buttonPressed = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingIntervalUpdate = 0;

void loop() {
  button.update();
  auxButton.update();
  
  // Apply LED breathing update (interrupt-safe for ESP8266)
  applyBreathingLed();
  
  #ifdef ESP8266
    // Process queued packets (deferred from interrupt handler)
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

  // OTA wake timeout - keep device awake for 30s after entering OTA mode
  if (otaState == OTA_WAITING_NOTIFY && millis() - ota_wake_time > 30000) {
    Serial.println("[OTA] No NOTIFY received within 30s, returning to normal");
    otaState = OTA_IDLE;
    ota_mode = false;
  }

  if (ledActive) {
    if (millis() > ledTimer) {
      digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
      ledActive = false;
    }
  }

  if (wakeupExt0) {
    if (digitalRead(PIN_BTN) == HIGH) {
      // Button was released during wakeup
      if (HOME_ID == 0) {
        sendPacket(PKT_HELLO, 0);
      } else {
        sendPacket(PKT_BTN_EVENT, ACT_CLICK);
      }
      lastActivityTime = millis();
      wakeupExt0 = false; 
      buttonPressed = false; 
    } else {
      // Still holding button, continue to normal state
      buttonHoldStartTime = millis();
      buttonPressed = true;
      lastActivityTime = millis();
      wakeupExt0 = false; 
    }
  }

  if (button.fell()) {
    buttonHoldStartTime = millis();
    buttonPressed = true;
    lastActivityTime = millis();
  }

  if (button.read() == LOW && buttonPressed && !isHolding) {
    if (millis() - buttonHoldStartTime >= HOLD_TIME) {
      isHolding = true; 
      if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING);
      holdingIntervalUpdate = millis();
    }
  }

  if (millis() - holdingIntervalUpdate >= HOLD_INTERVAL && isHolding) {
    if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING);
    holdingIntervalUpdate = millis();
    lastActivityTime = millis();
  }

  if (button.rose()) {
    if (isHolding) {
      isHolding = false;
      if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_RELEASE);
      Serial.println("HoldingStopped");
    } else {
      if (HOME_ID == 0) {
        sendPacket(PKT_HELLO, 0);
        Serial.println("Once (pair request)");
      } else {
        sendPacket(PKT_BTN_EVENT, ACT_CLICK);
        Serial.println("Once (click)");
      }
    }
    buttonPressed = false;
    lastActivityTime = millis();
  }

  // FACTORY RESET using AUX (hold 5s) OR OTA MODE (double-tap)
  static unsigned long aux_hold_start = 0;
  
  // Detect falling edge (button pressed)
  if (auxButton.fell()) {
    unsigned long now = millis();
    
    // Check if this is within double-tap window
    if (now - last_reset_press < DOUBLE_TAP_WINDOW) {
      reset_tap_count++;
    } else {
      reset_tap_count = 1;
    }
    last_reset_press = now;
    aux_hold_start = now; // Track hold start for factory reset
  }
  
  // Detect rising edge (button released)
  if (auxButton.rose()) {
    // Second tap released - enter OTA mode
    if (reset_tap_count >= 2 && !ota_mode) {
      // Double-tap detected - enter OTA mode
      Serial.println("[OTA] Double-tap detected! Entering OTA mode...");
      otaState = OTA_WAITING_NOTIFY;
      ota_mode = true;
      ota_wake_time = millis();
      lastActivityTime = millis();
      ledBlink(3, 200); // OTA mode indication
      
      // Send OTA_READY packet with firmware_size=0 to announce OTA mode
      HueMixLinkPacket ready;
      memset(&ready, 0, sizeof(HueMixLinkPacket));
      ready.type = PKT_OTA_READY;
      WiFi.macAddress(ready.sourceMAC);
      memset(ready.targetMAC, 0xFF, 6); // Broadcast to any gateway
      ready.payload.otaReady.firmware_size = 0; // Announce OTA mode
      ready.payload.otaReady.battery_mv = 0;
      ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
      
      // Try sending to gateways sequentially
      bool sent = false;
      int successfulGatewayIndex = -1;
      
      for(int i = 0; i < gateways.count; i++) {
        #if defined(ESP32)
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
        #else
          if (!esp_now_is_peer_exist(gateways.macs[i])) {
            esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
          }
          if (esp_now_send(gateways.macs[i], (uint8_t*)&ready, sizeof(ready)) == 0) {
            sent = true;
            successfulGatewayIndex = i;
            Serial.printf("[OTA] Sent OTA_READY announcement via gateway %d\n", i);
            break;
          }
        #endif
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
  
  // Check for factory reset (5s hold on aux button)
  if (auxButton.read() == LOW && !ota_mode) {
    if (millis() - aux_hold_start > 5000) {
      Serial.println("[RESET] Factory reset triggered!");
      ledBlink(10, 50);
      prefs.clear();
      HOME_ID = 0;
      gateways.count = 0;
      ESP.restart();
    }
  }

  if (homeSetupDone) {
    prefs.putUInt("hid", HOME_ID);
    ledBlink(5, 50); 
    Serial.println("Paired! Requesting Full Gateway List...");
    sendPacket(PKT_BTN_EVENT, ACT_SYNC); 
    lastActivityTime = millis(); 
    homeSetupDone = false;
  }

  #if !defined(ESP8266)
  // If been idle long enough, go to sleep (using your new SLEEP_TIMEOUT)
  // But don't sleep if in OTA mode
  if (!ota_mode && millis() - lastActivityTime > SLEEP_TIMEOUT) {
    if (button.read() == HIGH && auxButton.read() == HIGH) {
      goToSleep();
    }
  }
  #endif

  delay(5);
}