from flask import Blueprint, render_template, jsonify, request
from controllers.bridge_controller import BridgeController
from services import config_notifier

bridge_bp = Blueprint('bridge', __name__, url_prefix='/bridge')

# Initialize the bridge controller
bridge_controller = BridgeController()

@bridge_bp.route('/')
def index():
    """Render the bridge configuration page"""
    return render_template('bridge.html')

@bridge_bp.route('/api/bridge/discover', methods=['GET'])
def discover_bridge():
    """Discover Hue bridges on the local network"""
    result = bridge_controller.discover_bridges()
    return jsonify(result)

@bridge_bp.route('/api/bridge/verify', methods=['POST'])
def verify_bridge():
    """Verify a bridge IP address"""
    data = request.get_json()
    ip = data.get('ip')
    
    if not ip:
        return jsonify({'success': False, 'error': 'IP address required'})
    
    result = bridge_controller.verify_bridge(ip)
    return jsonify(result)

@bridge_bp.route('/api/bridge/pair', methods=['POST'])
def pair_bridge():
    """Create a new user on the Hue bridge (requires button press)"""
    data = request.get_json()
    ip = data.get('ip')
    app_name = data.get('app_name', 'hue_mix_link')
    device_name = data.get('device_name', 'server')
    
    if not ip:
        return jsonify({'success': False, 'error': 'IP address required'})
    
    result = bridge_controller.pair_bridge(ip, app_name, device_name)

    if result.get('success'):
        config_notifier.notify_change("bridge_config", result)

    return jsonify(result)

@bridge_bp.route('/api/bridge/config', methods=['GET'])
def get_bridge_config():
    """Get the current bridge configuration"""
    result = bridge_controller.get_config_with_status()
    return jsonify(result)

@bridge_bp.route('/api/bridge/config/exists', methods=['GET'])
def check_config_exists():
    """Check if bridge configuration exists (fast, no connection test)"""
    config = bridge_controller.load_config()
    if config:
        return jsonify({
            'success': True,
            'configured': True,
            'config': {
                'ip': config.get('ip')
            }
        })
    return jsonify({'success': True, 'configured': False})

@bridge_bp.route('/api/bridge/config', methods=['DELETE'])
def delete_bridge_config():
    """Delete the bridge configuration"""
    try:
        bridge_controller.delete_config()
        config_notifier.notify_change("bridge_config_deleted", {})
        return jsonify({'success': True, 'message': 'Bridge configuration deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@bridge_bp.route('/api/bridge/test', methods=['POST'])
def test_bridge():
    """Test the bridge connection"""
    result = bridge_controller.test_connection()
    return jsonify(result)

@bridge_bp.route('/api/bridge/touchlink', methods=['POST'])
def enable_touchlink():
    """Enable Touchlink on the Hue bridge"""
    result = bridge_controller.enable_touchlink()
    return jsonify(result)
