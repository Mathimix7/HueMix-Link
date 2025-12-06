"""Main routes blueprint."""
from flask import Blueprint, render_template, jsonify, request
from services import config_manager

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Render the main dashboard page."""
    return render_template('home.html')


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
    
    tcp_port = data.get('tcp_port')
    web_port = data.get('web_port')
    
    if tcp_port is not None:
        if not isinstance(tcp_port, int) or tcp_port < 1 or tcp_port > 65535:
            return jsonify({
                "success": False,
                "error": "TCP port must be between 1 and 65535"
            }), 400
        config_manager.update_tcp_port(tcp_port)
    
    if web_port is not None:
        if not isinstance(web_port, int) or web_port < 1 or web_port > 65535:
            return jsonify({
                "success": False,
                "error": "Web port must be between 1 and 65535"
            }), 400
        config_manager.update_web_port(web_port)
    
    return jsonify({
        "success": True,
        "message": "Configuration updated. Restart required for changes to take effect.",
        "config": config_manager.load_config()
    })


@main_bp.route('/api/config/restart', methods=['POST'])
def update_config_and_restart():
    """Update configuration and restart servers."""
    import threading
    import time
    from servers.tcp_server import tcp_server
    
    data = request.get_json()
    
    tcp_port = data.get('tcp_port')
    web_port = data.get('web_port')
    
    # Validate ports
    if tcp_port is not None:
        if not isinstance(tcp_port, int) or tcp_port < 1 or tcp_port > 65535:
            return jsonify({
                "success": False,
                "error": "TCP port must be between 1 and 65535"
            }), 400
    
    if web_port is not None:
        if not isinstance(web_port, int) or web_port < 1 or web_port > 65535:
            return jsonify({
                "success": False,
                "error": "Web port must be between 1 and 65535"
            }), 400
    
    # Get current ports
    current_tcp_port = config_manager.get_tcp_port()
    current_web_port = config_manager.get_web_port()
    
    # Save new configuration
    if tcp_port is not None:
        config_manager.update_tcp_port(tcp_port)
    if web_port is not None:
        config_manager.update_web_port(web_port)
    
    # Restart TCP server if port changed
    if tcp_port is not None and tcp_port != current_tcp_port:
        def restart_tcp():
            time.sleep(0.5)  # Small delay
            tcp_server.stop()
            time.sleep(0.5)
            tcp_server.port = tcp_port
            tcp_server.start()
        
        threading.Thread(target=restart_tcp, daemon=True).start()
    
    # Note: Flask server restart requires application restart
    # We can't restart Flask from within Flask itself
    message = "Settings saved! "
    if tcp_port is not None and tcp_port != current_tcp_port:
        message += "TCP server is restarting. "
    if web_port is not None and web_port != current_web_port:
        message += "Please restart the application manually to apply web server port changes."
    else:
        message += "Changes applied successfully."
    
    return jsonify({
        "success": True,
        "message": message,
        "config": config_manager.load_config(),
        "tcp_restarted": tcp_port is not None and tcp_port != current_tcp_port,
        "web_restart_required": web_port is not None and web_port != current_web_port
    })
