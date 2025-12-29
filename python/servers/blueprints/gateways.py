"""Gateways routes blueprint for ESP-NOW UDP network gateways."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager
from network.network_server import network_server
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from constants import FILE_GATEWAYS
from services import config_notifier

gateways_bp = Blueprint('gateways', __name__, url_prefix='/gateways')

def ping_single_gateway(server):
    """Ping a single gateway and return its status."""
    gateway_mac = server.get('mac_address')
    
    if not gateway_mac:
        return {
            'id': server.get('id'),
            'name': server.get('name'),
            'mac_address': server.get('mac_address'),
            'radio_mac': server.get('radio_mac'),
            'ip_address': server.get('ip_address'),
            'last_used': server.get('last_used'),
            'status': 'offline',
            'uptime': None,
            'led_on_time': server.get('led_on_time'),
            'led_off_time': server.get('led_off_time'),
        }
    
    # Ping gateway via UDP
    uptime = network_server.send_ping(gateway_mac, timeout=2.0)
    
    return {
        'id': server.get('id'),
        'name': server.get('name'),
        'mac_address': server.get('mac_address'),
        'radio_mac': server.get('radio_mac'),
        'ip_address': server.get('ip_address'),
        'last_used': server.get('last_used'),
        'status': 'online' if uptime is not None else 'offline',
        'uptime': uptime,
        'led_on_time': server.get('led_on_time'),
        'led_off_time': server.get('led_off_time'),
    }


def get_gateways():
    """Get all gateways from JSON file."""
    gateways = data_manager.read_json(FILE_GATEWAYS, default=[])
    return gateways


def get_gateways_with_status():
    """Get all gateways with status from UDP network (concurrent pings)."""
    gateways = get_gateways()
    results_dict = {}
    
    # Ping all gateways concurrently
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_index = {executor.submit(ping_single_gateway, gateway): idx for idx, gateway in enumerate(gateways)}
        
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                gateway_status = future.result()
                results_dict[idx] = gateway_status
            except Exception as e:
                # If ping fails, return offline status
                orig = gateways[idx]
                results_dict[idx] = {
                    'id': orig.get('id'),
                    'name': orig.get('name'),
                    'mac_address': orig.get('mac_address'),
                    'radio_mac': orig.get('radio_mac'),
                    'ip_address': orig.get('ip_address'),
                    'last_used': orig.get('last_used'),
                    'status': 'offline',
                    'uptime': None,
                    'led_on_time': orig.get('led_on_time'),
                    'led_off_time': orig.get('led_off_time'),
                }
    
    # Rebuild results in original order
    results = [results_dict[i] for i in range(len(gateways))]
    return results


def save_gateways(gateways):
    """Save gateways to JSON file."""
    data_manager.write_json(FILE_GATEWAYS, gateways)


def get_gateway_by_id(gateway_id):
    """Get a specific gateway by ID."""
    gateways = get_gateways()
    for gateway in gateways:
        if gateway['id'] == gateway_id:
            return gateway
    return None


def update_gateway(gateway_id, updates):
    """Update a gateway's data."""
    gateways = get_gateways()
    for gateway in gateways:
        if gateway['id'] == gateway_id:
            gateway.update(updates)
            save_gateways(gateways)
            return gateway
    return None


@gateways_bp.route('/')
def gateways_page():
    """Render the gateways management page."""
    return render_template('gateways.html')


@gateways_bp.route('/api/gateways', methods=['GET'])
def get_gateways_route():
    """Get all gateways with live status."""
    gateways = get_gateways_with_status()
    
    return jsonify({"success": True, "gateways": gateways})


@gateways_bp.route('/api/gateways/<gateway_id>', methods=['GET'])
def get_gateway_route(gateway_id):
    """Get a specific gateway with live status."""
    gateways = get_gateways_with_status()
    for gateway in gateways:
        if gateway['id'] == gateway_id:
            return jsonify({"success": True, "gateway": gateway})
    return jsonify({"success": False, "error": "Gateway not found"}), 404


@gateways_bp.route('/api/gateways/<gateway_id>', methods=['PUT'])
def update_gateway_route(gateway_id):
    """Update a gateway's name."""
    data = request.get_json()
    new_name = data.get('name')
    
    if not new_name:
        return jsonify({"success": False, "error": "Name is required"}), 400
    
    gateway = update_gateway(gateway_id, {'name': new_name})
    
    if gateway:
        return jsonify({"success": True, "gateway": gateway})
    else:
        return jsonify({"success": False, "error": "Gateway not found"}), 404


@gateways_bp.route('/api/gateways/<gateway_id>', methods=['DELETE'])
def delete_gateway(gateway_id):
    """Delete a gateway."""
    gateways = get_gateways()
    
    # Find the gateway to delete (to get MAC addresses for notification)
    deleted_gateway = None
    for gw in gateways:
        if gw['id'] == gateway_id:
            deleted_gateway = gw
            break
    
    # Find and remove the gateway
    original_length = len(gateways)
    gateways = [gateway for gateway in gateways if gateway['id'] != gateway_id]
    
    if len(gateways) == original_length:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
    
    save_gateways(gateways)
    
    # Notify network server to remove from routing table
    if deleted_gateway:
        config_notifier.notify_change('gateway_deleted', {
            'gateway_id': gateway_id,
            'wifi_mac': deleted_gateway.get('mac_address'),
            'radio_mac': deleted_gateway.get('radio_mac')
        })
    
    return jsonify({"success": True, "message": "Gateway deleted successfully"})


