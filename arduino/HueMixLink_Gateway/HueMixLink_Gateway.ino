/* 
   HUEMIXLINK V2 - GATEWAY FIRMWARE (ZERO-PAD & SELF-ADD FIX)
   - Fixed: Net Node zero-pads UDP packets to maintain Serial Sync
   - Fixed: Radio Node adds itself to gateway list on boot
*/

// ==========================================
// >>> SELECT DEVICE ROLE HERE <<<
// ==========================================

// #define ROLE_NET_NODE    
#define ROLE_RADIO_NODE 

// ==========================================

#include "HueMixLink.h"
#include <Preferences.h>
#include <nvs_flash.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_mac.h>

Preferences prefs;
uint32_t HOME_ID = 0; 
bool nightMode = false;

// ===================================================================================
// ------------------------------ ROLE 1: NET NODE -----------------------------------
// ===================================================================================
#ifdef ROLE_NET_NODE

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WiFiManager.h> 
#include <Bounce2.h> 
#include <Ticker.h> 
#include <vector>

#define PIN_BTN_MAIN  12   
#define PIN_BTN_AUX   13 
#define PIN_LED_WIFI  18
#define PIN_LED_DATA  19
#define PIN_RX        16
#define PIN_TX        17

WiFiUDP udp;
const char* ssidBase = "HueMix Link - ";
char ssidName[32]; 
char server_ip[40] = "192.168.1.1";
int server_port = 7777;
int local_port  = 4210;
WiFiManager wm; 
Ticker wifiTicker; 
HueMixLinkPacket txPkt;  
uint8_t udpBuffer[512];  
uint8_t radioNodeMAC[6] = {0,0,0,0,0,0}; 

Bounce btnMain = Bounce();
Bounce btnAux = Bounce();
bool buttonHeld = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingIntervalUpdate = 0;
const int holdingThreshold = 400; 
const int holdingInterval = 200;  

// --- SERIAL STATE MACHINE ---
enum SerialState { S_IDLE, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint8_t rxRawBuffer[sizeof(HueMixLinkPacket)];

void tickWifiLed() { digitalWrite(PIN_LED_WIFI, !digitalRead(PIN_LED_WIFI)); }
void setWifiLedState(int state) {
  wifiTicker.detach();
  if (state == 0) digitalWrite(PIN_LED_WIFI, LOW);
  else if (state == 1) digitalWrite(PIN_LED_WIFI, nightMode ? LOW : HIGH);
  else if (state == 2) wifiTicker.attach(0.5, tickWifiLed); 
  else if (state == 3) wifiTicker.attach(0.2, tickWifiLed); 
}
void flashDataLED(int times) {
  for(int i=0; i<times; i++) {
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
    digitalWrite(PIN_LED_DATA, !digitalRead(PIN_LED_DATA)); delay(50);
  }
}

// --- HANDLE SERIAL PACKET ---
void handleSerialPacket(uint8_t* data) {
  memcpy(&txPkt, data, sizeof(HueMixLinkPacket));
  
  // Debug
  Serial.printf("[NET] RX Serial Type 0x%02X\n", txPkt.type);

  if (WiFi.status() == WL_CONNECTED) {
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket));
    udp.endPacket();
    flashDataLED(1);
  } else {
    Serial.println("[NET] WiFi down, cannot forward");
  }
}

// --- PARSER ---
void parseSerialByte(uint8_t b) {
  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) {
        rxState = S_READING;
        rxIndex = 0;
      }
      break;
    case S_READING:
      rxRawBuffer[rxIndex++] = b;
      if (rxIndex >= sizeof(HueMixLinkPacket)) rxState = S_FOOTER;
      break;
    case S_FOOTER:
      if (b == SERIAL_END) handleSerialPacket(rxRawBuffer);
      else {
        Serial.printf("[NET] Footer Err %02X. Resyncing...\n", b);
        while(Serial2.available()) Serial2.read(); // Flush garbage
      }
      rxState = S_IDLE;
      break;
  }
}

