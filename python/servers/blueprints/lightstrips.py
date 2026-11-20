from flask import Blueprint, render_template, jsonify, request
from services import data_manager
from network.network_server import network_server
from services.automation_service import automation_service
from services.hue_state_manager import hue_state_manager
from constants import (
    MIN_LEDS, MAX_LEDS, CMD_SET_LED_COUNT, 
    TIMEOUT_PING, TIMEOUT_PING_SINGLE,
    FILE_LIGHTSTRIPS, FILE_GATEWAYS
)
import re
import threading

lightstrips_bp = Blueprint('lightstrips', __name__, url_prefix='/lightstrips')

# MAC address validation pattern
MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')

def is_valid_mac(mac: str) -> bool:
    """Validate MAC address format."""
    return MAC_PATTERN.match(mac) is not None

@lightstrips_bp.route('/')
def index():
    """Render lightstrips configuration page"""
    return render_template('lightstrips.html')

@lightstrips_bp.route('/api/lightstrips')
def get_lightstrips():
    """Get all lightstrips"""
    try:
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        return jsonify({'success': True, 'lightstrips': lightstrips})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>', methods=['PUT'])
def update_lightstrip(lightstrip_id):
    """Update a lightstrip's basic configuration"""
    try:
        data = request.get_json()
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        
        # Find the lightstrip
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        # Track if num_leds changed
        num_leds_changed = False
        new_num_leds = None
        
        # Update fields
        if 'name' in data:
            lightstrip['name'] = data['name']
        if 'room_id' in data:
            lightstrip['room_id'] = data['room_id']
        if 'target_id' in data:
            lightstrip['target_id'] = data['target_id']
        if 'target_type' in data:
            target_type = data['target_type']
            if target_type not in ('room', 'zone'):
                return jsonify({'success': False, 'error': 'target_type must be room or zone'}), 400
            lightstrip['target_type'] = target_type
        if 'single_color' in data:
            lightstrip['single_color'] = data['single_color']
        if 'ignore_third_party' in data:
            lightstrip['ignore_third_party'] = data['ignore_third_party']
        if 'coverage' in data:
            coverage = float(data['coverage'])
            if coverage < 0.1 or coverage > 10.0:
                return jsonify({'success': False, 'error': 'coverage must be between 0.1 and 10.0'}), 400
            lightstrip['coverage'] = coverage
        if 'distortion' in data:
            distortion = float(data['distortion'])
            if distortion < 0.0 or distortion > 1.0:
                return jsonify({'success': False, 'error': 'distortion must be between 0.0 and 1.0'}), 400
            lightstrip['distortion'] = distortion
        if 'num_leds' in data:
            new_num_leds = int(data['num_leds'])
            # Validate num_leds range
            if not (MIN_LEDS <= new_num_leds <= MAX_LEDS):
                return jsonify({'success': False, 'error': f'num_leds must be between {MIN_LEDS} and {MAX_LEDS}'}), 400
            if new_num_leds != lightstrip.get('number_colors'):
                num_leds_changed = True
                lightstrip['number_colors'] = new_num_leds
        
        data_manager.write_json(FILE_LIGHTSTRIPS, lightstrips)
        
        # If num_leds changed, send UDP command to device
        if num_leds_changed and 'mac_address' in lightstrip:
            light_mac = lightstrip['mac_address']
            # Send system command to set LED count
            success = network_server.send_system_command(light_mac, CMD_SET_LED_COUNT, new_num_leds)
            if not success:
                return jsonify({
                    'success': True, 
                    'lightstrip': lightstrip,
                    'warning': 'Configuration saved but failed to send LED count update to device'
                })

        if 'mac_address' in lightstrip:
            automation_engine = automation_service.get_engine()
            threading.Thread(
                target=automation_engine.send_current_colors_to_light, 
                args=(lightstrip['mac_address'],), 
                daemon=True
            ).start()
        
        return jsonify({'success': True, 'lightstrip': lightstrip})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>/overrides', methods=['POST'])
