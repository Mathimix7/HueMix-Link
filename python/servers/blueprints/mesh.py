"""Mesh visualization blueprint for gateway network topology."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request
from network.network_server import network_server
from services.data_manager import data_manager
from services.hue_state_manager import hue_state_manager
from constants import (
    DEV_BUTTON,
    DEV_DOOR,
    DEV_MOTION,
    DEV_REMOTE,
    FILE_BUTTONS,
    FILE_DOOR_SENSORS,
    FILE_GATEWAYS,
    FILE_LIGHTSTRIPS,
    FILE_MOTION_SENSORS,
)

mesh_bp = Blueprint('mesh', __name__, url_prefix='/mesh')

# Devices reporting no usable RSSI store 0 dBm as a sentinel value.
_RSSI_SENTINEL = 0
_RSSI_MIN = -5


def _extract_serial_port(endpoint):
    if not endpoint or not isinstance(endpoint, str):
        return None
    if endpoint.startswith('serial://'):
        return endpoint.replace('serial://', '', 1)
    return None


def _is_serial_gateway(server):
    endpoint = server.get('transport_endpoint') or server.get('ip_address')
    return (server.get('transport') == 'usb_serial') or (isinstance(endpoint, str) and endpoint.startswith('serial://'))


def _get_gateway_ping_target(server):
    """Return gateway MAC to use for ping lookups in NetworkServer."""
    gateway_mac = (server.get('mac_address') or server.get('radio_mac') or '').strip()
    return gateway_mac or None


def _get_gateway_status_map(servers, timeout=2.0):
    """Ping gateways concurrently and return online/uptime by radio MAC."""
    status_map = {}
    ping_jobs = []

    for server in servers:
        radio_mac = (server.get('radio_mac') or '').upper()
        if not radio_mac:
            continue

        status_map[radio_mac] = {
            'online': False,
            'uptime': None,
        }

        target_mac = _get_gateway_ping_target(server)
        if target_mac:
            ping_jobs.append((radio_mac, target_mac))

    if not ping_jobs:
        return status_map

    max_workers = min(10, len(ping_jobs))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_radio_mac = {
            executor.submit(network_server.send_ping, target_mac, timeout): radio_mac
            for radio_mac, target_mac in ping_jobs
        }

        for future in as_completed(future_to_radio_mac):
            radio_mac = future_to_radio_mac[future]
            try:
                uptime = future.result()
                status_map[radio_mac] = {
                    'online': uptime is not None,
                    'uptime': uptime,
                }
            except Exception:
                status_map[radio_mac] = {
                    'online': False,
                    'uptime': None,
                }

    return status_map


def _get_room_name(room_id, config=None):
    """Resolve a display name for a room id from config or Hue state manager."""
    if not room_id:
        return None
    if isinstance(config, dict) and config.get('room_name'):
        return config.get('room_name')
    try:
        rooms = hue_state_manager.get_all_rooms()
        room = rooms.get(room_id) or {}
        name = room.get('name')
        return name or None
    except Exception:
        return None


def _get_remote_room_names(config):
    """Remotes map each button to its own room inside config.buttons[]."""
    if not isinstance(config, dict):
        return None
    names = []
    for button_cfg in config.get('buttons') or []:
        if not isinstance(button_cfg, dict):
            continue
        name = _get_room_name(button_cfg.get('room_id'), button_cfg)
        if name and name not in names:
            names.append(name)
    if names:
        return ' · '.join(names)
    return _get_room_name(config.get('room_id'), config)


def _normalize_rssi(rssi):
    """Treat sentinel/None RSSI values as no signal."""
    if rssi is None:
        return None
    try:
        value = int(rssi)
    except (TypeError, ValueError):
        return None
    if value >= _RSSI_MIN:
        return None
    return value


def _signal_strength(rssi):
    """Classify RSSI into a signal quality bucket."""
    if rssi is None:
        return 'none'
    if rssi >= -55:
        return 'strong'
    if rssi >= -70:
        return 'weak'
    return 'poor'


def _get_light_rssi_map(lights):
    """Ping lights through their assigned gateway concurrently."""
    rssi_map = {}
    jobs = []
    for light in lights:
        light_mac = light.get('mac_address')
        last_gateway = light.get('last_gateway_mac')
        if light_mac and last_gateway:
            jobs.append((light_mac, last_gateway))

    if not jobs:
        return rssi_map

    max_workers = min(8, len(jobs))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_light = {
            executor.submit(
                network_server.ping_device_single_gateway, light_mac, last_gateway, 2.0
            ): light_mac
            for light_mac, last_gateway in jobs
        }
        for future in as_completed(future_to_light):
            light_mac = future_to_light[future]
            try:
                result = future.result()
                if result:
                    for gateway_mac, rssi in result.items():
                        rssi_map[light_mac] = _normalize_rssi(rssi)
                        break
            except Exception:
                continue

    return rssi_map


def _device_last_seen(raw):
    """Return an ISO timestamp string for a device, or None."""
    return raw if raw else None


@mesh_bp.route('/')
def mesh_view():
    """Render mesh visualization page."""
    return render_template('mesh.html')


@mesh_bp.route('/api/topology')
def get_topology():
    """Get network topology data for visualization.

    Returns:
        JSON with summary, nodes and edges covering every device type:
        gateways, lights, buttons, remotes, motion sensors, door sensors
        and unpaired/discovered devices.
    """
    try:
        nodes = []
        edges = []
        gateway_macs = set()

        # --- Gateways -----------------------------------------------------
        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        servers = sorted(servers, key=lambda s: (0 if _is_serial_gateway(s) else 1, (s.get('name') or '').lower()))
        gateway_status_map = _get_gateway_status_map(servers, timeout=2.0)
        gateway_by_radio_mac = {}

        for server in servers:
            radio_mac = server.get('radio_mac')
            if not radio_mac:
                continue
            radio_mac = radio_mac.upper()
            gateway_macs.add(radio_mac)
            is_serial = _is_serial_gateway(server)
            serial_endpoint = server.get('transport_endpoint') or server.get('ip_address')
            status = gateway_status_map.get(radio_mac, {'online': False, 'uptime': None})
            gateway_by_radio_mac[radio_mac] = server
            nodes.append({
                'id': radio_mac,
                'device_id': server.get('id'),
                'type': 'gateway',
                'label': server.get('name', f"Gateway {radio_mac[-8:]}"),
                'mac': radio_mac,
                'ip': server.get('ip_address'),
                'wifi_mac': server.get('mac_address'),
                'is_serial': is_serial,
                'serial_endpoint': serial_endpoint if is_serial else None,
                'serial_port': _extract_serial_port(serial_endpoint) if is_serial else None,
                'online': status['online'],
                'status': 'online' if status['online'] else 'offline',
                'uptime': status['uptime'],
                'version_net': server.get('version_net'),
                'version_radio': server.get('version_radio'),
                'last_used': server.get('last_used'),
            })

        # --- Lights --------------------------------------------------------
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        light_rssi_map = _get_light_rssi_map(lightstrips)
        for strip in lightstrips:
            light_mac = strip.get('mac_address')
            if not light_mac:
                continue
            light_mac = light_mac.upper()
            last_gateway = (strip.get('last_gateway_mac') or '').upper() or None
            rssi = light_rssi_map.get(light_mac)
            nodes.append({
                'id': light_mac,
                'device_id': strip.get('id'),
                'type': 'light',
                'label': strip.get('name', f"Light {light_mac[-8:]}"),
                'mac': light_mac,
                'gateway': last_gateway if last_gateway in gateway_macs else None,
                'rssi': rssi,
                'signal': _signal_strength(rssi),
                'online': rssi is not None,
                'room_id': strip.get('room_id'),
                'room_name': _get_room_name(strip.get('room_id')),
                'num_leds': strip.get('number_colors', 40),
                'last_seen': _device_last_seen(strip.get('last_seen')),
            })
            if last_gateway in gateway_macs:
                edges.append({
                    'from': last_gateway,
                    'to': light_mac,
                    'type': 'route',
                    'rssi': rssi,
                    'signal': _signal_strength(rssi),
                    'is_active': True,
                })

        # --- Buttons & remotes ----------------------------------------------
        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        for button in buttons:
            button_mac = button.get('mac_address')
            if not button_mac:
                continue
            button_mac = button_mac.upper()
            device_type = button.get('device_type')
            is_remote = device_type == DEV_REMOTE
            rssi = _normalize_rssi(button.get('rssi'))
            config = button.get('config') if isinstance(button.get('config'), dict) else {}
            last_gateway = (button.get('last_seen_gateway') or '').upper() or None
            node = {
                'id': button_mac,
                'device_id': button.get('id'),
                'type': 'remote' if is_remote else 'button',
                'label': button.get('name', f"{'Remote' if is_remote else 'Button'} {button_mac[-8:]}"),
                'mac': button_mac,
                'gateway': last_gateway if last_gateway in gateway_macs else None,
                'rssi': rssi,
                'signal': _signal_strength(rssi),
                'configured': button.get('configured', False),
                'room_name': _get_remote_room_names(config) if is_remote else _get_room_name(config.get('room_id'), config),
                'battery_percent': button.get('battery_percent'),
                'battery_mv': button.get('battery_mv'),
                'battery_last_updated': button.get('battery_last_updated'),
                'version': button.get('version'),
                'platform': button.get('platform'),
                'button_count': button.get('button_count'),
                'last_seen': _device_last_seen(button.get('last_seen')),
            }
            nodes.append(node)
            if last_gateway in gateway_macs:
                edges.append({
                    'from': button_mac,
                    'to': last_gateway,
                    'type': 'signal',
                    'rssi': rssi,
                    'signal': _signal_strength(rssi),
                    'is_active': True,
                })

        # --- Motion sensors ---------------------------------------------------
        motion_sensors = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
        for sensor in motion_sensors:
            sensor_mac = sensor.get('mac_address')
            if not sensor_mac:
                continue
            sensor_mac = sensor_mac.upper()
            rssi = _normalize_rssi(sensor.get('rssi'))
            config = sensor.get('config') if isinstance(sensor.get('config'), dict) else {}
            last_gateway = (sensor.get('last_seen_gateway') or '').upper() or None
            nodes.append({
                'id': sensor_mac,
                'device_id': sensor.get('id'),
                'type': 'motion',
                'label': sensor.get('name', f"Motion {sensor_mac[-8:]}"),
                'mac': sensor_mac,
                'gateway': last_gateway if last_gateway in gateway_macs else None,
                'rssi': rssi,
                'signal': _signal_strength(rssi),
                'configured': sensor.get('configured', False),
                'room_name': _get_room_name(config.get('room_id'), config),
                'battery_percent': sensor.get('battery_percent'),
                'battery_mv': sensor.get('battery_mv'),
                'battery_last_updated': sensor.get('battery_last_updated'),
                'version': sensor.get('version'),
                'platform': sensor.get('platform'),
                'light_level': sensor.get('light_level'),
                'enabled': config.get('enabled', True) if config else None,
                'last_seen': _device_last_seen(sensor.get('last_seen')),
            })
            if last_gateway in gateway_macs:
                edges.append({
                    'from': sensor_mac,
                    'to': last_gateway,
                    'type': 'signal',
                    'rssi': rssi,
                    'signal': _signal_strength(rssi),
                    'is_active': True,
                })

        # --- Door sensors ------------------------------------------------------
        door_sensors = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
        for sensor in door_sensors:
            sensor_mac = sensor.get('mac_address')
            if not sensor_mac:
                continue
            sensor_mac = sensor_mac.upper()
            rssi = _normalize_rssi(sensor.get('rssi'))
            config = sensor.get('config') if isinstance(sensor.get('config'), dict) else {}
            last_gateway = (sensor.get('last_seen_gateway') or '').upper() or None
            nodes.append({
                'id': sensor_mac,
                'device_id': sensor.get('id'),
                'type': 'door',
                'label': sensor.get('name', f"Door {sensor_mac[-8:]}"),
                'mac': sensor_mac,
                'gateway': last_gateway if last_gateway in gateway_macs else None,
                'rssi': rssi,
                'signal': _signal_strength(rssi),
                'configured': sensor.get('configured', False),
                'room_name': _get_room_name(config.get('room_id'), config),
                'battery_percent': sensor.get('battery_percent'),
                'battery_mv': sensor.get('battery_mv'),
                'battery_last_updated': sensor.get('battery_last_updated'),
                'version': sensor.get('version'),
                'platform': sensor.get('platform'),
                'state': sensor.get('state', 'unknown'),
                'last_opened': sensor.get('last_opened'),
                'last_closed': sensor.get('last_closed'),
                'last_seen': _device_last_seen(sensor.get('last_seen')),
            })
            if last_gateway in gateway_macs:
                edges.append({
                    'from': sensor_mac,
                    'to': last_gateway,
                    'type': 'signal',
                    'rssi': rssi,
                    'signal': _signal_strength(rssi),
                    'is_active': True,
                })

        # --- Summary ----------------------------------------------------------------
        gateway_nodes = [n for n in nodes if n['type'] == 'gateway']
        device_nodes = [n for n in nodes if n['type'] != 'gateway']

        summary = {
            'gateways': {
                'total': len(gateway_nodes),
                'online': sum(1 for n in gateway_nodes if n['online']),
                'offline': sum(1 for n in gateway_nodes if not n['online']),
                'serial': sum(1 for n in gateway_nodes if n.get('is_serial')),
            },
            'devices': {},
        }
        for node_type in ('light', 'button', 'remote', 'motion', 'door'):
            summary['devices'][node_type] = sum(1 for n in device_nodes if n['type'] == node_type)

        return jsonify({
            'success': True,
            'summary': summary,
            'nodes': nodes,
            'edges': edges,
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'nodes': [],
            'edges': [],
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
        lights = [s for s in lightstrips if s.get('last_gateway_mac', '').upper() == mac.upper()]
        lights_count = len(lights)

        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        buttons_list = [b for b in buttons if b.get('last_seen_gateway', '').upper() == mac.upper()]
        buttons_count = len(buttons_list)

        motion = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
        motion_list = [m for m in motion if m.get('last_seen_gateway', '').upper() == mac.upper()]
        motion_count = len(motion_list)

        door = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
        door_list = [d for d in door if d.get('last_seen_gateway', '').upper() == mac.upper()]
        door_count = len(door_list)

        ping_target = _get_gateway_ping_target(gateway)
        uptime = network_server.send_ping(ping_target, timeout=2.0) if ping_target else None
        online = uptime is not None

        return jsonify({
            'success': True,
            'gateway': {
                'radio_mac': gateway.get('radio_mac'),
                'wifi_mac': gateway.get('mac_address'),
                'name': gateway.get('name'),
                'ip': gateway.get('ip_address'),
                'version_net': gateway.get('version_net'),
                'version_radio': gateway.get('version_radio'),
                'last_used': gateway.get('last_used'),
                'lights_count': lights_count,
                'buttons_count': buttons_count,
                'motion_count': motion_count,
                'door_count': door_count,
                'total_devices': lights_count + buttons_count + motion_count + door_count,
                'online': online,
                'status': 'online' if online else 'offline',
                'uptime': uptime,
            }
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@mesh_bp.route('/api/device/<mac>/history')
def get_device_route_history(mac):
    """Get routing and status history for any device type.

    Args:
        mac: Device MAC address

    Returns:
        Device info with current gateway and status fields
    """
    try:
        target = mac.upper()

        # Check if it's a lightstrip
        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        strip = next((s for s in lightstrips if s.get('mac_address', '').upper() == target), None)
        if strip:
            return jsonify({
                'success': True,
                'device_type': 'light',
                'device_id': strip.get('id'),
                'device_name': strip.get('name'),
                'device_mac': strip.get('mac_address'),
                'current_gateway': strip.get('last_gateway_mac'),
                'current_gateway_ip': strip.get('gateway_ip'),
                'room_id': strip.get('room_id'),
                'rssi': None,
                'last_seen': strip.get('last_seen'),
            })

        # Check buttons / remotes
        buttons = data_manager.read_json(FILE_BUTTONS, default=[])
        button = next((b for b in buttons if b.get('mac_address', '').upper() == target), None)
        if button:
            device_type = 'remote' if button.get('device_type') == DEV_REMOTE else 'button'
            return jsonify({
                'success': True,
                'device_type': device_type,
                'device_id': button.get('id'),
                'device_name': button.get('name'),
                'device_mac': button.get('mac_address'),
                'current_gateway': button.get('last_seen_gateway'),
                'rssi': _normalize_rssi(button.get('rssi')),
                'battery_percent': button.get('battery_percent'),
                'battery_mv': button.get('battery_mv'),
                'version': button.get('version'),
                'platform': button.get('platform'),
                'last_seen': button.get('last_seen'),
            })

        # Check motion sensors
        motion = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
        sensor = next((m for m in motion if m.get('mac_address', '').upper() == target), None)
        if sensor:
            return jsonify({
                'success': True,
                'device_type': 'motion',
                'device_id': sensor.get('id'),
                'device_name': sensor.get('name'),
                'device_mac': sensor.get('mac_address'),
                'current_gateway': sensor.get('last_seen_gateway'),
                'rssi': _normalize_rssi(sensor.get('rssi')),
                'battery_percent': sensor.get('battery_percent'),
                'battery_mv': sensor.get('battery_mv'),
                'version': sensor.get('version'),
                'platform': sensor.get('platform'),
                'last_seen': sensor.get('last_seen'),
            })

        # Check door sensors
        door = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
        door_sensor = next((d for d in door if d.get('mac_address', '').upper() == target), None)
        if door_sensor:
            return jsonify({
                'success': True,
                'device_type': 'door',
                'device_id': door_sensor.get('id'),
                'device_name': door_sensor.get('name'),
                'device_mac': door_sensor.get('mac_address'),
                'current_gateway': door_sensor.get('last_seen_gateway'),
                'rssi': _normalize_rssi(door_sensor.get('rssi')),
                'battery_percent': door_sensor.get('battery_percent'),
                'battery_mv': door_sensor.get('battery_mv'),
                'version': door_sensor.get('version'),
                'platform': door_sensor.get('platform'),
                'last_seen': door_sensor.get('last_seen'),
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


@mesh_bp.route('/api/mesh/optimize', methods=['POST'])
def optimize_light():
    """Ping a lightstrip via all gateways and pick the best route.

    Body:
        {"mac": "<light mac>"}

    Returns:
        Per-gateway RSSI results, the best gateway and whether routing
        was updated.
    """
    try:
        data = request.get_json(silent=True) or {}
        mac_address = (data.get('mac') or '').strip().upper()
        if not mac_address:
            return jsonify({'success': False, 'error': 'Light MAC is required'}), 400

        lightstrips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
        lightstrip = next((s for s in lightstrips if s.get('mac_address', '').upper() == mac_address), None)
        if not lightstrip:
            return jsonify({'success': False, 'error': 'Light not found'}), 404

        previous_gateway = lightstrip.get('last_gateway_mac')

        rssi_map = network_server.ping_device(mac_address, timeout=2.0)
        if rssi_map is None:
            return jsonify({'success': False, 'error': 'Ping failed'}), 500

        servers = data_manager.read_json(FILE_GATEWAYS, default=[])
        gateway_by_radio = {s.get('radio_mac', '').upper(): s for s in servers if s.get('radio_mac')}

        gateway_results = []
        for gateway_mac, rssi in (rssi_map or {}).items():
            gateway_info = gateway_by_radio.get(gateway_mac.upper(), {})
            gateway_results.append({
                'gateway_mac': gateway_mac.upper(),
                'name': gateway_info.get('name') or f"Gateway {gateway_mac[-8:]}",
                'rssi': _normalize_rssi(rssi),
                'signal': _signal_strength(_normalize_rssi(rssi)),
                'online': bool(gateway_info),
            })

        gateway_results.sort(key=lambda x: (x['rssi'] is None, -(x['rssi'] or 0)))
        best_gateway = gateway_results[0] if gateway_results else None

        updated = False
        if best_gateway and best_gateway.get('rssi') is not None and best_gateway['gateway_mac'] != previous_gateway:
            lightstrip['last_gateway_mac'] = best_gateway['gateway_mac']
            gateway_info = gateway_by_radio.get(best_gateway['gateway_mac'], {})
            if gateway_info:
                lightstrip['gateway_ip'] = gateway_info.get('ip_address', '')
            data_manager.write_json(FILE_LIGHTSTRIPS, lightstrips)
            updated = True

        return jsonify({
            'success': True,
            'light': {
                'mac': mac_address,
                'name': lightstrip.get('name'),
                'previous_gateway': previous_gateway,
            },
            'gateways': gateway_results,
            'best_gateway': best_gateway,
            'routing_updated': updated,
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500