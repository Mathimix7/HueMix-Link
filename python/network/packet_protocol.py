"""
Protocol handler for HueMixLink UDP binary packets.

Handles packet encoding/decoding, signature validation, and MAC address formatting.
"""
import struct
import binascii
from typing import Tuple, Optional, List
import logging
from constants import (
    PKT_PAIR_CONFIRM, PKT_LIGHT_RAW, PKT_SYS_CMD, PKT_GW_LIST_UPD,
    PKT_HELLO, PKT_BTN_EVENT, PKT_DELIVERY_RPT,
    PKT_PING_DEVICE, PKT_PING,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT,
    FNV_OFFSET_BASIS, FNV_PRIME, FNV_MASK
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
    
    def _calculate_hash(self, payload_bytes: bytes, home_id: int = None) -> int:
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
        payload = struct.pack("<IB", new_home_id, 0)
        
        # Calculate signature (hash only the 5-byte payload with HOME_ID=0 for unpaired devices)
        signature = self._calculate_hash(payload, 0)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(5) = 23 bytes
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
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
        
        # Pad to 182 bytes total (2 header + 180 data)
        payload += b'\x00' * (182 - len(payload))
        
        # Calculate signature (hash only the payload portion)
        signature = self._calculate_hash(payload)
        
        # Build final packet: type(1) + sig(4) + src(6) + tgt(6) + msgid(1) + payload(182)
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
        
        # Build packet without signature
        packet_no_sig = struct.pack("<B", pkt_type) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        # Calculate signature
        signature = self._calculate_hash(packet_no_sig)
        
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
        
        # Build packet without signature
        packet_no_sig = struct.pack("<B", pkt_type) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        # Calculate signature
        signature = self._calculate_hash(packet_no_sig)
        
        # Build final packet
        packet = struct.pack("<BI", pkt_type, signature) + src_mac + tgt_mac + struct.pack("<B", msg_id) + payload
        
        return packet
    
    def encode_sys_cmd(self, target_mac: str, command: int, value: int = 0, msg_id: int = 0) -> bytes:
        """Encode a system command packet.
        
        Args:
            target_mac: MAC address of target device
            command: Command code (1=night mode on, 2=night mode off, etc.)
            value: Optional command value
            msg_id: Message ID
            
        Returns:
            Complete 203-byte packet
        """
        pkt_type = PKT_SYS_CMD
        src_mac = b'\x00' * 6
        tgt_mac = MACFormatter.to_bytes(target_mac)
        
        # Payload: command(1) + value(1)
        payload_data = struct.pack("<BB", command, value)
        
        # Calculate signature (hash only the 2 bytes of actual data, matching sizeof(Payload_SysCmd))
        signature = self._calculate_hash(payload_data)
        
        # Pad payload to 185 bytes
        payload = payload_data + b'\x00' * (185 - len(payload_data))
        
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
        
        # Empty payload
        payload = b'\x00' * 185
        
        # Calculate signature
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
            data: Raw packet data (minimum 18 bytes)
            
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
        if len(data) < 18:
            logger.warning(f"Packet too short: {len(data)} bytes")
            return None
        
        # Parse header: type(1) + sig(4) + src(6) + tgt(6) + msgid(1)
        pkt_type, signature = struct.unpack("<BI", data[0:5])
        src_mac_bytes = data[5:11]
        tgt_mac_bytes = data[11:17]
        msg_id = struct.unpack("<B", data[17:18])[0]
        payload = data[18:] if len(data) > 18 else b''
        
        # Determine payload portion to hash based on packet type (matching test.py logic)
        payload_to_hash = payload
        
        if pkt_type == PKT_HELLO and len(payload) > 0:
            dev_type = payload[0]
            if dev_type == DEV_GATEWAY and len(payload) >= 7:
                # Gateway: hash only first 7 bytes (type + radio MAC)
                payload_to_hash = payload[:7]
            elif dev_type == DEV_BUTTON and len(payload) >= 1:
                # Button: hash only device type byte
                payload_to_hash = payload[:1]
            elif dev_type == DEV_LIGHT and len(payload) >= 5:
                # Light: mask RSSI byte to 0 before hashing
                temp = bytearray(payload[:5])
                temp[1] = 0
                payload_to_hash = bytes(temp)
        
        elif pkt_type == PKT_BTN_EVENT and len(payload) >= 3:
            # Button event: hash first 3 bytes
            payload_to_hash = payload[:3]
        
        elif pkt_type == PKT_DELIVERY_RPT and len(payload) >= 8:
            # Delivery report: hash first 8 bytes
            payload_to_hash = payload[:8]
        
        elif pkt_type == PKT_PING and len(payload) >= 4:
            # Ping response: hash first 4 bytes (uptime)
            payload_to_hash = payload[:4]
        
        elif pkt_type == PKT_PING_DEVICE:
            temp = bytearray(payload)
            temp[0] = 0  # Zero out RSSI byte
            payload_to_hash = bytes(temp)
        
        # Try paired HomeID first
        sig_valid_paired = self._calculate_hash(payload_to_hash, self.home_id)
        if signature == sig_valid_paired:
            return {
                'type': pkt_type,
                'source_mac': MACFormatter.to_string(src_mac_bytes),
                'target_mac': MACFormatter.to_string(tgt_mac_bytes),
                'msg_id': msg_id,
                'payload': payload,
                'is_paired': True
            }
        
        # Try unpaired HomeID (0)
        sig_valid_unpaired = self._calculate_hash(payload_to_hash, self.unpaired_home_id)
        if signature == sig_valid_unpaired:
            return {
                'type': pkt_type,
                'source_mac': MACFormatter.to_string(src_mac_bytes),
                'target_mac': MACFormatter.to_string(tgt_mac_bytes),
                'msg_id': msg_id,
                'payload': payload,
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
                'device_type': DEV_GATEWAY/DEV_BUTTON/DEV_LIGHT,
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
        
        if dev_type == DEV_GATEWAY and len(payload) >= 7:
            # Gateway: radio MAC at bytes 1-7
            result['radio_mac'] = MACFormatter.to_string(payload[1:7])
        
        elif dev_type == DEV_BUTTON and len(payload) >= 2:
            # Button: RSSI at byte 1
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
        
        elif dev_type == DEV_LIGHT and len(payload) >= 5:
            # Light: RSSI(1) + RGBW(1) + LED_COUNT(2 big-endian)
            result['rssi'] = MACFormatter.parse_rssi(payload[1])
            result['is_rgbw'] = payload[2] == 1
            result['num_leds'] = struct.unpack(">H", payload[3:5])[0]  # Big-endian
        
        return result
    
    def parse_button_event(self, payload: bytes) -> Optional[dict]:
        """Parse button event payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Dictionary with event info:
            {
                'action': ACT_CLICK/ACT_HOLDING/ACT_RELEASE/ACT_SYNC
            }
        """
        if len(payload) < 1:
            return None
        
        action = struct.unpack("<B", payload[0:1])[0]
        return {'action': action}
    
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
