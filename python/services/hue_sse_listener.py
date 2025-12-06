"""Service for listening to Philips Hue Bridge Server-Sent Events (SSE)."""
import logging
import requests
import json
import threading
import time
import urllib3
from typing import Optional
from services import data_manager
from services.hue_state_manager import hue_state_manager

# Disable SSL warnings for Hue Bridge self-signed certificate
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class HueSSEListener:
    """
    Listens to Philips Hue Bridge EventStream API for real-time updates.
    Updates HueStateManager with light, room, and scene changes.
    """
    
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._hue_controller = None  # Will be set during initialization
        
        logger.info("HueSSEListener initialized")
    
    def start(self, hue_controller=None):
        """
        Start listening to SSE events.
        
        Args:
            hue_controller: Optional HueController instance for fetching scene data
        """
        if self._running:
            logger.warning("HueSSEListener already running")
            return
        
        self._hue_controller = hue_controller
        self._running = True
        self._stop_event.clear()
        
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        
        logger.info("HueSSEListener started")
    
    def stop(self):
        """Stop listening to SSE events."""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=5)
        
        logger.info("HueSSEListener stopped")
    
    def _listen_loop(self):
        """Main loop for SSE connection with auto-reconnect."""
        retry_delay = 1
        max_retry_delay = 60
        
        while self._running:
            try:
                # Get bridge config
                config = data_manager.read_json('bridge.json', default={})
                if not config:
                    logger.warning("Bridge not configured, retrying in 10s...")
                    time.sleep(10)
                    continue
                
                ip = config.get('ip')
                username = config.get('username')
                
                if not ip or not username:
                    logger.error("Bridge IP or username missing")
                    time.sleep(10)
                    continue
                
                # Connect to SSE endpoint
                url = f"https://{ip}/eventstream/clip/v2"
                headers = {
                    'hue-application-key': username,
                    'Accept': 'text/event-stream'  # Request SSE format
                }
                
                logger.info(f"Connecting to Hue SSE at {ip}...")
                
                # Stream events
                response = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    verify=False,  # Hue uses self-signed cert
                    timeout=30
                )
                
                if response.status_code != 200:
                    logger.error(f"SSE connection failed: {response.status_code} - {response.text}")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
                    continue
                
                # Log reconnection success if this was a retry
                if retry_delay > 1:
                    logger.info(f"SSE connection re-established (status={response.status_code})")
                else:
                    logger.info(f"SSE connection established (status={response.status_code})")
                retry_delay = 1  # Reset retry delay on success
                
                try:
                    # Read lines manually - Hue SSE seems to need raw byte reading
                    buffer = b""
                    chunk_count = 0
                    for chunk in response.iter_content(chunk_size=1024):
                        chunk_count += 1
                        if chunk_count == 1:
                            logger.info(f"First chunk received: {len(chunk)} bytes")
                        
                        if not self._running or self._stop_event.is_set():
                            logger.info("SSE listener stopping...")
                            break
                        
                        if not chunk:
                            logger.debug("Empty chunk received")
                            continue
                            
                        buffer += chunk
                        
                        # Process complete lines
                        while b'\n' in buffer:
                            line_bytes, buffer = buffer.split(b'\n', 1)
                            try:
                                line = line_bytes.decode('utf-8').strip()
                                
                                if not line:
                                    continue
                                
                                # SSE format: "data: {...}" or ": keep-alive"
                                if line.startswith('data: '):
                                    json_data = line[6:]  # Remove "data: " prefix
                                    self._process_event(json_data)
                                elif line.startswith(':'):
                                    # Keep-alive comment
                                    pass
                                else:
                                    # Event metadata like "id: ..."
                                    pass
                            
                            except Exception as e:
                                logger.error(f"Error processing SSE line: {e}", exc_info=True)
                    
                    logger.info(f"SSE event loop ended (processed {chunk_count} chunks)")
                
                finally:
                    response.close()
            
            except requests.exceptions.RequestException as e:
                if self._running:
                    # Timeouts are normal for long-lived SSE connections
                    log_func = logger.debug if "timed out" in str(e).lower() else logger.warning
                    log_func(f"SSE connection lost: {e}, reconnecting in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
            
            except Exception as e:
                logger.error(f"Unexpected error in SSE listener: {e}", exc_info=True)
                if self._running:
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
    
    def _process_event(self, json_data: str):
        """
        Process a single SSE event.
        
        Event structure from Hue Bridge:
        [
            {
                "creationtime": "2024-01-01T12:00:00Z",
                "data": [
                    {
                        "id": "...",
                        "type": "light" | "grouped_light" | "scene",
                        "owner": {...},
                        ...
                    }
                ]
            }
        ]
        """
        try:
            event = json.loads(json_data)
            
            # Event is a list of objects with "data" fields
            if isinstance(event, list):
                for event_obj in event:
                    if isinstance(event_obj, dict) and 'data' in event_obj:
                        data_items = event_obj.get('data', [])
                        for item in data_items:
                            self._process_item(item)
            else:
                # Fallback for unexpected structure
                logger.warning(f"Unexpected SSE event structure: {type(event)}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse SSE event JSON: {e}")
        except Exception as e:
            logger.error(f"Error handling SSE event: {e}", exc_info=True)
    
    def _process_item(self, item: dict):
        """Process a single data item from SSE event."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            logger.info(f"SSE event received: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_update(item_id, item)
            
            elif item_type == 'grouped_light':
                self._handle_grouped_light_update(item_id, item)
            
            elif item_type == 'scene':
                self._handle_scene_update(item_id, item)
            
            else:
                # Ignore other types (button, device, etc.)
                logger.debug(f"Ignoring SSE event type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing item {item.get('id')}: {e}", exc_info=True)

    
    def _handle_light_update(self, light_id: str, data: dict):
        """Handle light state update from SSE."""
        try:
            # Extract light state - only include fields that are present
            is_on = None
            brightness = None
            color = None
            
            on_data = data.get('on')
            if on_data is not None:
                is_on = on_data.get('on')
            
            dimming_data = data.get('dimming')
            if dimming_data is not None:
                brightness = dimming_data.get('brightness')
            
            color_data = data.get('color')
            if color_data is not None:
                color_xy = color_data.get('xy')
                if color_xy:
                    x = color_xy.get('x', 0)
                    y = color_xy.get('y', 0)
                    # Simple conversion (could be improved)
                    r = int(x * 255)
                    g = int(y * 255)
                    b = int((1 - x - y) * 255)
                    color = {'r': max(0, min(255, r)), 'g': max(0, min(255, g)), 'b': max(0, min(255, b))}
            
            # Only update if we have at least one field
            if is_on is not None or brightness is not None or color is not None:
                hue_state_manager.update_light(
                    light_id=light_id,
                    is_on=is_on,
                    brightness=brightness,
                    color=color
                )
        
        except Exception as e:
            logger.error(f"Error processing light update {light_id}: {e}", exc_info=True)
    
    def _handle_grouped_light_update(self, grouped_light_id: str, data: dict):
        """Handle room/zone state update from SSE."""
        try:
            # Get the actual room ID from the grouped_light ID
            room_id = hue_state_manager.get_room_id_from_grouped_light(grouped_light_id)
            
            if room_id is None:
                logger.warning(f"Received grouped_light update for unknown ID: {grouped_light_id}")
                return
            
            # Extract room state - only include fields that are present
            is_on = None
            brightness = None
            
            on_data = data.get('on')
            if on_data is not None:
                is_on = on_data.get('on')
            
            dimming_data = data.get('dimming')
            if dimming_data is not None:
                brightness = dimming_data.get('brightness')
            
            # Only update if we have at least one field
            if is_on is not None or brightness is not None:
                hue_state_manager.update_room(
                    room_id=room_id,
                    is_on=is_on,
                    brightness=brightness
                )
        
        except Exception as e:
            logger.error(f"Error processing grouped_light update {grouped_light_id}: {e}", exc_info=True)
    
    def _handle_scene_update(self, scene_id: str, data: dict):
        """Handle scene activation from SSE."""
        try:
            # Check if scene is becoming active or inactive
            status = data.get('status', {})
            active_status = status.get('active')
            
            logger.info(f"Scene {scene_id} status: {active_status}")
            
            # Handle scene becoming inactive
            if active_status == 'inactive':
                # Only clear the scene if this is the currently active scene in its room
                scene_info = hue_state_manager.get_scene_info(scene_id)
                if scene_info:
                    room_id = scene_info.get('room_id')
                    if room_id:
                        room_state = hue_state_manager.get_room_state(room_id)
                        if room_state and room_state.get('current_scene_id') == scene_id:
                            # This scene is currently active in the room, so clear it
                            hue_state_manager.set_room_scene(room_id, None)
                            logger.info(f"Cleared scene {scene_id} from room {room_id} (became inactive)")
                return
                            
            # First, try to get room from stored scene info
            scene_info = hue_state_manager.get_scene_info(scene_id)
            room_id = None
            
            if scene_info:
                room_id = scene_info.get('room_id')
                logger.debug(f"Found room_id {room_id} from scene registry")
            
            # If not found in registry, try to fetch from bridge
            if not room_id and self._hue_controller:
                logger.info(f"Scene {scene_id} not in registry, fetching from bridge...")
                try:
                    scenes_data = self._hue_controller.get_scenes()
                    for scene in scenes_data:
                        if scene.get('id') == scene_id:
                            scene_name = scene.get('metadata', {}).get('name', 'Unknown')
                            group_info = scene.get('group', {})
                            room_id = group_info.get('rid')
                            
                            # Register this scene for future use
                            hue_state_manager.register_scene(scene_id, {
                                'name': scene_name,
                                'room_id': room_id
                            })
                            logger.info(f"Registered scene {scene_id} ({scene_name}) for room {room_id}")
                            break
                except Exception as e:
                    logger.error(f"Failed to fetch scene data from bridge: {e}")
            
            # If we have a room_id, update the room's active scene
            if room_id:
                hue_state_manager.set_room_scene(
                    room_id=room_id,
                    scene_id=scene_id
                )
                logger.info(f"Set scene {scene_id} as active for room {room_id}")
            else:
                logger.warning(f"Could not determine room for scene {scene_id}")
        
        except Exception as e:
            logger.error(f"Error processing scene update {scene_id}: {e}", exc_info=True)


# Global singleton instance
hue_sse_listener = HueSSEListener()
