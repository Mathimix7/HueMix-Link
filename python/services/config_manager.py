"""Configuration manager for application settings."""
import json
from pathlib import Path
from threading import RLock


class ConfigManager:
    """Manages application configuration settings."""
    
    def __init__(self):
        self.config_file = Path(__file__).parent.parent / 'data' / 'config.json'
        self.lock = RLock()
        self._ensure_config_file()
    
    def _ensure_config_file(self):
        """Ensure config file exists with default values."""
        if not self.config_file.exists():
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            default_config = {
                'tcp_port': 5555,
                'web_port': 5001
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
                    'tcp_port': 5555,
                    'web_port': 5001
                }
    
    def save_config(self, config):
        """Save configuration to file."""
        with self.lock:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
    
    def get_tcp_port(self):
        """Get TCP server port."""
        config = self.load_config()
        return config.get('tcp_port', 5555)
    
    def get_web_port(self):
        """Get web server port."""
        config = self.load_config()
        return config.get('web_port', 5001)
    
    def update_tcp_port(self, port):
        """Update TCP server port."""
        config = self.load_config()
        config['tcp_port'] = port
        self.save_config(config)
    
    def update_web_port(self, port):
        """Update web server port."""
        config = self.load_config()
        config['web_port'] = port
        self.save_config(config)


# Singleton instance
config_manager = ConfigManager()
