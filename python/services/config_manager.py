"""Configuration manager for application settings."""
import json
from pathlib import Path
from threading import RLock
from services.config_change_notifier import config_notifier
from services.data_manager import data_manager
from constants import DEFAULT_UDP_PORT, FILE_CONFIG, FILE_GATEWAYS
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages application configuration settings."""
    
    def __init__(self):
        self.config_file = Path(__file__).parent.parent / 'data' / FILE_CONFIG
        self.lock = RLock()
        self._ensure_config_file()
    
    def _ensure_config_file(self):
        """Ensure config file exists with default values."""
        if not self.config_file.exists():
            logger.info("Config file not found, creating default config...")
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            default_config = {
                'udp_port': DEFAULT_UDP_PORT,
                'dev_mode': False,
                'serial_gateway_enabled': False,
                'serial_gateway_port': '',
                'serial_gateway_baudrate': 460800,
            }
            self.save_config(default_config)
    
    def load_config(self):
        """Load configuration from file."""
        with self.lock:
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                # Return defaults if file is corrupted or missing
                return {
                    'udp_port': DEFAULT_UDP_PORT,
                    'dev_mode': False,
                    'serial_gateway_enabled': False,
                    'serial_gateway_port': '',
                    'serial_gateway_baudrate': 460800,
                }
    
    def save_config(self, config):
        """Save configuration to file."""
        with self.lock:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
    
    def get_udp_port(self):
        """Get UDP server port."""
        config = self.load_config()
        return config.get('udp_port', DEFAULT_UDP_PORT)
    
    def update_udp_port(self, port):
        """Update UDP server port."""
        config = self.load_config()
        old_port = config.get('udp_port')
        config['udp_port'] = port
        self.save_config(config)
        
        # Notify subscribers if port changed
        if old_port != port:
            config_notifier.notify_change('udp_port_changed', {'old_port': old_port, 'new_port': port})
    
    def get_dev_mode(self):
        """Get dev mode setting."""
        config = self.load_config()
        return config.get('dev_mode', False)
    
    def set_dev_mode(self, enabled):
        """Set dev mode setting."""
        config = self.load_config()
        config['dev_mode'] = enabled
        self.save_config(config)

    def get_serial_gateway_config(self):
        """Get USB serial gateway settings."""
        config = self.load_config()
        return {
            'enabled': bool(config.get('serial_gateway_enabled', False)),
            'port': str(config.get('serial_gateway_port', '')).strip(),
            'baudrate': int(config.get('serial_gateway_baudrate', 460800) or 460800),
        }

    def set_serial_gateway_config(self, enabled: bool, port: str, baudrate: int = 460800):
        """Persist USB serial gateway settings."""
        config = self.load_config()
        old_cfg = {
            'enabled': bool(config.get('serial_gateway_enabled', False)),
            'port': str(config.get('serial_gateway_port', '')).strip(),
            'baudrate': int(config.get('serial_gateway_baudrate', 460800) or 460800),
        }

        config['serial_gateway_enabled'] = bool(enabled)
        config['serial_gateway_port'] = (port or '').strip()
        config['serial_gateway_baudrate'] = int(baudrate or 460800)
        self.save_config(config)

        removed_serial_gateways = []
        if not enabled:
            removed_serial_gateways = self._remove_serial_gateways_from_registry()

        config_notifier.notify_change('serial_gateway_config_changed', {
            'old': old_cfg,
            'new': self.get_serial_gateway_config(),
            'removed_serial_gateways': removed_serial_gateways,
        })

    @staticmethod
    def _is_serial_gateway(gateway: dict) -> bool:
        endpoint = gateway.get('transport_endpoint') or gateway.get('ip_address')
        return (gateway.get('transport') == 'usb_serial') or (isinstance(endpoint, str) and endpoint.startswith('serial://'))

    def _remove_serial_gateways_from_registry(self):
        """Remove serial gateway entries from gateways.json and return removed entries."""
        removed_gateways = []

        def update_gateways(gateways):
            nonlocal removed_gateways
            gateways = gateways if isinstance(gateways, list) else []
            removed_gateways = [gw for gw in gateways if isinstance(gw, dict) and self._is_serial_gateway(gw)]
            return [gw for gw in gateways if not (isinstance(gw, dict) and self._is_serial_gateway(gw))]

        data_manager.update_json(FILE_GATEWAYS, update_gateways)
        return removed_gateways

# Singleton instance
config_manager = ConfigManager()
