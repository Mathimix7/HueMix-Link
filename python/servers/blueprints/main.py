"""Main routes blueprint."""
from flask import Blueprint, render_template, jsonify, request
from services import config_manager

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Render the main dashboard page."""
    return render_template('home.html')


@main_bp.route('/pairing')
def pairing():
    """Render the device pairing page."""
    return render_template('pairing.html')


@main_bp.route('/api/config', methods=['GET'])
def get_config():
    """Get application configuration."""
    config = config_manager.load_config()
    return jsonify({
        "success": True,
        "config": config
    })


@main_bp.route('/api/config', methods=['POST'])
def update_config():
    """Update application configuration."""
    data = request.get_json()

    udp_port = data.get('udp_port')
    if udp_port is not None:
        if not isinstance(udp_port, int) or udp_port < 1 or udp_port > 65535:
            return jsonify({
                "success": False,
                "error": "UDP port must be between 1 and 65535"
            }), 400
        config_manager.update_udp_port(udp_port)
    
    return jsonify({
        "success": True,
        "message": "Configuration updated. Restart required for changes to take effect.",
        "config": config_manager.load_config()
    })


@main_bp.route('/api/config/restart', methods=['POST'])
def update_config_and_restart():
    """Update configuration and restart servers."""
    data = request.get_json()

    udp_port = data.get('udp_port')
    
    # Validate UDP port
    if udp_port is not None:
        if not isinstance(udp_port, int) or udp_port < 1 or udp_port > 65535:
            return jsonify({
                "success": False,
                "error": "UDP port must be between 1 and 65535"
            }), 400
    
    # Get current ports
    current_udp_port = config_manager.get_udp_port()
    
    # Save new configuration (only UDP port allowed)
    if udp_port is not None:
        config_manager.update_udp_port(udp_port)
    
    message = "Settings saved! "
    if udp_port is not None and udp_port != current_udp_port:
        message += "UDP server restarting with new port... "
    else:
        message += "No changes detected."
    
    return jsonify({
        "success": True,
        "message": message,
        "config": config_manager.load_config(),
        "udp_restart_required": udp_port is not None and udp_port != current_udp_port,
        "web_restart_required": False
    })
