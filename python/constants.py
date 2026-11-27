"""
Constants and configuration values for HueMixLink.

Centralizes all magic numbers, packet types, limits, and default values.
"""

# ===== Network Configuration =====
DEFAULT_UDP_IP = "0.0.0.0"
DEFAULT_UDP_PORT = 7777
DEFAULT_GATEWAY_PORT = 4210
DEFAULT_WEB_PORT = 5001

# ===== Packet Types =====
PKT_PAIR_CONFIRM = 0x01
PKT_LIGHT_RAW = 0x02
PKT_SYS_CMD = 0x04
PKT_GW_LIST_UPD = 0x05
PKT_PAIR_ACK = 0x06
PKT_KEY_DISTRIB = 0x07
PKT_HELLO = 0x10
PKT_BTN_EVENT = 0x11
PKT_SCENE_REQ = 0x12
PKT_DELIVERY_RPT = 0x13
PKT_MOTION_EVENT = 0x14
PKT_DOOR_EVENT = 0x15
PKT_OTA_NOTIFY = 0x20
PKT_OTA_READY = 0x21
PKT_OTA_CHUNK = 0x22
PKT_OTA_COMPLETE = 0x23
PKT_OTA_ABORT = 0x24
PKT_OTA_CHUNK_ACK = 0x25
PKT_OTA_CHECKPOINT_REQ = 0x26
PKT_PING_DEVICE = 0xFE
PKT_PING = 0xFF

# ===== Device Types =====
DEV_GATEWAY = 1
DEV_BUTTON = 2
DEV_LIGHT = 3
DEV_REMOTE = 4
DEV_MOTION = 5
DEV_DOOR = 6
DEV_PLUGIN_MARKER = 0xFE  # Unpaired plugin device (sends with plugin UUID in HELLO)

# ===== Button Action Codes =====
ACT_CLICK = 1
ACT_HOLDING = 2
ACT_RELEASE = 3
ACT_MOTION_DETECTED = 10
ACT_DOOR_OPENED = 11
ACT_DOOR_CLOSED = 12
ACT_SYNC = 9

# ===== Remote Button Action Types =====
REMOTE_ACTION_NORMAL = 'normal'                    # Hold = brightness, Click = scene cycle
REMOTE_ACTION_TOGGLE = 'toggle'                    # Hold = None, Click = toggle on/off
REMOTE_ACTION_BRIGHTNESS_UP = 'brightness_up'      # Hold = Increase brightness, Click = Increase brightness
REMOTE_ACTION_BRIGHTNESS_DOWN = 'brightness_down'  # Hold = Decrease brightness, Click = Decrease brightness
REMOTE_ACTION_SCENE_CYCLE = 'scene_cycle'          # Click = cycle scenes only (never turn off)
REMOTE_ACTION_ALL_OFF = 'all_off'                  # Click = turn off all rooms

# ===== System Commands =====
CMD_NIGHT_MODE_ON = 1   # LED OFF
CMD_NIGHT_MODE_OFF = 2  # LED ON
CMD_NETNODE_WIFI_STATUS = 3  # Net node connectivity status (payload[1]: 0=offline, 1=online)
CMD_SET_MOTION_COOLDOWN = 0x40  # Set motion sensor cooldown period (persistent)
CMD_SET_MOTION_SLEEP = 0x41  # Set motion sensor one-time sleep duration (not saved)
CMD_SET_LED_COUNT = 0x50
CMD_UPGRADE_TO_V2 = 0x60  # Trigger in-place v1 -> v2 TOFU handshake
CMD_SET_PIN = 0x61        # Provision 6-digit PIN over encrypted v2 channel (write-once)
CMD_SET_PIN_ACK = 0x62    # Device -> Backend encrypted v2 ACK for CMD_SET_PIN

PIN_ACK_OK = 0x01
PIN_ACK_ALREADY_SET = 0x02
PIN_ACK_INVALID = 0x03

# ===== Network Limits =====
MAX_GATEWAY_ATTEMPTS = 5
MAX_GATEWAYS_PER_PACKET = 10
MAX_LEDS_PER_PACKET = 60
GATEWAY_DELIVERY_TIMEOUT_SECONDS = 5

# ===== Timeout Values =====
TIMEOUT_SOCKET = 1.0
TIMEOUT_DELIVERY = 1.0
TIMEOUT_PING = 2.0
TIMEOUT_PING_SINGLE = 2.0
TIMEOUT_HTTP_REQUEST = 2.0
TIMEOUT_SCENE_CYCLE = 2.0

# ===== Retry Configuration =====
HTTP_MAX_RETRIES = 2
HTTP_RETRY_BACKOFF_BASE = 0.1  # Base delay in seconds (0.1, 0.2, 0.4...)

