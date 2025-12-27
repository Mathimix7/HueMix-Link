"""Buttons routes blueprint."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager, config_notifier
from constants import FILE_BUTTONS

buttons_bp = Blueprint('buttons', __name__, url_prefix='/buttons')


def get_buttons():
    """Get all buttons from JSON file."""
    return data_manager.read_json(FILE_BUTTONS, default=[])


def save_buttons(buttons):
    """Save buttons to JSON file."""
    data_manager.write_json(FILE_BUTTONS, buttons)


def get_button_config(device_id):
    """Get button configuration for a specific device."""
    buttons = get_buttons()
    for button in buttons:
        if button['id'] == device_id:
            return button.get('config')
    return None


def save_button_config(device_id, config):
    """Save button configuration for a specific device."""
    buttons = get_buttons()
    for button in buttons:
        if button['id'] == device_id:
            button['config'] = config
            button['configured'] = True
            break
    save_buttons(buttons)


@buttons_bp.route('/')
def buttons_page():
    """Render the buttons configuration page."""
    return render_template('buttons.html')


@buttons_bp.route('/api/devices', methods=['GET'])
def get_devices_route():
    """Get all devices."""
    buttons = get_buttons()
    return jsonify({"success": True, "devices": buttons})


@buttons_bp.route('/api/devices/<device_id>/rename', methods=['POST'])
def rename_device(device_id):
    """Rename a device."""
    data = request.get_json()
    new_name = data.get('name')
    buttons = get_buttons()
    
    for button in buttons:
        if button['id'] == device_id:
            old_name = button['name']
            button['name'] = new_name
            save_buttons(buttons)
            
            # Notify UDP server about the update
            config_notifier.notify_change('button_rename', {
                'device_id': device_id,
                'old_name': old_name,
                'new_name': new_name,
                'device': button
            })
            
            return jsonify({"success": True, "device": button})
    
    return jsonify({"success": False, "error": "Device not found"}), 404


@buttons_bp.route('/api/configure', methods=['POST'])
def configure_button():
    """Configure a button with room and scenes."""
    data = request.get_json()
    device_id = data.get('device_id')
    
    config = {
        'device_id': device_id,
        'room_id': data.get('room_id'),
        'scenes': data.get('scenes', []),
    }
    
    # Save configuration to separate file
    save_button_config(device_id, config)
    
    # Notify UDP server about the configuration update
    config_notifier.notify_change('button_config', {
        'device_id': device_id,
        'config': config
    })
    
    return jsonify({"success": True, "config": config})


@buttons_bp.route('/api/<device_id>/config', methods=['GET'])
def get_button_config_route(device_id):
    """Get button configuration."""
    config = get_button_config(device_id)
    if config:
        return jsonify({"success": True, "config": config})
    return jsonify({"success": False, "error": "Configuration not found"}), 404


@buttons_bp.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    """Delete a device."""
    buttons = get_buttons()
    
    # Find and remove the device
    original_length = len(buttons)
    buttons = [button for button in buttons if button['id'] != device_id]
    
    if len(buttons) == original_length:
        return jsonify({"success": False, "error": "Device not found"}), 404
    
    save_buttons(buttons)
    
    # Notify UDP server about the deletion
    config_notifier.notify_change('button_delete', {
        'device_id': device_id
    })
    
    return jsonify({"success": True, "message": "Device deleted successfully"})
