/* 
   HUEMIXLINK V2 - BATTERY BUTTON (EAGER SYNC EDITION)
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <Bounce2.h>

#if defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <espnow.h>
#else
  #include <WiFi.h>
  #include <esp_now.h>
#endif

#if defined(ESP8266)
  #define PIN_BTN  D2
  #define PIN_AUX  D1
  #define PIN_LED  D4
#else  // ESP32
  #define PIN_BTN  27
  #define PIN_AUX  16
  #define PIN_LED  2
#endif
#define HOLD_TIME   1000
#define HOLD_INTERVAL 500
#define SLEEP_TIMEOUT 2000 

Preferences prefs;
uint32_t HOME_ID = 0; 
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Bounce button;
Bounce auxButton;

bool wakeupExt0 = false; 
unsigned long lastButtonPress = 0;
volatile bool ackReceived = false;
unsigned long lastActivityTime = 0;
unsigned long lastHoldSend = 0;
bool isHolding = false;
unsigned long btnPressTime = 0;
bool btnState = HIGH; 
bool homeSetupDone = false;
#if defined(ESP32)
  esp_now_peer_info_t peerInfo;
#endif


void ledBlink(int times, int delayMs) {
  for(int i=0; i<times; i++) {
    digitalWrite(PIN_LED, HIGH); delay(delayMs);
    digitalWrite(PIN_LED, LOW); delay(delayMs);
  }
}

void saveGateways() { prefs.putBytes("gw", &gateways, sizeof(gateways)); }

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

#if defined(ESP8266)
void OnDataRecv(uint8_t *mac_addr, uint8_t *data, uint8_t len) {
#else
void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
#endif
  if (len < sizeof(uint8_t)) return;
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;

  #if defined(ESP8266)
    const uint8_t *mac = mac_addr;
  #else
    const uint8_t *mac = info->src_addr;
  #endif

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
       gateways = rx->payload.gwList;
       saveGateways();
       Serial.printf("Updated Gateway List. Count: %d\n", gateways.count);
    }
  }
}

void sendPacket(uint8_t type, uint8_t action) {
  HueMixLinkPacket pkt;
  pkt.type = type;
  WiFi.macAddress(pkt.sourceMAC);
  
  if (type == PKT_BTN_EVENT) {
    pkt.payload.btn.action = action;
    pkt.payload.btn.battery_mv = 3300; 
    pkt.signature = calculateHash((uint8_t*)&pkt.payload, sizeof(Payload_Button), HOME_ID);
  } else if (type == PKT_HELLO) {
    pkt.payload.raw[0] = 2; 
    pkt.signature = calculateHash(pkt.payload.raw, 1, 0); 
  }

  ackReceived = false;
  bool sent = false;

  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    for(int i=0; i<gateways.count; i++) {
      #if defined(ESP8266)
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(gateways.macs[i], WiFi.channel(), ESP_NOW_ROLE_COMBO, NULL, 0);
        if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == 0) {
          unsigned long w = millis();
          while(millis() - w < 50 && !ackReceived) delay(1);
          if (ackReceived) { sent = true; break; }
        }
      #else
        memcpy(peerInfo.peer_addr,gateways.macs[i],6);
        peerInfo.channel=0;
        peerInfo.encrypt=false;
        if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
        if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
          unsigned long w = millis();
          while(millis() - w < 50 && !ackReceived) delay(1);
          if (ackReceived) { sent = true; break; }
        }
      #endif
    }
    // Visual feedback only for actual clicks, not hidden syncs
    if (action != ACT_SYNC) {
      if (sent) ledBlink(1, 200); else ledBlink(2, 200);
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
  delay(500);
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
  
  Serial.begin(115200);
  Serial.println("\n--- BUTTON WAKE ---");

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));

  WiFi.mode(WIFI_STA);
  #if defined(ESP32) 
    if (esp_now_init() != ESP_OK) ESP.restart();
  #else 
    if(esp_now_init() != 0) ESP.restart();
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
  #endif

  esp_now_register_recv_cb(OnDataRecv);

  button.attach(PIN_BTN, INPUT_PULLUP);
  button.interval(50);
  auxButton.attach(PIN_AUX, INPUT_PULLUP);
  auxButton.interval(50);

  lastActivityTime = millis();

  #if defined(ESP32)
  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  switch (wakeupReason) {
    case ESP_SLEEP_WAKEUP_EXT0:
      wakeupExt0 = true;
      Serial.println("Wakeup caused by EXT0 (button). Marking wakeupExt0=true");
      break;
    default:
      Serial.println("Cold boot or other wakeup reason -> going to sleep (OLD behavior)");
      goToSleep();
  }
  #endif
}

bool buttonPressed = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingIntervalUpdate = 0;

void loop() {
  button.update();
  auxButton.update();

  if (button.fell() || wakeupExt0) {
    wakeupExt0 = false; 
    lastButtonPress = millis();
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
    lastButtonPress = millis();
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

  // FACTORY RESET using AUX
  if (auxButton.read() == LOW) {
    lastActivityTime = millis();
    unsigned long holdStart = millis();
    while(auxButton.read() == LOW) {
      if (millis() - holdStart > 5000) {
        ledBlink(10, 50); prefs.clear(); HOME_ID = 0; gateways.count = 0; ESP.restart();
      }
      auxButton.update();
      delay(10);
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
  if (millis() - lastActivityTime > SLEEP_TIMEOUT) {
    if (button.read() == HIGH && auxButton.read() == HIGH) {
      goToSleep();
    }
  }
  #endif

  delay(10);
}