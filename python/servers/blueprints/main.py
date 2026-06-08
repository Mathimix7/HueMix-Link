"""Main routes blueprint."""
from flask import Blueprint, render_template, jsonify, request
from services import config_manager
from services.plugin_manager import plugin_manager
import subprocess
import threading

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Render the main dashboard page."""
    # Build plugin home boxes server-side for immediate render (no client delay)
    boxes = []
    loaded_ids = {
        str(runtime.definition.plugin_id)
        for runtime in getattr(plugin_manager, '_loaded_plugins', [])
        if getattr(runtime, 'definition', None)
    }
    for raw_entry in plugin_manager.list_registered_plugins():
        try:
            plugin_id = str(raw_entry.get('plugin_id') or raw_entry.get('id') or '').strip()
            if not raw_entry.get('enabled') or plugin_id not in loaded_ids:
                continue
            hb = raw_entry.get('home_box')
            if not isinstance(hb, (dict, list)):
                continue
            for entry in (hb if isinstance(hb, list) else [hb]):
                boxes.append({
                    'plugin_id': raw_entry.get('plugin_id') or raw_entry.get('id'),
                    'plugin_name': raw_entry.get('id'),
                    'name': entry.get('name') or raw_entry.get('id'),
                    'description': entry.get('description') or '',
                    'icon': entry.get('icon') or '',
                    'color': entry.get('color') or '',
                    'link': entry.get('link') or '#'
                })
        except Exception:
            pass

    return render_template('home.html', plugin_home_boxes=boxes)


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
    
    return jsonify({
        "success": True,
        "message": message,
        "config": config_manager.load_config(),
        "udp_restart_required": udp_port is not None and udp_port != current_udp_port,
    })


@main_bp.route('/api/server/restart', methods=['POST'])
def restart_server():
    """Restart the entire HueMix-Link systemd service."""
    # Trigger full system restart in background thread
    def restart_service():
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "huemix-link"],
                check=True,
                timeout=30
            )
        except Exception as e:
            print(f"Service restart error: {e}")
    
    thread = threading.Thread(target=restart_service, daemon=True, name="ServiceRestart")
    thread.start()
    
    return jsonify({
        "success": True,
        "message": "Server restart initiated...",
    })
