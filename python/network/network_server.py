"""
UDP network server for HueMixLink protocol.

Handles UDP socket communication, packet routing, gateway mesh management,
and delivery tracking with automatic failover.
"""
import socket
import threading
import queue
import logging
import secrets
import time
from typing import Dict, Optional, List, Tuple
from datetime import datetime
import struct
from services.data_manager import data_manager
from services.config_manager import config_manager
from services.config_change_notifier import config_notifier
from services.home_id_manager import home_id_manager
from services.ota_manager import ota_manager, OTAState
from services.hue_service import hue_service
from constants import (
    DEFAULT_UDP_IP, DEFAULT_UDP_PORT, DEFAULT_GATEWAY_PORT,
    PKT_HELLO, PKT_BTN_EVENT, PKT_DELIVERY_RPT, PKT_GW_LIST_UPD, PKT_PING, PKT_PING_DEVICE, PKT_MOTION_EVENT, PKT_DOOR_EVENT,
    PKT_SYS_CMD,
    PKT_OTA_NOTIFY, PKT_OTA_READY, PKT_OTA_CHUNK, PKT_OTA_COMPLETE, PKT_OTA_ABORT, PKT_OTA_CHUNK_ACK, PKT_OTA_CHECKPOINT_REQ,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, DEV_MOTION, DEV_DOOR, DEV_PLUGIN_MARKER,
    MAX_GATEWAY_ATTEMPTS, GATEWAY_DELIVERY_TIMEOUT_SECONDS,
    TIMEOUT_SOCKET, OTA_READY_TIMEOUT, OTA_CHUNK_DATA_SIZE,
    OTA_CHUNK_ACK_TIMEOUT, OTA_CHUNK_MAX_RETRIES, OTA_CHECKPOINT_INTERVAL,
    OTA_POST_UPDATE_TIMEOUT, OTA_MIN_BATTERY_PERCENT,
    RSSI_AUTO_PAIR_THRESHOLD, MAX_GATEWAYS_PER_PACKET,
    FILE_GATEWAYS, FILE_LIGHTSTRIPS, FILE_MOTION_SENSORS,
    CMD_SET_MOTION_COOLDOWN, CMD_SET_MOTION_SLEEP, CMD_NIGHT_MODE_ON, CMD_NIGHT_MODE_OFF, CMD_NETNODE_WIFI_STATUS,
    CMD_UPGRADE_TO_V2, CMD_SET_PIN, CMD_SET_PIN_ACK, PIN_ACK_OK, PIN_ACK_ALREADY_SET,
    CRYPTO_MODE_V1, CRYPTO_MODE_V2, CRYPTO_MODE_V2_UNVERIFIED, PACKET_TOTAL_SIZE,
    V2_UDP_BUFFER_SIZE, V2_OVERHEAD, V2_MIN_FRAME_SIZE, V2_MAX_FRAME_ESP8266, V2_MAX_FRAME_ESP32, PROTO_VERSION_V2, PKT_PAIR_ACK,
)
from network.pairing_manager import pairing_manager
from .packet_protocol import PacketEncoder, PacketDecoder, PacketEncoderV2, PacketDecoderV2, MACFormatter, decode_dual
from .security_store import security_store
from .pairing_v2 import pairing_v2_manager, encode_key_distrib, KEY_DISTRIB_UPSERT, KEY_DISTRIB_REMOVE
from .device_manager import device_manager
from .serial_gateway_transport import SerialGatewayTransport

try:
    import psutil  # type: ignore
except Exception as e:
    print(f"Error importing psutil: {e}")
    psutil = None

logger = logging.getLogger(__name__)


