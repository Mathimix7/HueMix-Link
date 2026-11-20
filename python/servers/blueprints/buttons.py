"""Buttons routes blueprint."""
from flask import Blueprint, render_template, request, jsonify
from services import data_manager, config_notifier
from constants import FILE_BUTTONS
from services.hue_service import hue_service
import logging

logger = logging.getLogger(__name__)

buttons_bp = Blueprint('buttons', __name__, url_prefix='/buttons')


def get_buttons():
    """Get all buttons from JSON file."""
    return data_manager.read_json(FILE_BUTTONS, default=[])


def get_button_press_states():
    """Return a dict of button_id to last_pressed timestamp (ISO string or None)."""
    buttons = get_buttons()
    result = {}
    for button in buttons:
        result[button['id']] = button.get('last_seen')
    return result

@buttons_bp.route('/api/press_states', methods=['GET'])
def get_press_states():
    """API endpoint to get last press state for all buttons."""
    press_states = get_button_press_states()
    return jsonify({"success": True, "press_states": press_states})


def save_buttons(buttons):
    """Save buttons to JSON file."""
    data_manager.write_json(FILE_BUTTONS, buttons)


def cleanup_deleted_scenes(valid_scene_ids):
    """
    Remove deleted scenes from all button configurations.
    
    Args:
        valid_scene_ids: Set of scene IDs that currently exist in Hue
    
    Returns:
        Number of buttons that were updated
    """
    buttons = get_buttons()
    updated_count = 0
    
    for button in buttons:
        if button.get('configured') and button.get('config'):
            config = button['config']
            original_scenes = config.get('scenes', [])
            
            if original_scenes:
                # Filter out scenes that no longer exist
                valid_scenes = [scene_id for scene_id in original_scenes if scene_id in valid_scene_ids]
                
                # Check if any scenes were removed
                if len(valid_scenes) != len(original_scenes):
                    removed = set(original_scenes) - set(valid_scenes)
                    logger.info(f"Button {button['name']} ({button['id']}): Removed {len(removed)} deleted scene(s): {removed}")
                    
                    config['scenes'] = valid_scenes
                    
                    # If no scenes left, mark as not configured
                    if not valid_scenes:
                        button['configured'] = False
                        button['config'] = None
                        logger.info(f"Button {button['name']} ({button['id']}): No valid scenes remaining, marking as unconfigured")
                    
                    updated_count += 1
    
    if updated_count > 0:
        save_buttons(buttons)
        logger.info(f"Cleaned up {updated_count} button configuration(s)")
    
    return updated_count


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


@buttons_bp.route('/api/devices/<device_id>', methods=['GET'])
def get_device_route(device_id):
    """Get a single device by id."""
    buttons = get_buttons()
    for button in buttons:
        if button['id'] == device_id:
            return jsonify({"success": True, "device": button})
    return jsonify({"success": False, "error": "Device not found"}), 404


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
        'target_id': data.get('target_id') or data.get('room_id'),
        'target_type': data.get('target_type', 'room'),
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


@buttons_bp.route('/api/remote/<device_id>/configure', methods=['POST'])
def configure_remote(device_id):
    """Configure a remote with per-button actions and room mappings."""
    data = request.get_json()
    
    buttons_config = data.get('buttons', [])
    
    config = {
        'device_id': device_id,
        'device_type': 'remote',
        'buttons': buttons_config
    }
    
    # Save configuration
    save_button_config(device_id, config)
    
    # Notify UDP server about the configuration update
    config_notifier.notify_change('remote_config', {
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


def validate_scenes_at_startup():
    """Validate button scene configurations at startup against current Hue scenes."""
    try:        
        hue = hue_service.get_controller()
        if not hue:
            logger.info("Hue Bridge not configured, skipping startup scene validation")
            return
        
        # Get all current scenes from Hue
        all_scenes = hue.get_scenes()
        valid_scene_ids = {scene["id"] for scene in all_scenes}
        
        # Clean up button configurations
        updated_count = cleanup_deleted_scenes(valid_scene_ids)
        
        if updated_count > 0:
            logger.info(f"Startup validation: Cleaned up {updated_count} button configuration(s) with deleted scenes")
        else:
            logger.info("Startup validation: All button configurations are valid")
    
    except Exception as e:
        logger.error(f"Error during startup scene validation: {e}", exc_info=True)

