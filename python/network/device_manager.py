"""
Device manager for tracking gateways, buttons, and lightstrips with routing intelligence.

Manages device registry in JSON files with delivery-based gateway failover logic.
"""
import logging
from typing import Optional, List, Dict
from datetime import datetime
from services.data_manager import data_manager
from constants import FILE_BUTTONS, FILE_GATEWAYS, FILE_LIGHTSTRIPS, FILE_MOTION_SENSORS, DEV_REMOTE, DEV_BUTTON
import uuid

logger = logging.getLogger(__name__)


class DeviceManager:
    """Manages device registry and gateway routing with failure tracking."""
    
    def __init__(self):
        """Initialize device manager."""
        logger.info("DeviceManager initialized")
    
    # ===== Gateway Management =====
    
    def get_gateway_by_radio_mac(self, radio_mac: str) -> Optional[Dict]:
        """Get gateway by its radio MAC address.
        
        Args:
            radio_mac: Radio MAC address (with colons)
            
        Returns:
            Gateway dict or None if not found
        """
        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        for server in servers:
            if server.get('radio_mac', '').upper() == radio_mac.upper():
                return server
        return None
    
    def get_gateway_by_wifi_mac(self, wifi_mac: str) -> Optional[Dict]:
        """Get gateway by its WiFi MAC address.
        
        Args:
            wifi_mac: WiFi MAC address (with colons)
            
        Returns:
            Gateway dict or None if not found
        """
        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        for server in servers:
            if server.get('mac_address', '').upper() == wifi_mac.upper():
                return server
        return None
    
    def update_gateway(self, wifi_mac: str, radio_mac: str, ip_address: str, version_net: Optional[str] = None, version_radio: Optional[str] = None) -> Optional[Dict]:
        """Update or create gateway entry.
        
        Args:
            wifi_mac: WiFi MAC address
            radio_mac: Radio MAC address for mesh routing
            ip_address: Current IP address
            version_net: Net node firmware version
            version_radio: Radio node firmware version
            
        Returns:
            Updated gateway dict
        """
        def update_func(servers):
            # Find existing gateway
            gateway = None
            for server in servers:
                if server.get('mac_address', '').upper() == wifi_mac.upper():
                    gateway = server
                    break
            
            if gateway:
                # Update existing
                gateway['radio_mac'] = radio_mac.upper()
                gateway['ip_address'] = ip_address
                gateway['last_used'] = datetime.now().isoformat()
                if version_net:
                    gateway['version_net'] = version_net
                if version_radio:
                    gateway['version_radio'] = version_radio
            else:
                # Create new gateway
                gateway = {
                    'id': uuid.uuid4().hex,
                    'name': f"Gateway {wifi_mac[-8:]}",
                    'mac_address': wifi_mac.upper(),
                    'radio_mac': radio_mac.upper(),
                    'ip_address': ip_address,
                    'version_net': version_net or '0.0.0',
                    'version_radio': version_radio or '0.0.0',
                    'last_used': datetime.now().isoformat(),
                }
                servers.append(gateway)
            
            return servers
        
        data_manager.update_json(FILE_GATEWAYS, update_func)
        return self.get_gateway_by_wifi_mac(wifi_mac)
    
    def get_all_gateways(self) -> List[Dict]:
        """Get all registered gateways.
        
        Returns:
            List of gateway dicts
        """
        return data_manager.read_json(FILE_GATEWAYS, default=[])
    
    def get_gateway_ip_by_radio_mac(self, radio_mac: str) -> Optional[str]:
        """Get gateway IP address by radio MAC.
        
        Args:
            radio_mac: Radio MAC address
            
        Returns:
            IP address or None
        """
        gateway = self.get_gateway_by_radio_mac(radio_mac)
        return gateway.get('ip_address') if gateway else None
    
    # ===== Lightstrip Management =====
    
    def get_light_by_mac(self, mac_address: str) -> Optional[Dict]:
        """Get lightstrip by MAC address.
        
        Args:
            mac_address: Light MAC address (with colons)
            
        Returns:
            Lightstrip dict or None if not found
        """
        strips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        for strip in strips:
            if strip.get('mac_address', '').upper() == mac_address.upper():
                return strip
        return None
    
    def get_all_lights(self) -> List[Dict]:
        """Get all registered lightstrips.
        
        Returns:
            List of lightstrip dicts
        """
        return data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
    
    def update_light_gateway(self, light_mac: str, gateway_radio_mac: str, rssi: Optional[int] = None, version: Optional[str] = None, platform: Optional[str] = None, model_id: Optional[int] = None):
        """Update lightstrip's last successful gateway and tracking info.
        
        Records the gateway that successfully delivered to this light.
        
        Args:
            light_mac: Light MAC address
            gateway_radio_mac: Gateway radio MAC that succeeded
            rssi: Signal strength (optional)
            version: Firmware version (optional)
            platform: Platform type 'esp32' or 'esp8266' (optional)
            model_id: Firmware variant ID (optional)
        """
        # Get gateway IP
        gateway_ip = self.get_gateway_ip_by_radio_mac(gateway_radio_mac)
        if not gateway_ip:
            logger.warning(f"Cannot update light gateway - {gateway_radio_mac} not found")
            return
        
        def update_func(strips):
            for strip in strips:
                if strip.get('mac_address', '').upper() == light_mac.upper():
                    strip['gateway_ip'] = gateway_ip
                    strip['last_gateway_mac'] = gateway_radio_mac.upper()
                    strip['last_seen'] = datetime.now().isoformat()
                    
                    # Update RSSI if provided
                    if rssi is not None:
                        strip['rssi'] = rssi
                    
                    # Update version if provided
                    if version:
                        strip['version'] = version
                    
                    # Update platform if provided
                    if platform:
                        strip['platform'] = platform
                    
                    # Update model_id if provided
                    if model_id is not None:
                        strip['model_id'] = model_id
                    
                    logger.debug(f"Updated {light_mac} gateway: {gateway_radio_mac} ({gateway_ip})")
                    break
            return strips
        
        data_manager.update_json(FILE_LIGHTSTRIPS, update_func)
    
    def get_light_gateway(self, light_mac: str) -> tuple[Optional[str], Optional[str]]:
        """Get lightstrip's last successful gateway.
        
        Args:
            light_mac: Light MAC address
            
        Returns:
            Tuple of (gateway_ip, gateway_radio_mac) or None if not found
        """
        strip = self.get_light_by_mac(light_mac)
        if not strip:
            return None, None
        
        gateway_ip = strip.get('gateway_ip')
        gateway_mac = strip.get('last_gateway_mac')
        
        if gateway_ip and gateway_mac:
            return gateway_ip, gateway_mac
        return None, None
    
    def add_lightstrip(self, mac_address: str, name: str, num_leds: int, 
                      is_rgbw: bool, room_id: Optional[str] = None, model_id: Optional[int] = None) -> Optional[Dict]:
        """Add new lightstrip to registry.
        
        Args:
            mac_address: Light MAC address
            name: Display name
            num_leds: Number of LEDs
            is_rgbw: Whether strip is RGBW or RGB
            room_id: Associated room ID
            model_id: Firmware variant ID (optional)
            
        Returns:
            Created lightstrip dict
        """
        def update_func(strips):
            # Check if already exists
            for strip in strips:
                if strip.get('mac_address', '').upper() == mac_address.upper():
                    return strips  # Already exists
            
            # Create new entry
            new_strip = {
                'id': uuid.uuid4().hex,
                'name': name,
                'mac_address': mac_address.upper(),
                'type': 'led_strip',
                'room_id': room_id,
                'single_color': False,
                'number_colors': num_leds,
                'color_type': 'rgbw' if is_rgbw else 'rgb',
                'overrides': {},
                'gateway_ip': None,
                'last_gateway_mac': None,
                'version': '0.0.0',
                'platform': None,
                'model_id': model_id,
                'rssi': None,
                'last_seen': None
            }
            strips.append(new_strip)
            
            return strips
        
        data_manager.update_json(FILE_LIGHTSTRIPS, update_func)
        return self.get_light_by_mac(mac_address)
    
    # ===== Button Management =====
    
    def get_button_by_mac(self, mac_address: str) -> Optional[Dict]:
        """Get button by MAC address.
        
        Args:
            mac_address: Button MAC address (with colons)
            
        Returns:
            Button dict or None if not found
        """
        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        for button in buttons:
            if button.get('mac_address', '').upper() == mac_address.upper():
                return button
        return None
    
    def get_all_buttons(self) -> List[Dict]:
        """Get all registered buttons/remotes.
        
        Returns:
            List of button dicts
        """
        return data_manager.read_json(FILE_BUTTONS, default=[])
    
    def update_button_tracking(self, button_mac: str, gateway_radio_mac: str, rssi: int, battery_mv: Optional[int] = None, version: Optional[str] = None, platform: Optional[str] = None, button_count: Optional[int] = None):
        """Update button tracking information.
        
        Args:
            button_mac: Button MAC address
            gateway_radio_mac: Gateway that received the signal
            rssi: Signal strength
            battery_mv: Battery voltage in millivolts (optional)
            version: Firmware version (optional)
            platform: Platform type 'esp32' or 'esp8266' (optional)
            button_count: Number of buttons for remote devices (optional)
        """
        def update_func(buttons):
            for button in buttons:
                if button.get('mac_address', '').upper() == button_mac.upper():
                    button['last_seen_gateway'] = gateway_radio_mac.upper()
                    button['rssi'] = rssi
                    button['last_seen'] = datetime.now().isoformat()
                    
                    # Update battery if provided
                    if battery_mv is not None:
                        button['battery_mv'] = battery_mv
                        button['battery_percent'] = self._calculate_battery_percent(battery_mv)
                        button['battery_last_updated'] = datetime.now().isoformat()
                    
                    # Update version if provided
                    if version:
                        button['version'] = version
                    
                    # Update platform if provided
                    if platform:
                        button['platform'] = platform
                    
                    # Update button_count for remote devices if provided
                    if button_count is not None and button.get('device_type') == DEV_REMOTE:
                        button['button_count'] = button_count
                    
                    break
            
            return buttons
        
        data_manager.update_json(FILE_BUTTONS, update_func)
    
    def _calculate_battery_percent(self, voltage_mv: int) -> Optional[int]:
        """Calculate battery percentage from voltage using Li-Ion curve.
        
        ESP32 requires minimum 3.3V for WiFi operation, so we use:
        - 4.2V (4200mV) = 100%
        - 3.3V (3300mV) = 0%
        
        Args:
            voltage_mv: Battery voltage in millivolts
            
        Returns:
            Battery percentage (0-100)
        """
        if voltage_mv == 0:
            return None

        curve = [
            (3000, 0),
            (3300, 5),
            (3400, 10),
            (3500, 20),
            (3600, 35),
            (3700, 50),
            (3800, 70),
            (3900, 80),
            (4000, 90),
            (4100, 95),
            (4200, 100),
        ]

        # Clamp extremes
        if voltage_mv <= curve[0][0]:
            return 0
        if voltage_mv >= curve[-1][0]:
            return 100

        # Find nearest points and linearly interpolate between them
        lower_v, lower_p = curve[0]
        for upper_v, upper_p in curve[1:]:
            if voltage_mv <= upper_v:
                # voltage is between lower_v and upper_v
                if upper_v == lower_v:
                    return int(upper_p)
                frac = (voltage_mv - lower_v) / (upper_v - lower_v)
                percent = lower_p + frac * (upper_p - lower_p)
                return max(0, min(100, int(round(percent))))
            lower_v, lower_p = upper_v, upper_p

        # Fallback
        return 0
    
    def add_button(self, mac_address: str, name: str, device_type: int = DEV_BUTTON, button_count: Optional[int] = None) -> Optional[Dict]:
        """Add new button/remote to registry.
        
        Args:
            mac_address: Device MAC address
            name: Display name
            device_type: DEV_BUTTON or DEV_REMOTE (default DEV_BUTTON)
            button_count: Number of buttons (2-4 for flexible buttons, None for default)
            
        Returns:
            Created device dict
        """
        def update_func(buttons):
            # Check if already exists
            for button in buttons:
                if button.get('mac_address', '').upper() == mac_address.upper():
                    return buttons  # Already exists
            
            # Create new entry
            new_button = {
                'id': uuid.uuid4().hex,
                'name': name,
                'mac_address': mac_address.upper(),
                'device_type': device_type,
                'configured': False,
                'config': {
                    'device_id': None,
                    'room_id': None,
                    'scenes': []
                },
                'last_seen_gateway': None,
                'rssi': None,
                'last_seen': None,
                'version': '0.0.0',
                'platform': None,
                'battery_mv': None,
                'battery_percent': None,
                'battery_last_updated': None
            }
            
            # Add button_count for remote devices
            if device_type == DEV_REMOTE:
                new_button['button_count'] = button_count if button_count is not None else 4
            
            buttons.append(new_button)
            
            return buttons
        
        data_manager.update_json(FILE_BUTTONS, update_func)
        return self.get_button_by_mac(mac_address)
    
    # ===== Motion Sensor Management =====
    
    def get_motion_sensor_by_mac(self, mac_address: str) -> Optional[Dict]:
        """Get motion sensor by MAC address.
        
        Args:
            mac_address: Motion sensor MAC address (with colons)
            
        Returns:
            Motion sensor dict or None if not found
        """
        sensors = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
        for sensor in sensors:
            if sensor.get('mac_address', '').upper() == mac_address.upper():
                return sensor
        return None
    
    def get_all_motion_sensors(self) -> List[Dict]:
        """Get all registered motion sensors.
        
        Returns:
            List of motion sensor dicts
        """
        return data_manager.read_json(FILE_MOTION_SENSORS, default=[])
    
    def update_motion_sensor_tracking(self, sensor_mac: str, gateway_radio_mac: str, rssi: int, 
                                     battery_mv: Optional[int] = None, light_level: Optional[int] = None,
                                     version: Optional[str] = None, platform: Optional[str] = None):
        """Update motion sensor tracking information.
        
        Args:
            sensor_mac: Motion sensor MAC address
            gateway_radio_mac: Gateway that received the signal
            rssi: Signal strength
            battery_mv: Battery voltage in millivolts (optional)
            light_level: LDR light level reading (optional)
            version: Firmware version (optional)
            platform: Platform type 'esp32' or 'esp8266' (optional)
        """
        def update_func(sensors):
            for sensor in sensors:
                if sensor.get('mac_address', '').upper() == sensor_mac.upper():
                    sensor['last_seen_gateway'] = gateway_radio_mac.upper()
                    sensor['rssi'] = rssi
                    sensor['last_seen'] = datetime.now().isoformat()
                    
                    # Update battery if provided
                    if battery_mv is not None:
                        sensor['battery_mv'] = battery_mv
                        sensor['battery_percent'] = self._calculate_battery_percent(battery_mv)
                        sensor['battery_last_updated'] = datetime.now().isoformat()
                    
                    # Update light level if provided
                    if light_level is not None:
                        sensor['light_level'] = light_level
                        sensor['light_last_updated'] = datetime.now().isoformat()
                    
                    # Update version if provided
                    if version:
                        sensor['version'] = version
                    
                    # Update platform if provided
                    if platform:
                        sensor['platform'] = platform
                    
                    break
            
            return sensors
        
        data_manager.update_json(FILE_MOTION_SENSORS, update_func)
    
    def add_motion_sensor(self, mac_address: str, name: str) -> Optional[Dict]:
        """Add new motion sensor to registry.
        
        Args:
            mac_address: Motion sensor MAC address
            name: Display name
            
        Returns:
            Created motion sensor dict
        """
        def update_func(sensors):
            # Check if already exists
            for sensor in sensors:
                if sensor.get('mac_address', '').upper() == mac_address.upper():
                    return sensors  # Already exists
            
            # Create new entry
            new_sensor = {
                'id': uuid.uuid4().hex,
                'name': name,
                'mac_address': mac_address.upper(),
                'configured': False,
                'config': {
                    'room_id': None,
                    'cooldown_seconds': 60,
                    'enabled': True
                },
                'last_seen_gateway': None,
                'rssi': None,
                'last_seen': None,
                'last_motion': None,
                'version': '0.0.0',
                'platform': None,
                'battery_mv': None,
                'battery_percent': None,
                'battery_last_updated': None,
                'light_level': None,
                'light_last_updated': None
            }
            sensors.append(new_sensor)
            
            return sensors
        
        data_manager.update_json(FILE_MOTION_SENSORS, update_func)
        return self.get_motion_sensor_by_mac(mac_address)
# Global singleton instance
device_manager = DeviceManager()
