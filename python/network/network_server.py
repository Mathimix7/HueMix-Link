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
from services.config_change_notifier import config_notifier
from services.home_id_manager import home_id_manager
from constants import (
    DEFAULT_UDP_IP, DEFAULT_UDP_PORT, DEFAULT_GATEWAY_PORT,
    PKT_HELLO, PKT_BTN_EVENT, PKT_DELIVERY_RPT, PKT_GW_LIST_UPD, PKT_PING, PKT_PING_DEVICE,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE,
    MAX_GATEWAY_ATTEMPTS, GATEWAY_DELIVERY_TIMEOUT_SECONDS,
    TIMEOUT_SOCKET,
    RSSI_AUTO_PAIR_THRESHOLD, MAX_GATEWAYS_PER_PACKET,
    FILE_GATEWAYS, FILE_LIGHTSTRIPS
)
from network.pairing_manager import pairing_manager
from .packet_protocol import PacketEncoder, PacketDecoder, MACFormatter
from .device_manager import device_manager

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
        self.worker_threads: List[threading.Thread] = []
        self.packet_queue = queue.Queue()
        
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
        
        # Gateway LED state tracking
        self._gateway_leds_enabled = True
        self._led_state_lock = threading.Lock()
        self._led_scheduler_thread: Optional[threading.Thread] = None
        self._last_led_hour = -1  # Track last hour LEDs were checked
        
        # Event handlers
        self._button_event_handler = None
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
            for gateway in gateways:
                radio_mac = gateway.get('radio_mac')
                wifi_mac = gateway.get('mac_address')
                ip = gateway.get('ip_address')
                
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
            
            # Start worker threads (4 workers)
            for i in range(4):
                worker = threading.Thread(target=self._worker_loop, daemon=True, name=f"Worker-{i}")
                worker.start()
                self.worker_threads.append(worker)
            
            # Start LED scheduler thread
            self._led_scheduler_thread = threading.Thread(target=self._led_scheduler_loop, daemon=True, name="LED-Scheduler")
            self._led_scheduler_thread.start()
            
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
        
        for worker in self.worker_threads:
            worker.join(timeout=2.0)
        
        # Close socket
        if self.sock:
            self.sock.close()
            self.sock = None
        
        logger.info("NetworkServer stopped")
    
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
        
        elif pkt_type == PKT_DELIVERY_RPT:
            self._handle_delivery_report(payload, sender_ip)
        
        elif pkt_type == PKT_PING:
            self._handle_ping_response(src_mac, payload, sender_ip)
        
        elif pkt_type == PKT_PING_DEVICE:
            self._handle_ping_device_response(src_mac, payload, sender_ip)
        
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
        device_manager.update_gateway(wifi_mac, radio_mac, sender_ip)
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
        
        # Find gateway radio MAC by IP
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break
        
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
                device_manager.update_button_tracking(button_mac, gateway_radio_mac, rssi)
            
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
        
        # Find gateway radio MAC by IP
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break
        
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
                device_manager.add_lightstrip(light_mac, f"Light {light_mac[-8:]}", num_leds, is_rgbw)
                
                # Set initial gateway
                if gateway_radio_mac:
                    device_manager.update_light_gateway(light_mac, gateway_radio_mac)
                
                # Send gateway list to newly paired light
                with self._gateway_lock:
                    all_gateway_macs = list(self._gateway_table.keys())
                    if all_gateway_macs:
                        light_packet = self.encoder.encode_gateway_list_for_device(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
                        self.sock.sendto(light_packet, (sender_ip, self.gateway_port))
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
                device_manager.add_lightstrip(light_mac, f"Light {light_mac[-8:]}", num_leds, is_rgbw)
                # Record in pairing history as a reconnected device
                pairing_manager.record_device_paired(light_mac, DEV_LIGHT, f"Light {light_mac[-8:]}", 'short_range')
                
                # Set initial gateway and send gateway list
                if gateway_radio_mac:
                    device_manager.update_light_gateway(light_mac, gateway_radio_mac)
                
                with self._gateway_lock:
                    all_gateway_macs = list(self._gateway_table.keys())
                    if all_gateway_macs:
                        light_packet = self.encoder.encode_gateway_list_for_device(light_mac, all_gateway_macs[:MAX_GATEWAYS_PER_PACKET])
                        self.sock.sendto(light_packet, (sender_ip, self.gateway_port))
                        logger.debug(f"Sent initial gateway list to {light_mac}")
            
            # Update gateway that successfully received HELLO
            if gateway_radio_mac:
                device_manager.update_light_gateway(light_mac, gateway_radio_mac)
                logger.debug(f"Light {light_mac} online via {gateway_radio_mac} / {sender_ip} (RSSI: {rssi} dBm)")
            else:
                logger.warning(f"Light {light_mac} HELLO from unknown gateway {sender_ip}")
    
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
        
        # Find gateway radio MAC by IP
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break
        
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
                device_manager.add_button(remote_mac, f"Remote {remote_mac[-8:]}", device_type=DEV_REMOTE)
                
                if pairing_mode_active:
                    logger.info(f"🎮 Paired remote via pairing mode: {remote_mac}")
                    pairing_manager.record_device_paired(remote_mac, DEV_REMOTE, f"Remote {remote_mac[-8:]}", 'long_range')
                else:
                    logger.info(f"🎮 Auto-paired remote (RSSI: {rssi} dBm): {remote_mac}")
                    pairing_manager.record_device_paired(remote_mac, DEV_REMOTE, f"Remote {remote_mac[-8:]}", 'short_range')
            else:
                logger.warning(f"Remote {remote_mac} RSSI too weak for auto-pairing: {rssi} dBm (use pairing mode to pair anyway)")
        else:
            # Paired remote - update tracking
            if gateway_radio_mac:
                device_manager.update_button_tracking(remote_mac, gateway_radio_mac, rssi)
            
            logger.debug(f"Remote {remote_mac} online (RSSI: {rssi} dBm)")
    
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
        button_index = event_data.get('button_index')
        
        action_str = {1: "CLICK", 2: "HOLD", 3: "RELEASE", 9: "SYNC"}.get(action, f"UNKNOWN({action})")
        
        if button_index is not None:
            logger.info(f"🔘 Remote {button_mac} button {button_index} -> {action_str}")
        else:
            logger.info(f"🔘 Button {button_mac} -> {action_str}")
        
        # Auto-add button/remote if it doesn't exist
        button = device_manager.get_button_by_mac(button_mac)
        if not button:
            if button_index is None:
                logger.info(f"Auto-registering button: {button_mac}")
                device_manager.add_button(button_mac, f"Button {button_mac[-8:]}")
                pairing_manager.record_device_paired(button_mac, DEV_BUTTON, f"Button {button_mac[-8:]}", 'short_range')
            else:
                logger.info(f"Auto-registering remote: {button_mac}")
                device_manager.add_button(button_mac, f"Remote {button_mac[-8:]}", device_type=DEV_REMOTE)
                pairing_manager.record_device_paired(button_mac, DEV_REMOTE, f"Remote {button_mac[-8:]}", 'short_range')
        
        # Find gateway for tracking
        gateway_radio_mac = None
        with self._gateway_lock:
            for radio_mac, info in self._gateway_table.items():
                if info['ip_address'] == sender_ip:
                    gateway_radio_mac = radio_mac
                    break
        
        if gateway_radio_mac:
            device_manager.update_button_tracking(button_mac, gateway_radio_mac, 0)
        
        # Call event handler
        if self._button_event_handler:
            self._button_event_handler(button_mac, action, 0, button_index=button_index)
    
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
        self.sock.sendto(packet, (gateway_ip, self.gateway_port))
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
                self.sock.sendto(packet, (gateway_ip, self.gateway_port))
                logger.debug(f"Sent packet to gateway {target_mac} at {gateway_ip}")
                return True
            except Exception as e:
                logger.error(f"Failed to send to gateway {target_mac}: {e}")
                return False
        
        # Target is a device - use smart routing with fallback
        _, gateway_mac = device_manager.get_light_gateway(target_mac)
        
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
                    self.sock.sendto(packet, (gw_ip, self.gateway_port))
                    logger.info(f"Sent to {target_mac} via {gw_ip} (gateway: {gw_radio_mac}, msgID: {msg_id}, attempt: {attempt+1})")
                    
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
                    self.sock.sendto(packet, (gw_ip, self.gateway_port))
                    logger.debug(f"Sent to {target_mac} via {gw_ip} (gateway: {gw_radio_mac}, no delivery tracking)")
                    return True
                    
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
                self.sock.sendto(packet, (gateway_ip, self.gateway_port))
            
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
            self.sock.sendto(packet, (gateway_ip, self.gateway_port))
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
        cmd_name = {1: "Night Mode ON", 2: "Night Mode OFF"}.get(command, f"Command {command}")
        
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
                self.sock.sendto(packet, (gateway_ip, self.gateway_port))
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
            self.sock.sendto(packet, (gateway_ip, self.gateway_port))
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
            
            # Find gateway radio MAC by IP
            gateway_radio_mac = None
            with self._gateway_lock:
                for radio_mac, info in self._gateway_table.items():
                    if info['ip_address'] == sender_ip:
                        gateway_radio_mac = radio_mac
                        break
            
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


# Global singleton instance - will be reconfigured in main.py
network_server = NetworkServer(
    udp_ip=DEFAULT_UDP_IP,
    udp_port=DEFAULT_UDP_PORT,
    gateway_port=DEFAULT_GATEWAY_PORT,
    home_id=home_id_manager.get_or_create_home_id()
)