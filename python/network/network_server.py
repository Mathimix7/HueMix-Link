"""
UDP network server for HueMixLink protocol.

Handles UDP socket communication, packet routing, gateway mesh management,
and delivery tracking with automatic failover.
"""
import socket
import threading
import queue
import logging
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
    PKT_OTA_READY, PKT_OTA_CHUNK_ACK, PKT_OTA_ABORT,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, DEV_MOTION, DEV_DOOR,
    MAX_GATEWAY_ATTEMPTS, GATEWAY_DELIVERY_TIMEOUT_SECONDS,
    TIMEOUT_SOCKET, OTA_READY_TIMEOUT, OTA_CHUNK_DATA_SIZE,
    OTA_CHUNK_ACK_TIMEOUT, OTA_CHUNK_MAX_RETRIES, OTA_CHECKPOINT_INTERVAL,
    OTA_POST_UPDATE_TIMEOUT,
    RSSI_AUTO_PAIR_THRESHOLD, MAX_GATEWAYS_PER_PACKET,
    FILE_GATEWAYS, FILE_LIGHTSTRIPS, FILE_MOTION_SENSORS,
    CMD_SET_MOTION_COOLDOWN, CMD_SET_MOTION_SLEEP, CMD_NIGHT_MODE_ON, CMD_NIGHT_MODE_OFF, CMD_NETNODE_WIFI_STATUS
)
from network.pairing_manager import pairing_manager
from .packet_protocol import PacketEncoder, PacketDecoder, MACFormatter
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
        """Push the current HOME_ID to the direct serial radio node."""
        if not self._serial_transport or not self._serial_transport.available:
            return False

        endpoint = self._serial_endpoint
        if not endpoint:
            return False

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
                time.sleep(0.01)
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
            return 1 if hue_service.is_initialized() else 0
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

        ip = "0.0.0.0"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
        except Exception:
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
            except Exception:
                ip = "0.0.0.0"

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
                data, addr = self.sock.recvfrom(1024)
                
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
    
    def _handle_packet(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming packet.
        
        Args:
            data: Raw packet data
            addr: Sender (ip, port)
        """
        sender_ip, sender_port = addr
        
        # Decode packet
        packet = self.decoder.decode(data)
        if not packet:
            return  # Invalid packet
        
        pkt_type = packet['type']
        src_mac = packet['source_mac']
        is_paired = packet['is_paired']
        payload = packet['payload']
        
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
            self._handle_ping_device_response(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_OTA_READY:
            self._handle_ota_ready(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_OTA_CHUNK_ACK:
            self._handle_ota_chunk_ack(src_mac, payload)
        
        elif pkt_type == PKT_OTA_ABORT:
            self._handle_ota_abort_from_device(src_mac, payload)
        
        elif pkt_type == PKT_GW_LIST_UPD:
            # Gateway received our list - no action needed
            pass
    
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
        
        # Handle by device type
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
        
        # If unpaired, send pairing confirmation
        if not is_paired:
            # Auto-pair gateways
            self._send_pair_confirm(wifi_mac, sender_ip)
            logger.info(f"Auto-paired gateway: {wifi_mac}")
            # Record in pairing manager
            pairing_manager.record_device_paired(wifi_mac, DEV_GATEWAY, f"Gateway {wifi_mac[-8:]}", 'wifi')
        
        if is_new_gateway:
            pairing_manager.record_device_paired(wifi_mac, DEV_GATEWAY, f"Gateway {wifi_mac[-8:]}", 'wifi')

        if is_paired:
            self._broadcast_gateway_list((not is_new_gateway))
    
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
            
            # If pairing mode not active, check RSSI threshold
            if not pairing_mode_active and rssi > RSSI_AUTO_PAIR_THRESHOLD:
                should_pair = True
            
            if should_pair:
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
            
            # If pairing mode not active, check RSSI threshold
            if not pairing_mode_active and rssi > RSSI_AUTO_PAIR_THRESHOLD:
                should_pair = True
            
            if should_pair:
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
                        light_packet = self.encoder.encode_gateway_list_for_device(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
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
                        light_packet = self.encoder.encode_gateway_list_for_device(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
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
                logger.debug(f"Light {light_mac} online via {gateway_radio_mac} / {sender_ip} (RSSI: {rssi} dBm)")
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
            
            # If pairing mode not active, check RSSI threshold
            if not pairing_mode_active and rssi > RSSI_AUTO_PAIR_THRESHOLD:
                should_pair = True
            
            if should_pair:
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
            
            if rssi >= RSSI_AUTO_PAIR_THRESHOLD or pairing_mode_active:
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

            if rssi >= RSSI_AUTO_PAIR_THRESHOLD or pairing_mode_active:
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
                        
                        # Encode packet with uint32 cooldown value
                        packet = self.encoder.encode_sys_cmd(
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
            
            # Encode packet with uint32 sleep duration
            packet = self.encoder.encode_sys_cmd(
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
    
    def _send_with_fallback(self, target_mac: str, packet: bytes, wait_for_delivery: bool = True, msg_id: Optional[int] = None) -> bool:
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
                if info['wifi_mac'] == target_mac:
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
        gateway_mac = None

        # Lightstrip routing preference
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
            
            # Encode gateway list packet (max gateways per packet)
            packet = self.encoder.encode_gateway_list(all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
            
            # Send to all gateways
            for radio_mac, info in self._gateway_table.items():
                gateway_ip = info['ip_address']
                self._send_packet_to_gateway(gateway_ip, packet)
            
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
                        
                        # Encode gateway list packet for this light with msg_id
                        light_packet = self.encoder.encode_gateway_list_for_device(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET], msg_id)
                        
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
        
        # Encode packet
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
            # Encode and send ping packet
            packet = self.encoder.encode_ping(gateway_mac)
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
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['wifi_mac'] == target_mac:
                    is_gateway = True
                    break
        
        # Generate message ID (always needed for packet encoding)
        with self._delivery_lock:
            msg_id = self._next_msg_id
            self._next_msg_id = (self._next_msg_id + 1) % 256
        
        # Encode packet
        packet = self.encoder.encode_sys_cmd(target_mac, command, value, msg_id)
        
        # Send with fallback (delivery tracking for devices, direct send for gateways)
        success = self._send_with_fallback(target_mac, packet, wait_for_delivery=(not is_gateway), msg_id=msg_id)
        
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
            
            # Encode ping device packet
            packet = self.encoder.encode_ping_device(device_mac)
            
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
        
        # Send ping via the specific gateway
        packet = self.encoder.encode_ping_device(device_mac)
        
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
    
    def _handle_ping_device_response(self, src_mac: str, payload: bytes, sender_ip: str):
        """Handle ping device response from gateway.
        
        Args:
            src_mac: Device MAC address (source of response)
            payload: Raw payload with RSSI in first byte
            sender_ip: Gateway IP
        """
        try:
            # RSSI is stored in payload[0] by gateway radio node
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
    
    def start_ota_update(self, device_mac: str, device_type: int, firmware_path: str) -> bool:
        """Start OTA firmware update for a device.
        
        Args:
            device_mac: Target device MAC address
            device_type: Device type constant
            firmware_path: Path to firmware binary file
            
        Returns:
            True if OTA initiated successfully, False otherwise
        """
        # Create OTA session
        session = ota_manager.create_session(device_mac, device_type, firmware_path)
        if not session:
            logger.error(f"Failed to create OTA session for {device_mac}")
            return False
        
        try:
            # Generate message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Send OTA_NOTIFY packet
            packet = self.encoder.encode_ota_notify(
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
                # Send via gateway mesh (no delivery tracking - device responds with PKT_OTA_READY instead)
                success = self._send_with_fallback(device_mac, packet, wait_for_delivery=False)
                if not success:
                    ota_manager.update_session_state(device_mac, OTAState.FAILED, "Failed to send OTA_NOTIFY")
                    return False
                logger.info(f"📡 OTA_NOTIFY sent to device {device_mac}")
            
            # Update session state
            ota_manager.update_session_state(device_mac, OTAState.WAITING_READY)
            
            # Start timeout thread
            threading.Thread(
                target=self._ota_ready_timeout,
                args=(device_mac,),
                daemon=True,
                name=f"OTA-Ready-Timeout-{device_mac[-8:]}"
            ).start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start OTA for {device_mac}: {e}")
            ota_manager.update_session_state(device_mac, OTAState.FAILED, str(e))
            return False
    
    def _ota_ready_timeout(self, device_mac: str):
        """Timeout thread waiting for PKT_OTA_READY response.
        
        Args:
            device_mac: Device MAC address
        """
        time.sleep(OTA_READY_TIMEOUT)
        
        session = ota_manager.get_session(device_mac)
        if session and session.state == OTAState.WAITING_READY:
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
        if firmware_size_in_ready == 0 and session.state == OTAState.WAITING_READY:
            logger.info(f"📡 Device {src_mac} woke up in OTA mode - re-sending OTA_NOTIFY")
            
            # Small delay to allow ESP-NOW peer connection to stabilize after device wakeup
            time.sleep(0.2)
            
            # Generate new message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Re-send OTA_NOTIFY packet
            packet = self.encoder.encode_ota_notify(
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
            else:
                logger.error(f"Failed to re-send OTA_NOTIFY to {src_mac}")
            return
        
        # Normal PKT_OTA_READY response (firmware_size matches session)
        if session.state != OTAState.WAITING_READY:
            logger.warning(f"Unexpected OTA_READY from {src_mac} in state {session.state}")
            return
        
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
            
            # Calculate chunks
            total_chunks = (len(firmware_data) + OTA_CHUNK_DATA_SIZE - 1) // OTA_CHUNK_DATA_SIZE
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
            
            # Set chunk delay based on target type
            if is_gateway and not is_radio_node:
                chunk_delay = 0.010  # Net node direct
            else:
                chunk_delay = 0.016  # Other devices via ESP-NOW
            
            if is_gateway and locked_gateway_ip and self._is_serial_endpoint(locked_gateway_ip):
                chunk_delay = 0.000
                       
            logger.info(f"📦 Starting OTA transfer to {device_mac}: {len(firmware_data)} bytes in {total_chunks} chunks (is_gateway={is_gateway}, is_radio_node={is_radio_node}, delay={chunk_delay}s)")
                   
            
            # Send chunks with checkpoint-based ACK
            chunk_idx = 0
            retry_count = 0
            
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
                    offset = i * OTA_CHUNK_DATA_SIZE
                    chunk_data = firmware_data[offset:offset + OTA_CHUNK_DATA_SIZE]
                    
                    # Generate message ID
                    with self._delivery_lock:
                        msg_id = self._next_msg_id
                        self._next_msg_id = (self._next_msg_id + 1) % 256
                    
                    # Encode chunk packet
                    packet = self.encoder.encode_ota_chunk(device_mac, i, chunk_data, msg_id)
                    
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
                    
                    # Send checkpoint request to device
                    checkpoint_packet = self.encoder.encode_ota_checkpoint_req(device_mac, checkpoint_msg_id)
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
                        
                        # Update progress
                        bytes_sent = chunk_idx * OTA_CHUNK_DATA_SIZE
                        if bytes_sent > len(firmware_data):
                            bytes_sent = len(firmware_data)
                        ota_manager.update_progress(device_mac, chunk_idx, total_chunks, bytes_sent)
                    else:
                        # Some chunks were lost - resume from last received + 1
                        chunk_idx = last_chunk_index + 1
                        retry_count = 0  # Reset retry counter, we're making progress
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
            
            complete_packet = self.encoder.encode_ota_complete(device_mac, session.sha256_hash, msg_id)
            
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
        """Abort an ongoing OTA update.
        
        Args:
            device_mac: Device MAC address
            reason: Reason for abort
        """
        session = ota_manager.get_session(device_mac)
        if not session:
            return
        
        try:
            # Generate message ID
            with self._delivery_lock:
                msg_id = self._next_msg_id
                self._next_msg_id = (self._next_msg_id + 1) % 256
            
            # Send OTA_ABORT packet
            packet = self.encoder.encode_ota_abort(device_mac, reason_code=0, msg_id=msg_id)
            
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