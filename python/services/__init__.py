"""
Services Package - Application-wide singleton services.

This package contains services that manage application state and inter-component communication:

- DataManager: Thread-safe JSON file read/write operations
- ConfigChangeNotifier: Notifies TCP server when web UI changes configs
- ButtonStateManager: Tracks which scene each button is currently on (with timeout-based reset)
- EventBus: Pub/sub system for decoupled communication between components

These are initialized as singletons and used throughout the application.
"""

from .data_manager import DataManager, data_manager
from .config_change_notifier import ConfigChangeNotifier, config_notifier
from .button_state_manager import ButtonStateManager, button_state_manager
from .event_bus import EventBus, event_bus
from .config_manager import ConfigManager, config_manager

# Export both classes and singleton instances
__all__ = [
    'DataManager', 'data_manager',
    'ConfigChangeNotifier', 'config_notifier',
    'ButtonStateManager', 'button_state_manager',
    'EventBus', 'event_bus',
    'ConfigManager', 'config_manager'
]
