"""
Config Change Notifier - Notifies services when configurations change.

When the web UI changes button configs, server configs, or bridge settings,
this notifier broadcasts those updates to all subscribed services.

Example flow:
1. User changes button config in web UI
2. Web blueprint calls: config_notifier.notify_change('button_config', {...})
3. All subscribed services receive the notification
4. Services can react accordingly (e.g., reinitialize)
"""
import threading
from typing import Dict, Any, List, Callable
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ConfigChangeNotifier:
    """Notifies services of configuration changes via callback subscriptions."""
    
    def __init__(self):
        """Initialize the configuration change notifier."""
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
    
    def subscribe(self, change_type: str, callback: Callable[[Dict[str, Any]], None]):
        """Subscribe to configuration changes of a specific type.
        
        Args:
            change_type: Type of change to subscribe to (e.g., 'bridge_config', 'button_config')
            callback: Function to call when change occurs, receives notification dict
            
        Example:
            def on_bridge_change(notification):
                print(f"Bridge config changed: {notification['data']}")
            
            config_notifier.subscribe('bridge_config', on_bridge_change)
        """
        with self._lock:
            if change_type not in self._subscribers:
                self._subscribers[change_type] = []
            self._subscribers[change_type].append(callback)
            logger.debug(f"Subscriber added for '{change_type}' changes")
    
    def unsubscribe(self, change_type: str, callback: Callable[[Dict[str, Any]], None]):
        """Unsubscribe from configuration changes.
        
        Args:
            change_type: Type of change to unsubscribe from
            callback: The callback function to remove
        """
        with self._lock:
            if change_type in self._subscribers:
                try:
                    self._subscribers[change_type].remove(callback)
                    logger.debug(f"Subscriber removed for '{change_type}' changes")
                except ValueError:
                    pass
    
    def notify_change(self, change_type: str, data: Dict[str, Any]):
        """Notify all subscribers about a configuration change.
        
        Args:
            change_type: Type of change (e.g., 'bridge_config', 'button_config', 'server_added')
            data: Change data to send to subscribers
            
        Example:
            config_notifier.notify_change('bridge_config', {
                'ip': '192.168.1.100',
                'username': 'abc123'
            })
        """
        notification = {
            'type': change_type,
            'data': data,
            'timestamp': datetime.now().isoformat()
        }
        
        with self._lock:
            subscribers = self._subscribers.get(change_type, []).copy()
        
        # Call subscribers outside the lock to avoid deadlocks
        for callback in subscribers:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Error in subscriber callback for '{change_type}': {e}", exc_info=True)
        
        if subscribers:
            logger.debug(f"Notified {len(subscribers)} subscribers of '{change_type}' change")


# Global singleton instance - use this everywhere
config_notifier = ConfigChangeNotifier()
