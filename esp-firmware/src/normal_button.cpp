/* 
  HUEMIXLINK V3 - NORMAL BUTTON FIRMWARE
  Supports: ESP32 & ESP8266
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

#if defined(ESP32)
  esp_now_peer_info_t peerInfo;
#endif

void triggerLed(int duration) {
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
  ledActive = true;
  ledTimer = millis() + duration;
}

void ledBlink(int times, int delayMs) {
  for(int i=0; i<times; i++) {
    digitalWrite(PIN_LED, LED_ACTIVE_HIGH); delay(delayMs);
    digitalWrite(PIN_LED, !LED_ACTIVE_HIGH); delay(delayMs);
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

void sendPacket(uint8_t type, uint8_t action) {
  HueMixLinkPacket pkt;
  pkt.type = type;
  WiFi.macAddress(pkt.sourceMAC);
  
  if (type == PKT_BTN_EVENT) {
    pkt.payload.btn.action = action;
    pkt.payload.btn.battery_mv = 3300; 
    pkt.payload.raw[3] = 255;
    pkt.signature = calculateHash((uint8_t*)&pkt.payload, sizeof(Payload_Button), HOME_ID);
  } else if (type == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_BUTTON;
    pkt.signature = calculateHash(pkt.payload.raw, 1, 0); 
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
          while(millis() - w < 75 && !ackReceived) delay(1);
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
      Serial.println("Cold boot");
      ledBlink(5, 100);
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

  delay(5);
}