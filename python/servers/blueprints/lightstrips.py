from flask import Blueprint, render_template, jsonify, request
from services import data_manager
from services.lightstrip_service import lightstrip_service

lightstrips_bp = Blueprint('lightstrips', __name__, url_prefix='/lightstrips')

@lightstrips_bp.route('/')
def index():
    """Render lightstrips configuration page"""
    return render_template('lightstrips.html')

@lightstrips_bp.route('/api/lightstrips')
def get_lightstrips():
    """Get all lightstrips"""
    try:
        lightstrips = data_manager.read_json('lightstrips.json')
        return jsonify({'success': True, 'lightstrips': lightstrips})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>', methods=['PUT'])
def update_lightstrip(lightstrip_id):
    """Update a lightstrip's basic configuration"""
    try:
        data = request.get_json()
        lightstrips = data_manager.read_json('lightstrips.json')
        
        # Find the lightstrip
        lightstrip = next((ls for ls in lightstrips if ls['id'] == lightstrip_id), None)
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        # Update fields
        if 'name' in data:
            lightstrip['name'] = data['name']
        if 'ip' in data:
            lightstrip['ip'] = data['ip']
        if 'room_id' in data:
            lightstrip['room_id'] = data['room_id']
        if 'single_color' in data:
            lightstrip['single_color'] = data['single_color']
        if 'number_colors' in data:
            lightstrip['number_colors'] = data['number_colors']
        if 'color_type' in data:
            lightstrip['color_type'] = data['color_type']
        
        data_manager.write_json('lightstrips.json', lightstrips)
        
        # Reload service config
        lightstrip_service.reload_lightstrips()
        
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
        
        lightstrips = data_manager.read_json('lightstrips.json')
        
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
        
        data_manager.write_json('lightstrips.json', lightstrips)
        
        # Reload service config
        lightstrip_service.reload_lightstrips()
        
        return jsonify({'success': True, 'lightstrip': lightstrip})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/lightstrips/<lightstrip_id>', methods=['DELETE'])
def delete_lightstrip(lightstrip_id):
    """Delete a lightstrip."""
    try:
        lightstrips = data_manager.read_json('lightstrips.json')
        
        # Find and remove the lightstrip
        original_length = len(lightstrips)
        lightstrips = [ls for ls in lightstrips if ls['id'] != lightstrip_id]
        
        if len(lightstrips) == original_length:
            return jsonify({'success': False, 'error': 'Lightstrip not found'}), 404
        
        data_manager.write_json('lightstrips.json', lightstrips)
        
        # Reload service config
        lightstrip_service.reload_lightstrips()
        
        return jsonify({'success': True, 'message': 'Lightstrip deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lightstrips_bp.route('/api/device_status', methods=['POST'])
def device_status():
    """
    Device registration endpoint.
    Called by lightstrip devices on boot to register/update their IP.
    
    Expected JSON payload:
    {
        "device_id": "AA:BB:CC:DD:EE:FF",
        "ip": "192.168.1.100"
    }
    
    Returns:
    {
        "status": "configured" | "new" | "error",
        "device": {...},  // Device configuration
        "colors": [...] | null  // Current colors to display (if room has active scene)
    }
    """
    try:
        data = request.get_json()
        
        if not data or 'device_id' not in data or 'ip' not in data:
            return jsonify({
                'status': 'error',
                'error': 'Missing device_id or ip'
            }), 400
        
        device_id = data['device_id']
        ip = data['ip']
        
        # Register/update device
        result = lightstrip_service.register_device(device_id, ip)
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500
