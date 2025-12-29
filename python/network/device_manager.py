"""
Device manager for tracking gateways, buttons, and lightstrips with routing intelligence.

Manages device registry in JSON files with delivery-based gateway failover logic.
"""
import logging
from typing import Optional, List, Dict
from datetime import datetime
from services.data_manager import data_manager
from constants import FILE_BUTTONS, FILE_GATEWAYS, FILE_LIGHTSTRIPS
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
    
    def update_gateway(self, wifi_mac: str, radio_mac: str, ip_address: str) -> Dict:
        """Update or create gateway entry.
        
        Args:
            wifi_mac: WiFi MAC address
            radio_mac: Radio MAC address for mesh routing
            ip_address: Current IP address
            
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
            else:
                # Create new gateway
                gateway = {
                    'id': uuid.uuid4().hex,
                    'name': f"Gateway {wifi_mac[-8:]}",
                    'mac_address': wifi_mac.upper(),
                    'radio_mac': radio_mac.upper(),
                    'ip_address': ip_address,
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
    
    def update_light_gateway(self, light_mac: str, gateway_radio_mac: str):
        """Update lightstrip's last successful gateway.
        
        Records the gateway that successfully delivered to this light.
        
        Args:
            light_mac: Light MAC address
            gateway_radio_mac: Gateway radio MAC that succeeded
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
                    logger.debug(f"Updated {light_mac} gateway: {gateway_radio_mac} ({gateway_ip})")
                    break
            return strips
        
        data_manager.update_json(FILE_LIGHTSTRIPS, update_func)
    
    def get_light_gateway(self, light_mac: str) -> Optional[tuple[str, str]]:
        """Get lightstrip's last successful gateway.
        
        Args:
            light_mac: Light MAC address
            
        Returns:
            Tuple of (gateway_ip, gateway_radio_mac) or (None, None) if not found
        """
        strip = self.get_light_by_mac(light_mac)
        if not strip:
            return None, None
        
        return strip.get('gateway_ip'), strip.get('last_gateway_mac')
    
    def add_lightstrip(self, mac_address: str, name: str, num_leds: int, 
                      is_rgbw: bool, room_id: Optional[str] = None) -> Dict:
        """Add new lightstrip to registry.
        
        Args:
            mac_address: Light MAC address
            name: Display name
            num_leds: Number of LEDs
            is_rgbw: Whether strip is RGBW or RGB
            room_id: Associated room ID
            
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
                'last_gateway_mac': None
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
    
    def update_button_tracking(self, button_mac: str, gateway_radio_mac: str, rssi: int):
        """Update button tracking information.
        
        Args:
            button_mac: Button MAC address
            gateway_radio_mac: Gateway that received the signal
            rssi: Signal strength
        """
        def update_func(buttons):
            for button in buttons:
                if button.get('mac_address', '').upper() == button_mac.upper():
                    button['last_seen_gateway'] = gateway_radio_mac.upper()
                    button['rssi'] = rssi
                    button['last_seen'] = datetime.now().isoformat()
                    break
            
            return buttons
        
        data_manager.update_json(FILE_BUTTONS, update_func)
    
    def add_button(self, mac_address: str, name: str) -> Dict:
        """Add new button to registry.
        
        Args:
            mac_address: Button MAC address
            name: Display name
            
        Returns:
            Created button dict
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
                'configured': False,
                'config': {
                    'device_id': None,
                    'room_id': None,
                    'scenes': []
                },
                'last_seen_gateway': None,
                'rssi': None,
                'last_seen': None
            }
            buttons.append(new_button)
            
            return buttons
        
        data_manager.update_json(FILE_BUTTONS, update_func)
        return self.get_button_by_mac(mac_address)


# Global singleton instance
device_manager = DeviceManager()
