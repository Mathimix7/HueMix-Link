"""Manages scene cycling state with automatic timeout reset."""
import threading
import time
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ButtonStateManager:
    """
    Manages per-button scene cycling state with timeout-based reset.
    When a button hasn't been pressed for a configured timeout, its index resets to 0.
    """
    
    def __init__(self, timeout_seconds: float = 3.0):
        """
        Initialize scene state manager.
        
        Args:
            timeout_seconds: Seconds of inactivity before resetting scene index
        """
        self.timeout_seconds = timeout_seconds

        self._state: Dict[str, Dict] = {}  # device_id -> {index, last_press, scenes}
        self._lock = threading.Lock()

    def load_config(self, config: Dict[str, Dict]):
        """
        Load initial button configurations.
        
        Args:
            config: Mapping of device_id to configuration dicts with 'scenes' list
        """
        with self._lock:
            for device_id, conf in config.items():
                scenes = conf.get('scenes', [])
                self._state[device_id] = {
                    'index': 0,
                    'last_press': 0,
                    'scenes': scenes
                }
            logger.info("ButtonStateManager configuration loaded")
    
    def get_next_scene(self, device_id: str) -> Optional[str]:
        """
        Get the next scene for a button press and update state.
        
        Args:
            device_id: Button device ID
            scenes: List of scene IDs configured for this button
            
        Returns:
            Next scene ID to activate, or None if no scenes configured
        """

        with self._lock:
            now = time.time()
            
            if device_id not in self._state or not self._state[device_id]["scenes"]:
                return None  # No scenes configured
           
            state = self._state[device_id]
            
            # Check if timeout expired - reset to beginning
            if now - state['last_press'] > self.timeout_seconds:
                logger.debug(f"Button {device_id} timeout expired, resetting to scene 0")
                state['index'] = 0
            else:
                # Advance to next index
                state['index'] = (state['index'] + 1) % len(state['scenes'])
                
            state['last_press'] = now
            
            current_index = state['index']
            scenes = state['scenes']
            scene_id = scenes[current_index]
            
            logger.info(f"Button {device_id}: scene {current_index+1}/{len(scenes)} -> {scene_id}")
            return scene_id
    
    def update_button(self, device_id: str, scenes: list):
        """
        Add a button or update the scene list for a button.
        
        Args:
            device_id: Button device ID
            scenes: New list of scene IDs
        """
        with self._lock:
            if not device_id in self._state:
                self._state[device_id] = {
                    'index': 0,
                    'last_press': 0,
                    'scenes': scenes
                }
            else:
                self._state[device_id]['scenes'] = scenes
                # Reset index if it's now out of bounds
                if self._state[device_id]['index'] >= len(scenes):
                    self._state[device_id]['index'] = 0
    
            logger.info(f"Updated scenes for button {device_id}")
    
    def reset_button(self, device_id: str):
        """Reset a button's state to index 0."""
        with self._lock:
            if device_id in self._state:
                self._state[device_id]['index'] = 0
                logger.info(f"Reset button {device_id} to scene 0")
    
    def is_timeout_expired(self, device_id: str) -> bool:
        """
        Check if the timeout has expired for a button (without modifying state).
        
        Args:
            device_id: Button device ID
            
        Returns:
            True if timeout expired (next press would reset), False otherwise
        """
        with self._lock:
            if device_id not in self._state:
                return True  # No state means first press or expired
            
            now = time.time()
            state = self._state[device_id]
            return (now - state['last_press']) > self.timeout_seconds
    
    def mark_press(self, device_id: str):
        """
        Mark that a button was pressed (update last_press time without changing index).
        Used when toggling lights on/off instead of cycling scenes.
        
        Args:
            device_id: Button device ID
        """
        with self._lock:
            if device_id not in self._state:
                # Initialize if not exists
                self._state[device_id] = {
                    'index': 0,
                    'last_press': time.time(),
                    'scenes': []
                }
            else:
                self._state[device_id]['last_press'] = time.time()
            
            logger.debug(f"Marked press time for button {device_id}")


# Global instance
button_state_manager = ButtonStateManager(timeout_seconds=3.0)
