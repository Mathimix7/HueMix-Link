"""Service that tracks the current state of all Hue lights and scenes."""
import threading
import logging
from typing import Dict, Optional, List, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class HueStateManager:
    """
    Centralized service that maintains the current state of all Hue lights and scenes.
    Updates from multiple sources: SSE events, button presses, web controls.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        
        # State storage
        self._lights: Dict[str, Dict] = {}
        self._rooms: Dict[str, Dict] = {}
        self._scenes: Dict[str, Dict] = {}
        
        # Mapping for grouped_light_id to room_id (for SSE events)
        self._grouped_light_to_room: Dict[str, str] = {}  # grouped_light_id -> room_id
        
        # Subscribers for state changes
        self._light_change_callbacks: List[Callable] = []
        self._scene_change_callbacks: List[Callable] = []
        self._room_change_callbacks: List[Callable] = []
        
        logger.info("HueStateManager initialized")
    
    # Light State Management
    
    def update_light(self, light_id: str, is_on: Optional[bool] = None,
                    brightness: Optional[float] = None,
                    room_id: Optional[str] = None, name: Optional[str] = None,
                    model_id: Optional[str] = None, xy: Optional[Dict] = None,
                    ct: Optional[int] = None, color_mode: Optional[str] = None,
                    supports_dimming: Optional[bool] = None):
        """
        Update the state of a specific light.
        
        Args:
            light_id: Hue light ID
            is_on: Whether light is on
            brightness: Brightness level (0-100)
            room_id: Room this light belongs to
            name: Light name
            model_id: Light model ID for determining color gamut
            xy: XY color coordinates dict with 'x' and 'y' (0.0-1.0)
            ct: Color temperature in Mired (153-500)
            color_mode: 'xy' or 'ct' to indicate which color mode is active
        """
        with self._lock:
            # Build state dict from provided values
            state = {}
            if is_on is not None:
                state['on'] = is_on
            if brightness is not None:
                state['brightness'] = brightness
            if room_id is not None:
                state['room_id'] = room_id
            if name is not None:
                state['name'] = name
            if model_id is not None:
                state['model_id'] = model_id
            if xy is not None:
                state['xy'] = xy
            if ct is not None:
                state['ct'] = ct
            if color_mode is not None:
                state['color_mode'] = color_mode
            if supports_dimming is not None:
                state['supports_dimming'] = supports_dimming
            
            existing_on = self._lights.get(light_id, {}).get('on') if light_id in self._lights else None
            final_on = state.get('on') if 'on' in state else existing_on

            existing_supports_dimming = self._lights.get(light_id, {}).get('supports_dimming') if light_id in self._lights else None
            final_supports_dimming = state.get('supports_dimming') if 'supports_dimming' in state else existing_supports_dimming

            # Only synthesize brightness for known non-dimmable devices.
            # Dimmable lights can validly report 0% brightness while on.
            if 'brightness' not in state and final_on is not None and final_supports_dimming is False:
                state['brightness'] = 100 if final_on else 0

            # Skip empty updates
            if not state:
                return
            
            # Store or update light state
            if light_id not in self._lights:
                self._lights[light_id] = {}
            
            old_state = self._lights[light_id].copy()
            self._lights[light_id].update(state)
            self._lights[light_id]['last_update'] = datetime.now().isoformat()
            
            # Update room state if light belongs to a room
            room_id = state.get('room_id') or self._lights[light_id].get('room_id')
            if room_id:
                self._update_room_aggregate(room_id)
            
            # Notify subscribers
            self._notify_light_change(light_id, self._lights[light_id], old_state)
    
    def get_light_state(self, light_id: str) -> Optional[Dict]:
        """Get the current state of a light."""
        with self._lock:
            return self._lights.get(light_id, {}).copy() if light_id in self._lights else None
    
    def get_all_lights(self) -> Dict[str, Dict]:
        """Get all light states."""
        with self._lock:
            return {lid: state.copy() for lid, state in self._lights.items()}
    
    def remove_light(self, light_id: str) -> bool:
        """Remove a light from state tracking.
        
        Args:
            light_id: Hue light ID to remove
            
        Returns:
            True if light was removed, False if it didn't exist
        """
        with self._lock:
            if light_id in self._lights:
                # Remove light from any room it belongs to
                room_id = self._lights[light_id].get('room_id')
                if room_id and room_id in self._rooms:
                    room_lights = self._rooms[room_id].get('lights', [])
                    if light_id in room_lights:
                        room_lights.remove(light_id)
                        self._update_room_aggregate(room_id)
                
                del self._lights[light_id]
                logger.info(f"Light {light_id} removed from state manager")
                return True
            return False
    
    def get_room_id_from_grouped_light(self, grouped_light_id: str) -> Optional[str]:
        """
        Get the room ID associated with a grouped_light ID.
        
        Args:
            grouped_light_id: The grouped_light service ID from SSE events
            
        Returns:
            The actual room ID, or None if not found
        """
        with self._lock:
            return self._grouped_light_to_room.get(grouped_light_id)
    
    # Room State Management
    
    def update_room(self, room_id: str, is_on: Optional[bool] = None, 
                   brightness: Optional[float] = None, name: Optional[str] = None,
                   lights: Optional[List[str]] = None, grouped_light_id: Optional[str] = None):
        """
        Update room information and state.
        
        Args:
            room_id: Hue room ID
            is_on: Whether room is on
            brightness: Average brightness
            name: Room name
            lights: List of light IDs in room
            grouped_light_id: Grouped light ID for this room
        """
        with self._lock:
            old_grouped_light_id = self._rooms.get(room_id, {}).get('grouped_light_id')

            if room_id not in self._rooms:
                self._rooms[room_id] = {
                    'lights': [],
                    'current_scene_id': None,
                    'is_on': False
                }
            
            # Update provided fields
            if is_on is not None:
                self._rooms[room_id]['is_on'] = is_on
            if brightness is not None:
                self._rooms[room_id]['avg_brightness'] = brightness
            if name is not None:
                self._rooms[room_id]['name'] = name
            if lights is not None:
                self._rooms[room_id]['lights'] = lights
            if grouped_light_id is not None:
                self._rooms[room_id]['grouped_light_id'] = grouped_light_id
                if old_grouped_light_id and old_grouped_light_id in self._grouped_light_to_room:
                    if self._grouped_light_to_room[old_grouped_light_id] == room_id:
                        del self._grouped_light_to_room[old_grouped_light_id]
                if grouped_light_id:
                    self._grouped_light_to_room[grouped_light_id] = room_id
            
            self._rooms[room_id]['last_update'] = datetime.now().isoformat()
            
            # Update aggregate state if lights were provided
            if lights is not None:
                self._update_room_aggregate(room_id)
    
    def set_room_scene(self, room_id: str, scene_id: Optional[str], old_scene_id: Optional[str] = None, source: str = 'sse'):
        """
        Set the active scene for a room.
        
        Args:
            room_id: Hue room ID
            scene_id: Hue scene ID being activated (None to clear)
            old_scene_id: Previous scene ID (if known, otherwise will be looked up)
            source: Source of the change ('sse', 'button', 'web')
        """
        with self._lock:
            if room_id not in self._rooms:
                self._rooms[room_id] = {'lights': [], 'is_on': False}
            
            # Use provided old_scene_id or look it up
            if old_scene_id is None:
                old_scene_id = self._rooms[room_id].get('current_scene_id')
            
            self._rooms[room_id]['current_scene_id'] = scene_id
            self._rooms[room_id]['last_scene_change'] = datetime.now().isoformat()
            
            logger.info(f"Room {room_id} scene changed from {old_scene_id} to {scene_id} (source: {source})")
            
            # Notify subscribers
            self._notify_scene_change(room_id, scene_id, old_scene_id, source)
    
    def get_room_state(self, room_id: str) -> Optional[Dict]:
        """Get the current state of a room."""
        with self._lock:
            return self._rooms.get(room_id, {}).copy() if room_id in self._rooms else None
    
    def get_all_rooms(self) -> Dict[str, Dict]:
        """Get all room states."""
        with self._lock:
            return {rid: state.copy() for rid, state in self._rooms.items()}
    
    def remove_room(self, room_id: str) -> bool:
        """Remove a room from state tracking.
        
        Args:
            room_id: Hue room ID to remove
            
        Returns:
            True if room was removed, False if it didn't exist
        """
        with self._lock:
            if room_id in self._rooms:
                # Remove grouped_light_id mapping
                grouped_light_id = self._rooms[room_id].get('grouped_light_id')
                if grouped_light_id and grouped_light_id in self._grouped_light_to_room:
                    del self._grouped_light_to_room[grouped_light_id]
                
                del self._rooms[room_id]
                logger.info(f"Room {room_id} removed from state manager")
                return True
            return False
    
    def _update_room_aggregate(self, room_id: str):
        """Update aggregate room state based on its lights."""
        if room_id not in self._rooms:
            return
        
        room = self._rooms[room_id]
        light_ids = room.get('lights', [])
        
        if not light_ids:
            return
        
        # Check if any light is on
        any_on = False
        total_brightness = 0
        on_count = 0
        
        for light_id in light_ids:
            if light_id in self._lights:
                light = self._lights[light_id]
                if light.get('on'):
                    any_on = True
                    on_count += 1
                    total_brightness += light.get('brightness', 0)
        
        # Update room aggregate state
        old_is_on = room.get('is_on', False)
        old_brightness = room.get('avg_brightness', 0)
        room['is_on'] = any_on
        room['on_count'] = on_count
        room['avg_brightness'] = total_brightness / on_count if on_count > 0 else 0
        
        # Notify if room on/off or brightness changed
        if old_is_on != any_on or old_brightness != room['avg_brightness']:
            self._notify_room_change(room_id, room)
    
    # Scene Management
    
    def register_scene(self, scene_id: str, scene_data: Dict):
        """
        Register a scene with its metadata.
        
        Args:
            scene_id: Hue scene ID
            scene_data: Dict with keys like {name, room_id}
        """
        with self._lock:
            self._scenes[scene_id] = scene_data
            logger.debug(f"Scene {scene_id} registered: {scene_data.get('name')}")
    
    def get_scene_info(self, scene_id: str) -> Optional[Dict]:
        """Get scene metadata."""
        with self._lock:
            return self._scenes.get(scene_id, {}).copy() if scene_id in self._scenes else None
    
    def remove_scene(self, scene_id: str) -> bool:
        """Remove a scene from state tracking.
        
        Args:
            scene_id: Hue scene ID to remove
            
        Returns:
            True if scene was removed, False if it didn't exist
        """
        with self._lock:
            if scene_id in self._scenes:
                # Clear scene from any room if it's currently active
                scene_info = self._scenes[scene_id]
                room_id = scene_info.get('room_id')
                if room_id and room_id in self._rooms:
                    if self._rooms[room_id].get('current_scene_id') == scene_id:
                        self._rooms[room_id]['current_scene_id'] = None
                        logger.info(f"Cleared active scene {scene_id} from room {room_id}")
                
                del self._scenes[scene_id]
                logger.info(f"Scene {scene_id} removed from state manager")
                return True
            return False

    def get_all_scenes(self) -> Dict[str, Dict]:
        """Get all registered scenes."""
        with self._lock:
            return {sid: data.copy() for sid, data in self._scenes.items()}
    
    # Subscription Management
    
    def subscribe_light_changes(self, callback: Callable):
        """Subscribe to light state changes."""
        with self._lock:
            if callback not in self._light_change_callbacks:
                self._light_change_callbacks.append(callback)
                logger.debug(f"Added light change subscriber: {callback.__name__}")
    
    def subscribe_scene_changes(self, callback: Callable):
        """Subscribe to scene activation events."""
        with self._lock:
            if callback not in self._scene_change_callbacks:
                self._scene_change_callbacks.append(callback)
                logger.debug(f"Added scene change subscriber: {callback.__name__}")
    
    def subscribe_room_changes(self, callback: Callable):
        """Subscribe to room state changes."""
        with self._lock:
            if callback not in self._room_change_callbacks:
                self._room_change_callbacks.append(callback)
                logger.debug(f"Added room change subscriber: {callback.__name__}")
    
    def unsubscribe_light_changes(self, callback: Callable):
        """Unsubscribe from light state changes."""
        with self._lock:
            if callback in self._light_change_callbacks:
                self._light_change_callbacks.remove(callback)
    
    def unsubscribe_scene_changes(self, callback: Callable):
        """Unsubscribe from scene changes."""
        with self._lock:
            if callback in self._scene_change_callbacks:
                self._scene_change_callbacks.remove(callback)
    
    def unsubscribe_room_changes(self, callback: Callable):
        """Unsubscribe from room changes."""
        with self._lock:
            if callback in self._room_change_callbacks:
                self._room_change_callbacks.remove(callback)
    
    # Notification Helpers
    
    def _notify_light_change(self, light_id: str, new_state: Dict, old_state: Dict):
        """Notify all subscribers of light state change."""
        for callback in self._light_change_callbacks[:]:  # Copy to avoid modification during iteration
            try:
                callback(light_id, new_state, old_state)
            except Exception as e:
                logger.error(f"Error in light change callback {callback.__name__}: {e}")
    
    def _notify_scene_change(self, room_id: str, scene_id: Optional[str], old_scene_id: Optional[str], source: str = 'sse'):
        """Notify all subscribers of scene change."""
        for callback in self._scene_change_callbacks[:]:
            try:
                callback(room_id, scene_id, old_scene_id, source)
            except Exception as e:
                logger.error(f"Error in scene change callback {callback.__name__}: {e}")
    
    def _notify_room_change(self, room_id: str, room_state: Dict):
        """Notify all subscribers of room state change."""
        for callback in self._room_change_callbacks[:]:
            try:
                callback(room_id, room_state)
            except Exception as e:
                logger.error(f"Error in room change callback {callback.__name__}: {e}")
    
    # Utility
    
    def initialize_from_bridge(self, hue_controller):
        """
        Initialize state manager with current data from Hue Bridge.
        Should be called once on startup.
        
        Args:
            hue_controller: Instance of Hue controller to fetch data from
        """
        try:
            logger.info("Initializing state manager from Hue Bridge...")
            
            # Fetch all data from bridge
            rooms_data = hue_controller.get_rooms()
            lights_data = hue_controller.get_lights()
            scenes_data = hue_controller.get_scenes()
            devices_data = hue_controller.get_devices()
            grouped_lights_data = hue_controller.get_grouped_lights()
            
            # Build device to light mapping
            device_to_light = {}
            light_to_model = {}
            for device in devices_data:
                device_id = device.get('id')
                product_data = device.get('product_data', {})
                model_id = product_data.get('model_id', 'LCT001')  # Default to gamut C
                for service in device.get('services', []):
                    if service.get('rtype') == 'light':
                        light_id = service.get('rid')
                        if light_id:
                            device_to_light[device_id] = light_id
                            light_to_model[light_id] = model_id
            
            # Process rooms
            for room in rooms_data:
                room_id = room.get('id')
                room_name = room.get('metadata', {}).get('name', 'Unknown')
                
                # Get lights in this room
                light_ids = []
                for child in room.get('children', []):
                    if child.get('rtype') == 'device':
                        device_id = child.get('rid')
                        if device_id in device_to_light:
                            light_ids.append(device_to_light[device_id])
                
                # Get room on/off state from grouped_light and map grouped_light_id to room_id
                is_on = False
                grouped_light_id = None
                for service in room.get('services', []):
                    if service.get('rtype') == 'grouped_light':
                        grouped_light_id = service.get('rid')
                        # Store mapping for SSE events
                        self._grouped_light_to_room[grouped_light_id] = room_id
                        for gl in grouped_lights_data:
                            if gl.get('id') == grouped_light_id:
                                is_on = gl.get('on', {}).get('on', False)
                                break
                        break
                
                # Store room state with grouped_light_id
                if room_id not in self._rooms:
                    self._rooms[room_id] = {}
                
                self._rooms[room_id].update({
                    'name': room_name,
                    'lights': light_ids,
                    'is_on': is_on,
                    'grouped_light_id': grouped_light_id,
                    'last_update': datetime.now().isoformat()
                })
            
            # Process lights
            for light in lights_data:
                light_id = light.get('id')
                is_on = light.get('on', {}).get('on', False)
                supports_dimming = isinstance(light.get('dimming'), dict)
                brightness = light.get('dimming', {}).get('brightness') if supports_dimming else None
                light_name = light.get('metadata', {}).get('name', f'Light {light_id[:8]}')
                model_id = light_to_model.get(light_id, 'LCT001')  # Default to gamut C
                
                # Find room for this light
                room_id = None
                for rid, room_state in self._rooms.items():
                    if light_id in room_state.get('lights', []):
                        room_id = rid
                        break
                
                # Get color if available - store original values
                xy_color = None
                ct_color = None
                color_mode = None
                
                color_data = light.get('color', {})
                if color_data:
                    xy = color_data.get('xy', {})
                    if xy and 'x' in xy and 'y' in xy:
                        xy_color = {'x': xy['x'], 'y': xy['y']}
                        color_mode = 'xy'
                
                # Check for color temperature
                color_temp_data = light.get('color_temperature', {})
                if color_temp_data:
                    ct_value = color_temp_data.get('mirek')
                    if ct_value:
                        ct_color = ct_value
                        color_mode = 'ct'
                
                self.update_light(
                    light_id=light_id,
                    is_on=is_on,
                    brightness=brightness,
                    room_id=room_id,
                    name=light_name,
                    model_id=model_id,
                    xy=xy_color,
                    ct=ct_color,
                    color_mode=color_mode,
                    supports_dimming=supports_dimming
                )
            
            # Process scenes
            for scene in scenes_data:
                scene_id = scene.get('id')
                scene_name = scene.get('metadata', {}).get('name', 'Unknown')
                
                # Get room for this scene
                group_info = scene.get('group', {})
                room_id = group_info.get('rid')
                
                # Check if scene is currently active
                status = scene.get('status', {})
                is_active = status.get('active') != 'inactive'
                
                self.register_scene(scene_id, {
                    'name': scene_name,
                    'room_id': room_id
                })
                
                # If scene is active, set it as the current scene for the room
                if is_active and room_id:
                    logger.debug(f"Found active scene {scene_name} ({scene_id}) in room {room_id}")
                    self.set_room_scene(room_id, scene_id)
            
            logger.info(f"State manager initialized: {len(self._lights)} lights, "
                       f"{len(self._rooms)} rooms, {len(self._scenes)} scenes")
        
        except Exception as e:
            logger.error(f"Error initializing state manager from bridge: {e}")
            raise
    
    def get_current_state_summary(self) -> Dict:
        """Get a summary of the current state."""
        with self._lock:
            return {
                'total_lights': len(self._lights),
                'lights_on': sum(1 for l in self._lights.values() if l.get('on')),
                'total_rooms': len(self._rooms),
                'rooms_on': sum(1 for r in self._rooms.values() if r.get('is_on')),
                'total_scenes': len(self._scenes),
                'last_update': datetime.now().isoformat()
            }


# Global singleton instance
hue_state_manager = HueStateManager()
