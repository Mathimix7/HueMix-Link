#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiClient.h>
#include <SPIFFS.h>
#include <esp_system.h>
#include <Ticker.h>
#include <Bounce2.h>
#include <TimeLib.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <ctime>

#define LED_WIFI 18
#define LED_SERIAL 19
#define BUTTON_PIN 12
#define BUTTON_RESET 13

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
bool ledState = true;
int lastButtonPress = 0;
bool buttonPressed = false;
bool buttonHeld = false;
unsigned long buttonHoldStartTime = 0;
unsigned long holdingInterval = 500;
unsigned long holdingIntervalUpdate = 0;
unsigned long holdingThreshold = 1000;
String onTime;
String offTime;

Ticker ticker;
WiFiClient client;
WiFiManager wifiManager;
Bounce button;

WiFiManagerParameter custom_tcp_server("server", "TCP Server IP", "", 40);
WiFiManagerParameter custom_tcp_port("port", "TCP Server Port", "7777", 6);
WiFiManagerParameter custom_time_off("time", "LED off time", "22", 2);
WiFiManagerParameter custom_time_on("time", "LED on time", "8", 2);

void toggleLED() {
  ledState = !ledState;
  digitalWrite(LED_WIFI, ledState ? HIGH : LOW);
}

void saveCustomParameters() {
  File configFile = SPIFFS.open("/configTCP.txt", "w");
  if (!configFile) {
    Serial.println("Failed to open config file for writing");
    return;
  }
  serverIPString = custom_tcp_server.getValue();
  serverPortString = custom_tcp_port.getValue();
  onTime = custom_time_off.getValue();
  offTime = custom_time_on.getValue();
  configFile.println(serverIPString);
  configFile.println(serverPortString);
  configFile.println(onTime);
  configFile.println(offTime);
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
  onTime = configFile.readStringUntil('\n');
  offTime = configFile.readStringUntil('\n');
  Serial.println(serverIPString);
  Serial.println(serverPortString);
  configFile.close();
}

void setup() {
  pinMode(BUTTON_RESET, INPUT_PULLUP);
  pinMode(LED_WIFI, OUTPUT);
  pinMode(LED_SERIAL, OUTPUT);
  digitalWrite(LED_WIFI, LOW);
  digitalWrite(LED_SERIAL, LOW);
  Serial.begin(115200);
  Serial.println("Starting...");
  Serial2.begin(19200);
  
  if (!SPIFFS.begin(true)) {
    Serial.println("Failed to mount file system");
    return;
  }

  button.attach(BUTTON_PIN, INPUT_PULLUP);
  button.interval(50);
  wifiManager.addParameter(&custom_tcp_server);
  wifiManager.addParameter(&custom_tcp_port);
  wifiManager.addParameter(&custom_time_off);
  wifiManager.addParameter(&custom_time_on);
  std::vector<const char *> wm_menu  = {"wifi"};
  wifiManager.setShowInfoUpdate(false);
  wifiManager.setShowInfoErase(false);
  wifiManager.setMenu(wm_menu);
  wifiManager.setConnectTimeout(10);
  sprintf(ssidName, "%s%08X", ssid, ESP.getEfuseMac());
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

  HTTPClient http;
  http.begin("http://worldtimeapi.org/api/timezone/America/New_York");
  int httpResponseCode = http.GET();
    if (httpResponseCode == HTTP_CODE_OK) {
      String payload = http.getString();
      DynamicJsonDocument doc(1024);
      deserializeJson(doc, payload);
      const char* estTime = doc["datetime"];
      Serial.println(estTime);
      int year, month, day, hour, minute, second;
      sscanf(estTime, "%d-%d-%dT%d:%d:%d", &year, &month, &day, &hour, &minute, &second);
      setTime(hour, minute, second, day, month, year);
      Serial.println("Using api to fetch time");
    }
  http.end();
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
  Serial2.print("start:macAddress");
  Serial2.println();
  Serial.println("Sent handshake");
  while(!Serial2.available()){
    delay(10);
  }
  if (Serial2.available()) {
    macAddressString = Serial2.readStringUntil('\n');
    macAddressString.trim();
    if (macAddressString == "handshake"){
      while(!Serial2.available()){
        delay(10);
      }
      if (Serial2.available()) {
        macAddressString = Serial2.readStringUntil('\n');
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
  Serial2.println(recievedData);
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
        esp_restart();
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
      digitalWrite(LED_SERIAL, HIGH);
      Serial.println("Connected to server");
      client.print(combinedString);
      client.flush();
      client.stop();
    }
  } 
}

bool led_off_time() {
  time_t currentTime = now();
  struct tm *timeinfo;
  timeinfo = localtime(&currentTime);
  if (timeinfo) {
    int currentHour = timeinfo->tm_hour;
    if (currentHour >= offTime.toInt() || currentHour < onTime.toInt()) {
      return false;
    } else {
      return true;
    }
  } else {
    return true;
  }
}

void loop() {
  digitalWrite(LED_SERIAL, LOW);
  buttonReset();
  bool leds_status = led_off_time();
  while (WiFi.status() != WL_CONNECTED) {
    buttonReset();
    digitalWrite(LED_WIFI, LOW);
    wifiManager.setConnectTimeout(180);
    wifiManager.setConnectRetries(100);
    if (wifiManager.getWiFiIsSaved()) wifiManager.setEnableConfigPortal(false);
    wifiManager.autoConnect(ssidName, "HueMixLink");
    sleep(100);
  }
  if (leds_status) {
    digitalWrite(LED_WIFI, HIGH);
  } else {
     digitalWrite(LED_WIFI, LOW);
  }
  if (Serial2.available()) {
    String data = Serial2.readStringUntil('\n');
    data.trim();
    if (data == "handshake") {
      startHandshakeSerial();
    } else {
      if (!client.connected()) {
        if (client.connect(serverIP, serverPort)) {
          digitalWrite(LED_SERIAL, HIGH);
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