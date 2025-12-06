"""
Config Change Notifier - Notifies TCP server when configurations change.

When the web UI changes button configs, server configs, or bridge settings,
this notifier queues those updates so the TCP server can push them to connected ESP32 devices.

Example flow:
1. User changes button config in web UI
2. Web blueprint calls: config_notifier.notify_change('button_config', {...})
3. TCP server polls and gets the update
4. TCP server pushes new config to ESP32 receiver
"""
import queue
import threading
from typing import Dict, Any, Optional
from datetime import datetime


class ConfigChangeNotifier:
    """Notifies TCP server of configuration changes from the web UI."""
    
    def __init__(self):
        """Initialize the configuration change notifier."""
        self._queue = queue.Queue()
        self._lock = threading.Lock()
    
    def notify_change(self, change_type: str, data: Dict[str, Any]):
        """Notify about a configuration change.
        
        Args:
            change_type: Type of change (e.g., 'button_config', 'button_rename', 'server_added')
            data: Change data to send to TCP server
            
        Example:
            config_notifier.notify_change('button_config', {
                'device_id': 'abc123',
                'room_id': '1',
                'scenes': ['scene1', 'scene2']
            })
        """
        notification = {
            'type': change_type,
            'data': data,
            'timestamp': datetime.now().isoformat()
        }
        
        with self._lock:
            self._queue.put(notification)
    
    def get_change(self, block: bool = False, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Get the next configuration change notification.
        
        Args:
            block: Whether to block until a notification is available
            timeout: Timeout in seconds (only used if block=True)
            
        Returns:
            Notification dictionary or None if queue is empty
        """
        try:
            return self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None
    
    def get_all_changes(self) -> list:
        """Get all pending configuration changes.
        
        Returns:
            List of all pending notifications
        """
        changes = []
        with self._lock:
            while not self._queue.empty():
                try:
                    changes.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        return changes
    
    def pending_count(self) -> int:
        """Get the number of pending configuration changes.
        
        Returns:
            Number of notifications in queue
        """
        return self._queue.qsize()
    
    def clear(self):
        """Clear all pending configuration changes."""
        with self._lock:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break


# Global singleton instance - use this everywhere
config_notifier = ConfigChangeNotifier()
