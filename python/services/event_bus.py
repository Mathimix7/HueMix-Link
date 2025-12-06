"""Event bus for pub/sub communication between services."""
import queue
import threading
from typing import Callable, Dict, List, Any
import logging

logger = logging.getLogger(__name__)


class EventBus:
    """Simple pub/sub event bus for inter-service communication."""
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        
    def subscribe(self, event_type: str, callback: Callable[[Dict[str, Any]], None]):
        """
        Subscribe to an event type.
        
        Args:
            event_type: Type of event to listen for (e.g., 'scene_changed')
            callback: Function to call when event occurs. Receives event data dict.
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
            logger.info(f"Subscriber added for event: {event_type}")
    
    def unsubscribe(self, event_type: str, callback: Callable):
        """Unsubscribe from an event type."""
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logger.info(f"Subscriber removed for event: {event_type}")
                except ValueError:
                    pass
    
    def publish(self, event_type: str, data: Dict[str, Any]):
        """
        Publish an event to all subscribers.
        
        Args:
            event_type: Type of event (e.g., 'scene_changed')
            data: Event data to pass to subscribers
        """
        with self._lock:
            subscribers = self._subscribers.get(event_type, []).copy()
        
        logger.info(f"Publishing event: {event_type} to {len(subscribers)} subscribers")
        
        for callback in subscribers:
            try:
                callback(data)
            except Exception as e:
                logger.error(f"Error in event subscriber for {event_type}: {e}")


# Global event bus instance
event_bus = EventBus()
