/* 
  HUEMIXLINK V3 - GATEWAY RADIO FIRMWARE
  Supports: ESP32
*/

#include "HueMixLink.h"
#include "sec.h"
#include "sec_pairing.h"
#include "sec_device.h"
#include <Preferences.h>
#include <nvs_flash.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <esp_mac.h>
#include <esp_now.h>
#include <WiFi.h>
#include <Ticker.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include "mbedtls/sha256.h"

#ifndef IS_SERIAL_GATEWAY
  #define IS_SERIAL_GATEWAY 0
#endif

#if IS_SERIAL_GATEWAY
  #include <Wire.h>
  #include <Adafruit_GFX.h>
  #include <Adafruit_SSD1306.h>
#endif

Preferences prefs;
uint32_t HOME_ID = 0; 
bool nightMode = false;
bool netNodeHasWiFi = false;  // Tracks if net node has WiFi connectivity

#define PIN_LED_STATUS 19
#define PIN_RX         16
#define PIN_TX         17

#if IS_SERIAL_GATEWAY
  #define SERIAL_COM Serial
  #define SERIAL_DEBUG Serial2
#else
  #define SERIAL_COM Serial2
  #define SERIAL_DEBUG Serial
#endif

// --- STATUS LED STATE MACHINE ---
enum StatusLEDState { STATUS_LED_IDLE, STATUS_LED_ON, STATUS_LED_BRIEF_OFF };
StatusLEDState statusLEDState = STATUS_LED_IDLE;
unsigned long statusLEDTimer = 0;

// --- DATA LED BREATHING (for OTA) ---
#define DATA_LED_PWM_FREQ 5000
#define DATA_LED_PWM_RESOLUTION 8
Ticker breathingTicker;
int breathingDirection = 1;
int breathingBrightness = 0;

#define MAX_FRAME_BUFFER 1500
#define RADIO_RX_QUEUE_SIZE 4
HueMixLinkPacket radioRx;
HueMixLinkPacket radioTx;
struct RadioRxSlot {
  uint8_t buf[MAX_FRAME_BUFFER];
  uint16_t len;
};
RadioRxSlot radioRxQueue[RADIO_RX_QUEUE_SIZE];
volatile uint8_t radioRxHead = 0;
volatile uint8_t radioRxTail = 0;
Payload_GatewayList activeGateways;

SecPairingState gwSecState;
SecGwPeerTable  gwPeerTable;
SecCounter      gwCounter;
unsigned long   lastSecHelloMs = 0;

uint8_t lastMsgID = 0;
volatile bool waitingForDelivery = false;
volatile bool pendingDeliveryRpt = false;
Payload_Report pendingDeliveryPayload;
uint8_t lastTargetMAC[6] = {0}; // Store the actual target MAC for delivery reports

unsigned long lastOtaTrafficMs = 0;

// --- OTA STATE MACHINE ---
enum OtaState { OTA_IDLE, OTA_RECEIVING, OTA_VALIDATING, OTA_COMPLETE };
OtaState otaState = OTA_IDLE;
const esp_partition_t *update_partition = nullptr;
esp_ota_handle_t update_handle = 0;
uint32_t expected_firmware_size = 0;
uint32_t received_bytes = 0;
uint16_t expected_chunk_index = 0;
uint8_t expected_sha256[32];
mbedtls_sha256_context sha256_ctx;
unsigned long last_ota_activity = 0;

// --- STARTUP HANDSHAKE REQUEST TIMEOUT ---
unsigned long startup_time = 0;
bool handshake_request_received = false;
unsigned long last_serial_activity = 0;
#define STARTUP_HANDSHAKE_TIMEOUT 3000  // 3 seconds
#define SERIAL_IDLE_THRESHOLD 50  // Must be idle for 50ms before checking for handshake request

#if IS_SERIAL_GATEWAY
  #define OLED_WIDTH 128
  #define OLED_HEIGHT 64
  #define OLED_I2C_ADDR 0x3C
  #define OLED_RESET -1
  #define OLED_PIN_SDA 21
  #define OLED_PIN_SCL 22

  Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);

  struct DashboardStats {
    char cpu[8];
    char ram[8];
    char ip[24];
    char uptime[16];
    char temp[8];
    bool valid;
    unsigned long lastUpdateMs;
  };

  DashboardStats dashboardStats = {{0}, {0}, {0}, {0}, {0}, false, 0};
  char dashboardLineBuffer[96] = {0};
  uint8_t dashboardLineIndex = 0;
  bool dashboardDisplayReady = false;
  unsigned long lastDashboardRender = 0;
  unsigned long dashboardActivityFlashUntilMs = 0;
  bool dashboardScreenOn = true;
  #define DASHBOARD_STALE_RESET_MS 10000
  #define DASHBOARD_ACTIVITY_FLASH_MS 350
#endif

// --- RADIO SERIAL PARSER (length-prefixed: 0xFE | len_lo | len_hi | payload | 0xFD) ---
enum SerialState { S_IDLE, S_LEN_LO, S_LEN_HI, S_READING, S_FOOTER };
SerialState rxState = S_IDLE;
uint16_t rxIndex = 0;
uint16_t rxExpectedLen = 0;
uint8_t rxRawBuffer[MAX_FRAME_BUFFER];

void sendSerialFrame(const uint8_t* data, uint16_t len) {
  if (len < V2_OVERHEAD || len > V2_MAX_FRAME_ESP32) return;
  uint8_t hdr[3] = { SERIAL_START, (uint8_t)(len & 0xFF), (uint8_t)((len >> 8) & 0xFF) };
  SERIAL_COM.write(hdr, 3);
  SERIAL_COM.write(data, len);
  SERIAL_COM.write(SERIAL_END);
  SERIAL_COM.flush();
}

