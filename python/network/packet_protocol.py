"""
Protocol handler for HueMixLink UDP binary packets.

Handles packet encoding/decoding, signature validation, and MAC address formatting.
"""
import struct
import binascii
from typing import Tuple, Optional, List, Any
import logging
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from constants import (
    PKT_PAIR_CONFIRM, PKT_LIGHT_RAW, PKT_SYS_CMD, PKT_GW_LIST_UPD,
    PKT_PAIR_ACK, PKT_KEY_DISTRIB,
    PKT_HELLO, PKT_BTN_EVENT, PKT_DELIVERY_RPT, PKT_MOTION_EVENT,
    PKT_PING_DEVICE, PKT_PING,
    PKT_OTA_NOTIFY, PKT_OTA_READY, PKT_OTA_CHUNK, PKT_OTA_CHUNK_ACK, PKT_OTA_COMPLETE, PKT_OTA_ABORT, PKT_OTA_CHECKPOINT_REQ,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, DEV_MOTION, DEV_DOOR, DEV_PLUGIN_MARKER,
    FNV_OFFSET_BASIS, FNV_PRIME, FNV_MASK, OTA_CHUNK_DATA_SIZE,
    PACKET_TOTAL_SIZE, PROTO_VERSION_V1, PROTO_VERSION_V2,
    V2_HEADER_SIZE, V2_AAD_SIZE, V2_TAG_SIZE, V2_OVERHEAD,
    V2_MIN_FRAME_SIZE, V2_FORBIDDEN_LEN,
    V2_HELLO_CAP_OFFSETS, V2_HELLO_CAP_LEN, V2_HELLO_FLAG_HAS_PIN,
)
from services.home_id_manager import home_id_manager

logger = logging.getLogger(__name__)


class MACFormatter:
    """Utility class for MAC address formatting and conversion."""
    
    @staticmethod
    def to_string(mac_bytes: bytes) -> str:
        """Convert 6-byte MAC address to colon-separated string.
        
        Args:
            mac_bytes: 6 bytes representing MAC address
            
        Returns:
            MAC address string like "AA:BB:CC:DD:EE:FF"
        """
        return binascii.hexlify(mac_bytes, ':').decode('utf-8').upper()
    
    @staticmethod
    def to_bytes(mac_string: str) -> bytes:
        """Convert colon-separated MAC string to 6 bytes.
        
        Args:
            mac_string: MAC address like "AA:BB:CC:DD:EE:FF"
            
        Returns:
            6 bytes representing MAC address
        """
        return binascii.unhexlify(mac_string.replace(":", ""))
    
    @staticmethod
    def format_with_colons(mac_string: str) -> str:
        """Ensure MAC string is properly formatted with colons.
        
        Args:
            mac_string: MAC address in any format
            
        Returns:
            MAC address with colons like "AA:BB:CC:DD:EE:FF"
        """
        # Remove any existing separators
        clean = mac_string.replace(":", "").replace("-", "")
        # Add colons every 2 characters
        return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()
    
    @staticmethod
    def parse_rssi(rssi_byte: int) -> int:
        """Parse RSSI value from unsigned byte to signed dBm.
        
        Args:
            rssi_byte: Unsigned byte (0-255)
            
        Returns:
            Signed RSSI value in dBm (typically -100 to 0)
        """
        return rssi_byte - 256 if rssi_byte > 127 else rssi_byte