class NetworkServer:
    """UDP network server managing device communication and routing."""
    
    def __init__(self, 
                 udp_ip: str = DEFAULT_UDP_IP,
                 udp_port: int = DEFAULT_UDP_PORT,
                 gateway_port: int = DEFAULT_GATEWAY_PORT,
                 home_id: int = home_id_manager.get_or_create_home_id()):
        """Initialize network server.
        
        Args:
            udp_ip: IP to bind UDP socket
            udp_port: Port for receiving packets
            gateway_port: Port for sending to gateways
            home_id: HomeID for paired devices (unpaired use 0x00000000)
        """
        if home_id is None:
            home_id = home_id_manager.get_or_create_home_id()

        self.udp_ip = udp_ip
        self.udp_port = udp_port
        self.gateway_port = gateway_port
        self.home_id = home_id
        
        self.encoder = PacketEncoder(home_id)
        self.decoder = PacketDecoder(home_id)
        
        # Socket and threading
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None
        self.serial_receive_thread: Optional[threading.Thread] = None
        self._serial_handshake_thread: Optional[threading.Thread] = None
        self._serial_handshake_lock = threading.Lock()
        self._netnode_status_thread: Optional[threading.Thread] = None
        self._serial_dashboard_thread: Optional[threading.Thread] = None
        self.worker_threads: List[threading.Thread] = []
        self.packet_queue = queue.Queue()

        # Optional USB serial gateway transport
        self._serial_transport: Optional[SerialGatewayTransport] = None
        self._serial_gateway_radio_mac: Optional[str] = None
        self._serial_endpoint: Optional[str] = None
        self._serial_port: Optional[str] = None
        self._serial_baudrate: int = 460800
        self._last_serial_port_present: Optional[bool] = None
        self._last_netnode_wifi_status: Optional[int] = None
        self._process_start_time = time.time()
        self._cached_server_ip = "0.0.0.0"
        self._last_server_ip_refresh = 0.0
        
        # Gateway routing table: radio_mac -> {ip_address, wifi_mac, last_seen}
        self._gateway_table: Dict[str, Dict] = {}
        self._gateway_lock = threading.RLock()
        
        # Delivery tracking: msg_id -> {light_mac, gateway_chain, attempt, timestamp}
        self._pending_deliveries: Dict[int, Dict] = {}
        self._delivery_lock = threading.RLock()
        self._next_msg_id = 1
        
        # Ping response tracking: gateway_mac -> {event, uptime, timestamp}
        self._pending_pings: Dict[str, Dict] = {}
        self._ping_lock = threading.RLock()
        
        # Device ping response tracking: device_mac -> {event, rssi_map, timestamp}
        self._pending_device_pings: Dict[str, Dict] = {}
        self._device_ping_lock = threading.RLock()
        
        # Serial handshake deduplication: radio_mac -> last_handshake_time
        self._last_handshake_time: Dict[str, float] = {}
        self._handshake_dedup_lock = threading.RLock()
        
        # OTA chunk ACK tracking: device_mac -> {event, last_chunk_index}
        self._ota_ack_events: Dict[str, Dict] = {}
        self._ota_ack_lock = threading.RLock()
        self._ota_queue_lock = threading.Lock()
        ota_manager.set_slot_released_callback(self._schedule_ota_queue_processing)
        
        # Gateway LED state tracking
        self._gateway_leds_enabled = True
        self._led_state_lock = threading.Lock()
        self._led_scheduler_thread: Optional[threading.Thread] = None
        self._last_led_hour = -1  # Track last hour LEDs were checked
        
        # Event handlers
        self._button_event_handler = None
        self._motion_event_handler = None
        self._door_event_handler = None
        self._pairing_handler = None
        self._plugin_manager = None
        
        # Load persisted gateways
        self._load_gateways()

        # Automation engine reference
        self.automation_engine = None
        
        # Subscribe to config changes for UDP port
        self._subscribe_to_config_changes()
        
        logger.info(f"NetworkServer initialized (UDP:{udp_port}, Gateway:{gateway_port})")
    
    def _subscribe_to_config_changes(self):
        """Subscribe to configuration changes for automatic restart."""
        config_notifier.subscribe('udp_port_changed', self._on_udp_port_changed)
        config_notifier.subscribe('gateways_reload', self.reload_gateways)
        config_notifier.subscribe('gateway_deleted', self._on_gateway_deleted)
        config_notifier.subscribe('serial_gateway_config_changed', self._on_serial_gateway_config_changed)
        home_id_manager.subscribe(self._on_home_id_changed)
    
    def _on_home_id_changed(self, new_id):
        """Handle HOME_ID change notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        try:
            self.home_id = new_id
            if hasattr(self, 'encoder'):
                self.encoder.home_id = new_id
            if hasattr(self, 'decoder'):
                self.decoder.home_id = new_id
            logger.info(f"NetworkServer updated HOME_ID -> {new_id}")

            serial_mac = self._serial_gateway_radio_mac
            if serial_mac and self._serial_transport and self._serial_transport.available:
                self._sync_serial_gateway_home_id(serial_mac)
        except Exception:
            logger.exception('Error applying HOME_ID change to NetworkServer')

    def _on_udp_port_changed(self, notification):
        """Handle UDP port change notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        new_port = notification['data'].get('new_port')
        old_port = notification['data'].get('old_port')
        
        if new_port and new_port != old_port:
            logger.info(f"UDP port changed from {old_port} to {new_port}, restarting server...")
            # Restart in a separate thread to avoid blocking the config notification
            threading.Thread(target=self.restart, args=(new_port,), daemon=True, name="NetworkServer-Restart").start()
    
    def _on_gateway_deleted(self, notification):
        """Handle gateway deletion notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        wifi_mac = notification['data'].get('wifi_mac')
        radio_mac = notification['data'].get('radio_mac')
        
        # Run deletion in background thread to avoid blocking Flask request
        threading.Thread(
            target=self._handle_gateway_deletion_async,
            args=(wifi_mac, radio_mac),
            daemon=True,
            name="Gateway-Deletion"
        ).start()

    def _on_serial_gateway_config_changed(self, notification):
        """Apply serial gateway config changes without requiring backend restart."""
        new_cfg = (notification or {}).get('data', {}).get('new', {})
        threading.Thread(
            target=self._apply_serial_gateway_config_change,
            args=(new_cfg,),
            daemon=True,
            name="Serial-Gateway-Config-Apply",
        ).start()

    def _apply_serial_gateway_config_change(self, serial_cfg: Optional[Dict]):
        """Apply serial gateway configuration at runtime."""
        serial_cfg = serial_cfg or {}
        enabled = bool(serial_cfg.get('enabled', False))
        serial_port = str(serial_cfg.get('port', '') or '').strip()
        baudrate = int(serial_cfg.get('baudrate', 460800) or 460800)

        if not enabled or not serial_port:
            self._serial_port = None
            self._serial_baudrate = baudrate
            self._last_serial_port_present = None
            self._drop_serial_transport(clear_gateway_entry=True)
            self.reload_gateways()
            logger.info("Serial gateway disabled via config change")
            return

        port_changed = (self._serial_port or '').upper() != serial_port.upper()
        baud_changed = self._serial_baudrate != baudrate

        self._serial_port = serial_port
        self._serial_baudrate = baudrate
        self._last_serial_port_present = None

        if port_changed or baud_changed:
            self._drop_serial_transport(clear_gateway_entry=True)
            logger.info(f"Serial gateway reconfigured to {serial_port} @ {baudrate}")

        if self.running:
            self._start_serial_gateway_transport()
            self.reload_gateways()
        else:
            logger.info("Serial gateway config stored; it will be applied when NetworkServer starts")
    
    def _handle_gateway_deletion_async(self, wifi_mac: str, radio_mac: str):
        """Async handler for gateway deletion (runs in background thread).
        
        Args:
            wifi_mac: Gateway WiFi MAC address
            radio_mac: Gateway radio MAC address
        """
        if radio_mac:
            self.remove_gateway_from_table(radio_mac)
        elif wifi_mac:
            # Find radio_mac by wifi_mac
            with self._gateway_lock:
                for r_mac, info in list(self._gateway_table.items()):
                    if info['wifi_mac'] == wifi_mac:
                        self.remove_gateway_from_table(r_mac)
                        break
    
    def set_button_event_handler(self, handler):
        """Set callback for button events.
        
        Args:
            handler: Callable(button_mac, action, rssi)
        """
        self._button_event_handler = handler
    
    def set_motion_event_handler(self, handler):
        """Set callback for motion sensor events.
        
        Args:
            handler: Callable(sensor_mac, action, light_level, battery_mv)
        """
        self._motion_event_handler = handler

    def set_door_event_handler(self, handler):
        """Set callback for door sensor events.

        Args:
            handler: Callable(sensor_mac, action, light_level, battery_mv)
        """
        self._door_event_handler = handler
    
    def set_pairing_handler(self, handler):
        """Set callback for pairing requests.
        
        Args:
            handler: Callable(device_mac, device_type, rssi) -> bool (accept pairing)
        """
        self._pairing_handler = handler

    def set_plugin_manager(self, manager):
        self._plugin_manager = manager

    def send_pair_confirm(self, target_mac: str, sender_ip: str) -> bool:
        """Public wrapper for pairing confirmation packets."""
        try:
            self._send_pair_confirm(target_mac, sender_ip)
            return True
        except Exception:
            logger.exception("Failed to send pair confirm to %s", target_mac)
            return False
        
    def get_message_id(self) -> int:
        """Generate a new message ID for tracking deliveries."""
        msg_id = 0
        with self._delivery_lock:
            msg_id = self._next_msg_id
            self._next_msg_id = (self._next_msg_id + 1) % 256
        return msg_id

    def send_raw_packet_to_device(self, target_mac: str, packet: bytes, wait_for_delivery: bool = False, msg_id: int = None, gateway_preference: str = None) -> bool:
        """Send a raw packet to a device with gateway fallback.

        Args:
            target_mac: Device MAC address
            packet: Encoded packet bytes
            wait_for_delivery: Whether to wait for delivery confirmation
            msg_id: Optional message ID for delivery tracking
            gateway_preference: Preferred gateway MAC address

        Returns:
            True if packet was sent successfully, False otherwise.
        """

        return self._send_with_fallback(target_mac, packet, wait_for_delivery, msg_id, gateway_preference)

    def send_packet_to_device(
        self,
        target_mac: str,
        pkt_type: int,
        payload: bytes,
        wait_for_delivery: bool = False,
        msg_id: Optional[int] = None,
        gateway_preference: Optional[str] = None,
    ) -> bool:
        """Dual-stack v1/v2 packet sender for plugins and custom device protocols.

        Automatically encrypts `payload` with AES-128-GCM (Security v2) when `target_mac`
        is v2-paired and `security_v2_send` is enabled, or falls back to a legacy 203-byte
        FNV-signed v1 frame when `target_mac` is still on v1.
        """
        raw_payload = bytes(payload or b"")
        if wait_for_delivery and msg_id is None:
            msg_id = self.get_message_id()
        effective_msg_id = 0 if msg_id is None else (int(msg_id) & 0xFF)

        if self._use_v2_for_peer(target_mac):
            max_pt = security_store.get_max_frame(target_mac) - V2_OVERHEAD
            if len(raw_payload) > max_pt:
                logger.error(
                    f"Plugin v2 payload for {target_mac} (type={pkt_type:#x}) too large: "
                    f"{len(raw_payload)} > {max_pt} B"
                )
                return False
            if len(raw_payload) == 0:
                raw_payload = b"\x00"
            packet = self._encode_v2_for_peer(pkt_type, target_mac, raw_payload, msg_id=effective_msg_id)
        else:
            if len(raw_payload) > 185:
                logger.error(
                    f"Plugin v1 payload for {target_mac} (type={pkt_type:#x}) too large: "
                    f"{len(raw_payload)} > 185 B"
                )
                return False
            packet = self.encoder.encode_custom(pkt_type, target_mac, raw_payload, msg_id=effective_msg_id)

        return self._send_with_fallback(
            target_mac,
            packet,
            wait_for_delivery=wait_for_delivery,
            msg_id=effective_msg_id if wait_for_delivery else msg_id,
            gateway_preference=gateway_preference,
        )

    def set_automation_engine(self, engine):
        """Set reference to the automation engine.
        
        Args:
            engine: AutomationEngine instance
        """
        self.automation_engine = engine
    
    def _load_gateways(self):
        """Load persisted gateways from gateways.json into gateway table."""        
        gateways = data_manager.read_json(FILE_GATEWAYS, default=[])
        
        with self._gateway_lock:
            self._gateway_table.clear()
            for gateway in gateways:
                radio_mac = gateway.get('radio_mac')
                wifi_mac = gateway.get('mac_address')
                ip = gateway.get('ip_address') or gateway.get('transport_endpoint')
                
                if radio_mac and wifi_mac and ip:
                    self._gateway_table[radio_mac] = {
                        'ip_address': ip,
                        'wifi_mac': wifi_mac,
                        'last_seen': datetime.now()
                    }
                    logger.debug(f"Loaded gateway from storage: {radio_mac} at {ip}")
        
        if self._gateway_table:
            logger.info(f"Loaded {len(self._gateway_table)} gateway(s) from storage")
    
    def reload_gateways(self, notification=None):
        """Reload gateways from storage into gateway table."""
        self._load_gateways()
        # After reloading gateways, broadcast updated list to all gateways and paired devices
        threading.Thread(
            target=self._broadcast_gateway_list,
            args=(False,),
            daemon=True,
            name="Reload-GW-Broadcast"
        ).start()
        logger.info("Reloaded gateways and triggered broadcast to all devices")

    def remove_gateway_from_table(self, radio_mac: str):
        """Remove a gateway from the routing table.
        
        Args:
            radio_mac: Gateway radio MAC address to remove
        """
        with self._gateway_lock:
            if radio_mac in self._gateway_table:
                wifi_mac = self._gateway_table[radio_mac]['wifi_mac']
                ip = self._gateway_table[radio_mac]['ip_address']
                del self._gateway_table[radio_mac]
                logger.info(f"Removed gateway from routing table: WiFi={wifi_mac}, Radio={radio_mac}, IP={ip}")
                
                # Broadcast updated gateway list to remaining gateways and devices
                if self._gateway_table:
                    self._broadcast_gateway_list(only_gateways=False)
            else:
                logger.warning(f"Gateway {radio_mac} not found in routing table")
    
    def start(self):
        """Start the UDP server."""
        if self.running:
            logger.warning("NetworkServer already running")
            return
        
        try:
            # Create UDP socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.udp_ip, self.udp_port))
            self.sock.settimeout(TIMEOUT_SOCKET)  # timeout for clean shutdown
            
            self.running = True
            
            # Start receive thread
            self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.receive_thread.start()

            # Start optional serial gateway transport (if configured)
            self._start_serial_gateway_transport()

            # Watch host connectivity state and sync cmd=3 on status changes.
            self._netnode_status_thread = threading.Thread(
                target=self._netnode_wifi_status_loop,
                daemon=True,
                name='NetNode-WiFi-Status',
            )
            self._netnode_status_thread.start()

            self._serial_dashboard_thread = threading.Thread(
                target=self._serial_dashboard_loop,
                daemon=True,
                name='Serial-Dashboard',
            )
            self._serial_dashboard_thread.start()
            
            # Start worker threads (4 workers)
            for i in range(4):
                worker = threading.Thread(target=self._worker_loop, daemon=True, name=f"Worker-{i}")
                worker.start()
                self.worker_threads.append(worker)
            
            # Start LED scheduler thread
            self._led_scheduler_thread = threading.Thread(target=self._led_scheduler_loop, daemon=True, name="LED-Scheduler")
            self._led_scheduler_thread.start()
            
            # Start OTA timeout monitor thread
            self._ota_timeout_thread = threading.Thread(target=self._ota_timeout_monitor, daemon=True, name="OTA-Timeout-Monitor")
            self._ota_timeout_thread.start()
            
            # Perform initial LED sync for all gateways
            threading.Thread(target=self._initial_led_sync, daemon=True, name="Initial-LED-Sync").start()
            
            logger.info(f"NetworkServer started on {self.udp_ip}:{self.udp_port}")
            
        except Exception as e:
            logger.error(f"Failed to start NetworkServer: {e}")
            self.running = False
            raise
    
    def stop(self):
        """Stop the UDP server."""
        logger.info("Stopping NetworkServer...")
        self.running = False
        
        # Wait for threads
        if self.receive_thread:
            self.receive_thread.join(timeout=2.0)

        if self._netnode_status_thread:
            self._netnode_status_thread.join(timeout=2.0)

        if self._serial_dashboard_thread:
            self._serial_dashboard_thread.join(timeout=2.0)
        
        for worker in self.worker_threads:
            worker.join(timeout=2.0)
        
        # Close socket
        if self.sock:
            self.sock.close()
            self.sock = None

        # Close serial transport
        self._drop_serial_transport(clear_gateway_entry=False)
        
        logger.info("NetworkServer stopped")

    def _start_serial_gateway_transport(self):
        """Start USB serial gateway transport if enabled in configuration.
        
        Starts a supervisor thread that keeps retrying connect/handshake on failures.
        """
        serial_cfg = config_manager.get_serial_gateway_config()
        if serial_cfg.get('enabled'):
            serial_port = str(serial_cfg.get('port', '') or '').strip()
            if not serial_port:
                logger.warning("Serial gateway enabled but no serial port is configured")
                return
            self._serial_port = serial_port
            self._serial_baudrate = int(serial_cfg.get('baudrate', 460800) or 460800)
        elif not self._serial_port:
            return

        with self._serial_handshake_lock:
            if self._serial_handshake_thread and self._serial_handshake_thread.is_alive():
                return

            # Start handshake supervisor in background to avoid blocking startup.
            self._serial_handshake_thread = threading.Thread(
                target=self._async_serial_gateway_handshake,
                daemon=True,
                name='Serial-Handshake',
            )
            self._serial_handshake_thread.start()

    def _drop_serial_transport(self, clear_gateway_entry: bool = True):
        """Close and clear active serial transport state.

        Args:
            clear_gateway_entry: Remove stale serial endpoint from gateway table.
        """
        old_radio_mac = self._serial_gateway_radio_mac

        if self._serial_transport:
            try:
                self._serial_transport.close()
            except Exception:
                pass

        self._serial_transport = None
        self._serial_endpoint = None

        if clear_gateway_entry and old_radio_mac:
            with self._gateway_lock:
                if old_radio_mac in self._gateway_table:
                    del self._gateway_table[old_radio_mac]

        self._serial_gateway_radio_mac = None

    def _is_configured_serial_port_available(self, serial_port: str) -> bool:
        """Return True only when the configured serial port currently exists."""
        try:
            from serial.tools import list_ports  # type: ignore
            available = {p.device.upper() for p in list_ports.comports()}
            exists = serial_port.upper() in available
        except Exception as e:
            # If port listing is unavailable, fall back to trying connect.
            logger.debug(f"Serial port scan unavailable, falling back to connect attempts: {e}")
            return True

        if self._last_serial_port_present is None or self._last_serial_port_present != exists:
            if exists:
                logger.info(f"Serial port detected: {serial_port}")
            else:
                logger.warning(f"Serial port not present, waiting: {serial_port}")
            self._last_serial_port_present = exists

        return exists

    def _async_serial_gateway_handshake(self):
        """Perform serial gateway handshake asynchronously in background thread.
        
        Keeps retrying connect/handshake while server is running.
        """
        max_attempts = 3

        while self.running:
            serial_port = self._serial_port
            baudrate = self._serial_baudrate

            if not serial_port:
                time.sleep(1.0)
                continue

            # If already connected and healthy, idle briefly.
            if self._serial_transport and self._serial_transport.available and self._serial_gateway_radio_mac:
                active_port = (self._serial_endpoint or '').replace('serial://', '', 1)
                if active_port and active_port.upper() != serial_port.upper():
                    logger.info(
                        f"Serial port changed from {active_port} to {serial_port}; reconnecting transport"
                    )
                    self._drop_serial_transport(clear_gateway_entry=True)
                    continue
                time.sleep(1.0)
                continue

            # Only attempt reconnect when configured port is currently present.
            if not self._is_configured_serial_port_available(serial_port):
                time.sleep(2.0)
                continue

            attempt = 1
            connected = False

            while attempt <= max_attempts and self.running:
                logger.info(f"Serial gateway handshake attempt {attempt}/{max_attempts} on {serial_port} @ {baudrate} baud")

                self._drop_serial_transport(clear_gateway_entry=False)
            
                transport = SerialGatewayTransport(
                    port=serial_port,
                    baudrate=baudrate,
                )
                if not transport.connect():
                    logger.error(f"Failed to connect to serial port {serial_port}")
                    if attempt < max_attempts:
                        logger.info("Retrying in 5 seconds...")
                        time.sleep(5)
                    attempt += 1
                    continue

                logger.debug("Serial port connected, attempting handshake...")
                handshake = transport.request_handshake()
            
                if handshake:
                    if not self._register_serial_gateway_from_handshake(handshake, transport, serial_port):
                        transport.close()
                        break

                    radio_mac = handshake.get('radio_mac')
                    endpoint = self._serial_endpoint

                    if not self.serial_receive_thread or not self.serial_receive_thread.is_alive():
                        self.serial_receive_thread = threading.Thread(
                            target=self._serial_receive_loop,
                            daemon=True,
                            name='Serial-Receive',
                        )
                        self.serial_receive_thread.start()

                    logger.info(f"Serial gateway transport active: {radio_mac} via {endpoint}")
                    connected = True
                    break
            
                logger.warning(f"Serial gateway handshake attempt {attempt} failed on {serial_port}")
                transport.close()
                if attempt < max_attempts:
                    wait_time = 3
                    logger.info(f"Retrying handshake in {wait_time} seconds (attempt {attempt + 1}/{max_attempts})...")
                    time.sleep(wait_time)
                attempt += 1

            if not connected and self.running:
                logger.error(f"Serial gateway handshake failed after {max_attempts} attempts on {serial_port}; retrying in 5s")
                time.sleep(5)

    def _register_serial_gateway_from_handshake(
        self,
        handshake: Dict[str, str],
        transport: SerialGatewayTransport,
        serial_port: str,
    ) -> bool:
        """Register serial gateway endpoint and persist metadata from handshake."""
        radio_mac = handshake.get('radio_mac')
        if not radio_mac:
            logger.error("Serial gateway handshake missing radio MAC address")
            return False

        endpoint = f"serial://{serial_port}"
        transport_changed = (
            self._serial_transport is not transport
            or self._serial_endpoint != endpoint
            or self._serial_gateway_radio_mac != radio_mac
        )

        # Deduplicate: ignore redundant handshakes within 2 seconds of last one
        # (firmware sends 5 rapid handshakes for redundancy)
        current_time = time.time()
        with self._handshake_dedup_lock:
            last_time = self._last_handshake_time.get(radio_mac)
            if last_time and (current_time - last_time) < 2.0 and not transport_changed:
                logger.debug(f"Ignoring duplicate handshake from {radio_mac} (received {current_time - last_time:.1f}s after last)")
                return True
            self._last_handshake_time[radio_mac] = current_time

        self._serial_transport = transport
        self._serial_gateway_radio_mac = radio_mac
        self._serial_endpoint = endpoint

        gateway = device_manager.update_gateway(
            wifi_mac=radio_mac,
            radio_mac=radio_mac,
            ip_address=endpoint,
            version_net='serial-host',
            version_radio=handshake.get('version'),
        )
        if gateway:
            gateways = data_manager.read_json(FILE_GATEWAYS, default=[])
            for gw in gateways:
                if gw.get('radio_mac', '').upper() == radio_mac.upper():
                    gw['transport'] = 'usb_serial'
                    gw['transport_endpoint'] = endpoint
                    break
            data_manager.write_json(FILE_GATEWAYS, gateways)

        with self._gateway_lock:
            self._gateway_table[radio_mac] = {
                'ip_address': endpoint,
                'wifi_mac': radio_mac,
                'last_seen': datetime.now(),
            }

        # For serial-radio OTA, reboot confirmation arrives as a serial handshake
        # (not a HELLO packet). Accept this as post-update validation.
        session = ota_manager.get_session(radio_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = str(handshake.get('version', '0.0.0'))

            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS (SERIAL-RADIO): {radio_mac} now running {current_version}")
                ota_manager.update_session_state(radio_mac, OTAState.COMPLETE, "Firmware validated successfully via serial handshake",)
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED (SERIAL-RADIO): {radio_mac} - {error_msg}")
                ota_manager.update_session_state(radio_mac, OTAState.FAILED, error_msg)

        self._sync_serial_gateway_home_id(radio_mac)
        self._resync_serial_gateway_runtime_state(radio_mac)
        return True

    def _sync_serial_gateway_home_id(self, radio_mac: str) -> bool:
        """Push the current HOME_ID to the direct serial radio node (v1 only; v2 gateways receive HOME_ID during ECDH)."""
        if not self._serial_transport or not self._serial_transport.available:
            return False

        endpoint = self._serial_endpoint
        if not endpoint:
            return False

        if self._use_v2_for_peer(radio_mac):
            logger.debug(f"Skipping legacy v1 HOME_ID sync for v2-paired serial radio {radio_mac}")
            return True

        packet = self.encoder.encode_pair_confirm('00:00:00:00:00:00', self.home_id)
        success = self._send_packet_to_gateway(endpoint, packet)
        if success:
            logger.info(f"Synced HOME_ID to serial radio {radio_mac}: {self.home_id}")
        else:
            logger.warning(f"Failed to sync HOME_ID to serial radio {radio_mac}")
        return success

    def _resync_serial_gateway_runtime_state(self, radio_mac: str):
        """Re-apply host state to radio after (re)handshake/reboot."""
        self._last_netnode_wifi_status = None
        self._sync_netnode_wifi_status(radio_mac)

        with self._led_state_lock:
            leds_enabled = self._gateway_leds_enabled
        self.set_gateway_leds(radio_mac, leds_enabled)

        # Restore routing list in radio and in paired devices after radio reboot.
        self._broadcast_gateway_list(only_gateways=False)

        # Sync all v2 device K_ack keys into the serial gateway's in-RAM peer table
        # so it can immediately ACK v2 buttons/remotes/sensors after boot or reconnect.
        if self._serial_endpoint and security_store.is_v2_device(radio_mac):
            self._sync_all_v2_keys_to_gateway(radio_mac, self._serial_endpoint)


    def _serial_receive_loop(self):
        """Receive framed packets from the USB serial radio transport."""
        logger.info("Serial receive loop started")

        while self.running and self._serial_transport and self._serial_endpoint:
            handshake = self._serial_transport.pop_pending_handshake()
            if handshake:
                logger.info(
                    f"Runtime serial handshake detected from {handshake.get('radio_mac')}, re-syncing gateway state"
                )
                runtime_serial_port = (self._serial_port or '').strip()
                if not runtime_serial_port and self._serial_endpoint and self._is_serial_endpoint(self._serial_endpoint):
                    runtime_serial_port = self._serial_endpoint.replace('serial://', '', 1)
                self._register_serial_gateway_from_handshake(
                    handshake,
                    self._serial_transport,
                    runtime_serial_port,
                )
                continue

            packet = self._serial_transport.read_packet()
            handshake = self._serial_transport.pop_pending_handshake()
            if handshake:
                logger.info(
                    f"Runtime serial handshake detected from {handshake.get('radio_mac')}, re-syncing gateway state"
                )
                runtime_serial_port = (self._serial_port or '').strip()
                if not runtime_serial_port and self._serial_endpoint and self._is_serial_endpoint(self._serial_endpoint):
                    runtime_serial_port = self._serial_endpoint.replace('serial://', '', 1)
                self._register_serial_gateway_from_handshake(
                    handshake,
                    self._serial_transport,
                    runtime_serial_port,
                )
                continue

            if not packet:
                if self._serial_transport and not self._serial_transport.available:
                    logger.warning("Serial gateway transport became unavailable; will reconnect")
                    self._drop_serial_transport(clear_gateway_entry=False)
                    break
                continue

            # Reuse existing processing path by using a synthetic sender endpoint.
            self.packet_queue.put((packet, (self._serial_endpoint, self.gateway_port)))

        logger.info("Serial receive loop stopped")

    def _netnode_wifi_status_loop(self):
        """Push cmd=3 updates when host connectivity state changes."""
        while self.running:
            status_value = self._get_netnode_wifi_status_value()
            if self._last_netnode_wifi_status != status_value:
                radio_mac = self._serial_gateway_radio_mac
                if radio_mac:
                    self._sync_netnode_wifi_status(radio_mac)
                self._last_netnode_wifi_status = status_value
            time.sleep(2.0)

    def _get_netnode_wifi_status_value(self) -> int:
        """Return host connectivity status to publish to radio via SYS_CMD=3."""
        try:
            ip = self._get_server_ip()
            if ip == "127.0.0.1":
                return 0
            return 1
        except Exception:
            return 0

    def _sync_netnode_wifi_status(self, gateway_mac: str) -> bool:
        """Sync Python host connectivity state to radio netNodeHasWiFi."""
        status_value = self._get_netnode_wifi_status_value()
        success = self.send_system_command(gateway_mac, CMD_NETNODE_WIFI_STATUS, status_value)
        if success:
            logger.info(
                f"Synced net node WiFi status to {gateway_mac}: "
                f"{'CONNECTED' if status_value else 'DISCONNECTED'}"
            )
        else:
            logger.warning(f"Failed to sync net node WiFi status to {gateway_mac}")
        return success

    def _serial_dashboard_loop(self):
        """Push host metrics to serial gateway for OLED rendering."""
        if psutil:
            # Warm-up call to avoid an initial always-zero CPU reading.
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

        while self.running:
            try:
                transport = self._serial_transport
                endpoint = self._serial_endpoint
                if transport and endpoint and self._is_serial_endpoint(endpoint) and transport.available:
                    active_ota_mac = ota_manager.active_session_mac
                    active_ota = ota_manager.get_session(active_ota_mac) if active_ota_mac else None
                    if not (active_ota and active_ota.state == OTAState.TRANSFERRING):
                        line = self._build_serial_dashboard_line()
                        transport.send_dashboard_line(line)
            except Exception as e:
                logger.debug(f"Serial dashboard publish error: {e}")

            time.sleep(1.0)

    def _build_serial_dashboard_line(self) -> str:
        """Build compact dashboard line: @cpu,ram,ip,uptime,temp"""
        cpu = 0
        ram = 0

        if psutil:
            try:
                cpu = int(round(psutil.cpu_percent(interval=None)))
                ram = int(round(psutil.virtual_memory().percent))
            except Exception:
                pass
        else:
            logger.error("psutil not available, cannot get CPU/RAM metrics for serial dashboard")

        ip = self._get_server_ip()
        uptime = self._format_uptime(time.time() - self._process_start_time)
        temp = self._get_cpu_temp_c()

        return f"@{cpu},{ram},{ip},{uptime},{temp}"

    def _get_server_ip(self) -> str:
        """Resolve local host IPv4 suitable for UI display, with caching."""
        now = time.time()
        if now - self._last_server_ip_refresh < 30 and self._cached_server_ip:
            return self._cached_server_ip

        ip = "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
        except Exception:
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
            except Exception:
                ip = "127.0.0.1"

        self._cached_server_ip = ip
        self._last_server_ip_refresh = now
        return ip

    def _get_cpu_temp_c(self) -> str:
        """Best-effort CPU temperature in Celsius, '--' when unavailable."""
        if not psutil:
            return "--"

        try:
            temps = psutil.sensors_temperatures(fahrenheit=False)
            if not temps:
                return "--"

            for key in ("coretemp", "cpu-thermal", "soc_thermal", "k10temp"):
                entries = temps.get(key)
                if entries:
                    current = getattr(entries[0], "current", None)
                    if current is not None:
                        return str(int(round(current)))

            for entries in temps.values():
                if entries:
                    current = getattr(entries[0], "current", None)
                    if current is not None:
                        return str(int(round(current)))
        except Exception:
            return "--"

        return "--"

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """Format uptime as a single unit only: Xs, Xm, Xh, or Xd."""
        total = max(0, int(seconds))

        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m"
        if total < 86400:
            return f"{total // 3600}h"
        return f"{total // 86400}d"
    
    def restart(self, new_port: Optional[int] = None):
        """Restart the UDP server, optionally with a new port.
        
        Args:
            new_port: New UDP port to bind to (if None, uses current port)
        """
        logger.info(f"Restarting NetworkServer{f' on new port {new_port}' if new_port else ''}...")
        
        # Stop current server
        self.stop()
        
        # Update port if provided
        if new_port is not None:
            self.udp_port = new_port
        
        # Give a brief moment for port to be released
        time.sleep(0.5)
        
        # Start with new configuration
        self.start()
    
    def _receive_loop(self):
        """Main receive loop for incoming packets."""
        logger.info("Receive loop started")
        
        while self.running:
            if not self.sock:
                time.sleep(0.1)
                continue
            try:
                data, addr = self.sock.recvfrom(V2_UDP_BUFFER_SIZE)
                
                # Queue packet for processing
                self.packet_queue.put((data, addr))
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"Error in receive loop: {e}")
    
    def _worker_loop(self):
        """Worker thread for processing packets."""
        while self.running:
            try:
                # Get packet from queue with timeout
                try:
                    data, addr = self.packet_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                
                # Process packet
                self._handle_packet(data, addr)
                
            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)

    def _resolve_gateway_radio_mac_by_sender(self, sender_endpoint: str) -> Optional[str]:
        """Resolve gateway radio MAC by sender endpoint (IP or serial endpoint)."""
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info.get('ip_address') == sender_endpoint:
                    return radio_mac
        return None

    @staticmethod
    def _is_serial_endpoint(endpoint: str) -> bool:
        return isinstance(endpoint, str) and endpoint.startswith('serial://')

    def _send_packet_to_gateway(self, gateway_endpoint: str, packet: bytes) -> bool:
        """Send packet to a gateway endpoint over UDP or serial transport."""
        # Enforce allow_v1 on outbound traffic too (every legacy v1 frame is exactly 203 B; v2 is never 203 B)
        if len(packet) == PACKET_TOTAL_SIZE and not config_manager.get_security_flags().get('allow_v1', True):
            logger.debug(
                f"SECURITY: Blocked outbound legacy v1 packet (type=0x{packet[0]:02X}) to {gateway_endpoint} because allow_v1=false"
            )
            return False

        if self._is_serial_endpoint(gateway_endpoint):
            if not self._serial_transport:
                logger.warning(f"Serial gateway not available for endpoint {gateway_endpoint}")
                return False
            success = self._serial_transport.send_packet(packet)
            if not success:
                logger.warning("Serial gateway send failed; resetting transport for reconnect")
                self._drop_serial_transport(clear_gateway_entry=False)
            return success

        if not self.sock:
            logger.warning("UDP socket not available for gateway send")
            return False

        self.sock.sendto(packet, (gateway_endpoint, self.gateway_port))
        return True
    
    def _initial_led_sync(self):
        """Sync LED state for all gateways on startup (runs once in background)."""
        # Wait a few seconds for gateways to send HELLO packets
        time.sleep(5)
        
        logger.info("Performing initial LED sync for all gateways...")
        
        gateways = device_manager.get_all_gateways()
        for gateway in gateways:
            wifi_mac = gateway.get('mac_address')
            if not wifi_mac:
                continue
            
            leds_should_be_off = self._should_leds_be_off(wifi_mac)
            if leds_should_be_off is not None:
                if leds_should_be_off:
                    logger.info(f"Initial sync: LED OFF for gateway {wifi_mac}")
                    self.set_gateway_leds(wifi_mac, False)
                else:
                    logger.info(f"Initial sync: LED ON for gateway {wifi_mac}")
                    self.set_gateway_leds(wifi_mac, True)
        
        # Update global state based on first gateway's schedule
        if gateways:
            first_gateway = gateways[0]
            led_off_time = first_gateway.get('led_off_time')
            led_on_time = first_gateway.get('led_on_time')
            
            if led_off_time is not None and led_on_time is not None:
                current_hour = datetime.now().hour
                if led_off_time < led_on_time:
                    should_be_enabled = not (led_off_time <= current_hour < led_on_time)
                else:
                    should_be_enabled = not (current_hour >= led_off_time or current_hour < led_on_time)
                
                with self._led_state_lock:
                    self._gateway_leds_enabled = should_be_enabled
    
    def _should_leds_be_off(self, gateway_mac: str) -> Optional[bool]:
        """Check if LEDs should be off for a specific gateway based on its schedule.
        
        Args:
            gateway_mac: Gateway WiFi MAC address
            
        Returns:
            True if LEDs should be off, False if on, None if no schedule configured
        """
        # Get gateway data from gateways.json
        gateways = device_manager.get_all_gateways()
        gateway_data = None
        
        for gw in gateways:
            if gw.get('mac_address') == gateway_mac:
                gateway_data = gw
                break
        
        if not gateway_data:
            return None
        
        led_off_time = gateway_data.get('led_off_time')
        led_on_time = gateway_data.get('led_on_time')
        
        if led_off_time is None or led_on_time is None:
            return None
        
        current_hour = datetime.now().hour
        
        # Determine if LEDs should be off based on schedule
        if led_off_time < led_on_time:
            # Normal schedule (e.g., off at 8, on at 18)
            return led_off_time <= current_hour < led_on_time
        else:
            # Crosses midnight (e.g., off at 22, on at 7)
            return current_hour >= led_off_time or current_hour < led_on_time
    
    def _led_scheduler_loop(self):
        """Background thread to check LED schedules every minute."""
        logger.info("LED scheduler started")
        
        while self.running:
            try:
                current_time = datetime.now()
                current_hour = current_time.hour
                
                # Only check once per hour
                if current_hour != self._last_led_hour:
                    self._last_led_hour = current_hour
                    
                    # Get all gateways with their schedules
                    gateways = device_manager.get_all_gateways()
                    
                    for gateway in gateways:
                        try:
                            led_off_time = gateway.get('led_off_time')
                            led_on_time = gateway.get('led_on_time')
                            
                            if led_off_time is None or led_on_time is None:
                                continue
                            
                            # Determine if LEDs should be off based on schedule
                            # Handle schedules that cross midnight
                            if led_off_time < led_on_time:
                                # Normal schedule (e.g., off at 8, on at 18)
                                leds_should_be_off = led_off_time <= current_hour < led_on_time
                            else:
                                # Crosses midnight (e.g., off at 22, on at 7)
                                leds_should_be_off = current_hour >= led_off_time or current_hour < led_on_time
                            
                            # Send command to this gateway
                            wifi_mac = gateway.get('mac_address')
                            if wifi_mac:
                                if leds_should_be_off:
                                    # Turn LEDs off (cmd=1 is night mode ON / LED OFF)
                                    self.set_gateway_leds(wifi_mac, False)
                                    logger.info(f"Scheduled LED OFF for gateway {wifi_mac} (hour {current_hour})")
                                else:
                                    # Turn LEDs on (cmd=2 is night mode OFF / LED ON)
                                    self.set_gateway_leds(wifi_mac, True)
                                    logger.info(f"Scheduled LED ON for gateway {wifi_mac} (hour {current_hour})")
                        except Exception as e:
                            logger.error(f"Error processing LED schedule for gateway {gateway.get('mac_address', 'unknown')}: {e}")
                    
                    # Update global state based on first gateway's schedule
                    if gateways:
                        first_gateway = gateways[0]
                        led_off_time = first_gateway.get('led_off_time')
                        led_on_time = first_gateway.get('led_on_time')
                        
                        if led_off_time is not None and led_on_time is not None:
                            if led_off_time < led_on_time:
                                should_be_enabled = not (led_off_time <= current_hour < led_on_time)
                            else:
                                should_be_enabled = not (current_hour >= led_off_time or current_hour < led_on_time)
                            
                            with self._led_state_lock:
                                self._gateway_leds_enabled = should_be_enabled
                
                # Sleep for 60 seconds before next check
                time.sleep(60)
                
            except Exception as e:
                logger.error(f"Error in LED scheduler: {e}", exc_info=True)
                time.sleep(60)
    
    def _is_known_paired_mac(self, mac: str) -> bool:
        """Return True if mac belongs to a registered gateway, device, or plugin binding."""
        if not mac or mac == "00:00:00:00:00:00":
            return False

        with self._gateway_lock:
            if mac in self._gateway_table:
                return True
            for info in self._gateway_table.values():
                if info.get('wifi_mac') == mac:
                    return True

        if (
            device_manager.get_gateway_by_wifi_mac(mac)
            or device_manager.get_gateway_by_radio_mac(mac)
            or device_manager.get_button_by_mac(mac)
            or device_manager.get_light_by_mac(mac)
            or device_manager.get_motion_sensor_by_mac(mac)
            or device_manager.get_door_sensor_by_mac(mac)
        ):
            return True

        if self._plugin_manager and self._plugin_manager.get_plugin_for_mac(mac):
            return True

        return False

    def _try_reregister_authenticated_v2_device(
        self, pkt_type: int, src_mac: str, payload: bytes, sender_ip: str
    ) -> bool:
        """Re-register an already AES-GCM-authenticated v2 device if pairing mode is open."""
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)

        if pkt_type == PKT_BTN_EVENT:
            parsed = self.decoder.parse_button_event(payload) or {}
            btn_idx = parsed.get('button_index', -1)
            raw_btn_count = parsed.get('button_count') or 0
            is_remote = (btn_idx >= 0) or (raw_btn_count > 1)
            btn_count = raw_btn_count if raw_btn_count > 0 else 4
            dev_type = DEV_REMOTE if is_remote else DEV_BUTTON
            if not pairing_manager.is_pairing_allowed(src_mac, dev_type, 0):
                return False
            if is_remote:
                name = f"Remote {src_mac[-8:]}"
                device_manager.add_button(src_mac, name, device_type=DEV_REMOTE, button_count=btn_count)
                if gateway_radio_mac:
                    device_manager.update_button_tracking(
                        src_mac, gateway_radio_mac, 0,
                        version=parsed.get('version'), platform=parsed.get('platform'), button_count=btn_count
                    )
            else:
                name = f"Button {src_mac[-8:]}"
                device_manager.add_button(src_mac, name)
                if gateway_radio_mac:
                    device_manager.update_button_tracking(
                        src_mac, gateway_radio_mac, 0,
                        version=parsed.get('version'), platform=parsed.get('platform')
                    )
            pairing_manager.record_device_paired(src_mac, dev_type, name, 'long_range')
        elif pkt_type == PKT_MOTION_EVENT:
            if not pairing_manager.is_pairing_allowed(src_mac, DEV_MOTION, 0):
                return False
            parsed = self.decoder.parse_motion_event(payload) or {}
            name = f"Motion Sensor {src_mac[-8:]}"
            device_manager.add_motion_sensor(src_mac, name)
            if gateway_radio_mac:
                device_manager.update_motion_sensor_tracking(
                    src_mac, gateway_radio_mac, 0,
                    version=parsed.get('version'), platform=parsed.get('platform')
                )
            pairing_manager.record_device_paired(src_mac, DEV_MOTION, name, 'long_range')
        elif pkt_type == PKT_DOOR_EVENT:
            if not pairing_manager.is_pairing_allowed(src_mac, DEV_DOOR, 0):
                return False
            parsed = self.decoder.parse_door_event(payload) or {}
            name = f"Door Sensor {src_mac[-8:]}"
            device_manager.add_door_sensor(src_mac, name)
            if gateway_radio_mac:
                device_manager.update_door_sensor_tracking(
                    src_mac, gateway_radio_mac, 0,
                    version=parsed.get('version'), platform=parsed.get('platform')
                )
            pairing_manager.record_device_paired(src_mac, DEV_DOOR, name, 'long_range')
        else:
            return False

        # Ensure gateways also have its K_ack synced
        self._push_key_distrib_to_gateways(
            target_mac=src_mac,
            crypto_mode=security_store.get_crypto_mode(src_mac),
            max_frame=security_store.get_max_frame(src_mac),
            k_ack=security_store.derive_k_ack(src_mac),
            action=KEY_DISTRIB_UPSERT,
        )
        logger.info(f"🔄 Re-registered authenticated v2 device {src_mac} during pairing window")
        return True

    def _handle_packet(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming packet.
        
        Args:
            data: Raw packet data
            addr: Sender (ip, port)
        """
        sender_ip, sender_port = addr
        
        # Decode packet (dual-stack v1/v2 with runtime safety flags)
        sec_flags = config_manager.get_security_flags()

        # Intercept Flight 3 (PKT_PAIR_ACK) for active v2 pairing handshakes before general decode
        if sec_flags['security_v2_pair'] and len(data) >= 39 and data[0] == PKT_PAIR_ACK and data[1] == PROTO_VERSION_V2:
            result = pairing_v2_manager.handle_pair_ack(data, security_store)
            if result:
                self._finalize_v2_paired_device(result)
            return

        packet = decode_dual(
            data,
            self.decoder,
            security_store,
            allow_v2=sec_flags['security_v2_decode'],
            allow_v1=sec_flags['allow_v1'],
        )
        if not packet and sec_flags['security_v2_decode'] and len(data) >= V2_MIN_FRAME_SIZE and data[1] == PROTO_VERSION_V2:
            # Self-heal split-brain if a gateway committed v2 to NVS but its UDP PKT_PAIR_ACK
            # was lost: verify the frame directly under our deterministic HKDF K_own.
            hdr_v2 = PacketDecoderV2.parse_header(data)
            if hdr_v2:
                cand_mac = hdr_v2['source_mac']
                is_known_gw = bool(
                    device_manager.get_gateway_by_wifi_mac(cand_mac)
                    or device_manager.get_gateway_by_radio_mac(cand_mac)
                )
                if (
                    is_known_gw
                    and not security_store.is_v2_device(cand_mac)
                    and hdr_v2['key_epoch'] == security_store.epoch
                ):
                    cand_key = security_store.derive_k_own(cand_mac, epoch=security_store.epoch)
                    recovered = PacketDecoderV2.decode_with_key(data, cand_key)
                    if recovered is not None:
                        security_store.set_device_record(
                            cand_mac,
                            CRYPTO_MODE_V2_UNVERIFIED,
                            max_frame=V2_MAX_FRAME_ESP32,
                            key_epoch=security_store.epoch,
                        )
                        security_store.set_pending_v2_upgrade(cand_mac, False)
                        logger.info(
                            f"🔐 Recovered Security v2 state for gateway {cand_mac} "
                            f"from authenticated v2 packet (lost PKT_PAIR_ACK)"
                        )
                        packet = recovered
        if not packet:
            return  # Invalid packet
        
        pkt_type = packet['type']
        src_mac = packet['source_mac']
        is_paired = packet['is_paired']
        payload = packet['payload']
        proto_ver = packet.get('protocol_version', 1)

        # Replay protection & duplicate-retransmit dedupe for v2 packets
        if proto_ver == PROTO_VERSION_V2:
            replay_status = security_store.check_and_update_replay(
                src_mac, packet['counter'], packet['ciphertext']
            )
            if replay_status == 'duplicate':
                logger.debug(f"Duplicate v2 retransmission from {src_mac} counter={packet['counter']}; ignoring")
                return
            if replay_status == 'reject':
                logger.warning(
                    f"SECURITY: Rejected replayed/out-of-order v2 packet from {src_mac} "
                    f"counter={packet['counter']}"
                )
                return

        # Security Phase 0a: drop any non-HELLO packet from an unpaired or unknown source
        if pkt_type != PKT_HELLO:
            if not is_paired or not self._is_known_paired_mac(src_mac):
                # If a previously v2-paired device was removed from the UI (but not factory-reset
                # or revoked from security.json), it has ALREADY passed AES-128-GCM + replay checks.
                # Allow it to re-register cleanly when the user opens Pairing mode.
                if (
                    proto_ver == PROTO_VERSION_V2
                    and is_paired
                    and self._try_reregister_authenticated_v2_device(pkt_type, src_mac, payload, sender_ip)
                ):
                    pass
                else:
                    logger.warning(
                        f"SECURITY: Dropped non-HELLO packet type={pkt_type:#x} from "
                        f"unpaired/unknown source {src_mac} (is_paired={is_paired})"
                    )
                    return
        
        if pkt_type != PKT_PING:
            with self._gateway_lock:
                for radio_mac, info in self._gateway_table.items():
                    if info['wifi_mac'] == src_mac:
                        device_manager.update_gateway(src_mac, radio_mac, sender_ip)
                        break
                else:
                    # Gateway not in routing table - check if it's paired but deleted from config
                    if is_paired and pkt_type != PKT_HELLO:
                        gateways = device_manager.get_all_gateways()
                        gateway_exists = any(gw.get('ip_address') == sender_ip for gw in gateways)
                        if not gateway_exists:
                            logger.info(f"🌐 Paired gateway {src_mac} not in config, requesting HELLO...")
                            self._send_pair_confirm(src_mac, sender_ip)

        if self._plugin_manager:
            gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
            self._plugin_manager.dispatch_event('packet_received', {
                'type': pkt_type,
                'source_mac': src_mac,
                'target_mac': packet['target_mac'],
                'msg_id': packet['msg_id'],
                'payload': payload,
                'is_paired': is_paired,
                'protocol_version': proto_ver,
                'sender_ip': sender_ip,
                'gateway_radio_mac': gateway_radio_mac,
            })
                
        # Handle packet by type
        if pkt_type == PKT_HELLO:
            self._handle_hello(src_mac, payload, sender_ip, is_paired)
        
        elif pkt_type == PKT_BTN_EVENT:
            self._handle_button_event(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_MOTION_EVENT:
            self._handle_motion_event(src_mac, payload, sender_ip)

        elif pkt_type == PKT_DOOR_EVENT:
            self._handle_door_event(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_DELIVERY_RPT:
            self._handle_delivery_report(payload, sender_ip)
        
        elif pkt_type == PKT_PING:
            self._handle_ping_response(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_PING_DEVICE:
            rssi_v2 = packet.get('rssi') if proto_ver == PROTO_VERSION_V2 else None
            self._handle_ping_device_response(src_mac, payload, sender_ip, rssi_v2=rssi_v2)
        
        elif pkt_type == PKT_OTA_READY:
            self._handle_ota_ready(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_OTA_CHUNK_ACK:
            self._handle_ota_chunk_ack(src_mac, payload)
        
        elif pkt_type == PKT_OTA_ABORT:
            self._handle_ota_abort_from_device(src_mac, payload)
        
        elif pkt_type == PKT_SYS_CMD and proto_ver == PROTO_VERSION_V2:
            self._handle_v2_sys_cmd(src_mac, payload, sender_ip)

        elif pkt_type == PKT_GW_LIST_UPD:
            # Gateway received our list - no action needed
            pass

        # Deliver any queued v2 upgrade or PIN provisioning command while sleepy device is awake
        if is_paired and pkt_type != PKT_SYS_CMD:
            self._flush_pending_security_actions(src_mac, sender_ip, proto_ver)
    
    def _handle_hello(self, src_mac: str, payload: bytes, sender_ip: str, is_paired: bool):
        """Handle HELLO packet from device.
        
        Args:
            src_mac: Source MAC address
            payload: Raw payload
            sender_ip: Sender IP address
            is_paired: Whether device is already paired
        """
        hello_data = self.decoder.parse_hello(payload)
        if not hello_data:
            logger.warning(f"Invalid HELLO from {src_mac}")
            return
        
        dev_type = hello_data['device_type']

        # In-place v1 -> v2 TOFU upgrade triggered from the OTA page
        if (
            is_paired
            and dev_type != DEV_GATEWAY
            and security_store.is_pending_v2_upgrade(src_mac)
            and not security_store.is_v2_device(src_mac)
            and hello_data.get('proto_version', 1) >= PROTO_VERSION_V2
            and hello_data.get('ecdh_pub')
        ):
            logger.info(f"🔐 Processing in-place v2 upgrade HELLO from {src_mac} (type={dev_type})")
            if self._try_start_v2_pairing(src_mac, dev_type, hello_data, sender_ip):
                return
        
        # Handle plugin device (unpaired, sends with UUID marker)
        if dev_type == DEV_PLUGIN_MARKER:
            self._handle_plugin_hello(src_mac, hello_data, sender_ip, is_paired)
            return
        
        # Handle core device types
        if dev_type == DEV_GATEWAY:
            self._handle_gateway_hello(src_mac, hello_data, sender_ip, is_paired)
        
        elif dev_type == DEV_BUTTON:
            self._handle_button_hello(src_mac, hello_data, sender_ip, is_paired)
        
        elif dev_type == DEV_LIGHT:
            self._handle_light_hello(src_mac, hello_data, sender_ip, is_paired)
        
        elif dev_type == DEV_REMOTE:
            self._handle_remote_hello(src_mac, hello_data, sender_ip, is_paired)
        
        elif dev_type == DEV_MOTION:
            self._handle_motion_hello(src_mac, hello_data, sender_ip, is_paired)

        elif dev_type == DEV_DOOR:
            self._handle_door_hello(src_mac, hello_data, sender_ip, is_paired)
    
    def _handle_gateway_hello(self, wifi_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from gateway.
        
        Args:
            wifi_mac: Gateway WiFi MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP
            is_paired: Whether gateway is paired
        """
        radio_mac = hello_data.get('radio_mac')
        if not radio_mac or radio_mac == "00:00:00:00:00:00":
            logger.warning(f"Gateway {wifi_mac} HELLO missing radio_mac")
            return

        # Radio-node Security v2 bootstrap HELLO (sourceMAC == radio_mac)
        if wifi_mac.upper() == radio_mac.upper():
            sec_flags = config_manager.get_security_flags()
            is_known_gateway_radio = bool(device_manager.get_gateway_by_radio_mac(radio_mac))
            has_v2_ecdh = (
                sec_flags['security_v2_pair']
                and hello_data.get('proto_version', 1) >= PROTO_VERSION_V2
                and bool(hello_data.get('ecdh_pub'))
            )
            # SECURITY: Never run DEV_GATEWAY v2 pairing for a non-gateway MAC (blocks forged
            # DEV_GATEWAY HELLO key-extraction attacks against buttons/lightstrips/sensors).
            # Gateways on the local LAN are TOFU-only and auto-pair / auto-re-pair as Security v2.
            if security_store.is_v2_device(radio_mac):
                if not is_known_gateway_radio:
                    logger.warning(
                        f"SECURITY: Ignored forged/unexpected DEV_GATEWAY HELLO for already-v2 MAC {radio_mac}"
                    )
                    return
                if has_v2_ecdh:
                    frame = pairing_v2_manager.handle_hello_v2(
                        device_mac=radio_mac,
                        device_type=DEV_GATEWAY,
                        hello_data=hello_data,
                        home_id=self.home_id,
                        sender_ip=sender_ip,
                        store=security_store,
                        active_pin=None,
                    )
                    if frame:
                        self._send_packet_to_gateway(sender_ip, frame)
                        logger.info(f"🔐 Re-pairing gateway radio node as Security v2: {radio_mac}")
                    return
                self._sync_all_v2_keys_to_gateway(radio_mac, sender_ip)
                return

            if has_v2_ecdh:
                is_other_registered_device = (
                    not is_known_gateway_radio
                    and self._is_known_paired_mac(radio_mac)
                )
                if is_other_registered_device:
                    logger.warning(
                        f"SECURITY: Rejected DEV_GATEWAY v2 HELLO impersonating non-gateway device {radio_mac}"
                    )
                    return

                frame = pairing_v2_manager.handle_hello_v2(
                    device_mac=radio_mac,
                    device_type=DEV_GATEWAY,
                    hello_data=hello_data,
                    home_id=self.home_id,
                    sender_ip=sender_ip,
                    store=security_store,
                    active_pin=None,
                )
                if frame:
                    self._send_packet_to_gateway(sender_ip, frame)
            return
        
        sec_flags = config_manager.get_security_flags()
        is_v2_capable_gw = (
            hello_data.get('proto_version', 1) >= PROTO_VERSION_V2
            and bool(hello_data.get('ecdh_pub'))
        )

        # Prevent a deleted/revoked gateway that was NOT factory-reset from silently
        # re-registering itself as a legacy v1 device when it reconnects with stale HOME_ID/v2 keys.
        existing_gw = (
            device_manager.get_gateway_by_wifi_mac(wifi_mac)
            or device_manager.get_gateway_by_radio_mac(radio_mac)
        )
        if not existing_gw:
            is_stale_v2_gw = (
                is_paired
                and hello_data.get('proto_version', 1) >= PROTO_VERSION_V2
                and not hello_data.get('ecdh_pub')
            )
            is_revoked_unreset = (
                is_paired
                and (security_store.is_revoked(wifi_mac) or security_store.is_revoked(radio_mac))
            )
            if is_stale_v2_gw or is_revoked_unreset:
                logger.warning(
                    f"SECURITY: Ignored HELLO from removed/revoked gateway {wifi_mac} (radio={radio_mac}) "
                    f"— factory reset the gateway to re-pair"
                )
                return
            if sec_flags.get('require_v2') and not is_v2_capable_gw:
                logger.warning(
                    f"SECURITY: Refused legacy v1 gateway {wifi_mac} (require_v2=true)"
                )
                return

        # Update gateway table
        is_new_gateway = False
        with self._gateway_lock:
            if radio_mac not in self._gateway_table:
                logger.info(f"🌐 New gateway: WiFi={wifi_mac}, Radio={radio_mac}, IP={sender_ip}")
                self._gateway_table[radio_mac] = {
                    'ip_address': sender_ip,
                    'wifi_mac': wifi_mac,
                    'last_seen': datetime.now()
                }
                is_new_gateway = True
            else:
                # Update IP if changed and update last_seen
                old_ip = self._gateway_table[radio_mac]['ip_address']
                if old_ip != sender_ip:
                    logger.info(f"🌐 Gateway IP changed: {wifi_mac} ({radio_mac}): {old_ip} → {sender_ip}")
                self._gateway_table[radio_mac]['ip_address'] = sender_ip
                self._gateway_table[radio_mac]['last_seen'] = datetime.now()

        # Update device manager
        device_manager.update_gateway(
            wifi_mac, 
            radio_mac, 
            sender_ip, 
            version_net=hello_data.get('version_net'),
            version_radio=hello_data.get('version_radio')
        )
        
        # Check if net node has a pending OTA validation
        session = ota_manager.get_session(wifi_mac)
        if session and session.state == OTAState.VALIDATING:
            # Check if version matches expected firmware
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version_net = hello_data.get('version_net', '0.0.0')
            
            # Gateway net firmware validation
            if current_version_net == expected_version:
                logger.info(f"✅ OTA validation SUCCESS (NET): {wifi_mac} now running {current_version_net}")
                ota_manager.update_session_state(wifi_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version_net}"
                logger.error(f"❌ OTA validation FAILED (NET): {wifi_mac} - {error_msg}")
                ota_manager.update_session_state(wifi_mac, OTAState.FAILED, error_msg)
        
        # Check if radio node has a pending OTA validation
        if radio_mac:
            session = ota_manager.get_session(radio_mac)
            if session and session.state == OTAState.VALIDATING:
                # Check if version matches expected firmware
                expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
                current_version_radio = hello_data.get('version_radio', '0.0.0')
                
                # Gateway radio firmware validation
                if current_version_radio == expected_version:
                    logger.info(f"✅ OTA validation SUCCESS (RADIO): {radio_mac} now running {current_version_radio}")
                    ota_manager.update_session_state(radio_mac, OTAState.COMPLETE, "Firmware validated successfully")
                else:
                    error_msg = f"Version mismatch: expected {expected_version}, got {current_version_radio}"
                    logger.error(f"❌ OTA validation FAILED (RADIO): {radio_mac} - {error_msg}")
                    ota_manager.update_session_state(radio_mac, OTAState.FAILED, error_msg)
        
        # If gateway is paired, check if LEDs should be off based on its schedule
        if is_paired:
            leds_should_be_off = self._should_leds_be_off(wifi_mac)
            if leds_should_be_off is not None:
                # Gateway has a schedule - sync LED state
                if leds_should_be_off:
                    logger.info(f"Syncing LED OFF to newly online gateway {wifi_mac} (schedule-based)")
                    self.set_gateway_leds(wifi_mac, False)
                else:
                    logger.info(f"Syncing LED ON to newly online gateway {wifi_mac} (schedule-based)")
                    self.set_gateway_leds(wifi_mac, True)
        
        # If unpaired, send pairing confirmation (prefer v2 handshake when enabled; respect require_v2)
        if not is_paired:
            if sec_flags['security_v2_pair'] and is_v2_capable_gw:
                frame = pairing_v2_manager.handle_hello_v2(
                    device_mac=wifi_mac,
                    device_type=DEV_GATEWAY,
                    hello_data=hello_data,
                    home_id=self.home_id,
                    sender_ip=sender_ip,
                    store=security_store,
                    active_pin=None,
                )
                if frame:
                    self._send_packet_to_gateway(sender_ip, frame)
                    logger.info(f"🔐 Started Security v2 pairing for gateway net node: {wifi_mac}")
            elif sec_flags.get('require_v2'):
                logger.warning(f"SECURITY: Refused legacy v1 auto-pairing for gateway {wifi_mac} (require_v2=true)")
            else:
                # Auto-pair legacy v1 gateways
                self._send_pair_confirm(wifi_mac, sender_ip)
                logger.info(f"Auto-paired gateway: {wifi_mac}")
                pairing_manager.record_device_paired(wifi_mac, DEV_GATEWAY, f"Gateway {wifi_mac[-8:]}", 'wifi')
        
        if is_new_gateway and is_paired:
            pairing_manager.record_device_paired(wifi_mac, DEV_GATEWAY, f"Gateway {wifi_mac[-8:]}", 'wifi')

        if is_paired:
            self._broadcast_gateway_list((not is_new_gateway))
            if security_store.is_v2_device(radio_mac):
                self._sync_all_v2_keys_to_gateway(radio_mac, sender_ip)

        # Net-node Security v2 in-place upgrade (only for already-paired v1 gateways)
        if (
            is_paired
            and sec_flags['security_v2_pair']
            and is_v2_capable_gw
            and not security_store.is_v2_device(wifi_mac)
        ):
            frame = pairing_v2_manager.handle_hello_v2(
                device_mac=wifi_mac,
                device_type=DEV_GATEWAY,
                hello_data=hello_data,
                home_id=self.home_id,
                sender_ip=sender_ip,
                store=security_store,
                active_pin=pairing_manager.get_active_pin(),
            )
            if frame:
                self._send_packet_to_gateway(sender_ip, frame)
    
    def _can_secure_short_range_auto_pair(self, device_mac: str, hello_data: Dict, rssi: int) -> bool:
        """Return True ONLY when short-range auto-pairing is enabled AND the device is a
        non-revoked Security v2 device with a saved 6-digit PIN closer than -50 dBm.
        Never permits legacy v1 or TOFU auto-pairing.
        """
        if not config_manager.get_allow_rssi_auto_pair():
            return False
        if rssi <= RSSI_AUTO_PAIR_THRESHOLD:
            return False
        sec_flags = config_manager.get_security_flags()
        if not sec_flags.get('security_v2_pair'):
            return False
        if hello_data.get('proto_version', 1) < PROTO_VERSION_V2:
            return False
        if not hello_data.get('has_pin') or not hello_data.get('ecdh_pub') or not hello_data.get('nonce_d'):
            return False
        if security_store.is_revoked(device_mac):
            logger.info(f"SECURITY: Refused short-range auto-pair for revoked device {device_mac}")
            return False
        saved_pin = security_store.get_device_pin(device_mac)
        if not saved_pin or len(saved_pin) != 6 or not saved_pin.isdigit():
            return False
        return True

    def _handle_button_hello(self, button_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from button.
        
        Args:
            button_mac: Button MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP that forwarded
            is_paired: Whether button is paired
        """
        rssi = hello_data.get('rssi', -100)
        
        # Check if this device has a pending OTA validation
        session = ota_manager.get_session(button_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')
            
            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS: {button_mac} now running {current_version}")
                ota_manager.update_session_state(button_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED: {button_mac} - {error_msg}")
                ota_manager.update_session_state(button_mac, OTAState.FAILED, error_msg)
        
        # Find gateway radio MAC by sender endpoint
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if not is_paired:
            # Unpaired button - check pairing mode or RSSI
            logger.info(f"🔘 Unpaired button detected: {button_mac} (RSSI: {rssi} dBm)")
            
            should_pair = False
            pairing_mode_active = False
            
            # Check if pairing mode is active
            if self._pairing_handler:
                if self._pairing_handler(button_mac, DEV_BUTTON, rssi):
                    should_pair = True
                    pairing_mode_active = True
            
            # If pairing mode not active, only allow secure v2 Known-PIN short-range auto-pair (> -50 dBm)
            if not pairing_mode_active and self._can_secure_short_range_auto_pair(button_mac, hello_data, rssi):
                should_pair = True
            
            if should_pair:
                if self._try_start_v2_pairing(button_mac, DEV_BUTTON, hello_data, sender_ip):
                    return
                self._send_pair_confirm(button_mac, sender_ip)
                device_manager.add_button(button_mac, f"Button {button_mac[-8:]}")
                
                # Update with version, platform, and RSSI from this HELLO packet
                if gateway_radio_mac:
                    device_manager.update_button_tracking(button_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
                
                if pairing_mode_active:
                    logger.info(f"🔘 Paired button via pairing mode: {button_mac}")
                    pairing_manager.record_device_paired(button_mac, DEV_BUTTON, f"Button {button_mac[-8:]}", 'long_range')
                else:
                    logger.info(f"🔘 Auto-paired button (RSSI: {rssi} dBm): {button_mac}")
                    pairing_manager.record_device_paired(button_mac, DEV_BUTTON, f"Button {button_mac[-8:]}", 'short_range')
            else:
                logger.warning(f"Button {button_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired button - update tracking
            if gateway_radio_mac:
                device_manager.update_button_tracking(button_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
            
            logger.debug(f"Button {button_mac} online (RSSI: {rssi} dBm)")
    
    def _handle_light_hello(self, light_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from lightstrip.
        
        Args:
            light_mac: Light MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP that forwarded
            is_paired: Whether light is paired
        """
        rssi = hello_data.get('rssi', -100)
        num_leds = hello_data.get('num_leds', 60)
        is_rgbw = hello_data.get('is_rgbw', False)
        
        # Check if this device has a pending OTA validation
        session = ota_manager.get_session(light_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')
            
            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS: {light_mac} now running {current_version}")
                ota_manager.update_session_state(light_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED: {light_mac} - {error_msg}")
                ota_manager.update_session_state(light_mac, OTAState.FAILED, error_msg)
        
        # Find gateway radio MAC by sender endpoint
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if not is_paired:
            # Unpaired light - check pairing mode or RSSI
            logger.info(f"💡 Unpaired light detected: {light_mac} ({num_leds} LEDs, {'RGBW' if is_rgbw else 'RGB'}, RSSI: {rssi} dBm)")
            
            should_pair = False
            pairing_mode_active = False
            
            # Check if pairing mode is active
            if self._pairing_handler:
                if self._pairing_handler(light_mac, DEV_LIGHT, rssi):
                    should_pair = True
                    pairing_mode_active = True
            
            # If pairing mode not active, only allow secure v2 Known-PIN short-range auto-pair (> -50 dBm)
            if not pairing_mode_active and self._can_secure_short_range_auto_pair(light_mac, hello_data, rssi):
                should_pair = True
            
            if should_pair:
                if self._try_start_v2_pairing(light_mac, DEV_LIGHT, hello_data, sender_ip):
                    return
                self._send_pair_confirm(light_mac, sender_ip)
                device_manager.add_lightstrip(
                    light_mac, 
                    f"Light {light_mac[-8:]}", 
                    num_leds, 
                    is_rgbw,
                    model_id=hello_data.get('model_id')
                )
                
                # Set initial gateway
                if gateway_radio_mac:
                    device_manager.update_light_gateway(
                        light_mac, 
                        gateway_radio_mac, 
                        rssi=rssi, 
                        version=hello_data.get('version'),
                        platform=hello_data.get('platform'),
                        model_id=hello_data.get('model_id')
                    )
                
                # Send gateway list to newly paired light
                with self._gateway_lock:
                    all_gateway_macs = list(self._gateway_table.keys())
                    if all_gateway_macs:
                        light_packet = self._encode_gateway_list_dual(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
                        if self._send_packet_to_gateway(sender_ip, light_packet):
                            logger.debug(f"Sent initial gateway list to {light_mac}")
                
                if pairing_mode_active:
                    logger.info(f"💡 Paired light via pairing mode: {light_mac}")
                    pairing_manager.record_device_paired(light_mac, DEV_LIGHT, f"Light {light_mac[-8:]}", 'long_range')
                else:
                    logger.info(f"💡 Auto-paired light (RSSI: {rssi} dBm): {light_mac}")
                    pairing_manager.record_device_paired(light_mac, DEV_LIGHT, f"Light {light_mac[-8:]}", 'short_range')
            else:
                logger.warning(f"Light {light_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired light - check if it exists in config
            light = device_manager.get_light_by_mac(light_mac)
            if not light:
                # Device has home_id but was deleted from config - re-add it
                logger.info(f"💡 Re-registering previously paired light: {light_mac}")
                device_manager.add_lightstrip(
                    light_mac, 
                    f"Light {light_mac[-8:]}", 
                    num_leds, 
                    is_rgbw,
                    model_id=hello_data.get('model_id')
                )
                # Record in pairing history as a reconnected device
                pairing_manager.record_device_paired(light_mac, DEV_LIGHT, f"Light {light_mac[-8:]}", 'short_range')
                
                # Set initial gateway and send gateway list
                if gateway_radio_mac:
                    device_manager.update_light_gateway(
                        light_mac, 
                        gateway_radio_mac, 
                        rssi=rssi, 
                        version=hello_data.get('version'),
                        platform=hello_data.get('platform'),
                        model_id=hello_data.get('model_id')
                    )
                
                with self._gateway_lock:
                    all_gateway_macs = list(self._gateway_table.keys())
                    if all_gateway_macs:
                        light_packet = self._encode_gateway_list_dual(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
                        if self._send_packet_to_gateway(sender_ip, light_packet):
                            logger.debug(f"Sent initial gateway list to {light_mac}")
            
            # Update gateway that successfully received HELLO
            if gateway_radio_mac:
                device_manager.update_light_gateway(
                    light_mac, 
                    gateway_radio_mac, 
                    rssi=rssi, 
                    version=hello_data.get('version'),
                    platform=hello_data.get('platform'),
                    model_id=hello_data.get('model_id')
                )
                logger.info(
                    f"💡 Light {light_mac} HELLO via {gateway_radio_mac} / {sender_ip} "
                    f"(v{hello_data.get('version', '?')}, RSSI: {rssi} dBm)"
                )
            else:
                logger.warning(f"Light {light_mac} HELLO from unknown gateway {sender_ip}")
            
            if self.automation_engine:
                self.automation_engine.send_current_colors_to_light(light_mac)
    
    def _handle_remote_hello(self, remote_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from remote control device.
        
        Args:
            remote_mac: Remote MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP that forwarded
            is_paired: Whether remote is paired
        """
        rssi = hello_data.get('rssi', -100)
        
        # Check if this device has a pending OTA validation
        session = ota_manager.get_session(remote_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')
            
            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS: {remote_mac} now running {current_version}")
                ota_manager.update_session_state(remote_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED: {remote_mac} - {error_msg}")
                ota_manager.update_session_state(remote_mac, OTAState.FAILED, error_msg)
        
        # Find gateway radio MAC by sender endpoint
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if not is_paired:
            # Unpaired remote - check pairing mode or RSSI
            logger.info(f"🎮 Unpaired remote detected: {remote_mac} (RSSI: {rssi} dBm)")
            
            should_pair = False
            pairing_mode_active = False
            
            # Check if pairing mode is active
            if self._pairing_handler:
                if self._pairing_handler(remote_mac, DEV_REMOTE, rssi):
                    should_pair = True
                    pairing_mode_active = True
            
            # If pairing mode not active, only allow secure v2 Known-PIN short-range auto-pair (> -50 dBm)
            if not pairing_mode_active and self._can_secure_short_range_auto_pair(remote_mac, hello_data, rssi):
                should_pair = True
            
            if should_pair:
                if self._try_start_v2_pairing(remote_mac, DEV_REMOTE, hello_data, sender_ip):
                    return
                self._send_pair_confirm(remote_mac, sender_ip)
                button_count = hello_data.get('button_count', 4)
                device_name = f"Remote {remote_mac[-8:]}"
                
                device_manager.add_button(remote_mac, device_name, device_type=DEV_REMOTE, button_count=button_count)
                
                # Update with version, platform, button_count, and RSSI from this HELLO packet
                if gateway_radio_mac:
                    device_manager.update_button_tracking(remote_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'), button_count=button_count)
                
                if pairing_mode_active:
                    logger.info(f"🎮 Paired remote via pairing mode: {remote_mac} ({button_count} button{'s' if button_count != 1 else ''})")
                    pairing_manager.record_device_paired(remote_mac, DEV_REMOTE, device_name, 'long_range')
                else:
                    logger.info(f"🎮 Auto-paired remote (RSSI: {rssi} dBm): {remote_mac} ({button_count} button{'s' if button_count != 1 else ''})")
                    pairing_manager.record_device_paired(remote_mac, DEV_REMOTE, device_name, 'short_range')
            else:
                logger.warning(f"Remote {remote_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired remote - update tracking
            button_count = hello_data.get('button_count', 4)
            
            # Check if button count changed
            current_device = device_manager.get_button_by_mac(remote_mac)
            if current_device:
                current_button_count = current_device.get('button_count')
                if current_button_count != button_count:
                    logger.info(f"🎮 Remote {remote_mac} button count changed from {current_button_count} to {button_count}")
            
            if gateway_radio_mac:
                device_manager.update_button_tracking(remote_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'), button_count=button_count)
            
            logger.debug(f"Remote {remote_mac} online (RSSI: {rssi} dBm, buttons: {button_count})")
    
    def _handle_motion_hello(self, sensor_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from motion sensor.
        
        Args:
            sensor_mac: Motion sensor MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP that forwarded
            is_paired: Whether sensor is paired
        """
        rssi = hello_data.get('rssi', 0)
        
        # Check if this device has a pending OTA validation
        session = ota_manager.get_session(sensor_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')
            
            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS: {sensor_mac} now running {current_version}")
                ota_manager.update_session_state(sensor_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED: {sensor_mac} - {error_msg}")
                ota_manager.update_session_state(sensor_mac, OTAState.FAILED, error_msg)
        
        # Find gateway radio MAC by sender endpoint
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if not is_paired:
            # UNPAIRED - Check pairing mode
            pairing_mode_active = pairing_manager.is_pairing_allowed(sensor_mac, DEV_MOTION, rssi)
            rssi_auto_pair = self._can_secure_short_range_auto_pair(sensor_mac, hello_data, rssi)

            if rssi_auto_pair or pairing_mode_active:
                if self._try_start_v2_pairing(sensor_mac, DEV_MOTION, hello_data, sender_ip):
                    return
                # AUTO-PAIR or PAIRING MODE
                logger.info(f"📡 Motion sensor {sensor_mac} RSSI: {rssi} dBm → Pairing...")
                self._send_pair_confirm(sensor_mac, sender_ip)
                
                # Auto-register motion sensor
                sensor = device_manager.get_motion_sensor_by_mac(sensor_mac)
                if not sensor:
                    device_manager.add_motion_sensor(sensor_mac, f"Motion Sensor {sensor_mac[-8:]}")
                
                # Update with version, platform, and RSSI from this HELLO packet
                if gateway_radio_mac:
                    device_manager.update_motion_sensor_tracking(sensor_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
                
                if pairing_mode_active:
                    logger.info(f"🏃 Paired motion sensor via pairing mode: {sensor_mac}")
                    pairing_manager.record_device_paired(sensor_mac, DEV_MOTION, f"Motion Sensor {sensor_mac[-8:]}", 'long_range')
                else:
                    logger.info(f"🏃 Auto-paired motion sensor (RSSI: {rssi} dBm): {sensor_mac}")
                    pairing_manager.record_device_paired(sensor_mac, DEV_MOTION, f"Motion Sensor {sensor_mac[-8:]}", 'short_range')
            else:
                logger.warning(f"Motion sensor {sensor_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired motion sensor - update tracking
            if gateway_radio_mac:
                device_manager.update_motion_sensor_tracking(sensor_mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
            
            logger.debug(f"Motion sensor {sensor_mac} online (RSSI: {rssi} dBm)")

    def _handle_door_hello(self, sensor_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from door sensor.

        Args:
            sensor_mac: Door sensor MAC
            hello_data: Parsed HELLO data
            sender_ip: Gateway IP that forwarded
            is_paired: Whether sensor is paired
        """
        rssi = hello_data.get('rssi', 0)
        battery_type = hello_data.get('battery_type', 'li_ion')

        # Check if this device has a pending OTA validation
        session = ota_manager.get_session(sensor_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')

            if current_version == expected_version:
                logger.info(f"✅ OTA validation SUCCESS: {sensor_mac} now running {current_version}")
                ota_manager.update_session_state(sensor_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ OTA validation FAILED: {sensor_mac} - {error_msg}")
                ota_manager.update_session_state(sensor_mac, OTAState.FAILED, error_msg)

        # Find gateway radio MAC by sender IP
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break

        if not is_paired:
            # UNPAIRED - Check pairing mode
            pairing_mode_active = pairing_manager.is_pairing_allowed(sensor_mac, DEV_DOOR, rssi)
            rssi_auto_pair = self._can_secure_short_range_auto_pair(sensor_mac, hello_data, rssi)

            if rssi_auto_pair or pairing_mode_active:
                if self._try_start_v2_pairing(sensor_mac, DEV_DOOR, hello_data, sender_ip):
                    return
                # AUTO-PAIR or PAIRING MODE
                logger.info(f"🚪 Door sensor {sensor_mac} RSSI: {rssi} dBm → Pairing...")
                self._send_pair_confirm(sensor_mac, sender_ip)

                # Auto-register door sensor
                sensor = device_manager.get_door_sensor_by_mac(sensor_mac)
                if not sensor:
                    device_manager.add_door_sensor(sensor_mac, f"Door Sensor {sensor_mac[-8:]}")

                # Update with version, platform, and RSSI from this HELLO packet
                device_manager.update_door_sensor_tracking(
                    sensor_mac,
                    gateway_radio_mac,
                    rssi,
                    version=hello_data.get('version'),
                    platform=hello_data.get('platform'),
                    battery_type=battery_type
                )

                if pairing_mode_active:
                    logger.info(f"🚪 Paired door sensor via pairing mode: {sensor_mac}")
                    pairing_manager.record_device_paired(sensor_mac, DEV_DOOR, f"Door Sensor {sensor_mac[-8:]}", 'long_range')
                else:
                    logger.info(f"🚪 Auto-paired door sensor (RSSI: {rssi} dBm): {sensor_mac}")
                    pairing_manager.record_device_paired(sensor_mac, DEV_DOOR, f"Door Sensor {sensor_mac[-8:]}", 'short_range')
            else:
                logger.warning(f"Door sensor {sensor_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired door sensor - ensure it exists and update tracking
            sensor = device_manager.get_door_sensor_by_mac(sensor_mac)
            if not sensor:
                logger.info(f"🚪 Re-registering previously paired door sensor: {sensor_mac}")
                device_manager.add_door_sensor(sensor_mac, f"Door Sensor {sensor_mac[-8:]}")
                pairing_manager.record_device_paired(sensor_mac, DEV_DOOR, f"Door Sensor {sensor_mac[-8:]}", 'short_range')

            device_manager.update_door_sensor_tracking(
                sensor_mac,
                gateway_radio_mac,
                rssi,
                version=hello_data.get('version'),
                platform=hello_data.get('platform'),
                battery_type=battery_type
            )

            logger.debug(f"Door sensor {sensor_mac} online (RSSI: {rssi} dBm)")
    
    def _handle_plugin_hello(self, device_mac: str, hello_data: Dict, sender_ip: str, is_paired: bool):
        """Handle HELLO from plugin device (0xFE marker).
        
        Plugin devices advertise with UUID marker for discovery. When unpaired, device sends:
        - type=0xFE (DEV_PLUGIN_MARKER)
        - plugin_uuid: 16-byte UUID identifying plugin
        - plugin_device_type: Device type within plugin namespace (0-255)
        
        After pairing, MAC is bound to plugin_id for routing.
        
        Args:
            device_mac: Plugin device MAC
            hello_data: Parsed HELLO data with 'plugin_uuid' key
            sender_ip: Gateway IP that forwarded
            is_paired: Whether device is already paired
        """
        plugin_uuid = hello_data.get('plugin_uuid')
        plugin_device_type = hello_data.get('plugin_device_type', 0)
        rssi = hello_data.get('rssi', 0)

        if not plugin_uuid:
            logger.warning(f"Plugin HELLO from {device_mac} missing UUID")
            return

        # Lookup plugin by UUID
        if not self._plugin_manager:
            logger.warning(f"Plugin manager not available, cannot route plugin device {device_mac}")
            return

        plugin = self._plugin_manager.get_plugin_by_uuid(plugin_uuid)
        if not plugin:
            logger.warning(f"No plugin found for UUID {plugin_uuid}")
            return

        plugin_id = plugin.get('plugin_id', plugin_uuid)
        logger.info(f"🔌 Plugin device {device_mac} UUID={plugin_uuid} type={plugin_device_type} plugin={plugin_id}")

        # Check for pending OTA validation (device just rebooted after OTA)
        session = ota_manager.get_session(device_mac)
        if session and session.state == OTAState.VALIDATING:
            expected_version = f"{session.version[0]}.{session.version[1]}.{session.version[2]}"
            current_version = hello_data.get('version', '0.0.0')
            if current_version == expected_version:
                logger.info(f"✅ Plugin OTA validation SUCCESS: {device_mac} now running {current_version}")
                ota_manager.update_session_state(device_mac, OTAState.COMPLETE, "Firmware validated successfully")
            else:
                error_msg = f"Version mismatch: expected {expected_version}, got {current_version}"
                logger.error(f"❌ Plugin OTA validation FAILED: {device_mac} - {error_msg}")
                ota_manager.update_session_state(device_mac, OTAState.FAILED, error_msg)

        if not is_paired:
            try:
                pairing_mode_active = pairing_manager.is_pairing_allowed(device_mac, DEV_PLUGIN_MARKER, rssi)
            except Exception:
                pairing_mode_active = False

            rssi_auto_pair = self._can_secure_short_range_auto_pair(device_mac, hello_data, rssi)
            if rssi_auto_pair or pairing_mode_active:
                sec_flags = config_manager.get_security_flags()
                if sec_flags.get('require_v2') and hello_data.get('proto_version', 1) < PROTO_VERSION_V2:
                    logger.warning(f"SECURITY: Refused legacy v1 pairing for plugin device {device_mac} (require_v2=true)")
                    return
                if self._try_start_v2_pairing(device_mac, DEV_PLUGIN_MARKER, hello_data, sender_ip):
                    return
                # Confirm pairing to device via gateway
                try:
                    self._send_pair_confirm(device_mac, sender_ip)
                except Exception:
                    logger.exception(f"Failed to send pair confirm to {device_mac}")

                # Bind MAC to plugin id for fast routing
                try:
                    self._plugin_manager.bind_mac_to_plugin(device_mac, plugin_id)
                except Exception:
                    logger.exception(f"Failed to bind {device_mac} to plugin {plugin_id}")

                device_name = f"Plugin Device"
                try:
                    for device in (plugin.get('devices') or []):
                        if int(device.get('device_type', -1)) == int(plugin_device_type):
                            manifest_name = str(device.get('name') or '').strip()
                            if manifest_name:
                                device_name = manifest_name
                            break
                except Exception:
                    logger.exception(f"Failed to resolve manifest device name for {device_mac}")

                # Record device paired
                try:
                    mode = 'long_range' if pairing_mode_active else 'short_range'
                    pairing_manager.record_device_paired(
                        device_mac,
                        f"{plugin_device_type}:{plugin_id}",
                        f"{device_name} {device_mac[-8:]}",
                        mode
                    )
                    is_paired = True
                except Exception:
                    logger.exception(f"Failed to record pairing for {device_mac}")
            else:
                logger.info(f"🔌 Plugin device {device_mac} RSSI too weak for auto-pairing: {rssi} dBm")
        
        else:
            # For already paired plugin devices, ensure MAC is bound to plugin for routing
            try:
                self._plugin_manager.bind_mac_to_plugin(device_mac, plugin_id)
            except Exception:
                logger.exception(f"Failed to bind {device_mac} to plugin {plugin_id}")

        # Dispatch to plugin for post-pairing actions (plugin may ignore unpaired)
        try:
            gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
            self._plugin_manager.dispatch_event('plugin_hello', {
                'device_mac': device_mac,
                'plugin_uuid': plugin_uuid,
                'plugin_device_type': plugin_device_type,
                'plugin_id': plugin_id,
                'rssi': rssi,
                'version': hello_data.get('version'),
                'platform': hello_data.get('platform'),
                'sender_ip': sender_ip,
                'gateway_radio_mac': gateway_radio_mac,
                'is_paired': is_paired,
            })
        except Exception:
            logger.exception(f"Failed to dispatch plugin_hello for {device_mac} to plugin {plugin_id}")
    
    def _handle_button_event(self, button_mac: str, payload: bytes, sender_ip: str):
        """Handle button event.
        
        Args:
            button_mac: Button MAC
            payload: Raw payload
            sender_ip: Gateway IP that forwarded
        """
        event_data = self.decoder.parse_button_event(payload)
        if not event_data:
            logger.warning(f"Invalid button event from {button_mac}")
            return
        
        action = event_data['action']
        battery_mv = event_data.get('battery_mv')
        button_index = event_data.get('button_index')
        version = event_data.get('version')
        platform = event_data.get('platform')
        button_count = event_data.get('button_count')
        
        action_str = {1: "CLICK", 2: "HOLD", 3: "RELEASE", 9: "SYNC"}.get(action, f"UNKNOWN({action})")
        
        # Determine if this is a normal button (index -1) or remote (index >= 0)
        is_remote = button_index is not None and button_index >= 0
        
        if is_remote:
            logger.info(f"🔘 Remote {button_mac} button {button_index} -> {action_str}")
        else:
            logger.info(f"🔘 Button {button_mac} -> {action_str}")
        
        # Auto-add button/remote if it doesn't exist
        button = device_manager.get_button_by_mac(button_mac)
        if not button:
            if is_remote:
                logger.info(f"Auto-registering remote: {button_mac}")
                device_manager.add_button(button_mac, f"Remote {button_mac[-8:]}", device_type=DEV_REMOTE)
                pairing_manager.record_device_paired(button_mac, DEV_REMOTE, f"Remote {button_mac[-8:]}", 'short_range')
            else:
                logger.info(f"Auto-registering button: {button_mac}")
                device_manager.add_button(button_mac, f"Button {button_mac[-8:]}")
                pairing_manager.record_device_paired(button_mac, DEV_BUTTON, f"Button {button_mac[-8:]}", 'short_range')
        
        # Find gateway for tracking
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if gateway_radio_mac:
            device_manager.update_button_tracking(
                button_mac, 
                gateway_radio_mac, 
                0, 
                battery_mv=battery_mv,
                version=version,
                platform=platform,
                button_count=button_count
            )
        
        # Call event handler
        if self._button_event_handler:
            self._button_event_handler(button_mac, action, 0, button_index=button_index)
    
    def _handle_motion_event(self, sensor_mac: str, payload: bytes, sender_ip: str):
        """Handle motion sensor event.
        
        Args:
            sensor_mac: Motion sensor MAC
            payload: Raw payload
            sender_ip: Gateway IP that forwarded
        """
        event_data = self.decoder.parse_motion_event(payload)
        if not event_data:
            logger.warning(f"Invalid motion event from {sensor_mac}")
            return
        
        action = event_data['action']
        battery_mv = event_data.get('battery_mv')
        light_level = event_data.get('light_level')
        version = event_data.get('version')
        platform = event_data.get('platform')
        
        action_str = {10: "MOTION_DETECTED", 9: "SYNC"}.get(action, f"UNKNOWN({action})")
        
        logger.info(f"🏃 Motion sensor {sensor_mac} -> {action_str} (light_level: {light_level})")
        
        # Auto-add motion sensor if it doesn't exist
        sensor = device_manager.get_motion_sensor_by_mac(sensor_mac)
        if not sensor:
            logger.info(f"Auto-registering motion sensor: {sensor_mac}")
            device_manager.add_motion_sensor(sensor_mac, f"Motion Sensor {sensor_mac[-8:]}")
            pairing_manager.record_device_paired(sensor_mac, DEV_MOTION, f"Motion Sensor {sensor_mac[-8:]}", 'short_range')
        
        # Find gateway for tracking
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
        
        if gateway_radio_mac:
            device_manager.update_motion_sensor_tracking(
                sensor_mac, 
                gateway_radio_mac, 
                0,  # RSSI not available in motion event packets
                battery_mv=battery_mv,
                light_level=light_level,
                version=version,
                platform=platform
            )
        
        # Update last_motion timestamp
        sensors = device_manager.get_all_motion_sensors()
        for s in sensors:
            if s.get('mac_address', '').upper() == sensor_mac.upper():
                
                def update_func(sensors_list):
                    for sensor in sensors_list:
                        if sensor.get('mac_address', '').upper() == sensor_mac.upper():
                            sensor['last_motion'] = datetime.now().isoformat()
                            break
                    return sensors_list
                
                data_manager.update_json(FILE_MOTION_SENSORS, update_func)
                
                # Check if there's a pending cooldown update
                if s.get('pending_cooldown_update'):
                    cooldown_seconds = s.get('config', {}).get('cooldown_seconds', 60)
                    logger.info(f"🔧 Sending cooldown update to {sensor_mac}: {cooldown_seconds}s")
                    
                    # Send cooldown command to sensor with delivery tracking and feedback
                    try:
                        # Generate message ID
                        with self._delivery_lock:
                            msg_id = self._next_msg_id
                            self._next_msg_id = (self._next_msg_id + 1) % 256
                        
                        # Encode packet with uint32 cooldown value (dual-stack v1/v2)
                        packet = self._encode_sys_cmd_dual(
                            sensor_mac,
                            CMD_SET_MOTION_COOLDOWN,
                            value=0,
                            msg_id=msg_id,
                            value_uint32=cooldown_seconds
                        )
                        
                        # Send with automatic gateway routing, delivery tracking, and failover
                        success = self._send_with_fallback(sensor_mac, packet, wait_for_delivery=True, msg_id=msg_id)
                        
                        if success:
                            logger.info(f"✅ Cooldown command delivered to {sensor_mac}: {cooldown_seconds}s")
                            
                            # Clear the pending flag on successful delivery
                            def clear_flag(sensors_list):
                                for sensor in sensors_list:
                                    if sensor.get('mac_address', '').upper() == sensor_mac.upper():
                                        sensor['pending_cooldown_update'] = False
                                        break
                                return sensors_list
                            
                            data_manager.update_json(FILE_MOTION_SENSORS, clear_flag)
                        else:
                            logger.warning(f"⚠️  Cooldown command failed to deliver to {sensor_mac}, will retry on next motion event")
                            
                    except Exception as e:
                        logger.error(f"Failed to send cooldown update to {sensor_mac}: {e}")
                
                break
        
        # Call event handler
        if self._motion_event_handler:
            self._motion_event_handler(sensor_mac, action, light_level, battery_mv)

    def _handle_door_event(self, sensor_mac: str, payload: bytes, sender_ip: str):
        """Handle door sensor event.

        Args:
            sensor_mac: Door sensor MAC
            payload: Raw payload
            sender_ip: Gateway IP that forwarded
        """
        event_data = self.decoder.parse_door_event(payload)
        if not event_data:
            logger.warning(f"Invalid door event from {sensor_mac}")
            return

        action = event_data['action']
        battery_mv = event_data.get('battery_mv')
        light_level = event_data.get('light_level')
        version = event_data.get('version')
        platform = event_data.get('platform')
        battery_type = event_data.get('battery_type', 'li_ion')

        action_str = {
            11: "DOOR_OPENED",
            12: "DOOR_CLOSED",
            9: "SYNC"
        }.get(action, f"UNKNOWN({action})")

        logger.info(f"🚪 Door sensor {sensor_mac} -> {action_str} (light_level: {light_level})")

        # Auto-add door sensor if it doesn't exist
        sensor = device_manager.get_door_sensor_by_mac(sensor_mac)
        if not sensor:
            logger.info(f"Auto-registering door sensor: {sensor_mac}")
            device_manager.add_door_sensor(sensor_mac, f"Door Sensor {sensor_mac[-8:]}")
            pairing_manager.record_device_paired(sensor_mac, DEV_DOOR, f"Door Sensor {sensor_mac[-8:]}", 'short_range')

        # Find gateway for tracking
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break

        device_manager.update_door_sensor_tracking(
            sensor_mac,
            gateway_radio_mac,
            None,  # RSSI is not present in door event packets
            battery_mv=battery_mv,
            light_level=light_level,
            version=version,
            platform=platform,
            battery_type=battery_type,
            action=action
        )

        # Call event handler
        if self._door_event_handler:
            self._door_event_handler(sensor_mac, action, light_level, battery_mv)
    
    def _send_motion_sleep_command(self, sensor_mac: str, sleep_seconds: int):
        """Send one-time sleep command to motion sensor.
        
        Args:
            sensor_mac: Motion sensor MAC address
            sleep_seconds: Sleep duration in seconds (1-60)
        """
        try:
            # Validate duration
            if sleep_seconds < 1 or sleep_seconds > 60:
                logger.warning(f"Invalid sleep duration {sleep_seconds}s for {sensor_mac}, clamping to 1-60 range")
                sleep_seconds = max(1, min(60, sleep_seconds))
            
            logger.info(f"💤 Sending one-time sleep command to {sensor_mac}: {sleep_seconds}s")
            
            # Generate message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Encode packet with uint32 sleep duration (dual-stack v1/v2)
            packet = self._encode_sys_cmd_dual(
                sensor_mac,
                CMD_SET_MOTION_SLEEP,
                value=0,
                msg_id=msg_id,
                value_uint32=sleep_seconds
            )
            
            # Send with automatic gateway routing (no need to wait for delivery since device sleeps immediately)
            success = self._send_with_fallback(sensor_mac, packet, wait_for_delivery=False, msg_id=msg_id)
            
            if success:
                logger.info(f"✅ Sleep command sent to {sensor_mac}: {sleep_seconds}s")
            else:
                logger.warning(f"⚠️  Sleep command may not have reached {sensor_mac}")
                
        except Exception as e:
            logger.error(f"Failed to send sleep command to {sensor_mac}: {e}")
    
    def _handle_delivery_report(self, payload: bytes, sender_ip: str):
        """Handle delivery report from gateway.
        
        Args:
            payload: Raw payload
            sender_ip: Gateway IP
        """
        report_data = self.decoder.parse_delivery_report(payload)
        if not report_data:
            logger.warning("Invalid delivery report")
            return
                
        msg_id = report_data['msg_id']
        success = report_data['success']
        target_mac = report_data['target_mac']
        
        status_str = "✅ Delivered" if success else "❌ Failed"
        logger.info(f"📡 Report msgID={msg_id}: {target_mac} -> {status_str} (via {sender_ip})")
        
        # Update pending delivery
        with self._delivery_lock:
            if msg_id in self._pending_deliveries:
                self._pending_deliveries[msg_id]['result']['success'] = success
                self._pending_deliveries[msg_id]['event'].set()  # Wake up waiting thread
    
    def _handle_ping_response(self, src_mac: str, payload: bytes, sender_ip: str):
        """Handle ping response from gateway.
        
        Args:
            src_mac: Gateway WiFi MAC
            payload: Raw payload with uptime
            sender_ip: Gateway IP
        """
        try:
            uptime_seconds = struct.unpack('<I', payload[:4])[0]
            logger.info(f"🏓 Ping response from {src_mac}: Uptime {uptime_seconds}s ({sender_ip})")
            
            # Notify waiting thread
            with self._ping_lock:
                if src_mac in self._pending_pings:
                    self._pending_pings[src_mac]['uptime'] = uptime_seconds
                    self._pending_pings[src_mac]['received'] = True
                    self._pending_pings[src_mac]['event'].set()
        except Exception as e:
            logger.error(f"Failed to parse ping response: {e}")
    
    def _send_pair_confirm(self, target_mac: str, gateway_ip: str):
        """Send pairing confirmation to device.
        
        Args:
            target_mac: Device MAC to pair
            gateway_ip: Gateway IP to route through
        """
        packet = self.encoder.encode_pair_confirm(target_mac, self.home_id)
        if self._send_packet_to_gateway(gateway_ip, packet):
            logger.debug(f"Sent PAIR_CONFIRM to {target_mac} via {gateway_ip}")

    def _try_start_v2_pairing(self, device_mac: str, device_type: int, hello_data: Dict, sender_ip: str) -> bool:
        """Initiate Security v2 ECDH+PIN/TOFU handshake if enabled and device supports v2.

        Returns True if handled as v2 (or blocked from legacy v1 fallback).
        """
        sec_flags = config_manager.get_security_flags()
        proto_ver = hello_data.get('proto_version', 1)
        active_pin = pairing_manager.get_active_pin()

        if sec_flags['security_v2_pair']:
            if proto_ver >= PROTO_VERSION_V2 and hello_data.get('ecdh_pub'):
                frame = pairing_v2_manager.handle_hello_v2(
                    device_mac=device_mac,
                    device_type=device_type,
                    hello_data=hello_data,
                    home_id=self.home_id,
                    sender_ip=sender_ip,
                    store=security_store,
                    active_pin=active_pin,
                )
                if frame:
                    self._send_packet_to_gateway(sender_ip, frame)
                return True

            # Never fall back to v1 if device advertised v2 (e.g. in PIN backoff),
            # has an active/locked-out v2 handshake, or the user entered a 6-digit PIN.
            if (
                proto_ver >= PROTO_VERSION_V2
                or pairing_v2_manager.has_pending(device_mac)
                or pairing_v2_manager.is_locked_out(device_mac)
                or active_pin
            ):
                logger.warning(
                    f"SECURITY: Refused v1 fallback for {device_mac} "
                    f"(proto_ver={proto_ver}, pin_entered={bool(active_pin)}, "
                    f"pending_v2={pairing_v2_manager.has_pending(device_mac)})"
                )
                return True

        if sec_flags['require_v2']:
            logger.warning(
                f"SECURITY: Refused v1 pairing for {device_mac} (type={device_type}) because require_v2 is enabled"
            )
            return True

        return False

    def _push_key_distrib_to_gateways(self, target_mac: str, crypto_mode: str, max_frame: int, k_ack: bytes, action: int = KEY_DISTRIB_UPSERT):
        """Push per-device K_ack & mode entry to all v2-capable gateways."""
        with self._gateway_lock:
            gateways_snapshot = list(self._gateway_table.items())

        for radio_mac, info in gateways_snapshot:
            if not security_store.is_v2_device(radio_mac):
                continue
            try:
                pkt = encode_key_distrib(
                    gw_radio_mac=radio_mac,
                    target_mac=target_mac,
                    crypto_mode=crypto_mode,
                    max_frame=max_frame,
                    k_ack=k_ack,
                    action=action,
                    store=security_store,
                )
                self._send_packet_to_gateway(info['ip_address'], pkt)
            except Exception as exc:
                logger.error(f"Failed to push PKT_KEY_DISTRIB for {target_mac} to gateway {radio_mac}: {exc}")

    def _sync_all_v2_keys_to_gateway(self, gw_radio_mac: str, gateway_endpoint: str):
        """Push K_ack & mode entries for all v2 devices (and prune revoked ones) to one v2-paired gateway."""
        if not security_store.is_v2_device(gw_radio_mac):
            return
        devices = security_store.get_all_devices_snapshot()
        pushed = 0
        pruned = 0
        for dev_mac, rec in devices.items():
            if dev_mac.upper() == gw_radio_mac.upper():
                continue
            if rec.get("revoked"):
                try:
                    pkt = encode_key_distrib(
                        gw_radio_mac=gw_radio_mac,
                        target_mac=dev_mac,
                        crypto_mode=CRYPTO_MODE_V1,
                        max_frame=V2_MAX_FRAME_ESP8266,
                        k_ack=b"\x00" * 16,
                        action=KEY_DISTRIB_REMOVE,
                        store=security_store,
                    )
                    if self._send_packet_to_gateway(gateway_endpoint, pkt):
                        pruned += 1
                except Exception as exc:
                    logger.error(f"Failed pruning revoked K_ack for {dev_mac} on gateway {gw_radio_mac}: {exc}")
                continue
            mode = rec.get("crypto_mode", "v1")
            if mode not in ("v2", "v2-unverified"):
                continue
            try:
                pkt = encode_key_distrib(
                    gw_radio_mac=gw_radio_mac,
                    target_mac=dev_mac,
                    crypto_mode=mode,
                    max_frame=int(rec.get("max_frame", 250)),
                    k_ack=security_store.derive_k_ack(dev_mac),
                    action=KEY_DISTRIB_UPSERT,
                    store=security_store,
                )
                if self._send_packet_to_gateway(gateway_endpoint, pkt):
                    pushed += 1
            except Exception as exc:
                logger.error(f"Failed syncing K_ack for {dev_mac} to gateway {gw_radio_mac}: {exc}")
        if pushed > 0 or pruned > 0:
            logger.info(f"🔑 Synced {pushed} v2 device K_ack key(s) and pruned {pruned} revoked key(s) on gateway {gw_radio_mac}")

    def _finalize_v2_paired_device(self, result: Dict):
        """Register a device after its Flight 3 PKT_PAIR_ACK succeeds and push K_ack to gateways."""
        mac = result['device_mac']
        dev_type = result['device_type']
        sender_ip = result['sender_ip']
        hello_data = result.get('hello_data', {})
        rssi = hello_data.get('rssi', -100)
        gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)

        was_upgrade = security_store.is_pending_v2_upgrade(mac)
        security_store.set_pending_v2_upgrade(mac, False)

        if dev_type == DEV_GATEWAY:
            gw = device_manager.get_gateway_by_radio_mac(mac) or device_manager.get_gateway_by_wifi_mac(mac)
            gw_wifi_mac = gw.get('mac_address', mac) if gw else mac
            gw_name = gw.get('name', f"Gateway {gw_wifi_mac[-8:]}") if gw else f"Gateway {mac[-8:]}"
            pairing_manager.record_device_paired(gw_wifi_mac, DEV_GATEWAY, gw_name, 'wifi')
            logger.info(f"🌐 Gateway node {mac} upgraded to Security v2 ({result['crypto_mode']})")
            # Only push K_ack peer table to the radio node (not the WiFi net node)
            if gw and gw.get('radio_mac', '').upper() == mac.upper():
                self._sync_all_v2_keys_to_gateway(mac, sender_ip)
            return

        if dev_type == DEV_BUTTON:
            existing = device_manager.get_button_by_mac(mac)
            name = existing.get('name', f"Button {mac[-8:]}") if existing else f"Button {mac[-8:]}"
            if not existing:
                device_manager.add_button(mac, name)
            if gateway_radio_mac:
                device_manager.update_button_tracking(mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
            if not was_upgrade:
                pairing_manager.record_device_paired(mac, DEV_BUTTON, name, 'long_range')
        elif dev_type == DEV_REMOTE:
            existing = device_manager.get_button_by_mac(mac)
            name = existing.get('name', f"Remote {mac[-8:]}") if existing else f"Remote {mac[-8:]}"
            btn_count = hello_data.get('button_count', 4)
            if not existing:
                device_manager.add_button(mac, name, device_type=DEV_REMOTE, button_count=btn_count)
            if gateway_radio_mac:
                device_manager.update_button_tracking(mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'), button_count=btn_count)
            if not was_upgrade:
                pairing_manager.record_device_paired(mac, DEV_REMOTE, name, 'long_range')
        elif dev_type == DEV_LIGHT:
            existing = device_manager.get_light_by_mac(mac)
            name = existing.get('name', f"Light {mac[-8:]}") if existing else f"Light {mac[-8:]}"
            if not existing:
                device_manager.add_lightstrip(mac, name, hello_data.get('num_leds', 60), hello_data.get('is_rgbw', False), model_id=hello_data.get('model_id'))
            if gateway_radio_mac:
                device_manager.update_light_gateway(mac, gateway_radio_mac, rssi=rssi, version=hello_data.get('version'), platform=hello_data.get('platform'), model_id=hello_data.get('model_id'))
            if not was_upgrade:
                pairing_manager.record_device_paired(mac, DEV_LIGHT, name, 'long_range')
        elif dev_type == DEV_MOTION:
            existing = device_manager.get_motion_sensor_by_mac(mac)
            name = existing.get('name', f"Motion Sensor {mac[-8:]}") if existing else f"Motion Sensor {mac[-8:]}"
            if not existing:
                device_manager.add_motion_sensor(mac, name)
            if gateway_radio_mac:
                device_manager.update_motion_sensor_tracking(mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'))
            if not was_upgrade:
                pairing_manager.record_device_paired(mac, DEV_MOTION, name, 'long_range')
        elif dev_type == DEV_DOOR:
            existing = device_manager.get_door_sensor_by_mac(mac)
            name = existing.get('name', f"Door Sensor {mac[-8:]}") if existing else f"Door Sensor {mac[-8:]}"
            if not existing:
                device_manager.add_door_sensor(mac, name)
            device_manager.update_door_sensor_tracking(mac, gateway_radio_mac, rssi, version=hello_data.get('version'), platform=hello_data.get('platform'), battery_type=hello_data.get('battery_type', 'li_ion'))
            if not was_upgrade:
                pairing_manager.record_device_paired(mac, DEV_DOOR, name, 'long_range')
        elif dev_type == DEV_PLUGIN_MARKER:
            plugin_uuid = hello_data.get('plugin_uuid')
            plugin_device_type = hello_data.get('plugin_device_type', 0)
            plugin = self._plugin_manager.get_plugin_by_uuid(plugin_uuid) if (self._plugin_manager and plugin_uuid) else None
            plugin_id = (plugin.get('plugin_id') or plugin.get('id') or plugin_uuid) if plugin else plugin_uuid
            if self._plugin_manager and plugin_id:
                try:
                    self._plugin_manager.bind_mac_to_plugin(mac, plugin_id)
                except Exception:
                    logger.exception(f"Failed to bind v2 plugin device {mac} to plugin {plugin_id}")
            device_name = "Plugin Device"
            if plugin:
                try:
                    for device in (plugin.get('devices') or []):
                        if int(device.get('device_type', -1)) == int(plugin_device_type):
                            manifest_name = str(device.get('name') or '').strip()
                            if manifest_name:
                                device_name = manifest_name
                            break
                except Exception:
                    logger.exception(f"Failed to resolve manifest device name for v2 plugin device {mac}")
            if not was_upgrade and plugin_id:
                pairing_manager.record_device_paired(
                    mac,
                    f"{plugin_device_type}:{plugin_id}",
                    f"{device_name} {mac[-8:]}",
                    'long_range',
                )
            if self._plugin_manager and plugin_id:
                try:
                    self._plugin_manager.dispatch_event('plugin_hello', {
                        'device_mac': mac,
                        'plugin_uuid': plugin_uuid,
                        'plugin_device_type': plugin_device_type,
                        'plugin_id': plugin_id,
                        'rssi': rssi,
                        'version': hello_data.get('version'),
                        'platform': hello_data.get('platform'),
                        'sender_ip': sender_ip,
                        'gateway_radio_mac': gateway_radio_mac,
                        'is_paired': True,
                    })
                except Exception:
                    logger.exception(f"Failed to dispatch plugin_hello for v2 device {mac} to plugin {plugin_id}")

        self._push_key_distrib_to_gateways(
            target_mac=mac,
            crypto_mode=result['crypto_mode'],
            max_frame=result['max_frame'],
            k_ack=result['k_ack'],
            action=KEY_DISTRIB_UPSERT,
        )

    def _resolve_gateway_endpoint_for_mac(self, mac: str) -> Optional[str]:
        """Resolve the best gateway endpoint (IP or serial://) to reach a given device MAC."""
        norm = MACFormatter.format_with_colons(mac)
        gw = device_manager.get_gateway_by_wifi_mac(norm) or device_manager.get_gateway_by_radio_mac(norm)
        if gw:
            return gw.get('transport_endpoint') or gw.get('ip_address')

        preferred_gw_mac = None
        light = device_manager.get_light_by_mac(norm)
        if light:
            preferred_gw_mac = light.get('gateway_mac') or light.get('last_gateway')
        if not preferred_gw_mac:
            btn = device_manager.get_button_by_mac(norm)
            if btn:
                preferred_gw_mac = btn.get('last_gateway') or btn.get('gateway_mac')
        if not preferred_gw_mac:
            motion = device_manager.get_motion_sensor_by_mac(norm)
            if motion:
                preferred_gw_mac = motion.get('last_gateway') or motion.get('gateway_mac')
        if not preferred_gw_mac:
            door = device_manager.get_door_sensor_by_mac(norm)
            if door:
                preferred_gw_mac = door.get('last_gateway') or door.get('gateway_mac')

        with self._gateway_lock:
            if preferred_gw_mac and preferred_gw_mac in self._gateway_table:
                return self._gateway_table[preferred_gw_mac]['ip_address']
            for info in self._gateway_table.values():
                if info.get('ip_address'):
                    return info['ip_address']
        return None

    def _use_v2_for_peer(self, mac: str) -> bool:
        """Return True if outbound packets to `mac` should be encrypted under Security v2."""
        sec_flags = config_manager.get_security_flags()
        return bool(sec_flags.get('security_v2_send') and security_store.is_v2_device(mac))

    def _encode_v2_for_peer(self, pkt_type: int, target_mac: str, plaintext: bytes, msg_id: int = 0) -> bytes:
        return PacketEncoderV2.encode(
            pkt_type=pkt_type,
            src_mac="00:00:00:00:00:00",
            dst_mac=target_mac,
            msg_id=msg_id,
            counter=security_store.next_backend_tx_counter(),
            key_epoch=security_store.epoch,
            plaintext=plaintext,
            key=security_store.derive_k_own(target_mac),
        )

    def _encode_sys_cmd_dual(self, target_mac: str, command: int, value: int = 0, msg_id: int = 0, value_uint32: Optional[int] = None) -> bytes:
        if self._use_v2_for_peer(target_mac):
            if value_uint32 is not None:
                pt = struct.pack('<BBI', command & 0xFF, value & 0xFF, value_uint32 & 0xFFFFFFFF)
            else:
                pt = struct.pack('<BB', command & 0xFF, value & 0xFF)
            return self._encode_v2_for_peer(PKT_SYS_CMD, target_mac, pt, msg_id)
        return self.encoder.encode_sys_cmd(target_mac, command, value, msg_id, value_uint32)

    def _encode_gateway_list_dual(self, target_mac: str, gateway_macs: List[str], msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            macs = gateway_macs[:MAX_GATEWAYS_PER_PACKET]
            pt = bytearray(1 + MAX_GATEWAYS_PER_PACKET * 6)
            pt[0] = len(macs)
            for idx, m in enumerate(macs):
                pt[1 + idx * 6:1 + (idx + 1) * 6] = MACFormatter.to_bytes(m)
            return self._encode_v2_for_peer(PKT_GW_LIST_UPD, target_mac, bytes(pt), msg_id)
        return self.encoder.encode_gateway_list_for_device(target_mac, gateway_macs, msg_id)

    def _encode_ping_dual(self, gateway_mac: str, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(gateway_mac):
            return self._encode_v2_for_peer(PKT_PING, gateway_mac, b'\x00', msg_id)
        return self.encoder.encode_ping(gateway_mac)

    def _encode_ping_device_dual(self, device_mac: str, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(device_mac):
            return self._encode_v2_for_peer(PKT_PING_DEVICE, device_mac, b'\x00', msg_id)
        return self.encoder.encode_ping_device(device_mac)

    def _encode_ota_notify_dual(self, target_mac: str, firmware_size: int, sha256_hash: bytes, version: Tuple[int, int, int, int], msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            pt = struct.pack('<I', firmware_size & 0xFFFFFFFF) + sha256_hash[:32] + bytes(version[:4])
            return self._encode_v2_for_peer(PKT_OTA_NOTIFY, target_mac, pt, msg_id)
        return self.encoder.encode_ota_notify(target_mac, firmware_size, sha256_hash, version, msg_id)

    def _encode_ota_chunk_dual(self, target_mac: str, chunk_index: int, chunk_data: bytes, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            return PacketEncoderV2.encode_ota_chunk_v2(
                target_mac=target_mac,
                chunk_index=chunk_index,
                chunk_data=chunk_data,
                counter=security_store.next_backend_tx_counter(),
                key_epoch=security_store.epoch,
                key=security_store.derive_k_own(target_mac),
                max_frame=security_store.get_max_frame(target_mac),
                msg_id=msg_id,
            )
        return self.encoder.encode_ota_chunk(target_mac, chunk_index, chunk_data, msg_id)

    def _encode_ota_checkpoint_req_dual(self, target_mac: str, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            return self._encode_v2_for_peer(PKT_OTA_CHECKPOINT_REQ, target_mac, b'\x00', msg_id)
        return self.encoder.encode_ota_checkpoint_req(target_mac, msg_id)

    def _encode_ota_complete_dual(self, target_mac: str, sha256_hash: bytes, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            return self._encode_v2_for_peer(PKT_OTA_COMPLETE, target_mac, sha256_hash[:32], msg_id)
        return self.encoder.encode_ota_complete(target_mac, sha256_hash, msg_id)

    def _encode_ota_abort_dual(self, target_mac: str, reason_code: int = 0, msg_id: int = 0) -> bytes:
        if self._use_v2_for_peer(target_mac):
            return self._encode_v2_for_peer(PKT_OTA_ABORT, target_mac, bytes([reason_code & 0xFF]), msg_id)
        return self.encoder.encode_ota_abort(target_mac, reason_code=reason_code, msg_id=msg_id)

    def _ota_chunk_size_for_peer(self, target_mac: str) -> int:
        if self._use_v2_for_peer(target_mac):
            max_frame = security_store.get_max_frame(target_mac)
            # 38 B v2 overhead + 4 B Payload_OtaChunkV2Header, capped at 1024 B so UART/ESP-NOW
            # buffers never overflow during batched checkpoint transfers.
            return max(64, min(max_frame - V2_OVERHEAD - 4, 1024))
        return OTA_CHUNK_DATA_SIZE

    def _send_v2_set_pin(self, target_mac: str, pin: str, gateway_endpoint: str) -> bool:
        """Send encrypted v2 PKT_SYS_CMD (CMD_SET_PIN) containing a 6-digit ASCII PIN."""
        if not security_store.is_v2_device(target_mac):
            return False
        if not pin or len(pin) != 6 or not pin.isdigit():
            return False
        plaintext = bytes([CMD_SET_PIN, 0x00]) + pin.encode('ascii')
        frame = PacketEncoderV2.encode(
            pkt_type=PKT_SYS_CMD,
            src_mac="00:00:00:00:00:00",
            dst_mac=target_mac,
            msg_id=0,
            counter=security_store.next_backend_tx_counter(),
            key_epoch=security_store.epoch,
            plaintext=plaintext,
            key=security_store.derive_k_own(target_mac),
        )
        return self._send_packet_to_gateway(gateway_endpoint, frame)

    def _handle_v2_sys_cmd(self, src_mac: str, payload: bytes, sender_ip: str):
        """Handle authenticated v2 PKT_SYS_CMD responses from devices (e.g. CMD_SET_PIN_ACK)."""
        if len(payload) < 2:
            return
        cmd = payload[0]
        status = payload[1]
        if cmd == CMD_SET_PIN_ACK:
            pending_pin = security_store.get_pending_pin(src_mac)
            if status == PIN_ACK_OK and pending_pin:
                security_store.commit_pin(src_mac, pending_pin)
                logger.info(f"🔐 Confirmed 6-digit PIN provisioned on {src_mac}; upgraded to v2 Verified")
                rec = security_store.get_device_record(src_mac) or {}
                self._push_key_distrib_to_gateways(
                    target_mac=src_mac,
                    crypto_mode=CRYPTO_MODE_V2,
                    max_frame=int(rec.get('max_frame', 250)),
                    k_ack=security_store.derive_k_ack(src_mac),
                    action=KEY_DISTRIB_UPSERT,
                )
            elif status == PIN_ACK_ALREADY_SET:
                security_store.set_pending_pin(src_mac, None)
                logger.warning(f"🔐 Device {src_mac} refused CMD_SET_PIN: PIN already provisioned on device")
            else:
                security_store.set_pending_pin(src_mac, None)
                logger.warning(f"🔐 Device {src_mac} rejected CMD_SET_PIN (status={status:#x})")

    def _get_device_battery_percent(self, mac: str) -> Optional[int]:
        """Return the latest recorded battery percentage for a battery device, or None if unavailable."""
        btn = device_manager.get_button_by_mac(mac)
        if btn and btn.get('battery_percent') is not None:
            return int(btn['battery_percent'])
        motion = device_manager.get_motion_sensor_by_mac(mac)
        if motion and motion.get('battery_percent') is not None:
            return int(motion['battery_percent'])
        door = device_manager.get_door_sensor_by_mac(mac)
        if door and door.get('battery_percent') is not None:
            return int(door['battery_percent'])
        return None

    def _flush_pending_security_actions(self, src_mac: str, sender_ip: str, proto_ver: int):
        """Deliver queued v2 upgrade, PIN provisioning, or v2 auto-OTA while a device is awake."""
        if proto_ver == 1 and security_store.is_pending_v2_upgrade(src_mac) and not security_store.is_v2_device(src_mac):
            sec_flags = config_manager.get_security_flags()
            if sec_flags['security_v2_pair']:
                pkt = self.encoder.encode_sys_cmd(src_mac, CMD_UPGRADE_TO_V2, 0)
                if self._send_packet_to_gateway(sender_ip, pkt):
                    logger.info(f"🔐 Sent queued CMD_UPGRADE_TO_V2 to {src_mac} via {sender_ip}")
        elif proto_ver == PROTO_VERSION_V2:
            pending_pin = security_store.get_pending_pin(src_mac)
            if pending_pin:
                if self._send_v2_set_pin(src_mac, pending_pin, sender_ip):
                    logger.info(f"🔐 Sent queued encrypted CMD_SET_PIN to {src_mac} via {sender_ip}")

            # Wake-triggered auto-OTA for v2-authenticated battery devices (one active OTA at a time)
            session = ota_manager.get_session(src_mac)
            if session and session.state in (OTAState.QUEUED, OTAState.WAITING_READY) and self._use_v2_for_peer(src_mac):
                bat_pct = self._get_device_battery_percent(src_mac)
                if bat_pct is not None and bat_pct < OTA_MIN_BATTERY_PERCENT:
                    logger.warning(
                        f"🔋 Skipping auto-OTA for {src_mac}: battery is {bat_pct}% (< {OTA_MIN_BATTERY_PERCENT}%)"
                    )
                    return

                just_claimed = False
                if session.state == OTAState.QUEUED:
                    if not ota_manager.claim_active_session(src_mac):
                        logger.info(
                            f"⏳ Deferring wake auto-OTA for {src_mac}: another OTA is active ({ota_manager.active_session_mac})"
                        )
                        return
                    ota_manager.update_session_state(src_mac, OTAState.WAITING_READY)
                    just_claimed = True

                with self._delivery_lock:
                    msg_id = self._next_msg_id
                    self._next_msg_id = (self._next_msg_id + 1) % 256
                pkt = self._encode_ota_notify_dual(
                    src_mac,
                    session.firmware_size,
                    session.sha256_hash,
                    session.version,
                    msg_id,
                )
                if self._send_packet_to_gateway(sender_ip, pkt):
                    logger.info(f"📡 Auto-triggered queued v2 OTA_NOTIFY for {src_mac} on wake via {sender_ip}")
                    if just_claimed:
                        threading.Thread(
                            target=self._ota_ready_timeout,
                            args=(src_mac, session.ready_timeout_token),
                            daemon=True,
                            name=f"OTA-Ready-Timeout-{src_mac[-8:]}"
                        ).start()
                elif just_claimed:
                    ota_manager.requeue_session(src_mac, "Failed to send wake OTA_NOTIFY; re-queued")

    def request_v2_upgrade(self, mac: str) -> Dict:
        """Queue and immediately attempt an in-place v1 -> v2 TOFU upgrade for a 4.x.x device."""
        norm = MACFormatter.format_with_colons(mac)
        sec_flags = config_manager.get_security_flags()
        if not sec_flags.get('security_v2_pair'):
            return {'success': False, 'error': 'Enable Security v2 pairing first (Pairing page / security flags)'}
        if security_store.is_v2_device(norm):
            return {'success': False, 'error': 'Device is already using Security v2'}

        security_store.set_pending_v2_upgrade(norm, True)
        endpoint = self._resolve_gateway_endpoint_for_mac(norm)
        sent_now = False
        if endpoint:
            pkt = self.encoder.encode_sys_cmd(norm, CMD_UPGRADE_TO_V2, 0)
            sent_now = self._send_packet_to_gateway(endpoint, pkt)
            if sent_now:
                logger.info(f"🔐 Sent CMD_UPGRADE_TO_V2 to {norm} via {endpoint}")
            # If this is a gateway that already committed v2 to NVS (e.g. lost PKT_PAIR_ACK),
            # a PING elicits a v2 response so Python can self-heal via K_own without reflashing.
            if device_manager.get_gateway_by_wifi_mac(norm) or device_manager.get_gateway_by_radio_mac(norm):
                try:
                    self._send_packet_to_gateway(endpoint, self.encoder.encode_ping(norm, msg_id=0))
                except Exception:
                    pass
        return {
            'success': True,
            'mac': norm,
            'sent_now': sent_now,
            'pending_v2_upgrade': True,
        }

    def request_create_pin(self, mac: str) -> Dict:
        """Generate a 6-digit PIN and send it over the encrypted v2 channel (write-once)."""
        norm = MACFormatter.format_with_colons(mac)
        if device_manager.get_gateway_by_wifi_mac(norm) or device_manager.get_gateway_by_radio_mac(norm):
            return {'success': False, 'error': 'Gateways use TOFU Security v2 and do not use pairing PINs'}
        if not security_store.is_v2_device(norm):
            return {'success': False, 'error': 'Device must be upgraded to Security v2 first'}
        existing_pin = security_store.get_device_pin(norm)
        if existing_pin:
            return {
                'success': True,
                'mac': norm,
                'already_set': True,
                'pin': existing_pin,
                'pending_pin': False,
            }

        pin = security_store.get_pending_pin(norm) or f"{secrets.randbelow(1_000_000):06d}"
        security_store.set_pending_pin(norm, pin)
        endpoint = self._resolve_gateway_endpoint_for_mac(norm)
        sent_now = False
        if endpoint:
            sent_now = self._send_v2_set_pin(norm, pin, endpoint)
            if sent_now:
                logger.info(f"🔐 Sent encrypted CMD_SET_PIN to {norm} via {endpoint}")
        return {
            'success': True,
            'mac': norm,
            'already_set': False,
            'sent_now': sent_now,
            'pending_pin': True,
        }

    def cancel_pending_security_action(self, mac: str) -> Dict:
        """Cancel any pending v2 upgrade or pending PIN provisioning for a device."""
        norm = MACFormatter.format_with_colons(mac)
        security_store.set_pending_v2_upgrade(norm, False)
        security_store.set_pending_pin(norm, None)
        return {'success': True, 'mac': norm}

    def revoke_device(self, mac: str, keep_pin: bool = True, last_name: Optional[str] = None) -> Dict:
        """Revoke active v2 trust for a deleted device and prune K_ack from all gateways."""
        norm = MACFormatter.format_with_colons(mac)
        pairing_v2_manager.cancel_pending(norm)
        security_store.revoke_device(norm, keep_pin=keep_pin, last_name=last_name)
        try:
            self._push_key_distrib_to_gateways(
                target_mac=norm,
                crypto_mode=CRYPTO_MODE_V1,
                max_frame=V2_MAX_FRAME_ESP8266,
                k_ack=b"\x00" * 16,
                action=KEY_DISTRIB_REMOVE,
            )
        except Exception as e:
            logger.warning(f"Failed to push KEY_DISTRIB_REMOVE for {norm}: {e}")
        logger.info(f"🔒 Revoked Security v2 access for {norm} (keep_pin={keep_pin})")
        return {'success': True, 'mac': norm, 'keep_pin': keep_pin}

    def auto_upgrade_v2_gateways(self) -> int:
        """Automatically trigger in-place v2 upgrade for any 4.x.x gateway node still on v1."""
        sec_flags = config_manager.get_security_flags()
        if not sec_flags.get('security_v2_pair'):
            return 0

        def _is_v4(ver: Optional[str]) -> bool:
            try:
                major = int(str(ver or '0').split('.')[0])
                return major == 4
            except (ValueError, IndexError):
                return False

        triggered = 0
        for gw in device_manager.get_all_gateways():
            endpoint = gw.get('transport_endpoint') or gw.get('ip_address')
            if not endpoint:
                continue
            wifi_mac = gw.get('mac_address')
            radio_mac = gw.get('radio_mac')
            if wifi_mac and _is_v4(gw.get('version_net')) and not security_store.is_v2_device(wifi_mac):
                pkt = self.encoder.encode_sys_cmd(wifi_mac, CMD_UPGRADE_TO_V2, 0)
                if self._send_packet_to_gateway(endpoint, pkt):
                    triggered += 1
                    logger.info(f"🌐 Auto-triggered v2 upgrade for gateway net node {wifi_mac}")
            if radio_mac and _is_v4(gw.get('version_radio')) and not security_store.is_v2_device(radio_mac):
                pkt = self.encoder.encode_sys_cmd(radio_mac, CMD_UPGRADE_TO_V2, 0)
                if self._send_packet_to_gateway(endpoint, pkt):
                    triggered += 1
                    logger.info(f"🌐 Auto-triggered v2 upgrade for gateway radio node {radio_mac}")
        return triggered
    
    def _send_with_fallback(self, target_mac: str, packet: bytes, wait_for_delivery: bool = True, msg_id: Optional[int] = None, gateway_preference: Optional[str] = None) -> bool:
        """Send packet to device with automatic gateway failover.
        
        Centralized function for all message sending with smart routing:
        - Gateway targets: Send directly to gateway IP
        - Device targets: Try last successful gateway first, fallback to others on failure/timeout
        
        Args:
            target_mac: Target device MAC (gateway WiFi MAC or light/button MAC)
            packet: Encoded packet to send
            wait_for_delivery: Whether to wait for delivery confirmation with timeout
            msg_id: Message ID for delivery tracking (required if wait_for_delivery=True)
            
        Returns:
            True if packet sent successfully, False if all routes failed
        """
        # Check if target is a gateway (WiFi MAC)
        is_gateway = False
        gateway_ip = None
        
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['wifi_mac'] == target_mac or radio_mac == target_mac:
                    gateway_ip = info['ip_address']
                    is_gateway = True
                    break
        
        # If target is a gateway, send directly
        if is_gateway:
            try:
                if not gateway_ip:
                    logger.error(f"Gateway endpoint missing for {target_mac}")
                    return False
                if self._send_packet_to_gateway(gateway_ip, packet):
                    logger.debug(f"Sent packet to gateway {target_mac} at {gateway_ip}")
                    return True
                return False
            except Exception as e:
                logger.error(f"Failed to send to gateway {target_mac}: {e}")
                return False
        
        # Target is a device - use smart routing with fallback.
        # Prefer the last successful/seen gateway based on device type.
        gateway_mac = gateway_preference

        # Lightstrip routing preference
        if not gateway_mac:
            _, light_gateway_mac = device_manager.get_light_gateway(target_mac)
            if light_gateway_mac:
                gateway_mac = light_gateway_mac

        # Button/remote routing preference
        if not gateway_mac:
            button = device_manager.get_button_by_mac(target_mac)
            if button:
                gateway_mac = button.get('last_seen_gateway')

        # Motion sensor routing preference
        if not gateway_mac:
            sensor = device_manager.get_motion_sensor_by_mac(target_mac)
            if sensor:
                gateway_mac = sensor.get('last_seen_gateway')

        # Door sensor routing preference
        if not gateway_mac:
            sensor = device_manager.get_door_sensor_by_mac(target_mac)
            if sensor:
                gateway_mac = sensor.get('last_seen_gateway')
        
        # Get all available gateways for fallback
        with self._gateway_lock:
            available_gateways = list(self._gateway_table.items())
        
        if not available_gateways:
            logger.warning(f"No gateways available for device {target_mac}")
            return False
        
        # Build list of gateways to try: last successful first, then others
        gateways_to_try = []
        
        if gateway_mac:
            # Try last successful gateway first
            for radio_mac, info in available_gateways:
                if radio_mac.upper() == gateway_mac.upper():
                    gateways_to_try.append((radio_mac, info['ip_address']))
                    break
        
        # Add other gateways as fallback
        for radio_mac, info in available_gateways:
            if not gateway_mac or radio_mac.upper() != gateway_mac.upper():
                gateways_to_try.append((radio_mac, info['ip_address']))
        
        # Try each gateway
        for attempt, (gw_radio_mac, gw_ip) in enumerate(gateways_to_try[:MAX_GATEWAY_ATTEMPTS]):
            try:
                if wait_for_delivery:
                    if msg_id is None:
                        logger.error("msg_id required when wait_for_delivery=True")
                        return False
                    
                    # Create delivery tracking event
                    event = threading.Event()
                    delivery_result = {'success': False, 'timeout': False}
                    
                    with self._delivery_lock:
                        self._pending_deliveries[msg_id] = {
                            'event': event,
                            'result': delivery_result,
                            'target_mac': target_mac,
                            'gateway_radio_mac': gw_radio_mac,
                            'timestamp': time.time()
                        }
                    
                    # Send packet
                    if self._send_packet_to_gateway(gw_ip, packet):
                        logger.info(f"Sent to {target_mac} via {gw_ip} (gateway: {gw_radio_mac}, msgID: {msg_id}, attempt: {attempt+1})")
                    else:
                        logger.warning(f"Failed to send to {target_mac} via gateway endpoint {gw_ip}")
                        with self._delivery_lock:
                            if msg_id in self._pending_deliveries:
                                del self._pending_deliveries[msg_id]
                        continue
                    
                    # Wait for delivery report with timeout
                    if event.wait(timeout=GATEWAY_DELIVERY_TIMEOUT_SECONDS):
                        # Got delivery report
                        if delivery_result['success']:
                            # Success - update device manager
                            device_manager.update_light_gateway(target_mac, gw_radio_mac)
                            logger.info(f"✅ Device {target_mac} delivered via {gw_radio_mac}")
                            
                            # Clean up
                            with self._delivery_lock:
                                if msg_id in self._pending_deliveries:
                                    del self._pending_deliveries[msg_id]
                            
                            return True
                        else:
                            # Explicit failure - try next gateway
                            logger.warning(f"❌ Device {target_mac} failed via {gw_radio_mac}, trying next gateway")
                    else:
                        # Timeout - gateway might be off, try next
                        delivery_result['timeout'] = True
                        logger.warning(f"⏱️  Device {target_mac} timeout via {gw_radio_mac}, trying next gateway")
                    
                    # Clean up failed attempt
                    with self._delivery_lock:
                        if msg_id in self._pending_deliveries:
                            del self._pending_deliveries[msg_id]
                else:
                    # No delivery tracking - just send and return on first success
                    if self._send_packet_to_gateway(gw_ip, packet):
                        logger.debug(f"Sent to {target_mac} via {gw_ip} (gateway: {gw_radio_mac}, no delivery tracking)")
                        return True
                    logger.warning(f"Failed to send to {target_mac} via gateway endpoint {gw_ip}")
                    
            except Exception as e:
                logger.error(f"Failed to send to {target_mac} via {gw_ip}: {e}")
                
                # Clean up on exception
                if wait_for_delivery and msg_id is not None:
                    with self._delivery_lock:
                        if msg_id in self._pending_deliveries:
                            del self._pending_deliveries[msg_id]
        
        logger.error(f"❌ All gateways failed for device {target_mac}")
        return False

    def _broadcast_gateway_list(self, only_gateways: bool = True):
        """Broadcast gateway list to all gateways and paired lights if not only_gateways."""
        with self._gateway_lock:
            # Get all gateway radio MACs
            all_gateway_macs = list(self._gateway_table.keys())
            
            if not all_gateway_macs:
                return
            
            # Send to all gateways (dual-stack: v2 encrypted per radio node, or v1 broadcast)
            v1_packet = self.encoder.encode_gateway_list(all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
            for radio_mac, info in self._gateway_table.items():
                gateway_ip = info['ip_address']
                if self._use_v2_for_peer(radio_mac):
                    gw_packet = self._encode_gateway_list_dual(radio_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
                    self._send_packet_to_gateway(gateway_ip, gw_packet)
                else:
                    self._send_packet_to_gateway(gateway_ip, v1_packet)
            
            logger.info(f"Broadcasted gateway list to gateways: {len(all_gateway_macs)} gateways")
            
            if not only_gateways:
                # Also send to all paired lights via their routing gateways
                lights = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
                for light in lights:
                    light_mac = light.get('mac_address')
                    if light_mac:
                        # Generate message ID for delivery tracking
                        with self._delivery_lock:
                            msg_id = self._next_msg_id
                            self._next_msg_id = (self._next_msg_id + 1) % 256
                        
                        # Encode gateway list packet for this light with msg_id (dual-stack v1/v2)
                        light_packet = self._encode_gateway_list_dual(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET], msg_id)
                        
                        # Send in background thread to avoid blocking (each light update happens in parallel)
                        threading.Thread(
                            target=self._send_with_fallback,
                            args=(light_mac, light_packet, True, msg_id),
                            daemon=True,
                            name=f"GW-List-{light_mac[-8:]}"
                        ).start()
    
    def send_to_light(self, light_mac: str, rgb_data: List[Tuple[int, int, int]], 
                     brightness: int = 255) -> bool:
        """Send RGB data to lightstrip with automatic gateway routing and failover.
        
        Args:
            light_mac: Light MAC address
            rgb_data: List of (r, g, b) tuples
            brightness: Global brightness 0-255
            
        Returns:
            True if packet delivered successfully, False if all routes failed
        """
        # Generate message ID
        with self._delivery_lock:
            msg_id = self._next_msg_id
            self._next_msg_id = (self._next_msg_id + 1) % 256
        
        # Encode packet (v2 AES-128-GCM if enabled & peer is v2-paired, else v1 frozen 203 B)
        sec_flags = config_manager.get_security_flags()
        if sec_flags['security_v2_send'] and security_store.is_v2_device(light_mac):
            packet = PacketEncoderV2.encode_light_raw_v2(
                target_mac=light_mac,
                rgb_data=rgb_data,
                brightness=brightness,
                counter=security_store.next_backend_tx_counter(),
                key_epoch=security_store.epoch,
                key=security_store.derive_k_own(light_mac),
                max_frame=security_store.get_max_frame(light_mac),
                msg_id=msg_id,
            )
        else:
            packet = self.encoder.encode_light_raw(light_mac, rgb_data, brightness, msg_id)
        
        # Send with fallback and delivery tracking
        success = self._send_with_fallback(light_mac, packet, wait_for_delivery=True, msg_id=msg_id)
        
        if success:
            logger.info(f"✅ Light {light_mac} updated successfully")
        
        return success
    
    def get_gateway_table(self) -> Dict[str, Dict]:
        """Get current gateway routing table.
        
        Returns:
            Copy of gateway table
        """
        with self._gateway_lock:
            return dict(self._gateway_table)
    
    def trigger_gateway_list_broadcast(self):
        """Manually trigger gateway list broadcast."""
        self._broadcast_gateway_list(only_gateways=False)
    
    def send_ping(self, gateway_mac: str, timeout: float = 3.0) -> Optional[int]:
        """Send ping to gateway and wait for response.
        
        Args:
            gateway_mac: Gateway WiFi MAC address
            timeout: Timeout in seconds to wait for response
            
        Returns:
            Gateway uptime in seconds if response received, None if timeout/error
        """
        # Find gateway IP by WiFi MAC
        gateway_ip = None
        
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['wifi_mac'] == gateway_mac:
                    gateway_ip = info['ip_address']
                    break
        
        if not gateway_ip:
            logger.warning(f"Gateway {gateway_mac} not found for ping")
            return None
        
        # Create event for waiting
        event = threading.Event()
        
        with self._ping_lock:
            self._pending_pings[gateway_mac] = {
                'event': event,
                'uptime': None,
                'received': False,
                'timestamp': time.time()
            }
        
        try:
            # Encode and send ping packet (dual-stack v1/v2)
            packet = self._encode_ping_dual(gateway_mac)
            if self._send_packet_to_gateway(gateway_ip, packet):
                logger.info(f"📡 Ping sent to gateway {gateway_mac} ({gateway_ip})")
            
            # Wait for response
            if event.wait(timeout):
                # Response received
                with self._ping_lock:
                    if gateway_mac in self._pending_pings:
                        uptime = self._pending_pings[gateway_mac]['uptime']
                        del self._pending_pings[gateway_mac]
                        return uptime
            else:
                # Timeout
                logger.warning(f"⏱️  Ping timeout for gateway {gateway_mac}")
                with self._ping_lock:
                    if gateway_mac in self._pending_pings:
                        del self._pending_pings[gateway_mac]
                return None
                
        except Exception as e:
            logger.error(f"Failed to ping gateway {gateway_mac}: {e}")
            with self._ping_lock:
                if gateway_mac in self._pending_pings:
                    del self._pending_pings[gateway_mac]
            return None
    
    def send_system_command(self, target_mac: str, command: int, value: int = 0) -> bool:
        """Send system command to device with automatic gateway failover.
        
        Args:
            target_mac: Target device MAC (gateway WiFi MAC or light MAC)
            command: Command code (1=night mode on, 2=night mode off)
            value: Optional command value
            
        Returns:
            True if packet sent successfully, False if all routes failed
        """
        cmd_name = {
            CMD_NIGHT_MODE_ON: "Night Mode ON",
            CMD_NIGHT_MODE_OFF: "Night Mode OFF",
            CMD_NETNODE_WIFI_STATUS: "NetNode WiFi Status",
        }.get(command, f"Command {command}")
        
        # Check if target is a gateway (send directly) or device (use routing)
        is_gateway = False
        gateway_radio_mac = None
        gateway_ip = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['wifi_mac'] == target_mac:
                    is_gateway = True
                    gateway_radio_mac = radio_mac
                    gateway_ip = info['ip_address']
                    break
        
        # Generate message ID (always needed for packet encoding)
        with self._delivery_lock:
            msg_id = self._next_msg_id
            self._next_msg_id = (self._next_msg_id + 1) % 256
        
        # Encode packet (dual-stack v1/v2)
        packet = self._encode_sys_cmd_dual(target_mac, command, value, msg_id)
        
        # Send with fallback (delivery tracking for devices, direct send for gateways)
        success = self._send_with_fallback(target_mac, packet, wait_for_delivery=(not is_gateway), msg_id=msg_id)

        # For v2 WiFi gateways, also send night mode command encrypted to the radio node
        if is_gateway and gateway_radio_mac and gateway_ip and command in (CMD_NIGHT_MODE_ON, CMD_NIGHT_MODE_OFF):
            if self._use_v2_for_peer(gateway_radio_mac):
                radio_pkt = self._encode_sys_cmd_dual(gateway_radio_mac, command, value, msg_id)
                self._send_packet_to_gateway(gateway_ip, radio_pkt)
        
        if success:
            logger.info(f"⚙️  System command sent to {target_mac}: {cmd_name}")
        
        return success
    
    def set_night_mode(self, gateway_mac: str, enabled: bool) -> bool:
        """Enable or disable night mode on gateway.
        
        Args:
            gateway_mac: Gateway WiFi MAC address
            enabled: True to enable night mode, False to disable
            
        Returns:
            True if command sent successfully
        """
        command = 1 if enabled else 2
        return self.send_system_command(gateway_mac, command)
    
    def set_gateway_leds(self, gateway_mac: str, enabled: bool) -> bool:
        """Enable or disable LEDs on a specific gateway.
        
        Args:
            gateway_mac: Gateway WiFi MAC address
            enabled: True to enable LEDs, False to disable
            
        Returns:
            True if command sent successfully
        """
        # Command 1 = LED OFF (night mode ON), Command 2 = LED ON (night mode OFF)
        command = 2 if enabled else 1
        return self.send_system_command(gateway_mac, command)
    
    def set_all_gateway_leds(self, enabled: bool) -> int:
        """Enable or disable LEDs on all gateways.
        
        Args:
            enabled: True to enable LEDs, False to disable
            
        Returns:
            Number of gateways that received the command
        """
        with self._led_state_lock:
            self._gateway_leds_enabled = enabled
        
        logger.info(f"Setting all gateway LEDs to {'ON' if enabled else 'OFF'}")
        
        success_count = 0
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                wifi_mac = info['wifi_mac']
                if self.set_gateway_leds(wifi_mac, enabled):
                    success_count += 1
        
        return success_count
    
    def get_gateway_leds_state(self) -> bool:
        """Get current global gateway LED state.
        
        Returns:
            True if LEDs are enabled, False if disabled
        """
        with self._led_state_lock:
            return self._gateway_leds_enabled
    
    def ping_device(self, device_mac: str, timeout: float = 3.0) -> Optional[Dict[str, int]]:
        """Send ping to device via all gateways and collect RSSI values.
        
        Args:
            device_mac: Device MAC address (lightstrip, button, etc.)
            timeout: Timeout in seconds to wait for responses
            
        Returns:
            Dictionary of {gateway_radio_mac: rssi_dbm} if responses received, None if error
        """
        # Get all available gateways
        with self._gateway_lock:
            gateway_list = list(self._gateway_table.items())
        
        if not gateway_list:
            logger.warning("No gateways available for device ping")
            return None
        
        # Prepare ping tracking
        event = threading.Event()
        rssi_map = {}
        
        with self._device_ping_lock:
            self._pending_device_pings[device_mac] = {
                'event': event,
                'rssi_map': rssi_map,
                'received': False,
                'timestamp': time.time(),
                'expected_count': len(gateway_list)
            }
        
        logger.info(f"🏓 Pinging device {device_mac} via {len(gateway_list)} gateways...")
        
        # Send ping via each gateway
        sent_count = 0
        for radio_mac, info in gateway_list:
            gateway_ip = info['ip_address']
            
            # Encode ping device packet (dual-stack v1/v2)
            packet = self._encode_ping_device_dual(device_mac)
            
            try:
                if self._send_packet_to_gateway(gateway_ip, packet):
                    logger.debug(f"  → Sent ping to {device_mac} via {gateway_ip} (radio: {radio_mac})")
                    sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send ping via {gateway_ip}: {e}")
        
        if sent_count == 0:
            logger.error("Failed to send ping to any gateway")
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    del self._pending_device_pings[device_mac]
            return None
        
        # Wait for responses (or timeout)
        try:
            # Wait up to timeout for responses
            event.wait(timeout)
            
            # Collect results
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    result_map = self._pending_device_pings[device_mac]['rssi_map'].copy()
                    del self._pending_device_pings[device_mac]
                    
                    if result_map:
                        logger.info(f"🏓 Device ping results for {device_mac}: {len(result_map)} responses")
                        for gw_mac, rssi in result_map.items():
                            logger.info(f"  ✓ Gateway {gw_mac}: {rssi} dBm")
                        return result_map
                    else:
                        logger.warning(f"⏱️  No ping responses received for {device_mac}")
                        return {}
        except Exception as e:
            logger.error(f"Failed to ping device {device_mac}: {e}")
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    del self._pending_device_pings[device_mac]
            return None
    
    def ping_device_single_gateway(self, device_mac: str, gateway_radio_mac: str, timeout: float = 2.0) -> Optional[Dict[str, int]]:
        """Send ping to device via a specific gateway and get RSSI.
        
        Args:
            device_mac: Device MAC address (lightstrip, button, etc.)
            gateway_radio_mac: Radio MAC address of the specific gateway to use
            timeout: Timeout in seconds to wait for response
            
        Returns:
            Dictionary of {gateway_radio_mac: rssi_dbm} if response received, None if error
        """
        # Find the gateway in the table
        with self._gateway_lock:
            if gateway_radio_mac not in self._gateway_table:
                logger.warning(f"Gateway {gateway_radio_mac} not found in gateway table")
                return {}
            
            gateway_info = self._gateway_table[gateway_radio_mac]
            gateway_ip = gateway_info['ip_address']
        
        # Prepare ping tracking
        event = threading.Event()
        rssi_map = {}
        
        with self._device_ping_lock:
            self._pending_device_pings[device_mac] = {
                'event': event,
                'rssi_map': rssi_map,
                'received': False,
                'timestamp': time.time(),
                'expected_count': 1
            }
        
        logger.debug(f"🏓 Pinging device {device_mac} via gateway {gateway_radio_mac} ({gateway_ip})...")
        
        # Send ping via the specific gateway (dual-stack v1/v2)
        packet = self._encode_ping_device_dual(device_mac)
        
        try:
            if not self._send_packet_to_gateway(gateway_ip, packet):
                logger.error(f"Failed to send ping via gateway endpoint {gateway_ip}")
            logger.debug(f"  → Sent ping to {device_mac} via {gateway_ip}")
        except Exception as e:
            logger.error(f"Failed to send ping via {gateway_ip}: {e}")
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    del self._pending_device_pings[device_mac]
            return None
        
        # Wait for response (or timeout)
        try:
            event.wait(timeout)
            
            # Collect results
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    result_map = self._pending_device_pings[device_mac]['rssi_map'].copy()
                    del self._pending_device_pings[device_mac]
                    
                    if result_map:
                        logger.debug(f"🏓 Single gateway ping result for {device_mac}: {result_map}")
                        return result_map
                    else:
                        logger.debug(f"⏱️  No ping response from {gateway_radio_mac} for {device_mac}")
                        return {}
        except Exception as e:
            logger.error(f"Failed to ping device {device_mac} via single gateway: {e}")
            with self._device_ping_lock:
                if device_mac in self._pending_device_pings:
                    del self._pending_device_pings[device_mac]
            return None
    
    def _handle_ping_device_response(self, src_mac: str, payload: bytes, sender_ip: str, rssi_v2: Optional[int] = None):
        """Handle ping device response from gateway.
        
        Args:
            src_mac: Device MAC address (source of response)
            payload: Raw payload with RSSI in first byte (v1)
            sender_ip: Gateway IP
            rssi_v2: Parsed RSSI from v2 header byte 21 (when proto_ver == 2)
        """
        try:
            if rssi_v2 is not None:
                rssi_dbm = rssi_v2
            else:
                # RSSI is stored in payload[0] by gateway radio node (v1)
                rssi_byte = payload[0] if len(payload) > 0 else 0
                rssi_dbm = MACFormatter.parse_rssi(rssi_byte)
            
            # Find gateway radio MAC by sender endpoint
            gateway_radio_mac = self._resolve_gateway_radio_mac_by_sender(sender_ip)
            
            if not gateway_radio_mac:
                logger.warning(f"Received ping device response from unknown gateway {sender_ip}")
                return
            
            logger.debug(f"🏓 Ping device response: {src_mac} via {gateway_radio_mac} (RSSI: {rssi_dbm} dBm)")
            
            # Store RSSI in pending ping map
            with self._device_ping_lock:
                if src_mac in self._pending_device_pings:
                    self._pending_device_pings[src_mac]['rssi_map'][gateway_radio_mac] = rssi_dbm
                    self._pending_device_pings[src_mac]['received'] = True
                    
                    # Check if we've received all expected responses
                    expected = self._pending_device_pings[src_mac]['expected_count']
                    received = len(self._pending_device_pings[src_mac]['rssi_map'])
                    
                    if received >= expected:
                        # All responses received, trigger event early
                        self._pending_device_pings[src_mac]['event'].set()
                        
        except Exception as e:
            logger.error(f"Failed to parse ping device response: {e}")
    
    def start_ota_update(self, device_mac: str, device_type: int, firmware_path: str,
                         gateway_preference: Optional[str] = None,
                         depends_on_mac: Optional[str] = None) -> bool:
        """Queue (and immediately start if the active OTA slot is free) an OTA update.
        
        Multiple devices may be queued concurrently; only one device transfers at a time.
        v2 battery devices stay queued until their next wake (and only claim the active
        slot when no other transfer is running and battery >= OTA_MIN_BATTERY_PERCENT).
        
        Args:
            device_mac: Target device MAC address
            device_type: Device type constant
            firmware_path: Path to firmware binary file
            gateway_preference: Preferred gateway MAC (e.g., plugin device's last_seen_gateway)
            depends_on_mac: Optional dependency MAC that must complete OTA first
            
        Returns:
            True if OTA was queued/initiated successfully, False otherwise
        """
        is_v2_battery = (
            self._use_v2_for_peer(device_mac)
            and device_type in (DEV_BUTTON, DEV_REMOTE, DEV_MOTION, DEV_DOOR)
        )

        # Create queued OTA session
        session = ota_manager.create_session(
            device_mac,
            device_type,
            firmware_path,
            wake_triggered=is_v2_battery,
            preferred_gateway_mac=gateway_preference,
            depends_on_mac=depends_on_mac,
        )
        if not session:
            logger.error(f"Failed to create OTA session for {device_mac}")
            return False

        if is_v2_battery:
            logger.info(
                f"⏳ Queued v2 auto-OTA for battery device {device_mac} "
                f"(will start on next wake when slot is free and battery >= {OTA_MIN_BATTERY_PERCENT}%)"
            )
            return True

        # Try to start immediately if slot is free and dependency (if any) is satisfied
        self._process_ota_queue(delay=0.0)
        return True

    def _schedule_ota_queue_processing(self):
        """Schedule background advancement of the OTA queue after an active slot frees up."""
        if not self.running:
            return
        threading.Thread(
            target=self._process_ota_queue,
            args=(1.0,),
            daemon=True,
            name="OTA-Queue-Advance",
        ).start()

    def _process_ota_queue(self, delay: float = 0.0):
        """Start the next eligible queued always-on OTA session if the active slot is free."""
        if delay > 0:
            time.sleep(delay)

        with self._ota_queue_lock:
            while True:
                next_session = ota_manager.get_next_queued_session()
                if not next_session:
                    return
                if not ota_manager.claim_active_session(next_session.device_mac):
                    return
                if self._activate_ota_session(next_session):
                    return
                # If activation failed immediately, slot was released; loop to try next queued session

    def _activate_ota_session(self, session) -> bool:
        """Send PKT_OTA_NOTIFY and move a claimed session into WAITING_READY."""
        device_mac = session.device_mac
        gateway_preference = session.preferred_gateway_mac
        try:
            # Generate message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Send OTA_NOTIFY packet (dual-stack v1/v2)
            packet = self._encode_ota_notify_dual(
                device_mac,
                session.firmware_size,
                session.sha256_hash,
                session.version,
                msg_id
            )
            
            # Determine target gateway
            is_gateway = False
            gateway_ip = None
            
            with self._gateway_lock:
                for radio_mac, info in self._gateway_table.items():
                    if info['wifi_mac'] == device_mac or radio_mac == device_mac:
                        gateway_ip = info['ip_address']
                        is_gateway = True
                        break
            
            # Send packet
            if is_gateway:
                # Direct send to gateway
                if not gateway_ip:
                    ota_manager.update_session_state(device_mac, OTAState.FAILED, "Gateway endpoint not found")
                    return False
                if self._send_packet_to_gateway(gateway_ip, packet):
                    logger.info(f"📡 OTA_NOTIFY sent to gateway {device_mac} at {gateway_ip}")
                else:
                    ota_manager.update_session_state(device_mac, OTAState.FAILED, "Failed to send OTA_NOTIFY to gateway")
                    return False
            else:
                # Send via gateway mesh with last-known gateway preference (no delivery tracking)
                success = self._send_with_fallback(device_mac, packet, wait_for_delivery=False, gateway_preference=gateway_preference)
                if not success:
                    ota_manager.update_session_state(device_mac, OTAState.FAILED, "Failed to send OTA_NOTIFY")
                    return False
                logger.info(f"📡 OTA_NOTIFY sent to device {device_mac}")
            
            # Update session state
            ota_manager.update_session_state(device_mac, OTAState.WAITING_READY)
            
            threading.Thread(
                target=self._ota_ready_timeout,
                args=(device_mac, session.ready_timeout_token),
                daemon=True,
                name=f"OTA-Ready-Timeout-{device_mac[-8:]}"
            ).start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start OTA for {device_mac}: {e}")
            ota_manager.update_session_state(device_mac, OTAState.FAILED, str(e))
            return False
    
    def _ota_ready_timeout(self, device_mac: str, token: int = 0):
        """Timeout thread waiting for PKT_OTA_READY response.
        
        Args:
            device_mac: Device MAC address
            token: ready_timeout_token captured when WAITING_READY began
        """
        time.sleep(OTA_READY_TIMEOUT)
        
        session = ota_manager.get_session(device_mac)
        if session and session.state == OTAState.WAITING_READY and session.ready_timeout_token == token:
            if session.wake_triggered:
                logger.info(f"⏱️  No OTA_READY from v2 battery device {device_mac} on this wake; re-queuing for next wake")
                ota_manager.requeue_session(device_mac, "No OTA_READY on wake; re-queued for next wake")
            else:
                logger.warning(f"⏱️  OTA ready timeout for {device_mac}")
                ota_manager.update_session_state(device_mac, OTAState.FAILED, "Timeout waiting for OTA_READY")
    
    def _handle_ota_ready(self, src_mac: str, payload: bytes, sender_ip: str):
        """Handle OTA_READY response from device.
        
        Args:
            src_mac: Source device MAC
            payload: Raw payload
            sender_ip: Sender IP
        """
        ready_data = self.decoder.parse_ota_ready(payload)
        if not ready_data:
            logger.warning(f"Invalid OTA_READY from {src_mac}")
            return
        
        battery_mv = ready_data.get('battery_mv', 0)
        firmware_size_in_ready = ready_data.get('firmware_size', 0)
        
        logger.info(f"📡 OTA_READY received from {src_mac} (battery: {battery_mv}mV, fw_size: {firmware_size_in_ready})")
        
        # Check if session exists
        session = ota_manager.get_session(src_mac)
        if not session:
            logger.warning(f"No OTA session for {src_mac}")
            return
        
        # Store battery voltage
        ota_manager.set_battery_voltage(src_mac, battery_mv)
        
        # If firmware_size is 0, this is an unsolicited PKT_OTA_READY from device waking up
        # The device is in OTA_WAITING_NOTIFY state and needs PKT_OTA_NOTIFY to transition
        if firmware_size_in_ready == 0 and session.state in (OTAState.QUEUED, OTAState.WAITING_READY):
            just_claimed = False
            if session.state == OTAState.QUEUED:
                if not ota_manager.claim_active_session(src_mac):
                    logger.info(
                        f"⏳ Ignoring unsolicited OTA_READY from queued {src_mac}: another OTA is active ({ota_manager.active_session_mac})"
                    )
                    return
                ota_manager.update_session_state(src_mac, OTAState.WAITING_READY)
                just_claimed = True

            logger.info(f"📡 Device {src_mac} woke up in OTA mode - re-sending OTA_NOTIFY")
            
            # Small delay to allow ESP-NOW peer connection to stabilize after device wakeup
            time.sleep(0.2)
            
            # Generate new message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Re-send OTA_NOTIFY packet (dual-stack v1/v2)
            packet = self._encode_ota_notify_dual(
                src_mac,
                session.firmware_size,
                session.sha256_hash,
                session.version,
                msg_id
            )
            
            # Send via gateway mesh
            success = self._send_with_fallback(src_mac, packet, wait_for_delivery=False)
            if success:
                logger.info(f"📡 OTA_NOTIFY re-sent to {src_mac}")
                if just_claimed:
                    threading.Thread(
                        target=self._ota_ready_timeout,
                        args=(src_mac, session.ready_timeout_token),
                        daemon=True,
                        name=f"OTA-Ready-Timeout-{src_mac[-8:]}"
                    ).start()
            else:
                logger.error(f"Failed to re-send OTA_NOTIFY to {src_mac}")
                if just_claimed:
                    if session.wake_triggered:
                        ota_manager.requeue_session(src_mac, "Failed to send OTA_NOTIFY")
                    else:
                        ota_manager.update_session_state(src_mac, OTAState.FAILED, "Failed to send OTA_NOTIFY")
            return
        
        # Normal PKT_OTA_READY response (firmware_size matches session)
        # Atomically claim TRANSFERRING under session.lock so duplicate OTA_READY
        # packets (e.g. forwarded by multiple gateways) cannot spawn two transfer threads.
        with session.lock:
            if session.state != OTAState.WAITING_READY:
                logger.warning(f"Unexpected OTA_READY from {src_mac} in state {session.state}")
                return
            session.state = OTAState.TRANSFERRING
            if not session.transfer_start_time:
                session.transfer_start_time = datetime.now()
        
        # Start chunk transfer in background thread
        threading.Thread(
            target=self._send_ota_chunks,
            args=(src_mac, sender_ip),
            daemon=True,
            name=f"OTA-Transfer-{src_mac[-8:]}"
        ).start()
    
    def _handle_ota_chunk_ack(self, src_mac: str, payload: bytes):
        """Handle OTA_CHUNK_ACK (checkpoint) from device.
        
        Args:
            src_mac: Device MAC address
            payload: Raw payload bytes
        """
        ack_data = self.decoder.parse_ota_chunk_ack(payload)
        if not ack_data:
            logger.warning(f"Invalid OTA_CHUNK_ACK from {src_mac}")
            return
        
        last_chunk_index = ack_data['chunk_index']
        
        logger.debug(f"📦 OTA checkpoint ACK from {src_mac}: last_chunk={last_chunk_index}")
        
        # Signal the waiting transfer thread
        with self._ota_ack_lock:
            if src_mac in self._ota_ack_events:
                self._ota_ack_events[src_mac]['last_chunk_index'] = last_chunk_index
                self._ota_ack_events[src_mac]['event'].set()
    
    def _handle_ota_abort_from_device(self, src_mac: str, payload: bytes):
        """Handle OTA_ABORT notification from device (device-initiated abort).
        
        Args:
            src_mac: Device MAC address
            payload: Raw payload bytes (reason_code)
        """
        if len(payload) < 1:
            logger.warning(f"Invalid OTA_ABORT from {src_mac}: missing reason_code")
            return
        
        reason_code = payload[0]
        logger.error(f"🚫 Device {src_mac} aborted OTA update (reason_code={reason_code})")
        
        # Update OTA session to ABORTED state
        ota_manager.update_session_state(device_mac=src_mac, new_state=OTAState.ABORTED, failure_reason=f"Device abort (code={reason_code})")
    
    def _send_ota_chunks(self, device_mac: str, preferred_gateway_ip: Optional[str] = None):
        """Send firmware chunks to device.
        
        Args:
            device_mac: Target device MAC
            preferred_gateway_ip: Gateway IP that delivered OTA_READY (best path)
        """
        session = ota_manager.get_session(device_mac)
        if not session:
            return
        
        try:
            # Update state
            ota_manager.update_session_state(device_mac, OTAState.TRANSFERRING)
            
            logger.info(f"📦 Reading firmware file for {device_mac}...")
            
            # Read firmware file
            with open(session.firmware_path, 'rb') as f:
                firmware_data = f.read()
            
            # Calculate chunks (adaptive v2 chunk size or frozen 182 B for v1)
            chunk_data_size = self._ota_chunk_size_for_peer(device_mac)
            total_chunks = (len(firmware_data) + chunk_data_size - 1) // chunk_data_size
            session.total_chunks = total_chunks
            
            # Lock to one gateway IP for entire transfer.
            # Prefer the gateway that delivered OTA_READY to avoid route mismatch.
            locked_gateway_ip = preferred_gateway_ip
            
            # Determine if target is gateway and if it's radio node (via UART passthrough)
            is_gateway = False
            is_radio_node = False
            with self._gateway_lock:
                for radio_mac, info in self._gateway_table.items():
                    if info['wifi_mac'] == device_mac:
                        # Updating net node directly
                        locked_gateway_ip = info['ip_address']
                        is_gateway = True
                        break
                    elif radio_mac == device_mac:
                        # Updating radio node via UART passthrough through net node
                        locked_gateway_ip = info['ip_address']
                        is_gateway = True
                        is_radio_node = True
                        break
            
            # Set chunk delay based on target type and chunk size:
            # Only large ESP-NOW v2 frames (>= 512 B, i.e. ESP32 1024 B chunks) need the longer delay;
            # ESP8266 v2 chunks are 208 B (ESP-NOW v1 250 B cap) and only need a tiny BearSSL margin.
            is_large_v2 = chunk_data_size >= 512
            is_small_v2 = (not is_large_v2) and (chunk_data_size > OTA_CHUNK_DATA_SIZE)
            if is_gateway and not is_radio_node:
                chunk_delay = 0.015 if is_large_v2 else 0.010  # Net node direct WiFi UDP
            elif is_radio_node:
                chunk_delay = 0.035 if is_large_v2 else 0.016  # 460800 baud UART (~23ms/1066B + flash write)
            else:
                chunk_delay = 0.045 if is_large_v2 else (0.018 if is_small_v2 else 0.016)  # UART + ESP-NOW + flash write
            
            if is_gateway and locked_gateway_ip and self._is_serial_endpoint(locked_gateway_ip):
                chunk_delay = 0.035 if is_large_v2 else 0.000  # 460800 baud (~23ms/1070B) + AES-GCM + 4KB flash writes
                       
            logger.info(f"📦 Starting OTA transfer to {device_mac}: {len(firmware_data)} bytes in {total_chunks} chunks (is_gateway={is_gateway}, is_radio_node={is_radio_node}, delay={chunk_delay}s)")
                   
            
            # Send chunks with checkpoint-based ACK
            chunk_idx = 0
            retry_count = 0
            stall_chunk_idx = -1
            stall_retries = 0
            
            while chunk_idx < total_chunks:
                # Check if session still active
                if session.state != OTAState.TRANSFERRING:
                    logger.warning(f"OTA transfer aborted for {device_mac}")
                    return
                
                # Prepare ACK tracking for this batch
                with self._ota_ack_lock:
                    self._ota_ack_events[device_mac] = {
                        'event': threading.Event(),
                        'last_chunk_index': -1
                    }
                
                # Send a batch of chunks (up to checkpoint interval)
                batch_start = chunk_idx
                batch_end = min(chunk_idx + OTA_CHECKPOINT_INTERVAL, total_chunks)
                
                for i in range(batch_start, batch_end):
                    # Extract chunk data
                    offset = i * chunk_data_size
                    chunk_data = firmware_data[offset:offset + chunk_data_size]
                    
                    # Generate message ID
                    with self._delivery_lock:
                        msg_id = self._next_msg_id
                        self._next_msg_id = (self._next_msg_id + 1) % 256
                    
                    # Encode chunk packet (dual-stack v1/v2)
                    packet = self._encode_ota_chunk_dual(device_mac, i, chunk_data, msg_id)
                    
                    # Send chunk
                    success = False
                    if is_gateway:
                        try:
                            if locked_gateway_ip and self._send_packet_to_gateway(locked_gateway_ip, packet):
                                success = True
                        except Exception as e:
                            logger.error(f"Failed to send chunk {i}: {e}")
                    else:
                        # Send via gateway mesh routing
                        if locked_gateway_ip is None:
                            gateway_mac = None

                            # Lightstrip preferred gateway
                            _, light_gateway_mac = device_manager.get_light_gateway(device_mac)
                            if light_gateway_mac:
                                gateway_mac = light_gateway_mac

                            # Button/remote preferred gateway
                            if not gateway_mac:
                                button = device_manager.get_button_by_mac(device_mac)
                                if button:
                                    gateway_mac = button.get('last_seen_gateway')

                            # Motion sensor preferred gateway
                            if not gateway_mac:
                                sensor = device_manager.get_motion_sensor_by_mac(device_mac)
                                if sensor:
                                    gateway_mac = sensor.get('last_seen_gateway')

                            # Door sensor preferred gateway
                            if not gateway_mac:
                                sensor = device_manager.get_door_sensor_by_mac(device_mac)
                                if sensor:
                                    gateway_mac = sensor.get('last_seen_gateway')

                            # Plugin device preferred gateway (stored in session)
                            if not gateway_mac:
                                gateway_mac = session.preferred_gateway_mac

                            if gateway_mac:
                                with self._gateway_lock:
                                    for radio_mac, info in self._gateway_table.items():
                                        if radio_mac.upper() == gateway_mac.upper():
                                            locked_gateway_ip = info['ip_address']
                                            break
                            if locked_gateway_ip is None:
                                with self._gateway_lock:
                                    if self._gateway_table:
                                        locked_gateway_ip = list(self._gateway_table.values())[0]['ip_address']
                        
                        if locked_gateway_ip:
                            try:
                                if self._send_packet_to_gateway(locked_gateway_ip, packet):
                                    success = True
                            except Exception as e:
                                logger.error(f"Failed to send chunk {i}: {e}")
                    
                    if not success:
                        ota_manager.update_session_state(device_mac, OTAState.FAILED, f"Failed to send chunk {i}")
                        return
                    
                    # Delay between chunks (longer for radio node due to UART passthrough)
                    time.sleep(chunk_delay)
                
                # Always send checkpoint request after each batch (we just sent batch_end - batch_start chunks)
                # Small delay to let ESP32 process chunks and clear buffers before checkpoint
                time.sleep(0.05)
                
                # Try checkpoint request up to 5 times before retrying the entire batch
                checkpoint_retry = 0
                ack_received = False
                
                while checkpoint_retry < 5 and not ack_received:
                    if session.state != OTAState.TRANSFERRING:
                        logger.warning(f"OTA transfer aborted for {device_mac}")
                        return
                    # Generate new message ID for checkpoint request
                    with self._delivery_lock:
                        checkpoint_msg_id = self._next_msg_id
                        self._next_msg_id = (self._next_msg_id + 1) % 256
                    
                    # Clear the event before sending checkpoint request (to wait for fresh ACK)
                    with self._ota_ack_lock:
                        event = self._ota_ack_events[device_mac]['event']
                        event.clear()  # Reset event to wait for new ACK
                    
                    # Send checkpoint request to device (dual-stack v1/v2)
                    checkpoint_packet = self._encode_ota_checkpoint_req_dual(device_mac, checkpoint_msg_id)
                    if is_gateway:
                        if locked_gateway_ip:
                            self._send_packet_to_gateway(locked_gateway_ip, checkpoint_packet)
                    else:
                        if locked_gateway_ip:
                            self._send_packet_to_gateway(locked_gateway_ip, checkpoint_packet)
                    
                    if checkpoint_retry == 0:
                        logger.debug(f"📍 Sent checkpoint request after chunk {batch_end - 1}, waiting for ACK...")
                    else:
                        logger.info(f"📍 Retrying checkpoint request (attempt {checkpoint_retry + 1}/5)...")
                    
                    ack_received = event.wait(timeout=OTA_CHUNK_ACK_TIMEOUT)
                    
                    if not ack_received:
                        checkpoint_retry += 1
                        if checkpoint_retry < 5:
                            time.sleep(0.1)  # Small delay before retry
                
                if ack_received:
                    with self._ota_ack_lock:
                        last_chunk_index = self._ota_ack_events[device_mac]['last_chunk_index']
                    
                    if last_chunk_index == batch_end - 1:
                        # All chunks in batch received successfully
                        logger.debug(f"✅ Checkpoint confirmed: chunks {batch_start}-{batch_end - 1} received")
                        chunk_idx = batch_end
                        retry_count = 0  # Reset retry counter on success
                        stall_chunk_idx = -1
                        stall_retries = 0
                        
                        # Update progress
                        bytes_sent = chunk_idx * chunk_data_size
                        if bytes_sent > len(firmware_data):
                            bytes_sent = len(firmware_data)
                        ota_manager.update_progress(device_mac, chunk_idx, total_chunks, bytes_sent)
                    else:
                        # Firmware reports 0 both when 0 chunks arrived and when only chunk 0 arrived.
                        # Track stalls at ANY chunk index (0 or N > 0) so a wedged device never loops forever.
                        next_idx = 0 if (last_chunk_index == 0 and (batch_start == 0 or stall_chunk_idx == 0)) else (last_chunk_index + 1)
                        if next_idx == stall_chunk_idx:
                            stall_retries += 1
                            if stall_retries > 5:
                                logger.error(f"❌ OTA stalled at chunk {next_idx} for {device_mac} after 5 retries")
                                ota_manager.update_session_state(device_mac, OTAState.FAILED, f"OTA stalled at chunk {next_idx}")
                                return
                        else:
                            stall_chunk_idx = next_idx
                            stall_retries = 1
                        chunk_idx = next_idx
                        retry_count = 0  # Reset timeout retry counter, device is still ACKing
                        logger.warning(f"⚠️ Packet loss detected: resuming from chunk {chunk_idx} (expected {batch_end - 1}, got {last_chunk_index})")
                else:
                    # No ACK received - retry the batch
                    retry_count += 1
                    if retry_count >= OTA_CHUNK_MAX_RETRIES:
                        logger.error(f"❌ Max retries ({OTA_CHUNK_MAX_RETRIES}) exceeded for batch starting at chunk {batch_start}")
                        ota_manager.update_session_state(device_mac, OTAState.FAILED, f"Checkpoint ACK timeout after {OTA_CHUNK_MAX_RETRIES} retries")
                        return
                    logger.warning(f"⏱️ Checkpoint ACK timeout - retrying batch from chunk {batch_start} (attempt {retry_count}/{OTA_CHUNK_MAX_RETRIES})")
                    # chunk_idx stays the same, will retry
            
            logger.info(f"✅ All chunks sent to {device_mac}, sending OTA_COMPLETE")
            
            # Send OTA_COMPLETE
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            complete_packet = self._encode_ota_complete_dual(device_mac, session.sha256_hash, msg_id)
            
            if locked_gateway_ip:
                self._send_packet_to_gateway(locked_gateway_ip, complete_packet)
            else:
                ota_manager.update_session_state(device_mac, OTAState.FAILED, "No gateway endpoint for OTA_COMPLETE")
                return
            
            # Update state
            ota_manager.update_session_state(device_mac, OTAState.VALIDATING)
            
            # Wait for device to reboot and send new HELLO
            logger.info(f"⏳ Waiting for {device_mac} to validate and reboot...")
            
        except Exception as e:
            logger.error(f"OTA transfer failed for {device_mac}: {e}")
            ota_manager.update_session_state(device_mac, OTAState.FAILED, str(e))
    
    def abort_ota_update(self, device_mac: str, reason: str = "User cancelled"):
        """Abort an ongoing or queued OTA update.
        
        Args:
            device_mac: Device MAC address
            reason: Reason for abort
        """
        session = ota_manager.get_session(device_mac)
        if not session:
            return

        # Never overwrite an already-finished session or transmit spurious abort traffic
        if session.state in (OTAState.COMPLETE, OTAState.FAILED, OTAState.ABORTED, OTAState.IDLE):
            logger.debug(f"Ignoring abort_ota_update for {device_mac} in terminal state {session.state.value}")
            return

        # If the session was only queued (never sent OTA_NOTIFY), cancel locally without radio traffic
        if session.state == OTAState.QUEUED:
            logger.info(f"🛑 Cancelled queued OTA for {device_mac}: {reason}")
            ota_manager.update_session_state(device_mac, OTAState.ABORTED, reason)
            return
        
        try:
            # Generate message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Send OTA_ABORT packet (dual-stack v1/v2)
            packet = self._encode_ota_abort_dual(device_mac, reason_code=0, msg_id=msg_id)
            
            # Send to device
            success = self._send_with_fallback(device_mac, packet, wait_for_delivery=False)
            
            logger.info(f"🛑 OTA aborted for {device_mac}: {reason}")
            
        except Exception as e:
            logger.error(f"Failed to send OTA_ABORT: {e}")
        finally:
            # Update session state
            ota_manager.update_session_state(device_mac, OTAState.ABORTED, reason)
    
    def _ota_timeout_monitor(self):
        """Background thread to monitor OTA session timeouts."""
        logger.info("OTA timeout monitor started")
        
        while self.running:
            try:
                time.sleep(5)  # Check every 5 seconds
                
                sessions = ota_manager.get_all_sessions()
                for mac, session_data in sessions.items():
                    state = session_data.get('state')
                    
                    # Check VALIDATING timeout (waiting for post-update HELLO)
                    if state == OTAState.VALIDATING.value:
                        session = ota_manager.get_session(mac)
                        if session and session.validating_start_time:
                            elapsed = (datetime.now() - session.validating_start_time).total_seconds()
                            if elapsed > OTA_POST_UPDATE_TIMEOUT:
                                logger.error(f"⏱️ OTA validation timeout for {mac}: No HELLO received after {elapsed:.0f}s")
                                ota_manager.update_session_state(mac, OTAState.FAILED, f"Validation timeout - no HELLO after {elapsed:.0f}s")
                
            except Exception as e:
                logger.error(f"Error in OTA timeout monitor: {e}")
        
        logger.info("OTA timeout monitor stopped")


# Global singleton instance - will be reconfigured in main.py
network_server = NetworkServer(
    udp_ip=DEFAULT_UDP_IP,
    udp_port=DEFAULT_UDP_PORT,
    gateway_port=DEFAULT_GATEWAY_PORT,
    home_id=home_id_manager.get_or_create_home_id()
)