void sendRadioSecHello() {
  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_HELLO;
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  memcpy(pkt.sourceMAC, myMac, 6);
  pkt.payload.raw[0] = DEV_GATEWAY;
  memcpy(pkt.payload.raw + 1, myMac, 6);
  #ifdef FIRMWARE_VERSION
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(FIRMWARE_VERSION, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    pkt.payload.raw[11] = major;
    pkt.payload.raw[12] = minor;
    pkt.payload.raw[13] = patch;
  #endif
  if (gwSecState.crypto_mode == SEC_MODE_V1) {
    sec_pairing_fill_hello_tlv(&gwSecState, pkt.payload.raw, 15, V2_MAX_FRAME_ESP32);
  } else {
    pkt.payload.raw[15] = PROTO_VERSION_V2;
    pkt.payload.raw[16] = (uint8_t)(V2_MAX_FRAME_ESP32 & 0xFF);
    pkt.payload.raw[17] = (uint8_t)((V2_MAX_FRAME_ESP32 >> 8) & 0xFF);
  }
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  sendSerialFrame((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  SERIAL_DEBUG.println("[RADIO] Sent Security v2 gateway HELLO");
}

static bool shouldAckInboundPacket(uint8_t packetType) {
  return packetType == PKT_BTN_EVENT ||
         packetType == PKT_MOTION_EVENT ||
         packetType == PKT_DOOR_EVENT ||
         (packetType >= PLUGIN_EVENT_PACKET_MIN && packetType <= PLUGIN_EVENT_PACKET_MAX);
}

// --- OTA BUFFERING CONFIG ---
// Sized to hold a full 10-chunk v2 batch (10 * 1024 B = 10,240 B) in RAM so
// esp_ota_write() only runs at checkpoints when UART is idle (no mid-batch flash stalls).
#define OTA_ACCUMULATOR_SIZE 11264
uint8_t otaAccumulator[OTA_ACCUMULATOR_SIZE];
uint16_t otaAccumulatorIndex = 0;

#if IS_SERIAL_GATEWAY
void drawCenteredText(const char* text, int y, uint8_t textSize = 1) {
  int16_t x1, y1;
  uint16_t w, h;
  oled.setTextSize(textSize);
  oled.getTextBounds(text, 0, y, &x1, &y1, &w, &h);
  int x = (OLED_WIDTH - (int)w) / 2;
  if (x < 0) x = 0;
  oled.setCursor(x, y);
  oled.print(text);
}

void drawWifiIcon(int x, int y) {
  // First signal arc (outer, largest)
  oled.drawPixel(x+0, y+3, SSD1306_WHITE);
  oled.drawPixel(x+1, y+2, SSD1306_WHITE);
  oled.drawPixel(x+2, y+1, SSD1306_WHITE);
  oled.drawFastHLine(x+3, y, 8, SSD1306_WHITE);
  oled.drawPixel(x+11, y+1, SSD1306_WHITE);
  oled.drawPixel(x+12, y+2, SSD1306_WHITE);
  oled.drawPixel(x+13, y+3, SSD1306_WHITE);

  // Second signal arc (middle)
  oled.drawPixel(x+2, y+5, SSD1306_WHITE);
  oled.drawPixel(x+3, y+4, SSD1306_WHITE);
  oled.drawFastHLine(x+4, y+3, 6, SSD1306_WHITE);
  oled.drawPixel(x+10, y+4, SSD1306_WHITE);
  oled.drawPixel(x+11, y+5, SSD1306_WHITE);

  // Third signal arc (inner, smallest)
  oled.drawPixel(x+4, y+7, SSD1306_WHITE);
  oled.drawPixel(x+5, y+6, SSD1306_WHITE);
  oled.drawFastHLine(x+6, y+6, 2, SSD1306_WHITE);
  oled.drawPixel(x+8, y+6, SSD1306_WHITE);
  oled.drawPixel(x+9, y+7, SSD1306_WHITE);

  // WiFi dot (square)
  oled.drawPixel(x+6, y+9, SSD1306_WHITE);
  oled.drawPixel(x+7, y+9, SSD1306_WHITE);
  oled.drawPixel(x+6, y+10, SSD1306_WHITE);
  oled.drawPixel(x+7, y+10, SSD1306_WHITE);
}

void drawActivityIndicator() {
  if (millis() < dashboardActivityFlashUntilMs) {
    oled.fillCircle(OLED_WIDTH - 5, OLED_HEIGHT - 5, 2, SSD1306_WHITE);
  }
}

void renderInitializingScreen() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);

  drawCenteredText("HueMix-Link", 19, 1);

  int16_t x1, y1;
  uint16_t wBase, hBase, wFull, hFull;
  oled.getTextBounds("Initializing", 0, 0, &x1, &y1, &wBase, &hBase);
  oled.getTextBounds("Initializing...", 0, 0, &x1, &y1, &wFull, &hFull);

  int startX = (OLED_WIDTH - (int)wFull) / 2;
  if (startX < 0) startX = 0;

  oled.setCursor(startX, 36);
  oled.print("Initializing");

  uint8_t dots = (millis() / 300) % 4;
  const char* dotsText = "";
  if (dots == 1) dotsText = ".";
  else if (dots == 2) dotsText = "..";
  else if (dots == 3) dotsText = "...";

  oled.setCursor(startX + (int)wBase, 36);
  oled.print(dotsText);

  drawActivityIndicator();

  oled.display();
}

void drawCpuIcon(int x, int y) {
  oled.drawRect(x + 2, y + 2, 12, 12, SSD1306_WHITE);
  for (int i = 0; i < 4; i++) {
    oled.drawFastVLine(x + 4 + i * 2, y, 2, SSD1306_WHITE);
    oled.drawFastVLine(x + 4 + i * 2, y + 14, 2, SSD1306_WHITE);
    oled.drawFastHLine(x, y + 4 + i * 2, 2, SSD1306_WHITE);
    oled.drawFastHLine(x + 14, y + 4 + i * 2, 2, SSD1306_WHITE);
  }
  oled.drawRect(x + 6, y + 6, 4, 4, SSD1306_WHITE);
}

void drawRamIcon(int x, int y) {
  oled.drawRect(x + 1, y + 3, 14, 10, SSD1306_WHITE);
  oled.drawFastHLine(x + 2, y + 6, 12, SSD1306_WHITE);
  oled.drawFastHLine(x + 2, y + 9, 12, SSD1306_WHITE);
  for (int i = 0; i < 4; i++) {
    oled.drawPixel(x + 3 + i * 3, y + 2, SSD1306_WHITE);
    oled.drawPixel(x + 3 + i * 3, y + 13, SSD1306_WHITE);
  }
}

void drawTempIcon(int x, int y) {
  oled.drawRoundRect(x + 6, y + 2, 4, 10, 2, SSD1306_WHITE);
  oled.fillCircle(x + 8, y + 13, 4, SSD1306_WHITE);
  oled.drawFastVLine(x + 7, y + 4, 6, SSD1306_BLACK);
}

void drawClockIcon(int x, int y) {
  oled.drawCircle(x + 8, y + 8, 7, SSD1306_WHITE);
  oled.drawLine(x + 8, y + 8, x + 8, y + 4, SSD1306_WHITE);
  oled.drawLine(x + 8, y + 8, x + 11, y + 10, SSD1306_WHITE);
}

void drawStatCell(int x, int y, const char* value, void (*iconFn)(int, int)) {
  iconFn(x, y + 5);

  uint8_t valueLen = strlen(value);
  uint8_t valueSize = (valueLen <= 4) ? 2 : 1;
  oled.setTextSize(valueSize);

  int16_t x1, y1;
  uint16_t w, h;
  oled.getTextBounds(value, 0, 0, &x1, &y1, &w, &h);

  int textX = x + 19;
  int textY = y + ((24 - (int)h) / 2) + 2;
  if (textY < y) textY = y;

  oled.setCursor(textX, textY);
  oled.print(value);
}

void renderDashboard() {
  if (!dashboardDisplayReady) return;

  unsigned long ageMs = dashboardStats.valid ? (millis() - dashboardStats.lastUpdateMs) : 0;
  bool showInit = (!dashboardStats.valid) || (ageMs > DASHBOARD_STALE_RESET_MS);
  if (showInit) {
    if (dashboardStats.valid && netNodeHasWiFi) {
      netNodeHasWiFi = false;
      SERIAL_DEBUG.println("[RADIO] Dashboard data stale, assuming net node WiFi is OFF");
    }
    renderInitializingScreen();
    return;
  }

  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);

  uint8_t ipLen = strlen(dashboardStats.ip);
  uint16_t ipWidth = ipLen * 6;
  uint8_t wifiWidth = 16;
  uint8_t gap = 4;
  int blockWidth = netNodeHasWiFi ? (wifiWidth + gap + ipWidth) : ipWidth;
  int startX = (OLED_WIDTH - blockWidth) / 2;
  if (startX < 0) startX = 0;

  oled.setTextSize(1);
  if (netNodeHasWiFi) {
    drawWifiIcon(startX, 0);
    oled.setCursor(startX + wifiWidth + gap, 1);
  } else {
    oled.setCursor(startX, 1);
  }
  oled.print(dashboardStats.ip);

  oled.drawFastHLine(0, 13, OLED_WIDTH, SSD1306_WHITE);

  char cpuDisplay[10] = {0};
  char ramDisplay[10] = {0};
  char tempDisplay[10] = {0};

  if (dashboardStats.valid && strcmp(dashboardStats.cpu, "--") != 0) {
    snprintf(cpuDisplay, sizeof(cpuDisplay), "%s%%", dashboardStats.cpu);
  } else {
    strncpy(cpuDisplay, "--", sizeof(cpuDisplay) - 1);
  }

  if (dashboardStats.valid && strcmp(dashboardStats.ram, "--") != 0) {
    snprintf(ramDisplay, sizeof(ramDisplay), "%s%%", dashboardStats.ram);
  } else {
    strncpy(ramDisplay, "--", sizeof(ramDisplay) - 1);
  }

  if (dashboardStats.valid && strcmp(dashboardStats.temp, "--") != 0) {
    snprintf(tempDisplay, sizeof(tempDisplay), "%sC", dashboardStats.temp);
  } else {
    strncpy(tempDisplay, "--", sizeof(tempDisplay) - 1);
  }

  const char* cpuVal = cpuDisplay;
  const char* ramVal = ramDisplay;
  const char* tempVal = tempDisplay;
  const char* upVal = dashboardStats.valid ? dashboardStats.uptime : "--";

  drawStatCell(0, 14, cpuVal, drawCpuIcon);
  drawStatCell(64, 14, ramVal, drawRamIcon);
  drawStatCell(0, 38, tempVal, drawTempIcon);
  drawStatCell(64, 38, upVal, drawClockIcon);

  drawActivityIndicator();

  oled.display();
}

void resetDashboardLine() {
  dashboardLineIndex = 0;
  dashboardLineBuffer[0] = '\0';
}

void setDashboardScreenPower(bool on) {
  if (!dashboardDisplayReady) return;
  if (dashboardScreenOn == on) return;

  dashboardScreenOn = on;
  oled.ssd1306_command(on ? SSD1306_DISPLAYON : SSD1306_DISPLAYOFF);
}

