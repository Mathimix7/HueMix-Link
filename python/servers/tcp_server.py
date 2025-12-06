"""TCP server that receives button press events from ESP32 receivers."""
import socket
import threading
import json
import logging
import re
from typing import Dict, Optional
from datetime import datetime

from controllers.hue_controller import Hue
from services import data_manager, config_notifier, event_bus, button_state_manager
from services.hue_state_manager import hue_state_manager

logger = logging.getLogger(__name__)


class ButtonTCPServer:
    """
    TCP server that receives button press events from ESP32 receivers.
    Each ESP32 receiver communicates via ESP-NOW with buttons and forwards
    the MAC address to this server via TCP.
    """
    
    def __init__(self, host: str = '0.0.0.0', port: int = 5555):
        """
        Initialize TCP server.
        
        Args:
            host: Host to bind to (0.0.0.0 for all interfaces)
            port: Port to listen on
        """
        self.host = host
        self.port = port
        self.server_socket = None
        self._running = False
        self._server_thread = None
        self._config_thread = None
        self.hue_controller: Optional[Hue] = None
        self._button_configs: Dict[str, Dict] = {}  # mac_address -> config
        self._button_holding_status: Dict[str, int] = {}  # mac_address -> 0 (decrease) or 1 (increase)
    
    @staticmethod
    def _is_valid_mac_address(mac: str) -> bool:
        """Validate MAC address format."""
        pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
        return bool(pattern.match(mac))
        
    def start(self):
        """Start the TCP server and background threads."""
        if self._running:
            logger.warning("TCP server already running")
            return
        
        # Load Hue controller
        self._init_hue_controller()
        
        # Load button configurations
        self._load_button_configs()

        # Load button state manager configurations
        button_state_manager.load_config(self._button_configs)

        # Start server
        self._running = True
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        
        # Start config update listener
        self._config_thread = threading.Thread(target=self._config_update_worker, daemon=True)
        self._config_thread.start()
        
        logger.info(f"ButtonTCPServer started on {self.host}:{self.port}")
    
    def stop(self):
        """Stop the TCP server."""
        self._running = False
        
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        
        if self._server_thread:
            self._server_thread.join(timeout=2)
        
        if self._config_thread:
            self._config_thread.join(timeout=2)
        
        logger.info("ButtonTCPServer stopped")
    
    def _init_hue_controller(self):
        """Initialize Hue controller from saved config."""
        try:
            config = data_manager.read_json('bridge.json', default={})
            bridge_ip = config.get('ip')
            api_token = config.get('username')
            
            if bridge_ip and api_token:
                self.hue_controller = Hue(bridge_ip, api_token)
                logger.info(f"Hue controller initialized for bridge {bridge_ip}")
            else:
                logger.warning("No Hue bridge configuration found. Please configure via web UI.")
        except Exception as e:
            logger.error(f"Failed to initialize Hue controller: {e}")
    
    def _load_button_configs(self):
        """Load button configurations from JSON file."""
        try:
            buttons = data_manager.read_json('buttons.json', default=[])
            self._button_configs.clear()
            
            for button in buttons:
                if button.get('configured') and button.get('mac_address'):
                    mac = button['mac_address'].upper()
                    self._button_configs[mac] = button.get('config', {})
            
            logger.info(f"Loaded {len(self._button_configs)} button configurations")
        except Exception as e:
            logger.error(f"Failed to load button configs: {e}")
    
    def _run_server(self):
        """Main server loop that accepts connections."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            self.server_socket.settimeout(1.0)  # Timeout to check _running flag
            
            logger.info(f"TCP server listening on {self.host}:{self.port}")
            
            while self._running:
                try:
                    client_socket, address = self.server_socket.accept()
                    logger.info(f"Connection from {address}")
                    
                    # Handle in separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_socket, address),
                        daemon=True
                    )
                    client_thread.start()
                    
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        logger.error(f"Error accepting connection: {e}")
        
        except Exception as e:
            logger.error(f"TCP server error: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()
    
    def _handle_client(self, client_socket: socket.socket, address):
        """
        Handle a client connection from ESP32 receiver.
        
        Message formats:
        1. "svMacs,<mac>" - Server registration/heartbeat
        2. "time" - Time sync request
        3. "<button_mac>,<mode>,<server_mac>" - Button press event
           - mode: "Once" (single press), "Holding" (long press), "HoldingStopped" (release)
        """
        try:
            data = client_socket.recv(1024)
            if not data:
                return
            
            message = data.decode('utf-8').strip()
            logger.info(f"Received from {address}: {message}")
            
            parts = message.split(',')
            
            # Route message to appropriate handler
            if parts[0] == "svMacs" and len(parts) >= 2:
                self._handle_server_registration(parts[1], address, client_socket)
            elif parts[0] == "time":
                self._handle_time_sync(client_socket)
            elif len(parts) == 3:
                self._handle_button_event(parts[0], parts[1], parts[2], address)
            else:
                logger.warning(f"Invalid message format from {address}: {message}")
        
        except Exception as e:
            logger.error(f"Error handling client {address}: {e}")
        finally:
            client_socket.close()
    
    def _handle_server_registration(self, server_mac: str, address, client_socket: socket.socket):
        """Handle ESP32 server registration/heartbeat."""
        def update_servers(servers):
            if servers is None:
                servers = []
            
            # Update existing server or add new one
            for server in servers:
                if server_mac == server.get("mac_address"):
                    server["last_used"] = datetime.now().isoformat()
                    server["ip_address"] = address[0]
                    return servers  # Return full servers list
            
            # Server not found - add if valid
            if self._is_valid_mac_address(server_mac):
                # Generate new ID
                max_id = max([int(s.get("id", 0)) for s in servers], default=0)
                servers.append({
                    "id": str(max_id + 1),
                    "name": "UNKNOWN",
                    "mac_address": server_mac,
                    "ip_address": address[0],
                    "last_used": datetime.now().isoformat()
                })
                logger.info(f"New server registered: {server_mac}")
            else:
                logger.error(f"Invalid MAC address: {server_mac}")
            
            return servers  # Return full servers list
        
        # Update servers and extract MAC addresses
        updated_servers = data_manager.update_json('servers.json', update_servers)
        server_macs = [s.get("mac_address") for s in updated_servers]
        
        # Send list of all server MACs back
        client_socket.sendall(",".join(server_macs).encode())
    
    def _handle_time_sync(self, client_socket: socket.socket):
        """Handle time synchronization request from ESP32."""
        timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f%z')
        client_socket.sendall(timestamp.encode())
    
    def _handle_button_event(self, button_mac: str, mode: str, server_mac: str, address):
        """Handle button press event from ESP32."""
        button_mac = button_mac.upper()
        server_mac = server_mac.upper()
        
        # Update server timestamp
        self._update_server_timestamp(server_mac, address[0])
        
        # Ensure button is registered
        self._ensure_button_registered(button_mac)
        
        # Route to appropriate button handler
        if mode == "Once":
            self._handle_button_press(button_mac)
        elif mode == "Holding":
            self._handle_button_holding(button_mac)
        elif mode == "HoldingStopped":
            self._handle_button_hold_stopped(button_mac)
        else:
            logger.warning(f"Unknown button mode: {mode}")
    
    def _update_server_timestamp(self, server_mac: str, ip: str):
        """Update server's last used timestamp and IP address."""
        def update(servers):
            if servers is None:
                servers = []
            
            for server in servers:
                if server_mac == server.get("mac_address"):
                    server["last_used"] = datetime.now().isoformat()
                    server["ip_address"] = ip
                    return servers
            
            # Server not found - add it
            max_id = max([int(s.get("id", 0)) for s in servers], default=0)
            servers.append({
                "id": str(max_id + 1),
                "name": "UNKNOWN",
                "mac_address": server_mac,
                "ip_address": ip,
                "last_used": datetime.now().isoformat()
            })
            return servers
        
        data_manager.update_json('servers.json', update)
    
    def _ensure_button_registered(self, button_mac: str):
        """Ensure button is registered and initialize holding status."""
        # Initialize holding status if needed
        if button_mac not in self._button_holding_status:
            self._button_holding_status[button_mac] = 0
        
        # Check if button exists in JSON
        def check_and_add(buttons):
            if buttons is None:
                buttons = []
            
            for button in buttons:
                if button.get("mac_address") == button_mac:
                    return buttons  # Already registered
            
            # Not found - add new button
            logger.info(f"New button registered: {button_mac}")
            buttons.append({
                "device_name": "UNKNOWN",
                "mac_address": button_mac,
                "configured": False
            })
            return buttons
        
        data_manager.update_json('buttons.json', check_and_add)
    
    def _handle_button_press(self, mac_address: str):
        """
        Handle a button press event.
        
        If the button press is after the timeout (cycle reset), check the room state:
        - If lights are on, turn them off
        - If lights are off, activate the first scene
        
        If the button press is within the timeout, cycle to the next scene.
        
        Args:
            mac_address: MAC address of the button that was pressed
        """
        logger.info(f"Button press: {mac_address}")
        
        # Look up button configuration
        config = self._button_configs.get(mac_address)
        if not config:
            logger.warning(f"No configuration found for button {mac_address}")
            return
        
        room_id = config.get('room_id')
        if not room_id:
            logger.warning(f"No room_id configured for button {mac_address}")
            return
        
        if not self.hue_controller:
            logger.error("Hue controller not initialized")
            return
        
        # Check if this button press is after the timeout (would reset to scene 0)
        is_timeout_reset = button_state_manager.is_timeout_expired(mac_address)
        try:
            turn_off = is_timeout_reset and self.hue_controller.is_room_on(room_id)
        except Exception as e:
            turn_off = False
            logger.error(f"Failed to handle room state for {room_id}: {e}")

        if turn_off:
            logger.info(f"Room {room_id} is on, turning off...")
            try:
                self.hue_controller.set_room(room_id, {"on": {"on": False}})
                logger.info(f"Turned off room {room_id}")
                
                # Update state manager immediately (before SSE)
                hue_state_manager.update_room(room_id, is_on=False, source='button')
                
            except Exception as e:
                logger.error(f"Failed to turn off room {room_id}: {e}")
            
            button_state_manager.mark_press(mac_address)
            event_bus.publish('room_toggled', {
                        'room_id': room_id,
                        'button_mac': mac_address,
                        'turned_on': False,
            })
        else:
            scene_id = button_state_manager.get_next_scene(mac_address)
            
            if not scene_id:
                logger.warning(f"No scene to activate for button {mac_address}")
                return
            
            try:
                self.hue_controller.set_scene(scene_id, payload={"recall": {"action": "active"}})
                logger.info(f"Activated scene {scene_id} in room {room_id}")
                
                # Update state manager immediately (before SSE)
                hue_state_manager.set_room_scene(room_id, scene_id, source='button')
                
                # Publish scene change event for lightstrip sync
                event_bus.publish('scene_changed', {
                    'scene_id': scene_id,
                    'room_id': room_id,
                    'button_mac': mac_address,
                })
                
            except Exception as e:
                logger.error(f"Failed to activate scene {scene_id}: {e}")
    
    def _handle_button_holding(self, mac_address: str):
        """
        Handle button hold event - adjust brightness.
        
        Holding status alternates between decrease (0) and increase (1) brightness.
        
        Args:
            mac_address: MAC address of the button being held
        """
        logger.info(f"Button holding: {mac_address}")
        
        # Look up button configuration
        config = self._button_configs.get(mac_address)
        if not config:
            logger.warning(f"No configuration found for button {mac_address}")
            return
        
        room_id = config.get('room_id')
        if not room_id:
            logger.warning(f"No room_id configured for button {mac_address}")
            return
        
        if not self.hue_controller:
            logger.error("Hue controller not initialized")
            return
        
        try:
            holding_status = self._button_holding_status.get(mac_address, 0)
            
            if holding_status == 0:
                # Decrease brightness
                logger.debug(f"Decreasing brightness for room {room_id}")
                # Adjust by -10% brightness (dimming value: -10.9 maps to roughly -10%)
                self.hue_controller.set_room(room_id, {
                    "dimming_delta": {"action": "down", "brightness_delta": 10.0}
                })
                
                # Update state manager (brightness decreased)
                # Note: We don't know exact new brightness, SSE will provide that
                hue_state_manager.update_room(room_id, source='button')
                
            else:
                # Increase brightness
                logger.debug(f"Increasing brightness for room {room_id}")
                # Adjust by +10% brightness
                self.hue_controller.set_room(room_id, {
                    "dimming_delta": {"action": "up", "brightness_delta": 10.0}
                })
                
                # Update state manager (brightness increased)
                hue_state_manager.update_room(room_id, source='button')
        
        except Exception as e:
            logger.error(f"Failed to adjust brightness for room {room_id}: {e}")
    
    def _handle_button_hold_stopped(self, mac_address: str):
        """
        Handle button hold release event - toggle brightness direction.
        
        Args:
            mac_address: MAC address of the button that was released
        """
        logger.info(f"Button hold stopped: {mac_address}")
        
        # Toggle holding status between 0 (decrease) and 1 (increase)
        current_status = self._button_holding_status.get(mac_address, 0)
        self._button_holding_status[mac_address] = 1 if current_status == 0 else 0
        
        logger.debug(f"Button {mac_address} holding status toggled to {self._button_holding_status[mac_address]}")
    
    def _config_update_worker(self):
        """Background worker that polls the update queue for config changes from Flask."""
        logger.info("Config update worker started")
        
        while self._running:
            try:
                # Poll queue with timeout
                change = config_notifier.get_change(block=True, timeout=1.0)
                
                if change:
                    change_type = change.get('type')
                    data = change.get('data', {})
                    
                    logger.info(f"Received config change: {change_type}")
                    
                    if change_type == 'button_config':
                        # Reload all button configs
                        self._load_button_configs()
                        
                        # Update scene state manager
                        for mac_address in self._button_configs.keys():
                            config = self._button_configs.get(mac_address)
                            if not config:
                                continue
                            scenes = config.get('scenes', [])
                        
                            button_state_manager.update_button(mac_address, scenes)
                    
                    elif change_type in ['bridge_config', 'bridge_config_deleted']:
                        # Reload Hue controller
                        self._init_hue_controller()
                    
            except Exception as e:
                if self._running:
                    # Timeout is normal, other errors should be logged
                    if "Empty" not in str(e):
                        logger.error(f"Config update worker error: {e}")
        
        logger.info("Config update worker stopped")


# Global instance (will be started by main.py)
tcp_server = ButtonTCPServer(host='0.0.0.0', port=5555)
