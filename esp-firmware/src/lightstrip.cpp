/* 
   HUEMIXLINK V2 - LIGHT STRIP (PHYSICAL RESET + CONFIG)
   - Support for WS2812B (RGB) and SK6812 (RGBW)
   - Hold BOOT button (GPIO 0) for 5s to Factory Reset
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <FastLED.h>

// --- CONFIGURATION ---
#define NUM_LEDS    5
#define MAX_LEDS    150

// --- SELECT YOUR STRIP TYPE HERE (Set IS_RGBW accordingly) ---

// Option 1: Standard RGB (WS2812B)
// const bool IS_RGBW = false;
// #define COLOR_ORDER GRB

// Option 2: RGBW (SK6812) - Cold/Warm/Natural White
const bool IS_RGBW = true;
#define COLOR_ORDER GRB 

// --- PLATFORM SETUP ---
#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #define LED_PIN     D2  // D2
  #define PIN_RESET   D4  // D3 (Flash Button)
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_wifi.h>
  #define LED_PIN     16  
  #define PIN_RESET   27  // BOOT Button
#endif

// --- GLOBALS ---
Preferences prefs;
uint32_t HOME_ID = 0; 
uint16_t numLeds = NUM_LEDS;

CRGB leds[MAX_LEDS]; 

uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
bool isPaired = false;
unsigned long lastPairRequest = 0;

// --- LED FEEDBACK ---
void showStatusColor(CRGB color) {
  for(int i=0; i<3; i++) leds[i] = color;
  FastLED.show();
}

void flashStatus(CRGB color, int times) {
  for(int i=0; i<times; i++) {
    showStatusColor(color); delay(100);
    showStatusColor(CRGB::Black); delay(100);
  }
}

// --- SEND HELLO ---
void sendHello() {
  HueMixLinkPacket pkt;
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  
  // Payload: [Type(3), RSSI_Hole, Flags, CountH, CountL]
  pkt.payload.raw[0] = 3; 
  pkt.payload.raw[1] = 0; 
  pkt.payload.raw[2] = IS_RGBW ? 1 : 0; // Config Flag
  pkt.payload.raw[3] = (uint8_t)((numLeds >> 8) & 0xFF);
  pkt.payload.raw[4] = (uint8_t)(numLeds & 0xFF);
  
  pkt.signature = calculateHash(pkt.payload.raw, 5, 0);

  static int ch = 1;
  #if defined(ESP8266)
    wifi_set_channel(ch);
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
  #else
    esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
  #endif
  
  // Visual Blip (Blue)
  leds[0] = CRGB::Blue; FastLED.show(); delay(10);
  leds[0] = CRGB::Black; FastLED.show();

  ch = (ch >= 11) ? 1 : ch + 5; 
}

// --- RECEIVE CALLBACK ---
#if defined(ESP8266)
void OnDataRecv(uint8_t *mac_addr, uint8_t *data, uint8_t len) {
#else
void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
#endif

  if (len < sizeof(HueMixLinkPacket)) return;
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;

  // 1. PAIRING
  if (rx->type == PKT_PAIR_CONFIRM) {
    if (HOME_ID == 0) {
      uint32_t sig = calculateHash((uint8_t*)&rx->payload, sizeof(Payload_Pairing), 0);
      if (rx->signature == sig) {
        HOME_ID = rx->payload.pair.newHomeID;
        prefs.putUInt("hid", HOME_ID);
        isPaired = true;
        Serial.printf("PAIRED! ID: 0x%X\n", HOME_ID);
        flashStatus(CRGB::Green, 3);
      }
    }
  }

  // 2. LIGHT DATA
  else if (rx->type == PKT_LIGHT_RAW) {
    if (HOME_ID != 0) {
      uint32_t sig = calculateHash((uint8_t*)&rx->payload, sizeof(Payload_Light), HOME_ID);
      
      if (rx->signature == sig) {
         uint8_t count = rx->payload.light.count;
         uint8_t bri   = rx->payload.light.brightness;
         
         if (count > MAX_LEDS) count = MAX_LEDS;
         
         FastLED.setBrightness(bri);

         uint8_t* d = rx->payload.light.data;
         int idx = 0;
         
         for(int i=0; i<count; i++) {
            leds[i] = CRGB(d[idx], d[idx+1], d[idx+2]);
            idx += 3; 
         }
         FastLED.show();
      }
    }
  }
  
  // 3. SYSTEM RESET (Remote)
  else if (rx->type == PKT_SYS_CMD) {
     if (rx->payload.sys.cmd == 0xFF) {
         if (rx->signature == calculateHash((uint8_t*)&rx->payload, sizeof(Payload_SysCmd), HOME_ID)) {
             prefs.clear(); ESP.restart();
         }
     }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RESET, INPUT_PULLUP); // Init Reset Button

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  
  // --- FASTLED CONFIGURATION ---
  
  if (IS_RGBW) {
    // >>> SK6812 (RGBW) <<<
    // Use .setRgbw(RgbwDefault()) to map standard RGB commands to the White channel
    FastLED.addLeds<SK6812, LED_PIN, COLOR_ORDER>(leds, MAX_LEDS).setRgbw(RgbwDefault());
    Serial.println("Config: RGBW Strip (SK6812)");
  } 
  else {
    // >>> WS2812B (RGB) <<<
    FastLED.addLeds<WS2812B, LED_PIN, COLOR_ORDER>(leds, MAX_LEDS);
    Serial.println("Config: RGB Strip (WS2812B)");
  }
  
  FastLED.setBrightness(255); 
  fill_solid(leds, MAX_LEDS, CRGB::Black);
  FastLED.show();

  // WiFi
  WiFi.mode(WIFI_STA);
  
  #if defined(ESP8266)
    if (esp_now_init() != 0) ESP.restart();
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
    esp_now_add_peer(broadcastAddress, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  #else
    if (esp_now_init() != ESP_OK) ESP.restart();
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
  #endif

  esp_now_register_recv_cb(OnDataRecv);

  if (HOME_ID != 0) {
    isPaired = true;
    Serial.printf("--- LIGHT READY (ID: 0x%X) ---\n", HOME_ID);
    flashStatus(CRGB::Green, 1);
  } else {
    Serial.println("--- LIGHT UNPAIRED ---");
  }
}

void loop() {
  // --- FACTORY RESET LOGIC ---
  if (digitalRead(PIN_RESET) == LOW) {
    unsigned long holdStart = millis();
    
    // Show visual warning (Red)
    fill_solid(leds, 5, CRGB::Red); FastLED.show();
    
    while (digitalRead(PIN_RESET) == LOW) {
      if (millis() - holdStart > 5000) {
        Serial.println("FACTORY RESET!");
        // Flash White to confirm
        for(int i=0; i<5; i++) {
            fill_solid(leds, 10, CRGB::White); FastLED.show(); delay(50);
            fill_solid(leds, 10, CRGB::Black); FastLED.show(); delay(50);
        }
        prefs.clear();
        ESP.restart();
      }
      delay(10);
    }
    
    // If released early, clear the warning red
    fill_solid(leds, 5, CRGB::Black); FastLED.show();
  }

  // --- UNPAIRED BEHAVIOR ---
  if (!isPaired) {
    if (millis() - lastPairRequest > 2000) {
      sendHello();
      lastPairRequest = millis();
    }
  }
  
  #if defined(ESP8266)
    yield();
  #endif
}