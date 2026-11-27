#ifndef HUEMIXLINK_H
#define HUEMIXLINK_H

#include <Arduino.h>

// --- CONFIGURATION ---
#define HUEMIXLINK_CHANNEL 1       
#define MAX_GATEWAYS       10      
#define PAIR_WINDOW_MS     30000UL // Accept PKT_PAIR_CONFIRM only within 30s of unpaired HELLO
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
  PKT_PAIR_ACK     = 0x06,
  PKT_KEY_DISTRIB  = 0x07,

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

// --- SECURITY V2 PROTOCOL CONSTANTS & STRUCTURES ---
#define PROTO_VERSION_V1         0x01
#define PROTO_VERSION_V2         0x02
#define V2_HEADER_SIZE           22
#define V2_AAD_SIZE              21    // Header bytes 0..20 (excludes rssi)
#define V2_TAG_SIZE              16
#define V2_NONCE_SIZE            12    // srcMAC(6) || counter(4) || key_epoch(2)
#define V2_OVERHEAD              38    // 22 + 16
#define V2_MIN_FRAME_SIZE        39    // 38 + 1
#define V2_FORBIDDEN_LEN         203   // Reserved for v1
#define V2_MAX_FRAME_ESP8266     250
#define V2_MAX_FRAME_ESP32       1470
#define V2_PAIR_CONFIRM_LEN      151
#define V2_COUNTER_RESERVE       100
#define V2_HELLO_FLAG_HAS_PIN    0x01
#define V2_HELLO_CAP_LEN         44

// Security v2 OTA/Migration System Commands (PKT_SYS_CMD)
#ifndef CMD_FACTORY_RESET
#define CMD_FACTORY_RESET        0xFF
#endif
#define CMD_UPGRADE_TO_V2        0x60
#define CMD_SET_PIN              0x61
#define CMD_SET_PIN_ACK          0x62
#define PIN_ACK_OK               0x01
#define PIN_ACK_ALREADY_SET      0x02
#define PIN_ACK_INVALID          0x03

typedef struct {
  uint8_t  type;
  uint8_t  version;         // 0x02
  uint8_t  sourceMAC[6];
  uint8_t  targetMAC[6];
  uint8_t  msgID;
  uint32_t counter;         // Sender monotonic TX counter (LE)
  uint16_t key_epoch;       // Rekey generation (LE)
  uint8_t  rssi;            // Gateway-written RX RSSI (excluded from AAD)
} HueMixLinkHeaderV2;

typedef struct {
  uint16_t chunk_index;
  uint16_t data_len;        // Supports >255 B chunks on ESP-NOW v2
} Payload_OtaChunkV2Header;

typedef struct {
  uint16_t count;           // Supports >255 LEDs on ESP-NOW v2
  uint8_t  brightness;
} Payload_LightV2Header;

typedef struct {
  uint8_t  pub_B[32];
  uint8_t  nonce_B[8];
  uint32_t newHomeID;
  uint8_t  assignedDeviceID;
  uint16_t key_epoch;
  uint8_t  proof_B[32];
  uint8_t  wrap[50];        // AES-GCM(K_own[16] || K_ack[16] || epoch[2]) + 16 B tag
} Payload_PairConfirmV2;

#pragma pack(pop) 

// --- SECURITY HELPER ---
inline uint32_t calculateHash(uint8_t* data, size_t len, uint32_t homeID) {
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
