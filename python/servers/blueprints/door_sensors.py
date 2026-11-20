"""Door sensors routes blueprint."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager, config_notifier
from constants import FILE_DOOR_SENSORS
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

door_sensors_bp = Blueprint('door_sensors', __name__, url_prefix='/door-sensors')

ALLOWED_ACTIONS = {'nothing', 'scene', 'off'}


def _is_valid_hhmm(time_str):
    """Validate 24-hour HH:MM time format."""
    try:
        datetime.strptime(time_str, '%H:%M')
        return True
    except (TypeError, ValueError):
        return False


def _normalize_time_slots(raw_slots):
    """Validate and normalize raw timeslot payload."""
    if not isinstance(raw_slots, list):
        raise ValueError('time_slots must be an array')

    normalized = []
    for index, slot in enumerate(raw_slots):
        if not isinstance(slot, dict):
            raise ValueError(f'time_slots[{index}] must be an object')

        start_time = (slot.get('start_time') or '').strip()
        if not _is_valid_hhmm(start_time):
            raise ValueError(f'time_slots[{index}] start_time must be HH:MM')

        open_action = (slot.get('open_action') or 'nothing').strip()
        close_action = (slot.get('close_action') or 'nothing').strip()

        if open_action not in ALLOWED_ACTIONS:
            raise ValueError(f'Invalid open action in time_slots[{index}]: {open_action}')
        if close_action not in ALLOWED_ACTIONS:
            raise ValueError(f'Invalid close action in time_slots[{index}]: {close_action}')

        open_scene_id = (slot.get('open_scene_id') or '').strip() if open_action == 'scene' else ''
        close_scene_id = (slot.get('close_scene_id') or '').strip() if close_action == 'scene' else ''

        try:
            close_delay_seconds = int(slot.get('close_delay_seconds', 0))
        except (TypeError, ValueError):
            raise ValueError(
                f'time_slots[{index}] close_delay_seconds must be an integer between 0 and 86400'
            )

        if close_delay_seconds < 0 or close_delay_seconds > 86400:
            raise ValueError(f'time_slots[{index}] close_delay_seconds must be between 0 and 86400')

        if open_action == 'scene' and not open_scene_id:
            raise ValueError(f'time_slots[{index}] open action requires a scene')
        if close_action == 'scene' and not close_scene_id:
            raise ValueError(f'time_slots[{index}] close action requires a scene')

        normalized.append({
            'start_time': start_time,
            'open_action': open_action,
            'open_scene_id': open_scene_id,
            'open_scene_name': (slot.get('open_scene_name') or '').strip() if open_action == 'scene' else '',
            'close_action': close_action,
            'close_scene_id': close_scene_id,
            'close_scene_name': (slot.get('close_scene_name') or '').strip() if close_action == 'scene' else '',
            'close_delay_seconds': close_delay_seconds if close_action != 'nothing' else 0,
            'do_not_disturb': bool(slot.get('do_not_disturb', False)),
        })

    normalized.sort(key=lambda x: x.get('start_time', '00:00'))
    return normalized


def _normalize_light_sensitivity(raw_value):
    """Validate light threshold value (0-10)."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError('light_sensitivity must be an integer between 0 and 10')

    if value < 0 or value > 10:
        raise ValueError('light_sensitivity must be between 0 and 10')

    return value


def get_door_sensors():
    """Get all door sensors from JSON file."""
    sensors = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
    updated = False
    for sensor in sensors:
        if 'last_sync' in sensor:
            sensor.pop('last_sync', None)
            updated = True
    if updated:
        data_manager.write_json(FILE_DOOR_SENSORS, sensors)
    return sensors


def save_door_sensors(sensors):
    """Save all door sensors to JSON file."""
    data_manager.write_json(FILE_DOOR_SENSORS, sensors)


def get_door_sensor_states():
    """Return compact state payload for all sensors."""
    sensors = get_door_sensors()
    result = {}
    for sensor in sensors:
        result[sensor['id']] = {
            'state': sensor.get('state', 'unknown'),
            'last_action_at': sensor.get('last_action_at'),
            'last_opened': sensor.get('last_opened'),
            'last_closed': sensor.get('last_closed'),
            'last_seen': sensor.get('last_seen'),
            'battery_type': sensor.get('battery_type'),
            'battery_percent': sensor.get('battery_percent'),
            'battery_mv': sensor.get('battery_mv'),
            'battery_last_updated': sensor.get('battery_last_updated'),
            'light_level': sensor.get('light_level'),
            'light_last_updated': sensor.get('light_last_updated'),
        }
    return result


def get_door_sensor_config(device_id):
    """Get door sensor configuration by device ID."""
    sensors = get_door_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            return sensor.get('config', {})
    return None


def save_door_sensor_config(device_id, config):
    """Save door sensor configuration for a specific device."""
    sensors = get_door_sensors()
    updated = None

    for sensor in sensors:
        if sensor['id'] == device_id:
            sensor['config'] = config
            sensor['configured'] = bool(config.get('target_id') or config.get('room_id'))
            updated = sensor
            break

    if updated is None:
        return None

    save_door_sensors(sensors)
    return updated


