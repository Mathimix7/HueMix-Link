"""Lightstrip controller that syncs colors with Hue scenes via EventStream API."""
import threading
import requests
import json
import logging
from typing import Dict, Optional, List
from sseclient import SSEClient  # pip install sseclient-py

from services import data_manager, event_bus

logger = logging.getLogger(__name__)


class LightStripController:
    """
    Controller that syncs lightstrip colors with Hue scenes.
    Listens to Hue EventStream API for scene changes and sends HTTP requests to lightstrips.
    """
    
    def __init__(self):
        self.bridge_ip: Optional[str] = None    
        self.api_token: Optional[str] = None
        self._running = False
        self._eventstream_thread = None
        self._lightstrip_configs: Dict[str, Dict] = {}  # strip_id -> config
        
    def start(self):
        """Start the lightstrip controller and EventStream listener."""
        if self._running:
            logger.warning("LightStripController already running")
            return
        
        # Load configuration
        self._load_bridge_config()
        self._load_lightstrip_configs()
        
        if not self.bridge_ip or not self.api_token:
            logger.warning("No Hue bridge config - lightstrip sync disabled")
            return
        
        # Subscribe to scene change events from TCP server
        event_bus.subscribe('scene_changed', self._on_scene_changed)
        
        # Start EventStream listener
        self._running = True
        self._eventstream_thread = threading.Thread(
            target=self._eventstream_worker,
            daemon=True
        )
        self._eventstream_thread.start()
        
        logger.info("LightStripController started")
    
    def stop(self):
        """Stop the lightstrip controller."""
        self._running = False
        
        if self._eventstream_thread:
            self._eventstream_thread.join(timeout=2)
        
        event_bus.unsubscribe('scene_changed', self._on_scene_changed)
        logger.info("LightStripController stopped")
    
    def _load_bridge_config(self):
        """Load Hue bridge configuration."""
        try:
            config = data_manager.read_json('bridge_config.json', default={})
            self.bridge_ip = config.get('bridge_ip')
            self.api_token = config.get('api_token')
        except Exception as e:
            logger.error(f"Failed to load bridge config: {e}")
    
    def _load_lightstrip_configs(self):
        """
        Load lightstrip configurations from JSON.
        Format: [
            {
                "id": "strip1",
                "name": "TV Backlight",
                "ip": "192.168.1.100",
                "room_id": "room1"
            }
        ]
        """
        try:
            strips = data_manager.read_json('lightstrips.json', default=[])
            self._lightstrip_configs.clear()
            
            for strip in strips:
                strip_id = strip.get('id')
                if strip_id:
                    self._lightstrip_configs[strip_id] = strip
            
            logger.info(f"Loaded {len(self._lightstrip_configs)} lightstrip configurations")
        except Exception as e:
            logger.error(f"Failed to load lightstrip configs: {e}")
    
    def _on_scene_changed(self, event_data: Dict):
        """
        Handle scene change event from TCP server.
        
        Args:
            event_data: Dict with scene_id, room_id, etc.
        """
        scene_id = event_data.get('scene_id')
        room_id = event_data.get('room_id')
        
        logger.info(f"Scene changed: {scene_id} in room {room_id}")
        
        # Find lightstrips for this room
        strips_for_room = [
            strip for strip in self._lightstrip_configs.values()
            if strip.get('room_id') == room_id
        ]
        
        if not strips_for_room:
            logger.debug(f"No lightstrips configured for room {room_id}")
            return
        
        # Get scene color info from Hue
        try:
            scene_colors = self._get_scene_colors(scene_id, room_id)
            
            if scene_colors:
                # Update each lightstrip
                for strip in strips_for_room:
                    self._update_lightstrip(strip, scene_colors)
        
        except Exception as e:
            logger.error(f"Error updating lightstrips for scene {scene_id}: {e}")
    
    def _get_scene_colors(self, scene_id: str, room_id: str) -> Optional[Dict]:
        """
        Get dominant colors from a Hue scene.
        
        Returns:
            Dict with color info (rgb, brightness, etc.) or None
        """
        try:
            # Get scene details
            url = f"https://{self.bridge_ip}/clip/v2/resource/scene/{scene_id}"
            headers = {"hue-application-key": self.api_token}
            
            response = requests.get(url, headers=headers, verify=False, timeout=5)
            response.raise_for_status()
            
            scene_data = response.json().get('data', [{}])[0]
            actions = scene_data.get('actions', [])
            
            if not actions:
                return None
            
            # Extract color from first light action
            first_action = actions[0]
            color_data = first_action.get('action', {}).get('color', {})
            brightness = first_action.get('action', {}).get('dimming', {}).get('brightness', 100)
            
            # Convert XY to RGB (simplified - you may want more accurate conversion)
            xy = color_data.get('xy', {})
            x, y = xy.get('x', 0.3), xy.get('y', 0.3)
            
            # Basic XY to RGB conversion
            z = 1.0 - x - y
            Y = brightness / 100.0
            X = (Y / y) * x if y > 0 else 0
            Z = (Y / y) * z if y > 0 else 0
            
            # Convert XYZ to RGB (simplified sRGB)
            r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
            g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
            b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
            
            # Clamp and scale
            r = max(0, min(1, r)) * 255
            g = max(0, min(1, g)) * 255
            b = max(0, min(1, b)) * 255
            
            return {
                'r': int(r),
                'g': int(g),
                'b': int(b),
                'brightness': int(brightness)
            }
        
        except Exception as e:
            logger.error(f"Failed to get scene colors: {e}")
            return None
    
    def _update_lightstrip(self, strip: Dict, colors: Dict):
        """
        Send HTTP request to lightstrip to update its color.
        
        Args:
            strip: Lightstrip config dict
            colors: Color data dict (r, g, b, brightness)
        """
        try:
            strip_ip = strip.get('ip')
            if not strip_ip:
                logger.warning(f"No IP configured for lightstrip {strip.get('id')}")
                return
            
            # Send HTTP request to lightstrip
            # Adjust endpoint and payload format based on your lightstrip API
            url = f"http://{strip_ip}/api/color"
            payload = {
                'r': colors['r'],
                'g': colors['g'],
                'b': colors['b'],
                'brightness': colors['brightness']
            }
            
            response = requests.post(url, json=payload, timeout=2)
            response.raise_for_status()
            
            logger.info(f"Updated lightstrip {strip.get('name')} to RGB({colors['r']}, {colors['g']}, {colors['b']})")
        
        except Exception as e:
            logger.error(f"Failed to update lightstrip {strip.get('id')}: {e}")
    
    def _eventstream_worker(self):
        """
        Worker that listens to Hue EventStream API for real-time scene changes.
        This provides an alternative/backup to the event_bus for scene changes.
        """
        logger.info("EventStream worker started")
        
        url = f"https://{self.bridge_ip}/eventstream/clip/v2"
        headers = {
            "hue-application-key": self.api_token,
            "Accept": "text/event-stream"
        }
        
        while self._running:
            try:
                # Connect to SSE stream
                messages = SSEClient(url, headers=headers, verify=False)
                
                for msg in messages:
                    if not self._running:
                        break
                    
                    if msg.data:
                        try:
                            events = json.loads(msg.data)
                            
                            for event in events:
                                # Check for scene activation events
                                if event.get('type') == 'update':
                                    for item in event.get('data', []):
                                        if item.get('type') == 'scene':
                                            # Scene was updated/activated
                                            scene_id = item.get('id')
                                            status = item.get('status', {})
                                            
                                            if status.get('active') == 'active':
                                                logger.info(f"EventStream: Scene {scene_id} activated")
                                                # Could trigger lightstrip update here too
                        
                        except json.JSONDecodeError:
                            pass
            
            except Exception as e:
                if self._running:
                    logger.error(f"EventStream error: {e}")
                    logger.info("Reconnecting in 5 seconds...")
                    threading.Event().wait(5)
        
        logger.info("EventStream worker stopped")


# Global instance
lightstrip_controller = LightStripController()
