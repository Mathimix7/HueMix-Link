"""Service for managing LED lightstrips and syncing them with Hue scenes."""
import logging
import requests
from typing import Dict, List, Optional
from services import data_manager
from services.hue_state_manager import hue_state_manager

logger = logging.getLogger(__name__)


class LightstripService:
    """
    Service responsible for sending color data to LED lightstrips via HTTP.
    Subscribes to scene changes from HueStateManager and updates lightstrips accordingly.
    """
    
    def __init__(self):
        self._lightstrips: Dict[str, Dict] = {}  # device_id -> config
        self._running = False
        
        logger.info("LightstripService initialized")
    
    def start(self):
        """Start the lightstrip service."""
        if self._running:
            logger.warning("LightstripService already running")
            return
        
        self._running = True
        self._load_lightstrips()
        
        # Subscribe to scene changes from state manager
        hue_state_manager.subscribe_scene_changes(self._on_scene_changed)
        
        logger.info("LightstripService started")
    
    def stop(self):
        """Stop the lightstrip service."""
        self._running = False
        hue_state_manager.unsubscribe_scene_changes(self._on_scene_changed)
        logger.info("LightstripService stopped")
    
    def _load_lightstrips(self):
        """Load lightstrip configurations from JSON."""
        try:
            strips = data_manager.read_json('lightstrips.json', default=[])
            self._lightstrips.clear()
            
            for strip in strips:
                device_id = strip.get('id')
                if device_id:
                    self._lightstrips[device_id] = strip
            
            logger.info(f"Loaded {len(self._lightstrips)} lightstrip configurations")
        except Exception as e:
            logger.error(f"Failed to load lightstrip configs: {e}")
    
    def reload_lightstrips(self):
        """Reload lightstrip configurations."""
        self._load_lightstrips()
    
    def register_device(self, device_id: str, ip: str) -> Dict:
        """
        Register or update a lightstrip device.
        Called when device sends /device_status.
        
        Args:
            device_id: Unique device identifier
            ip: Device IP address
        
        Returns:
            Dict with device config and current state to send back
        """
        try:
            strips = data_manager.read_json('lightstrips.json', default=[])
            
            # Find existing device
            existing = None
            for strip in strips:
                if strip.get('id') == device_id:
                    existing = strip
                    break
            
            if existing:
                # Update IP if changed
                if existing.get('ip') != ip:
                    logger.info(f"Device {device_id} IP changed: {existing.get('ip')} -> {ip}")
                    existing['ip'] = ip
                    data_manager.write_json('lightstrips.json', strips)
                
                # Reload config
                self._lightstrips[device_id] = existing
                
                # Get current state for this device's room
                room_id = existing.get('room_id')
                if room_id:
                    room_state = hue_state_manager.get_room_state(room_id)
                    if room_state and room_state.get('current_scene_id'):
                        # Return current scene colors
                        scene_id = room_state['current_scene_id']
                        colors = self._get_colors_for_scene(existing, scene_id)
                        if colors:
                            return {
                                'status': 'configured',
                                'device': existing,
                                'colors': colors
                            }
                
                return {
                    'status': 'configured',
                    'device': existing,
                    'colors': None  # No active scene
                }
            
            else:
                # New device - add to config
                new_strip = {
                    'id': device_id,
                    'name': f'Lightstrip {device_id[-4:]}',
                    'mac_address': device_id,
                    'ip': ip,
                    'room_id': '',
                    'single_color': True,
                    'number_colors': 40,
                    'color_type': 'rgb',
                    'overrides': {}
                }
                
                strips.append(new_strip)
                data_manager.write_json('lightstrips.json', strips)
                self._lightstrips[device_id] = new_strip
                
                logger.info(f"New lightstrip device registered: {device_id} at {ip}")
                
                return {
                    'status': 'new',
                    'device': new_strip,
                    'colors': None
                }
        
        except Exception as e:
            logger.error(f"Error registering device {device_id}: {e}")
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def _on_scene_changed(self, room_id: str, scene_id: str, old_scene_id: Optional[str], source: str):
        """
        Handle scene change event from state manager.
        
        Args:
            room_id: Room where scene changed
            scene_id: New scene ID
            old_scene_id: Previous scene ID (if any)
            source: Source of the change (sse, button, web)
        """
        logger.info(f"Scene changed in room {room_id}: {scene_id} (from {source})")
        
        # Find lightstrips for this room
        strips_for_room = [
            strip for strip in self._lightstrips.values()
            if strip.get('room_id') == room_id and strip.get('ip')
        ]
        
        if not strips_for_room:
            logger.debug(f"No lightstrips configured for room {room_id}")
            return
        
        # Update each lightstrip
        for strip in strips_for_room:
            try:
                colors = self._get_colors_for_scene(strip, scene_id)
                if colors:
                    self._send_colors_to_strip(strip, colors)
            except Exception as e:
                logger.error(f"Error updating lightstrip {strip.get('id')}: {e}")
    
    def _get_colors_for_scene(self, strip: Dict, scene_id: str) -> Optional[List[Dict]]:
        """
        Get the colors to send to a lightstrip for a specific scene.
        Checks for scene overrides first, then uses default scene colors.
        
        Args:
            strip: Lightstrip configuration
            scene_id: Scene ID
        
        Returns:
            List of color dicts [{r, g, b}, ...] or None
        """
        try:
            # Check for scene override
            overrides = strip.get('overrides', {})
            if scene_id in overrides:
                override = overrides[scene_id]
                override_type = override.get('type')
                
                if override_type == 'off':
                    # Turn off lightstrip
                    return self._generate_off_colors(strip)
                
                elif override_type == 'single_color':
                    color = override.get('color', {})
                    return self._generate_single_color(strip, color)
                
                elif override_type == 'multi_color':
                    colors = override.get('colors', [])
                    return self._generate_multi_color(strip, colors)
            
            # No override - get colors from scene
            return self._get_scene_colors_from_hue(strip, scene_id)
        
        except Exception as e:
            logger.error(f"Error getting colors for scene {scene_id}: {e}")
            return None
    
    def _get_scene_colors_from_hue(self, strip: Dict, scene_id: str) -> Optional[List[Dict]]:
        """
        Get colors from Hue scene by looking at light states in the room.
        
        Args:
            strip: Lightstrip configuration
            scene_id: Scene ID
        
        Returns:
            List of color dicts or None
        """
        # Get room state to find lights
        room_id = strip.get('room_id')
        if not room_id:
            return None
        
        room_state = hue_state_manager.get_room_state(room_id)
        if not room_state:
            return None
        
        # Get the first light's color in the room
        light_ids = room_state.get('lights', [])
        if not light_ids:
            return None
        
        # Use first light's state as reference
        first_light_state = hue_state_manager.get_light_state(light_ids[0])
        if not first_light_state or not first_light_state.get('on'):
            return self._generate_off_colors(strip)
        
        # Extract color from light state
        color = first_light_state.get('color', {})
        if not color:
            # Default to white if no color info
            color = {'r': 255, 'g': 255, 'b': 255}
        
        # Generate colors based on strip mode
        if strip.get('single_color', True):
            return self._generate_single_color(strip, color)
        else:
            # Multi-color mode - use the same color for all LEDs
            return self._generate_single_color(strip, color)
    
    def _generate_off_colors(self, strip: Dict) -> List[Dict]:
        """Generate colors for turning off the strip."""
        num_leds = strip.get('number_colors', 40)
        return [{'r': 0, 'g': 0, 'b': 0}] * num_leds
    
    def _generate_single_color(self, strip: Dict, color: Dict) -> List[Dict]:
        """Generate single color for all LEDs."""
        num_leds = strip.get('number_colors', 40)
        r = color.get('r', 255)
        g = color.get('g', 255)
        b = color.get('b', 255)
        return [{'r': r, 'g': g, 'b': b}] * num_leds
    
    def _generate_multi_color(self, strip: Dict, colors: List[Dict]) -> List[Dict]:
        """Generate multi-color pattern."""
        num_leds = strip.get('number_colors', 40)
        
        if not colors:
            return self._generate_off_colors(strip)
        
        # Repeat colors to fill all LEDs
        result = []
        for i in range(num_leds):
            color = colors[i % len(colors)]
            result.append({
                'r': color.get('r', 0),
                'g': color.get('g', 0),
                'b': color.get('b', 0)
            })
        
        return result
    
    def _send_colors_to_strip(self, strip: Dict, colors: List[Dict]):
        """
        Send color data to lightstrip via HTTP POST.
        
        Args:
            strip: Lightstrip configuration
            colors: List of color dicts [{r, g, b}, ...]
        """
        try:
            ip = strip.get('ip')
            if not ip:
                logger.warning(f"No IP for lightstrip {strip.get('id')}")
                return
            
            # Format payload as expected by device
            payload = ''
            for color in colors:
                r = color.get('r', 0)
                g = color.get('g', 0)
                b = color.get('b', 0)
                payload += f"{r},{g},{b};"
            
            # Remove trailing semicolon
            payload = payload.rstrip(';')
            
            # Send HTTP POST
            response = requests.post(
                f'http://{ip}',
                data=payload,
                timeout=3,
                headers={'Content-Type': 'text/plain'}
            )
            
            if response.status_code == 200:
                logger.info(f"Sent colors to lightstrip {strip.get('name')} ({ip}) - {len(colors)} LEDs")
            else:
                logger.warning(f"Lightstrip {strip.get('id')} returned status {response.status_code}")
        
        except requests.Timeout:
            logger.warning(f"Timeout sending to lightstrip {strip.get('id')} at {ip}")
        except Exception as e:
            logger.error(f"Error sending to lightstrip {strip.get('id')}: {e}")
    
    def get_all_lightstrips(self) -> List[Dict]:
        """Get all registered lightstrips."""
        return list(self._lightstrips.values())
    
    def get_lightstrip(self, device_id: str) -> Optional[Dict]:
        """Get a specific lightstrip configuration."""
        return self._lightstrips.get(device_id)


# Global singleton instance
lightstrip_service = LightstripService()
