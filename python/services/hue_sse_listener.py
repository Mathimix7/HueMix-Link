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
from constants import FILE_BRIDGE

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
                config = data_manager.read_json(FILE_BRIDGE, default={})
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
                
                logger.debug(f"Connecting to Hue SSE at {ip}...")
                
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
                    logger.debug(f"SSE connection re-established (status={response.status_code})")
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
                            logger.debug(f"First chunk received: {len(chunk)} bytes")
                        
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
                        event_type = event_obj.get('type', 'unknown')
                        logger.debug(f"Processing SSE event type: {event_type} with {len(data_items)} items")
                        if event_type == 'update':
                            for item in data_items:
                                self._process_update(item)
                        elif event_type == 'add':
                            for item in data_items:
                                self._process_add(item)
                        elif event_type == 'delete':
                            for item in data_items:
                                self._process_delete(item)
                        elif event_type == 'error':
                            logger.error(f"Hue SSE error event: {data_items}")
            else:
                # Fallback for unexpected structure
                logger.warning(f"Unexpected SSE event structure: {type(event)}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse SSE event JSON: {e}")
        except Exception as e:
            logger.error(f"Error handling SSE event: {e}", exc_info=True)
    
    def _process_update(self, item: dict):
        """Process a single data item from SSE event."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            logger.debug(f"SSE event received: type={item_type}, id={item_id}")

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
    
    def _process_add(self, item: dict):
        """Process add events for new resources."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            logger.info(f"Addition event: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_update(item_id, item)
            
            elif item_type == 'grouped_light':
                self._handle_grouped_light_update(item_id, item)
            
            elif item_type == 'scene':
                self._handle_scene_addition(item_id, item)
            
            elif item_type == 'room':
                self._handle_room_addition(item_id, item)
            
            else:
                logger.debug(f"Ignoring addition of type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing addition for {item.get('type')} {item.get('id')}: {e}", exc_info=True)
    
    def _process_delete(self, item: dict):
        """Process deletion events for any resource type."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')
            
            logger.debug(f"Deletion event: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_deletion(item_id)
            elif item_type == 'grouped_light':
                self._handle_grouped_light_deletion(item_id)
            elif item_type == 'scene':
                self._handle_scene_deletion(item_id)
            elif item_type == 'room':
                self._handle_room_deletion(item_id)
            else:
                logger.debug(f"Ignoring deletion of type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing deletion for {item_type} {item_id}: {e}", exc_info=True)
    
    def _handle_scene_deletion(self, scene_id: str):
        """Handle scene deletion from SSE."""
        try:
            logger.info(f"Scene deletion detected: {scene_id}")
            
            hue_state_manager.remove_scene(scene_id)
            
            all_scenes = hue_state_manager.get_all_scenes()
            valid_scene_ids = set(all_scenes)

            from servers.blueprints.buttons import cleanup_deleted_scenes
            updated_count = cleanup_deleted_scenes(valid_scene_ids)
            
            if updated_count > 0:
                logger.info(f"Updated {updated_count} button configuration(s) after scene deletion")
        
        except Exception as e:
            logger.error(f"Error handling scene deletion {scene_id}: {e}", exc_info=True)
    
    def _handle_light_deletion(self, light_id: str):
        """Handle light deletion from SSE."""
        try:
            logger.info(f"Light deletion detected: {light_id}")
            hue_state_manager.remove_light(light_id)
        
        except Exception as e:
            logger.error(f"Error handling light deletion {light_id}: {e}", exc_info=True)
    
    def _handle_grouped_light_deletion(self, grouped_light_id: str):
        """Handle grouped_light deletion from SSE."""
        try:
            logger.info(f"Grouped light deletion detected: {grouped_light_id}")
            room_id = hue_state_manager.get_room_id_from_grouped_light(grouped_light_id)
            if room_id:
                logger.debug(f"Grouped light {grouped_light_id} was for room {room_id}")
        
        except Exception as e:
            logger.error(f"Error handling grouped_light deletion {grouped_light_id}: {e}", exc_info=True)
    
    def _handle_room_deletion(self, room_id: str):
        """Handle room deletion from SSE."""
        try:
            logger.info(f"Room deletion detected: {room_id}")
            hue_state_manager.remove_room(room_id)
        
        except Exception as e:
            logger.error(f"Error handling room deletion {room_id}: {e}", exc_info=True)
    
    def _handle_scene_addition(self, scene_id: str, data: dict):
        """Handle new scene added from SSE."""
        try:
            logger.info(f"Scene addition detected: {scene_id}")
            
            # Extract scene metadata
            scene_name = data.get('metadata', {}).get('name', 'Unknown')
            group_info = data.get('group', {})
            room_id = group_info.get('rid')
            
            # Register the new scene
            hue_state_manager.register_scene(scene_id, {
                'name': scene_name,
                'room_id': room_id
            })
            logger.info(f"Registered new scene {scene_id} ({scene_name}) for room {room_id}")
        
        except Exception as e:
            logger.error(f"Error handling scene addition {scene_id}: {e}", exc_info=True)
    
    def _handle_room_addition(self, room_id: str, data: dict):
        """Handle new room added from SSE."""
        try:
            logger.info(f"Room addition detected: {room_id}")
            
            # Extract room metadata
            room_name = data.get('metadata', {}).get('name', 'Unknown')
            
            # Get grouped_light_id for this room
            grouped_light_id = None
            for service in data.get('services', []):
                if service.get('rtype') == 'grouped_light':
                    grouped_light_id = service.get('rid')
                    break
            
            # Update room in state manager
            hue_state_manager.update_room(
                room_id=room_id,
                name=room_name,
                grouped_light_id=grouped_light_id
            )
            logger.info(f"Registered new room {room_id} ({room_name})")
        
        except Exception as e:
            logger.error(f"Error handling room addition {room_id}: {e}", exc_info=True)

    
    def _handle_light_update(self, light_id: str, data: dict):
        """Handle light state update from SSE."""
        try:
            # Extract light state - only include fields that are present
            is_on = None
            brightness = None
            xy_color = None
            ct_color = None
            color_mode = None
            
            on_data = data.get('on')
            if on_data is not None:
                is_on = on_data.get('on')
            
            dimming_data = data.get('dimming')
            if dimming_data is not None:
                brightness = dimming_data.get('brightness')
            
            # Get XY color if present
            color_data = data.get('color')
            if color_data is not None:
                color_xy = color_data.get('xy')
                if color_xy and 'x' in color_xy and 'y' in color_xy:
                    xy_color = {'x': color_xy['x'], 'y': color_xy['y']}
                    color_mode = 'xy'
            
            # Get color temperature if present
            color_temp_data = data.get('color_temperature')
            if color_temp_data is not None:
                ct_value = color_temp_data.get('mirek')
                if ct_value:
                    ct_color = ct_value
                    color_mode = 'ct'
            
            # Only update if we have at least one field
            if is_on is not None or brightness is not None or xy_color is not None or ct_color is not None:
                hue_state_manager.update_light(
                    light_id=light_id,
                    is_on=is_on,
                    brightness=brightness,
                    xy=xy_color,
                    ct=ct_color,
                    color_mode=color_mode
                )
        
        except Exception as e:
            logger.error(f"Error processing light update {light_id}: {e}", exc_info=True)
    
    def _handle_grouped_light_update(self, grouped_light_id: str, data: dict):
        """Handle room state update from SSE."""
        try:
            # Get the actual room ID from the grouped_light ID
            room_id = hue_state_manager.get_room_id_from_grouped_light(grouped_light_id)
            
            if room_id is None:
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
