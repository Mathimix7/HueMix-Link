"""
Automation engine for button events and lightstrip synchronization.

Handles button→Hue scene mapping, brightness control, and Hue→lightstrip color sync.
"""
import logging
import threading
import time
from typing import Dict, List, Tuple, Optional
from constants import ACT_CLICK, ACT_HOLDING, ACT_RELEASE, TIMEOUT_SCENE_CYCLE, FILE_LIGHTSTRIPS
from controllers.hue_controller import Hue
from controllers.color_controller import color_controller
from services.hue_state_manager import hue_state_manager
from services.data_manager import data_manager
from .device_manager import device_manager

logger = logging.getLogger(__name__)


class AutomationEngine:
    """Automation engine for button and lightstrip control."""
    
    def __init__(self, hue_controller: Hue):
        """Initialize automation engine.
        
        Args:
            hue_controller: Hue controller instance
        """
        self.hue = hue_controller
        self.network_server = None  # Set via set_network_server()
        
        # Button state tracking: mac -> {scene_index, last_press, brightness_direction}
        self._button_states: Dict[str, Dict] = {}
        self._button_lock = threading.RLock()
        
        # Scene cycling timeout
        self.scene_timeout = TIMEOUT_SCENE_CYCLE
        
        # Lightstrip sync debounce: room_id -> {'scene': Timer, 'room': Timer}
        self._lightstrip_timers: Dict[str, Dict[str, threading.Timer]] = {}
        self._timer_lock = threading.RLock()
        
        # Preview mode tracking: set of lightstrip MAC addresses currently in preview mode
        self._preview_mode_lightstrips: set = set()
        self._preview_lock = threading.RLock()
        
        # Running state
        self.running = False
        
        logger.info("AutomationEngine initialized")
    
    def set_network_server(self, network_server):
        """Set network server for sending lightstrip commands.
        
        Args:
            network_server: NetworkServer instance
        """
        self.network_server = network_server
    
    def start(self):
        """Start the automation engine."""
        if self.running:
            logger.warning("AutomationEngine already running")
            return
        
        self.running = True
        
        # Subscribe to Hue state changes
        hue_state_manager.subscribe_scene_changes(self._on_hue_scene_changed)
        hue_state_manager.subscribe_room_changes(self._on_hue_room_changed)
        
        logger.info("AutomationEngine started")
    
    def stop(self):
        """Stop the automation engine."""
        self.running = False
        
        # Unsubscribe from state changes
        hue_state_manager.unsubscribe_scene_changes(self._on_hue_scene_changed)
        hue_state_manager.unsubscribe_room_changes(self._on_hue_room_changed)
        
        logger.info("AutomationEngine stopped")
    
    # ===== Button Event Handling =====
    
    def handle_button_event(self, button_mac: str, action: int, rssi: int):
        """Handle button event.
        
        Args:
            button_mac: Button MAC address
            action: ACT_CLICK/ACT_HOLDING/ACT_RELEASE
            rssi: Signal strength
        """
        # Get button configuration
        button = device_manager.get_button_by_mac(button_mac)
        if not button:
            logger.warning(f"Unknown button: {button_mac}")
            return
        
        if not button.get('configured', False):
            logger.info(f"Button {button_mac} not configured yet")
            return
        
        config = button.get('config', {})
        room_id = config.get('room_id')
        
        if not room_id:
            logger.warning(f"Button {button_mac} has no room configured")
            return
        
        # Handle by action type
        if action == ACT_CLICK:
            self._handle_button_click(button_mac, button, room_id)
        
        elif action == ACT_HOLDING:
            self._handle_button_hold(button_mac, button, room_id)
        
        elif action == ACT_RELEASE:
            self._handle_button_release(button_mac, button, room_id)
    
    def _handle_button_click(self, button_mac: str, button: Dict, room_id: str):
        """Handle button click - cycle scenes or turn off.
        
        Args:
            button_mac: Button MAC address
            button: Button configuration
            room_id: Room ID to control
        """
        config = button.get('config', {})
        scenes = config.get('scenes', [])
        
        if not scenes:
            logger.warning(f"Button {button_mac} has no scenes configured")
            return
        
        # Check timeout and get next scene
        now = time.time()
        
        with self._button_lock:
            if button_mac not in self._button_states:
                self._button_states[button_mac] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1  # 1 = up, -1 = down
                }
            
            state = self._button_states[button_mac]
            
            # Check if timeout expired
            time_since_last = now - state['last_press']
            
            if time_since_last > self.scene_timeout:
                # Timeout expired - check if room is on using state manager
                room_state = hue_state_manager.get_room_state(room_id)
                room_is_on = room_state.get('is_on', False) if room_state else False
                
                if room_is_on:
                    # Turn off room
                    logger.info(f"Button {button_mac}: Timeout expired, turning off room {room_id}")
                    self._turn_off_room(room_id)
                    state['scene_index'] = 0
                    state['last_press'] = now
                    state['brightness_direction'] = -1
                    return
                else:
                    # Room is off, start from first scene
                    logger.debug(f"Button {button_mac}: Timeout expired, reset to scene 0")
                    state['scene_index'] = 0

            
            # Get current scene
            scene_id = scenes[state['scene_index']]
            
            # Advance to next scene
            state['scene_index'] = (state['scene_index'] + 1) % len(scenes)
            state['last_press'] = now
            state['brightness_direction'] = -1
            
            logger.info(f"Button {button_mac}: Activating scene {scene_id} in room {room_id}")
        
        # Activate scene
        try:
            self._activate_scene(room_id, scene_id)
        except Exception as e:
            logger.error(f"Failed to activate scene {scene_id}: {e}")
    
    def _handle_button_hold(self, button_mac: str, button: Dict, room_id: str):
        """Handle button hold - adjust brightness.
        
        Args:
            button_mac: Button MAC address
            button: Button configuration
            room_id: Room ID to control
        """
        with self._button_lock:
            if button_mac not in self._button_states:
                self._button_states[button_mac] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }
            
            state = self._button_states[button_mac]
            direction = state['brightness_direction']
        
        logger.info(f"Button {button_mac}: Adjusting brightness {'up' if direction > 0 else 'down'} in room {room_id}")
        
        try:
            self._adjust_room_brightness(room_id, direction)
        except Exception as e:
            logger.error(f"Failed to adjust brightness: {e}")
    
    def _handle_button_release(self, button_mac: str, button: Dict, room_id: str):
        """Handle button release - toggle brightness direction.
        
        Args:
            button_mac: Button MAC address
            button: Button configuration
            room_id: Room ID to control
        """
        with self._button_lock:
            if button_mac not in self._button_states:
                self._button_states[button_mac] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }
            
            state = self._button_states[button_mac]
            state['brightness_direction'] *= -1
            
            direction_str = 'up' if state['brightness_direction'] > 0 else 'down'
            logger.info(f"Button {button_mac}: Brightness direction toggled to {direction_str}")
    
    # ===== Hue Control =====
    
    def _activate_scene(self, room_id: str, scene_id: str):
        """Activate a scene in a room.
        
        Args:
            room_id: Room ID
            scene_id: Scene ID to activate
        """
        # Activate scene via API
        payload = {
            'recall': {'action': 'active'}
        }
        self.hue.set_scene(scene_id, payload)
        

        room_state = hue_state_manager.get_room_state(room_id)
        old_scene_id = room_state.get('current_scene_id') if room_state else None
        hue_state_manager.set_room_scene(room_id, scene_id, old_scene_id, source='button')
        
        logger.info(f"Activated scene {scene_id} in room {room_id}")
    
    def _turn_off_room(self, room_id: str):
        """Turn off all lights in a room.
        
        Args:
            room_id: Room ID
        """
        # Turn off room via grouped_light
        room_state = hue_state_manager.get_room_state(room_id)
        
        if not room_state:
            logger.error(f"Room {room_id} not found in state manager")
            return
        
        grouped_light_id = room_state.get('grouped_light_id')
        if grouped_light_id:
            payload = {'on': {'on': False}}
            self.hue._put_resource('grouped_light', grouped_light_id, payload)
        
        # Update state manager
        old_scene_id = room_state.get('current_scene_id')
        hue_state_manager.set_room_scene(room_id, None, old_scene_id, source='button')
        
        logger.info(f"Turned off room {room_id}")
    
    def _adjust_room_brightness(self, room_id: str, direction: int):
        """Adjust room brightness.
        
        Args:
            room_id: Room ID
            direction: 1 for increase, -1 for decrease
        """
        # Get room to find grouped_light
        room = self.hue.get_room(room_id)
        services = room.get('services', [])
        
        grouped_light_id = None
        for service in services:
            if service.get('rtype') == 'grouped_light':
                grouped_light_id = service.get('rid')
                break
        
        if not grouped_light_id:
            raise ValueError(f"No grouped_light found for room {room_id}")
        
        # Get current brightness
        grouped_light = self.hue.get_grouped_light(grouped_light_id)
        current_brightness = grouped_light.get('dimming', {}).get('brightness', 50.0)
        
        # Calculate new brightness (adjust by 10%)
        adjustment = 10.0 * direction
        new_brightness = max(1.0, min(100.0, current_brightness + adjustment))
        
        # Set new brightness
        payload = {
            'dimming': {'brightness': new_brightness}
        }
        self.hue._put_resource('grouped_light', grouped_light_id, payload)
        
        logger.debug(f"Room {room_id} brightness: {current_brightness:.1f}% -> {new_brightness:.1f}%")
    
    # ===== Lightstrip Synchronization =====
    
    def _on_hue_room_changed(self, room_id: str, room_state: Dict):
        """Handle room state change (on/off, brightness) - sync lightstrips with short debounce.
        
        Args:
            room_id: Room that changed
            room_state: New room state
        """
        logger.debug(f"Room state changed for {room_id}: is_on={room_state.get('is_on')}, brightness={room_state.get('avg_brightness')}")
        
        if not self.network_server:
            return
        
        # Get current scene for this room
        scene_id = room_state.get('current_scene_id')
        
        # Debounce room state changes (0.3s) to avoid spam when multiple lights update
        # This handles: turning on/off, brightness changes, etc.
        with self._timer_lock:
            if room_id not in self._lightstrip_timers:
                self._lightstrip_timers[room_id] = {}
            
            # Cancel ALL existing timers (room and scene) to avoid duplicates
            if 'room' in self._lightstrip_timers[room_id]:
                self._lightstrip_timers[room_id]['room'].cancel()
                logger.debug(f"Cancelled previous room state sync timer for room {room_id}")
            if 'scene' in self._lightstrip_timers[room_id]:
                self._lightstrip_timers[room_id]['scene'].cancel()
                logger.debug(f"Cancelled scene sync timer (superseded by room change) for room {room_id}")
            
            # Start new debounced timer (0.3 seconds for quick response)
            timer = threading.Timer(0.3, self._sync_lightstrips_for_room, args=(room_id, scene_id))
            self._lightstrip_timers[room_id]['room'] = timer
            timer.start()
            logger.debug(f"Started room state sync timer for room {room_id} (0.3s delay)")
    
    def _on_hue_scene_changed(self, room_id: str, scene_id: str, 
                             old_scene_id: Optional[str] = None, 
                             source: str = 'unknown'):
        """Handle Hue scene change - sync lightstrips with debounce.
        
        Args:
            room_id: Room where scene changed
            scene_id: New scene ID
            old_scene_id: Previous scene ID
            source: Source of change
        """
        logger.info(f"Scene changed in room {room_id}: {scene_id} (from {source})")
        
        # Only sync lightstrips when we get SSE confirmation (actual color change)
        # Skip button/web sources since colors haven't updated yet
        if source != 'sse':
            logger.debug(f"Skipping lightstrip sync for source '{source}', waiting for SSE confirmation")
            return
        
        if not self.network_server:
            logger.warning("NetworkServer not set, cannot sync lightstrips")
            return
        
        # Check if room is turning on/off by looking at scene_id changes
        # Turning on: old_scene_id is None and scene_id is not None
        # Turning off: scene_id is None (regardless of old_scene_id)
        is_turning_off = (scene_id is None)
        is_turning_on = (old_scene_id is None and scene_id is not None)
        
        if is_turning_off or is_turning_on:
            # Cancel any pending timers and sync immediately for on/off transitions
            with self._timer_lock:
                if room_id in self._lightstrip_timers:
                    for timer_type in ['room', 'scene']:
                        if timer_type in self._lightstrip_timers[room_id]:
                            self._lightstrip_timers[room_id][timer_type].cancel()
            
            logger.debug(f"Room {room_id} turning {'off' if is_turning_off else 'on'}, syncing lightstrips immediately")
            threading.Thread(target=self._sync_lightstrips_for_room, args=(room_id, scene_id), daemon=True).start()
        else:
            # Debounced sync for scene changes
            # Cancel existing timer for this room if any
            with self._timer_lock:
                if room_id not in self._lightstrip_timers:
                    self._lightstrip_timers[room_id] = {}
                
                # Cancel ALL existing timers (scene and room) to avoid duplicates
                if 'scene' in self._lightstrip_timers[room_id]:
                    self._lightstrip_timers[room_id]['scene'].cancel()
                    logger.debug(f"Cancelled previous scene sync timer for room {room_id}")
                if 'room' in self._lightstrip_timers[room_id]:
                    self._lightstrip_timers[room_id]['room'].cancel()
                    logger.debug(f"Cancelled room sync timer (superseded by scene change) for room {room_id}")
                
                # Start new debounced timer (1.5 seconds)
                timer = threading.Timer(1.5, self._sync_lightstrips_for_room, args=(room_id, scene_id))
                self._lightstrip_timers[room_id]['scene'] = timer
                timer.start()
                logger.debug(f"Started scene sync timer for room {room_id} (1.5s delay)")
    
    def _sync_lightstrips_for_room(self, room_id: str, scene_id: str):
        """Sync lightstrips for a room after debounce delay.
        
        Args:
            room_id: Room ID
            scene_id: Scene ID
        """
        try:
            # Find lightstrips for this room
            lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
            strips_for_room = [
                strip for strip in lightstrips
                if strip.get('room_id') == room_id and strip.get('mac_address')
            ]
            
            if not strips_for_room:
                logger.debug(f"No lightstrips for room {room_id}")
                return
            
            # Get brightness from room state
            room_state = hue_state_manager.get_room_state(room_id)
            brightness_pct = room_state.get('avg_brightness', 100.0) if room_state else 100.0
            brightness_val = int(brightness_pct * 2.55)  # Convert 0-100 to 0-255
            
            # Update each lightstrip
            for strip in strips_for_room:
                try:
                    light_mac = strip.get('mac_address')
                    
                    # Skip lightstrips in preview mode
                    with self._preview_lock:
                        if light_mac in self._preview_mode_lightstrips:
                            logger.debug(f"Skipping lightstrip {strip.get('name', light_mac)} - in preview mode")
                            continue
                    
                    rgb_data = self._get_lightstrip_colors(strip, scene_id, room_id)
                    if rgb_data is not None:
                        self.network_server.send_to_light(light_mac, rgb_data, brightness_val)
                        logger.info(f"Sent colors to lightstrip {strip.get('name', light_mac)} (brightness: {brightness_pct:.0f}%)")
                except Exception as e:
                    logger.error(f"Error syncing lightstrip {strip.get('id')}: {e}")
            
            # Clean up timer references (both scene and room timers may exist)
            with self._timer_lock:
                if room_id in self._lightstrip_timers:
                    # Remove completed timers
                    self._lightstrip_timers[room_id] = {
                        k: v for k, v in self._lightstrip_timers[room_id].items()
                        if v.is_alive()
                    }
                    # Remove room entry if no active timers
                    if not self._lightstrip_timers[room_id]:
                        del self._lightstrip_timers[room_id]
                    
        except Exception as e:
            logger.error(f"Error in lightstrip sync for room {room_id}: {e}")

    def send_current_colors_to_light(self, light_mac: str):
        """Send current scene colors to a light that just came online.
        
        Args:
            light_mac: Light MAC address
        """
        try:
            # Get light configuration
            lights = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
            light_config = None
            for light in lights:
                if light.get('mac_address') == light_mac:
                    light_config = light
                    break
            
            if not light_config:
                logger.debug(f"Light {light_mac} not found in config, skipping color update")
                return
            
            # Get room and check for active scene
            room_id = light_config.get('room_id')
            if not room_id:
                logger.debug(f"Light {light_mac} has no room assigned, skipping color update")
                return
            
            room_state = hue_state_manager.get_room_state(room_id)
            if not room_state or not room_state.get('current_scene_id'):
                logger.debug(f"Room {room_id} has no active scene, skipping color update")
                return
            
            # Get colors for current scene
            scene_id = room_state['current_scene_id']
            rgb_data = self._get_lightstrip_colors(light_config, scene_id, room_id)
            brightness_pct = room_state.get('avg_brightness', 100.0) if room_state else 100.0
            brightness_val = int(brightness_pct * 2.55)
            
            if rgb_data:
                # Send to light
                success = self.network_server.send_to_light(light_mac, rgb_data, brightness_val)
                if success:
                    logger.info(f"✨ Sent current scene colors to {light_mac} (scene: {scene_id})")
                else:
                    logger.warning(f"Failed to send colors to {light_mac}")
            
        except Exception as e:
            logger.error(f"Error sending current colors to {light_mac}: {e}", exc_info=True)
    
    def set_preview_mode(self, light_mac: str, enabled: bool):
        """Enable or disable preview mode for a lightstrip.
        
        When in preview mode, automatic updates are skipped.
        When disabled, the lightstrip is synced to the current scene.
        
        Args:
            light_mac: Lightstrip MAC address
            enabled: True to enable preview mode, False to disable
        """
        with self._preview_lock:
            if enabled:
                self._preview_mode_lightstrips.add(light_mac)
                logger.debug(f"Preview mode ENABLED for {light_mac}")
            else:
                self._preview_mode_lightstrips.discard(light_mac)
                logger.debug(f"Preview mode DISABLED for {light_mac}")
                
                # Sync the lightstrip to current scene colors when exiting preview mode
                if self.network_server:
                    threading.Thread(
                        target=self.send_current_colors_to_light, 
                        args=(light_mac,), 
                        daemon=True
                    ).start()
    
    def is_preview_mode(self, light_mac: str) -> bool:
        """Check if a lightstrip is in preview mode.
        
        Args:
            light_mac: Lightstrip MAC address
            
        Returns:
            True if in preview mode, False otherwise
        """
        with self._preview_lock:
            return light_mac in self._preview_mode_lightstrips
    
    # ===== Color Palette Extraction =====
    
    def _get_hue_light_palette(self, room_id: str, ignore_third_party: bool = False) -> List[Tuple[int, int, int]]:
        """Get RGB color palette from all lights in a room.
        
        Args:
            room_id: Room ID to get colors from
            ignore_third_party: If True, skip lights not in known Hue gamut list
            
        Returns:
            List of (r, g, b) tuples representing the room's color palette
        """
        # Get room state
        room_state = hue_state_manager.get_room_state(room_id)
        if not room_state or not room_state.get('is_on', False):
            return [(0, 0, 0)]
        
        # Get all light states in the room
        light_ids = room_state.get('lights', [])
        if not light_ids:
            return []
        
        # Collect colors from all lights that are on
        xy_colors = []  # List of (x, y, model_id) tuples
        ct_colors = []  # List of ct values
        
        for light_id in light_ids:
            light_state = hue_state_manager.get_light_state(light_id)
            if not light_state or not light_state.get('on'):
                continue
            
            model_id = light_state.get('model_id', None)

            # Check if we should skip third-party lights
            if ignore_third_party:
                known_models = (
                    'LST001', 'LLC010', 'LLC011', 'LLC012', 'LLC005', 'LLC006', 'LLC007', 'LLC013', 'LLC014',
                    'LCT001', 'LCT007', 'LCT002', 'LCT003', 'LLM001', 'LCA005',
                    'LCT010', 'LCT014', 'LCT015', 'LCT016', 'LCT011', 'LLC020', 'LST002', 'LCT012', 'LCL001', 'LCA003', "440400982841", "LTE001", "LTA008", "LTA010"
                )
                if model_id not in known_models:
                    logger.debug(f"Skipping third-party light {light_id} (model: {model_id})")
                    continue
            
            color_mode = light_state.get('color_mode')
            
            # Check if light has xy color
            if color_mode == 'xy' and 'xy' in light_state:
                xy = light_state['xy']
                if 'x' in xy and 'y' in xy:
                    xy_colors.append((xy['x'], xy['y'], model_id))
            
            # Check if light has color temperature
            elif color_mode == 'ct' and 'ct' in light_state:
                ct_colors.append(light_state['ct'])
        
        # Decide which color mode to use based on majority
        use_xy_mode = len(xy_colors) >= len(ct_colors)
        
        # Convert colors to RGB
        rgb_colors = []
        if use_xy_mode and xy_colors:
            # Use XY colors
            for x, y, model_id in xy_colors:
                rgb_dict = color_controller.xy_to_rgb(x, y, model_id)
                rgb_colors.append((rgb_dict['r'], rgb_dict['g'], rgb_dict['b']))
        elif ct_colors:
            # Use CT colors
            for ct in ct_colors:
                r, g, b = color_controller.ct_to_rgb(ct)
                rgb_colors.append((r, g, b))
        
        # Remove duplicates for multi-color mode
        if use_xy_mode and len(rgb_colors) >= 2:
            rgb_colors = list(dict.fromkeys(rgb_colors))
        
        return rgb_colors
    
    def _get_scene_override_colors(self, strip: Dict, scene_id: str) -> Optional[List[Tuple[int, int, int]]]:
        """Get color palette from scene override configuration if present.
        
        Args:
            strip: Lightstrip configuration
            scene_id: Scene ID to check for override
            
        Returns:
            List of (r, g, b) tuples representing the override palette, or None if no override.
            - 'off' type: returns [(0, 0, 0)]
            - 'single_color' type: returns [(r, g, b)]
            - 'multi_color' type: returns list of colors from config
        """
        # Check for scene override
        overrides = strip.get('overrides', {})
        if scene_id not in overrides:
            return None
        
        override = overrides[scene_id]
        override_type = override.get('type')
        
        if override_type == 'off':
            # Return black palette
            return [(0, 0, 0)]
        
        elif override_type == 'single_color':
            # Return single color palette
            color = override.get('color', {})
            r = color.get('r', 255)
            g = color.get('g', 255)
            b = color.get('b', 255)
            return [(r, g, b)]
        
        elif override_type == 'multi_color':
            # Return multi-color palette
            colors = override.get('colors', [])
            if not colors:
                return [(0, 0, 0)]
            
            # Convert to RGB tuples
            result = []
            for color in colors:
                r = color.get('r', 0)
                g = color.get('g', 0)
                b = color.get('b', 0)
                result.append((r, g, b))
            return result
        
        return None
    
    def _generate_strip_colors(self, strip: Dict, rgb_palette: List[Tuple[int, int, int]], num_leds: int) -> List[Tuple[int, int, int]]:
        """Generate strip colors from palette based on strip configuration.
        
        Args:
            strip: Lightstrip configuration
            rgb_palette: Color palette to use (from room lights or override)
            num_leds: Number of LEDs in the strip
            
        Returns:
            List of (r, g, b) tuples for the strip
        """
        if not rgb_palette:
            # No colors available, use warm white
            return [(255, 216, 94)] * num_leds
        
        mac_address = strip.get('mac_address')
        single_color = strip.get('single_color', True)
        coverage = strip.get('coverage', 1.5)
        distortion = strip.get('distortion', 0.3)
        
        # If palette has only 1 color, force single-color mode (e.g., overrides)
        if len(rgb_palette) == 1:
            return [rgb_palette[0]] * num_leds
        
        # Multiple colors available
        if single_color:
            # Single color mode - select one color based on seed
            numeric_seed = color_controller._ensure_int_seed(mac_address)
            index = numeric_seed % len(rgb_palette)
            selected_color = rgb_palette[index]
            return [selected_color] * num_leds
        else:
            # Multi-color mode - generate pattern with coverage and distortion
            strip_colors = color_controller.generate_strip(
                palette=rgb_palette,
                num_leds=num_leds,
                seed=mac_address,
                coverage=coverage,
                distortion=distortion
            )
            
            return strip_colors
    
    def _get_lightstrip_colors(self, strip: Dict, scene_id: str, room_id: str) -> Optional[List[Tuple[int, int, int]]]:
        """Get RGB colors for lightstrip based on scene.
        
        This is the main orchestrator that:
        1. Checks for scene overrides first
        2. Falls back to room light colors
        3. Generates the final strip pattern
        
        Args:
            strip: Lightstrip configuration
            scene_id: Scene ID
            room_id: Room ID
            
        Returns:
            List of (r, g, b) tuples or None
        """
        num_leds = strip.get('number_colors', 40)
        ignore_third_party = strip.get('ignore_third_party', False)

        room_state = hue_state_manager.get_room_state(room_id)
        if not room_state or not room_state.get('is_on', False):
            return self._generate_strip_colors(strip, [(0, 0, 0)], num_leds)
        
        # Step 1: Check for scene override palette
        palette = self._get_scene_override_colors(strip, scene_id)
        if not palette:
            palette = self._get_hue_light_palette(room_id, ignore_third_party)
            
        return self._generate_strip_colors(strip, palette, num_leds)