class PacketEncoder:
    """Encodes outgoing packets with proper structure and signatures."""
    
    def __init__(self, home_id: int | None = None):
        """Initialize encoder with HomeID.
        
        Args:
            home_id: 4-byte network identifier for signature calculation
        """
        if home_id is None:
            home_id = home_id_manager.get_or_create_home_id()
        self.home_id = home_id
    
    def _calculate_hash(self, payload_bytes: bytes, home_id: Optional[int] = None) -> int:
        """Calculate FNV-1a hash for message authentication.
        
        Args:
            payload_bytes: The payload to hash (only the payload portion, not full packet)
            home_id: HomeID to use (defaults to self.home_id)
            
        Returns:
            32-bit hash value
        """
        if home_id is None:
            home_id = self.home_id
            
        hash_val = FNV_OFFSET_BASIS
        
        # Hash the home ID
        for b in struct.pack("<I", home_id):
            hash_val ^= b
            hash_val = (hash_val * FNV_PRIME) & FNV_MASK
        
        # Hash the payload
        for b in payload_bytes:
            hash_val ^= b
            hash_val = (hash_val * FNV_PRIME) & FNV_MASK
        
        return hash_val
    
    def encode_pair_confirm(self, target_mac: str, new_home_id: int, msg_id: int = 0) -> bytes:
        """Encode a pairing confirmation packet.
        
        Args:
            target_mac: MAC address of device to pair (string with colons)
            new_home_id: HomeID to assign to the device
            msg_id: Message ID for tracking
            
        Returns:
            Packet with header + 5-byte payload (matching test.py)
        """
        pkt_type = PKT_PAIR_CONFIRM
        src_mac = b'\x00' * 6  # Server has no MAC
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: new HomeID (4 bytes) + assigned device ID (1 byte) = 5 bytes total
        payload_data = struct.pack("<IB", new_home_id, 0)
        
        # Pad to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload with HOME_ID=0 for unpaired devices)
        signature = self._calculate_hash(payload, 0)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(185) = 203 bytes
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet

    def encode_custom(self, pkt_type: int, target_mac: str, payload: bytes, msg_id: int = 0) -> bytes:
        """Encode a legacy v1 203-byte packet with arbitrary packet type and payload (<= 185 B)."""
        raw_payload = bytes(payload or b"")
        if len(raw_payload) > 185:
            raise ValueError(f"v1 payload too large: {len(raw_payload)} > 185 bytes")
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        padded = raw_payload + b'\x00' * (185 - len(raw_payload))
        signature = self._calculate_hash(padded)
        return struct.pack("<BI", pkt_type & 0xFF, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id & 0xFF) + padded
    
    def encode_light_raw(self, target_mac: str, rgb_data: List[Tuple[int, int, int]], 
                        brightness: int = 255, msg_id: int = 0) -> bytes:
        """Encode a raw RGB light control packet.
        
        Args:
            target_mac: MAC address of light strip (string with colons)
            rgb_data: List of (r, g, b) tuples for each LED
            brightness: Global brightness 0-255
            msg_id: Message ID for delivery tracking
            
        Returns:
            Complete 200-byte packet (18 header + 182 payload)
        """
        pkt_type = PKT_LIGHT_RAW
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: count(1) + brightness(1) + data[180]
        count = min(len(rgb_data), 60)  # Max 60 LEDs
        payload = struct.pack("<BB", count, brightness)
        
        for i in range(count):
            r, g, b = rgb_data[i]
            payload += struct.pack("<BBB", r, g, b)
        
        # Pad to 185 bytes
        payload += b'\x00' * (185 - len(payload))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(185) = 203 bytes
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_gateway_list(self, gateway_macs: List[str], msg_id: int = 0) -> bytes:
        """Encode a gateway list update packet for mesh synchronization.
        
        Args:
            gateway_macs: List of gateway radio MAC addresses (max 10)
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_GW_LIST_UPD
        src_mac = b'\x00' * 6
        tgt_mac = b'\xFF' * 6  # Broadcast
        
        # Payload: count(1) + mac_list(6 * count)
        count = min(len(gateway_macs), 10)
        payload = struct.pack("<B", count)
        
        for i in range(count):
            mac_bytes = MACFormatter.to_bytes(gateway_macs[i])
            payload += mac_bytes
        
        # Pad to 185 bytes
        payload += b'\x00' * (185 - len(payload))
        
        # Calculate signature
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_gateway_list_for_device(self, target_mac: str, gateway_macs: List[str], msg_id: int = 0) -> bytes:
        """Encode a gateway list update packet for a specific device.
        
        Args:
            target_mac: Target device MAC address
            gateway_macs: List of gateway radio MAC addresses (max 10)
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_GW_LIST_UPD
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: count(1) + mac_list(6 * count)
        count = min(len(gateway_macs), 10)
        payload = struct.pack("<B", count)
        
        for i in range(count):
            mac_bytes = MACFormatter.to_bytes(gateway_macs[i])
            payload += mac_bytes
        
        # Pad to 185 bytes
        payload += b'\x00' * (185 - len(payload))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_sys_cmd(self, target_mac: str, command: int, value: int = 0, msg_id: int = 0, value_uint32: Optional[int] = None) -> bytes:
        """Encode a system command packet.
        
        Args:
            target_mac: MAC address of target device
            command: Command code (1=night mode on, 2=night mode off, 0x40=motion cooldown, etc.)
            value: Optional command value (single byte)
            msg_id: Message ID
            value_uint32: Optional uint32 value (used for motion sensor cooldown at offset 2)
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_SYS_CMD
        src_mac = b'\x00' * 6 
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: command(1) + value(1) + optional uint32 data
        if value_uint32 is not None:
            # For commands that need uint32 at offset 2 (like motion sensor cooldown)
            payload_data = struct.pack("<BBI", command, value, value_uint32)
        else:
            payload_data = struct.pack("<BB", command, value)
        
        # Pad payload to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(185)
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ping(self, target_mac: str, msg_id: int = 0) -> bytes:
        """Encode a ping packet.
        
        Args:
            target_mac: MAC address of target device (gateway)
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_PING
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Empty payload
        payload = b'\x00' * 185
        
        # Calculate signature
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ping_device(self, target_mac: str, msg_id: int = 0) -> bytes:
        """Encode a device ping packet.
        
        Args:
            target_mac: MAC address of target device (lightstrip, button, etc.)
            msg_id: Message ID for delivery tracking
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_PING_DEVICE
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Empty payload (185 bytes of zeros)
        payload = b'\x00' * 185
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(185) = 203 bytes
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ota_notify(self, target_mac: str, firmware_size: int, sha256_hash: bytes, 
                         version: Tuple[int, int, int, int], msg_id: int = 0) -> bytes:
        """Encode an OTA notify packet to initiate firmware update.
        
        Args:
            target_mac: MAC address of target device
            firmware_size: Total firmware size in bytes
            sha256_hash: 32-byte SHA256 hash of firmware
            version: Tuple of (major, minor, patch, build) version bytes
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_OTA_NOTIFY
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: firmware_size(4) + sha256_hash(32) + version(4)
        payload_data = struct.pack("<I", firmware_size) + sha256_hash[:32] + struct.pack("BBBB", *version)
        
        # Pad to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ota_chunk(self, target_mac: str, chunk_index: int, chunk_data: bytes, msg_id: int = 0) -> bytes:
        """Encode an OTA chunk packet containing firmware data.
        
        Args:
            target_mac: MAC address of target device
            chunk_index: Chunk sequence number (0-based)
            chunk_data: Firmware data for this chunk (max 182 bytes)
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_OTA_CHUNK
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: chunk_index(2) + data_len(1) + data(182)
        data_len = min(len(chunk_data), OTA_CHUNK_DATA_SIZE)
        payload_data = struct.pack("<HB", chunk_index, data_len) + chunk_data[:data_len]
        
        # Pad to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ota_complete(self, target_mac: str, sha256_hash: bytes, msg_id: int = 0) -> bytes:
        """Encode an OTA complete packet to signal end of transfer.
        
        Args:
            target_mac: MAC address of target device
            sha256_hash: 32-byte SHA256 hash for final validation
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_OTA_COMPLETE
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: sha256_hash(32)
        payload_data = sha256_hash[:32]
        
        # Pad to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ota_checkpoint_req(self, target_mac: str, msg_id: int = 0) -> bytes:
        """Encode OTA checkpoint request packet (server asks device for progress).
        
        Args:
            target_mac: MAC address of target device
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_OTA_CHECKPOINT_REQ
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # No payload needed - just a request (185 bytes of zeros)
        payload = b'\x00' * 185
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_ota_abort(self, target_mac: str, reason_code: int = 0, msg_id: int = 0) -> bytes:
        """Encode an OTA abort packet to cancel update.
        
        Args:
            target_mac: MAC address of target device
            reason_code: Reason for abort (0=user cancel, 1=timeout, 2=error)
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_OTA_ABORT
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: reason_code(1)
        payload_data = struct.pack("<B", reason_code)
        
        # Pad to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
        # Calculate signature (hash full 185-byte payload)
        signature = self._calculate_hash(payload)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet


