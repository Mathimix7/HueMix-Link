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


class SerialGatewayTransport:
    """Handles framed packet I/O and handshake over USB serial."""

    def __init__(self, port: str, baudrate: int = 460800, timeout: float = 0.05):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[Any] = None
        self._lock = threading.RLock()
        self._available = False
        self._pending_handshake: Optional[Dict[str, str]] = None

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
        with self._lock:
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
        with self._lock:
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

        with self._lock:
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
        """Send one framed HueMix packet to the radio link."""
        if not self.available:
            return False
        if len(packet) != PACKET_SIZE:
            logger.error(f"Invalid packet size for serial transport: {len(packet)}")
            return False

        with self._lock:
            ser = self.serial
            if ser is None:
                return False
            try:
                ser.write(bytes([SERIAL_START]))
                ser.write(packet)
                ser.write(bytes([SERIAL_END]))
                ser.flush()
                return True
            except Exception as e:
                logger.error(f"Failed to send packet over serial gateway: {e}")
                return False

    def read_packet(self) -> Optional[bytes]:
        """Read one framed HueMix packet from serial stream."""
        if not self.available:
            return None

        with self._lock:
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
                        rest = ser.read(HANDSHAKE_STRUCT.size - 1)
                        if len(rest) == HANDSHAKE_STRUCT.size - 1:
                            handshake = self._parse_handshake_payload(b + rest)
                            if handshake:
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

                    payload = ser.read(PACKET_SIZE)
                    if len(payload) != PACKET_SIZE:
                        return None

                    footer = ser.read(1)
                    if not footer or footer[0] != SERIAL_END:
                        return None

                    return payload
            except Exception as e:
                logger.error(f"Serial gateway read error: {e}")
                self._available = False
                return None
