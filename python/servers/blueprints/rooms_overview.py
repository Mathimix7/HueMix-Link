"""Blueprint for rooms overview page."""
from flask import Blueprint, render_template, jsonify
from services.hue_state_manager import hue_state_manager
from controllers.color_controller import color_controller

bp = Blueprint('rooms_overview', __name__)


@bp.route('/rooms-overview')
def rooms_overview():
    """Render the rooms overview page."""
    return render_template('rooms_overview.html')


@bp.route('/api/rooms-overview/data')
def get_rooms_overview_data():
    """
    Get comprehensive overview data for all rooms.
    
    Returns JSON with:
    - Room info (name, on/off state)
    - All lights in room with colors, brightness, on/off
    - Current scene
    - Average brightness
    - Color distribution
    """
    try:
        rooms = hue_state_manager.get_all_rooms()
        lights = hue_state_manager.get_all_lights()
        
        overview_data = []
        
        for room_id, room_state in rooms.items():
            room_name = room_state.get('name', 'Unknown Room')
            is_on = room_state.get('is_on', False)
            current_scene_id = room_state.get('current_scene_id')
            light_ids = room_state.get('lights', [])
            
            # Get scene name if available
            scene_name = None
            if current_scene_id:
                scene_info = hue_state_manager.get_scene_info(current_scene_id)
                if scene_info:
                    scene_name = scene_info.get('name', 'Unknown Scene')
                else:
                    # Scene not found in registry, use ID as fallback
                    scene_name = f"Scene {current_scene_id[:8]}"
            
            # Process lights in this room
            room_lights = []
            total_brightness = 0
            on_count = 0
            colors = []
            
            for light_id in light_ids:
                light_state = lights.get(light_id, {})
                light_is_on = light_state.get('on', False)
                light_brightness = light_state.get('brightness', 0)
                light_name = light_state.get('name', f'Light {light_id[:8]}')
                
                # Get color information
                color_info = None
                color_hex = None
                color_name = None
                color_mode = None
                
                # Check if light has color information in state
                if 'color' in light_state and light_state['color']:
                    color = light_state['color']
                    if isinstance(color, dict):
                        # Check if it's already RGB
                        if 'r' in color and 'g' in color and 'b' in color:
                            r, g, b = color['r'], color['g'], color['b']
                            color_hex = color_controller.rgb_to_hex(r, g, b)
                            color_name = color_controller.get_color_name(r, g, b)
                            color_mode = 'color'
                        # Check if it's XY coordinates
                        elif 'x' in color and 'y' in color:
                            x, y = color['x'], color['y']
                            rgb = color_controller.xy_to_rgb(x, y, light_brightness or 100)
                            color_hex = color_controller.rgb_to_hex(rgb['r'], rgb['g'], rgb['b'])
                            color_name = color_controller.get_color_name(rgb['r'], rgb['g'], rgb['b'])
                            color_mode = 'xy'
                elif 'xy' in light_state:
                    # XY values stored separately
                    xy = light_state['xy']
                    if isinstance(xy, dict) and 'x' in xy and 'y' in xy:
                        x, y = xy['x'], xy['y']
                        rgb = color_controller.xy_to_rgb(x, y, light_brightness or 100)
                        color_hex = color_controller.rgb_to_hex(rgb['r'], rgb['g'], rgb['b'])
                        color_name = color_controller.get_color_name(rgb['r'], rgb['g'], rgb['b'])
                        color_mode = 'xy'
                elif 'ct' in light_state:
                    # Color temperature
                    ct = light_state['ct']
                    x, y = color_controller.ct_to_xy(ct)
                    rgb = color_controller.xy_to_rgb(x, y, light_brightness or 100)
                    color_hex = color_controller.rgb_to_hex(rgb['r'], rgb['g'], rgb['b'])
                    kelvin = color_controller.xy_to_ct(x, y)
                    color_name = f"{kelvin}K"
                    color_mode = 'ct'
                
                # Default to white/gray if no color info
                if not color_hex:
                    bri_val = int((light_brightness or 50) / 100 * 255)
                    color_hex = color_controller.rgb_to_hex(bri_val, bri_val, bri_val)
                    color_name = "White" if light_is_on else "Off"
                    color_mode = 'white'
                
                if light_is_on:
                    on_count += 1
                    total_brightness += light_brightness
                    if color_hex:
                        colors.append(color_hex)
                
                room_lights.append({
                    'id': light_id,
                    'name': light_name,
                    'is_on': light_is_on,
                    'brightness': round(light_brightness, 1) if light_brightness else 0,
                    'color_hex': color_hex,
                    'color_name': color_name,
                    'color_mode': color_mode
                })
            
            # Calculate average brightness
            avg_brightness = round(total_brightness / on_count, 1) if on_count > 0 else 0
            
            overview_data.append({
                'room_id': room_id,
                'name': room_name,
                'is_on': is_on,
                'current_scene': scene_name,
                'current_scene_id': current_scene_id,
                'lights_count': len(light_ids),
                'lights_on': on_count,
                'avg_brightness': avg_brightness,
                'lights': room_lights,
                'colors': colors[:10]  # Limit to 10 colors for display
            })
        
        # Sort by room name
        overview_data.sort(key=lambda x: x['name'])
        
        return jsonify({
            'success': True,
            'rooms': overview_data
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
