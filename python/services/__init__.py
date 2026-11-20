"""
Services Package - Application-wide singleton services.

This package contains services that manage application state:

- DataManager: Thread-safe JSON file read/write operations
- ConfigManager: Application configuration management
- ConfigChangeNotifier: Notifies when web UI changes configs
- HueService: Manages Hue controller lifecycle
- HueStateManager: Manages Hue bridge state
- HueSSEListener: Listens to Hue bridge SSE events
- AutomationService: Manages automation engine lifecycle
- PluginManager: Loads optional plugins and their lifecycle hooks

These are initialized as singletons and used throughout the application.
"""

from .data_manager import DataManager, data_manager
from .config_change_notifier import ConfigChangeNotifier, config_notifier
from .config_manager import ConfigManager, config_manager
from .hue_service import HueService, hue_service
from .hue_state_manager import HueStateManager, hue_state_manager
from .hue_sse_listener import HueSSEListener, hue_sse_listener
from .automation_service import AutomationService, automation_service
from .plugin_manager import PluginManager, plugin_manager

# Export both classes and singleton instances
__all__ = [
    'DataManager', 'data_manager',
    'ConfigChangeNotifier', 'config_notifier',
    'ConfigManager', 'config_manager',
    'HueService', 'hue_service',
    'HueStateManager', 'hue_state_manager',
    'HueSSEListener', 'hue_sse_listener',
    'AutomationService', 'automation_service',
    'PluginManager', 'plugin_manager',
]
