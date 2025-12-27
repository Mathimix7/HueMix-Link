/* 
  HUEMIXLINK V3 - LIGHT STRIP FIRMWARE
  Supports: ESP32 & ESP8266
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <FastLED.h>

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
// #define LED_TYPE WS2812B
// #define COLOR_ORDER GRB 

// --- PLATFORM SETUP ---
#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
  #define LED_PIN     D2
  #define PIN_RESET   D4
#else
  #include <WiFi.h>
  #include <esp_now.h>
  #include <esp_wifi.h>
  #define LED_PIN     16  
  #define PIN_RESET   27
#endif

// --- GLOBALS ---
Preferences prefs;
uint32_t HOME_ID = 0; 
uint16_t numLeds = NUM_LEDS;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
int lastSuccessfulGatewayIndex = -1;

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

// --- SEND HELLO ---
void sendHello() {
  HueMixLinkPacket pkt;
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  
  // Payload: [Type(3), RSSI_Hole, Flags, CountH, CountL]
  pkt.payload.raw[0] = 3; 
  pkt.payload.raw[1] = 0; 
  pkt.payload.raw[2] = IS_RGBW ? 1 : 0;
  pkt.payload.raw[3] = (uint8_t)((numLeds >> 8) & 0xFF);
  pkt.payload.raw[4] = (uint8_t)(numLeds & 0xFF);
  
  pkt.signature = calculateHash(pkt.payload.raw, 5, HOME_ID);

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

// --- RECEIVE CALLBACK ---
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

  Serial.printf("[LIGHT] Packet Received. Type: 0x%02X From: %02X:%02X:%02X:%02X:%02X:%02X\n",
    rx->type,
    mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

  // 1. PAIRING
  if (rx->type == PKT_PAIR_CONFIRM) {
    if (HOME_ID == 0) {
      uint32_t sig = calculateHash((uint8_t*)&rx->payload, sizeof(Payload_Pairing), HOME_ID);
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
      uint32_t sig = calculateHash((uint8_t*)&rx->payload, sizeof(Payload_Light), HOME_ID);
      
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
    if (rx->signature == calculateHash((uint8_t*)&rx->payload, sizeof(Payload_SysCmd), HOME_ID)) {
        // Factory Reset
        if (rx->payload.sys.cmd == 0xFF) {
          prefs.clear(); ESP.restart();
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
  HOME_ID = prefs.getUInt("hid", 0);
  
  // Load saved gateways
  size_t gwSize = prefs.getBytes("gw", &gateways, sizeof(gateways));
  if (gwSize != sizeof(gateways)) {
    gateways.count = 0;
  }
  Serial.printf("Loaded %d gateways from memory\n", gateways.count);

  uint16_t storedLeds = prefs.getUInt("leds", 0);
  if (storedLeds > 0) numLeds = storedLeds;
  if (numLeds > MAX_LEDS) numLeds = MAX_LEDS;
    
  #if IS_RGBW
    uint16_t virtualLeds = (MAX_LEDS * 4 + 2) / 3; 
    FastLED.addLeds<LED_TYPE, LED_PIN, RGB>(ledsAsRGB, virtualLeds).setCorrection(TypicalLEDStrip);
  #else
    FastLED.addLeds<LED_TYPE, LED_PIN, GRB>(leds, MAX_LEDS).setCorrection(TypicalLEDStrip);
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
        prefs.clear();
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