def set_override(lightstrip_id):
    """Set a scene override for a lightstrip"""
    try:
        data = request.get_json()
        
        if 'scene_id' not in data:
            return jsonify({'success': False, 'error': 'Missing scene_id'}), 400
        
        scene_id = data['scene_id']
        override_data = data.get('override')
        
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        
        # Find the lightstrip
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        # Set or remove override
        if override_data is None:
            # Remove override
            if scene_id in lightstrip['overrides']:
                del lightstrip['overrides'][scene_id]
        else:
            # Validate override structure
            if 'type' not in override_data:
                return jsonify({'success': False, 'error': 'Override must have a type'}), 400
            
            override_type = override_data['type']
            
            if override_type == 'off':
                lightstrip['overrides'][scene_id] = {'type': 'off'}
            elif override_type == 'single_color':
                if 'color' not in override_data:
                    return jsonify({'success': False, 'error': 'single_color override requires color'}), 400
                lightstrip['overrides'][scene_id] = {
                    'type': 'single_color',
                    'color': override_data['color']
                }
            elif override_type == 'multi_color':
                if 'colors' not in override_data:
                    return jsonify({'success': False, 'error': 'multi_color override requires colors array'}), 400
                lightstrip['overrides'][scene_id] = {
                    'type': 'multi_color',
                    'colors': override_data['colors']
                }
            else:
                return jsonify({'success': False, 'error': 'Invalid override type'}), 400
        
        data_manager.write_json(FILE_LIGHTSTRIPS, lightstrips)
        
        return jsonify({'success': True, 'lightstrip': lightstrip})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>', methods=['DELETE'])
