"""Serial transport for a direct USB-connected gateway radio node."""

import logging
import struct
import threading
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

SERIAL_START = 0xFE
SERIAL_END = 0xFD
SERIAL_HANDSHAKE = 0x11
SERIAL_REQ_HANDSHAKE = 0x12
HANDSHAKE_STRUCT = struct.Struct("<B6sBBB")
PACKET_SIZE = 203
MIN_FRAME_SIZE = 38
MAX_FRAME_SIZE = 1470


class SerialGatewayTransport:
    """Handles framed packet I/O and handshake over USB serial."""

    def __init__(self, port: str, baudrate: int = 460800, timeout: float = 0.05):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[Any] = None
        # Separate read/write locks so continuous read_packet() polling never stalls send_packet()
        self._read_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._available = False
        self._pending_handshake: Optional[Dict[str, str]] = None
        # Default to 4.x length-prefixed framing; auto-switches to legacy fixed 203 B
        # framing if a 3.x serial radio handshake is observed (and back to 4.x after OTA).
        self._use_length_prefix: bool = True

    def _update_framing_mode_from_handshake(self, handshake: Dict[str, str]) -> None:
        version_str = str(handshake.get('version') or '0.0.0')
        try:
            major = int(version_str.split('.')[0])
        except (ValueError, IndexError):
            major = 4
        new_mode = major >= 4
        if new_mode != self._use_length_prefix:
            logger.info(
                f"Serial gateway framing switched to "
                f"{'4.x length-prefixed' if new_mode else '3.x legacy fixed-203B'} "
                f"(radio v{version_str})"
            )
        self._use_length_prefix = new_mode

    @staticmethod
    def _parse_handshake_payload(data: bytes) -> Optional[Dict[str, str]]:
        """Parse raw handshake payload and return metadata dict."""
        if len(data) != HANDSHAKE_STRUCT.size:
            return None

        try:
            magic, mac_bytes, ver_major, ver_minor, ver_patch = HANDSHAKE_STRUCT.unpack(data)
        except struct.error:
            return None

        if magic != SERIAL_HANDSHAKE:
            return None

        radio_mac = ":".join(f"{b:02X}" for b in mac_bytes)
        version = f"{ver_major}.{ver_minor}.{ver_patch}"
        return {
            'radio_mac': radio_mac,
            'version': version,
        }

    def pop_pending_handshake(self) -> Optional[Dict[str, str]]:
        """Return and clear a handshake observed during stream reads."""
        with self._state_lock:
            handshake = self._pending_handshake
            self._pending_handshake = None
            return handshake

    def connect(self) -> bool:
        """Open serial port and prepare transport."""
        try:
            import serial  # type: ignore
        except Exception:
            logger.error("pyserial is required for USB serial gateway support")
            return False

        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=1.0,
            )
            self._available = True
            logger.info(f"Serial gateway connected on {self.port} @ {self.baudrate}")
            return True
        except Exception as e:
            logger.error(f"Failed to open serial gateway port {self.port}: {e}")
            self.serial = None
            self._available = False
            return False

    def close(self):
        """Close serial port."""
        with self._write_lock, self._read_lock:
            if self.serial:
                try:
                    self.serial.close()
                except Exception:
                    pass
            self.serial = None
            self._available = False

    @property
    def available(self) -> bool:
        return self._available and self.serial is not None

    def request_handshake(self, attempts: int = 50, interval_s: float = 0.1, initial_delay_s: float = 0.2) -> Optional[Dict]:
        """Request and parse a radio handshake frame.
        
        Sends multiple handshake requests with delays to allow radio to boot and respond.
        Total timeout: initial_delay_s + attempts * interval_s (default: 5.2 seconds)

        Args:
            attempts: Number of handshake request attempts
            interval_s: Delay in seconds between attempts
            initial_delay_s: Initial delay before first attempt to allow radio to boot

        Returns:
            {'radio_mac': str, 'version': str} or None
        """
        if not self.available:
            logger.warning("Serial transport not available for handshake")
            return None

        # Wait briefly for radio to be ready after connection
        if initial_delay_s > 0:
            time.sleep(initial_delay_s)

        with self._write_lock, self._read_lock:
            ser = self.serial
            if ser is None:
                return None
            try:
                # Clear stale bytes before requesting a fresh handshake.
                ser.reset_input_buffer()
            except Exception:
                pass

            for attempt in range(attempts):
                try:
                    ser.write(bytes([SERIAL_REQ_HANDSHAKE]))
                    ser.flush()
                except Exception as e:
                    logger.error(f"Failed to send handshake request: {e}")
                    return None

                # Try to read the 10-byte handshake response
                data = ser.read(HANDSHAKE_STRUCT.size)
                
                if len(data) == HANDSHAKE_STRUCT.size:
                    handshake = self._parse_handshake_payload(data)
                    if not handshake:
                        logger.warning("Failed to parse handshake payload")
                        continue

                    self._update_framing_mode_from_handshake(handshake)
                    logger.info(
                        f"Serial radio handshake received: {handshake['radio_mac']} v{handshake['version']}"
                    )
                    return handshake
                else:
                    # Partial or no response, retry after delay
                    if len(data) > 0:
                        logger.debug(f"Handshake attempt {attempt + 1}: got {len(data)} bytes, expected {HANDSHAKE_STRUCT.size}")
                    
                    if interval_s > 0 and attempt < attempts - 1:
                        time.sleep(interval_s)

            logger.warning(f"Handshake timeout after {attempts} attempts ({initial_delay_s + attempts * interval_s:.1f}s total)")
            return None

    def send_packet(self, packet: bytes) -> bool:
        """Send one length-prefixed HueMix packet (v1 203 B or v2 39..1470 B) to the radio link.

        Wire format (§4.4): 0xFE | len_lo | len_hi | payload(len) | 0xFD
        """
        if not self.available:
            return False
        pkt_len = len(packet)
        if pkt_len < MIN_FRAME_SIZE or pkt_len > MAX_FRAME_SIZE:
            logger.error(f"Invalid packet size for serial transport: {pkt_len}")
            return False

        with self._write_lock:
            ser = self.serial
            if ser is None:
                return False
            try:
                if self._use_length_prefix:
                    frame = struct.pack("<BH", SERIAL_START, pkt_len) + packet + bytes([SERIAL_END])
                else:
                    if pkt_len != PACKET_SIZE:
                        logger.warning(f"Cannot send {pkt_len}-byte v2 frame over 3.x legacy serial gateway")
                        return False
                    frame = bytes([SERIAL_START]) + packet + bytes([SERIAL_END])
                ser.write(frame)
                return True
            except Exception as e:
                logger.error(f"Failed to send packet over serial gateway: {e}")
                return False

    def send_dashboard_line(self, line: str) -> bool:
        """Send one ASCII dashboard line to serial gateway OLED service."""
        if not self.available:
            return False

        payload = (line.rstrip("\n") + "\n").encode("ascii", errors="ignore")

        with self._write_lock:
            ser = self.serial
            if ser is None:
                return False
            try:
                ser.write(payload)
                return True
            except Exception as e:
                logger.error(f"Failed to send dashboard line over serial gateway: {e}")
                return False

    @staticmethod
    def _read_exact(ser: Any, count: int, deadline: float) -> bytes:
        """Read exactly `count` bytes before `deadline`, tolerating short serial timeouts."""
        chunks = bytearray()
        while len(chunks) < count:
            if time.monotonic() >= deadline:
                break
            chunk = ser.read(count - len(chunks))
            if chunk:
                chunks.extend(chunk)
        return bytes(chunks)

    def read_packet(self) -> Optional[bytes]:
        """Read one framed HueMix packet from serial stream."""
        if not self.available:
            return None

        with self._read_lock:
            ser = self.serial
            if ser is None:
                return None
            try:
                while True:
                    b = ser.read(1)
                    if not b:
                        return None
                    if b[0] == SERIAL_HANDSHAKE:
                        # Radio can emit handshake asynchronously after reboot.
                        # Capture it so the server can re-sync runtime state.
                        rest = self._read_exact(ser, HANDSHAKE_STRUCT.size - 1, time.monotonic() + 0.25)
                        if len(rest) == HANDSHAKE_STRUCT.size - 1:
                            handshake = self._parse_handshake_payload(b + rest)
                            if handshake:
                                self._update_framing_mode_from_handshake(handshake)
                                with self._state_lock:
                                    self._pending_handshake = handshake
                                logger.info(
                                    f"Serial radio runtime handshake received: "
                                    f"{handshake['radio_mac']} v{handshake['version']}"
                                )
                                return None
                        else:
                            logger.debug("Incomplete runtime handshake payload received on serial stream")
                        continue
                    if b[0] != SERIAL_START:
                        continue

                    frame_deadline = time.monotonic() + 0.25
                    if self._use_length_prefix:
                        len_bytes = self._read_exact(ser, 2, frame_deadline)
                        if len(len_bytes) != 2:
                            logger.warning("Serial frame timeout reading length header; discarding")
                            return None
                        pkt_len = struct.unpack("<H", len_bytes)[0]
                        if pkt_len < MIN_FRAME_SIZE or pkt_len > MAX_FRAME_SIZE:
                            logger.warning(f"Invalid serial frame length {pkt_len}; resyncing")
                            continue
                    else:
                        pkt_len = PACKET_SIZE

                    payload = self._read_exact(ser, pkt_len, frame_deadline)
                    if len(payload) != pkt_len:
                        logger.warning(f"Serial frame truncated ({len(payload)}/{pkt_len} B); discarding")
                        return None

                    footer = self._read_exact(ser, 1, frame_deadline)
                    if not footer or footer[0] != SERIAL_END:
                        logger.warning("Serial frame footer mismatch; discarding packet")
                        continue

                    return payload
            except Exception as e:
                logger.error(f"Serial gateway read error: {e}")
                self._available = False
                return None