void sendGatewayHello() {
  if (WiFi.status() != WL_CONNECTED) return;
  // Serial.println("Sending Hello...");
  HueMixLinkPacket pkt;
  pkt.type = PKT_HELLO;
  WiFi.macAddress(pkt.sourceMAC);
  pkt.payload.raw[0] = 1; 
  memcpy(&pkt.payload.raw[1], radioNodeMAC, 6);
  pkt.signature = calculateHash(pkt.payload.raw, 7, HOME_ID);
  udp.beginPacket(server_ip, server_port);
  udp.write((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  udp.endPacket();
  flashDataLED(1);
}

void performFactoryReset() {
  Serial.println("FACTORY RESET TRIGGERED!");
  wifiTicker.detach();
  for(int i=0; i<5; i++) { 
    digitalWrite(PIN_LED_WIFI, HIGH); digitalWrite(PIN_LED_DATA, HIGH); delay(100); 
    digitalWrite(PIN_LED_WIFI, LOW); digitalWrite(PIN_LED_DATA, LOW); delay(100);
  }
  wm.resetSettings(); nvs_flash_erase(); nvs_flash_init(); prefs.clear();
  Serial.println("Rebooting..."); ESP.restart();
}

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); delay(500);
  pinMode(PIN_LED_WIFI, OUTPUT); pinMode(PIN_LED_DATA, OUTPUT);
  setWifiLedState(2); 
  btnMain.attach(PIN_BTN_MAIN, INPUT_PULLUP); btnMain.interval(25);
  btnAux.attach(PIN_BTN_AUX, INPUT_PULLUP); btnAux.interval(25);
  Serial.begin(115200); 
  
  Serial2.setRxBufferSize(4096); 
  Serial2.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX); 
  
  Serial.println("\n--- BOOTING NET NODE ---");

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);
  
  Serial.print("Handshake... ");
  unsigned long startWait = millis();
  unsigned long lastReq = 0;
  bool radioReady = false;
  
  while(Serial2.available()) Serial2.read();

  while(millis() - startWait < 5000) { 
    if (millis() - lastReq > 200) { Serial2.write(SERIAL_REQ_HANDSHAKE); lastReq = millis(); }
    if(Serial2.available() >= sizeof(SerialHandshake)) {
      if(Serial2.peek() == SERIAL_HANDSHAKE) {
        SerialHandshake h; Serial2.readBytes((uint8_t*)&h, sizeof(h));
        bool v = false; for(int k=0;k<6;k++) if(h.mac[k]!=0) v=true;
        if(v) { memcpy(radioNodeMAC, h.mac, 6); radioReady = true; break; }
      } else { Serial2.read(); }
    }
    delay(10);
  }
  if(radioReady) { 
    Serial.print("OK! Radio MAC: ");
    for(int i=0; i<6; i++) Serial.printf("%02X", radioNodeMAC[i]);
    Serial.println();
    flashDataLED(2); 
  } else { Serial.println("FAILED (No Radio)"); }

  WiFiManagerParameter custom_ip("server", "Server IP", "", 40);
  char portStr[6]; itoa(server_port, portStr, 10);
  WiFiManagerParameter custom_port("port", "Server Port", portStr, 6);
  wm.addParameter(&custom_ip); wm.addParameter(&custom_port);
  std::vector<const char *> wm_menu = {"wifi", "exit"}; wm.setMenu(wm_menu);
  uint32_t chipId = (uint32_t)ESP.getEfuseMac();
  sprintf(ssidName, "%s%08X", ssidBase, chipId);
  wm.setAPCallback([](WiFiManager *myWiFiManager) { setWifiLedState(3); Serial.println("Entered Config Mode"); });

  bool hasSavedConfig = (prefs.getString("srv_ip", "").length() > 0);
  if (!hasSavedConfig) {
    Serial.println("No config -> Opening Portal");
    wm.setConnectTimeout(180);
    if (!wm.autoConnect(ssidName, "HueMixLink")) { ESP.restart(); }
  } else {
    Serial.println("Connecting...");
    wm.setEnableConfigPortal(false); wm.setConnectTimeout(20);
    if (!wm.autoConnect(ssidName, "HueMixLink")) {
      Serial.println("Connection Failed! Retrying...");
      setWifiLedState(2); 
      unsigned long la = millis();
      while (WiFi.status() != WL_CONNECTED) {
        btnAux.update();
        if (btnAux.read() == LOW && btnAux.currentDuration() > 5000) performFactoryReset();
        if (millis() - la > 10000) { Serial.print("."); WiFi.disconnect(); WiFi.reconnect(); la = millis(); }
        delay(10);
      }
    }
  }

  String sip = custom_ip.getValue();
  if (sip.length() > 0) {
    strcpy(server_ip, custom_ip.getValue()); server_port = atoi(custom_port.getValue());
    prefs.putString("srv_ip", server_ip); prefs.putInt("srv_port", server_port);
  } else {
    String s = prefs.getString("srv_ip"); s.toCharArray(server_ip, 40); server_port = prefs.getInt("srv_port");
  }

  udp.begin(local_port);
  setWifiLedState(1);
  Serial.println("\n--- OPERATIONAL ---");
  Serial.printf("Server: %s:%d\n", server_ip, server_port);
  sendGatewayHello(); 
}

