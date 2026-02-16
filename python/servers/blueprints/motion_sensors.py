"""Motion Sensors routes blueprint."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager, config_notifier
from constants import FILE_MOTION_SENSORS
import logging

logger = logging.getLogger(__name__)

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
        'cooldown_seconds': data.get('cooldown_seconds', 60),
        'light_sensitivity': data.get('light_sensitivity', 5),
        'time_slots': data.get('time_slots', []),
        'enabled': data.get('enabled', True)
    }
    
    # Save configuration
    save_motion_sensor_config(device_id, config)
    
    # Notify UDP server about the configuration update
    config_notifier.notify_change('motion_sensor_config', {
        'device_id': device_id,
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
    cooldown_seconds = data.get('cooldown_seconds', 60)
    
    sensors = get_motion_sensors()
    for sensor in sensors:
        if sensor['id'] == device_id:
            if not sensor.get('config'):
                sensor['config'] = {}
            sensor['config']['cooldown_seconds'] = cooldown_seconds
            save_motion_sensors(sensors)
            
            # Notify UDP server
            config_notifier.notify_change('motion_sensor_cooldown', {
                'device_id': device_id,
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