@door_sensors_bp.route('/')
def door_sensors_page():
    """Render the door sensors configuration page."""
    return render_template('door_sensors.html')


@door_sensors_bp.route('/api/devices', methods=['GET'])
def get_devices_route():
    """Get all door sensor devices."""
    sensors = get_door_sensors()
    return jsonify({'success': True, 'devices': sensors})


@door_sensors_bp.route('/api/devices/<device_id>', methods=['GET'])
def get_device_route(device_id):
    """Get one door sensor by ID."""
    sensors = get_door_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            return jsonify({'success': True, 'device': sensor})
    return jsonify({'success': False, 'error': 'Device not found'}), 404


@door_sensors_bp.route('/api/door_states', methods=['GET'])
def get_door_states_route():
    """Get current state for all door sensors."""
    states = get_door_sensor_states()
    return jsonify({'success': True, 'door_states': states})


@door_sensors_bp.route('/api/devices/<device_id>/rename', methods=['POST'])
def rename_device(device_id):
    """Rename a door sensor device."""
    data = request.get_json(silent=True) or {}
    new_name = (data.get('name') or '').strip()

    if not new_name:
        return jsonify({'success': False, 'error': 'Device name is required'}), 400

    sensors = get_door_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            old_name = sensor.get('name', 'Unnamed Door Sensor')
            sensor['name'] = new_name
            save_door_sensors(sensors)

            config_notifier.notify_change('door_sensor_rename', {
                'device_id': device_id,
                'old_name': old_name,
                'new_name': new_name,
                'device': sensor,
            })

            return jsonify({'success': True, 'device': sensor})

    return jsonify({'success': False, 'error': 'Device not found'}), 404


@door_sensors_bp.route('/api/configure', methods=['POST'])
def configure_door_sensor():
    """Configure a door sensor with room assignment and time-slot actions."""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')

    if not device_id:
        return jsonify({'success': False, 'error': 'Device ID is required'}), 400

    if get_door_sensor_config(device_id) is None:
        return jsonify({'success': False, 'error': 'Device not found'}), 404

    raw_time_slots = data.get('time_slots')
    if raw_time_slots is None:
        return jsonify({'success': False, 'error': 'time_slots is required'}), 400

    try:
        time_slots = _normalize_time_slots(raw_time_slots)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    room_id = data.get('room_id') or None
    target_id = data.get('target_id') or room_id
    target_type = data.get('target_type', 'room')
    if target_type not in ('room', 'zone'):
        target_type = 'room'
    has_action_slots = any(
        slot.get('open_action') != 'nothing' or slot.get('close_action') != 'nothing'
        for slot in time_slots
    )
    if has_action_slots and not target_id:
        return jsonify({'success': False, 'error': 'Room/zone assignment is required for door time-slot actions'}), 400

    room_name = (data.get('room_name') or '') if target_id else ''

    enabled = bool(data.get('enabled', True))

    try:
        light_sensitivity = _normalize_light_sensitivity(data.get('light_sensitivity', 5))
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    config = {
        'room_id': target_id,
        'room_name': room_name,
        'target_id': target_id,
        'target_type': target_type,
        'enabled': enabled,
        'light_sensitivity': light_sensitivity,
        'time_slots': time_slots,
    }

    updated_sensor = save_door_sensor_config(device_id, config)
    if updated_sensor is None:
        return jsonify({'success': False, 'error': 'Device not found'}), 404

    config_notifier.notify_change('door_sensor_config', {
        'device_id': device_id,
        'mac_address': updated_sensor.get('mac_address'),
        'config': config,
    })

    return jsonify({'success': True, 'config': config, 'device': updated_sensor})


@door_sensors_bp.route('/api/<device_id>/config', methods=['GET'])
def get_door_sensor_config_route(device_id):
    """Get current door sensor config."""
    config = get_door_sensor_config(device_id)
    if config is not None:
        return jsonify({'success': True, 'config': config})
    return jsonify({'success': False, 'error': 'Configuration not found'}), 404


@door_sensors_bp.route('/api/<device_id>/toggle', methods=['POST'])
def toggle_enabled(device_id):
    """Enable or disable a door sensor."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', True))

    sensors = get_door_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            config = sensor.get('config') or {}
            config['enabled'] = enabled
            sensor['config'] = config
            save_door_sensors(sensors)

            config_notifier.notify_change('door_sensor_toggle', {
                'device_id': device_id,
                'enabled': enabled,
            })

            return jsonify({'success': True, 'enabled': enabled})

    return jsonify({'success': False, 'error': 'Device not found'}), 404


@door_sensors_bp.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    """Delete a door sensor from configuration."""
    sensors = get_door_sensors()
    original_length = len(sensors)
    sensors = [sensor for sensor in sensors if sensor['id'] != device_id]

    if len(sensors) == original_length:
        return jsonify({'success': False, 'error': 'Device not found'}), 404

    save_door_sensors(sensors)

    config_notifier.notify_change('door_sensor_delete', {
        'device_id': device_id,
    })

    return jsonify({'success': True, 'message': 'Device deleted successfully'})