class PacketDecoder:
    """Decodes incoming packets and validates signatures."""
    
    def __init__(self, home_id: int | None = None):
        """Initialize decoder with HomeID.
        
        Args:
            home_id: Paired device HomeID (unpaired devices use 0x00000000)
        """
        if home_id is None:
            home_id = home_id_manager.get_or_create_home_id()
        self.home_id = home_id
        self.unpaired_home_id = 0
    
    def _calculate_hash(self, payload_bytes: bytes, home_id: int) -> int:
        """Calculate FNV-1a hash for message authentication.
        
        Args:
            payload_bytes: The payload to hash
            home_id: HomeID to use for hashing
            
        Returns:
            32-bit hash value
        """
        hash_val = 2166136261
        for b in struct.pack("<I", home_id):
            hash_val ^= b
            hash_val = (hash_val * 16777619) & 0xFFFFFFFF
        for b in payload_bytes:
            hash_val ^= b
            hash_val = (hash_val * 16777619) & 0xFFFFFFFF
        return hash_val
    
    def decode(self, data: bytes) -> Optional[dict]:
        """Decode a packet and validate its signature.
        
        Args:
            data: Raw packet data (minimum 203 bytes)
            
        Returns:
            Dictionary with packet fields if valid, None otherwise:
            {
                'type': packet_type,
                'source_mac': MAC string,
                'target_mac': MAC string,
                'msg_id': message ID,
                'payload': raw payload bytes,
                'is_paired': True if using paired HomeID
            }
        """
        if len(data) < 203:
            logger.warning(f"Packet too short: {len(data)} bytes (expected 203)")
            return None
        
        # Parse header: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) = 18 bytes
        pkt_type, signature = struct.unpack("<BI", data[0:5])
        src_mac_bytes = data[5:11]
        tgt_mac_bytes = data[11:17]
        msg_id = struct.unpack("<B", data[17:18])[0]
        
        # Payload is always 185 bytes
        payload = data[18:203]
        
        # Gateway modifies RSSI after receiving packet but before forwarding:
        # - PKT_HELLO: RSSI inserted at payload[1]
        # - PKT_PING_DEVICE: RSSI inserted at payload[0]
        # Signature was calculated BEFORE RSSI modification, so we need to zero it for validation
        payload_for_hash = bytearray(payload)
        if pkt_type == PKT_HELLO:
            dev_type = struct.unpack("<B", payload[0:1])[0]
            if dev_type != DEV_GATEWAY:
                payload_for_hash[1] = 0
        elif pkt_type == PKT_PING_DEVICE:
            payload_for_hash[0] = 0  # Zero out RSSI byte for signature validation
        
        # Always hash the full 185-byte payload
        # Try paired HomeID first
        sig_valid_paired = self._calculate_hash(bytes(payload_for_hash), self.home_id)
        if signature == sig_valid_paired:
            return {
                'type': pkt_type,
                'source_mac': MACFormatter.to_string(src_mac_bytes),
                'target_mac': MACFormatter.to_string(tgt_mac_bytes),
                'msg_id': msg_id,
                'payload': payload,  # Return original payload with RSSI intact
                'is_paired': True
            }
        
        # Try unpaired HomeID (0)
        sig_valid_unpaired = self._calculate_hash(bytes(payload_for_hash), self.unpaired_home_id)
        if signature == sig_valid_unpaired:
            return {
                'type': pkt_type,
                'source_mac': MACFormatter.to_string(src_mac_bytes),
                'target_mac': MACFormatter.to_string(tgt_mac_bytes),
                'msg_id': msg_id,
                'payload': payload,  # Return original payload with RSSI intact
                'is_paired': False
            }
        
        # Signature invalid - log details for debugging
        src_mac_str = MACFormatter.to_string(src_mac_bytes)
        if src_mac_str != "00:00:00:00:00:00":
            logger.warning(f"Invalid signature from {src_mac_str} - pkt_type={pkt_type:#x}, "
                         f"sig={signature:#010x}, paired_expected={sig_valid_paired:#010x}, "
                         f"unpaired_expected={sig_valid_unpaired:#010x}")
        
        return None
    
    def parse_hello(self, payload: bytes) -> Optional[dict]:
        """Parse HELLO packet payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with device info:
            {
                'device_type': DEV_GATEWAY/DEV_BUTTON/DEV_LIGHT/DEV_REMOTE,
                'version': Version string (e.g., "3.7.3"),
                'radio_mac': MAC string (for gateways),
                'rssi': Signal strength (for buttons/lights),
                'is_rgbw': RGBW flag (for lights),
                'num_leds': LED count (for lights)
            }
        """
        if len(payload) < 1:
            return None
        
        dev_type = struct.unpack("<B", payload[0:1])[0]
        result = {'device_type': dev_type}
        
        # Handle plugin device type marker (unpaired plugin device with UUID)
        if dev_type == DEV_PLUGIN_MARKER and len(payload) >= 24:
            # Plugin HELLO format:
            # payload[0] = 0xFE (DEV_PLUGIN_MARKER)
            # payload[1] = RSSI
            # payload[2:18] = plugin_uuid (16 bytes)
            # payload[18] = plugin_device_type (plugin-scoped, 0-255)
            # payload[19:23] = version (major, minor, patch, build)
            result['plugin_uuid'] = payload[2:18].hex()
            result['plugin_device_type'] = struct.unpack("<B", payload[18:19])[0]
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            
            if len(payload) >= 23:
                major, minor, patch, build = struct.unpack("<BBBB", payload[19:23])
                result['version'] = f"{major}.{minor}.{patch}"
                result['platform'] = 'esp8266' if build == 1 else 'esp32'

            self._attach_hello_capability(dev_type, payload, result)
            return result
        
        if dev_type == DEV_GATEWAY and len(payload) >= 15:
            # Gateway: radio MAC at bytes 1-7, net version at bytes 7-11, radio version at bytes 11-15
            result['radio_mac'] = MACFormatter.to_string(payload[1:7])
            major_net, minor_net, patch_net, build_net = struct.unpack("<BBBB", payload[7:11])
            result['version_net'] = f"{major_net}.{minor_net}.{patch_net}"
            major_radio, minor_radio, patch_radio, build_radio = struct.unpack("<BBBB", payload[11:15])
            result['version_radio'] = f"{major_radio}.{minor_radio}.{patch_radio}"
        
        elif dev_type == DEV_BUTTON and len(payload) >= 6:
            # Button: type(1) + RSSI(1) + version(3) + platform(1)
            major, minor, patch, build = struct.unpack("<BBBB", payload[2:6])
            result['version'] = f"{major}.{minor}.{patch}"
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['platform'] = 'esp8266' if build == 1 else 'esp32'

        elif dev_type == DEV_REMOTE and len(payload) >= 6:
            # Remote: type(1) + RSSI(1) + version(3) + platform(1) + button_count(1, optional)
            major, minor, patch, build = struct.unpack("<BBBB", payload[2:6])
            result['version'] = f"{major}.{minor}.{patch}"
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['platform'] = 'esp8266' if build == 1 else 'esp32'
            
            # Parse button_count if available (byte 6)
            # 0 or missing = default to 4 buttons, 1-4 = actual button count
            if len(payload) >= 7:
                button_count = struct.unpack("<B", payload[6:7])[0]
                result['button_count'] = button_count if button_count >= 1 else 4
            else:
                result['button_count'] = 4  # Default for legacy remotes
        
        elif dev_type == DEV_MOTION and len(payload) >= 6:
            # Motion sensor: type(1) + RSSI(1) + version(3) + platform(1)
            major, minor, patch, build = struct.unpack("<BBBB", payload[2:6])
            result['version'] = f"{major}.{minor}.{patch}"
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['platform'] = 'esp8266' if build == 1 else 'esp32'

        elif dev_type == DEV_DOOR and len(payload) >= 6:
            # Door sensor: type(1) + RSSI(1) + version(3) + platform_flags(1)
            major, minor, patch, platform_byte = struct.unpack("<BBBB", payload[2:6])
            result['version'] = f"{major}.{minor}.{patch}"
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['platform'] = 'esp8266' if (platform_byte & 0x01) == 0x01 else 'esp32'
            result['battery_type'] = 'cr123a' if (platform_byte & 0x80) == 0x80 else 'li_ion'

        elif dev_type == DEV_LIGHT and len(payload) >= 9:
            # Light: type(1) + RSSI(1) + RGBW(1) + LED_COUNT(2) + version(3) + platform(1) + model_id(2)
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['is_rgbw'] = payload[2] == 1
            result['num_leds'] = struct.unpack(">H", payload[3:5])[0]  # Big-endian
            major, minor, patch = struct.unpack("<BBB", payload[5:8])
            result['version'] = f"{major}.{minor}.{patch}"
            
            # Parse platform if available (byte 8)
            if len(payload) >= 9:
                platform_byte = struct.unpack("<B", payload[8:9])[0]
                result['platform'] = 'esp8266' if platform_byte == 1 else 'esp32'
            
            # Parse model_id if available (bytes 9-10, 2 bytes little-endian)
            # Model 1: ESP32, RGB, GRB, WS2812B
            # Model 2: ESP32, RGBW, GRB, SK6812
            # Model 3: ESP8266, RGB, GRB, WS2812B
            # Model 4: ESP8266, RGBW, GRB, SK6812
            if len(payload) >= 11:
                model_id = struct.unpack("<H", payload[9:11])[0]  # uint16_t little-endian
                result['model_id'] = model_id

        self._attach_hello_capability(dev_type, payload, result)
        return result

    @staticmethod
    def _attach_hello_capability(dev_type: int, payload: bytes, result: dict) -> None:
        """Parse optional Security v2 capability TLV + pairing fields appended to v1 HELLO."""
        cap_off = V2_HELLO_CAP_OFFSETS.get(dev_type)
        if cap_off is None or len(payload) < cap_off + 4:
            return

        proto_ver = payload[cap_off]
        if proto_ver < PROTO_VERSION_V2:
            return

        max_frame = struct.unpack("<H", payload[cap_off + 1:cap_off + 3])[0]
        flags = payload[cap_off + 3]
        result['proto_version'] = proto_ver
        result['max_frame'] = max_frame
        result['has_pin'] = bool(flags & V2_HELLO_FLAG_HAS_PIN)

        if len(payload) >= cap_off + V2_HELLO_CAP_LEN:
            ecdh_pub = payload[cap_off + 4:cap_off + 36]
            nonce_d = payload[cap_off + 36:cap_off + 44]
            if any(ecdh_pub):
                result['ecdh_pub'] = ecdh_pub
                result['nonce_d'] = nonce_d
    
    def parse_button_event(self, payload: bytes) -> Optional[dict]:
        """Parse button event payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with event info:
            {
                'action': ACT_CLICK/ACT_HOLDING/ACT_RELEASE/ACT_SYNC,
                'battery_mv': Battery voltage in millivolts (uint16),
                'button_index': Index (-1 for normal button, 0-3 for remote),
                'version_major': Firmware major version,
                'version_minor': Firmware minor version,
                'version_patch': Firmware patch version,
                'platform': 'esp32' or 'esp8266'
            }
        """
        if len(payload) < 3:
            return None
        
        # Payload_Button structure (9 bytes):
        # action(1) + battery_mv(2) + button_index(1) + version_major(1) + version_minor(1) + version_patch(1) + platform(1) + button_count(1)
        action = struct.unpack("<B", payload[0:1])[0]
        battery_mv = struct.unpack("<H", payload[1:3])[0]  # uint16 little-endian
        
        result = {
            'action': action,
            'battery_mv': battery_mv
        }
        
        # Parse button_index (signed int8, -1 for normal button, 0-3 for remote)
        if len(payload) >= 4:
            button_index = struct.unpack("<b", payload[3:4])[0]  # signed byte
            result['button_index'] = button_index
        
        # Parse version fields - only include if all version bytes are present AND not all zeros
        if len(payload) >= 7:
            version_major = struct.unpack("<B", payload[4:5])[0]
            version_minor = struct.unpack("<B", payload[5:6])[0]
            version_patch = struct.unpack("<B", payload[6:7])[0]
            
            # Only set version if it's not 0.0.0 (indicates valid firmware version)
            if version_major > 0 or version_minor > 0 or version_patch > 0:
                result['version_major'] = version_major
                result['version_minor'] = version_minor
                result['version_patch'] = version_patch
                result['version'] = f"{version_major}.{version_minor}.{version_patch}"
        
            # Parse platform (0=ESP32, 1=ESP8266)
            if len(payload) >= 8:
                platform_byte = struct.unpack("<B", payload[7:8])[0]
                result['platform'] = 'esp8266' if platform_byte == 1 else 'esp32'
            
            # Parse button_count (1-4)
            if len(payload) >= 9:
                button_count = struct.unpack("<B", payload[8:9])[0]
                if button_count >= 1 and button_count <= 4:
                    result['button_count'] = button_count
        
        return result
    
    def parse_motion_event(self, payload: bytes) -> Optional[dict]:
        """Parse motion sensor event payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with event info:
            {
                'action': ACT_MOTION_DETECTED/ACT_SYNC,
                'battery_mv': Battery voltage in millivolts (uint16),
                'light_level': Light level from LDR sensor (0-255),
                'version_major': Firmware major version,
                'version_minor': Firmware minor version,
                'version_patch': Firmware patch version,
                'platform': 'esp32' or 'esp8266'
            }
        """
        if len(payload) < 4:
            return None
        
        # Payload_Motion structure (8 bytes):
        # action(1) + battery_mv(2) + light_level(1) + version_major(1) + version_minor(1) + version_patch(1) + platform(1)
        action = struct.unpack("<B", payload[0:1])[0]
        battery_mv = struct.unpack("<H", payload[1:3])[0]  # uint16 little-endian
        light_level = struct.unpack("<B", payload[3:4])[0]
        
        result = {
            'action': action,
            'battery_mv': battery_mv,
            'light_level': light_level
        }
        
        # Parse version fields - only include if all version bytes are present AND not all zeros
        if len(payload) >= 7:
            version_major = struct.unpack("<B", payload[4:5])[0]
            version_minor = struct.unpack("<B", payload[5:6])[0]
            version_patch = struct.unpack("<B", payload[6:7])[0]
            
            # Only set version if it's not 0.0.0 (indicates valid firmware version)
            if version_major > 0 or version_minor > 0 or version_patch > 0:
                result['version_major'] = version_major
                result['version_minor'] = version_minor
                result['version_patch'] = version_patch
                result['version'] = f"{version_major}.{version_minor}.{version_patch}"
        
            # Parse platform (0=ESP32, 1=ESP8266)
            if len(payload) >= 8:
                platform_byte = struct.unpack("<B", payload[7:8])[0]
                result['platform'] = 'esp8266' if platform_byte == 1 else 'esp32'
        
        return result

    def parse_door_event(self, payload: bytes) -> Optional[dict]:
        """Parse door sensor event payload.

        Args:
            payload: Raw payload bytes

        Returns:
            Dictionary with event info:
            {
                'action': ACT_DOOR_OPENED/ACT_DOOR_CLOSED/ACT_SYNC,
                'battery_mv': Battery voltage in millivolts (uint16),
                'light_level': Light level from LDR sensor (0-10),
                'version_major': Firmware major version,
                'version_minor': Firmware minor version,
                'version_patch': Firmware patch version,
                'platform': 'esp32' or 'esp8266'
            }
        """
        if len(payload) < 4:
            return None

        # Payload_Door structure (8 bytes):
        # action(1) + battery_mv(2) + light_level(1) + version_major(1) + version_minor(1) + version_patch(1) + platform(1)
        action = struct.unpack("<B", payload[0:1])[0]
        battery_mv = struct.unpack("<H", payload[1:3])[0]  # uint16 little-endian
        light_level = struct.unpack("<B", payload[3:4])[0]

        result = {
            'action': action,
            'battery_mv': battery_mv,
            'light_level': light_level
        }

        # Parse version fields - only include if all version bytes are present AND not all zeros
        if len(payload) >= 7:
            version_major = struct.unpack("<B", payload[4:5])[0]
            version_minor = struct.unpack("<B", payload[5:6])[0]
            version_patch = struct.unpack("<B", payload[6:7])[0]

            # Only set version if it's not 0.0.0 (indicates valid firmware version)
            if version_major > 0 or version_minor > 0 or version_patch > 0:
                result['version_major'] = version_major
                result['version_minor'] = version_minor
                result['version_patch'] = version_patch
                result['version'] = f"{version_major}.{version_minor}.{version_patch}"

            # Parse platform+flags (bit0: platform, bit7: CR123A battery type)
            if len(payload) >= 8:
                platform_byte = struct.unpack("<B", payload[7:8])[0]
                result['platform'] = 'esp8266' if (platform_byte & 0x01) == 0x01 else 'esp32'
                result['battery_type'] = 'cr123a' if (platform_byte & 0x80) == 0x80 else 'li_ion'
                result['is_cr123a'] = (platform_byte & 0x80) == 0x80

        return result
    
    def parse_delivery_report(self, payload: bytes) -> Optional[dict]:
        """Parse delivery report payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with report info:
            {
                'msg_id': Original message ID,
                'success': True/False,
                'target_mac': MAC string of final recipient
            }
        """
        if len(payload) < 8:
            return None
        
        msg_id = payload[0]
        success = payload[1] != 0
        target_mac = MACFormatter.to_string(payload[2:8])
        
        return {
            'msg_id': msg_id,
            'success': success,
            'target_mac': target_mac
        }
    
    def parse_gateway_list(self, payload: bytes) -> Optional[List[str]]:
        """Parse gateway list update payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            List of gateway radio MAC addresses
        """
        if len(payload) < 1:
            return None
        
        count = struct.unpack("<B", payload[0:1])[0]
        gateway_macs = []
        
        for i in range(min(count, 10)):
            offset = 1 + (i * 6)
            if offset + 6 <= len(payload):
                mac = MACFormatter.to_string(payload[offset:offset+6])
                gateway_macs.append(mac)
        
        return gateway_macs
    
    def parse_ota_notify(self, payload: bytes) -> Optional[dict]:
        """Parse OTA notify payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with firmware_size, sha256_hash, version
        """
        if len(payload) < 40:
            return None
        
        firmware_size = struct.unpack("<I", payload[0:4])[0]
        sha256_hash = payload[4:36]
        version = struct.unpack("BBBB", payload[36:40])
        
        return {
            'firmware_size': firmware_size,
            'sha256_hash': sha256_hash,
            'version': version
        }
    
    def parse_ota_ready(self, payload: bytes) -> Optional[dict]:
        """Parse OTA ready response payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with firmware_size, battery_mv
        """
        if len(payload) < 6:
            return None
        
        firmware_size = struct.unpack("<I", payload[0:4])[0]
        battery_mv = struct.unpack("<H", payload[4:6])[0]
        
        return {
            'firmware_size': firmware_size,
            'battery_mv': battery_mv
        }
    
    def parse_ota_chunk(self, payload: bytes) -> Optional[dict]:
        """Parse OTA chunk payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with chunk_index, data_len, data
        """
        if len(payload) < 3:
            return None
        
        chunk_index = struct.unpack("<H", payload[0:2])[0]
        data_len = struct.unpack("<B", payload[2:3])[0]
        data = payload[3:3+data_len]
        
        return {
            'chunk_index': chunk_index,
            'data_len': data_len,
            'data': data
        }
    
    def parse_ota_complete(self, payload: bytes) -> Optional[dict]:
        """Parse OTA complete payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with sha256_hash
        """
        if len(payload) < 32:
            return None
        
        sha256_hash = payload[0:32]
        
        return {
            'sha256_hash': sha256_hash
        }
    
    def parse_ota_chunk_ack(self, payload: bytes) -> Optional[dict]:
        """Parse OTA chunk ACK payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with chunk_index
        """
        if len(payload) < 2:
            return None
        
        chunk_index = struct.unpack("<H", payload[0:2])[0]
        
        return {
            'chunk_index': chunk_index
        }
    
    def parse_ota_abort(self, payload: bytes) -> Optional[dict]:
        """Parse OTA abort payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with reason_code
        """
        if len(payload) < 1:
            return None
        
        reason_code = struct.unpack("<B", payload[0:1])[0]
        
        return {
            'reason_code': reason_code
        }