def delete_lightstrip(lightstrip_id):
    """Delete a lightstrip."""
    try:
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        
        # Find and remove the lightstrip
        original_length = len(lightstrips)
        lightstrips = [ls for ls in lightstrips if ls['id'] != lightstrip_id]
        
        if len(lightstrips) == original_length:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        data_manager.write_json(FILE_LIGHTSTRIPS, lightstrips)
        
        return jsonify({'success': True, 'message': 'Lightstrip deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<mac_address>/rssi', methods=['GET'])
def get_lightstrip_rssi(mac_address):
    """Get current RSSI from the lightstrip's assigned gateway (last_gateway_mac)"""
    try:
        # Validate MAC address format
        if not is_valid_mac(mac_address):
            return jsonify({'success': False, 'error': 'Invalid MAC address format'}), 400
        
        # Get lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        lightstrip = next((ls for ls in lightstrips if ls['mac_address'] == mac_address), None)
        
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        last_gateway_mac = lightstrip.get('last_gateway_mac')
        if not last_gateway_mac:
            return jsonify({
                'success': True,
                'rssi': None,
                'message': 'No gateway assigned'
            })
        
        # Ping via the assigned gateway only
        light_mac = lightstrip['mac_address']
        rssi_map = network_server.ping_device_single_gateway(light_mac, last_gateway_mac, timeout=TIMEOUT_PING_SINGLE)
        
        if rssi_map is None:
            return jsonify({'success': False, 'error': 'Ping failed'}), 500
        
        rssi = rssi_map.get(last_gateway_mac)
        
        return jsonify({
            'success': True,
            'rssi': rssi,
            'gateway_mac': last_gateway_mac
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<mac_address>/ping', methods=['POST'])
def ping_lightstrip(mac_address):
    """Ping a lightstrip via all gateways to find best gateway and update last_gateway_mac"""
    try:
        # Validate MAC address format
        if not is_valid_mac(mac_address):
            return jsonify({'success': False, 'error': 'Invalid MAC address format'}), 400
        
        # Get lightstrip to find MAC address
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        lightstrip = next((ls for ls in lightstrips if ls['mac_address'] == mac_address), None)
        
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        if 'mac_address' not in lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip has no MAC address'}), 400
        
        light_mac = lightstrip['mac_address']
        
        # Send ping to all gateways
        rssi_map = network_server.ping_device(light_mac, timeout=TIMEOUT_PING)
        
        if rssi_map is None:
            return jsonify({'success': False, 'error': 'Ping failed'}), 500
        
        if not rssi_map:
            return jsonify({
                'success': True,
                'message': 'No responses received',
                'gateways': []
            })
        
        # Format response
        gateway_results = []
        for gateway_mac, rssi in rssi_map.items():
            gateway_results.append({
                'gateway_mac': gateway_mac,
                'rssi': rssi
            })
        
        # Sort by strongest signal (highest RSSI)
        gateway_results.sort(key=lambda x: x['rssi'], reverse=True)
        
        best_gateway = gateway_results[0] if gateway_results else None
        
        # Update last_gateway_mac to the best one
        if best_gateway:
            lightstrip['last_gateway_mac'] = best_gateway['gateway_mac']
            
            # Also update gateway_ip from gateways.json
            gateways = data_manager.read_json(FILE_GATEWAYS, default=[])
            gateway_info = next((g for g in gateways if g.get('radio_mac') == best_gateway['gateway_mac']), None)
            if gateway_info:
                lightstrip['gateway_ip'] = gateway_info.get('ip_address', '')
                best_gateway['name'] = gateway_info.get('name', '')
            
            data_manager.write_json(FILE_LIGHTSTRIPS, lightstrips)
        
        return jsonify({
            'success': True,
            'lightstrip_id': lightstrip["id"],
            'lightstrip_mac': light_mac,
            'gateways': gateway_results,
            'best_gateway': best_gateway
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>/preview-colors', methods=['POST'])
def preview_colors(lightstrip_id):
    """Preview colors on a lightstrip in real-time (temporary, not saved)"""
    try:
        automation_engine = automation_service.get_engine()
        if not automation_engine:
            return jsonify({'success': False, 'error': 'Automation engine not initialized'}), 500
        
        data = request.get_json()
        colors = data.get('colors', [])  # Array of {r, g, b} objects
        
        if not colors:
            return jsonify({'success': False, 'error': 'No colors provided'}), 400
        
        # Get lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        # Convert colors to RGB tuples
        rgb_palette = [(c['r'], c['g'], c['b']) for c in colors]
        
        # Generate strip colors using automation engine
        num_leds = lightstrip.get('num_leds', 40)
        strip_colors = automation_engine._generate_strip_colors(lightstrip, rgb_palette, num_leds)
        
        # Get current brightness from room/zone
        group_id = lightstrip.get('target_id') or lightstrip.get('room_id')
        group_type = lightstrip.get('target_type', 'room')
        if group_type not in ('room', 'zone'):
            group_type = 'room'
        brightness_pct = 100.0
        if group_id:
            group_state = (
                hue_state_manager.get_zone_state(group_id)
                if group_type == 'zone'
                else hue_state_manager.get_room_state(group_id)
            )
            if group_state:
                brightness_pct = group_state.get('avg_brightness', 100.0)
        
        brightness_val = int(brightness_pct * 2.55)
        
        # Send to lightstrip
        light_mac = lightstrip.get('mac_address')
        if not light_mac:
            return jsonify({'success': False, 'error': 'Lightstrip has no MAC address'}), 400
        
        success = network_server.send_to_light(light_mac, strip_colors, brightness_val)
        
        if not success:
            return jsonify({'success': False, 'error': 'Failed to send colors to lightstrip'}), 500
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>/default-colors/<scene_id>', methods=['GET'])
def get_default_colors(lightstrip_id, scene_id):
    """Get default colors for a scene (what the lightstrip would show without override)"""
    try:        
        automation_engine = automation_service.get_engine()
        if not automation_engine:
            return jsonify({'success': False, 'error': 'Automation engine not initialized'}), 500
        
        # Get lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        group_id = lightstrip.get('target_id') or lightstrip.get('room_id')
        if not group_id:
            return jsonify({'success': False, 'error': 'Lightstrip has no room/zone assigned'}), 400
        
        group_type = lightstrip.get('target_type', 'room')
        if group_type not in ('room', 'zone'):
            group_type = 'room'
        ignore_third_party = lightstrip.get('ignore_third_party', False)
        
        # Get palette from room/zone lights (ignoring any overrides)
        palette = automation_engine._get_hue_light_palette(group_id, ignore_third_party, group_type)
        
        if not palette:
            return jsonify({'success': True, 'colors': []})
        
        # Convert to color objects
        colors = [{'r': r, 'g': g, 'b': b} for r, g, b in palette]
        
        return jsonify({'success': True, 'colors': colors})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>/preview-mode', methods=['POST'])
def set_preview_mode(lightstrip_id):
    """Enable or disable preview mode for a lightstrip"""
    try:
        automation_engine = automation_service.get_engine()
        if not automation_engine:
            return jsonify({'success': False, 'error': 'Automation engine not initialized'}), 500
        
        data = request.get_json()
        enabled = data.get('enabled', False)
        
        # Get lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS)
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        light_mac = lightstrip.get('mac_address')
        if not light_mac:
            return jsonify({'success': False, 'error': 'Lightstrip has no MAC address'}), 400
        
        # Set preview mode
        automation_engine.set_preview_mode(light_mac, enabled)
        
        return jsonify({'success': True, 'preview_mode': enabled})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