void applyDashboardLine(const char* line) {
  if (!line || line[0] != '@') return;

  char scratch[96] = {0};
  strncpy(scratch, line + 1, sizeof(scratch) - 1);

  char* savePtr = nullptr;
  char* cpu = strtok_r(scratch, ",", &savePtr);
  char* ram = strtok_r(nullptr, ",", &savePtr);
  char* ip = strtok_r(nullptr, ",", &savePtr);
  char* uptime = strtok_r(nullptr, ",", &savePtr);
  char* temp = strtok_r(nullptr, ",", &savePtr);

  if (!cpu || !ram || !ip || !uptime || !temp) return;

  strncpy(dashboardStats.cpu, cpu, sizeof(dashboardStats.cpu) - 1);
  dashboardStats.cpu[sizeof(dashboardStats.cpu) - 1] = '\0';
  strncpy(dashboardStats.ram, ram, sizeof(dashboardStats.ram) - 1);
  dashboardStats.ram[sizeof(dashboardStats.ram) - 1] = '\0';
  strncpy(dashboardStats.ip, ip, sizeof(dashboardStats.ip) - 1);
  dashboardStats.ip[sizeof(dashboardStats.ip) - 1] = '\0';
  strncpy(dashboardStats.uptime, uptime, sizeof(dashboardStats.uptime) - 1);
  dashboardStats.uptime[sizeof(dashboardStats.uptime) - 1] = '\0';
  strncpy(dashboardStats.temp, temp, sizeof(dashboardStats.temp) - 1);
  dashboardStats.temp[sizeof(dashboardStats.temp) - 1] = '\0';

  dashboardStats.valid = true;
  dashboardStats.lastUpdateMs = millis();
  if (!netNodeHasWiFi) {
    netNodeHasWiFi = true;
    SERIAL_DEBUG.println("[RADIO] Dashboard data resumed, assuming net node WiFi is ON");
  }
  bool otaBusy = (otaState == OTA_RECEIVING) || (lastOtaTrafficMs != 0 && (millis() - lastOtaTrafficMs) < 1500);
  if (!otaBusy) {
    renderDashboard();
  }
}

void initDashboardDisplay() {
  Wire.begin(OLED_PIN_SDA, OLED_PIN_SCL);
  Wire.setClock(400000);

  Wire.beginTransmission(OLED_I2C_ADDR);
  if (Wire.endTransmission() != 0) {
    SERIAL_DEBUG.println("[OLED] No SSD1306 found at 0x3C");
    dashboardDisplayReady = false;
    return;
  }

  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
    SERIAL_DEBUG.println("[OLED] SSD1306 init failed");
    dashboardDisplayReady = false;
    return;
  }

  dashboardDisplayReady = true;
  dashboardScreenOn = true;
  renderInitializingScreen();
}
#endif

void updateBreathing() {
  breathingBrightness += breathingDirection * 10;
  if (breathingBrightness >= 255) {
    breathingBrightness = 255;
    breathingDirection = -1;
  } else if (breathingBrightness <= 0) {
    breathingBrightness = 0;
    breathingDirection = 1;
  }
  ledcWrite(PIN_LED_STATUS, nightMode ? 0 : breathingBrightness);
}

void startDataLedBreathing() {
  ledcAttach(PIN_LED_STATUS, DATA_LED_PWM_FREQ, DATA_LED_PWM_RESOLUTION);
  breathingBrightness = 0;
  breathingDirection = 1;
  breathingTicker.attach_ms(30, updateBreathing);
}

void stopDataLedBreathing() {
  breathingTicker.detach();
  ledcDetach(PIN_LED_STATUS);
  pinMode(PIN_LED_STATUS, OUTPUT);
  digitalWrite(PIN_LED_STATUS, LOW);
}

void triggerStatusLED() {
  if (nightMode) return;

#if IS_SERIAL_GATEWAY
  dashboardActivityFlashUntilMs = millis() + DASHBOARD_ACTIVITY_FLASH_MS;
#endif
  
  if (statusLEDState == STATUS_LED_IDLE) {
    // LED is off, turn it on
    digitalWrite(PIN_LED_STATUS, HIGH);
    statusLEDTimer = millis() + LED_ON_DURATION;
    statusLEDState = STATUS_LED_ON;
  } else if (statusLEDState == STATUS_LED_ON) {
    // LED is on, create brief blink-off to show new event
    digitalWrite(PIN_LED_STATUS, LOW);
    statusLEDTimer = millis() + LED_BLINK_OFF_DURATION;
    statusLEDState = STATUS_LED_BRIEF_OFF;
  }
}

void sendSerialHandshake() {
  SerialHandshake h; 
  h.magic = SERIAL_HANDSHAKE;
  esp_read_mac(h.mac, ESP_MAC_WIFI_STA);
  
  // Parse version from build flag (format: "3.7.3")
  #ifdef FIRMWARE_VERSION
    const char* ver = FIRMWARE_VERSION;
    uint8_t major = 0, minor = 0, patch = 0;
    sscanf(ver, "%hhu.%hhu.%hhu", &major, &minor, &patch);
    h.version_major = major;
    h.version_minor = minor;
    h.version_patch = patch;
  #else
    h.version_major = 0;
    h.version_minor = 0;
    h.version_patch = 0;
  #endif
  
  SERIAL_COM.write((uint8_t*)&h, sizeof(h));
}

void saveGateways() {
  prefs.putBytes("gw", &activeGateways, sizeof(activeGateways));
  SERIAL_DEBUG.printf("[RADIO] Saved %d gateways to NVS\n", activeGateways.count);
}

void loadGateways() {
  size_t len = prefs.getBytes("gw", &activeGateways, sizeof(activeGateways));
  if (len == sizeof(activeGateways) && activeGateways.count > 0) {
    SERIAL_DEBUG.printf("[RADIO] Loaded %d gateways from NVS\n", activeGateways.count);
  } else {
    // No saved gateways, initialize with self
    activeGateways.count = 1;
    esp_read_mac(activeGateways.macs[0], ESP_MAC_WIFI_STA);
    SERIAL_DEBUG.println("[RADIO] No saved gateways, initialized with self");
  }
}

// --- OTA FUNCTIONS ---
void abortOta(const char* reason) {
  SERIAL_DEBUG.printf("[OTA] ABORT: %s\n", reason);
  
  // Notify server about the abort via UART to net node (dual-stack v1/v2)
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  if (gwSecState.crypto_mode != SEC_MODE_V1) {
    uint8_t reasonCode = 0;
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &gwSecState, &gwCounter, PKT_OTA_ABORT, myMac, backendMac, 0,
        &reasonCode, sizeof(reasonCode), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      sendSerialFrame(v2Buf, (uint16_t)v2Len);
    }
  } else {
    HueMixLinkPacket abortPkt;
    memset(&abortPkt, 0, sizeof(HueMixLinkPacket));
    abortPkt.type = PKT_OTA_ABORT;
    memcpy(abortPkt.sourceMAC, myMac, 6);
    memset(abortPkt.targetMAC, 0, 6);
    abortPkt.msgID = 0;
    abortPkt.payload.raw[0] = 0;  // reason_code
    abortPkt.signature = calculateHash(abortPkt.payload.raw, 185, HOME_ID);
    sendSerialFrame((uint8_t*)&abortPkt, sizeof(HueMixLinkPacket));
  }
  
  SERIAL_DEBUG.printf("[OTA] Sent abort notification via UART\n");
  
  // Clean up local state
  if (update_handle) {
    esp_ota_abort(update_handle);
    update_handle = 0;
  }
  
  // Stop DATA LED breathing
  stopDataLedBreathing();
  
  otaState = OTA_IDLE;
  expected_chunk_index = 0;
  otaAccumulatorIndex = 0;
  received_bytes = 0;
}

