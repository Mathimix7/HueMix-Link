#include <espnow.h>
#include <ESP8266WiFi.h>
#include <SoftwareSerial.h>

#define LED_PIN 2

volatile bool sendDataFlag = false;
bool macAddressesSave = false;
uint8_t* sendDataMac;
String recievedData;
String macAddresses;

#define ESP32_UART_TX_PIN 13  // Pin D7 of ESP8266
#define ESP32_UART_RX_PIN 12  // Pin D6 of ESP8266

SoftwareSerial esp32Serial(ESP32_UART_RX_PIN, ESP32_UART_TX_PIN);

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  Serial.begin(115200);
  delay(100);

  WiFi.mode(WIFI_STA);
  WiFi.setOutputPower(20.5);

  if (esp_now_init() != 0) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  
  esp32Serial.begin(19200);
  esp32Serial.println("handshake");
  Serial.println("waiting for handshake...");
  while(!esp32Serial.available()){
    delay(10);
  }
  if (esp32Serial.available()) {
    String message = esp32Serial.readStringUntil('\n');
    message.trim();
    Serial.println("recieved");
    Serial.println(message);
    if (message == "start:macAddress"){
      startHandshakeSerial();
    }
  }

  esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
  esp_now_register_recv_cb(onDataReceived);
  esp_now_register_send_cb(onDataSent);
  digitalWrite(LED_PIN, HIGH);
}

void startHandshakeSerial() {
  Serial.println("sending...");
  String macAddress = WiFi.macAddress();
  esp32Serial.print(macAddress);
  esp32Serial.println();

  while(!esp32Serial.available()){
    delay(10);
  }

  if (esp32Serial.available()) {
    String message = esp32Serial.readStringUntil('\n');
    message.trim();
    Serial.println("recieved");
    Serial.println(message);
    if (message != "None"){
      macAddresses = message;
      macAddressesSave = true;
    }
  }
}


void loop() {
  if (esp32Serial.available()) {
    String message = esp32Serial.readStringUntil('\n');
    message.trim();
    Serial.println("recieved");
    Serial.println(message);
    if (message == "start:macAddress"){
      startHandshakeSerial();
    } else {
      if (message != "None"){
        macAddresses = message;
        macAddressesSave = true;
      }
    }
  }
}

void sendRepeaterMacs() {
  String data;
  if (macAddressesSave == true){
    data = macAddresses;
  }else{
    data = "cancel";
  }
  esp_now_add_peer((uint8_t*)sendDataMac, ESP_NOW_ROLE_COMBO, 1, NULL, 0);
  u8 buffer[data.length()];
  for (size_t i = 0; i < data.length(); i++) {
    buffer[i] = static_cast<u8>(data.charAt(i));
  }
  int result = esp_now_send(sendDataMac, buffer, data.length());
  
  if (result == 0) {
    Serial.println("Data sent successfully");
  } else {
    Serial.print("Failed to send data. Error code: ");
    Serial.println(result);
  }
}

void onDataSent(uint8_t* mac, uint8_t sendStatus) {
  if (sendStatus == 0) {
    Serial.println("Succesfully sent!");
  }else {
    Serial.print("Error sending message!");
    Serial.println(sendStatus);
  }
}

void onDataReceived(uint8_t* mac, uint8_t* data, uint8_t len) {
  sendDataMac = mac;
  digitalWrite(LED_PIN, LOW);
  data[len] = '\0';
  recievedData = (char*)data;
  if (recievedData.startsWith("HML:LFB")) {
    char *delimiter = ";";
    char *second_part;
    second_part = strstr((char*)data, delimiter);
    if (second_part != NULL) {
      second_part += strlen(delimiter);
      while (*second_part == ' ') {
          second_part++;
      }
    }
    recievedData = (char*)second_part;
  }
  String rawRecievedData = recievedData;
  String macAddress = WiFi.macAddress();
  recievedData = recievedData + "," +macAddress;
  Serial.println(recievedData);
  esp32Serial.print(recievedData);
  esp32Serial.println();
  digitalWrite(LED_PIN, HIGH);
  if (rawRecievedData.endsWith("HoldingStopped") || rawRecievedData.endsWith("Once")) {
    delay(500);
    sendRepeaterMacs();
  }
}