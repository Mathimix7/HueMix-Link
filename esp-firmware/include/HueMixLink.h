#ifndef HUEMIXLINK_H
#define HUEMIXLINK_H

#include <Arduino.h>

// --- CONFIGURATION ---
#define HUEMIXLINK_CHANNEL 1       
#define MAX_GATEWAYS       10      
#define SERIAL_START       0xFE    
#define SERIAL_END         0xFD
#define SERIAL_HANDSHAKE   0x11    
#define SERIAL_REQ_HANDSHAKE 0x12

// --- LED TIMING ---
#define LED_ON_DURATION        150   // LED stays on for 150ms per event
#define LED_BLINK_OFF_DURATION 75    // Brief off-time between events

// --- PACKET TYPES ---
enum PacketType {
  PKT_PAIR_CONFIRM = 0x01, 
  PKT_LIGHT_RAW    = 0x02, 
  PKT_SCENE_DATA   = 0x03, 
  PKT_SYS_CMD      = 0x04, 
  PKT_GW_LIST_UPD  = 0x05, 

  PKT_HELLO        = 0x10, 
  PKT_BTN_EVENT    = 0x11, 
  PKT_SCENE_REQ    = 0x12, 
  PKT_DELIVERY_RPT = 0x13,
  PKT_MOTION_EVENT = 0x14, 
  PKT_DOOR_EVENT   = 0x15,

  PKT_OTA_NOTIFY   = 0x20,
  PKT_OTA_READY    = 0x21,
  PKT_OTA_CHUNK    = 0x22,
  PKT_OTA_COMPLETE = 0x23,
  PKT_OTA_ABORT    = 0x24,
  PKT_OTA_CHUNK_ACK = 0x25,
  PKT_OTA_CHECKPOINT_REQ = 0x26,

  PKT_ACK_TO_BTN   = 0xAA, 
  PKT_PING         = 0xFF,
  PKT_PING_DEVICE  = 0xFE
};

// Packet type range reserved for plugin-owned device event packets.
#define PLUGIN_EVENT_PACKET_MIN 0x50
#define PLUGIN_EVENT_PACKET_MAX 0x7F

// --- DEVICE TYPES ---
#define DEV_GATEWAY 1
#define DEV_BUTTON  2
#define DEV_LIGHT   3
#define DEV_REMOTE  4
#define DEV_MOTION  5
#define DEV_DOOR    6

// --- ACTION CODES ---
#define ACT_CLICK        1
#define ACT_HOLDING      2 
#define ACT_RELEASE      3 
#define ACT_SYNC         9
#define ACT_MOTION_DETECTED  10
#define ACT_DOOR_OPENED      11
#define ACT_DOOR_CLOSED      12

// --- PLATFORM/BATTERY FLAGS (packed in payload platform byte) ---
#define PLATFORM_FLAG_ESP8266         0x01
#define PLATFORM_FLAG_BATTERY_CR123A  0x80

// --- STRUCTURES ---
#pragma pack(push, 1) 

typedef struct {
  uint8_t magic;   // 0x11
  uint8_t mac[6];  // The Radio Node's ESP-NOW MAC
  uint8_t version_major;
  uint8_t version_minor;
  uint8_t version_patch;
} SerialHandshake;

typedef struct {
  uint8_t count;
  uint8_t brightness;
  uint8_t data[180]; 
} Payload_Light;

typedef struct {
  uint8_t action;
  uint16_t battery_mv;
  int8_t button_index;  // -1 for normal button, actual index for remote
  uint8_t version_major;
  uint8_t version_minor;
  uint8_t version_patch;
  uint8_t platform;  // 0=ESP32, 1=ESP8266
  uint8_t button_count;  // Number of buttons configured (1-4)
} Payload_Button;

typedef struct {
  uint8_t action;  // ACT_MOTION_DETECTED
  uint16_t battery_mv;
  uint8_t light_level;  // LDR sensor reading (0-10)
  uint8_t version_major;
  uint8_t version_minor;
  uint8_t version_patch;
  uint8_t platform;  // 0=ESP32, 1=ESP8266
} Payload_Motion;

typedef struct {
  uint8_t action;  // ACT_DOOR_OPENED / ACT_DOOR_CLOSED
  uint16_t battery_mv;
  uint8_t light_level;
  uint8_t version_major;
  uint8_t version_minor;
  uint8_t version_patch;
  uint8_t platform;  // 0=ESP32, 1=ESP8266
} Payload_Door;

typedef struct {
  uint8_t count;
  uint8_t macs[MAX_GATEWAYS][6];
} Payload_GatewayList;

typedef struct {
  uint8_t originalMsgID; 
  bool    success;
  uint8_t targetMAC[6];
} Payload_Report;

typedef struct {
  uint8_t cmd;
  uint8_t value; 
} Payload_SysCmd;

typedef struct {
  uint32_t newHomeID;
  uint8_t  assignedDeviceID;
} Payload_Pairing;

typedef struct {
  uint32_t firmware_size;      // Total firmware size in bytes
  uint8_t  sha256_hash[32];    // SHA256 hash of firmware
  uint8_t  version[4];         // Version bytes (major.minor.patch.build)
} Payload_OtaNotify;

typedef struct {
  uint16_t chunk_index;        // Chunk sequence number
  uint8_t  data_len;           // Actual data length in this chunk (≤182)
  uint8_t  data[182];          // Chunk data
} Payload_OtaChunk;

typedef struct {
  uint32_t firmware_size;      // Echo firmware size from notify
  uint16_t battery_mv;         // Current battery voltage (buttons only)
} Payload_OtaReady;

typedef struct {
  uint16_t last_chunk_index;   // Last successfully received chunk index
} Payload_OtaChunkAck;

// --- MASTER PACKET ---
typedef struct {
  uint8_t  type;           
  uint32_t signature;      
  uint8_t  sourceMAC[6];
  uint8_t  targetMAC[6];
  uint8_t  msgID;          

  union {
    Payload_Light       light;
    Payload_Button      btn;
    Payload_Motion      motion;
    Payload_Door        door;
    Payload_GatewayList gwList;
    Payload_Report      report;
    Payload_SysCmd      sys;
    Payload_Pairing     pair;
    Payload_OtaNotify   otaNotify;
    Payload_OtaChunk    otaChunk;
    Payload_OtaReady    otaReady;
    Payload_OtaChunkAck otaChunkAck;
    uint8_t             raw[185];
  } payload;
  
} HueMixLinkPacket;

#pragma pack(pop) 

// --- SECURITY HELPER ---
uint32_t calculateHash(uint8_t* data, size_t len, uint32_t homeID) {
  uint32_t hash = 2166136261u;
  
  // Mix 4 bytes of HomeID (Little Endian)
  hash ^= (homeID & 0xFF);         hash *= 16777619;
  hash ^= ((homeID >> 8) & 0xFF);  hash *= 16777619;
  hash ^= ((homeID >> 16) & 0xFF); hash *= 16777619;
  hash ^= ((homeID >> 24) & 0xFF); hash *= 16777619;

  // Mix Data Payload
  for(size_t i=0; i<len; i++) {
    hash ^= data[i];
    hash *= 16777619;
  }
  return hash;
}

#endif