# ===== RSSI and Signal =====
RSSI_AUTO_PAIR_THRESHOLD = -50  # dBm
RSSI_MIN = -90  # Weakest signal in dBm
RSSI_MAX = -40  # Strongest signal in dBm

# ===== Battery Monitoring =====
BATTERY_WARNING_THRESHOLD = 25  # Percent at or below which a battery is flagged for charging
BATTERY_CRITICAL_THRESHOLD = 10  # Percent at or below which a battery is critical

# ===== LED Configuration Limits =====
MIN_LEDS = 1
MAX_LEDS = 60
DEFAULT_BRIGHTNESS = 255

# ===== Packet Sizes (v1 frozen) =====
PACKET_HEADER_SIZE = 18
PACKET_PAYLOAD_SIZE = 185
PACKET_TOTAL_SIZE = 203

# ===== Security v2 Protocol Constants =====
PROTO_VERSION_V1 = 0x01
PROTO_VERSION_V2 = 0x02

V2_HEADER_SIZE = 22
V2_AAD_SIZE = 21          # Header bytes 0..20 (excludes rssi at offset 21)
V2_TAG_SIZE = 16
V2_NONCE_SIZE = 12        # srcMAC(6) || counter(4) || key_epoch(2)
V2_OVERHEAD = V2_HEADER_SIZE + V2_TAG_SIZE  # 38 bytes
V2_MIN_FRAME_SIZE = V2_OVERHEAD + 1         # 39 bytes (N >= 1)
V2_FORBIDDEN_LEN = PACKET_TOTAL_SIZE        # 203 bytes reserved for v1
V2_MAX_FRAME_ESP8266 = 250
V2_MAX_FRAME_ESP32 = 1470
V2_PAIR_CONFIRM_LEN = 151  # 22 hdr + 32 pub_B + 8 nonce_B + 4 homeID + 1 devID + 2 epoch + 32 proof_B + 50 wrap
V2_UDP_BUFFER_SIZE = 2048

V2_COUNTER_BLOCK_SIZE_BACKEND = 1024
V2_COUNTER_RESERVE_DEVICE = 100

V2_HELLO_FLAG_HAS_PIN = 0x01
V2_HELLO_CAP_LEN = 44  # proto(1) + max_frame(2) + flags(1) + pub_D(32) + nonce_D(8)
V2_HELLO_CAP_OFFSETS = {
    DEV_GATEWAY: 15,
    DEV_BUTTON: 6,
    DEV_LIGHT: 11,
    DEV_REMOTE: 7,
    DEV_MOTION: 6,
    DEV_DOOR: 6,
    DEV_PLUGIN_MARKER: 23,
}

CRYPTO_MODE_V1 = 'v1'
CRYPTO_MODE_V2_UNVERIFIED = 'v2-unverified'
CRYPTO_MODE_V2 = 'v2'

# ===== OTA Configuration =====
OTA_CHUNK_DATA_SIZE = 182  # Bytes of actual firmware data per chunk
OTA_READY_TIMEOUT = 10  # Seconds to wait for PKT_OTA_READY response
OTA_CHUNK_ACK_TIMEOUT = 3  # Seconds to wait for checkpoint ACK
OTA_CHUNK_MAX_RETRIES = 2  # Max retries for a chunk batch before aborting
OTA_CHECKPOINT_INTERVAL = 10  # Send checkpoint ACK every N chunks
OTA_POST_UPDATE_TIMEOUT = 30  # Seconds to wait for post-update HELLO
OTA_MIN_BATTERY_PERCENT = 20  # Skip wake-triggered auto-OTA below this battery level
OTA_MAX_FIRMWARE_SIZE = 2 * 1024 * 1024  # 2MB max firmware size

# ===== FNV-1a Hash =====
FNV_OFFSET_BASIS = 2166136261
FNV_PRIME = 16777619
FNV_MASK = 0xFFFFFFFF

# ===== File Names =====
FILE_GATEWAYS = 'gateways.json'
FILE_MOTION_SENSORS = 'motion_sensors.json'
FILE_BUTTONS = 'buttons.json'
FILE_LIGHTSTRIPS = 'lightstrips.json'
FILE_DOOR_SENSORS = 'door_sensors.json'
FILE_BRIDGE = 'bridge.json'
FILE_CONFIG = 'config.json'
FILE_PAIRING_HISTORY = 'pairing_history.json'
FILE_OTA_SESSIONS = 'ota_sessions.json'
FILE_PLUGINS = 'plugins.json'
FILE_PLUGIN_DEVICE_BINDINGS = 'plugin_device_bindings.json'
FILE_SECURITY = 'security.json'
FILE_SECURITY_COUNTERS = 'security_counters.db'

# ===== Logging =====
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

#==== GitHub Repository Info =====
GITHUB_REPO = "HueMix-Link"
GITHUB_OWNER = "mathimix7"