void handleOtaNotify(HueMixLinkPacket* pkt) {
  // Check if this is for us (our WiFi STA MAC)
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    SERIAL_DEBUG.println("[OTA] NOTIFY forwarded via ESP-NOW");
    return;
  }
  
  // Security: Verify signature for OTA packet targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      SERIAL_DEBUG.printf("[RADIO] SECURITY: Invalid OTA signature. Expected 0x%08X, got 0x%08X\n", expected_sig, pkt->signature);
      SERIAL_DEBUG.println("[RADIO] Rejected unauthorized OTA packet");
      return;
    }
  }
  
  if (otaState != OTA_IDLE) {
    SERIAL_DEBUG.println("[OTA] Busy with another update");
    return;
  }
  
  SERIAL_DEBUG.println("[OTA] NOTIFY received for radio node (self)");
  
  expected_firmware_size = pkt->payload.otaNotify.firmware_size;
  memcpy(expected_sha256, pkt->payload.otaNotify.sha256_hash, 32);
  
  // Initialize update partition
  update_partition = esp_ota_get_next_update_partition(NULL);
  if (!update_partition) {
    SERIAL_DEBUG.println("[OTA] No update partition available");
    abortOta("No partition");
    return;
  }
  
  SERIAL_DEBUG.printf("[OTA] Starting update: %u bytes\n", expected_firmware_size);
  SERIAL_DEBUG.printf("[OTA] Target partition: %s at 0x%x\n", update_partition->label, update_partition->address);
  
  esp_err_t err = esp_ota_begin(update_partition, OTA_SIZE_UNKNOWN, &update_handle);
  if (err != ESP_OK) {
    SERIAL_DEBUG.printf("[OTA] Begin failed: %d\n", err);
    abortOta("Begin failed");
    return;
  }
  
  mbedtls_sha256_init(&sha256_ctx);
  mbedtls_sha256_starts(&sha256_ctx, 0); // 0 = SHA256 (not SHA224)
  
  otaState = OTA_RECEIVING;
  expected_chunk_index = 0;
  received_bytes = 0;
  last_ota_activity = millis();
    
  // Start DATA LED breathing during OTA
  startDataLedBreathing();
  
  // Send PKT_OTA_READY via UART to net node (dual-stack v1/v2)
  if (gwSecState.crypto_mode != SEC_MODE_V1) {
    Payload_OtaReady r = {};
    r.firmware_size = expected_firmware_size;
    r.battery_mv = 0;
    uint8_t myMac[6];
    esp_read_mac(myMac, ESP_MAC_WIFI_STA);
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &gwSecState, &gwCounter, PKT_OTA_READY, myMac, backendMac, 0,
        (const uint8_t*)&r, sizeof(r), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      sendSerialFrame(v2Buf, (uint16_t)v2Len);
      SERIAL_DEBUG.println("[OTA] Sent v2 READY via UART");
    }
  } else {
    HueMixLinkPacket ready;
    memset(&ready, 0, sizeof(HueMixLinkPacket));
    ready.type = PKT_OTA_READY;
    esp_read_mac(ready.sourceMAC, ESP_MAC_WIFI_STA);
    memcpy(ready.targetMAC, pkt->sourceMAC, 6);
    ready.payload.otaReady.firmware_size = expected_firmware_size;
    ready.payload.otaReady.battery_mv = 0; // Not battery powered
    ready.signature = calculateHash(ready.payload.raw, 185, HOME_ID);
    
    sendSerialFrame((uint8_t*)&ready, sizeof(HueMixLinkPacket));
    SERIAL_DEBUG.println("[OTA] Sent READY via UART");
  }
}

void handleOtaChunkRaw(uint16_t chunk_idx, const uint8_t* chunk_data, uint16_t data_len) {
  if (otaState != OTA_RECEIVING) {
    return;
  }
  last_ota_activity = millis();
  if (chunk_idx < expected_chunk_index) {
    return;
  }
  if (chunk_idx != expected_chunk_index) {
    SERIAL_DEBUG.printf("[OTA] Ignoring out-of-order chunk %d (expecting %d)\n", chunk_idx, expected_chunk_index);
    return;
  }

  if (otaAccumulatorIndex + data_len <= sizeof(otaAccumulator)) {
    memcpy(&otaAccumulator[otaAccumulatorIndex], chunk_data, data_len);
    otaAccumulatorIndex += data_len;
  } else {
    if (otaAccumulatorIndex > 0) {
      esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
      if (err != ESP_OK) {
        abortOta("Flash write failed");
        return;
      }
      otaAccumulatorIndex = 0;
    }
    if (data_len <= sizeof(otaAccumulator)) {
      memcpy(&otaAccumulator[0], chunk_data, data_len);
      otaAccumulatorIndex = data_len;
    } else {
      esp_err_t err = esp_ota_write(update_handle, chunk_data, data_len);
      if (err != ESP_OK) {
        abortOta("Flash write failed");
        return;
      }
    }
  }

  mbedtls_sha256_update(&sha256_ctx, chunk_data, data_len);
  received_bytes += data_len;
  expected_chunk_index++;

  if (chunk_idx % 50 == 0) {
    SERIAL_DEBUG.printf("[OTA] Progress: %u / %u bytes (%.1f%%)\n",
      received_bytes, expected_firmware_size,
      (received_bytes * 100.0) / expected_firmware_size);
  }
}

void handleOtaChunk(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
    
  if (otaState != OTA_RECEIVING) {
    return;
  }

  // Security: Verify signature for OTA chunk targeted to us
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      SERIAL_DEBUG.printf("[RADIO] SECURITY: Invalid OTA CHUNK signature\n");
      return;
    }
  }
  
  handleOtaChunkRaw(pkt->payload.otaChunk.chunk_index, pkt->payload.otaChunk.data, pkt->payload.otaChunk.data_len);
}

void sendOtaChunkAck(uint16_t last_chunk_index) {
  if (gwSecState.crypto_mode != SEC_MODE_V1) {
    Payload_OtaChunkAck ack = {};
    ack.last_chunk_index = last_chunk_index;
    uint8_t myMac[6];
    esp_read_mac(myMac, ESP_MAC_WIFI_STA);
    const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
    uint8_t v2Buf[96];
    size_t v2Len = sec_device_encode_tx(
        &gwSecState, &gwCounter, PKT_OTA_CHUNK_ACK, myMac, backendMac, 0,
        (const uint8_t*)&ack, sizeof(ack), v2Buf, sizeof(v2Buf)
    );
    if (v2Len > 0) {
      sendSerialFrame(v2Buf, (uint16_t)v2Len);
      SERIAL_DEBUG.printf("[OTA] Sent v2 checkpoint ACK via UART: last_chunk=%d\n", last_chunk_index);
    }
    return;
  }

  HueMixLinkPacket pkt;
  memset(&pkt, 0, sizeof(HueMixLinkPacket));
  pkt.type = PKT_OTA_CHUNK_ACK;
  esp_read_mac(pkt.sourceMAC, ESP_MAC_WIFI_STA);
  memset(pkt.targetMAC, 0, 6); // Server doesn't need target MAC
  pkt.msgID = 0;
  
  pkt.payload.otaChunkAck.last_chunk_index = last_chunk_index;
  
  // Calculate signature (2 bytes for last_chunk_index)
  pkt.signature = calculateHash(pkt.payload.raw, 185, HOME_ID);
  
  // Send via UART to net node
  sendSerialFrame((uint8_t*)&pkt, sizeof(HueMixLinkPacket));
  
  SERIAL_DEBUG.printf("[OTA] Sent checkpoint ACK via UART: last_chunk=%d\n", last_chunk_index);
}

void handleOtaCheckpointReq(HueMixLinkPacket* pkt) {
  // Check if this is for us
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      SERIAL_DEBUG.println("[RADIO] SECURITY: Invalid OTA CHECKPOINT_REQ signature");
      return;
    }
  }
  
  if (otaState != OTA_RECEIVING) {
    return;
  }

  if (otaAccumulatorIndex > 0) {
    SERIAL_DEBUG.printf("[OTA] Flushing %d bytes at checkpoint\n", otaAccumulatorIndex);
    esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
    if (err != ESP_OK) {
      abortOta("Flash write failed at checkpoint");
      return;
    }
    otaAccumulatorIndex = 0; // Clear RAM buffer
  }
  
  // Respond with last successfully received chunk (expected_chunk_index - 1)
  uint16_t last_chunk = (expected_chunk_index > 0) ? (expected_chunk_index - 1) : 0;
  sendOtaChunkAck(last_chunk);
}

