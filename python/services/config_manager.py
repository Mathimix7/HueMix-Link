"""Configuration manager for application settings."""
import json
from pathlib import Path
from threading import RLock
from services.config_change_notifier import config_notifier
from constants import DEFAULT_UDP_PORT, DEFAULT_WEB_PORT, FILE_CONFIG

class ConfigManager:
    """Manages application configuration settings."""
    
    def __init__(self):
        self.config_file = Path(__file__).parent.parent / 'data' / FILE_CONFIG
        self.lock = RLock()
        self._ensure_config_file()
    
    def _ensure_config_file(self):
        """Ensure config file exists with default values."""
        if not self.config_file.exists():
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            default_config = {
                'udp_port': DEFAULT_UDP_PORT,
                'web_port': DEFAULT_WEB_PORT
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
                    'web_port': DEFAULT_WEB_PORT
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
    
    def get_web_port(self):
        """Get web server port."""
        config = self.load_config()
        return config.get('web_port', DEFAULT_WEB_PORT)
    
    def update_udp_port(self, port):
        """Update UDP server port."""
        config = self.load_config()
        old_port = config.get('udp_port')
        config['udp_port'] = port
        self.save_config(config)
        
        # Notify subscribers if port changed
        if old_port != port:
            config_notifier.notify_change('udp_port_changed', {'old_port': old_port, 'new_port': port})
    
    def update_web_port(self, port):
        """Update web server port."""
        config = self.load_config()
        config['web_port'] = port
        self.save_config(config)


# Singleton instance
config_manager = ConfigManager()
