"""Motion Sensors routes blueprint."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager, config_notifier
from constants import FILE_MOTION_SENSORS
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

MOTION_ALLOWED_ACTIONS = {'nothing', 'scene', 'off'}


def _is_valid_hhmm(time_str):
    """Validate 24-hour HH:MM time format."""
    try:
        datetime.strptime(time_str, '%H:%M')
        return True
    except (TypeError, ValueError):
        return False


def _normalize_motion_time_slots(raw_slots):
    """Validate and normalize motion sensor time slots."""
    if not isinstance(raw_slots, list):
        raise ValueError('time_slots must be an array')

    normalized = []
    for index, slot in enumerate(raw_slots):
        if not isinstance(slot, dict):
            raise ValueError(f'time_slots[{index}] must be an object')

        start_time = (slot.get('start_time') or '').strip()
        if not _is_valid_hhmm(start_time):
            raise ValueError(f'time_slots[{index}] start_time must be HH:MM')

        motion_action = (slot.get('motion_action') or 'nothing').strip()
        if motion_action not in MOTION_ALLOWED_ACTIONS:
            raise ValueError(f'time_slots[{index}] invalid motion_action: {motion_action}')

        scene_id = (slot.get('scene_id') or '').strip() if motion_action == 'scene' else ''
        scene_name = (slot.get('scene_name') or '').strip() if motion_action == 'scene' else ''

        if motion_action == 'scene' and not scene_id:
            raise ValueError(f'time_slots[{index}] motion scene action requires a scene')

        after_action = (slot.get('after_action') or 'off').strip()
        if after_action not in MOTION_ALLOWED_ACTIONS:
            raise ValueError(f'time_slots[{index}] invalid after_action: {after_action}')

        after_scene_id = (slot.get('after_scene_id') or '').strip() if after_action == 'scene' else ''
        after_scene_name = (slot.get('after_scene_name') or '').strip() if after_action == 'scene' else ''

        if after_action == 'scene' and not after_scene_id:
            raise ValueError(f'time_slots[{index}] after scene action requires a scene')

        try:
            after_duration_seconds = int(slot.get('after_duration_seconds', 300))
        except (TypeError, ValueError):
            raise ValueError(f'time_slots[{index}] after_duration_seconds must be an integer')

        if after_duration_seconds < 0 or after_duration_seconds > 3600:
            raise ValueError(f'time_slots[{index}] after_duration_seconds must be between 0 and 3600')

        normalized.append({
            'start_time': start_time,
            'motion_action': motion_action,
            'scene_id': scene_id,
            'scene_name': scene_name,
            'after_duration_seconds': after_duration_seconds,
            'after_action': after_action,
            'after_scene_id': after_scene_id,
            'after_scene_name': after_scene_name,
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


motion_sensors_bp = Blueprint('motion_sensors', __name__, url_prefix='/motion-sensors')


def get_motion_sensors():
    """Get all motion sensors from JSON file."""
    return data_manager.read_json(FILE_MOTION_SENSORS, default=[])


def get_motion_sensor_states():
    """Return a dict of sensor_id to last_motion timestamp (ISO string or None)."""
    sensors = get_motion_sensors()
    result = {}
    for sensor in sensors:
        result[sensor['id']] = sensor.get('last_motion')
    return result


@motion_sensors_bp.route('/api/motion_states', methods=['GET'])
def get_motion_states():
    """API endpoint to get last motion state for all sensors."""
    motion_states = get_motion_sensor_states()
    return jsonify({"success": True, "motion_states": motion_states})


def save_motion_sensors(sensors):
    """Save motion sensors to JSON file."""
    data_manager.write_json(FILE_MOTION_SENSORS, sensors)


def get_motion_sensor_config(device_id):
    """Get motion sensor configuration for a specific device."""
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            return sensor.get('config')
    return None


def save_motion_sensor_config(device_id, config):
    """Save motion sensor configuration for a specific device."""
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            sensor['config'] = config
            sensor['configured'] = True
            break
    save_motion_sensors(sensors)


@motion_sensors_bp.route('/')
def motion_sensors_page():
    """Render the motion sensors configuration page."""
    return render_template('motion_sensors.html')


@motion_sensors_bp.route('/api/devices', methods=['GET'])
def get_devices_route():
    """Get all motion sensor devices."""
    sensors = get_motion_sensors()
    return jsonify({"success": True, "devices": sensors})


@motion_sensors_bp.route('/api/devices/<device_id>', methods=['GET'])
def get_device_route(device_id):
    """Get a single motion sensor by id."""
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            return jsonify({"success": True, "device": sensor})
    return jsonify({"success": False, "error": "Device not found"}), 404


@motion_sensors_bp.route('/api/devices/<device_id>/rename', methods=['POST'])
def rename_device(device_id):
    """Rename a motion sensor device."""
    data = request.get_json()
    new_name = data.get('name')
    sensors = get_motion_sensors()
    
    for sensor in sensors:
        if sensor['id'] == device_id:
            old_name = sensor['name']
            sensor['name'] = new_name
            save_motion_sensors(sensors)
            
            # Notify UDP server about the update
            config_notifier.notify_change('motion_sensor_rename', {
                'device_id': device_id,
                'old_name': old_name,
                'new_name': new_name,
                'device': sensor
            })
            
            return jsonify({"success": True, "device": sensor})
    
    return jsonify({"success": False, "error": "Device not found"}), 404


@motion_sensors_bp.route('/api/configure', methods=['POST'])
def configure_motion_sensor():
    """Configure a motion sensor with room and settings."""
    data = request.get_json()
    device_id = data.get('device_id')
    
    config = {
        'room_id': data.get('room_id'),
        'target_id': data.get('target_id') or data.get('room_id'),
        'target_type': data.get('target_type', 'room'),
        'cooldown_seconds': data.get('cooldown_seconds', 60),
        'light_sensitivity': data.get('light_sensitivity', 5),
        'time_slots': data.get('time_slots', []),
        'enabled': data.get('enabled', True)
    }

    # Validate cooldown as an integer in the 5-60 second firmware range
    try:
        cooldown = int(config.get('cooldown_seconds', 60))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Cooldown must be an integer between 5 and 60 seconds"}), 400
    if cooldown < 5 or cooldown > 60:
        return jsonify({"success": False, "error": "Cooldown must be between 5 and 60 seconds"}), 400
    config['cooldown_seconds'] = cooldown

    try:
        config['light_sensitivity'] = _normalize_light_sensitivity(config.get('light_sensitivity', 5))
        config['time_slots'] = _normalize_motion_time_slots(config.get('time_slots', []))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    
    # Save configuration
    save_motion_sensor_config(device_id, config)
    
    # Get the sensor to find MAC address and set pending flag
    sensors = get_motion_sensors()
    sensor_mac = None
    for sensor in sensors:
        if sensor['id'] == device_id:
            sensor['pending_cooldown_update'] = True
            sensor_mac = sensor.get('mac_address')
            save_motion_sensors(sensors)
            break
    
    # Notify UDP server about the configuration update
    config_notifier.notify_change('motion_sensor_config', {
        'device_id': device_id,
        'mac_address': sensor_mac,
        'config': config
    })
    
    return jsonify({"success": True, "config": config})


@motion_sensors_bp.route('/api/<device_id>/config', methods=['GET'])
def get_motion_sensor_config_route(device_id):
    """Get motion sensor configuration."""
    config = get_motion_sensor_config(device_id)
    if config:
        return jsonify({"success": True, "config": config})
    return jsonify({"success": False, "error": "Configuration not found"}), 404


@motion_sensors_bp.route('/api/<device_id>/cooldown', methods=['POST'])
def update_cooldown(device_id):
    """Update motion sensor cooldown period."""
    data = request.get_json()
    
    # Validate cooldown as an integer in the 5-60 second firmware range
    try:
        cooldown_seconds = int(data.get('cooldown_seconds', 60))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Cooldown must be an integer between 5 and 60 seconds"}), 400
    if cooldown_seconds < 5 or cooldown_seconds > 60:
        return jsonify({"success": False, "error": "Cooldown must be between 5 and 60 seconds"}), 400
    
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            if not sensor.get('config'):
                sensor['config'] = {}
            sensor['config']['cooldown_seconds'] = cooldown_seconds
            
            # Set flag to indicate pending cooldown update
            sensor['pending_cooldown_update'] = True
            
            save_motion_sensors(sensors)
            
            # Notify UDP server
            config_notifier.notify_change('motion_sensor_cooldown', {
                'device_id': device_id,
                'mac_address': sensor.get('mac_address'),
                'cooldown_seconds': cooldown_seconds
            })
            
            return jsonify({"success": True, "cooldown_seconds": cooldown_seconds})
    
    return jsonify({"success": False, "error": "Device not found"}), 404


@motion_sensors_bp.route('/api/<device_id>/toggle', methods=['POST'])
def toggle_enabled(device_id):
    """Enable or disable a motion sensor."""
    data = request.get_json()
    enabled = data.get('enabled', True)
    
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            if not sensor.get('config'):
                sensor['config'] = {}
            sensor['config']['enabled'] = enabled
            save_motion_sensors(sensors)
            
            # Notify UDP server
            config_notifier.notify_change('motion_sensor_toggle', {
                'device_id': device_id,
                'enabled': enabled
            })
            
            return jsonify({"success": True, "enabled": enabled})
    
    return jsonify({"success": False, "error": "Device not found"}), 404


@motion_sensors_bp.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    """Delete a motion sensor device."""
    sensors = get_motion_sensors()
    
    # Find and remove the device
    original_length = len(sensors)
    sensors = [sensor for sensor in sensors if sensor['id'] != device_id]
    
    if len(sensors) == original_length:
        return jsonify({"success": False, "error": "Device not found"}), 404
    
    save_motion_sensors(sensors)
    
    # Notify UDP server about the deletion
    config_notifier.notify_change('motion_sensor_delete', {
        'device_id': device_id
    })
    
    return jsonify({"success": True, "message": "Device deleted successfully"})
