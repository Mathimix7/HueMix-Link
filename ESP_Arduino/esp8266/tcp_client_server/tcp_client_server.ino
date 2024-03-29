#include <ESP8266WiFi.h>
#include <WiFiManager.h>
#include <WiFiClient.h>
#include <FS.h>
#include <Ticker.h>
#include <Bounce2.h>
#include <SoftwareSerial.h>

#define LED_WIFI 4
#define LED_SERIAL 32
#define BUTTON_PIN 22
#define ESP32_UART_TX_PIN 12  // Pin D6 of ESP8266
#define ESP32_UART_RX_PIN 13  // Pin D7 of ESP8266
#define BUTTON_RESET 14 // Pin D5 of ESP8266

SoftwareSerial esp32Serial(ESP32_UART_RX_PIN, ESP32_UART_TX_PIN);

bool macAddressesSave;
String macAddresses;
String macAddressString;
String serverIPString;
String serverPortString;
const char* serverIP;
int serverPort;
bool buttonState = false;
bool lastButtonState = false;
long buttonPressTime;
char ssid[] = "HueMix Link - ";
const int combinedLength = sizeof(ssid) + 9; 
char ssidName[combinedLength];
bool ledState = false;
int lastButtonPress = 0;
bool buttonPressed = false;
bool buttonHeld = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingInterval = 500;
unsigned long holdingIntervalUpdate = 0;
unsigned long holdingThreshold = 1000;

Ticker ticker;
WiFiClient client;
WiFiManager wifiManager;
Bounce button;

WiFiManagerParameter custom_tcp_server("server", "TCP Server IP", "", 40);
WiFiManagerParameter custom_tcp_port("port", "TCP Server Port", "7777", 6);

void toggleLED() {
  ledState = !ledState;
  digitalWrite(LED_WIFI, ledState ? LOW : HIGH);
  Serial.println(ledState);
}

void saveCustomParameters() {
  File configFile = SPIFFS.open("/configTCP.txt", "w");
  if (!configFile) {
    Serial.println("Failed to open config file for writing");
    return;
  }
  serverIPString = custom_tcp_server.getValue();
  serverPortString = custom_tcp_port.getValue();
  configFile.println(serverIPString);
  configFile.println(serverPortString);
  configFile.close();
}

void retrieveCustomParameters() {
  File configFile = SPIFFS.open("/configTCP.txt", "r");
  if (!configFile) {
    Serial.println("Failed to open config file for reading");
    return;
  }
  serverIPString = configFile.readStringUntil('\n');
  serverPortString = configFile.readStringUntil('\n');
  Serial.println(serverIPString);
  Serial.println(serverPortString);
  configFile.close();
}

void setup() {
  pinMode(BUTTON_RESET, INPUT_PULLUP);
  pinMode(LED_WIFI, OUTPUT);
  pinMode(LED_SERIAL, OUTPUT);
  digitalWrite(LED_WIFI, HIGH);
  digitalWrite(LED_SERIAL, HIGH);
  Serial.begin(115200);
  Serial.println("Starting...");
  esp32Serial.begin(19200);
  
  if (!SPIFFS.begin()) {
    Serial.println("Failed to mount file system");
    return;
  }

  button.attach(BUTTON_PIN, INPUT_PULLUP);
  button.interval(50);
  wifiManager.addParameter(&custom_tcp_server);
  wifiManager.addParameter(&custom_tcp_port);
  std::vector<const char *> wm_menu  = {"wifi"};
  wifiManager.setShowInfoUpdate(false);
  wifiManager.setShowInfoErase(false);
  wifiManager.setMenu(wm_menu);
  wifiManager.setConnectTimeout(10);
  sprintf(ssidName, "%s%08X", ssid, ESP.getChipId());
  ticker.attach(500 / 1000.0, toggleLED);
  wifiManager.autoConnect(ssidName, "HueMixLink");
  serverIPString = custom_tcp_server.getValue();
  if (!serverIPString.isEmpty()) {
    Serial.println("saving custom param");
    saveCustomParameters();
  } else {
    Serial.println("retrieving custom param");
    retrieveCustomParameters();
  }
  ticker.detach();
  digitalWrite(LED_WIFI, HIGH);
  serverIP = serverIPString.c_str();
  serverPort = serverPortString.toInt();
  Serial.println(serverIP);
  Serial.println(serverPort);

  startHandshakeSerial();
  lastButtonState = digitalRead(BUTTON_RESET);
}

