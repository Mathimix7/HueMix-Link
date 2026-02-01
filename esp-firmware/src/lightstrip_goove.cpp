/* 
  HUEMIXLINK V3 - LIGHT STRIP FIRMWARE
  MODIFIED FOR: Individual RGB + Global Inverted PWM White
  FEATURES: Serial Logging & Shared White Extraction
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <FastLED.h>

// --- CONFIGURATION ---
#define NUM_LEDS    35 
#define MAX_LEDS    60

// --- HARDWARE PINS ---
#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #define LED_PIN        D2
  #define PIN_RESET      D4
  #define WHITE_LED_PIN  D1  
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_wifi.h>
  #define LED_PIN        27  
  #define PIN_RESET      23
  #define WHITE_LED_PIN  26  
#endif

// --- STRIP TYPE ---
#define LED_TYPE    WS2812B
#define COLOR_ORDER RGB

// --- GLOBALS ---
Preferences prefs;
uint32_t HOME_ID = 0; 
uint16_t numLeds = NUM_LEDS;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
CRGB leds[MAX_LEDS];

bool isPaired = false;
unsigned long lastPairRequest = 0;
volatile bool deliveryComplete = false;
volatile bool deliverySuccess = false;

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

// --- HELPER FUNCTIONS ---

void setWhitePWM(uint8_t val) {
  // val is 0..255. Inverted: 0 is FULL ON (pin low), 255 is OFF (pin high)
  analogWrite(WHITE_LED_PIN, 255 - val);
}

void showStatusColor(CRGB color) {
  for(int i=0; i<numLeds; i++) leds[i] = color;
  setWhitePWM(0); // White PWM off for status indications
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
  Serial.printf("[INIT] Gateway list saved. Count: %d\n", gateways.count);
}

void addGateway(const uint8_t *mac) {
  for(int i=0; i<gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return; 
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.printf("[NOW] Added new gateway: %02X:%02X:%02X:%02X:%02X:%02X\n", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  }
}

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

void sendHello() {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  pkt.payload.raw[0] = DEV_LIGHT; 
  pkt.payload.raw[1] = 0; // Flags
  pkt.payload.raw[2] = 0;
  pkt.payload.raw[3] = (uint8_t)((numLeds >> 8) & 0xFF);
  pkt.payload.raw[4] = (uint8_t)(numLeds & 0xFF);
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);

  Serial.println("[NOW] Sending HELLO packet...");

  #if defined(ESP32)
    esp_now_peer_info_t peerInfo = {};
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
  #endif

  if (HOME_ID != 0 && gateways.count > 0) {
    for(int i=0; i<gateways.count; i++) {
      #if defined(ESP8266)
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
      #else
        memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
      #endif
      deliveryComplete = false;
      esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt));
      
      unsigned long startWait = millis();
      while(!deliveryComplete && (millis() - startWait < 200)) delay(1);
      
      if (deliverySuccess) {
        Serial.printf("[NOW] Hello delivered to gateway %d\n", i);
        break;
      }
    }
  } else {
    Serial.println("[NOW] Broadcasting Hello (Unpaired)");
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
  }
}

// --- DATA RECEIVE ---
#if defined(ESP8266)
void OnDataRecv(uint8_t *mac_addr, uint8_t *data, uint8_t len) {
#else
void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
#endif
  if (len < sizeof(HueMixLinkPacket)) return;
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;
  #if defined(ESP8266)
    const uint8_t *mac = mac_addr;
  #else
    const uint8_t *mac = info->src_addr;
  #endif

  // 1. PAIRING LOGIC
  if (rx->type == PKT_PAIR_CONFIRM && HOME_ID == 0) {
    Serial.println("[NOW] Pair Confirm received. Validating...");
    if (rx->signature == calculateHash(rx->payload.raw, 185, HOME_ID)) {
      HOME_ID = rx->payload.pair.newHomeID;
      prefs.putUInt("hid", HOME_ID);
      addGateway(mac);
      isPaired = true;
      Serial.printf("[INIT] Successfully Paired! Home ID: 0x%X\n", HOME_ID);
      flashStatus(CRGB::Green, 3);
      sendHello();
    } else {
      Serial.println("[ERR] Pair signature mismatch!");
    }
  }
  
  // 2. LIGHT DATA LOGIC
  else if (rx->type == PKT_LIGHT_RAW && HOME_ID != 0) {
    if (rx->signature == calculateHash(rx->payload.raw, 185, HOME_ID)) {
       uint8_t count = rx->payload.light.count;
       uint8_t bri   = rx->payload.light.brightness;
       if (count > MAX_LEDS) count = MAX_LEDS;
       FastLED.setBrightness(bri);

       uint8_t* d = rx->payload.light.data;
       
       // PASS 1: Shared White Extraction logic
       uint8_t commonWhite = 255;
       int scanIdx = 0;
       for(int i=0; i<count; i++) {
          uint8_t r = d[scanIdx];
          uint8_t g = d[scanIdx+1];
          uint8_t b = d[scanIdx+2];
          uint8_t pixelMin = min(r, min(g, b));
          if (pixelMin < commonWhite) commonWhite = pixelMin;
          scanIdx += 3;
       }

       // PASS 2: Update Pixels
       int applyIdx = 0;
       for(int i=0; i<count; i++) {
          leds[i].r = gamma8[d[applyIdx]   - commonWhite];
          leds[i].g = gamma8[d[applyIdx+1] - commonWhite];
          leds[i].b = gamma8[d[applyIdx+2] - commonWhite];
          applyIdx += 3;
       }

       uint8_t finalWhite = (uint16_t(gamma8[commonWhite]) * bri) / 255;
       setWhitePWM(finalWhite);
       FastLED.show();

       // Log occasionally or on significant changes to avoid spamming
       static uint8_t lastLoggedWhite = 0;
       if (abs(finalWhite - lastLoggedWhite) > 10) {
         Serial.printf("[LIGHT] Updating: Leds=%d, MasterBri=%d, WhitePWM=%d\n", count, bri, finalWhite);
         lastLoggedWhite = finalWhite;
       }
    }
  }
  
  // 3. SYSTEM COMMANDS
  else if (rx->type == PKT_SYS_CMD) {
    Serial.printf("[NOW] Sys Command 0x%02X received.\n", rx->payload.sys.cmd);
    if (rx->signature == calculateHash(rx->payload.raw, 185, HOME_ID)) {
        if (rx->payload.sys.cmd == 0xFF) { 
          Serial.println("[RESET] Remote Factory Reset Triggered!");
          prefs.clear(); delay(500); ESP.restart(); 
        }
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n[INIT] --- HUEMIXLINK LIGHT STARTING ---");

  pinMode(PIN_RESET, INPUT_PULLUP); 
  pinMode(WHITE_LED_PIN, OUTPUT);
  setWhitePWM(0); // Ensure off initially

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  Serial.printf("[INIT] Stored Home ID: 0x%X\n", HOME_ID);
  
  size_t gwSize = prefs.getBytes("gw", &gateways, sizeof(gateways));
  if (gwSize != sizeof(gateways)) {
    gateways.count = 0;
    Serial.println("[INIT] No gateways found in flash.");
  } else {
    Serial.printf("[INIT] Loaded %d gateways from flash.\n", gateways.count);
  }

  uint16_t storedLeds = prefs.getUInt("leds", 0);
  if (storedLeds > 0) numLeds = storedLeds;
  Serial.printf("[INIT] Configured for %d LEDs.\n", numLeds);
    
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, MAX_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(255); 
  FastLED.clear(true);
  Serial.println("[INIT] FastLED initialized.");

    WiFi.mode(WIFI_STA);
  delay(100); // Give the radio a moment to power up

  // Force a hardware read if it's still zero
  int retry = 0;
  while (WiFi.macAddress() == "00:00:00:00:00:00" && retry < 10) {
    Serial.println("[INIT] Waiting for WiFi hardware to report MAC...");
    WiFi.disconnect();
    WiFi.mode(WIFI_OFF);
    delay(100);
    WiFi.mode(WIFI_STA);
    delay(500);
    retry++;
  }

  String macAddr = WiFi.macAddress();
  Serial.printf("[INIT] MAC Address: %s\n", macAddr.c_str());

  if (macAddr == "00:00:00:00:00:00") {
    Serial.println("[FATAL] WiFi Hardware failed to initialize. Restarting...");
    delay(2000);
    ESP.restart();
  }

  #if defined(ESP8266)
    if (esp_now_init() != 0) { Serial.println("[ERR] ESP-NOW Init Failed"); ESP.restart(); }
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
    esp_now_register_send_cb(OnDataSent);
    esp_now_add_peer(broadcastAddress, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  #else
    if (esp_now_init() != ESP_OK) { Serial.println("[ERR] ESP-NOW Init Failed"); ESP.restart(); }
    esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
  #endif

  esp_now_register_recv_cb(OnDataRecv);
  Serial.println("[INIT] ESP-NOW Ready.");

  if (HOME_ID != 0) {
    isPaired = true;
    sendHello();
  } else {
    Serial.println("[INIT] Device is currently UNPAIRED.");
  }
}

void loop() {
  // FACTORY RESET BUTTON LOGIC
  if (digitalRead(PIN_RESET) == LOW) {
    unsigned long holdStart = millis();
    Serial.println("[RESET] Reset button pressed...");
    showStatusColor(CRGB::Red);
    
    while (digitalRead(PIN_RESET) == LOW) {
      if (millis() - holdStart > 5000) {
        Serial.println("[RESET] 5 second hold detected. WIPING FLASH!");
        flashStatus(CRGB::Red, 5);
        prefs.clear(); 
        delay(500);
        ESP.restart();
      }
      delay(10);
    }
    Serial.println("[RESET] Reset aborted.");
    showStatusColor(CRGB::Black);
    sendHello();
  }

  // RE-BROADCAST HELLO IF UNPAIRED
  if (!isPaired && (millis() - lastPairRequest > 5000)) {
    Serial.println("[NOW] Still unpaired, retrying Hello broadcast...");
    sendHello();
    lastPairRequest = millis();
  }

  #if defined(ESP8266)
    yield();
  #endif
}