#include <WiFi.h>
#include <esp_now.h>
#include <Bounce2.h>
#include <SPIFFS.h>
#include <vector>

#define BUTTON_PIN 12
#define LED_PIN 18

Bounce button;

bool dataSent = false;
bool flagCheck = false;
bool buttonPressed = false;
bool buttonHeld = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingInterval = 500;
unsigned long holdingIntervalUpdate = 0;
unsigned long holdingThreshold = 1000;
String macAddressString;
esp_now_peer_info_t peerInfo;
bool wakeupExt0 = false; // Used to know if the button was pressed when waking up
int lastButtonPress = 0;
int sleepTimer = 2000;
const char* macFileName = "/mac_addresses.txt";

std::vector<std::array<uint8_t, 6>> repeaterMacs;

void sleep() {
  delay(500);
  pinMode(LED_PIN, INPUT);
  WiFi.mode(WIFI_OFF);
  btStop();
  Serial.println("Going to sleep now");
  esp_sleep_enable_ext0_wakeup(GPIO_NUM_12,0);
  esp_deep_sleep_start();
}

void addMacAddress(const uint8_t mac[6]) {
  std::array<uint8_t, 6> newMac;
  std::copy(mac, mac + 6, newMac.begin());
  repeaterMacs.push_back(newMac);
}

void saveMacAddresses() {
  File file = SPIFFS.open(macFileName, FILE_WRITE);
  if (file) {
    for (size_t i = 0; i < repeaterMacs.size(); i++) {
      for (uint8_t j = 0; j < 6; j++) {
        file.print(repeaterMacs[i][j], HEX);
        file.print(":");
      }
      file.println();
    }
    file.close();
    Serial.println("MAC addresses saved successfully.");
  } else {
    Serial.println("Error opening file for writing.");
  }
}

void loadMacAddresses() {
  File file = SPIFFS.open(macFileName);
  if (file) {
    repeaterMacs.clear();
    char line[20];
    while (file.available()) {
      file.readBytesUntil('\n', line, sizeof(line));
      char* token = strtok(line, ":");
      std::array<uint8_t, 6> mac;
      uint8_t j = 0;
      while (token != NULL && j < 6) {
        mac[j] = strtol(token, NULL, 16);
        token = strtok(NULL, ":");
        j++;
      }
      repeaterMacs.push_back(mac);
    }
    file.close();
  } else {
    Serial.println("Error opening file for reading.");
  }
}

void resetMacAddresses() {
  repeaterMacs.clear();
}

void processResponse(const String& response) {
  size_t pos = 0;
  while (pos < response.length()) {
    size_t commaIndex = response.indexOf(',', pos);
    if (commaIndex == -1) {
      commaIndex = response.length();
    }
    String macString = response.substring(pos, commaIndex);
    processMacAddress(macString);
    pos = commaIndex + 1;
  }
}

void processMacAddress(const String& macString) {
  std::array<uint8_t, 6> mac;
  size_t byteIndex = 0;
  size_t colonIndex = macString.indexOf(':');
  size_t prevColonIndex = 0;
  while (colonIndex != -1 && byteIndex < 6) {
    String byteString = macString.substring(prevColonIndex, colonIndex);
    mac[byteIndex] = strtol(byteString.c_str(), NULL, 16);
    byteIndex++;
    prevColonIndex = colonIndex + 1;
    colonIndex = macString.indexOf(':', prevColonIndex);
  }
  if (byteIndex == 5) {
    String byteString = macString.substring(prevColonIndex);
    mac[byteIndex] = strtol(byteString.c_str(), NULL, 16);
    addMacAddress(mac.data());
  } else {
    Serial.println("Invalid MAC address format: " + macString);
  }
}

uint8_t hexToDigit(char c) {
  if (c >= '0' && c <= '9') {
    return c - '0';
  } else if (c >= 'A' && c <= 'F') {
    return 10 + (c - 'A');
  } else if (c >= 'a' && c <= 'f') {
    return 10 + (c - 'a');
  }
  return 0;
}