# ===== Security v2 Codec (AES-128-GCM, variable-length) =====

V2_HEADER_STRUCT = struct.Struct("<BB6s6sBIHB")


class PacketEncoderV2:
    """Encodes variable-length AES-128-GCM authenticated v2 packets."""

    @staticmethod
    def build_nonce(src_mac_bytes: bytes, counter: int, key_epoch: int) -> bytes:
        if len(src_mac_bytes) != 6:
            raise ValueError("src_mac_bytes must be 6 bytes")
        return src_mac_bytes + struct.pack("<IH", int(counter) & 0xFFFFFFFF, int(key_epoch) & 0xFFFF)

    @staticmethod
    def encode(
        pkt_type: int,
        src_mac: str,
        dst_mac: str,
        msg_id: int,
        counter: int,
        key_epoch: int,
        plaintext: bytes,
        key: bytes,
        rssi: int = 0,
        max_frame: Optional[int] = None,
    ) -> bytes:
        """Encode and encrypt one v2 frame.

        Enforces:
        - N >= 1 byte plaintext
        - Never produces an exact 203-byte frame (pads 1 zero byte if N == 165)
        - AAD covers header bytes 0..20 (excludes rssi at offset 21)
        """
        if len(key) != 16:
            raise ValueError("AES-128-GCM key must be 16 bytes")
        if len(plaintext) < 1:
            plaintext = b"\x00"

        # Rule 1 (§4.2): A v2 frame must never be exactly 203 bytes (reserved for v1)
        if len(plaintext) + V2_OVERHEAD == V2_FORBIDDEN_LEN:
            plaintext = plaintext + b"\x00"

        total_len = len(plaintext) + V2_OVERHEAD
        if max_frame is not None and total_len > max_frame:
            raise ValueError(f"v2 frame size {total_len} exceeds peer max_frame {max_frame}")

        src_bytes = MACFormatter.to_bytes(src_mac)
        dst_bytes = MACFormatter.to_bytes(dst_mac)
        header = V2_HEADER_STRUCT.pack(
            pkt_type & 0xFF,
            PROTO_VERSION_V2,
            src_bytes,
            dst_bytes,
            msg_id & 0xFF,
            int(counter) & 0xFFFFFFFF,
            int(key_epoch) & 0xFFFF,
            rssi & 0xFF,
        )
        nonce = PacketEncoderV2.build_nonce(src_bytes, counter, key_epoch)
        aad = header[:V2_AAD_SIZE]
        ciphertext_and_tag = AESGCM(key).encrypt(nonce, plaintext, aad)
        return header + ciphertext_and_tag

    @staticmethod
    def max_ota_chunk_size(max_frame: int) -> int:
        """Return max OTA chunk data bytes for a v2 peer (Payload_OtaChunkV2Header = 4 B)."""
        return max(1, int(max_frame) - V2_OVERHEAD - 4)

    @staticmethod
    def max_leds_per_frame(max_frame: int) -> int:
        """Return max RGB LEDs in one v2 PKT_LIGHT_RAW frame (Payload_LightV2Header = 3 B)."""
        return max(1, (int(max_frame) - V2_OVERHEAD - 3) // 3)

    @staticmethod
    def encode_ota_chunk_v2(
        target_mac: str,
        chunk_index: int,
        chunk_data: bytes,
        counter: int,
        key_epoch: int,
        key: bytes,
        max_frame: int = 250,
        msg_id: int = 0,
    ) -> bytes:
        """Encode variable-length v2 OTA chunk with uint16 data_len."""
        cap = PacketEncoderV2.max_ota_chunk_size(max_frame)
        data = chunk_data[:cap]
        plaintext = struct.pack("<HH", chunk_index & 0xFFFF, len(data)) + data
        return PacketEncoderV2.encode(
            pkt_type=PKT_OTA_CHUNK,
            src_mac="00:00:00:00:00:00",
            dst_mac=target_mac,
            msg_id=msg_id,
            counter=counter,
            key_epoch=key_epoch,
            plaintext=plaintext,
            key=key,
            max_frame=max_frame,
        )

    @staticmethod
    def encode_light_raw_v2(
        target_mac: str,
        rgb_data: List[Tuple[int, int, int]],
        brightness: int,
        counter: int,
        key_epoch: int,
        key: bytes,
        max_frame: int = 250,
        msg_id: int = 0,
    ) -> bytes:
        """Encode variable-length v2 RGB light frame with uint16 LED count."""
        max_leds = PacketEncoderV2.max_leds_per_frame(max_frame)
        count = min(len(rgb_data), max_leds)
        parts = [struct.pack("<HB", count, brightness & 0xFF)]
        for r, g, b in rgb_data[:count]:
            parts.append(struct.pack("<BBB", r & 0xFF, g & 0xFF, b & 0xFF))
        plaintext = b"".join(parts)
        return PacketEncoderV2.encode(
            pkt_type=PKT_LIGHT_RAW,
            src_mac="00:00:00:00:00:00",
            dst_mac=target_mac,
            msg_id=msg_id,
            counter=counter,
            key_epoch=key_epoch,
            plaintext=plaintext,
            key=key,
            max_frame=max_frame,
        )


class PacketDecoderV2:
    """Decodes and authenticates AES-128-GCM v2 packets."""

    @staticmethod
    def parse_header(data: bytes) -> Optional[dict]:
        if len(data) < V2_HEADER_SIZE:
            return None
        pkt_type, version, src_bytes, dst_bytes, msg_id, counter, key_epoch, rssi_raw = (
            V2_HEADER_STRUCT.unpack(data[:V2_HEADER_SIZE])
        )
        return {
            'type': pkt_type,
            'version': version,
            'source_mac': MACFormatter.to_string(src_bytes),
            'source_mac_bytes': src_bytes,
            'target_mac': MACFormatter.to_string(dst_bytes),
            'target_mac_bytes': dst_bytes,
            'msg_id': msg_id,
            'counter': counter,
            'key_epoch': key_epoch,
            'rssi': MACFormatter.parse_rssi(rssi_raw),
        }

    @staticmethod
    def decode_with_key(data: bytes, key: bytes) -> Optional[dict]:
        """Decrypt and verify a v2 frame using the given 16-byte AES-GCM key."""
        if len(data) < V2_MIN_FRAME_SIZE or len(key) != 16:
            return None

        hdr = PacketDecoderV2.parse_header(data)
        if not hdr or hdr['version'] != PROTO_VERSION_V2:
            return None

        nonce = PacketEncoderV2.build_nonce(hdr['source_mac_bytes'], hdr['counter'], hdr['key_epoch'])
        aad = data[:V2_AAD_SIZE]
        ciphertext_and_tag = data[V2_HEADER_SIZE:]

        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext_and_tag, aad)
        except InvalidTag:
            return None

        return {
            'protocol_version': PROTO_VERSION_V2,
            'type': hdr['type'],
            'source_mac': hdr['source_mac'],
            'target_mac': hdr['target_mac'],
            'msg_id': hdr['msg_id'],
            'counter': hdr['counter'],
            'key_epoch': hdr['key_epoch'],
            'rssi': hdr['rssi'],
            'payload': plaintext,
            'ciphertext': ciphertext_and_tag[:-V2_TAG_SIZE],
            'is_paired': True,
        }


