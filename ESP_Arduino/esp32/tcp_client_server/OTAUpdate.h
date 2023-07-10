#include <WiFi.h>
#include <Update.h>
#include <WebServer.h>
#include <ArduinoJson.h>

#define LED_WIFI 18
#define LED_SERIAL 19

WebServer server(80);

void handleStatus() {
  StaticJsonDocument<200> jsonDoc;
  jsonDoc["uptime"] = millis() / 1000;
  jsonDoc["id"] = ESP.getEfuseMac();
  
  File configFile = SPIFFS.open("/configTCP.txt", "r");
  if (!configFile) {
    Serial.println("Failed to open config file for reading");
    return;
  }
  String serverValue = configFile.readStringUntil('\n');
  String portValue = configFile.readStringUntil('\n');
  String ledOnTimeValue = configFile.readStringUntil('\n');
  String ledOffTimeValue = configFile.readStringUntil('\n');
  serverValue.trim();
  portValue.trim();
  ledOnTimeValue.trim();
  ledOffTimeValue.trim();
  configFile.close();
  jsonDoc["server"] = serverValue;
  jsonDoc["port"] = portValue;
  jsonDoc["led_on_time"] = ledOnTimeValue;
  jsonDoc["led_off_time"] = ledOffTimeValue;
  String responseJson;
  serializeJson(jsonDoc, responseJson);
  server.send(200, "application/json", responseJson);
}

void handleReset() {
  server.send(200, "text/plain", "OK");
  delay(500);
  ESP.restart();
}

void handleLedTimes() {
  if (server.hasArg("led_on_time") && server.hasArg("led_off_time")) {
    File configFile = SPIFFS.open("/configTCP.txt", "r");
    if (!configFile) {
      Serial.println("Failed to open config file for reading");
      return;
    }
    String serverIPString = configFile.readStringUntil('\n');
    String serverPortString = configFile.readStringUntil('\n');
    String ledOnTimeValue = configFile.readStringUntil('\n');
    String ledOffTimeValue = configFile.readStringUntil('\n');
    serverIPString.trim();
    serverPortString.trim();
    ledOnTimeValue.trim();
    serverIPString.trim();
    configFile.close();
    File configFileWrite = SPIFFS.open("/configTCP.txt", "w");
    if (!configFileWrite) {
      Serial.println("Failed to open config file for writing");
      return;
    }
    String onTimeLED = server.arg("led_on_time");
    String offTimeLED = server.arg("led_off_time");
    configFileWrite.println(serverIPString);
    configFileWrite.println(serverPortString);
    configFileWrite.println(onTimeLED);
    configFileWrite.println(offTimeLED);
    configFileWrite.close();
    server.send(200, "text/plain", "OK");
    delay(500);
    ESP.restart();
  }
  server.send(200, "text/plain", "error");
}

void handleNewPort() {
  if (server.hasArg("port")) {
    File configFile = SPIFFS.open("/configTCP.txt", "r");
    if (!configFile) {
      Serial.println("Failed to open config file for reading");
      return;
    }
    String serverIPString = configFile.readStringUntil('\n');
    String serverPortString = configFile.readStringUntil('\n');
    String ledOnTimeValue = configFile.readStringUntil('\n');
    String ledOffTimeValue = configFile.readStringUntil('\n');
    serverIPString.trim();
    serverPortString.trim();
    ledOnTimeValue.trim();
    serverIPString.trim();
    configFile.close();
    File configFileWrite = SPIFFS.open("/configTCP.txt", "w");
    if (!configFileWrite) {
      Serial.println("Failed to open config file for writing");
      return;
    }
    String new_port = server.arg("port");
    configFileWrite.println(serverIPString);
    configFileWrite.println(new_port);
    configFileWrite.println(ledOnTimeValue);
    configFileWrite.println(ledOffTimeValue);
    configFileWrite.close();
    server.send(200, "text/plain", "OK");
    delay(500);
    ESP.restart();
  }
  server.send(200, "text/plain", "error");
}

void setupOTA(const char* hostname, const char*  password) {
  String hostnameString = String(hostname);
  hostnameString.replace(" ", "");
  server.enableCORS();
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/reset", HTTP_GET, handleReset);
  server.on("/led_off_times", HTTP_GET, handleLedTimes);
  server.on("/new_port", HTTP_GET, handleNewPort);
  server.on("/update", HTTP_POST, []() {
    server.send(200, "text/plain", (Update.hasError()) ? "FAIL" : "OK");
    delay(1000);
    ESP.restart();
  }, []() {
    HTTPUpload& upload = server.upload();
    if (upload.status == UPLOAD_FILE_START) {
      Serial.printf("Update: %s\n", upload.filename.c_str());
      digitalWrite(LED_WIFI, HIGH);
      digitalWrite(LED_SERIAL, HIGH);
      if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
        Update.printError(Serial);
      }
    } else if (upload.status == UPLOAD_FILE_WRITE) {
      if (millis() % 1000 < 500) {
      digitalWrite(LED_WIFI, HIGH);
      digitalWrite(LED_SERIAL, LOW);
      } else {
        digitalWrite(LED_WIFI, LOW);
        digitalWrite(LED_SERIAL, HIGH);
      }
      if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
        Update.printError(Serial);
      }
    } else if (upload.status == UPLOAD_FILE_END) {
      if (Update.end(true)) {
        Serial.printf("Update Success: %u\nRebooting...\n", upload.totalSize);
        digitalWrite(LED_SERIAL, LOW);
        digitalWrite(LED_WIFI, HIGH);
      } else {
        Update.printError(Serial);
      }
    }
  });
  server.begin();
}

void handleOTA() {
  server.handleClient();
}