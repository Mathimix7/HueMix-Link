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
PKT_HELLO = 0x10
PKT_BTN_EVENT = 0x11
PKT_SCENE_REQ = 0x12
PKT_DELIVERY_RPT = 0x13
PKT_PING_DEVICE = 0xFE
PKT_PING = 0xFF

# ===== Device Types =====
DEV_GATEWAY = 1
DEV_BUTTON = 2
DEV_LIGHT = 3
DEV_REMOTE = 4

# ===== Button Action Codes =====
ACT_CLICK = 1
ACT_HOLDING = 2
ACT_RELEASE = 3
ACT_SYNC = 9

# ===== Remote Button Action Types =====
REMOTE_ACTION_NORMAL = 'normal'                    # Hold = brightness, Click = scene cycle
REMOTE_ACTION_TOGGLE = 'toggle'                    # Hold = None, Click = toggle on/off
REMOTE_ACTION_BRIGHTNESS_UP = 'brightness_up'      # Hold = Increase brightness, Click = Increase brightness
REMOTE_ACTION_BRIGHTNESS_DOWN = 'brightness_down'  # Hold = Decrease brightness, Click = Decrease brightness

# ===== System Commands =====
CMD_NIGHT_MODE_ON = 1   # LED OFF
CMD_NIGHT_MODE_OFF = 2  # LED ON
CMD_SET_LED_COUNT = 0x50

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

# ===== LED Configuration Limits =====
MIN_LEDS = 1
MAX_LEDS = 60
DEFAULT_BRIGHTNESS = 255

# ===== Packet Sizes =====
PACKET_HEADER_SIZE = 18
PACKET_PAYLOAD_SIZE = 185
PACKET_TOTAL_SIZE = 203

# ===== FNV-1a Hash =====
FNV_OFFSET_BASIS = 2166136261
FNV_PRIME = 16777619
FNV_MASK = 0xFFFFFFFF

# ===== File Names =====
FILE_GATEWAYS = 'gateways.json'
FILE_BUTTONS = 'buttons.json'
FILE_LIGHTSTRIPS = 'lightstrips.json'
FILE_BRIDGE = 'bridge.json'
FILE_CONFIG = 'config.json'
FILE_PAIRING_HISTORY = 'pairing_history.json'

# ===== Logging =====
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
