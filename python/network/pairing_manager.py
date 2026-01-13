"""
Pairing manager for controlling device pairing windows.

Manages time-based pairing mode with device type filtering and RSSI validation.
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from network.device_manager import device_manager
from services.data_manager import data_manager
from constants import DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, FILE_PAIRING_HISTORY

logger = logging.getLogger(__name__)

class PairingManager:
    """Manages device pairing mode with time-based windows."""
    
    def __init__(self):
        """Initialize pairing manager."""
        self._pairing_active = False
        self._expiry_time: Optional[datetime] = None
        self._allowed_types: List[int] = []  # Empty = all types allowed
        self._lock = threading.RLock()
        
        # Track last 5 paired devices
        self._paired_devices: List[Dict] = []  # [{mac, type, name, paired_date, mode}]
        
        # Load paired devices history from file
        self._load_paired_devices()
        
        logger.info("PairingManager initialized")
    
    def start_pairing(self, duration: int = 60, device_types: Optional[List[str]] = None):
        """Start pairing mode for specified duration.
        
        Args:
            duration: Pairing window duration in seconds (default 60)
            device_types: List of device type strings ['gateway', 'button', 'light']
                         None or empty = all types allowed
        """
        with self._lock:
            self._pairing_active = True
            self._expiry_time = datetime.now() + timedelta(seconds=duration)
            
            # Convert device type strings to constants
            if device_types:
                type_map = {
                    'gateway': DEV_GATEWAY,
                    'button': DEV_BUTTON,
                    'light': DEV_LIGHT,
                    'remote': DEV_REMOTE
                }
                self._allowed_types = [type_map[t.lower()] for t in device_types if t.lower() in type_map]
            else:
                self._allowed_types = []  # All types allowed
            
            type_str = ', '.join(device_types) if device_types else 'all devices'
            logger.info(f"Pairing mode started for {duration}s ({type_str})")
    
    def stop_pairing(self):
        """Stop pairing mode immediately."""
        with self._lock:
            if self._pairing_active:
                self._pairing_active = False
                self._expiry_time = None
                logger.info("Pairing mode stopped")
    
    def is_pairing_allowed(self, device_mac: str, device_type: int, rssi: int = 0) -> bool:
        """Check if pairing is allowed for a device.
        
        Args:
            device_mac: Device MAC address
            device_type: DEV_GATEWAY/DEV_BUTTON/DEV_LIGHT
            rssi: Signal strength in dBm (for buttons/lights)
            
        Returns:
            True if device can be paired
        """
        with self._lock:
            # Check if pairing is active
            if not self._pairing_active:
                return False
            
            # Check if expired
            if self._expiry_time and datetime.now() > self._expiry_time:
                self._pairing_active = False
                logger.info("Pairing mode expired")
                return False
            
            # Check device type filter
            if self._allowed_types and device_type not in self._allowed_types:
                logger.debug(f"Device type {device_type} not allowed in current pairing mode")
                return False
            
            # In pairing mode, allow pairing regardless of signal strength
            logger.info(f"Device {device_mac} (type {device_type}) allowed to pair via long range mode (RSSI: {rssi} dBm)")
            return True
    
    def get_status(self) -> Dict:
        """Get current pairing mode status.
        
        Returns:
            Dict with pairing status:
            {
                'active': bool,
                'remaining_seconds': int or None,
                'allowed_types': list of type strings,
                'devices_found': list of device dicts
            }
        """
        with self._lock:
            remaining = None
            if self._pairing_active and self._expiry_time:
                delta = self._expiry_time - datetime.now()
                remaining = max(0, int(delta.total_seconds()))
                
                # Auto-expire if time is up
                if remaining == 0:
                    self._pairing_active = False
            
            # Convert type constants back to strings
            type_map = {
                DEV_GATEWAY: 'gateway',
                DEV_BUTTON: 'button',
                DEV_LIGHT: 'light'
            }
            allowed_type_names = [type_map.get(t, f'unknown({t})') for t in self._allowed_types]
            
            return {
                'active': self._pairing_active,
                'remaining_seconds': remaining,
                'allowed_types': allowed_type_names if allowed_type_names else ['all']
            }
    
    def record_device_paired(self, device_mac: str, device_type: int, device_name: str, mode: str = 'unknown'):
        """Record a device that was successfully paired.
        
        Maintains a list of the last 5 paired devices.
        
        Args:
            device_mac: Device MAC address
            device_type: DEV_GATEWAY/DEV_BUTTON/DEV_LIGHT
            device_name: Human-readable device name
            mode: 'short_range', 'long_range', or 'wifi' (for gateways)
        """
        with self._lock:
            # Create paired device record
            paired_device = {
                'mac': device_mac,
                'type': device_type,
                'name': device_name,
                'paired_date': datetime.now().isoformat(),
                'mode': mode
            }
            
            # Remove if already exists (update)
            self._paired_devices = [d for d in self._paired_devices if d['mac'].upper() != device_mac.upper()]
            
            # Add to front of list
            self._paired_devices.insert(0, paired_device)
            
            # Keep only last 5
            self._paired_devices = self._paired_devices[:5]
            
            # Save to file
            self._save_paired_devices()
            
            logger.info(f"Recorded paired device: {device_name} ({device_mac}, mode={mode})")
    
    def get_paired_devices(self) -> List[Dict]:
        """Get list of last 5 paired devices.
        
        Names and IDs are fetched from device_manager to show current info (in case they were renamed).
        
        Returns:
            List of paired device dicts [{mac, type, name, id, paired_date, mode}]
        """
        with self._lock:            
            # Create a copy with updated names and IDs
            updated_devices = []
            for device in self._paired_devices:
                device_copy = device.copy()
                
                # Try to get current name and ID from device_manager
                if device['type'] == DEV_GATEWAY:
                    gateway = device_manager.get_gateway_by_wifi_mac(device['mac'])
                    if gateway:
                        device_copy['name'] = gateway.get('name', device['name'])
                        device_copy['id'] = gateway.get('id')
                elif device['type'] == DEV_BUTTON or device['type'] == DEV_REMOTE:
                    button = device_manager.get_button_by_mac(device['mac'])
                    if button:
                        device_copy['name'] = button.get('name', device['name'])
                        device_copy['id'] = button.get('id')
                elif device['type'] == DEV_LIGHT:
                    light = device_manager.get_light_by_mac(device['mac'])
                    if light:
                        device_copy['name'] = light.get('name', device['name'])
                        device_copy['id'] = light.get('id')
                
                updated_devices.append(device_copy)
            
            return updated_devices
    
    def remove_paired_device(self, device_mac: str) -> bool:
        """Remove a device from the paired devices history.
        
        Args:
            device_mac: MAC address of device to remove
            
        Returns:
            True if device was found and removed, False otherwise
        """
        with self._lock:
            initial_count = len(self._paired_devices)
            self._paired_devices = [d for d in self._paired_devices if d['mac'].upper() != device_mac.upper()]
            
            if len(self._paired_devices) < initial_count:
                self._save_paired_devices()
                logger.info(f"Removed device {device_mac} from paired devices history")
                return True
            else:
                logger.warning(f"Device {device_mac} not found in paired devices history")
                return False
    
    def _load_paired_devices(self):
        """Load paired devices history from file."""
        try:            
            self._paired_devices = data_manager.read_json(FILE_PAIRING_HISTORY, default=[])
            # Keep only last 5
            self._paired_devices = self._paired_devices[:5]
            logger.info(f"Loaded {len(self._paired_devices)} paired devices from history")
        except Exception as e:
            logger.error(f"Error loading paired devices history: {e}")
            self._paired_devices = []
    
    def _save_paired_devices(self):
        """Save paired devices history to file."""
        try:            
            data_manager.write_json(FILE_PAIRING_HISTORY, self._paired_devices)
            logger.debug(f"Saved {len(self._paired_devices)} paired devices to history")
        except Exception as e:
            logger.error(f"Error saving paired devices history: {e}")


# Global singleton instance
pairing_manager = PairingManager()
