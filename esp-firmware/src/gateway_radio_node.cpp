/* 
  HUEMIXLINK V3 - GATEWAY RADIO FIRMWARE
  Supports: ESP32
*/

#include "HueMixLink.h"
#include <Preferences.h>
#include <nvs_flash.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_mac.h>
#include <esp_now.h>
#include <WiFi.h>

Preferences prefs;
uint32_t HOME_ID = 0; 
bool nightMode = false;

#define PIN_LED_STATUS 19
#define PIN_RX         16
#define PIN_TX         17

HueMixLinkPacket radioRx;
HueMixLinkPacket radioTx;
Payload_GatewayList activeGateways;

uint8_t lastMsgID = 0;
bool waitingForDelivery = false;
uint8_t lastTargetMAC[6] = {0}; // Store the actual target MAC for delivery reports

volatile bool pktReady = false;
HueMixLinkPacket bufferPkt;

// --- RADIO SERIAL PARSER ---
enum SerialState { S_IDLE, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint8_t rxRawBuffer[sizeof(HueMixLinkPacket)];

void sendSerialHandshake() {
  SerialHandshake h; h.magic = SERIAL_HANDSHAKE;
  esp_read_mac(h.mac, ESP_MAC_WIFI_STA);
  Serial2.write((uint8_t*)&h, sizeof(h));
}

void saveGateways() {
  prefs.putBytes("gw", &activeGateways, sizeof(activeGateways));
  Serial.printf("[RADIO] Saved %d gateways to NVS\n", activeGateways.count);
}

void loadGateways() {
  size_t len = prefs.getBytes("gw", &activeGateways, sizeof(activeGateways));
  if (len == sizeof(activeGateways) && activeGateways.count > 0) {
    Serial.printf("[RADIO] Loaded %d gateways from NVS\n", activeGateways.count);
  } else {
    // No saved gateways, initialize with self
    activeGateways.count = 1;
    esp_read_mac(activeGateways.macs[0], ESP_MAC_WIFI_STA);
    Serial.println("[RADIO] No saved gateways, initialized with self");
  }
}

// --- HANDLE PACKET FROM NET NODE ---
void handleSerialPacket(uint8_t* data) {
  memcpy(&radioTx, data, sizeof(HueMixLinkPacket));
  
  Serial.printf("[RADIO] Serial RX Type 0x%02X\n", radioTx.type);

  if (radioTx.type == PKT_SYS_CMD) {
    if (radioTx.payload.sys.cmd == 1) nightMode = true;
    else if (radioTx.payload.sys.cmd == 2) nightMode = false;
    else {
      esp_now_peer_info_t peer = {};
      memcpy(peer.peer_addr, radioTx.targetMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL;
      peer.encrypt = false;
      if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
      lastMsgID = radioTx.msgID;
      memcpy(lastTargetMAC, radioTx.targetMAC, 6);
      waitingForDelivery = true;
      esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
    }
  } else if (radioTx.type == PKT_GW_LIST_UPD) {
    activeGateways = radioTx.payload.gwList;
    saveGateways();
    Serial.printf("[RADIO] Updated Gateways List (%d nodes)\n", activeGateways.count);
    
    // Check if this update is targeted to a specific device (not broadcast)
    bool isBroadcast = true;
    for(int i=0; i<6; i++) {
      if (radioTx.targetMAC[i] != 0xFF) {
        isBroadcast = false;
        break;
      }
    }
    
    // If targeted, forward to the device via ESP-NOW
    if (!isBroadcast) {      
      esp_now_peer_info_t peer = {};
      memcpy(peer.peer_addr, radioTx.targetMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL;
      peer.encrypt = false;
      if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
      lastMsgID = radioTx.msgID;
      memcpy(lastTargetMAC, radioTx.targetMAC, 6);
      waitingForDelivery = true;
      esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
    }
  } else if (radioTx.type == PKT_PAIR_CONFIRM) {
    HOME_ID = radioTx.payload.pair.newHomeID;
    prefs.putUInt("hid", HOME_ID);

    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID;
    memcpy(lastTargetMAC, radioTx.targetMAC, 6);
    waitingForDelivery = true;
    esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
  } else {
    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID;
    memcpy(lastTargetMAC, radioTx.targetMAC, 6);
    waitingForDelivery = true;
    esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
  }
}

void parseSerialByte(uint8_t b) {
  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) { rxState = S_READING; rxIndex = 0; } 
      else if (b == SERIAL_REQ_HANDSHAKE) { sendSerialHandshake(); }
      break;
    case S_READING:
      rxRawBuffer[rxIndex++] = b;
      if (rxIndex >= sizeof(HueMixLinkPacket)) rxState = S_FOOTER;
      break;
    case S_FOOTER:
      if (b == SERIAL_END) handleSerialPacket(rxRawBuffer);
      else { 
        Serial.printf("[RADIO] Footer Err %02X\n", b); 
        while(Serial2.available()) Serial2.read();
      }
      rxState = S_IDLE;
      break;
  }
}

void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  if (waitingForDelivery) {
    HueMixLinkPacket rpt; 

    memset(&rpt, 0, sizeof(HueMixLinkPacket));

    rpt.type = PKT_DELIVERY_RPT;
    WiFi.macAddress(rpt.sourceMAC);

    rpt.payload.report.originalMsgID = lastMsgID;
    rpt.payload.report.success = (status == ESP_NOW_SEND_SUCCESS);
    memcpy(rpt.payload.report.targetMAC, lastTargetMAC, 6); // Use stored target MAC instead of callback parameter

    rpt.signature = calculateHash((uint8_t*)&rpt.payload, 8, HOME_ID);

    Serial2.write(SERIAL_START); Serial2.write((uint8_t*)&rpt, sizeof(rpt)); Serial2.write(SERIAL_END);
    waitingForDelivery = false;
    
    if (!nightMode) {
      digitalWrite(PIN_LED_STATUS, (status == ESP_NOW_SEND_SUCCESS) ? HIGH : LOW);
      delay(10); digitalWrite(PIN_LED_STATUS, LOW);
    }
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if (len != sizeof(HueMixLinkPacket)) return;
  if (pktReady) return; 

  memcpy(&radioRx, data, sizeof(HueMixLinkPacket));
  memcpy(radioRx.sourceMAC, info->src_addr, 6);
  
  // Record RSSI for HELLO and PING_DEVICE responses
  if (radioRx.type == PKT_HELLO) { 
    radioRx.payload.raw[1] = (uint8_t)info->rx_ctrl->rssi; 
  }
  else if (radioRx.type == PKT_PING_DEVICE) {
    // Store RSSI in first byte of payload for Python to read
    radioRx.payload.raw[0] = (uint8_t)info->rx_ctrl->rssi;
  }
  
  memcpy(&bufferPkt, &radioRx, sizeof(HueMixLinkPacket));
  pktReady = true;
  
  if (!nightMode) {
    digitalWrite(PIN_LED_STATUS, HIGH);
  }
}

void setup() {
  pinMode(PIN_LED_STATUS, OUTPUT);
  Serial.begin(115200); 
  Serial2.setRxBufferSize(4096);
  Serial2.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX); 
  WiFi.mode(WIFI_STA); 
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);

  for(int i=0; i<5; i++) { sendSerialHandshake(); delay(100); }
  esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);
  
  // Load saved gateways from NVS, or initialize with self
  loadGateways();
  
  // Ensure self is in the list
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  bool selfInList = false;
  for(int i=0; i<activeGateways.count; i++) {
    if(memcmp(activeGateways.macs[i], myMac, 6) == 0) {
      selfInList = true;
      break;
    }
  }
  if(!selfInList && activeGateways.count < MAX_GATEWAYS) {
    memcpy(activeGateways.macs[activeGateways.count], myMac, 6);
    activeGateways.count++;
    saveGateways();
    Serial.println("[RADIO] Added self to gateway list");
  }
  
  Serial.println("--- RADIO NODE READY ---");
}

void loop() {
  if (pktReady) {
    Serial2.write(SERIAL_START);
    Serial2.write((uint8_t*)&bufferPkt, sizeof(bufferPkt));
    Serial2.write(SERIAL_END);
    
    if (bufferPkt.type == PKT_BTN_EVENT) {
      HueMixLinkPacket ack; ack.type = PKT_ACK_TO_BTN;
      ack.payload.gwList = activeGateways; 
      esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, bufferPkt.sourceMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
      if(!esp_now_is_peer_exist(bufferPkt.sourceMAC)) esp_now_add_peer(&peer);
      esp_now_send(bufferPkt.sourceMAC, (uint8_t*)&ack, sizeof(ack));
    }
    digitalWrite(PIN_LED_STATUS, LOW);
    pktReady = false;
  }

  if (!waitingForDelivery) {
    while (Serial2.available() > 0) {
      parseSerialByte(Serial2.read());
      if (waitingForDelivery) break;
    }
  }
}