void printMacs() {
    for (size_t i = 0; i < repeaterMacs.size(); i++) {
    Serial.print("MAC ");
    Serial.print(i + 1);
    Serial.print(": ");
    for (uint8_t j = 0; j < 6; j++) {
      Serial.print(repeaterMacs[i][j], HEX);
      Serial.print(":");
    }
    Serial.println();
  }
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  button.attach(BUTTON_PIN, INPUT_PULLUP);
  button.interval(50);
  Serial.begin(115200);
  if (!SPIFFS.begin(true)) {
    sleep();
    return;
  }

  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  macAddressString = WiFi.macAddress();
  macAddressString.toLowerCase();
  loadMacAddresses();
  // uint8_t mac[6] = {0xBC,0xDD,0xC2, 0x2D, 0xEF, 0xE1};
  // addMacAddress(mac);
  // saveMacAddresses();
  printMacs();
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    sleep();
    return;
  }

  for (const auto& mac : repeaterMacs) {
    memcpy(peerInfo.peer_addr, mac.data(), 6);
    peerInfo.channel = 0;  
    peerInfo.encrypt = false;
    esp_now_add_peer(&peerInfo);
  }
  esp_now_register_send_cb(onDataSent);
  esp_now_register_recv_cb(OnDataRecv);

  esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
  switch (wakeupReason) {
    case ESP_SLEEP_WAKEUP_EXT0:
      wakeupExt0 = true;
      break;
    default:
      sleep();
  }
}

void ledSuccess() {
  digitalWrite(LED_PIN, HIGH);
  delay(200);
  digitalWrite(LED_PIN, LOW);
}

void ledFailed() {
  digitalWrite(LED_PIN, HIGH);
  delay(200);
  digitalWrite(LED_PIN, LOW);
  delay(200);
  digitalWrite(LED_PIN, HIGH);
  delay(200);
  digitalWrite(LED_PIN, LOW);
}

void sendData(const char* message) {
  size_t numMacs = repeaterMacs.size();
  const size_t combinedSize = macAddressString.length() + strlen(message) + 1;
  char combinedString[combinedSize];
  strcpy(combinedString, macAddressString.c_str());
  strcat(combinedString, ",");
  strcat(combinedString, message);
  dataSent = false;
  for (size_t i = 0; i < numMacs; i++) {
    flagCheck = false;
    esp_err_t status = esp_now_send(repeaterMacs[i].data(), reinterpret_cast<const uint8_t*>(combinedString), strlen(combinedString));
    while (!flagCheck) {
      delay(10);
    }
    if (dataSent) {
      Serial.println("Message sent successfully");
      ledSuccess();
      break;
    }
  }
  if (!dataSent) {
    lookUpForServers(message);
    Serial.println("Failed to send message");
    ledFailed();
    sleep();
  }
}

void lookUpForServers(const char* message) {
  const size_t combinedSize = macAddressString.length() + strlen(message) + 1 + strlen("HML:LFB;");
  char combinedString[combinedSize];
  strcpy(combinedString, "HML:LFB;");
  strcat(combinedString, macAddressString.c_str());
  strcat(combinedString, ",");
  strcat(combinedString, message);
  uint8_t globalMacAddress[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  memcpy(peerInfo.peer_addr, globalMacAddress, 6);
  peerInfo.channel = 0;  
  peerInfo.encrypt = false;
  esp_now_add_peer(&peerInfo);
  esp_now_send(globalMacAddress, reinterpret_cast<const uint8_t*>(combinedString), strlen(combinedString));
  flagCheck = false;
  while (!flagCheck) {
    delay(10);
  }
  if (dataSent) {
    ledSuccess();
  } else {
    ledFailed();
  }
}

void loop() {
  button.update();
  if (button.fell() || wakeupExt0) {
    wakeupExt0 = false;
    lastButtonPress = millis();
    buttonHoldStartTime = millis();
    buttonPressed = true;
  }

  if (button.read() == LOW && buttonPressed && !buttonHeld) {
    if (millis() - buttonHoldStartTime >= holdingThreshold) {
      buttonHeld = true;
    }
  }

  if (button.rose()) {
    if (buttonHeld) {
      buttonHeld = false;
      sendData("HoldingStopped");
      Serial.println("HoldingStopped");
    } else {
      sendData("Once");
      Serial.println("Once");
    }
    buttonPressed = false;
  }

  if (millis() - holdingIntervalUpdate >= holdingInterval && buttonHeld) {
    sendData("Holding");
    Serial.println("Holding");
    holdingIntervalUpdate = millis();
    lastButtonPress = millis();
  }
  if (millis() - lastButtonPress >= sleepTimer) {
    sleep();
  }
}

void OnDataRecv(const uint8_t* mac, const uint8_t* data, int len) {
  resetMacAddresses();
  String receivedData(reinterpret_cast<const char*>(data), len);
  if (receivedData != "cancel"){
    processResponse(receivedData);
    saveMacAddresses();
    loadMacAddresses();
    printMacs();
  }
}

void onDataSent(const uint8_t* mac, esp_now_send_status_t sendStatus) {
  if (sendStatus == ESP_OK) {
    dataSent = true;
  }
  flagCheck = true;
}