void handleOtaComplete(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      SERIAL_DEBUG.println("[RADIO] SECURITY: Invalid OTA COMPLETE signature");
      return;
    }
  }
  
  SERIAL_DEBUG.printf("[OTA] COMPLETE packet received (state=%d)\n", otaState);
  
  if (otaState != OTA_RECEIVING) {
    SERIAL_DEBUG.printf("[OTA] COMPLETE rejected: wrong state (got %d)\n", otaState);
    return;
  }

  if (otaAccumulatorIndex > 0) {
    SERIAL_DEBUG.printf("[OTA] Flushing final %d bytes\n", otaAccumulatorIndex);
    esp_err_t err = esp_ota_write(update_handle, otaAccumulator, otaAccumulatorIndex);
    if (err != ESP_OK) {
      abortOta("Final flush failed");
      return;
    }
    otaAccumulatorIndex = 0;
  }

  // Send final checkpoint ACK to confirm all chunks received
  if (expected_chunk_index > 0) {
    sendOtaChunkAck(expected_chunk_index - 1);
  }
  
  SERIAL_DEBUG.println("[OTA] COMPLETE received, validating...");
  otaState = OTA_VALIDATING;
  
  // Finalize SHA256
  uint8_t calculated_sha256[32];
  mbedtls_sha256_finish(&sha256_ctx, calculated_sha256);
  mbedtls_sha256_free(&sha256_ctx);
  
  // Compare SHA256
  if (memcmp(calculated_sha256, expected_sha256, 32) != 0) {
    SERIAL_DEBUG.println("[OTA] SHA256 MISMATCH!");
    SERIAL_DEBUG.print("[OTA] Expected: ");
    for(int i=0; i<32; i++) SERIAL_DEBUG.printf("%02X", expected_sha256[i]);
    SERIAL_DEBUG.println();
    SERIAL_DEBUG.print("[OTA] Calculated: ");
    for(int i=0; i<32; i++) SERIAL_DEBUG.printf("%02X", calculated_sha256[i]);
    SERIAL_DEBUG.println();
    abortOta("SHA256 mismatch");
    return;
  }
  
  SERIAL_DEBUG.println("[OTA] SHA256 verified!");
  
  // Finalize OTA
  esp_err_t err = esp_ota_end(update_handle);
  if (err != ESP_OK) {
    SERIAL_DEBUG.printf("[OTA] End failed: %d\n", err);
    abortOta("End failed");
    return;
  }
  update_handle = 0;
  
  // Set boot partition
  err = esp_ota_set_boot_partition(update_partition);
  if (err != ESP_OK) {
    SERIAL_DEBUG.printf("[OTA] Set boot partition failed: %d\n", err);
    abortOta("Set boot failed");
    return;
  }
  
  SERIAL_DEBUG.println("[OTA] UPDATE SUCCESSFUL! Rebooting in 2 seconds...");
  otaState = OTA_COMPLETE;
  
  // Stop breathing effect before flashing LED
  stopDataLedBreathing();
  
  for(int i=0; i<10; i++) {
    digitalWrite(PIN_LED_STATUS, HIGH);
    delay(100);
    digitalWrite(PIN_LED_STATUS, LOW);
    delay(100);
  }
  
  ESP.restart();
}

void handleOtaAbort(HueMixLinkPacket* pkt) {
  uint8_t myMac[6];
  esp_read_mac(myMac, ESP_MAC_WIFI_STA);
  
  if (memcmp(pkt->targetMAC, myMac, 6) != 0) {
    // Not for us, forward via ESP-NOW
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, pkt->targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(pkt->targetMAC)) {
      esp_now_add_peer(&peer);
    }
    esp_now_send(pkt->targetMAC, (uint8_t*)pkt, sizeof(HueMixLinkPacket));
    return;
  }
  
  // Security: Verify signature
  if (HOME_ID != 0) {
    uint32_t expected_sig = calculateHash(pkt->payload.raw, 185, HOME_ID);
    if (pkt->signature != expected_sig) {
      SERIAL_DEBUG.println("[RADIO] SECURITY: Invalid OTA ABORT signature");
      return;
    }
  }
  
  SERIAL_DEBUG.println("[OTA] ABORT received from server");
  abortOta("Server abort");
}