void sendBtnEvent(uint8_t action) {
  HueMixLinkPacket btnPkt;
  btnPkt.type = PKT_BTN_EVENT;
  WiFi.macAddress(btnPkt.sourceMAC);
  btnPkt.payload.btn.battery_mv = 0; 
  btnPkt.payload.btn.action = action;
  btnPkt.signature = calculateHash((uint8_t*)&btnPkt.payload, sizeof(Payload_Button), HOME_ID);
  if (WiFi.status() == WL_CONNECTED) {
    udp.beginPacket(server_ip, server_port);
    udp.write((uint8_t*)&btnPkt, sizeof(HueMixLinkPacket));
    udp.endPacket();
    flashDataLED(1);
  }
}

void loop() {
  btnMain.update(); btnAux.update();
  if (btnAux.read() == LOW && btnAux.currentDuration() > 5000) performFactoryReset();
  
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastReconnect = 0;
    digitalWrite(PIN_LED_WIFI, LOW); wifiTicker.detach(); 
    if (millis() - lastReconnect > 10000) { Serial.println("WiFi lost..."); WiFi.disconnect(); WiFi.reconnect(); lastReconnect = millis(); }
  } else { setWifiLedState(1); }

  // 1. UDP IN
  if (WiFi.status() == WL_CONNECTED) {
    int pSize = udp.parsePacket();
    if (pSize) {
      // Read partial packet from UDP
      int len = udp.read(udpBuffer, 512);
      if (len > 0) {
        
        // --- CRITICAL FIX: PAD PACKET TO FULL SIZE ---
        memset(&txPkt, 0, sizeof(HueMixLinkPacket));
        memcpy(&txPkt, udpBuffer, len);
        
        Serial.printf("[NET] UDP Recv Type 0x%02X (Len %d)\n", txPkt.type, len);

        if (txPkt.type == PKT_PAIR_CONFIRM) {
           uint32_t incomingSig = txPkt.signature;
           uint32_t expectedSig = calculateHash((uint8_t*)&txPkt.payload, sizeof(Payload_Pairing), 0);
           if (incomingSig == expectedSig) {
             uint32_t newID = txPkt.payload.pair.newHomeID;
             if (newID != HOME_ID && newID != 0) { 
               Serial.printf("!!! UPDATING HOME ID: 0x%X !!!\n", newID);
               HOME_ID = newID; prefs.putUInt("hid", HOME_ID);
               digitalWrite(PIN_LED_DATA, HIGH); delay(1000); digitalWrite(PIN_LED_DATA, LOW);
               sendGatewayHello();
             }
           }
        } 
        
        // --- SEND FULL STRUCTURE TO RADIO ---
        Serial2.write(SERIAL_START); 
        Serial2.write((uint8_t*)&txPkt, sizeof(HueMixLinkPacket)); 
        Serial2.write(SERIAL_END);
        flashDataLED(1);
      }
    }
  }

  // 2. SERIAL IN
  while (Serial2.available() > 0) parseSerialByte(Serial2.read());

  if (btnMain.fell()) { buttonHoldStartTime = millis(); }
  if (btnMain.read() == LOW && !buttonHeld) { if (millis() - buttonHoldStartTime >= holdingThreshold) buttonHeld = true; }
  if (buttonHeld && btnMain.read() == LOW) { if (millis() - holdingIntervalUpdate >= holdingInterval) { sendBtnEvent(ACT_HOLDING); holdingIntervalUpdate = millis(); }}
  if (btnMain.rose()) { if (buttonHeld) { buttonHeld = false; sendBtnEvent(ACT_RELEASE); } else { sendBtnEvent(ACT_CLICK); }}
}
#endif

