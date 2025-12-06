"""Servers routes blueprint for ESP-NOW TCP servers."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

servers_bp = Blueprint('servers', __name__, url_prefix='/servers')

# Data files
SERVERS_FILE = 'servers.json'


def fetch_server_status(server):
    """Fetch status from a server's /status endpoint."""
    try:
        response = requests.get(f"http://{server['ip_address']}/status", timeout=2)
        if response.ok:
            status_data = response.json()

            return {
                'id': server.get('id'),
                'name': server.get('name'),
                'mac_address': server.get('mac_address'),
                'ip_address': server.get('ip_address'),
                'last_used': server.get('last_used'),
                'status': 'online',
                'uptime': status_data.get('uptime'),
                'led_on_time': status_data.get('led_on_time'),
                'led_off_time': status_data.get('led_off_time'),
                'tcp_port': status_data.get('port'),
            }
    except:
        pass
    
    return {
        'id': server['id'],
        'status': 'offline'
    }


def get_servers():
    """Get all servers from JSON file."""
    servers = data_manager.read_json(SERVERS_FILE, default=[])
    return servers


def get_servers_with_status():
    """Get all servers with real-time status from devices."""
    servers = get_servers()
    # Fetch status from all servers concurrently
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_server = {executor.submit(fetch_server_status, server): server for server in servers}

        for future in as_completed(future_to_server):
            orig = future_to_server[future]
            try:
                status_data = future.result()
                if status_data and status_data.get('status') == 'online':
                    # Build returned server object: include static identifying
                    # fields from JSON plus all live fields from the device.
                    results.append({
                        'id': status_data.get('id') or orig.get('id'),
                        'name': status_data.get('name') or orig.get('name'),
                        'mac_address': status_data.get('mac_address') or orig.get('mac_address'),
                        'ip_address': status_data.get('ip_address') or orig.get('ip_address'),
                        'status': 'online',
                        'uptime': status_data.get('uptime'),
                        'last_used': status_data.get('last_used'),
                        'led_on_time': status_data.get('led_on_time'),
                        'led_off_time': status_data.get('led_off_time'),
                        'tcp_port': status_data.get('tcp_port'),
                    })
                else:
                    # Device unreachable or offline: return minimal info with offline status
                    results.append({
                        'id': orig.get('id'),
                        'name': orig.get('name'),
                        'mac_address': orig.get('mac_address'),
                        'ip_address': orig.get('ip_address'),
                        'status': 'offline'
                    })
            except Exception:
                results.append({
                    'id': orig.get('id'),
                    'name': orig.get('name'),
                    'mac_address': orig.get('mac_address'),
                    'ip_address': orig.get('ip_address'),
                    'status': 'offline'
                })

    return results


def save_servers(servers):
    """Save servers to JSON file."""
    data_manager.write_json(SERVERS_FILE, servers)


def get_server_by_id(server_id):
    """Get a specific server by ID."""
    servers = get_servers()
    for server in servers:
        if server['id'] == server_id:
            return server
    return None


def update_server(server_id, updates):
    """Update a server's data."""
    servers = get_servers()
    for server in servers:
        if server['id'] == server_id:
            server.update(updates)
            save_servers(servers)
            return server
    return None


@servers_bp.route('/')
def servers_page():
    """Render the servers management page."""
    return render_template('servers.html')


@servers_bp.route('/api/servers', methods=['GET'])
def get_servers_route():
    """Get all servers with live status."""
    servers = get_servers_with_status()
    return jsonify({"success": True, "servers": servers})


@servers_bp.route('/api/servers/<server_id>', methods=['GET'])
def get_server_route(server_id):
    """Get a specific server with live status."""
    servers = get_servers_with_status()
    for server in servers:
        if server['id'] == server_id:
            return jsonify({"success": True, "server": server})
    return jsonify({"success": False, "error": "Server not found"}), 404


@servers_bp.route('/api/servers/<server_id>/led-times', methods=['POST'])
def update_led_times(server_id):
    """Update LED on/off times for a server."""
    data = request.get_json()
    led_on_time = data.get('led_on_time')
    led_off_time = data.get('led_off_time')
    
    if not led_on_time or not led_off_time:
        return jsonify({"success": False, "error": "Missing LED times"}), 400
    
    # Get server info from JSON (only for IP address)
    server = get_server_by_id(server_id)
    if not server:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    # Send request to device to update LED times
    try:
        response = requests.get(
            f"http://{server['ip_address']}/led_off_times",
            params={'led_on_time': led_on_time, 'led_off_time': led_off_time},
            timeout=5
        )
        
        if response.ok and response.text.strip().upper() == 'OK':
            return jsonify({"success": True, "message": "LED times updated on device"})
        else:
            return jsonify({"success": False, "error": f"Device returned: {response.text}"}), 500
    except requests.Timeout:
        return jsonify({"success": False, "error": "Device did not respond in time"}), 504
    except requests.RequestException as e:
        return jsonify({"success": False, "error": f"Failed to contact device: {str(e)}"}), 500


@servers_bp.route('/api/servers/<server_id>/port', methods=['POST'])
def update_port(server_id):
    """Update TCP port for a server."""
    data = request.get_json()
    port = data.get('port')
    
    if not port or not isinstance(port, int) or port < 0 or port > 65535:
        return jsonify({"success": False, "error": "Invalid port number"}), 400
    
    # Get server info from JSON (only for IP address)
    server = get_server_by_id(server_id)
    if not server:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    # Send request to device to update port
    try:
        response = requests.get(
            f"http://{server['ip_address']}/new_port",
            params={'port': port},
            timeout=5
        )
        
        if response.ok and response.text.strip().upper() == 'OK':
            return jsonify({"success": True, "message": f"Port updated to {port} on device"})
        else:
            return jsonify({"success": False, "error": f"Device returned: {response.text}"}), 500
    except requests.Timeout:
        return jsonify({"success": False, "error": "Device did not respond in time"}), 504
    except requests.RequestException as e:
        return jsonify({"success": False, "error": f"Failed to contact device: {str(e)}"}), 500


@servers_bp.route('/api/servers/<server_id>', methods=['DELETE'])
def delete_server(server_id):
    """Delete a server."""
    servers = get_servers()
    
    # Find and remove the server
    original_length = len(servers)
    servers = [server for server in servers if server['id'] != server_id]
    
    if len(servers) == original_length:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    save_servers(servers)
    
    return jsonify({"success": True, "message": "Server deleted successfully"})