def decode_dual(
    data: bytes,
    v1_decoder: PacketDecoder,
    security_store: Any,
    allow_v2: bool = True,
    allow_v1: bool = True,
) -> Optional[dict]:
    """Dual-stack v1/v2 frame dispatcher with AEAD-first verification and anti-downgrade policy.

    Rules (§4.2):
    - len != 203: v2 only (try AEAD; drop if invalid or unknown version).
    - len == 203: try AEAD first if v2 header present; otherwise try v1 FNV only if
      the source is NOT recorded as a v2 device (except v1 HELLO discovery frames).
    """
    def _try_v2(strict_version_log: bool) -> Optional[dict]:
        if not allow_v2 or security_store is None or len(data) < V2_MIN_FRAME_SIZE:
            return None
        version = data[1]
        if version > PROTO_VERSION_V2:
            if strict_version_log:
                logger.warning(f"SECURITY: Dropping packet with unsupported protocol version {version:#x}")
            return None
        if version != PROTO_VERSION_V2:
            return None
        hdr = PacketDecoderV2.parse_header(data)
        if not hdr:
            return None
        src_mac = hdr['source_mac']
        if not security_store.is_v2_device(src_mac):
            if strict_version_log:
                logger.warning(
                    f"SECURITY: Dropped v2 packet (type={hdr['type']:#x}) from {src_mac} "
                    f"— device is not registered as v2 on server (factory reset device to re-pair)"
                )
            return None
        rec = security_store.get_device_record(src_mac) or {}
        expected_epoch = int(rec.get('key_epoch', security_store.epoch))
        if hdr['key_epoch'] != expected_epoch:
            if strict_version_log:
                logger.warning(
                    f"SECURITY: Rejecting v2 packet from {src_mac} with stale/wrong key_epoch="
                    f"{hdr['key_epoch']} (expected {expected_epoch})"
                )
            return None
        key = security_store.derive_k_own(src_mac, epoch=expected_epoch)
        return PacketDecoderV2.decode_with_key(data, key)

    if len(data) != PACKET_TOTAL_SIZE:
        return _try_v2(strict_version_log=True)

    # Ambiguous 203-byte length: try AEAD first (quietly, since byte 1 is FNV sig[0] for v1 frames)
    v2_pkt = _try_v2(strict_version_log=False)
    if v2_pkt is not None:
        return v2_pkt

    v1_pkt = v1_decoder.decode(data)
    if v1_pkt is None:
        return None

    if not allow_v1:
        # Even when legacy v1 is disabled, allow v2 pairing Flight 1 (203-byte PKT_HELLO
        # carrying a valid v2 capability TLV with proto_version >= 2) so new v2 devices
        # can still pair and re-pair.
        if v1_pkt['type'] == PKT_HELLO and allow_v2:
            parsed_hello = v1_decoder.parse_hello(v1_pkt['payload'])
            if parsed_hello and parsed_hello.get('proto_version', 1) >= PROTO_VERSION_V2:
                v1_pkt['protocol_version'] = PROTO_VERSION_V1
                return v1_pkt
        logger.warning(
            f"SECURITY: Dropped legacy v1 packet (type={v1_pkt['type']:#x}) from "
            f"{v1_pkt['source_mac']} because allow_v1=false"
        )
        return None

    # Anti-downgrade: reject non-HELLO v1 packets from devices already paired as v2
    src_mac = v1_pkt['source_mac']
    if (
        security_store is not None
        and v1_pkt['type'] != PKT_HELLO
        and security_store.is_v2_device(src_mac)
    ):
        logger.warning(
            f"SECURITY: Rejected v1 downgrade attempt (type={v1_pkt['type']:#x}) from v2 device {src_mac}"
        )
        return None

    v1_pkt['protocol_version'] = PROTO_VERSION_V1
    return v1_pkt
