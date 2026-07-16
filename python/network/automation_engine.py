"""
Automation engine for button events and lightstrip synchronization.

Handles button→Hue scene mapping, brightness control, and Hue→lightstrip color sync.
"""
import logging
import threading
import time
from typing import Dict, List, Tuple, Optional
from constants import (
    ACT_CLICK,
    ACT_HOLDING,
    ACT_RELEASE,
    ACT_MOTION_DETECTED,
    ACT_DOOR_OPENED,
    ACT_DOOR_CLOSED,
    ACT_SYNC,
    TIMEOUT_SCENE_CYCLE,
    FILE_LIGHTSTRIPS,
    FILE_MOTION_SENSORS,
    FILE_DOOR_SENSORS,
    REMOTE_ACTION_NORMAL,
    REMOTE_ACTION_TOGGLE,
    REMOTE_ACTION_BRIGHTNESS_UP,
    REMOTE_ACTION_BRIGHTNESS_DOWN,
    REMOTE_ACTION_SCENE_CYCLE,
)
from controllers.hue_controller import Hue
from controllers.color_controller import color_controller
from services.hue_state_manager import hue_state_manager
from services.data_manager import data_manager
from .device_manager import device_manager
from .network_server import NetworkServer
from datetime import datetime

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
        
        # Motion sensor state tracking: mac -> {after_timer, room_id, expected_scene_id,
        # last_expected_scene_id, last_expected_clear_time, last_motion_activity_time}
        self._motion_states: Dict[str, Dict] = {}
        self._motion_lock = threading.RLock()
        self._motion_expected_cooldown_seconds = 4.0
        self._motion_room_change_grace_seconds = 3.0
        self._manual_brightness_cancel_threshold = 4.0
        # Sources that represent explicit non-motion manual/internal control changes.
        # Motion-origin scene changes are evaluated separately and should not auto-cancel.
        self._internal_timer_cancel_sources = {'button', 'web', 'remote', 'door'}

        # Previous room snapshot for classifying room change events.
        self._room_change_snapshots: Dict[str, Dict] = {}

        # Door sensor state tracking: mac -> {close_timer, close_timer_id}
        self._door_states: Dict[str, Dict] = {}
        self._door_lock = threading.RLock()
        
        # Room manual turn-off tracking: room_id -> timestamp (to prevent motion triggers after manual off)
        self._room_manual_off_times: Dict[str, float] = {}
        
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
    
    def set_network_server(self, network_server: NetworkServer):
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

        # Cancel pending delayed door close actions
        with self._door_lock:
            for state in self._door_states.values():
                timer = state.get('close_timer')
                if timer and timer.is_alive():
                    timer.cancel()
            self._door_states.clear()
        
        logger.info("AutomationEngine stopped")
    
    # ===== Button Event Handling =====
    
    def handle_button_event(self, button_mac: str, action: int, rssi: int, button_index: Optional[int] = None):
        """Handle button event.
        
        Args:
            button_mac: Button/Remote MAC address
            action: ACT_CLICK/ACT_HOLDING/ACT_RELEASE
            rssi: Signal strength
            button_index: For remote buttons, index of which button was pressed (0-3)
        """
        # Get button/remote configuration
        button = device_manager.get_button_by_mac(button_mac)
        if not button:
            logger.warning(f"Unknown button: {button_mac}")
            return
        
        if not button.get('configured', False):
            logger.info(f"Button {button_mac} not configured yet")
            return
        
        config = button.get('config', {})
        
        # Handle remote button events (detected by presence of button_index)
        if button_index is not None and button_index >= 0:
            self._handle_remote_button_event(button_mac, button, button_index, action)
            return
        
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
    
    def _handle_remote_button_event(self, remote_mac: str, remote: Dict, button_index: int, action: int):
        """Handle remote button press.
        
        Args:
            remote_mac: Remote MAC address
            remote: Remote configuration
            button_index: Index of pressed button (0-3)
            action: ACT_CLICK or ACT_HOLDING
        """
        config = remote.get('config', {})
        buttons_config = config.get('buttons', [])
        
        # Find config for this button
        button_config = None
        for btn in buttons_config:
            if btn.get('index') == button_index:
                button_config = btn
                break
        
        if not button_config:
            logger.warning(f"Remote {remote_mac} button {button_index} has no configuration")
            return
        
        action_type = button_config.get('action_type', None)
        room_id = button_config.get('room_id')
        
        action_str = {ACT_CLICK: "CLICK", ACT_HOLDING: "HOLDING", ACT_RELEASE: "RELEASE", ACT_SYNC: "SYNC"}.get(action, f"UNKNOWN({action})")
        logger.info(f"Remote {remote_mac} button {button_index}: {action_str} -> {action_type} action in room {room_id}")
        
        if not room_id:
            logger.warning(f"Remote {remote_mac} button {button_index} has no room configured")
            return
        
        # Handle different action types
        if action_type == REMOTE_ACTION_NORMAL:
            # CLICK = scene cycle, HOLDING = brightness adjust, RELEASE = toggle direction
            if action == ACT_CLICK:
                self._handle_remote_normal_action_click(remote_mac, button_config, room_id)
            elif action == ACT_HOLDING:
                self._handle_remote_normal_action_holding(remote_mac, button_config, room_id)
            elif action == ACT_RELEASE:
                self._handle_remote_normal_action_release(remote_mac, button_config)

        elif action_type == REMOTE_ACTION_SCENE_CYCLE:
            # Click cycles scenes but never turns the room off on timeout
            if action == ACT_CLICK:
                self._handle_remote_scene_cycle_click(remote_mac, button_config, room_id)
            elif action == ACT_HOLDING:
                # Preserve brightness hold behavior
                self._handle_remote_normal_action_holding(remote_mac, button_config, room_id)
            elif action == ACT_RELEASE:
                self._handle_remote_normal_action_release(remote_mac, button_config)
        
        elif action_type == REMOTE_ACTION_TOGGLE:
            # Only respond to CLICK, ignore HOLDING
            if action == ACT_CLICK:
                self._handle_remote_toggle_action(room_id)
        
        elif action_type == REMOTE_ACTION_BRIGHTNESS_UP:
            # Both CLICK and HOLDING do the same - increase brightness
            if action in (ACT_CLICK, ACT_HOLDING):
                try:
                    self._adjust_room_brightness(room_id, 1)
                except Exception as e:
                    logger.error(f"Failed to increase brightness: {e}")
        
        elif action_type == REMOTE_ACTION_BRIGHTNESS_DOWN:
            # Both CLICK and HOLDING do the same - decrease brightness
            if action in (ACT_CLICK, ACT_HOLDING):
                try:
                    self._adjust_room_brightness(room_id, -1)
                except Exception as e:
                    logger.error(f"Failed to decrease brightness: {e}")
    
    def _handle_remote_normal_action_click(self, remote_mac: str, button_config: Dict, room_id: str):
        """Handle normal remote button CLICK action (scene cycling).
        
        Args:
            remote_mac: Remote MAC address
            button_config: Button configuration
            room_id: Room ID to control
        """
        scenes = button_config.get('scenes', [])
        
        if not scenes:
            logger.warning(f"Remote {remote_mac} button has no scenes configured")
            return
        
        # Use remote_mac + button_index as state key
        button_index = button_config.get('index', 0)
        state_key = f"{remote_mac}_{button_index}"
        
        now = time.time()
        
        with self._button_lock:
            if state_key not in self._button_states:
                self._button_states[state_key] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }
            
            state = self._button_states[state_key]
            
            # Check if timeout expired
            time_since_last = now - state['last_press']
            
            if time_since_last > self.scene_timeout:
                # Timeout expired - turn off room
                room_is_on = self._is_room_currently_on(room_id)
                
                if room_is_on:
                    logger.info(f"Remote {remote_mac}: Timeout expired, turning off room {room_id}")
                    self._turn_off_room(room_id)
                    state['scene_index'] = 0
                    state['last_press'] = now
                    return
                else:
                    logger.debug(f"Remote {remote_mac}: Timeout expired, reset to scene 0")
                    state['scene_index'] = 0
            
            # Get current scene
            scene_id = scenes[state['scene_index']]
            
            # Advance to next scene
            state['scene_index'] = (state['scene_index'] + 1) % len(scenes)
            state['last_press'] = now
            
            logger.info(f"Remote {remote_mac} button {button_index}: Activating scene {scene_id}")
        
        # Activate scene
        try:
            self._activate_scene(room_id, scene_id)
        except Exception as e:
            logger.error(f"Failed to activate scene {scene_id}: {e}")

    def _handle_remote_scene_cycle_click(self, remote_mac: str, button_config: Dict, room_id: str):
        """Handle scene-cycle-only remote CLICK action (cycle scenes but never turn off).

        Args:
            remote_mac: Remote MAC address
            button_config: Button configuration
            room_id: Room ID to control
        """
        scenes = button_config.get('scenes', [])

        if not scenes:
            logger.warning(f"Remote {remote_mac} button has no scenes configured")
            return

        button_index = button_config.get('index', 0)
        state_key = f"{remote_mac}_{button_index}"

        now = time.time()

        with self._button_lock:
            if state_key not in self._button_states:
                self._button_states[state_key] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }

            state = self._button_states[state_key]

            # If timeout expired, simply reset to first scene (do NOT turn off room)
            time_since_last = now - state['last_press']
            if time_since_last > self.scene_timeout:
                logger.debug(f"Remote {remote_mac}: Timeout expired, reset to scene 0 (no off)")
                state['scene_index'] = 0

            # Get current scene
            scene_id = scenes[state['scene_index']]

            # Advance to next scene
            state['scene_index'] = (state['scene_index'] + 1) % len(scenes)
            state['last_press'] = now
            logger.info(f"Remote {remote_mac} button {button_index}: Activating scene {scene_id} (scene-cycle only)")

        try:
            self._activate_scene(room_id, scene_id)
        except Exception as e:
            logger.error(f"Failed to activate scene {scene_id}: {e}")
    
    def _handle_remote_normal_action_holding(self, remote_mac: str, button_config: Dict, room_id: str):
        """Handle normal remote button HOLDING action (brightness adjustment).
        
        Args:
            remote_mac: Remote MAC address
            button_config: Button configuration
            room_id: Room ID to control
        """
        # Use remote_mac + button_index as state key
        button_index = button_config.get('index', 0)
        state_key = f"{remote_mac}_{button_index}"
        
        with self._button_lock:
            if state_key not in self._button_states:
                self._button_states[state_key] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }
            
            state = self._button_states[state_key]
            logger.info(f"Remote {remote_mac} button {button_index}: Brightness {'UP' if state['brightness_direction'] > 0 else 'DOWN'}")
        
        # Adjust brightness
        try:
            self._adjust_room_brightness(room_id, state['brightness_direction'])
        except Exception as e:
            logger.error(f"Failed to adjust brightness: {e}")
    
    def _handle_remote_normal_action_release(self, remote_mac: str, button_config: Dict):
        """Handle normal remote button RELEASE action (toggle brightness direction).
        
        Args:
            remote_mac: Remote MAC address
            button_config: Button configuration
        """
        # Use remote_mac + button_index as state key
        button_index = button_config.get('index', 0)
        state_key = f"{remote_mac}_{button_index}"
        
        with self._button_lock:
            if state_key not in self._button_states:
                self._button_states[state_key] = {
                    'scene_index': 0,
                    'last_press': 0,
                    'brightness_direction': -1
                }
            
            state = self._button_states[state_key]
            state['brightness_direction'] *= -1
            
            direction_str = 'UP' if state['brightness_direction'] > 0 else 'DOWN'
            logger.info(f"Remote {remote_mac} button {button_index}: Brightness direction toggled to {direction_str}")
    
    def _handle_remote_toggle_action(self, room_id: str):
        """Handle toggle action - turn room on/off.
        
        Args:
            room_id: Room ID to toggle
        """
        try:
            room_is_on = self._is_room_currently_on(room_id)
            
            if room_is_on:
                logger.info(f"Remote toggle: Turning off room {room_id}")
                self._turn_off_room(room_id, source='remote')
            else:
                logger.info(f"Remote toggle: Turning on room {room_id}")
                self._turn_on_room(room_id, source='remote')
        except Exception as e:
            logger.error(f"Failed to toggle room: {e}")
    
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
                room_is_on = self._is_room_currently_on(room_id)
                
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
    
    def _activate_scene(self, room_id: str, scene_id: str, source: str = 'button'):
        """Activate a scene in a room.
        
        Args:
            room_id: Room ID
            scene_id: Scene ID to activate
            source: Source of activation ('button', 'motion', 'web', etc.)
        """
        # Non-motion HuemixLink scene changes should immediately cancel motion timers.
        if source != 'motion':
            self._cancel_motion_timers_for_room(room_id)

        # Activate scene via API
        payload = {
            'recall': {'action': 'active'}
        }
        self.hue.set_scene(scene_id, payload)
        

        room_state = hue_state_manager.get_room_state(room_id)
        old_scene_id = room_state.get('current_scene_id') if room_state else None
        hue_state_manager.set_room_scene(room_id, scene_id, old_scene_id, source=source)
        
        logger.info(f"Activated scene {scene_id} in room {room_id}")

    def _resolve_grouped_light_id(self, room_id: str) -> Optional[str]:
        """Resolve grouped_light ID for a room, hydrating state manager on cache miss."""
        room_state = hue_state_manager.get_room_state(room_id)
        if room_state:
            grouped_light_id = room_state.get('grouped_light_id')
            if grouped_light_id:
                return grouped_light_id

        try:
            room = self.hue.get_room(room_id)
        except Exception as e:
            logger.error(f"Failed to fetch room {room_id} while resolving grouped_light: {e}")
            return None

        room_name = room.get('metadata', {}).get('name')
        grouped_light_id = None
        for service in room.get('services', []):
            if service.get('rtype') == 'grouped_light':
                grouped_light_id = service.get('rid')
                break

        if not grouped_light_id:
            logger.error(f"No grouped_light found for room {room_id}")
            return None

        hue_state_manager.update_room(
            room_id=room_id,
            name=room_name,
            grouped_light_id=grouped_light_id,
        )
        return grouped_light_id

    def _is_room_currently_on(self, room_id: str) -> bool:
        """Get room on/off state, falling back to live bridge query when cache is incomplete."""
        room_state = hue_state_manager.get_room_state(room_id)
        has_mapping = bool(room_state and room_state.get('grouped_light_id'))

        if has_mapping and room_state.get('is_on') is not None:
            return bool(room_state.get('is_on'))

        try:
            is_on = self.hue.is_room_on(room_id)
            hue_state_manager.update_room(room_id=room_id, is_on=is_on)
            return bool(is_on)
        except Exception as e:
            logger.warning(f"Failed to query live room state for {room_id}: {e}")
            return bool(room_state.get('is_on', False)) if room_state else False
    
    def _turn_off_room(self, room_id: str, source: str = 'button'):
        """Turn off all lights in a room.
        
        Args:
            room_id: Room ID
            source: Source of turn-off action ('button', 'motion', 'door', etc.)
        """
        # Non-motion HuemixLink room changes should immediately cancel motion timers.
        if source != 'motion':
            self._cancel_motion_timers_for_room(room_id)

        # Turn off room via grouped_light
        room_state = hue_state_manager.get_room_state(room_id)
        grouped_light_id = self._resolve_grouped_light_id(room_id)
        if not grouped_light_id:
            logger.error(f"Cannot turn off room {room_id}: grouped_light could not be resolved")
            return

        payload = {'on': {'on': False}}
        self.hue._put_resource('grouped_light', grouped_light_id, payload)

        # Update state manager
        old_scene_id = room_state.get('current_scene_id') if room_state else None
        hue_state_manager.set_room_scene(room_id, None, old_scene_id, source=source)
        
        logger.info(f"Turned off room {room_id}")
    
    def _turn_on_room(self, room_id: str, source: str = 'button'):
        """Turn on all lights in a room.
        
        Args:
            room_id: Room ID
            source: Source of turn-on action ('button', 'remote', etc.)
        """
        # Non-motion HuemixLink room changes should immediately cancel motion timers.
        if source != 'motion':
            self._cancel_motion_timers_for_room(room_id)

        # Turn on room via grouped_light
        grouped_light_id = self._resolve_grouped_light_id(room_id)
        if not grouped_light_id:
            logger.error(f"Cannot turn on room {room_id}: grouped_light could not be resolved")
            return

        payload = {'on': {'on': True}}
        self.hue._put_resource('grouped_light', grouped_light_id, payload)
        
        logger.info(f"Turned on room {room_id}")
    
    def _adjust_room_brightness(self, room_id: str, direction: int):
        """Adjust room brightness.
        
        Args:
            room_id: Room ID
            direction: 1 for increase, -1 for decrease
        """
        # Local brightness changes from buttons/remotes are explicit manual intent.
        # Cancel pending motion timers immediately rather than waiting for SSE updates.
        self._cancel_motion_timers_for_room(room_id)

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
    
    def _dim_room_brightness(self, room_id: str, dim_percentage: float = 0.5):
        """Dim room brightness by a percentage.
        
        Args:
            room_id: Room ID
            dim_percentage: Percentage to dim (0.5 = 50% brightness)
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
        
        # Calculate dimmed brightness
        new_brightness = max(1.0, current_brightness * dim_percentage)
        
        # Set new brightness
        payload = {
            'dimming': {'brightness': new_brightness}
        }
        self.hue._put_resource('grouped_light', grouped_light_id, payload)
        
        logger.info(f"Room {room_id} dimmed: {current_brightness:.1f}% -> {new_brightness:.1f}%")
    
    # ===== Motion Sensor Event Handling =====

    def _get_motion_after_duration_seconds(self, time_slot: Dict) -> int:
        """Get after-action delay in seconds for a motion time slot.

        Supports both the new second-based field and legacy minute-based configs.
        """
        raw_seconds = time_slot.get('after_duration_seconds')
        try:
            if raw_seconds is not None:
                explicit_seconds = int(str(raw_seconds).strip())
                return max(0, explicit_seconds)
        except (TypeError, ValueError):
            pass

        # Backward compatibility: older configs stored this duration in minutes.
        raw_minutes = time_slot.get('after_duration', 5)
        try:
            legacy_minutes = int(str(raw_minutes).strip())
            return max(0, legacy_minutes) * 60
        except (TypeError, ValueError):
            pass

        return 300

    @staticmethod
    def _get_motion_warning_lead_seconds(after_duration: int) -> int:
        """Get dim-warning lead time based on total after-duration.

        Rules:
        - >= 60s: warn 15s before
        - >= 30s: warn 10s before
        - < 30s: warn 5s before
        """
        if after_duration >= 60:
            return 15
        if after_duration >= 30:
            return 10
        return 5
    
    def handle_motion_event(self, sensor_mac: str, action: int, light_level: Optional[int] = None, battery_mv: Optional[int] = None):
        """Handle motion sensor event.
        
        Args:
            sensor_mac: Motion sensor MAC address
            action: 10 for MOTION_DETECTED, 9 for SYNC
            light_level: Ambient light level (0-10)
            battery_mv: Battery voltage in millivolts
        """
        # Only handle actual motion events, not sync
        if action != ACT_MOTION_DETECTED:  # 10 = MOTION_DETECTED
            return
        
        # Get sensor configuration
        sensors = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
        sensor = None
        for s in sensors:
            if s.get('mac_address', '').upper() == sensor_mac.upper():
                sensor = s
                break
        
        if not sensor:
            logger.debug(f"Motion detected but sensor {sensor_mac} not configured")
            return
        
        config = sensor.get('config', {})
        
        # Check if sensor is enabled
        if not config.get('enabled', True):
            logger.debug(f"Motion sensor {sensor_mac} is disabled, ignoring event")
            return
        
        # Get room_id from config (needed for timer check)
        room_id = config.get('room_id')
        if not room_id:
            logger.debug(f"Motion sensor {sensor_mac} has no room assigned")
            return
        
        # Check if lights were manually turned off recently (within last 10 seconds)
        # Send one-time sleep command to prevent sensor from wasting its cooldown period
        with self._motion_lock:
            if room_id in self._room_manual_off_times:
                time_since_manual_off = time.time() - self._room_manual_off_times[room_id]
                if time_since_manual_off < 10.0:
                    # Calculate remaining sleep time (round up to nearest second)
                    remaining_seconds = int(10.0 - time_since_manual_off) + 1
                    logger.info(f"Motion sensor {sensor_mac}: lights manually turned off {time_since_manual_off:.1f}s ago, sending sleep command for {remaining_seconds}s")
                    
                    # Send one-time sleep command to the sensor
                    if self.network_server:
                        self.network_server._send_motion_sleep_command(sensor_mac, remaining_seconds)
                    
                    return
        
        # Check if there's an active timer from a previous motion event
        # If there is, we should restart it even if conditions prevent new action
        has_active_timer = False
        with self._motion_lock:
            if sensor_mac in self._motion_states:
                if 'after_timer' in self._motion_states[sensor_mac]:
                    timer = self._motion_states[sensor_mac]['after_timer']
                    if timer and timer.is_alive():
                        has_active_timer = True
        
        # Check light sensitivity threshold
        light_sensitivity = config.get('light_sensitivity', 5)
        if light_level is not None and light_level > light_sensitivity:
            if has_active_timer:
                logger.debug(f"Motion sensor {sensor_mac} light level too high ({light_level} > {light_sensitivity}), but restarting existing timer")
            else:
                logger.debug(f"Motion sensor {sensor_mac} light level too high ({light_level} > {light_sensitivity})")
                return
        
        # Find current time slot
        time_slots = config.get('time_slots', [])
        current_slot = self._get_current_time_slot(time_slots)
        
        if not current_slot:
            logger.debug(f"Motion sensor {sensor_mac} has no matching time slot")
            return
        
        # Check "Do Not Disturb" mode
        if current_slot.get('do_not_disturb', False):
            # Don't activate if lights are already on
            room_state = hue_state_manager.get_room_state(room_id)
            if room_state and room_state.get('is_on', False):
                if has_active_timer:
                    logger.debug(f"Motion sensor {sensor_mac} DND mode: lights already on, but restarting existing timer")
                else:
                    logger.debug(f"Motion sensor {sensor_mac} DND mode: lights already on, ignoring")
                    return
        
        # Execute motion action
        motion_action = current_slot.get('motion_action', 'nothing')
        action_executed = False
        
        if motion_action == 'scene':
            scene_id = current_slot.get('scene_id')
            if scene_id:
                logger.info(f"🏃 Motion sensor {sensor_mac} activating scene {scene_id} in room {room_id}")
                
                # Store expected scene before activation
                with self._motion_lock:
                    if sensor_mac not in self._motion_states:
                        self._motion_states[sensor_mac] = {}
                    self._motion_states[sensor_mac]['expected_scene_id'] = scene_id
                
                try:
                    self._activate_scene(room_id, scene_id, source='motion')
                    action_executed = True
                except Exception as e:
                    logger.error(f"Failed to activate scene {scene_id}: {e}")
                    # Clear expected scene if activation failed
                    with self._motion_lock:
                        if sensor_mac in self._motion_states:
                            self._motion_states[sensor_mac].pop('expected_scene_id', None)
        
        # Schedule/restart after-action timer if:
        # 1. Motion action was executed, OR
        # 2. There's already an active timer (extend it even if conditions prevent new action)
        if action_executed or has_active_timer:
            with self._motion_lock:
                if sensor_mac not in self._motion_states:
                    self._motion_states[sensor_mac] = {}
                
                # Cancel any existing after timer
                if 'after_timer' in self._motion_states[sensor_mac]:
                    old_timer = self._motion_states[sensor_mac]['after_timer']
                    if old_timer:
                        old_timer.cancel()
                        if not action_executed:
                            logger.debug(f"Restarting existing timer for {sensor_mac} due to continued motion")
                
                # Store room_id for timer cancellation
                self._motion_states[sensor_mac]['room_id'] = room_id
                self._motion_states[sensor_mac]['last_motion_activity_time'] = time.time()
                
                # Schedule after action
                after_duration = self._get_motion_after_duration_seconds(current_slot)
                after_action = current_slot.get('after_action', 'off')
                
                if after_action != 'nothing' and after_duration > 0:
                    # For OFF actions, always use a dim warning with dynamic lead time.
                    use_dim_warning = (after_action == 'off' and after_duration > 5)
                    
                    if use_dim_warning:
                        warning_lead = self._get_motion_warning_lead_seconds(after_duration)
                        warning_delay = max(0, after_duration - warning_lead)
                        timer = threading.Timer(
                            warning_delay,
                            self._execute_motion_dim_warning,
                            args=(sensor_mac, room_id, after_action, current_slot, warning_lead)
                        )
                        timer.daemon = True
                        timer.start()
                        self._motion_states[sensor_mac]['after_timer'] = timer
                        if action_executed:
                            logger.debug(
                                f"Scheduled dim warning for {sensor_mac} in {warning_delay}s "
                                f"(off in {after_duration}s, lead {warning_lead}s)"
                            )
                        else:
                            logger.debug(
                                f"Restarted dim warning timer for {sensor_mac} "
                                f"({warning_delay}s, lead {warning_lead}s)"
                            )
                    else:
                        # Schedule normal after action
                        timer = threading.Timer(
                            after_duration,
                            self._execute_motion_after_action,
                            args=(sensor_mac, room_id, after_action, current_slot)
                        )
                        timer.daemon = True
                        timer.start()
                        self._motion_states[sensor_mac]['after_timer'] = timer
                        if action_executed:
                            logger.debug(f"Scheduled after action for {sensor_mac} in {after_duration}s")
                        else:
                            logger.debug(f"Restarted after action timer for {sensor_mac} ({after_duration}s)")

    # ===== Door Sensor Event Handling =====

    def handle_door_event(self, sensor_mac: str, action: int, light_level: Optional[int] = None,
                          battery_mv: Optional[int] = None):
        """Handle door sensor event and execute configured slot action.

        Args:
            sensor_mac: Door sensor MAC address
            action: ACT_DOOR_OPENED, ACT_DOOR_CLOSED, or ACT_SYNC
            light_level: Ambient light level (0-10)
            battery_mv: Battery voltage in millivolts
        """
        if action not in (ACT_DOOR_OPENED, ACT_DOOR_CLOSED):
            return

        sensors = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
        sensor = None
        for item in sensors:
            if item.get('mac_address', '').upper() == sensor_mac.upper():
                sensor = item
                break

        if not sensor:
            logger.debug(f"Door event ignored: sensor {sensor_mac} not configured")
            return

        config = sensor.get('config', {})
        if not config.get('enabled', True):
            logger.debug(f"Door sensor {sensor_mac} is disabled, ignoring event")
            return

        room_id = config.get('room_id')
        if not room_id:
            logger.debug(f"Door sensor {sensor_mac} has no room assigned")
            return

        if action == ACT_DOOR_OPENED:
            self._cancel_pending_door_close_timer(sensor_mac, reason='door opened')
        else:
            self._cancel_pending_door_close_timer(sensor_mac, reason='new close event received')

        light_sensitivity = config.get('light_sensitivity', 5)
        try:
            light_sensitivity = int(light_sensitivity)
        except (TypeError, ValueError):
            light_sensitivity = 5

        light_sensitivity = max(0, min(10, light_sensitivity))

        current_slot = self._get_current_time_slot(config.get('time_slots', []))
        if not current_slot:
            logger.debug(f"Door sensor {sensor_mac} has no matching time slot")
            return

        if action == ACT_DOOR_OPENED:
            event_name = 'opened'
            action_key = 'open_action'
            scene_key = 'open_scene_id'
        else:
            event_name = 'closed'
            action_key = 'close_action'
            scene_key = 'close_scene_id'

        door_action = current_slot.get(action_key, 'nothing')
        close_delay_seconds = 0
        if action == ACT_DOOR_CLOSED:
            close_delay_seconds = self._normalize_close_delay_seconds(
                current_slot.get('close_delay_seconds', 0)
            )

        if door_action == 'nothing':
            logger.debug(f"Door sensor {sensor_mac} event {event_name}: slot action is 'nothing'")
            return

        scene_id = current_slot.get(scene_key) if door_action == 'scene' else None

        if door_action == 'scene' and light_level is not None and light_level > light_sensitivity:
            logger.debug(
                f"Door sensor {sensor_mac} light level too high "
                f"({light_level} > {light_sensitivity}), ignoring scene action"
            )
            return

        if door_action == 'scene' and current_slot.get('do_not_disturb', False):
            room_state = hue_state_manager.get_room_state(room_id)
            if room_state and room_state.get('is_on', False):
                logger.debug(
                    f"Door sensor {sensor_mac} DND mode: lights already on, skipping scene activation"
                )
                return

        if door_action == 'scene' and not scene_id:
            logger.warning(
                f"Door sensor {sensor_mac} event {event_name}: "
                f"missing scene id for action '{action_key}'"
            )
            return

        if action == ACT_DOOR_CLOSED and close_delay_seconds > 0:
            self._schedule_delayed_door_close_action(
                sensor_mac,
                room_id,
                door_action,
                scene_id,
                close_delay_seconds,
            )
            return

        self._execute_door_slot_action(sensor_mac, room_id, event_name, door_action, scene_id)

    @staticmethod
    def _normalize_close_delay_seconds(raw_delay: object) -> int:
        """Normalize close delay to supported bounds."""
        try:
            delay_seconds = int(raw_delay)
        except (TypeError, ValueError):
            return 0

        return max(0, min(86400, delay_seconds))

    def _cancel_pending_door_close_timer(self, sensor_mac: str, reason: Optional[str] = None):
        """Cancel any pending delayed close action for a door sensor."""
        cancelled = False
        with self._door_lock:
            state = self._door_states.get(sensor_mac)
            if not state:
                return

            timer = state.get('close_timer')
            if timer and timer.is_alive():
                timer.cancel()
                cancelled = True

            state['close_timer'] = None
            state['close_timer_id'] = int(state.get('close_timer_id', 0)) + 1

        if cancelled:
            if reason:
                logger.debug(f"Cancelled pending delayed close action for {sensor_mac}: {reason}")
            else:
                logger.debug(f"Cancelled pending delayed close action for {sensor_mac}")

    def _schedule_delayed_door_close_action(
        self,
        sensor_mac: str,
        room_id: str,
        door_action: str,
        scene_id: Optional[str],
        delay_seconds: int,
    ):
        """Schedule door close action after configured delay."""
        with self._door_lock:
            state = self._door_states.setdefault(sensor_mac, {})

            old_timer = state.get('close_timer')
            if old_timer and old_timer.is_alive():
                old_timer.cancel()

            timer_id = int(state.get('close_timer_id', 0)) + 1
            state['close_timer_id'] = timer_id

            timer = threading.Timer(
                delay_seconds,
                self._execute_delayed_door_close_action,
                args=(sensor_mac, timer_id, room_id, door_action, scene_id, delay_seconds),
            )
            timer.daemon = True
            timer.start()
            state['close_timer'] = timer

        if door_action == 'scene' and scene_id:
            action_desc = f"activate scene {scene_id}"
        elif door_action == 'off':
            action_desc = "turn off room"
        else:
            action_desc = door_action

        logger.info(
            f"🚪 Door sensor {sensor_mac} closed: scheduled action '{action_desc}' "
            f"in room {room_id} after {delay_seconds}s"
        )

    def _execute_delayed_door_close_action(
        self,
        sensor_mac: str,
        timer_id: int,
        room_id: str,
        door_action: str,
        scene_id: Optional[str],
        delay_seconds: int,
    ):
        """Execute a previously scheduled delayed close action."""
        with self._door_lock:
            state = self._door_states.get(sensor_mac)
            if not state or state.get('close_timer_id') != timer_id:
                logger.debug(f"Ignoring stale delayed close action for {sensor_mac}")
                return

        logger.info(
            f"🚪 Door sensor {sensor_mac} closed: executing delayed action after {delay_seconds}s"
        )
        self._execute_door_slot_action(sensor_mac, room_id, 'closed (delayed)', door_action, scene_id)

        with self._door_lock:
            state = self._door_states.get(sensor_mac)
            if state and state.get('close_timer_id') == timer_id:
                state['close_timer'] = None

    def _execute_door_slot_action(
        self,
        sensor_mac: str,
        room_id: str,
        event_name: str,
        door_action: str,
        scene_id: Optional[str],
    ):
        """Execute resolved door slot action immediately."""
        try:
            if door_action == 'scene':
                logger.info(
                    f"🚪 Door sensor {sensor_mac} {event_name}: "
                    f"activating scene {scene_id} in room {room_id}"
                )
                self._activate_scene(room_id, scene_id, source='door')
            elif door_action == 'off':
                logger.info(f"🚪 Door sensor {sensor_mac} {event_name}: turning off room {room_id}")
                self._turn_off_room(room_id, source='door')
            else:
                logger.warning(
                    f"Door sensor {sensor_mac} event {event_name}: unsupported action '{door_action}'"
                )
        except Exception as e:
            logger.error(f"Failed handling door event for {sensor_mac}: {e}")

    
    def _get_current_time_slot(self, time_slots: List[Dict]) -> Optional[Dict]:
        """Find the time slot that matches the current time.
        
        Args:
            time_slots: List of time slot configurations
            
        Returns:
            Matching time slot or None
        """
        if not time_slots:
            return None
                
        # Get current time
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        
        # Sort slots by start time
        sorted_slots = sorted(time_slots, key=lambda x: x.get('start_time', '00:00'))
        
        # Find matching slot
        for i, slot in enumerate(sorted_slots):
            start_time = slot.get('start_time', '00:00')
            
            # This slot is active if current time >= start_time
            # and current time < next slot's start time (or 24:00 if last slot)
            if current_time >= start_time:
                # Check if there's a next slot
                if i + 1 < len(sorted_slots):
                    next_start = sorted_slots[i + 1].get('start_time', '00:00')
                    if current_time < next_start:
                        return slot
                else:
                    # Last slot - active until midnight or until first slot
                    return slot
        
        # Handle wrap-around case: current time is before first slot
        # Check if last slot wraps around
        if sorted_slots:
            last_slot = sorted_slots[-1]
            first_slot = sorted_slots[0]
            if current_time < first_slot.get('start_time', '00:00'):
                # We're before the first slot, so last slot is active (wraps around midnight)
                return last_slot
        
        return None
    
    def _execute_motion_dim_warning(self, sensor_mac: str, room_id: str, after_action: str, time_slot: Dict,
                                    warning_lead_seconds: int):
        """Execute dim warning before turning off lights.
        
        Args:
            sensor_mac: Motion sensor MAC address
            room_id: Room ID
            after_action: 'off' (should always be 'off' for dim warning)
            time_slot: Time slot configuration
            warning_lead_seconds: Delay between dim warning and final off action
        """
        try:
            # Set last_expected_clear_time BEFORE dimming to prevent timer cancellation
            with self._motion_lock:
                if sensor_mac in self._motion_states:
                    self._motion_states[sensor_mac]['last_expected_clear_time'] = time.time()
                    self._motion_states[sensor_mac]['last_motion_activity_time'] = time.time()
            
            # Dim lights to 50% as warning
            logger.info(
                f"⚠️  Motion sensor {sensor_mac} dimming lights in room {room_id} "
                f"(warning: off in {warning_lead_seconds}s)"
            )
            self._dim_room_brightness(room_id, dim_percentage=0.5)
            
            # Schedule final off action using the configured warning lead.
            with self._motion_lock:
                if sensor_mac in self._motion_states:
                    timer = threading.Timer(
                        warning_lead_seconds,
                        self._execute_motion_after_action,
                        args=(sensor_mac, room_id, after_action, time_slot)
                    )
                    timer.daemon = True
                    timer.start()
                    self._motion_states[sensor_mac]['after_timer'] = timer
                    logger.debug(f"Scheduled final off action for {sensor_mac} in {warning_lead_seconds}s")
                    
        except Exception as e:
            logger.error(f"Failed to execute dim warning for {sensor_mac}: {e}")
            # On error, try to schedule the off action anyway
            with self._motion_lock:
                if sensor_mac in self._motion_states:
                    timer = threading.Timer(
                        warning_lead_seconds,
                        self._execute_motion_after_action,
                        args=(sensor_mac, room_id, after_action, time_slot)
                    )
                    timer.daemon = True
                    timer.start()
                    self._motion_states[sensor_mac]['after_timer'] = timer
    
    def _execute_motion_after_action(self, sensor_mac: str, room_id: str, after_action: str, time_slot: Dict):
        """Execute the after action for a motion sensor.
        
        Args:
            sensor_mac: Motion sensor MAC address
            room_id: Room ID
            after_action: 'off', 'scene', or 'nothing'
            time_slot: Time slot configuration
        """
        try:
            # Set last_expected_clear_time BEFORE action to prevent this from being treated as manual
            with self._motion_lock:
                if sensor_mac in self._motion_states:
                    self._motion_states[sensor_mac]['last_expected_clear_time'] = time.time()
                    self._motion_states[sensor_mac]['last_motion_activity_time'] = time.time()
            
            if after_action == 'off':
                logger.info(f"🏃 Motion sensor {sensor_mac} turning off lights in room {room_id}")
                self._turn_off_room(room_id, source='motion')
            elif after_action == 'scene':
                after_scene_id = time_slot.get('after_scene_id')
                if after_scene_id:
                    logger.info(f"🏃 Motion sensor {sensor_mac} activating after-scene {after_scene_id} in room {room_id}")
                    self._activate_scene(room_id, after_scene_id, source='motion')
        except Exception as e:
            logger.error(f"Failed to execute after action for {sensor_mac}: {e}")
        finally:
            # Clear the timer reference
            with self._motion_lock:
                if sensor_mac in self._motion_states:
                    self._motion_states[sensor_mac]['after_timer'] = None
    
    def _cancel_motion_timers_for_room(self, room_id: str):
        """Cancel all pending motion sensor after-action timers for a specific room.
        
        This is called when lights in a room are manually changed to prevent
        automatic actions from interfering with user intent.
        
        Args:
            room_id: Room ID to cancel timers for
        """
        with self._motion_lock:
            cancelled_count = 0
            for sensor_mac, state in self._motion_states.items():
                if state.get('room_id') == room_id and 'after_timer' in state:
                    timer = state['after_timer']
                    if timer and timer.is_alive():
                        timer.cancel()
                        state['after_timer'] = None
                        cancelled_count += 1
                        logger.debug(f"Cancelled motion timer for sensor {sensor_mac} (room {room_id} was manually changed)")
            
            if cancelled_count > 0:
                logger.info(f"Cancelled {cancelled_count} motion timer(s) for room {room_id}")

    def _room_has_active_motion_timer(self, room_id: str) -> bool:
        """Check whether any motion sensor in the room has a live timer."""
        with self._motion_lock:
            for state in self._motion_states.values():
                if state.get('room_id') != room_id:
                    continue
                timer = state.get('after_timer')
                if timer and timer.is_alive():
                    return True
        return False

    def _room_has_recent_motion_activity(self, room_id: str, window_seconds: float) -> bool:
        """Check whether motion activity happened recently in a room."""
        now = time.time()
        with self._motion_lock:
            for state in self._motion_states.values():
                if state.get('room_id') != room_id:
                    continue
                last_activity = float(state.get('last_motion_activity_time', 0) or 0)
                if now - last_activity < window_seconds:
                    return True
        return False

    def _is_recent_expected_motion_change(self, room_id: str, scene_id: Optional[str] = None) -> bool:
        """Check if room change is likely from motion-driven scene/timer activity.

        Args:
            room_id: Room to inspect
            scene_id: Optional scene ID from a scene-change callback. When provided,
                cooldown is only considered expected if scene_id matches the last
                motion-expected scene.
        """
        now = time.time()
        with self._motion_lock:
            for sensor_mac, state in self._motion_states.items():
                if state.get('room_id') != room_id:
                    continue
                expected_scene_id = state.get('expected_scene_id')
                if expected_scene_id is not None:
                    if scene_id is None or scene_id == expected_scene_id:
                        return True
                    continue
                last_clear = float(state.get('last_expected_clear_time', 0) or 0)
                if now - last_clear < self._motion_expected_cooldown_seconds:
                    last_expected_scene_id = state.get('last_expected_scene_id')
                    if (
                        scene_id is not None
                        and last_expected_scene_id is not None
                        and scene_id != last_expected_scene_id
                    ):
                        logger.debug(
                            f"Ignoring motion cooldown for {sensor_mac}: "
                            f"scene {scene_id} != last expected {last_expected_scene_id}"
                        )
                        continue
                    logger.debug(
                        f"Room change within motion cooldown for {sensor_mac} "
                        f"({now - last_clear:.2f}s ago)"
                    )
                    return True
        return False
    
    def _get_brightness_change_percent(self, room_id: str) -> float:
        """Calculate what percentage of lights in a room had their brightness change.
        
        Returns percentage (0-100). If < 50%, likely external noise rather than user intent.
        Tracks lights that changed within the last 1 second to catch rapid SSE bursts.
        """
        room_state = hue_state_manager.get_room_state(room_id)
        if not room_state:
            return 0.0
        
        light_ids = room_state.get('lights', [])
        if not light_ids:
            return 0.0
        
        # Get previous snapshot if it exists
        previous = self._room_change_snapshots.get(room_id, {})
        previous_lights = previous.get('light_brightness_snapshot', {})
        previous_time = float(previous.get('light_snapshot_time', 0) or 0)
        
        current_time = time.time()
        lights_with_change = 0
        lights_changed_recently = set()
        
        for light_id in light_ids:
            current_light_state = hue_state_manager.get_light_state(light_id)
            if not current_light_state:
                continue
            
            current_brightness = current_light_state.get('brightness')
            previous_brightness = previous_lights.get(light_id)
            
            # Check if this light's brightness changed
            if previous_brightness is not None and current_brightness != previous_brightness:
                lights_with_change += 1
                lights_changed_recently.add(light_id)
        
        # Also include lights that changed in the last 1 second (rapid SSE burst)
        recently_changed = set(previous.get('lights_changed_recently', []))
        recently_changed.update(lights_changed_recently)
        
        # Clean up old entries (older than 1 second)
        if current_time - previous_time > 1.0:
            recently_changed.clear()
        
        percent = (len(recently_changed) / len(light_ids)) * 100.0 if light_ids else 0.0
        return percent
    
    # ===== Lightstrip Synchronization =====
    
    def _on_hue_room_changed(self, room_id: str, room_state: Dict):
        """Handle room state change (on/off, brightness) - sync lightstrips with short debounce.
        
        Args:
            room_id: Room that changed
            room_state: New room state
        """
        logger.debug(f"Room state changed for {room_id}: is_on={room_state.get('is_on')}, brightness={room_state.get('avg_brightness')}")

        previous = self._room_change_snapshots.get(room_id, {})
        current_scene = room_state.get('current_scene_id')
        current_brightness = room_state.get('avg_brightness')
        current_is_on = bool(room_state.get('is_on'))
        previous_scene = previous.get('current_scene_id')
        previous_brightness = previous.get('avg_brightness')
        previous_is_on = previous.get('is_on')

        scene_changed = previous_scene != current_scene
        brightness_changed = previous_brightness != current_brightness
        onoff_changed = previous_is_on != current_is_on
        brightness_only_change = brightness_changed and not scene_changed and not onoff_changed

        brightness_delta = None
        if isinstance(previous_brightness, (int, float)) and isinstance(current_brightness, (int, float)):
            brightness_delta = abs(float(current_brightness) - float(previous_brightness))

        is_confident_manual_brightness_change = (
            brightness_only_change
            and brightness_delta is not None
            and brightness_delta >= self._manual_brightness_cancel_threshold
        )

        has_expected_scene = self._is_recent_expected_motion_change(room_id)
        has_active_timer = self._room_has_active_motion_timer(room_id)
        has_recent_motion_activity = self._room_has_recent_motion_activity(
            room_id,
            self._motion_room_change_grace_seconds,
        )

        brightness = current_brightness
        is_on = current_is_on
        transient_zero_while_on = (
            has_active_timer
            and is_on
            and isinstance(brightness, (int, float))
            and brightness <= 0.1
        )

        if transient_zero_while_on:
            logger.debug(
                f"Ignoring transient brightness=0 room update for {room_id} "
                f"while motion timer is active"
            )

        # Calculate what percentage of lights in the room changed brightness
        # (BEFORE updating the snapshot, so we compare against previous state)
        lights_change_percent = self._get_brightness_change_percent(room_id)
        
        # Now capture and update light brightness snapshot for next comparison
        light_brightness_snapshot = {}
        lights_changed_this_event = set()
        light_ids = room_state.get('lights', [])
        for light_id in light_ids:
            light_state = hue_state_manager.get_light_state(light_id)
            if light_state:
                current_brightness = light_state.get('brightness')
                light_brightness_snapshot[light_id] = current_brightness
                
                # Track which lights changed in this event
                previous = self._room_change_snapshots.get(room_id, {})
                previous_lights = previous.get('light_brightness_snapshot', {})
                previous_brightness = previous_lights.get(light_id)
                if previous_brightness is not None and current_brightness != previous_brightness:
                    lights_changed_this_event.add(light_id)
        
        # Maintain set of recently changed lights
        previous = self._room_change_snapshots.get(room_id, {})
        recently_changed = set(previous.get('lights_changed_recently', []))
        recently_changed.update(lights_changed_this_event)

        self._room_change_snapshots[room_id] = {
            'current_scene_id': current_scene,
            'avg_brightness': current_brightness,
            'is_on': current_is_on,
            'light_brightness_snapshot': light_brightness_snapshot,
            'lights_changed_recently': list(recently_changed),
            'light_snapshot_time': time.time(),
        }
        
        # Scene changes are evaluated in _on_hue_scene_changed where source is known.
        should_cancel_motion_timers = False
        if not scene_changed:
            should_cancel_motion_timers = not (
                has_expected_scene
                or transient_zero_while_on
                or (brightness_only_change and has_recent_motion_activity)
                or (has_active_timer and has_recent_motion_activity)
            )

        # Evaluate brightness-only changes with stricter filtering
        ignore_for_recent_motion = brightness_only_change and has_recent_motion_activity
        # Only ignore for active timer if it's likely the timer's own action:
        # - Few lights changed (SSE noise), OR
        # - Recent motion activity (timer likely dimming)
        ignore_for_active_timer = (
            brightness_only_change 
            and has_active_timer 
            and (lights_change_percent < 50.0 or has_recent_motion_activity)
        )
        ignore_for_small_lights_change = brightness_only_change and lights_change_percent < 50.0
        ignore_for_small_delta = (
            brightness_only_change
            and brightness_delta is not None
            and brightness_delta < self._manual_brightness_cancel_threshold
        )

        logger.debug(
            f"Brightness analysis for {room_id}: delta={f'{brightness_delta:.2f}' if brightness_delta is not None else 'N/A'}%, "
            f"lights_changed={lights_change_percent:.1f}%, has_active_timer={has_active_timer}, "
            f"has_recent_motion={has_recent_motion_activity}, ignore_motion={ignore_for_recent_motion}, "
            f"ignore_active_timer={ignore_for_active_timer}, ignore_small_lights={ignore_for_small_lights_change}, "
            f"ignore_small_delta={ignore_for_small_delta}"
        )

        if ignore_for_active_timer:
            logger.debug(
                f"Ignoring brightness-only room update for {room_id}: "
                f"motion timer is active (likely from timer's own actions)"
            )
        elif ignore_for_recent_motion:
            logger.debug(
                f"Ignoring brightness-only room update for {room_id}: "
                f"within {self._motion_room_change_grace_seconds}s of motion activity"
            )
        elif ignore_for_small_lights_change:
            logger.debug(
                f"Ignoring brightness-only room update for {room_id}: "
                f"only {lights_change_percent:.1f}% of lights changed (likely external noise)"
            )
        elif ignore_for_small_delta:
            logger.debug(
                f"Ignoring minor brightness-only room update for {room_id}: "
                f"delta={f'{brightness_delta:.2f}' if brightness_delta is not None else 'N/A'}% < {self._manual_brightness_cancel_threshold:.2f}%"
            )

        if should_cancel_motion_timers:
            if ignore_for_active_timer or ignore_for_recent_motion or ignore_for_small_lights_change or ignore_for_small_delta:
                should_cancel_motion_timers = False
            else:
                self._cancel_motion_timers_for_room(room_id)
        
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

        if source in self._internal_timer_cancel_sources and source != 'sse':
            logger.debug(
                f"Scene source '{source}' is internal HuemixLink trigger; "
                f"cancelling motion timers for room {room_id}"
            )
            self._cancel_motion_timers_for_room(room_id)
        
        # Check if this scene change was expected from any motion sensor.
        is_expected = False
        motion_scene_reference_ids = set()
        with self._motion_lock:
            current_time = time.time()
            for sensor_mac, state in self._motion_states.items():
                if state.get('room_id') != room_id:
                    continue

                expected_scene = state.get('expected_scene_id')
                if expected_scene:
                    motion_scene_reference_ids.add(expected_scene)

                last_expected_scene = state.get('last_expected_scene_id')
                last_clear = float(state.get('last_expected_clear_time', 0) or 0)
                timer = state.get('after_timer')
                has_active_timer = bool(timer and timer.is_alive())
                if last_expected_scene and (has_active_timer or (current_time - last_clear < self._motion_expected_cooldown_seconds)):
                    motion_scene_reference_ids.add(last_expected_scene)

                if state.get('expected_scene_id') == scene_id and scene_id is not None:
                    state.pop('expected_scene_id', None)
                    state['last_expected_scene_id'] = scene_id
                    state['last_expected_clear_time'] = current_time
                    state['last_motion_activity_time'] = current_time
                    is_expected = True
                    logger.debug(f"Scene change to {scene_id} in {room_id} was expected from motion sensor {sensor_mac}")
                    break

        if not is_expected:
            is_expected = self._is_recent_expected_motion_change(room_id, scene_id=scene_id)

        has_recent_motion_activity = self._room_has_recent_motion_activity(
            room_id,
            self._motion_room_change_grace_seconds,
        )

        # For external SSE updates, cancel when scene diverges from the
        # motion-owned scene reference; otherwise keep skepticism with motion timing.
        should_cancel_for_sse = False
        if source == 'sse':
            if scene_id is not None and motion_scene_reference_ids and scene_id not in motion_scene_reference_ids:
                should_cancel_for_sse = True
                logger.debug(
                    f"SSE scene {scene_id} differs from motion scene references "
                    f"{sorted(motion_scene_reference_ids)} for room {room_id}; cancelling timer"
                )
            elif not is_expected and not has_recent_motion_activity:
                should_cancel_for_sse = True

        if should_cancel_for_sse:
            self._cancel_motion_timers_for_room(room_id)
        
        # Track manual turn-offs to prevent motion sensors from triggering immediately after
        # (scene_id is None = lights turned off, NOT is_expected = not from motion sensor timer)
        if scene_id is None and not is_expected:
            with self._motion_lock:
                self._room_manual_off_times[room_id] = time.time()
                logger.debug(f"Room {room_id} manually turned off (source: {source})")
        
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
            
            # Helper function to update a single lightstrip
            def update_single_strip(strip):
                try:
                    light_mac = strip.get('mac_address')
                    
                    # Skip lightstrips in preview mode
                    with self._preview_lock:
                        if light_mac in self._preview_mode_lightstrips:
                            logger.debug(f"Skipping lightstrip {strip.get('name', light_mac)} - in preview mode")
                            return
                    
                    rgb_data = self._get_lightstrip_colors(strip, scene_id, room_id)
                    if rgb_data is not None:
                        if self.network_server:
                            self.network_server.send_to_light(light_mac, rgb_data, brightness_val)
                        logger.info(f"Sent colors to lightstrip {strip.get('name', light_mac)} (brightness: {brightness_pct:.0f}%)")
                except Exception as e:
                    logger.error(f"Error syncing lightstrip {strip.get('id')}: {e}")
            
            # Update all lightstrips in parallel using threads
            threads = []
            for strip in strips_for_room:
                thread = threading.Thread(target=update_single_strip, args=(strip,), daemon=True)
                thread.start()
                threads.append(thread)
            
            # Wait for all updates to complete (with a reasonable timeout)
            for thread in threads:
                thread.join(timeout=30)  # 30 second max wait per thread
            
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
                if self.network_server:
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
        
        mac_address = strip.get('mac_address', "")
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