// --- HANDLE PACKET FROM NET NODE (v1 203 B or v2 39..1470 B) ---
void handleSerialPacket(uint8_t* data, uint16_t len) {
  if (len >= 1) {
    uint8_t pt = data[0];
    if (pt == PKT_OTA_NOTIFY || pt == PKT_OTA_CHUNK || pt == PKT_OTA_CHECKPOINT_REQ ||
        pt == PKT_OTA_COMPLETE || pt == PKT_OTA_ABORT) {
      lastOtaTrafficMs = millis();
    }
  }
  // Security v2 variable-length frame (never 203 B)
  if (len != sizeof(HueMixLinkPacket)) {
    if (len < V2_MIN_FRAME_SIZE || len > V2_MAX_FRAME_ESP32 || data[1] != PROTO_VERSION_V2) {
      return;
    }
    HueMixLinkHeaderV2 hdr;
    memcpy(&hdr, data, V2_HEADER_SIZE);

    uint8_t myMac[6];
    esp_read_mac(myMac, ESP_MAC_WIFI_STA);

    // Gateway-targeted v2 PKT_PAIR_CONFIRM (151 B): complete ECDH handshake and return PKT_PAIR_ACK
    if (hdr.type == PKT_PAIR_CONFIRM && len == V2_PAIR_CONFIRM_LEN && memcmp(hdr.targetMAC, myMac, 6) == 0) {
      uint32_t newHomeId = 0;
      uint8_t ackBuf[96];
      size_t ackLen = 0;
      if (sec_pairing_handle_confirm_v2(&gwSecState, myMac, data, len, &gwCounter, &newHomeId, ackBuf, sizeof(ackBuf), &ackLen)) {
        if (newHomeId != 0 && newHomeId != HOME_ID) {
          HOME_ID = newHomeId;
          prefs.putUInt("hid", HOME_ID);
        }
        sendSerialFrame(ackBuf, (uint16_t)ackLen);
        delay(150);
        sendSerialFrame(ackBuf, (uint16_t)ackLen);  // Redundant send in case net node was mid-NVS-write
        SERIAL_DEBUG.printf("[RADIO] PAIRED v2! K_own installed (epoch=%u)\n", gwSecState.key_epoch);
      } else {
        SERIAL_DEBUG.println("[RADIO] SECURITY: Gateway v2 PAIR_CONFIRM failed");
      }
      return;
    }

    // Gateway-targeted PKT_KEY_DISTRIB (0x07): decrypt with K_own[gw_rad] and update peer table.
    // Always consume PKT_KEY_DISTRIB here (never relay over ESP-NOW if still in SEC_MODE_V1).
    if (hdr.type == PKT_KEY_DISTRIB) {
      if (memcmp(hdr.targetMAC, myMac, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
        uint8_t pt[64];
        size_t pt_len = 0;
        if (sec_decode_v2(data, len, gwSecState.k_own, nullptr, pt, sizeof(pt), &pt_len)) {
          sec_gw_handle_key_distrib(&gwPeerTable, pt, pt_len);
          SERIAL_DEBUG.println("[RADIO] Updated Security v2 peer key entry");
        } else {
          SERIAL_DEBUG.println("[RADIO] SECURITY: Rejected invalid PKT_KEY_DISTRIB");
        }
      }
      return;
    }

    // Gateway-targeted v2 PKT_SYS_CMD (CMD_SET_PIN, Night Mode)
    if (hdr.type == PKT_SYS_CMD && memcmp(hdr.targetMAC, myMac, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
      uint8_t pt[64];
      size_t pt_len = 0;
      if (sec_decode_v2(data, len, gwSecState.k_own, nullptr, pt, sizeof(pt), &pt_len) && pt_len >= 1) {
        uint8_t rspBuf[96];
        size_t rspLen = 0;
        if (sec_device_handle_set_pin_cmd(&gwSecState, &gwCounter, myMac, pt, pt_len, rspBuf, sizeof(rspBuf), &rspLen)) {
          sendSerialFrame(rspBuf, (uint16_t)rspLen);
          SERIAL_DEBUG.println("[RADIO] Processed v2 CMD_SET_PIN and sent ACK");
        } else if (pt[0] == 1) {
          nightMode = true;
          #if IS_SERIAL_GATEWAY
            setDashboardScreenPower(false);
          #endif
        } else if (pt[0] == 2) {
          nightMode = false;
          #if IS_SERIAL_GATEWAY
            setDashboardScreenPower(true);
            renderDashboard();
          #endif
        } else if (pt[0] == 3 && pt_len >= 2) {
          netNodeHasWiFi = (pt[1] == 1);
          SERIAL_DEBUG.printf("[RADIO] v2 Net node WiFi status: %s\n", netNodeHasWiFi ? "CONNECTED" : "DISCONNECTED");
        }
      }
      return;
    }

    // Gateway-targeted v2 PKT_GW_LIST_UPD
    if (hdr.type == PKT_GW_LIST_UPD && memcmp(hdr.targetMAC, myMac, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
      uint8_t pt[96];
      size_t pt_len = 0;
      if (sec_decode_v2(data, len, gwSecState.k_own, nullptr, pt, sizeof(pt), &pt_len) && pt_len >= sizeof(Payload_GatewayList)) {
        memcpy(&activeGateways, pt, sizeof(Payload_GatewayList));
        saveGateways();
        SERIAL_DEBUG.printf("[RADIO] Updated v2 Gateways List (%d nodes)\n", activeGateways.count);
      }
      return;
    }

    // Gateway-targeted v2 PKT_PING (serial gateway)
    #if IS_SERIAL_GATEWAY
    if (hdr.type == PKT_PING && memcmp(hdr.targetMAC, myMac, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
      uint8_t pt[32];
      size_t pt_len = 0;
      if (sec_decode_v2(data, len, gwSecState.k_own, nullptr, pt, sizeof(pt), &pt_len)) {
        uint32_t uptime_seconds = millis() / 1000;
        const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
        uint8_t pongBuf[96];
        size_t pongLen = sec_device_encode_tx(
            &gwSecState, &gwCounter, PKT_PING, myMac, backendMac, hdr.msgID,
            (const uint8_t*)&uptime_seconds, sizeof(uptime_seconds),
            pongBuf, sizeof(pongBuf)
        );
        if (pongLen > 0) sendSerialFrame(pongBuf, (uint16_t)pongLen);
      }
      return;
    }
    #endif

    // Gateway-targeted v2 OTA packets
    if ((hdr.type == PKT_OTA_NOTIFY || hdr.type == PKT_OTA_CHUNK ||
         hdr.type == PKT_OTA_CHECKPOINT_REQ || hdr.type == PKT_OTA_COMPLETE ||
         hdr.type == PKT_OTA_ABORT) &&
        memcmp(hdr.targetMAC, myMac, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
      static uint8_t otaPt[1440];
      size_t pt_len = 0;
      if (sec_decode_v2(data, len, gwSecState.k_own, nullptr, otaPt, sizeof(otaPt), &pt_len)) {
        if (hdr.type == PKT_OTA_CHUNK && pt_len >= sizeof(Payload_OtaChunkV2Header)) {
          Payload_OtaChunkV2Header ch;
          memcpy(&ch, otaPt, sizeof(ch));
          if (sizeof(ch) + ch.data_len <= pt_len) {
            handleOtaChunkRaw(ch.chunk_index, otaPt + sizeof(ch), ch.data_len);
          }
        } else {
          HueMixLinkPacket synth;
          memset(&synth, 0, sizeof(synth));
          synth.type = hdr.type;
          memcpy(synth.sourceMAC, hdr.sourceMAC, 6);
          memcpy(synth.targetMAC, myMac, 6);
          synth.msgID = hdr.msgID;
          size_t cp = (pt_len > sizeof(synth.payload.raw)) ? sizeof(synth.payload.raw) : pt_len;
          memcpy(synth.payload.raw, otaPt, cp);
          synth.signature = calculateHash(synth.payload.raw, 185, HOME_ID);
          if (hdr.type == PKT_OTA_NOTIFY) handleOtaNotify(&synth);
          else if (hdr.type == PKT_OTA_CHECKPOINT_REQ) handleOtaCheckpointReq(&synth);
          else if (hdr.type == PKT_OTA_COMPLETE) handleOtaComplete(&synth);
          else if (hdr.type == PKT_OTA_ABORT) handleOtaAbort(&synth);
        }
      }
      return;
    }

    // Relay v2 frame verbatim over ESP-NOW to targetMAC
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, hdr.targetMAC, 6);
    peer.channel = HUEMIXLINK_CHANNEL;
    peer.encrypt = false;
    if (!esp_now_is_peer_exist(hdr.targetMAC)) { esp_now_add_peer(&peer); }
    // Skip per-packet delivery reports for OTA and ping packets (matching v1 behavior;
    // OTA uses checkpoint ACKs so per-chunk delivery reports waste UART/counter/logs).
    bool trackDelivery = (
      hdr.type != PKT_OTA_NOTIFY &&
      hdr.type != PKT_OTA_CHUNK &&
      hdr.type != PKT_OTA_CHECKPOINT_REQ &&
      hdr.type != PKT_OTA_COMPLETE &&
      hdr.type != PKT_OTA_ABORT &&
      hdr.type != PKT_PING_DEVICE
    );
    if (trackDelivery) {
      lastMsgID = hdr.msgID;
      memcpy(lastTargetMAC, hdr.targetMAC, 6);
      waitingForDelivery = true;
    }
    esp_now_send(hdr.targetMAC, data, len);
    return;
  }

  memcpy(&radioTx, data, sizeof(HueMixLinkPacket));
  
  SERIAL_DEBUG.printf("[RADIO] Serial RX Type 0x%02X\n", radioTx.type);

  // Handle OTA packets (anti-downgrade: reject v1 OTA targeted to self once v2-paired)
  if (radioTx.type == PKT_OTA_NOTIFY || radioTx.type == PKT_OTA_CHUNK ||
      radioTx.type == PKT_OTA_CHECKPOINT_REQ || radioTx.type == PKT_OTA_COMPLETE ||
      radioTx.type == PKT_OTA_ABORT) {
    uint8_t myMacV1[6];
    esp_read_mac(myMacV1, ESP_MAC_WIFI_STA);
    if (memcmp(radioTx.targetMAC, myMacV1, 6) == 0 && gwSecState.crypto_mode != SEC_MODE_V1) {
      SERIAL_DEBUG.printf("[RADIO] SECURITY: Rejected v1 OTA packet (type=0x%02X) while v2-paired\n", radioTx.type);
      return;
    }
    if (radioTx.type == PKT_OTA_NOTIFY) handleOtaNotify(&radioTx);
    else if (radioTx.type == PKT_OTA_CHUNK) handleOtaChunk(&radioTx);
    else if (radioTx.type == PKT_OTA_CHECKPOINT_REQ) handleOtaCheckpointReq(&radioTx);
    else if (radioTx.type == PKT_OTA_COMPLETE) handleOtaComplete(&radioTx);
    else if (radioTx.type == PKT_OTA_ABORT) handleOtaAbort(&radioTx);
    return;
  }

  #if IS_SERIAL_GATEWAY
    if (radioTx.type == PKT_PING) {
      uint32_t uptime_seconds = millis() / 1000;
      uint8_t myMac[6];
      esp_read_mac(myMac, ESP_MAC_WIFI_STA);
      if (gwSecState.crypto_mode != SEC_MODE_V1) {
        const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
        uint32_t ctr = 0;
        if (sec_counter_next(&gwCounter, &ctr)) {
          uint8_t pongBuf[96];
          size_t pongLen = sec_encode_v2(
              PKT_PING, myMac, backendMac, radioTx.msgID, ctr, gwSecState.key_epoch,
              (const uint8_t*)&uptime_seconds, sizeof(uptime_seconds),
              gwSecState.k_own, pongBuf, sizeof(pongBuf)
          );
          if (pongLen > 0) sendSerialFrame(pongBuf, (uint16_t)pongLen);
        }
      } else {
        HueMixLinkPacket pingRsp;
        memset(&pingRsp, 0, sizeof(HueMixLinkPacket));
        pingRsp.type = PKT_PING;
        memcpy(pingRsp.sourceMAC, myMac, 6);
        memcpy(pingRsp.targetMAC, radioTx.sourceMAC, 6);
        pingRsp.msgID = radioTx.msgID;
        memcpy(&pingRsp.payload.raw[0], &uptime_seconds, sizeof(uptime_seconds));
        pingRsp.signature = calculateHash(pingRsp.payload.raw, 185, HOME_ID);
        sendSerialFrame((uint8_t*)&pingRsp, sizeof(HueMixLinkPacket));
      }
      return;
    }
  #endif
  
  if (radioTx.type == PKT_SYS_CMD) {
    // Security: Verify signature for SYS_CMD
    if (HOME_ID != 0) {
      uint32_t expected_sig = calculateHash(radioTx.payload.raw, 185, HOME_ID);
      if (radioTx.signature != expected_sig) {
        SERIAL_DEBUG.printf("[RADIO] SECURITY: Invalid SYS_CMD signature. Expected 0x%08X, got 0x%08X\n", expected_sig, radioTx.signature);
        SERIAL_DEBUG.println("[RADIO] Rejected unauthorized SYS_CMD");
        return;
      }
    }
    
        if (radioTx.payload.sys.cmd == 1) {
      nightMode = true;
    #if IS_SERIAL_GATEWAY
      setDashboardScreenPower(false);
    #endif
        }
        else if (radioTx.payload.sys.cmd == 2) {
      nightMode = false;
    #if IS_SERIAL_GATEWAY
      setDashboardScreenPower(true);
      renderDashboard();
    #endif
        }
    else if (radioTx.payload.sys.cmd == 3) {
      // WiFi status update from net node
      netNodeHasWiFi = (radioTx.payload.raw[1] == 1);
      SERIAL_DEBUG.printf("[RADIO] Net node WiFi status: %s\n", netNodeHasWiFi ? "CONNECTED" : "DISCONNECTED");
      return;  // Don't forward this command via ESP-NOW
    }
    else if (radioTx.payload.sys.cmd == CMD_FACTORY_RESET) {
      uint8_t myMac[6];
      esp_read_mac(myMac, ESP_MAC_WIFI_STA);
      if (memcmp(radioTx.targetMAC, myMac, 6) == 0) {
        SERIAL_DEBUG.println("[RADIO] Factory reset command received from net node!");
        sec_device_on_factory_reset(&gwSecState);
        prefs.remove("hid");
        prefs.remove("gw");
        delay(50);
        ESP.restart();
        return;
      }
      // Targeted at a downstream radio device: forward over ESP-NOW
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
    else if (radioTx.payload.sys.cmd == CMD_UPGRADE_TO_V2) {
      uint8_t myMac[6];
      esp_read_mac(myMac, ESP_MAC_WIFI_STA);
      if (memcmp(radioTx.targetMAC, myMac, 6) == 0) {
        if (gwSecState.crypto_mode != SEC_MODE_V1) {
          SERIAL_DEBUG.println("[RADIO] Clearing stale v2 keys for re-pairing (CMD_UPGRADE_TO_V2)");
          sec_pairing_clear_keys(&gwSecState);
        }
        SERIAL_DEBUG.println("[RADIO] Triggering in-place v2 upgrade HELLO");
        lastSecHelloMs = millis();
        sendRadioSecHello();
        return;
      }
      // Targeted at a downstream radio device: forward over ESP-NOW
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
    SERIAL_DEBUG.printf("[RADIO] Updated Gateways List (%d nodes)\n", activeGateways.count);
    
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
    // Anti-downgrade: only accept v1 HOME_ID update while still in v1 and signature is valid
    if (sec_pairing_allow_v1_confirm(&gwSecState)) {
      uint32_t expectedSig = calculateHash(radioTx.payload.raw, 185, 0);
      if (radioTx.signature == expectedSig && radioTx.payload.pair.newHomeID != 0) {
        HOME_ID = radioTx.payload.pair.newHomeID;
        prefs.putUInt("hid", HOME_ID);
      }
    }

    uint8_t zeroMac[6] = {0};
    if (memcmp(radioTx.targetMAC, zeroMac, 6) != 0) {
      esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, radioTx.targetMAC, 6);
      peer.channel = HUEMIXLINK_CHANNEL; peer.encrypt = false;
      if (!esp_now_is_peer_exist(radioTx.targetMAC)) { esp_now_add_peer(&peer); }
      lastMsgID = radioTx.msgID;
      memcpy(lastTargetMAC, radioTx.targetMAC, 6);
      waitingForDelivery = true;
      esp_now_send(radioTx.targetMAC, (uint8_t*)&radioTx, sizeof(radioTx));
    }
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
  // Inter-frame idle timeout: a 1470 B frame at 460800 baud takes ~32 ms continuously.
  // If >100 ms elapsed mid-frame, reset to S_IDLE so a corrupted frame can never swallow
  // subsequent packets (e.g. checkpoint requests).
  if (rxState != S_IDLE && (millis() - last_serial_activity) > 100) {
    SERIAL_DEBUG.printf("[RADIO] Serial frame timeout in state %d (got %u/%u B) - resync\n",
                        rxState, rxIndex, rxExpectedLen);
    rxState = S_IDLE;
    rxIndex = 0;
    rxExpectedLen = 0;
  }

  switch(rxState) {
    case S_IDLE:
      if (b == SERIAL_START) { 
        rxState = S_LEN_LO; 
        rxIndex = 0; 
        rxExpectedLen = 0;
        last_serial_activity = millis();
      } 
      else if (b == SERIAL_REQ_HANDSHAKE && (millis() - last_serial_activity) > SERIAL_IDLE_THRESHOLD) { 
        handshake_request_received = true;  // Mark that we got a request
        SERIAL_DEBUG.println("[RADIO] Received handshake request from net node");
        sendSerialHandshake(); 
        lastSecHelloMs = 0;  // Re-advertise v2 HELLO so host/net-node syncs K_ack peer table
        last_serial_activity = millis();
      }
#if IS_SERIAL_GATEWAY
      else if (b == '@') {
        resetDashboardLine();
        dashboardLineBuffer[dashboardLineIndex++] = '@';
      }
      else if (dashboardLineIndex > 0) {
        if (b == '\n' || b == '\r') {
          dashboardLineBuffer[dashboardLineIndex] = '\0';
          applyDashboardLine(dashboardLineBuffer);
          resetDashboardLine();
        } else if (b >= 32 && b <= 126) {
          if (dashboardLineIndex < sizeof(dashboardLineBuffer) - 1) {
            dashboardLineBuffer[dashboardLineIndex++] = (char)b;
          } else {
            resetDashboardLine();
          }
        } else {
          resetDashboardLine();
        }
      }
#endif
      break;
    case S_LEN_LO:
      last_serial_activity = millis();
      rxExpectedLen = b;
      rxState = S_LEN_HI;
      break;
    case S_LEN_HI:
      last_serial_activity = millis();
      rxExpectedLen |= ((uint16_t)b << 8);
      if (rxExpectedLen < V2_OVERHEAD || rxExpectedLen > V2_MAX_FRAME_ESP32) {
        SERIAL_DEBUG.printf("[RADIO] Invalid serial frame len %u - resync\n", rxExpectedLen);
        rxState = (b == SERIAL_START) ? S_LEN_LO : S_IDLE;
        rxIndex = 0;
        rxExpectedLen = 0;
      } else {
        rxIndex = 0;
        rxState = S_READING;
      }
      break;
    case S_READING:
      last_serial_activity = millis();
      rxRawBuffer[rxIndex++] = b;
      if (rxIndex >= rxExpectedLen) {
        rxState = S_FOOTER;
      }
      break;
    case S_FOOTER:
      last_serial_activity = millis();
      if (b == SERIAL_END) {
        handleSerialPacket(rxRawBuffer, rxExpectedLen);
        rxState = S_IDLE;
      } else if (b == SERIAL_START) {
        SERIAL_DEBUG.printf("[RADIO] Footer Err (got 0x%02X), resyncing to next packet\n", b);
        rxState = S_LEN_LO;
        rxIndex = 0;
        rxExpectedLen = 0;
      } else {
        SERIAL_DEBUG.printf("[RADIO] Footer Err (got 0x%02X), discarding packet\n", b);
        rxState = S_IDLE;
      }
      break;
  }
}

void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  if (waitingForDelivery && !pendingDeliveryRpt) {
    pendingDeliveryPayload.originalMsgID = lastMsgID;
    pendingDeliveryPayload.success = (status == ESP_NOW_SEND_SUCCESS);
    memcpy(pendingDeliveryPayload.targetMAC, lastTargetMAC, 6);
    pendingDeliveryRpt = true;
    triggerStatusLED();
  }
}

void OnDataRecv(const esp_now_recv_info_t * info, const uint8_t *data, int len) {
  bool is_v1 = (len == (int)sizeof(HueMixLinkPacket));
  bool is_v2 = (!is_v1 && len >= V2_MIN_FRAME_SIZE && len <= V2_MAX_FRAME_ESP32 && data[1] == PROTO_VERSION_V2);
  if (!is_v1 && !is_v2) return;

  uint8_t nextHead = (uint8_t)((radioRxHead + 1) % RADIO_RX_QUEUE_SIZE);
  if (nextHead == radioRxTail) return; // Queue full

  RadioRxSlot &slot = radioRxQueue[radioRxHead];
  memcpy(slot.buf, data, (size_t)len);
  slot.len = (uint16_t)len;

  if (is_v1) {
    // Legacy v1: stamp sourceMAC at offset 5 and RSSI into payload hole
    memcpy(slot.buf + 5, info->src_addr, 6);
    if (slot.buf[0] == PKT_HELLO) {
      slot.buf[18 + 1] = (uint8_t)info->rx_ctrl->rssi;
    } else if (slot.buf[0] == PKT_PING_DEVICE) {
      slot.buf[18 + 0] = (uint8_t)info->rx_ctrl->rssi;
    }
  } else {
    // Security v2: stamp RSSI into header offset 21 (excluded from AAD)
    slot.buf[21] = (uint8_t)info->rx_ctrl->rssi;
  }

  radioRxHead = nextHead;
  triggerStatusLED();
}

void setup() {
  pinMode(PIN_LED_STATUS, OUTPUT);

  #if IS_SERIAL_GATEWAY
    SERIAL_COM.setRxBufferSize(8192);
    SERIAL_COM.setTxBufferSize(8192);
    SERIAL_COM.begin(460800);
    SERIAL_DEBUG.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX);
    initDashboardDisplay();
  #else
    SERIAL_DEBUG.begin(115200);
    SERIAL_COM.setRxBufferSize(8192);
    SERIAL_COM.setTxBufferSize(8192);
    SERIAL_COM.begin(460800, SERIAL_8N1, PIN_RX, PIN_TX);
  #endif

  WiFi.mode(WIFI_STA); 
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  if (esp_now_init() != ESP_OK) ESP.restart();
  uint32_t espnow_ver = 0;
  if (esp_now_get_version(&espnow_ver) == ESP_OK) {
    SERIAL_DEBUG.printf("[RADIO] ESP-NOW version: %u (max frame %u B)\n",
                        espnow_ver, (espnow_ver >= 2) ? V2_MAX_FRAME_ESP32 : V2_MAX_FRAME_ESP8266);
  }
  sec_pairing_load(&gwSecState);
  sec_gw_table_init(&gwPeerTable);
  sec_counter_init_default(&gwCounter);

  if(!prefs.begin("huemixlink", false)) {
    nvs_flash_erase(); nvs_flash_init(); prefs.begin("huemixlink", false);
  }
  HOME_ID = prefs.getUInt("hid", 0);

  // Track startup for handshake timeout
  startup_time = millis();
  last_serial_activity = millis();
  handshake_request_received = false;

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
    SERIAL_DEBUG.println("[RADIO] Added self to gateway list");
  }
  
  SERIAL_DEBUG.println("--- RADIO NODE READY ---");
}

void loop() {
  if (!handshake_request_received && (millis() - startup_time) > STARTUP_HANDSHAKE_TIMEOUT) {
    SERIAL_DEBUG.println("[RADIO] No handshake request from net node, requesting...");
    SERIAL_COM.write(SERIAL_REQ_HANDSHAKE);
    SERIAL_COM.flush();
    handshake_request_received = true;
  }

  // Periodically advertise Security v2 gateway HELLO until paired (or once on boot to sync K_ack table)
  if ((netNodeHasWiFi || IS_SERIAL_GATEWAY) &&
      ((gwSecState.crypto_mode == SEC_MODE_V1 && (lastSecHelloMs == 0 || millis() - lastSecHelloMs > 5000)) ||
       (gwSecState.crypto_mode != SEC_MODE_V1 && lastSecHelloMs == 0))) {
    lastSecHelloMs = millis();
    sendRadioSecHello();
  }

  // OTA timeout check
  if (otaState == OTA_RECEIVING && millis() - last_ota_activity > 30000) {
    SERIAL_DEBUG.println("[OTA] Timeout - no activity for 30s");
    abortOta("Timeout");
  }

  while (radioRxTail != radioRxHead) {
    uint8_t* bufferPktBuf = radioRxQueue[radioRxTail].buf;
    uint16_t bufferPktLen = radioRxQueue[radioRxTail].len;
    sendSerialFrame(bufferPktBuf, bufferPktLen);

    uint8_t inType = bufferPktBuf[0];
    if (inType == PKT_OTA_READY || inType == PKT_OTA_CHUNK_ACK || inType == PKT_OTA_ABORT) {
      lastOtaTrafficMs = millis();
    }
    bool inIsV2 = (bufferPktLen != sizeof(HueMixLinkPacket) && bufferPktLen >= V2_MIN_FRAME_SIZE && bufferPktBuf[1] == PROTO_VERSION_V2);
    const uint8_t* inSrcMac = inIsV2 ? (bufferPktBuf + 2) : (bufferPktBuf + 5);

    if (shouldAckInboundPacket(inType)) {
      // Only send ACK if net node has WiFi connectivity
      if (netNodeHasWiFi) {
        esp_now_peer_info_t peer = {};
        memcpy(peer.peer_addr, inSrcMac, 6);
        peer.channel = HUEMIXLINK_CHANNEL;
        peer.encrypt = false;
        if (!esp_now_is_peer_exist(inSrcMac)) esp_now_add_peer(&peer);

        const SecGwPeerEntry* peerSec = inIsV2 ? sec_gw_table_find(&gwPeerTable, inSrcMac) : nullptr;
        if (inIsV2 && peerSec != nullptr) {
          // Security v2 ACK encrypted with K_ack[d] carrying activeGateways
          uint8_t myMac[6];
          esp_read_mac(myMac, ESP_MAC_WIFI_STA);
          uint32_t ackCtr = 0;
          if (sec_counter_next(&gwCounter, &ackCtr)) {
            uint8_t ackBuf[128];
            size_t ackLen = sec_encode_v2(
                PKT_ACK_TO_BTN, myMac, inSrcMac, 0, ackCtr, gwSecState.key_epoch,
                (const uint8_t*)&activeGateways, sizeof(activeGateways),
                peerSec->k_ack, ackBuf, sizeof(ackBuf)
            );
            if (ackLen > 0) {
              esp_now_send(inSrcMac, ackBuf, ackLen);
            }
          }
        } else if (!inIsV2) {
          // Legacy v1 203-byte ACK
          HueMixLinkPacket ack;
          memset(&ack, 0, sizeof(HueMixLinkPacket));
          ack.type = PKT_ACK_TO_BTN;
          esp_read_mac(ack.sourceMAC, ESP_MAC_WIFI_STA);
          memcpy(ack.targetMAC, inSrcMac, 6);
          ack.msgID = 0;
          ack.payload.gwList = activeGateways;
          ack.signature = calculateHash(ack.payload.raw, 185, HOME_ID);
          esp_now_send(inSrcMac, (uint8_t*)&ack, sizeof(ack));
        }
      } else {
        SERIAL_DEBUG.println("[RADIO] Button ACK suppressed - Net node has no WiFi");
      }
    }
    radioRxTail = (uint8_t)((radioRxTail + 1) % RADIO_RX_QUEUE_SIZE);
  }

  // STATUS LED STATE MACHINE HANDLER
  if (statusLEDState == STATUS_LED_ON && millis() >= statusLEDTimer) {
    digitalWrite(PIN_LED_STATUS, LOW);
    statusLEDState = STATUS_LED_IDLE;
  } else if (statusLEDState == STATUS_LED_BRIEF_OFF && millis() >= statusLEDTimer) {
    digitalWrite(PIN_LED_STATUS, HIGH);
    statusLEDTimer = millis() + LED_ON_DURATION;
    statusLEDState = STATUS_LED_ON;
  }

  if (pendingDeliveryRpt) {
    Payload_Report rptPayload = pendingDeliveryPayload;
    pendingDeliveryRpt = false;
    uint8_t myMac[6];
    esp_read_mac(myMac, ESP_MAC_WIFI_STA);

    if (gwSecState.crypto_mode != SEC_MODE_V1) {
      const uint8_t backendMac[6] = {0, 0, 0, 0, 0, 0};
      uint32_t ctr = 0;
      if (sec_counter_next(&gwCounter, &ctr)) {
        uint8_t v2Buf[96];
        size_t v2Len = sec_encode_v2(
            PKT_DELIVERY_RPT, myMac, backendMac, 0, ctr, gwSecState.key_epoch,
            (const uint8_t*)&rptPayload, sizeof(Payload_Report),
            gwSecState.k_own, v2Buf, sizeof(v2Buf)
        );
        if (v2Len > 0) {
          sendSerialFrame(v2Buf, (uint16_t)v2Len);
        }
      }
    } else {
      HueMixLinkPacket rpt;
      memset(&rpt, 0, sizeof(HueMixLinkPacket));
      rpt.type = PKT_DELIVERY_RPT;
      memcpy(rpt.sourceMAC, myMac, 6);
      rpt.payload.report = rptPayload;
      rpt.signature = calculateHash(rpt.payload.raw, 185, HOME_ID);
      sendSerialFrame((uint8_t*)&rpt, sizeof(rpt));
    }
    waitingForDelivery = false;
  }

  if (!waitingForDelivery) {
    while (SERIAL_COM.available() > 0) {
      parseSerialByte(SERIAL_COM.read());
      if (waitingForDelivery) break;
    }
  }

#if IS_SERIAL_GATEWAY
  if (dashboardDisplayReady && (millis() - lastDashboardRender) > 250) {
    lastDashboardRender = millis();
    bool otaBusy = (otaState == OTA_RECEIVING) || (lastOtaTrafficMs != 0 && (millis() - lastOtaTrafficMs) < 1500);
    if (otaBusy) {
      if (dashboardStats.valid) {
        dashboardStats.lastUpdateMs = millis();
      }
    } else {
      renderDashboard();
    }
  }
#endif
}
