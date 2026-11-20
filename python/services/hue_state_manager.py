"""Service that tracks the current state of all Hue lights and scenes."""
import threading
import logging
from typing import Dict, Optional, List, Callable, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class HueStateManager:
    """
    Centralized service that maintains the current state of all Hue lights and scenes.
    Updates from multiple sources: SSE events, button presses, web controls.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        # Serializes full bridge syncs (init vs. periodic reconciliation).
        self._sync_lock = threading.Lock()
        
        # State storage
        self._lights: Dict[str, Dict] = {}
        self._rooms: Dict[str, Dict] = {}
        self._zones: Dict[str, Dict] = {}
        self._scenes: Dict[str, Dict] = {}
        
        # Mapping for grouped_light_id to room_id (for SSE events)
        self._grouped_light_to_room: Dict[str, str] = {}  # grouped_light_id -> room_id
        # Mapping for grouped_light_id to zone_id (for SSE events)
        self._grouped_light_to_zone: Dict[str, str] = {}  # grouped_light_id -> zone_id
        # Reverse index: light_id -> zone_ids (zones overlap; a light can be in many zones)
        self._light_to_zones: Dict[str, List[str]] = {}
        
        # Subscribers for state changes
        self._light_change_callbacks: List[Callable] = []
        self._scene_change_callbacks: List[Callable] = []
        self._room_change_callbacks: List[Callable] = []
        self._zone_change_callbacks: List[Callable] = []
        
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
            previous_room_id = old_state.get('room_id')
            self._lights[light_id].update(state)
            self._lights[light_id]['last_update'] = datetime.now().isoformat()
            
            # Keep room membership lists in sync when the room changes
            if 'room_id' in state:
                self._update_light_membership(light_id, previous_room_id, state['room_id'])
            
            # Update room state if light belongs to a room
            room_id = state.get('room_id') or self._lights[light_id].get('room_id')
            if room_id:
                self._update_room_aggregate(room_id)
            
            # Update all zone states this light belongs to (zones overlap)
            for zone_id in self._light_to_zones.get(light_id, []):
                self._update_zone_aggregate(zone_id)
            
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
                # Remove the light from every room list it could still be in
                for room_id in list(self._rooms.keys()):
                    room_lights = self._rooms[room_id].get('lights', [])
                    if light_id in room_lights:
                        room_lights.remove(light_id)
                        self._update_room_aggregate(room_id)
                
                # Remove light from any zones it belongs to
                for zone_id in self._light_to_zones.pop(light_id, []):
                    if zone_id in self._zones:
                        zone_lights = self._zones[zone_id].get('lights', [])
                        if light_id in zone_lights:
                            zone_lights.remove(light_id)
                            self._update_zone_aggregate(zone_id)
                
                del self._lights[light_id]
                logger.info(f"Light {light_id} removed from state manager")
                return True
            return False
    
    def _update_light_membership(self, light_id: str, previous_room_id: Optional[str], room_id: Optional[str]):
        """Keep the room light lists in sync with a light's room assignment.
        
        Must be called while holding the lock.
        
        Args:
            light_id: Light being assigned
            previous_room_id: Room the light belonged to before this change
            room_id: Room the light belongs to now (None when unassigned)
        """
        if previous_room_id and previous_room_id != room_id and previous_room_id in self._rooms:
            previous_lights = self._rooms[previous_room_id].get('lights', [])
            if light_id in previous_lights:
                previous_lights.remove(light_id)
                self._update_room_aggregate(previous_room_id)
        
        if room_id:
            room = self._rooms.get(room_id)
            if room is None:
                room = {'lights': [], 'current_scene_id': None, 'is_on': False}
                self._rooms[room_id] = room
            room_lights = room.setdefault('lights', [])
            if light_id not in room_lights:
                room_lights.append(light_id)
                self._update_room_aggregate(room_id)
    
    def _assign_light_room_locked(self, light_id: str, room_id: Optional[str]):
        """Locked variant of assign_light_room.
        
        Returns:
            Tuple of (new_state, old_state) when something changed, else None
        """
        light = self._lights.get(light_id)
        created = False
        if light is None:
            if room_id is None:
                return None
            light = {}
            self._lights[light_id] = light
            created = True
            old_state = {}
        else:
            old_state = light.copy()
        
        previous_room_id = light.get('room_id')
        membership_changed = previous_room_id != room_id
        if membership_changed:
            if room_id is None:
                light.pop('room_id', None)
            else:
                light['room_id'] = room_id
            light['last_update'] = datetime.now().isoformat()
        
        self._update_light_membership(light_id, previous_room_id, room_id)
        
        if not created and not membership_changed:
            return None
        return (light.copy(), old_state)
    
    def assign_light_room(self, light_id: str, room_id: Optional[str]) -> bool:
        """Assign (or clear) the room a light belongs to.
        
        Args:
            light_id: Hue light ID
            room_id: Room ID the light belongs to, or None to unassign
            
        Returns:
            True when the assignment changed, False otherwise
        """
        with self._lock:
            result = self._assign_light_room_locked(light_id, room_id)
            if not result:
                return False
            new_state, old_state = result
            logger.info(f"Light {light_id} assigned to room {room_id}")
            self._notify_light_change(light_id, new_state, old_state)
            return True
    
    def sync_group_membership(self, group_id: str, group_type: str, light_ids: Optional[List[str]],
                              device_ids: Optional[List[str]] = None) -> bool:
        """Reconcile a room's or zone's light membership from authoritative bridge data.
        
        Zones overlap, so a light may belong to many zones but only one room.
        
        Args:
            group_id: Room or zone ID
            group_type: 'room' or 'zone'
            light_ids: Light IDs that belong to the group according to the bridge
            device_ids: Device IDs that are children of the group (optional)
            
        Returns:
            True when local membership changed, False otherwise
        """
        if not group_id:
            return False
        
        is_zone = group_type == 'zone'
        new_lights: List[str] = []
        seen = set()
        for light_id in light_ids or []:
            if light_id and light_id not in seen:
                seen.add(light_id)
                new_lights.append(light_id)
        
        with self._lock:
            groups = self._zones if is_zone else self._rooms
            group = groups.get(group_id)
            if group is None:
                group = {'lights': [], 'current_scene_id': None, 'is_on': False}
                groups[group_id] = group
            
            old_lights = list(group.get('lights') or [])
            membership_changed = old_lights != new_lights
            devices_changed = (
                device_ids is not None
                and list(group.get('device_ids') or []) != list(device_ids)
            )
            
            if not membership_changed and not devices_changed:
                return False
            
            if device_ids is not None:
                group['device_ids'] = list(device_ids)
            
            if membership_changed:
                group['lights'] = list(new_lights)
                group['last_update'] = datetime.now().isoformat()
                
                new_set = set(new_lights)
                old_set = set(old_lights)
                changed_lights = []
                
                if is_zone:
                    for light_id in old_set - new_set:
                        light_zones = self._light_to_zones.get(light_id)
                        if light_zones and group_id in light_zones:
                            light_zones.remove(group_id)
                            if not light_zones:
                                del self._light_to_zones[light_id]
                    for light_id in new_lights:
                        light_zones = self._light_to_zones.setdefault(light_id, [])
                        if group_id not in light_zones:
                            light_zones.append(group_id)
                else:
                    for light_id in old_set - new_set:
                        light = self._lights.get(light_id)
                        if light is None or light.get('room_id') != group_id:
                            continue
                        result = self._assign_light_room_locked(light_id, None)
                        if result:
                            changed_lights.append((light_id, *result))
                    for light_id in new_lights:
                        result = self._assign_light_room_locked(light_id, group_id)
                        if result:
                            changed_lights.append((light_id, *result))
                    # Re-apply bridge ordering after the per-light updates.
                    group['lights'] = list(new_lights)
                
                logger.debug(
                    f"{group_type.capitalize()} {group_id} membership changed: {len(old_lights)} -> {len(new_lights)} lights"
                )
                
                if is_zone:
                    self._update_zone_aggregate(group_id)
                    self._notify_zone_change(group_id, group)
                else:
                    self._update_room_aggregate(group_id)
                    self._notify_room_change(group_id, group)
                
                for light_id, new_state, old_state in changed_lights:
                    self._notify_light_change(light_id, new_state, old_state)
            
            return True
    
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
    
    def get_grouped_light_owner(self, grouped_light_id: str) -> Optional[tuple]:
        """
        Get the owning group (room or zone) of a grouped_light ID.
        
        Args:
            grouped_light_id: The grouped_light service ID from SSE events
            
        Returns:
            Tuple of (owner_type, owner_id) where owner_type is 'room' or 'zone',
            or None if not found
        """
        with self._lock:
            room_id = self._grouped_light_to_room.get(grouped_light_id)
            if room_id:
                return ('room', room_id)
            zone_id = self._grouped_light_to_zone.get(grouped_light_id)
            if zone_id:
                return ('zone', zone_id)
            return None
    
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
            
            # Capture old on/brightness for change detection when lights are not provided
            old_is_on = self._rooms[room_id].get('is_on')
            old_brightness = self._rooms[room_id].get('avg_brightness')

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
            else:
                # When is_on/brightness are set directly (e.g. from grouped_light SSE),
                # notify subscribers so lightstrip sync and other listeners are triggered.
                new_is_on = self._rooms[room_id].get('is_on')
                new_brightness = self._rooms[room_id].get('avg_brightness')
                if (is_on is not None and old_is_on != new_is_on) or \
                   (brightness is not None and old_brightness != new_brightness):
                    self._notify_room_change(room_id, self._rooms[room_id])
    
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
            self._notify_scene_change(room_id, scene_id, old_scene_id, source, group_type='room')
    
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
                
                # Unassign the room from its lights so they do not keep pointing
                # at a room that no longer exists.
                for light_id in list(self._rooms[room_id].get('lights', [])):
                    light = self._lights.get(light_id)
                    if light and light.get('room_id') == room_id:
                        old_state = light.copy()
                        light.pop('room_id', None)
                        light['last_update'] = datetime.now().isoformat()
                        self._notify_light_change(light_id, light.copy(), old_state)
                
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
            # Empty room: clear stale aggregates instead of keeping the last state.
            old_is_on = room.get('is_on', False)
            old_brightness = room.get('avg_brightness', 0)
            room['is_on'] = False
            room['on_count'] = 0
            room['avg_brightness'] = 0
            if old_is_on or old_brightness:
                self._notify_room_change(room_id, room)
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
    
    # Zone State Management
    
    def update_zone(self, zone_id: str, is_on: Optional[bool] = None,
                    brightness: Optional[float] = None, name: Optional[str] = None,
                    lights: Optional[List[str]] = None, grouped_light_id: Optional[str] = None):
        """
        Update zone information and state.
        
        Args:
            zone_id: Hue zone ID
            is_on: Whether zone is on
            brightness: Average brightness
            name: Zone name
            lights: List of light IDs in zone
            grouped_light_id: Grouped light ID for this zone
        """
        with self._lock:
            old_grouped_light_id = self._zones.get(zone_id, {}).get('grouped_light_id')

            if zone_id not in self._zones:
                self._zones[zone_id] = {
                    'lights': [],
                    'current_scene_id': None,
                    'is_on': False
                }
            
            # Capture old on/brightness for change detection when lights are not provided
            old_is_on = self._zones[zone_id].get('is_on')
            old_brightness = self._zones[zone_id].get('avg_brightness')

            # Update provided fields
            if is_on is not None:
                self._zones[zone_id]['is_on'] = is_on
            if brightness is not None:
                self._zones[zone_id]['avg_brightness'] = brightness
            if name is not None:
                self._zones[zone_id]['name'] = name
            if lights is not None:
                # Update reverse index for zone membership
                old_lights = set(self._zones[zone_id].get('lights', []))
                new_lights = set(lights)
                for light_id in old_lights - new_lights:
                    if light_id in self._light_to_zones:
                        self._light_to_zones[light_id] = [z for z in self._light_to_zones[light_id] if z != zone_id]
                        if not self._light_to_zones[light_id]:
                            del self._light_to_zones[light_id]
                for light_id in new_lights - old_lights:
                    self._light_to_zones.setdefault(light_id, [])
                    if zone_id not in self._light_to_zones[light_id]:
                        self._light_to_zones[light_id].append(zone_id)
                self._zones[zone_id]['lights'] = lights
            if grouped_light_id is not None:
                self._zones[zone_id]['grouped_light_id'] = grouped_light_id
                if old_grouped_light_id and old_grouped_light_id in self._grouped_light_to_zone:
                    if self._grouped_light_to_zone[old_grouped_light_id] == zone_id:
                        del self._grouped_light_to_zone[old_grouped_light_id]
                if grouped_light_id:
                    self._grouped_light_to_zone[grouped_light_id] = zone_id
            
            self._zones[zone_id]['last_update'] = datetime.now().isoformat()
            
            # Update aggregate state if lights were provided
            if lights is not None:
                self._update_zone_aggregate(zone_id)
            else:
                # When is_on/brightness are set directly (e.g. from grouped_light SSE),
                # notify subscribers so lightstrip sync and other listeners are triggered.
                new_is_on = self._zones[zone_id].get('is_on')
                new_brightness = self._zones[zone_id].get('avg_brightness')
                if (is_on is not None and old_is_on != new_is_on) or \
                   (brightness is not None and old_brightness != new_brightness):
                    self._notify_zone_change(zone_id, self._zones[zone_id])
    
    def set_zone_scene(self, zone_id: str, scene_id: Optional[str], old_scene_id: Optional[str] = None, source: str = 'sse'):
        """
        Set the active scene for a zone.
        
        Args:
            zone_id: Hue zone ID
            scene_id: Hue scene ID being activated (None to clear)
            old_scene_id: Previous scene ID (if known, otherwise will be looked up)
            source: Source of the change ('sse', 'button', 'web')
        """
        with self._lock:
            if zone_id not in self._zones:
                self._zones[zone_id] = {'lights': [], 'is_on': False}
            
            # Use provided old_scene_id or look it up
            if old_scene_id is None:
                old_scene_id = self._zones[zone_id].get('current_scene_id')
            
            self._zones[zone_id]['current_scene_id'] = scene_id
            self._zones[zone_id]['last_scene_change'] = datetime.now().isoformat()
            
            logger.info(f"Zone {zone_id} scene changed from {old_scene_id} to {scene_id} (source: {source})")
            
            # Notify subscribers
            self._notify_scene_change(zone_id, scene_id, old_scene_id, source, group_type='zone')
    
    def get_zone_state(self, zone_id: str) -> Optional[Dict]:
        """Get the current state of a zone."""
        with self._lock:
            return self._zones.get(zone_id, {}).copy() if zone_id in self._zones else None
    
    def get_all_zones(self) -> Dict[str, Dict]:
        """Get all zone states."""
        with self._lock:
            return {zid: state.copy() for zid, state in self._zones.items()}
    
    def remove_zone(self, zone_id: str) -> bool:
        """Remove a zone from state tracking.
        
        Args:
            zone_id: Hue zone ID to remove
            
        Returns:
            True if zone was removed, False if it didn't exist
        """
        with self._lock:
            if zone_id in self._zones:
                # Remove grouped_light_id mapping
                grouped_light_id = self._zones[zone_id].get('grouped_light_id')
                if grouped_light_id and grouped_light_id in self._grouped_light_to_zone:
                    del self._grouped_light_to_zone[grouped_light_id]
                
                # Clean reverse index
                for light_id in self._zones[zone_id].get('lights', []):
                    if light_id in self._light_to_zones:
                        self._light_to_zones[light_id] = [z for z in self._light_to_zones[light_id] if z != zone_id]
                        if not self._light_to_zones[light_id]:
                            del self._light_to_zones[light_id]
                
                del self._zones[zone_id]
                logger.info(f"Zone {zone_id} removed from state manager")
                return True
            return False
    
    def _update_zone_aggregate(self, zone_id: str):
        """Update aggregate zone state based on its lights."""
        if zone_id not in self._zones:
            return
        
        zone = self._zones[zone_id]
        light_ids = zone.get('lights', [])
        
        if not light_ids:
            # Empty zone: clear stale aggregates instead of keeping the last state.
            old_is_on = zone.get('is_on', False)
            old_brightness = zone.get('avg_brightness', 0)
            zone['is_on'] = False
            zone['on_count'] = 0
            zone['avg_brightness'] = 0
            if old_is_on or old_brightness:
                self._notify_zone_change(zone_id, zone)
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
        
        # Update zone aggregate state
        old_is_on = zone.get('is_on', False)
        old_brightness = zone.get('avg_brightness', 0)
        zone['is_on'] = any_on
        zone['on_count'] = on_count
        zone['avg_brightness'] = total_brightness / on_count if on_count > 0 else 0
        
        # Notify if zone on/off or brightness changed
        if old_is_on != any_on or old_brightness != zone['avg_brightness']:
            self._notify_zone_change(zone_id, zone)
    
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
                # Clear scene from any room/zone if it's currently active
                scene_info = self._scenes[scene_id]
                group_id = scene_info.get('room_id')
                group_type = scene_info.get('group_type', 'room')
                if group_id:
                    if group_type == 'zone' and group_id in self._zones:
                        if self._zones[group_id].get('current_scene_id') == scene_id:
                            self._zones[group_id]['current_scene_id'] = None
                            logger.info(f"Cleared active scene {scene_id} from zone {group_id}")
                    elif group_type == 'room' and group_id in self._rooms:
                        if self._rooms[group_id].get('current_scene_id') == scene_id:
                            self._rooms[group_id]['current_scene_id'] = None
                            logger.info(f"Cleared active scene {scene_id} from room {group_id}")
                
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
    
    def subscribe_zone_changes(self, callback: Callable):
        """Subscribe to zone state changes."""
        with self._lock:
            if callback not in self._zone_change_callbacks:
                self._zone_change_callbacks.append(callback)
                logger.debug(f"Added zone change subscriber: {callback.__name__}")
    
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
        """Unsubscribe from room state changes."""
        with self._lock:
            if callback in self._room_change_callbacks:
                self._room_change_callbacks.remove(callback)
    
    def unsubscribe_zone_changes(self, callback: Callable):
        """Unsubscribe from zone state changes."""
        with self._lock:
            if callback in self._zone_change_callbacks:
                self._zone_change_callbacks.remove(callback)
    
    # Notification Helpers
    
    def _notify_light_change(self, light_id: str, new_state: Dict, old_state: Dict):
        """Notify all subscribers of light state change."""
        for callback in self._light_change_callbacks[:]:  # Copy to avoid modification during iteration
            try:
                callback(light_id, new_state, old_state)
            except Exception as e:
                logger.error(f"Error in light change callback {callback.__name__}: {e}")
    
    def _notify_scene_change(self, room_id: str, scene_id: Optional[str], old_scene_id: Optional[str], source: str = 'sse', group_type: str = 'room'):
        """Notify all subscribers of scene change."""
        for callback in self._scene_change_callbacks[:]:
            try:
                callback(room_id, scene_id, old_scene_id, source, group_type=group_type)
            except TypeError:
                # Backward compatibility: callers or callbacks that don't yet pass group_type
                callback(room_id, scene_id, old_scene_id, source)
            except Exception as e:
                logger.error(f"Error in scene change callback {callback.__name__}: {e}")
    
    def _notify_room_change(self, room_id: str, room_state: Dict):
        """Notify all subscribers of room state change."""
        for callback in self._room_change_callbacks[:]:  # Copy to avoid modification during iteration
            try:
                callback(room_id, room_state)
            except Exception as e:
                logger.error(f"Error in room change callback {callback.__name__}: {e}")
    
    def _notify_zone_change(self, zone_id: str, zone_state: Dict):
        """Notify all subscribers of zone state change."""
        for callback in self._zone_change_callbacks[:]:  # Copy to avoid modification during iteration
            try:
                callback(zone_id, zone_state)
            except Exception as e:
                logger.error(f"Error in zone change callback {callback.__name__}: {e}")
    
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

            with self._sync_lock:
                snapshot = self._fetch_bridge_snapshot(hue_controller)

                # Start from a clean slate so topology that changed while we were
                # down (deleted rooms, moved or removed lights) cannot linger.
                with self._lock:
                    self._lights.clear()
                    self._rooms.clear()
                    self._zones.clear()
                    self._scenes.clear()
                    self._grouped_light_to_room.clear()
                    self._grouped_light_to_zone.clear()
                    self._light_to_zones.clear()

                self._apply_bridge_snapshot(snapshot)

            logger.info(f"State manager initialized: {len(self._lights)} lights, "
                       f"{len(self._rooms)} rooms, {len(self._zones)} zones, {len(self._scenes)} scenes")

        except Exception as e:
            logger.error(f"Error initializing state manager from bridge: {e}")
            raise

    def reconcile_with_bridge(self, hue_controller):
        """
        Periodic reconciliation: query bridge and sync local state.
        Detects and logs any mismatches, then updates local state to match bridge.
        Called after SSE reconnection to catch missed events.

        Reconciles group membership, light room assignment, light metadata,
        on/off state, active scenes and removes stale resources.

        Args:
            hue_controller: Instance of Hue controller to fetch current data from
        """
        try:
            logger.info("Starting state reconciliation with bridge...")

            with self._sync_lock:
                snapshot = self._fetch_bridge_snapshot(hue_controller)
                mismatches = self._apply_bridge_snapshot(snapshot)

            if mismatches:
                logger.warning(f"State reconciliation found {len(mismatches)} mismatches:")
                for mismatch in mismatches:
                    logger.warning(f"  - {mismatch}")
            else:
                logger.info("State reconciliation complete: no mismatches found")

        except Exception as e:
            logger.error(f"State reconciliation failed: {e}", exc_info=True)

    # Bridge snapshot helpers

    @staticmethod
    def _fetch_bridge_snapshot(hue_controller) -> Dict:
        """Fetch every resource needed to reconcile local state with the bridge."""
        return {
            'rooms': hue_controller.get_rooms() or [],
            'zones': hue_controller.get_zones() or [],
            'lights': hue_controller.get_lights() or [],
            'devices': hue_controller.get_devices() or [],
            'grouped_lights': hue_controller.get_grouped_lights() or [],
            'scenes': hue_controller.get_scenes() or [],
        }

    @staticmethod
    def _build_device_indexes(devices) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        """Build (device_id -> light ids, light_id -> model id) indexes.

        Devices without a light service (sensors, remotes, ...) are kept with an
        empty list so callers can tell "known device without lights" apart from
        "unknown device".
        """
        device_to_lights: Dict[str, List[str]] = {}
        light_to_model: Dict[str, str] = {}

        for device in devices:
            device_id = device.get('id')
            if not device_id:
                continue
            model_id = (device.get('product_data') or {}).get('model_id') or 'LCT001'
            light_ids = [
                service.get('rid')
                for service in device.get('services', [])
                if service.get('rtype') == 'light' and service.get('rid')
            ]
            device_to_lights[device_id] = light_ids
            for light_id in light_ids:
                light_to_model[light_id] = model_id

        return device_to_lights, light_to_model

    @staticmethod
    def _resolve_group_children(group: dict, device_to_lights: Dict[str, List[str]]) -> Tuple[List[str], List[str]]:
        """Resolve (device_ids, light_ids) for a room/zone from its children."""
        device_ids: List[str] = []
        light_ids: List[str] = []
        seen = set()

        for child in group.get('children') or []:
            if not isinstance(child, dict):
                continue
            child_id = child.get('rid')
            if not child_id:
                continue
            child_type = child.get('rtype')
            if child_type == 'device':
                device_ids.append(child_id)
                for light_id in device_to_lights.get(child_id, []):
                    if light_id not in seen:
                        seen.add(light_id)
                        light_ids.append(light_id)
            elif child_type == 'light':
                if child_id not in seen:
                    seen.add(child_id)
                    light_ids.append(child_id)

        return device_ids, light_ids

    def _apply_bridge_snapshot(self, snapshot: Dict) -> List[str]:
        """Apply a full bridge snapshot to local state.

        Reconciles group membership (rooms/zones -> lights), light metadata and
        room assignment, on/off state, active scenes, and removes anything that
        no longer exists on the bridge.

        Returns the list of detected mismatches.
        """
        rooms = snapshot.get('rooms') or []
        zones = snapshot.get('zones') or []
        lights = snapshot.get('lights') or []
        devices = snapshot.get('devices') or []
        grouped_lights = snapshot.get('grouped_lights') or []
        scenes = snapshot.get('scenes') or []

        mismatches: List[str] = []

        device_to_lights, light_to_model = self._build_device_indexes(devices)

        # A completely empty device index while rooms/zones still reference
        # devices means we received an incomplete response: never treat that as
        # "every room became empty" or "every light was deleted".
        topology_valid = bool(device_to_lights) or not any(
            any(isinstance(child, dict) and child.get('rtype') == 'device' and child.get('rid')
                for child in (group.get('children') or []))
            for group in list(rooms) + list(zones)
        )
        if not topology_valid:
            mismatches.append(
                "Bridge device index is empty while groups still reference devices; "
                "skipping membership and removal reconciliation"
            )

        grouped_light_on = {
            gl.get('id'): gl.get('on', {}).get('on', False)
            for gl in grouped_lights
            if gl.get('id')
        }

        active_scene_by_group: Dict[str, str] = {}
        for scene in scenes:
            scene_id = scene.get('id')
            group_id = scene.get('group', {}).get('rid')
            scene_status = (scene.get('status') or {}).get('active')
            # A real recall reports 'static'/'dynamic'; scenes without a status
            # carry no activation info and must not flip the active scene.
            if scene_id and group_id and scene_status is not None and scene_status != 'inactive':
                active_scene_by_group[group_id] = scene_id

        light_to_room: Dict[str, str] = {}
        bridge_room_ids = {room.get('id') for room in rooms if room.get('id')}
        bridge_zone_ids = {zone.get('id') for zone in zones if zone.get('id')}

        # 1) Rooms and zones: membership, name, grouped_light mapping, on-state.
        for group_type, group_list in (('room', rooms), ('zone', zones)):
            for group in group_list:
                group_id = group.get('id')
                if not group_id:
                    continue

                device_ids, light_ids = self._resolve_group_children(group, device_to_lights)
                if topology_valid:
                    if group_type == 'room':
                        for light_id in light_ids:
                            light_to_room[light_id] = group_id

                    if self.sync_group_membership(group_id, group_type, light_ids, device_ids=device_ids):
                        mismatches.append(f"{group_type.capitalize()} {group_id}: membership changed -> {light_ids}")

                grouped_light_id = None
                for service in group.get('services', []):
                    if service.get('rtype') == 'grouped_light':
                        grouped_light_id = service.get('rid')
                        break

                group_name = (group.get('metadata') or {}).get('name')
                group_on = grouped_light_on.get(grouped_light_id) if grouped_light_id else None

                with self._lock:
                    local_group = (self._zones if group_type == 'zone' else self._rooms).get(group_id, {})
                    local_name = local_group.get('name')
                    local_grouped_light_id = local_group.get('grouped_light_id')
                    local_is_on = local_group.get('is_on')

                name_mismatch = group_name is not None and local_name != group_name
                grouped_mismatch = grouped_light_id is not None and local_grouped_light_id != grouped_light_id
                on_mismatch = group_on is not None and local_is_on != group_on

                if name_mismatch or grouped_mismatch or on_mismatch:
                    details = []
                    if name_mismatch:
                        details.append(f"name local={local_name} bridge={group_name}")
                    if grouped_mismatch:
                        details.append(f"grouped_light local={local_grouped_light_id} bridge={grouped_light_id}")
                    if on_mismatch:
                        details.append(f"on local={local_is_on} bridge={group_on}")
                    mismatches.append(f"{group_type.capitalize()} {group_id}: " + ", ".join(details))

                if group_type == 'zone':
                    self.update_zone(
                        zone_id=group_id,
                        name=group_name,
                        is_on=group_on,
                        grouped_light_id=grouped_light_id
                    )
                else:
                    self.update_room(
                        room_id=group_id,
                        name=group_name,
                        is_on=group_on,
                        grouped_light_id=grouped_light_id
                    )

        # 2) Lights: add unknown lights, refresh changed metadata and state.
        bridge_light_ids = set()
        for light in lights:
            light_id = light.get('id')
            if not light_id:
                continue
            bridge_light_ids.add(light_id)

            supports_dimming = isinstance(light.get('dimming'), dict)
            brightness = light.get('dimming', {}).get('brightness') if supports_dimming else None
            is_on = light.get('on', {}).get('on', False)
            name = (light.get('metadata') or {}).get('name')
            model_id = light_to_model.get(light_id, 'LCT001')
            room_id = light_to_room.get(light_id)

            xy_color = None
            ct_color = None
            color_mode = None
            color_data = light.get('color') or {}
            xy = color_data.get('xy') or {}
            if xy and 'x' in xy and 'y' in xy:
                xy_color = {'x': xy['x'], 'y': xy['y']}
                color_mode = 'xy'
            color_temp_data = light.get('color_temperature') or {}
            ct_value = color_temp_data.get('mirek')
            if ct_value:
                ct_color = ct_value
                color_mode = 'ct'

            with self._lock:
                local_light = self._lights.get(light_id)

            if local_light is None:
                self.update_light(
                    light_id=light_id,
                    is_on=is_on,
                    brightness=brightness,
                    room_id=room_id,
                    name=name,
                    model_id=model_id,
                    xy=xy_color,
                    ct=ct_color,
                    color_mode=color_mode,
                    supports_dimming=supports_dimming
                )
                mismatches.append(f"Light {light_id}: known on bridge but missing locally, added")
                continue

            local_on = local_light.get('on')
            local_brightness = local_light.get('brightness')
            local_name = local_light.get('name')
            local_model_id = local_light.get('model_id')
            local_supports_dimming = local_light.get('supports_dimming')

            on_mismatch = local_on != is_on
            brightness_mismatch = (
                supports_dimming
                and brightness is not None
                and local_brightness is not None
                and abs(float(local_brightness) - float(brightness)) > 0.5
            )
            name_mismatch = name is not None and local_name != name
            model_mismatch = local_model_id != model_id
            dimming_mismatch = local_supports_dimming != supports_dimming

            if not (on_mismatch or brightness_mismatch or name_mismatch or model_mismatch or dimming_mismatch):
                continue

            details = []
            if on_mismatch:
                details.append(f"on local={local_on} bridge={is_on}")
            if brightness_mismatch:
                details.append(f"brightness local={local_brightness} bridge={brightness}")
            if name_mismatch:
                details.append(f"name local={local_name} bridge={name}")
            if model_mismatch:
                details.append(f"model local={local_model_id} bridge={model_id}")
            if dimming_mismatch:
                details.append(f"dimming local={local_supports_dimming} bridge={supports_dimming}")
            mismatches.append(f"Light {light_id}: " + ", ".join(details))

            self.update_light(
                light_id=light_id,
                is_on=is_on,
                brightness=brightness,
                name=name,
                model_id=model_id,
                supports_dimming=supports_dimming
            )

        # 3) Room assignment for every known light (also clears stale assignments).
        with self._lock:
            known_light_ids = list(self._lights.keys())

        if topology_valid:
            for light_id in known_light_ids:
                expected_room = light_to_room.get(light_id)
                with self._lock:
                    current_room = self._lights.get(light_id, {}).get('room_id')
                if current_room == expected_room:
                    continue
                if self.assign_light_room(light_id, expected_room):
                    mismatches.append(
                        f"Light {light_id}: room local={current_room} bridge={expected_room}"
                    )

            # 4) Remove resources that no longer exist on the bridge.
            for light_id in known_light_ids:
                if light_id not in bridge_light_ids:
                    self.remove_light(light_id)
                    mismatches.append(f"Light {light_id}: known locally but missing on bridge, removed")

            with self._lock:
                local_room_ids = list(self._rooms.keys())
            for room_id in local_room_ids:
                if room_id not in bridge_room_ids:
                    self.remove_room(room_id)
                    mismatches.append(f"Room {room_id}: known locally but missing on bridge, removed")

            with self._lock:
                local_zone_ids = list(self._zones.keys())
            for zone_id in local_zone_ids:
                if zone_id not in bridge_zone_ids:
                    self.remove_zone(zone_id)
                    mismatches.append(f"Zone {zone_id}: known locally but missing on bridge, removed")

        # 5) Scenes: register, sync active scene, remove stale entries.
        bridge_scene_ids = set()
        for scene in scenes:
            scene_id = scene.get('id')
            if not scene_id:
                continue
            bridge_scene_ids.add(scene_id)

            group_info = scene.get('group') or {}
            group_id = group_info.get('rid')
            group_type = group_info.get('rtype')
            if group_type not in ('room', 'zone') and group_id:
                if group_id in bridge_room_ids:
                    group_type = 'room'
                elif group_id in bridge_zone_ids:
                    group_type = 'zone'

            self.register_scene(scene_id, {
                'name': (scene.get('metadata') or {}).get('name', 'Unknown'),
                'room_id': group_id,
                'group_type': group_type or 'room'
            })

        with self._lock:
            local_scene_ids = list(self._scenes.keys())
        for scene_id in local_scene_ids:
            if scene_id not in bridge_scene_ids:
                self.remove_scene(scene_id)
                mismatches.append(f"Scene {scene_id}: known locally but missing on bridge, removed")

        for group_id in bridge_room_ids | bridge_zone_ids:
            is_zone = group_id in bridge_zone_ids
            bridge_scene_id = active_scene_by_group.get(group_id)
            with self._lock:
                group_state = (self._zones if is_zone else self._rooms).get(group_id, {})
                local_scene_id = group_state.get('current_scene_id')
            if local_scene_id == bridge_scene_id:
                continue
            mismatches.append(
                f"{'Zone' if is_zone else 'Room'} {group_id}: scene local={local_scene_id} bridge={bridge_scene_id}"
            )
            if is_zone:
                self.set_zone_scene(group_id, bridge_scene_id, old_scene_id=local_scene_id, source='reconcile')
            else:
                self.set_room_scene(group_id, bridge_scene_id, old_scene_id=local_scene_id, source='reconcile')

        return mismatches
    
    def get_current_state_summary(self) -> Dict:
        """Get a summary of the current state."""
        with self._lock:
            return {
                'total_lights': len(self._lights),
                'lights_on': sum(1 for l in self._lights.values() if l.get('on')),
                'total_rooms': len(self._rooms),
                'rooms_on': sum(1 for r in self._rooms.values() if r.get('is_on')),
                'total_zones': len(self._zones),
                'zones_on': sum(1 for z in self._zones.values() if z.get('is_on')),
                'total_scenes': len(self._scenes),
                'last_update': datetime.now().isoformat()
            }


# Global singleton instance
hue_state_manager = HueStateManager()
