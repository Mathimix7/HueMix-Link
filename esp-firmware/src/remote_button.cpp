/* 
  HUEMIXLINK V3 - REMOTE BUTTON FIRMWARE (4-BUTTON)
  Supports: ESP32 only
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <Bounce2.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_adc_cal.h>

// GPIO pins with pull-down resistors (HIGH = pressed)
#define PIN_BTN0  33
#define PIN_BTN1  32
#define PIN_BTN2  27
#define PIN_BTN3  26
#define PIN_RESET 14
#define PIN_LED   18
#define PIN_BATTERY 35

#define LED_ACTIVE_HIGH HIGH

#define HOLD_TIME     500
#define HOLD_INTERVAL 500
#define SLEEP_TIMEOUT 2000

// EXT1 bitmask for all 4 buttons + reset
#define EXT1_BUTTON_MASK ((1ULL << PIN_BTN0) | (1ULL << PIN_BTN1) | (1ULL << PIN_BTN2) | (1ULL << PIN_BTN3) | (1ULL << PIN_RESET))

Preferences prefs;
uint32_t HOME_ID = 0;
Payload_GatewayList gateways;
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Bounce buttons[4];
const uint8_t buttonPins[4] = {PIN_BTN0, PIN_BTN1, PIN_BTN2, PIN_BTN3};

bool wakeupExt1 = false;
int wakeupButtonIndex = -1;
bool wakeupWasReset = false;
unsigned long lastActivityTime = 0;
volatile bool ackReceived = false;

// Per-button state
struct ButtonState {
  bool pressed = false;
  unsigned long holdStartTime = 0;
  unsigned long holdingIntervalUpdate = 0;
  bool isHolding = false;
};

ButtonState buttonStates[4];

unsigned long ledTimer = 0;
bool ledActive = false;
bool homeSetupDone = false;
uint16_t battery_mv = 0;

esp_now_peer_info_t peerInfo;
esp_adc_cal_characteristics_t adc_chars;

void triggerLed(int duration) {
  digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
  ledActive = true;
  ledTimer = millis() + duration;
}

void ledBlink(int times, int delayMs) {
  for(int i = 0; i < times; i++) {
    digitalWrite(PIN_LED, LED_ACTIVE_HIGH);
    delay(delayMs);
    digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
    delay(delayMs);
  }
}

void saveGateways() { 
  prefs.putBytes("gw", &gateways, sizeof(gateways)); 
}

void addGateway(const uint8_t *mac) {
  for(int i = 0; i < gateways.count; i++) {
    if (memcmp(gateways.macs[i], mac, 6) == 0) return;
  }
  if (gateways.count < MAX_GATEWAYS) {
    memcpy(gateways.macs[gateways.count], mac, 6);
    gateways.count++;
    saveGateways();
    Serial.println("New Gateway Saved");
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if (len < sizeof(uint8_t)) return;
  HueMixLinkPacket *rx = (HueMixLinkPacket*)data;
  const uint8_t *mac = info->src_addr;

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
      Serial.printf("[REMOTE] Server sent %d gateways:\n", rx->payload.gwList.count);
      for(int i = 0; i < rx->payload.gwList.count; i++) {
        Serial.printf("  [%d] %02X:%02X:%02X:%02X:%02X:%02X\n", i,
          rx->payload.gwList.macs[i][0], rx->payload.gwList.macs[i][1],
          rx->payload.gwList.macs[i][2], rx->payload.gwList.macs[i][3],
          rx->payload.gwList.macs[i][4], rx->payload.gwList.macs[i][5]);
      }

      // Merge new gateway list while preserving successful order
      Payload_GatewayList newList;
      newList.count = 0;

      // First, add existing gateways that are still in the new list (preserve order)
      for(int i = 0; i < gateways.count && newList.count < MAX_GATEWAYS; i++) {
        bool stillExists = false;
        for(int j = 0; j < rx->payload.gwList.count; j++) {
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
      for(int i = 0; i < rx->payload.gwList.count && newList.count < MAX_GATEWAYS; i++) {
        bool isNew = true;
        for(int j = 0; j < newList.count; j++) {
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

void getBatteryVoltage() {
  // Read calibrated battery voltage from ADC pin 35 with voltage divider
  // Divider: 1MΩ and 270kΩ, ratio = 1300/300 ≈ 4.333
  uint32_t raw = analogRead(PIN_BATTERY);
  uint32_t vout_mv = esp_adc_cal_raw_to_voltage(raw, &adc_chars);
  battery_mv = (vout_mv * 1300) / 300;
}

void sendPacket(uint8_t type, uint8_t action, uint8_t buttonIndex) {
  HueMixLinkPacket pkt;
  pkt.type = type;
  WiFi.macAddress(pkt.sourceMAC);

  if (type == PKT_BTN_EVENT) {
    pkt.payload.btn.action = action;
    pkt.payload.btn.battery_mv = battery_mv;
    pkt.payload.raw[3] = buttonIndex;
    pkt.signature = calculateHash((uint8_t*)&pkt.payload, sizeof(Payload_Button), HOME_ID);
  } else if (type == PKT_HELLO) {
    pkt.payload.raw[0] = DEV_REMOTE;
    pkt.signature = calculateHash(pkt.payload.raw, 1, 0);
  }

  ackReceived = false;
  bool sent = false;
  int successfulGatewayIndex = -1;

  // A. PAIRED MODE
  if (HOME_ID != 0 && gateways.count > 0) {
    Serial.printf("[REMOTE] Trying %d gateways for packet type 0x%02X action %d button %d\n", 
      gateways.count, type, action, buttonIndex);

    for(int i = 0; i < gateways.count; i++) {
      Serial.printf("[REMOTE] Attempt %d/%d: %02X:%02X:%02X:%02X:%02X:%02X\n", i+1, gateways.count,
        gateways.macs[i][0], gateways.macs[i][1], gateways.macs[i][2],
        gateways.macs[i][3], gateways.macs[i][4], gateways.macs[i][5]);

      memcpy(peerInfo.peer_addr, gateways.macs[i], 6);
      peerInfo.channel = 0;
      peerInfo.encrypt = false;
      if(!esp_now_is_peer_exist(gateways.macs[i])) esp_now_add_peer(&peerInfo);
      if (esp_now_send(gateways.macs[i], (uint8_t*)&pkt, sizeof(pkt)) == ESP_OK) {
        unsigned long w = millis();
        while(millis() - w < 75 && !ackReceived) delay(1);
        if (ackReceived) {
          Serial.println("[REMOTE]   ACK received!");
          sent = true;
          successfulGatewayIndex = i;
          break;
        }
      }
    }

    // Move successful gateway to front of list
    if (successfulGatewayIndex > 0) {
      Serial.printf("[REMOTE] Moving gateway %d to front\n", successfulGatewayIndex);
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
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if(!esp_now_is_peer_exist(broadcastAddress)) esp_now_add_peer(&peerInfo);
    esp_now_send(broadcastAddress, (uint8_t*)&pkt, sizeof(pkt));
    unsigned long w = millis();
    while(millis() - w < 500 && !ackReceived) delay(1);
    if(!ackReceived) ledBlink(2, 200);
  }
}

void goToSleep() {
  delay(100);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  pinMode(PIN_LED, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  Serial.println("Going to sleep with EXT1 wakeup on buttons 0-3 and reset");
  Serial.flush();

  // Enable EXT1 wakeup on all 4 buttons + reset (HIGH level)
  esp_sleep_enable_ext1_wakeup(EXT1_BUTTON_MASK, ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

int getWakeupButtonIndex() {
  uint64_t wakeup_pin_mask = esp_sleep_get_ext1_wakeup_status();
  int pin = __builtin_ctzll(wakeup_pin_mask);
  
  if (pin == PIN_RESET) {
    return -1;
  }
  
  if (pin == PIN_BTN0) return 0;
  if (pin == PIN_BTN1) return 1;
  if (pin == PIN_BTN2) return 2;
  if (pin == PIN_BTN3) return 3;
  
  return -1;
}

void setup() {
  // Initialize all button pins as inputs with pull-down (already on hardware)
  for(int i = 0; i < 4; i++) {
    pinMode(buttonPins[i], INPUT);
  }
  pinMode(PIN_RESET, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
  
  // Initialize ADC for battery reading with calibration
  analogSetAttenuation(ADC_2_5db);
  analogSetWidth(12);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_2_5, ADC_WIDTH_BIT_12, 1100, &adc_chars);
  getBatteryVoltage();

  Serial.begin(115200);
  Serial.println("\n--- REMOTE CONTROL WAKE (EXT1) ---");

  prefs.begin("huemixlink", false);
  HOME_ID = prefs.getUInt("hid", 0);
  prefs.getBytes("gw", &gateways, sizeof(gateways));

  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();

  esp_now_register_recv_cb(OnDataRecv);

  // Initialize button debouncing
  for(int i = 0; i < 4; i++) {
    buttons[i].attach(buttonPins[i], INPUT);
    buttons[i].interval(25);
  }

  lastActivityTime = millis();

  // Check wakeup reason
  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  if(wakeupReason == ESP_SLEEP_WAKEUP_EXT1) {
    wakeupExt1 = true;
    wakeupButtonIndex = getWakeupButtonIndex();
    if (wakeupButtonIndex == -1) {
      wakeupWasReset = true;
      Serial.println("Wakeup caused by RESET button");
    } else {
      Serial.printf("Wakeup caused by button %d\n", wakeupButtonIndex);
    }
  } else {
    Serial.println("Cold boot");
    ledBlink(5, 100);
    goToSleep();
  }
}

void loop() {
  // Update all buttons
  for(int i = 0; i < 4; i++) {
    buttons[i].update();
  }

  // Handle LED blinking
  if (ledActive) {
    if (millis() > ledTimer) {
      digitalWrite(PIN_LED, !LED_ACTIVE_HIGH);
      ledActive = false;
    }
  }

  // Handle reset pin from EXT1 wakeup
  if (wakeupExt1) {
    if (wakeupWasReset) {
      wakeupWasReset = false;
      lastActivityTime = millis();
    }
    else if (wakeupButtonIndex >= 0) {
      if (digitalRead(buttonPins[wakeupButtonIndex]) == LOW) {
        // Button was released during wakeup
        if (HOME_ID == 0) {
          sendPacket(PKT_HELLO, 0, wakeupButtonIndex);
          Serial.printf("Button %d press (pair request)\n", wakeupButtonIndex);
        } else {
          sendPacket(PKT_BTN_EVENT, ACT_CLICK, wakeupButtonIndex);
        }
        buttonStates[wakeupButtonIndex].pressed = false;
      } else {
        // Still holding button, continue to normal state
        buttonStates[wakeupButtonIndex].pressed = true;
        buttonStates[wakeupButtonIndex].holdStartTime = millis();
      }
    }
    wakeupExt1 = false;
    lastActivityTime = millis();
  }

  // Check for button state changes
  for(int i = 0; i < 4; i++) {
    // Button pressed
    if (buttons[i].rose()) {
      buttonStates[i].pressed = true;
      buttonStates[i].holdStartTime = millis();
      lastActivityTime = millis();
    }

    // Button is being held
    if (buttons[i].read() == HIGH && buttonStates[i].pressed && !buttonStates[i].isHolding) {
      if (millis() - buttonStates[i].holdStartTime >= HOLD_TIME) {
        buttonStates[i].isHolding = true;
        if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING, i);
        buttonStates[i].holdingIntervalUpdate = millis();
      }
    }

    // Repeat holding signal while button held
    if (millis() - buttonStates[i].holdingIntervalUpdate >= HOLD_INTERVAL && buttonStates[i].isHolding) {
      if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_HOLDING, i);
      buttonStates[i].holdingIntervalUpdate = millis();
      lastActivityTime = millis();
    }

    // Button released
    if (buttons[i].fell()) {
      if (buttonStates[i].isHolding) {
        buttonStates[i].isHolding = false;
        if (HOME_ID != 0) sendPacket(PKT_BTN_EVENT, ACT_RELEASE, i);
        Serial.printf("Button %d holding stopped\n", i);
      } else if (buttonStates[i].pressed) {
        if (HOME_ID == 0) {
          sendPacket(PKT_HELLO, 0, i);
          Serial.printf("Button %d press (pair request)\n", i);
        } else {
          sendPacket(PKT_BTN_EVENT, ACT_CLICK, i);
          Serial.printf("Button %d click\n", i);
        }
      }
      buttonStates[i].pressed = false;
      lastActivityTime = millis();
    }
  }

  // FACTORY RESET using RESET pin (5 second hold)
  if (digitalRead(PIN_RESET) == HIGH) {
    lastActivityTime = millis();
    unsigned long holdStart = millis();
    while(digitalRead(PIN_RESET) == HIGH) {
      if (millis() - holdStart > 5000) {
        Serial.println("Factory reset initiated...");
        ledBlink(10, 50);
        prefs.clear();
        HOME_ID = 0;
        gateways.count = 0;
        Serial.println("Reset complete, restarting...");
        ESP.restart();
      }
      delay(10);
    }
  }

  if (homeSetupDone) {
    prefs.putUInt("hid", HOME_ID);
    ledBlink(5, 50);
    Serial.println("Paired! Requesting Full Gateway List...");
    sendPacket(PKT_BTN_EVENT, ACT_SYNC, 0);
    lastActivityTime = millis();
    homeSetupDone = false;
  }

  // If been idle long enough, go to sleep
  if (millis() - lastActivityTime > SLEEP_TIMEOUT) {
    // Make sure all buttons are released
    bool allReleased = true;
    for(int i = 0; i < 4; i++) {
      if (buttons[i].read() == HIGH) {
        allReleased = false;
        break;
      }
    }
    // Also check reset pin is released
    if (allReleased && digitalRead(PIN_RESET) == LOW) {
      goToSleep();
    }
  }

  delay(5);
}