void startHandshakeSerial() {
  esp32Serial.print("start:macAddress");
  esp32Serial.println();
  Serial.println("Sent handshake");
  while(!esp32Serial.available()){
    delay(10);
  }
  if (esp32Serial.available()) {
    macAddressString = esp32Serial.readStringUntil('\n');
    macAddressString.trim();
    if (macAddressString == "handshake"){
      while(!esp32Serial.available()){
        delay(10);
      }
      if (esp32Serial.available()) {
        macAddressString = esp32Serial.readStringUntil('\n');
        macAddressString.trim();
      }
    }
    Serial.println(macAddressString);
  }
  getServerMacAddresses();
}

void getServerMacAddresses() {
  String recievedData = "None";
  String prefix = "svMacs,";
  String suffix = ",-p";
  String combinedString = prefix + macAddressString + suffix;
  if (client.connect(serverIP, serverPort)) {
    client.print(combinedString);
    client.flush();
    while(!client.available()){
      delay(10);
    }
    if (client.available()) {
      recievedData = client.readStringUntil('\n');
      macAddresses = recievedData;
      Serial.println("Server response: " + recievedData);
      macAddressesSave = true;
    }
    client.stop();
  }
  esp32Serial.println(recievedData);
}

void buttonReset() {
  buttonState = digitalRead(BUTTON_RESET);
  if (buttonState != lastButtonState) {
    if (buttonState == LOW) {
      Serial.println("Button Pressed");
      buttonPressTime = millis();
    } else {
      Serial.println("Button Released");
      if (millis() - buttonPressTime > 5000) {
        wifiManager.resetSettings();
        ESP.restart();
      } else {
        getServerMacAddresses();
      }
    }
    lastButtonState = buttonState;
  }
}

void sendData(const char* message) {
  String macAddress = WiFi.macAddress();
  macAddress.toLowerCase();
  const size_t combinedSize = macAddress.length() + macAddressString.length() + strlen(message) + 2;
  char combinedString[combinedSize];
  strcpy(combinedString, macAddress.c_str());
  strcat(combinedString, ",");
  strcat(combinedString, message);
  strcat(combinedString, ",");
  strcat(combinedString, macAddressString.c_str());
  if (!client.connected()) {
    if (client.connect(serverIP, serverPort)) {
      digitalWrite(LED_SERIAL, LOW);
      Serial.println("Connected to server");
      client.print(combinedString);
      client.flush();
      client.stop();
    }
  } 
}

void loop() {
  digitalWrite(LED_SERIAL, HIGH);
  buttonReset();
  while (WiFi.status() != WL_CONNECTED) {
    buttonReset();
    digitalWrite(LED_WIFI, HIGH);
    wifiManager.setConnectTimeout(180);
    wifiManager.setConnectRetries(100);
    if (wifiManager.getWiFiIsSaved()) wifiManager.setEnableConfigPortal(false);
    wifiManager.autoConnect(ssidName, "HueMixLink");
    delay(100);
  }
  digitalWrite(LED_WIFI, LOW);
  if (esp32Serial.available()) {
    String data = esp32Serial.readStringUntil('\n');
    data.trim();
    if (data == "handshake") {
      startHandshakeSerial();
    } else {
      if (!client.connected()) {
        if (client.connect(serverIP, serverPort)) {
          digitalWrite(LED_SERIAL, LOW);
          Serial.println("Connected to server");
          client.print(data);
          client.flush();
          client.stop();
        }
      } 
    }
  }
  button.update();
  if (button.fell()) {
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
}