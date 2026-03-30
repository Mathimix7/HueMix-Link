"""Mesh visualization blueprint for gateway network topology."""
from flask import Blueprint, render_template, jsonify
from services.data_manager import data_manager
from constants import FILE_BUTTONS, FILE_GATEWAYS, FILE_LIGHTSTRIPS

mesh_bp = Blueprint('mesh', __name__, url_prefix='/mesh')


def _extract_serial_port(endpoint):
    if not endpoint or not isinstance(endpoint, str):
        return None
    if endpoint.startswith('serial://'):
        return endpoint.replace('serial://', '', 1)
    return None


def _is_serial_gateway(server):
    endpoint = server.get('transport_endpoint') or server.get('ip_address')
    return (server.get('transport') == 'usb_serial') or (isinstance(endpoint, str) and endpoint.startswith('serial://'))


@mesh_bp.route('/')
def mesh_view():
    """Render mesh visualization page."""
    return render_template('mesh.html')


@mesh_bp.route('/api/topology')
def get_topology():
    """Get network topology data for visualization.
    
    Returns:
        JSON with nodes and edges:
        {
            "nodes": [
                {"id": "mac", "type": "gateway|button|light", "label": "name", 
                 "ip": "...", "online": true, "rssi": -45}
            ],
            "edges": [
                {"from": "gateway_mac", "to": "device_mac", "rssi": -45, 
                 "failures": 0, "success_rate": 100}
            ]
        }
    """
    try:
        # Import network server to get live gateway table
        nodes = []
        edges = []
        
        # Add gateway nodes (serial gateways first)
        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        servers = sorted(servers, key=lambda s: (0 if _is_serial_gateway(s) else 1, (s.get('name') or '').lower()))
        for server in servers:
            radio_mac = server.get('radio_mac')
            if not radio_mac:
                continue
            is_serial = _is_serial_gateway(server)
            serial_endpoint = server.get('transport_endpoint') or server.get('ip_address')
            nodes.append({
                'id': radio_mac,
                'type': 'gateway',
                'label': server.get('name', f"Gateway {radio_mac[-8:]}"),
                'ip': server.get('ip_address'),
                'wifi_mac': server.get('mac_address'),
                'is_serial': is_serial,
                'serial_endpoint': serial_endpoint if is_serial else None,
                'serial_port': _extract_serial_port(serial_endpoint) if is_serial else None,
            })

        # Add lightstrip nodes and routing edges
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        for strip in lightstrips:
            light_mac = strip.get('mac_address')
            if not light_mac:
                continue
            nodes.append({
                'id': light_mac,
                'type': 'light',
                'label': strip.get('name', f"Light {light_mac[-8:]}"),
                'room': strip.get('room_id'),
                'num_leds': strip.get('number_colors', 40)
            })
            # Add routing edge to gateway
            last_gateway_mac = strip.get('last_gateway_mac')
            if last_gateway_mac:
                edges.append({
                    'from': last_gateway_mac,
                    'to': light_mac,
                    'type': 'route',
                    'is_active': True
                })

        # Add button nodes
        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        for button in buttons:
            button_mac = button.get('mac_address')
            if not button_mac:
                continue
            rssi = button.get('rssi')

            config = button.get('config', {}) if button.get('config') else {}
            nodes.append({
                'id': button_mac,
                'type': 'button',
                'label': button.get('name', f"Button {button_mac[-8:]}"),
                'room': config.get('room_id', None),
                'rssi': rssi,
                'configured': button.get('configured', False)
            })
            # Add edge to last seen gateway
            last_gateway = button.get('last_seen_gateway')
            if last_gateway:
                edges.append({
                    'from': button_mac,
                    'to': last_gateway,
                    'type': 'button_signal',
                    'rssi': rssi
                })
        
        return jsonify({
            'success': True,
            'nodes': nodes,
            'edges': edges
        })
    
    except Exception as e:
        raise e
        return jsonify({
            'success': False,
            'error': str(e),
            'nodes': [],
            'edges': []
        }), 500


@mesh_bp.route('/api/gateway/<mac>')
def get_gateway_details(mac):
    """Get details for a specific gateway.
    
    Args:
        mac: Gateway radio MAC address
        
    Returns:
        Gateway details with device counts
    """
    try:
        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        gateway = next((s for s in servers if s.get('radio_mac', '').upper() == mac.upper()), None)
        
        if not gateway:
            return jsonify({
                'success': False,
                'error': 'Gateway not found'
            }), 404
        
        # Count devices routed through this gateway
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        lights_count = sum(1 for s in lightstrips if s.get('last_gateway_mac', '').upper() == mac.upper())
        
        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        buttons_count = sum(1 for b in buttons if b.get('last_seen_gateway', '').upper() == mac.upper())
        
        return jsonify({
            'success': True,
            'gateway': {
                'radio_mac': gateway.get('radio_mac'),
                'wifi_mac': gateway.get('mac_address'),
                'name': gateway.get('name'),
                'ip': gateway.get('ip_address'),
                'last_used': gateway.get('last_used'),
                'lights_count': lights_count,
                'buttons_count': buttons_count
            }
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@mesh_bp.route('/api/device/<mac>/history')
def get_device_route_history(mac):
    """Get gateway routing history for a device.
    
    Args:
        mac: Device MAC address
        
    Returns:
        Routing history with success/failure stats
    """
    try:
        # Check if it's a lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        strip = next((s for s in lightstrips if s.get('mac_address', '').upper() == mac.upper()), None)
        
        if strip:
            # Get current gateway info
            current_gateway_mac = strip.get('last_gateway_mac')
            current_gateway_ip = strip.get('gateway_ip')
            
            # Enrich with gateway name
            gateway_name = None
            if current_gateway_mac:
                servers = data_manager.read_json(FILE_GATEWAYS, default=[])
                gateway = next((s for s in servers if s.get('radio_mac', '').upper() == current_gateway_mac.upper()), None)
                if gateway:
                    gateway_name = gateway.get('name')
            
            return jsonify({
                'success': True,
                'device_type': 'light',
                'device_name': strip.get('name'),
                'current_gateway': current_gateway_mac,
                'current_gateway_name': gateway_name,
                'current_gateway_ip': current_gateway_ip
            })
        
        # Not found
        return jsonify({
            'success': False,
            'error': 'Device not found or no routing history available'
        }), 404
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
