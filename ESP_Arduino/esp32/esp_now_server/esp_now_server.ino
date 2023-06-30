#include <esp_now.h>
#include <WiFi.h>

#define LED_PIN 19

volatile bool sendDataFlag = false;
bool macAddressesSave = false;
const uint8_t* sendDataMac;
String recievedData;
String macAddresses;
esp_now_peer_info_t peerInfo;

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  Serial.begin(115200);
  delay(100);

  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

  if (esp_now_init() != 0) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  esp_now_register_recv_cb(onDataReceived);
  esp_now_register_send_cb(onDataSent);
  
  Serial2.begin(19200);
  Serial2.println("handshake");
  Serial.println("waiting for handshake...");
  while(!Serial2.available()){
    delay(10);
  }
  if (Serial2.available()) {
    String message = Serial2.readStringUntil('\n');
    message.trim();
    Serial.println("recieved");
    Serial.println(message);
    if (message == "start:macAddress"){
      startHandshakeSerial();
    }
  }

  digitalWrite(LED_PIN, LOW);
}

void startHandshakeSerial() {
  Serial.println("sending...");
  String macAddress = WiFi.macAddress();
  Serial2.print(macAddress);
  Serial2.println();

  while(!Serial2.available()){
    delay(10);
  }

  if (Serial2.available()) {
    String message = Serial2.readStringUntil('\n');
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
  if (Serial2.available()) {
    String message = Serial2.readStringUntil('\n');
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
  if (macAddressesSave == true) {
    data = macAddresses;
  } else {
    data = "cancel";
  }

  memcpy(peerInfo.peer_addr, sendDataMac, 6);
  peerInfo.channel = 0;
  peerInfo.encrypt = false;
  esp_now_add_peer(&peerInfo);
  Serial.println(data.c_str());
  esp_err_t result = esp_now_send(peerInfo.peer_addr, (const uint8_t*)data.c_str(), data.length() + 1);

  if (result == ESP_OK) {
    Serial.println("Data sent successfully");
  } else {
    Serial.print("Failed to send data. Error code: ");
    Serial.println(result);
  }
}

void onDataSent(const uint8_t* mac, esp_now_send_status_t  sendStatus) {
  if (sendStatus == ESP_OK) {
    Serial.println("Succesfully sent!");
  }else {
    Serial.print("Error sending message!");
    Serial.println(sendStatus);
  }
}

void onDataReceived(const uint8_t* mac, const uint8_t* data, int len) {
  sendDataMac = mac;
  digitalWrite(LED_PIN, HIGH);
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
  if (!recievedData.endsWith(macAddress)) {
    recievedData = recievedData + "," + macAddress;
  }
  Serial.println(recievedData);
  Serial2.print(recievedData);
  Serial2.println();
  digitalWrite(LED_PIN, LOW);
  if (rawRecievedData.endsWith("HoldingStopped") || rawRecievedData.endsWith("Once")) {
    delay(500);
    sendRepeaterMacs();
  }
}