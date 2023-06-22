#include <ESP8266WiFi.h>
#include <espnow.h>
#include <Bounce2.h>
#include <FS.h>
#include <vector>
#include <array>

#define BUTTON_PIN D2
#define LED_PIN D4

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
int lastButtonPress = 0;
const char* macFileName = "/mac_addresses.txt";

std::vector<std::array<uint8_t, 6>> repeaterMacs;

void addMacAddress(const uint8_t mac[6]) {
  std::array<uint8_t, 6> newMac;
  std::copy(mac, mac + 6, newMac.begin());
  repeaterMacs.push_back(newMac);
}

void checkMacFile() {
  if (!SPIFFS.exists(macFileName)) {
    File file = SPIFFS.open(macFileName, "w");
    if (file) {
      file.close();
      Serial.println("Created mac_addresses.txt file.");
    } else {
      Serial.println("Failed to create mac_addresses.txt file.");
    }
  }
}

void saveMacAddresses() {
  File file = SPIFFS.open(macFileName, "w");
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
  File file = SPIFFS.open(macFileName, "r");
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
  if (!SPIFFS.begin()) {
    return;
  }
  checkMacFile();
  WiFi.mode(WIFI_STA);
  macAddressString = WiFi.macAddress();
  macAddressString.toLowerCase();
  saveMacAddresses();
  printMacs();
  if (esp_now_init() != 0) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  esp_now_set_self_role(ESP_NOW_ROLE_COMBO); 
  for (const auto& mac : repeaterMacs) {
    esp_now_add_peer(const_cast<u8*>(reinterpret_cast<const u8*>(mac.data())), ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  }
  esp_now_register_send_cb(OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);
  digitalWrite(LED_PIN, HIGH);
}


void ledSuccess() {
  digitalWrite(LED_PIN, LOW);
  delay(200);
  digitalWrite(LED_PIN, HIGH);
}

void ledFailed() {
  digitalWrite(LED_PIN, LOW);
  delay(200);
  digitalWrite(LED_PIN, HIGH);
  delay(200);
  digitalWrite(LED_PIN, LOW);
  delay(200);
  digitalWrite(LED_PIN, HIGH);
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
    esp_now_send(repeaterMacs[i].data(), const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(combinedString)), strlen(combinedString));
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
    Serial.println("Failed to send message");
    lookUpForServers(message);
    ledFailed();
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
  esp_now_add_peer(globalMacAddress, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  esp_now_send(globalMacAddress, const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(combinedString)), strlen(combinedString));
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
  if (button.fell()) {
    lastButtonPress = millis();
    buttonHoldStartTime = millis();
    buttonPressed = true;
  }

  if (button.read() == HIGH && buttonPressed && !buttonHeld) {
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
}

void OnDataRecv(uint8_t * mac, uint8_t * data, uint8_t len) {
  Serial.print("Recieved: ");
  String receivedData;
  for (int i = 0; i < len; i++) {
    receivedData += (char)data[i];
  }
  Serial.println(receivedData);
  if (receivedData != "cancel"){
    resetMacAddresses();
    processResponse(receivedData);
    saveMacAddresses();
    loadMacAddresses();
    printMacs();
  }
}

void OnDataSent(uint8_t * mac, uint8_t sendStatus) {
  if (sendStatus == 0) {
    dataSent = true;
  }
  flagCheck = true;
}