// ===================================================================================
// ------------------------------ ROLE 2: RADIO NODE ---------------------------------
// ===================================================================================
#ifdef ROLE_RADIO_NODE
#include <esp_now.h>
#include <WiFi.h>

#define PIN_LED_STATUS 19
#define PIN_RX         16
#define PIN_TX         17

HueMixLinkPacket radioRx;
HueMixLinkPacket radioTx;
Payload_GatewayList activeGateways;

uint8_t lastMsgID = 0;
bool waitingForDelivery = false;

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

// --- HANDLE PACKET FROM NET NODE ---
void handleSerialPacket(uint8_t* data) {
  memcpy(&radioTx, data, sizeof(HueMixLinkPacket));
  
  Serial.printf("[RADIO] Serial RX Type 0x%02X\n", radioTx.type);

  if (radioTx.type == PKT_SYS_CMD) {
    if (radioTx.payload.sys.cmd == 1) nightMode = true;
    if (radioTx.payload.sys.cmd == 2) nightMode = false;
  } else if (radioTx.type == PKT_GW_LIST_UPD) {
    activeGateways = radioTx.payload.gwList;
    Serial.printf("[RADIO] Updated Gateways List (%d nodes)\n", activeGateways.count);
  } else if (radioTx.type == PKT_PAIR_CONFIRM) {
    HOME_ID = radioTx.payload.pair.newHomeID;
    prefs.putUInt("hid", HOME_ID);

    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID; waitingForDelivery = true;
    esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
  } else {
    esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
    if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
    lastMsgID = radioTx.msgID; waitingForDelivery = true;
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
    memcpy(rpt.payload.report.targetMAC, mac_addr, 6);

    rpt.signature = calculateHash((uint8_t*)&rpt.payload, 8, HOME_ID);

    Serial2.write(SERIAL_START); Serial2.write((uint8_t*)&rpt, sizeof(rpt)); Serial2.write(SERIAL_END);
    waitingForDelivery = false;
    digitalWrite(PIN_LED_STATUS, (status == ESP_NOW_SEND_SUCCESS) ? HIGH : LOW);
    delay(10); digitalWrite(PIN_LED_STATUS, LOW);
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  if (len != sizeof(HueMixLinkPacket)) return;
  if (pktReady) return; 

  memcpy(&radioRx, data, sizeof(HueMixLinkPacket));
  memcpy(radioRx.sourceMAC, info->src_addr, 6);
  if (radioRx.type == PKT_HELLO) { radioRx.payload.raw[1] = (uint8_t)info->rx_ctrl->rssi; }
  
  memcpy(&bufferPkt, &radioRx, sizeof(HueMixLinkPacket));
  pktReady = true;
  digitalWrite(PIN_LED_STATUS, HIGH); 
}

void setup() {
  pinMode(PIN_LED_STATUS, OUTPUT);
  Serial.begin(115200); 
  Serial2.setRxBufferSize(4096);
  Serial2.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX); 
  WiFi.mode(WIFI_STA); 
  if (esp_now_init() != ESP_OK) ESP.restart();

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);

  for(int i=0; i<5; i++) { sendSerialHandshake(); delay(100); }
  esp_now_register_send_cb((esp_now_send_cb_t)OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);
  
  activeGateways.count = 1;
  esp_read_mac(activeGateways.macs[0], ESP_MAC_WIFI_STA);
  
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

  while (Serial2.available() > 0) {
    parseSerialByte(Serial2.read());
  }
}
#endif