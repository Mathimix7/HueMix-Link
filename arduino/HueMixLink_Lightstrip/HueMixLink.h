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

  PKT_ACK_TO_BTN   = 0xAA, 
  PKT_PING         = 0xFF  
};

// --- ACTION CODES ---
#define ACT_CLICK        1
#define ACT_HOLDING      2 
#define ACT_RELEASE      3 
#define ACT_SYNC         9

// --- STRUCTURES ---
#pragma pack(push, 1) 

typedef struct {
  uint8_t magic;   // 0x11
  uint8_t mac[6];  // The Radio Node's ESP-NOW MAC
} SerialHandshake;

typedef struct {
  uint8_t count;
  uint8_t brightness;
  uint8_t data[180]; 
} Payload_Light;

typedef struct {
  uint8_t action;
  uint16_t battery_mv;
} Payload_Button;

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
    Payload_GatewayList gwList;
    Payload_Report      report;
    Payload_SysCmd      sys;
    Payload_Pairing     pair;
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