@gateways_bp.route('/api/gateways/<gateway_id>/ping', methods=['POST'])
def ping_gateway(gateway_id):
    """Send ping to gateway and wait for response."""
    
    gateway = get_gateway_by_id(gateway_id)
    if not gateway:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
    
    gateway_mac = gateway.get('mac_address')
    if not gateway_mac:
        return jsonify({"success": False, "error": "Gateway MAC address not found"}), 400
    
    uptime = network_server.send_ping(gateway_mac, timeout=3.0)
    
    if uptime is not None:
        # Convert uptime to human readable format
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        seconds = uptime % 60
        uptime_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
        
        return jsonify({
            "success": True,
            "message": f"Gateway {gateway['name']} responded",
            "uptime_seconds": uptime,
            "uptime_formatted": uptime_str
        })
    else:
        return jsonify({
            "success": False,
            "error": "Gateway did not respond (timeout or unreachable)"
        }), 504


@gateways_bp.route('/api/gateways/<gateway_id>/night_mode', methods=['POST'])
def set_night_mode(gateway_id):
    """Enable or disable night mode on gateway."""    
    data = request.get_json()
    enabled = data.get('enabled', False)
    
    gateway = get_gateway_by_id(gateway_id)
    if not gateway:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
    
    gateway_mac = gateway.get('mac_address')
    if not gateway_mac:
        return jsonify({"success": False, "error": "Gateway MAC address not found"}), 400
    
    success = network_server.set_night_mode(gateway_mac, enabled)
    
    if success:
        mode = "enabled" if enabled else "disabled"
        return jsonify({"success": True, "message": f"Night mode {mode} on {gateway['name']}"})
    else:
        return jsonify({"success": False, "error": "Failed to set night mode"}), 500


@gateways_bp.route('/api/gateways/<gateway_id>/led_times', methods=['POST'])
def update_led_times(gateway_id):
    """Update LED on/off times for a gateway in gateways.json."""
    data = request.get_json()
    led_on_time = data.get('led_on_time')
    led_off_time = data.get('led_off_time')
    
    # Allow clearing the schedule by passing None/null for both values
    if led_on_time is None and led_off_time is None:
        # Clear the LED schedule
        gateways = get_gateways()
        gateway_found = False
        
        for gateway in gateways:
            if gateway['id'] == gateway_id:
                gateway.pop('led_on_time', None)
                gateway.pop('led_off_time', None)
                gateway_found = True
                break
        
        if not gateway_found:
            return jsonify({"success": False, "error": "Gateway not found"}), 404
        
        save_gateways(gateways)
        
        # Turn LEDs back on when schedule is cleared
        gateway = get_gateway_by_id(gateway_id)
        if gateway:
            gateway_mac = gateway.get('mac_address')
            if gateway_mac:
                network_server.set_gateway_leds(gateway_mac, True)
        
        return jsonify({"success": True, "message": "LED schedule cleared, LEDs enabled"})
    
    # If only one value is provided, reject
    if led_on_time is None or led_off_time is None:
        return jsonify({"success": False, "error": "Both led_on_time and led_off_time must be provided (or both null to clear)"}), 400
    
    # Validate hours (0-23)
    if not isinstance(led_on_time, int) or not isinstance(led_off_time, int):
        return jsonify({"success": False, "error": "LED times must be integers (hours 0-23)"}), 400
    
    if led_on_time < 0 or led_on_time > 23 or led_off_time < 0 or led_off_time > 23:
        return jsonify({"success": False, "error": "LED times must be between 0-23"}), 400
    
    # Update in gateways.json
    gateways = get_gateways()
    gateway_found = False
    
    for gateway in gateways:
        if gateway['id'] == gateway_id:
            gateway['led_on_time'] = led_on_time
            gateway['led_off_time'] = led_off_time
            gateway_found = True
            break
    
    if not gateway_found:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
    
    save_gateways(gateways)
    
    # Immediately apply the new schedule by syncing LED state
    gateway = get_gateway_by_id(gateway_id)
    if gateway:
        gateway_mac = gateway.get('mac_address')
        if gateway_mac:
            # Check if LEDs should be off right now based on new schedule
            current_hour = datetime.now().hour
            
            if led_off_time < led_on_time:
                leds_should_be_off = led_off_time <= current_hour < led_on_time
            else:
                leds_should_be_off = current_hour >= led_off_time or current_hour < led_on_time
            
            # Send LED command
            network_server.set_gateway_leds(gateway_mac, not leds_should_be_off)
    
    return jsonify({"success": True, "message": f"LED times updated: ON={led_on_time}:00, OFF={led_off_time}:00"})
