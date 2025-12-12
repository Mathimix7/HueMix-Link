"""API routes blueprint for rooms and scenes."""
from flask import Blueprint, jsonify, request
from controllers.bridge_controller import BridgeController
from controllers.hue_controller import Hue
from services.hue_state_manager import hue_state_manager

api_bp = Blueprint('api', __name__, url_prefix='/api')

# Initialize bridge controller
bridge_controller = BridgeController()


def get_hue_controller():
    """Get Hue controller instance or None if not configured."""
    config = bridge_controller.load_config()
    if config and config.get('ip') and config.get('username'):
        try:
            return Hue(config['ip'], config['username'])
        except Exception:
            return None
    return None


@api_bp.route('/rooms', methods=['GET'])
def get_rooms():
    """Get all rooms from state manager."""
    config = bridge_controller.load_config()
    
    if not config or not config.get('ip'):
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True,
            "rooms": []
        }), 200
    
    try:
        # Get rooms from state manager
        all_rooms = hue_state_manager.get_all_rooms()
        
        # Format rooms for frontend
        rooms = []
        for room_id, room_state in all_rooms.items():
            light_count = len(room_state.get('lights', []))
            
            rooms.append({
                "id": room_id,
                "name": room_state.get('name', 'Unknown'),
                "light_count": light_count,
                "is_on": room_state.get('is_on', False)
            })
        
        # Sort by name
        rooms.sort(key=lambda x: x['name'].lower())
        
        return jsonify({
            "success": True,
            "rooms": rooms
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch rooms: {str(e)}",
            "rooms": []
        }), 200
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True,
            "rooms": []
        }), 200
    
    try:
        rooms_data = hue.get_rooms()
        grouped_lights = hue.get_grouped_lights()  # Fetch all grouped lights at once
        
        # Create a map of grouped light IDs to their on/off state
        grouped_light_map = {gl['id']: gl.get('on', {}).get('on', False) for gl in grouped_lights}
        
        # Format rooms for frontend with light count
        rooms = []
        for room in rooms_data:
            room_id = room["id"]
            light_count = sum(1 for child in room.get('children', []) if child.get('rtype') == 'device')
            
            # Get room on/off status from grouped lights
            is_on = False
            for service in room.get('services', []):
                if service.get('rtype') == 'grouped_light':
                    grouped_light_id = service.get('rid')
                    if grouped_light_id in grouped_light_map:
                        is_on = grouped_light_map[grouped_light_id]
                        break
            
            rooms.append({
                "id": room_id,
                "name": room["metadata"]["name"],
                "light_count": light_count,
                "is_on": is_on
            })
        
        return jsonify({"success": True, "rooms": rooms})
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch rooms: {str(e)}",
            "needs_config": True,
            "rooms": []
        }), 200


@api_bp.route('/rooms/<room_id>/scenes', methods=['GET'])
def get_room_scenes(room_id):
    """Get scenes for a specific room from Hue Bridge."""
    hue = get_hue_controller()
    
    if not hue:
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True,
            "scenes": []
        }), 200
    
    try:
        all_scenes = hue.get_scenes()
        
        # Filter scenes for this room
        room_scenes = [
            {
                "id": scene["id"],
                "name": scene["metadata"]["name"],
                "room_id": room_id
            }
            for scene in all_scenes
            if scene.get("group", {}).get("rid") == room_id
        ]
        
        return jsonify({"success": True, "scenes": room_scenes})
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch scenes: {str(e)}",
            "needs_config": True,
            "scenes": []
        }), 200


@api_bp.route('/scenes/all', methods=['GET'])
def get_all_scenes():
    """Get all scenes from state manager with room names."""
    config = bridge_controller.load_config()
    
    if not config or not config.get('ip'):
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True,
            "scenes": []
        }), 200
    
    try:
        # Get scenes and rooms from state manager
        all_scenes = hue_state_manager._scenes  # Access scenes dict directly
        all_rooms = hue_state_manager.get_all_rooms()
        
        # Create a map of room IDs to names
        room_map = {rid: room.get('name', 'Unknown') for rid, room in all_rooms.items()}
        
        # Format scenes with room names
        scenes = []
        for scene_id, scene_data in all_scenes.items():
            room_id = scene_data.get('room_id')
            scenes.append({
                "id": scene_id,
                "name": scene_data.get('name', f'Scene {scene_id[:8]}'),
                "room_id": room_id,
                "room_name": room_map.get(room_id, "Unknown Room")
            })
        
        # Sort by room name, then scene name
        scenes.sort(key=lambda x: (x['room_name'].lower(), x['name'].lower()))
        
        return jsonify({"success": True, "scenes": scenes})
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch scenes: {str(e)}",
            "needs_config": True,
            "scenes": []
        }), 200


@api_bp.route('/lights/all', methods=['GET'])
def get_all_lights():
    """Get all lights from state manager with room information."""
    config = bridge_controller.load_config()
    
    if not config or not config.get('ip'):
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True,
            "lights": []
        }), 200
    
    try:
        # Get lights and rooms from state manager
        all_lights = hue_state_manager.get_all_lights()
        all_rooms = hue_state_manager.get_all_rooms()
        
        # Create a map of room IDs to room names
        room_names = {rid: room.get('name', 'Unknown') for rid, room in all_rooms.items()}
        
        # Format lights for frontend
        lights = []
        for light_id, light_state in all_lights.items():
            room_id = light_state.get('room_id')
            room_name = room_names.get(room_id, 'Unassigned') if room_id else 'Unassigned'
            
            lights.append({
                "id": light_id,
                "name": light_state.get('name', f'Light {light_id[:8]}'),
                "on": light_state.get('on', False),
                "brightness": light_state.get('brightness'),
                "room_id": room_id,
                "room_name": room_name
            })
        
        # Sort by room name, then light name
        lights.sort(key=lambda x: (x['room_name'].lower(), x['name'].lower()))
        
        return jsonify({"success": True, "lights": lights})
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch lights: {str(e)}",
            "needs_config": True,
            "lights": []
        }), 200


@api_bp.route('/rooms/<room_id>', methods=['GET'])
def get_room_detail(room_id):
    """Get detailed information about a specific room."""
    hue = get_hue_controller()
    
    if not hue:
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True
        }), 200
    
    try:
        room = hue.get_room(room_id)
        all_lights = hue.get_lights()  # Fetch all lights at once
        all_devices = hue.get_devices()  # Fetch all devices at once
        
        # Create a map of light IDs to light data
        light_map = {light['id']: light for light in all_lights}
        
        # Create a map of device IDs to light IDs
        device_to_light = {}
        for device in all_devices:
            device_id = device['id']
            for service in device.get('services', []):
                if service.get('rtype') == 'light':
                    light_id = service.get('rid')
                    if light_id:
                        device_to_light[device_id] = light_id
        
        # Get lights in this room
        lights = []
        for child in room.get('children', []):
            if child.get('rtype') == 'device':
                device_id = child.get('rid')
                if device_id and device_id in device_to_light:
                    light_id = device_to_light[device_id]
                    if light_id in light_map:
                        light = light_map[light_id]
                        lights.append({
                            'id': light_id,
                            'name': light['metadata']['name'],
                            'on': light.get('on', {}).get('on', False),
                            'brightness': light.get('dimming', {}).get('brightness')
                        })
        
        # Get room on/off status
        is_on = False
        try:
            is_on = hue.is_room_on(room_id)
        except Exception:
            pass
        
        # Get scenes for this room
        all_scenes = hue.get_scenes()
        room_scenes = [
            {
                "id": scene["id"],
                "name": scene["metadata"]["name"]
            }
            for scene in all_scenes
            if scene.get("group", {}).get("rid") == room_id
        ]
        
        return jsonify({
            "success": True,
            "room": {
                "id": room_id,
                "name": room['metadata']['name'],
                "is_on": is_on,
                "lights": lights,
                "scenes": room_scenes
            }
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch room details: {str(e)}"
        }), 200


@api_bp.route('/rooms/<room_id>/toggle', methods=['POST'])
def toggle_room(room_id):
    """Toggle a room on or off."""
    hue = get_hue_controller()
    
    if not hue:
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured"
        }), 200
    
    try:
        # Get current room status
        is_on = hue.is_room_on(room_id)
        
        # Get the grouped_light service for the room
        room = hue.get_room(room_id)
        services = room.get('services', [])
        grouped_light_id = None
        
        for service in services:
            if service.get('rtype') == 'grouped_light':
                grouped_light_id = service.get('rid')
                break
        
        if not grouped_light_id:
            return jsonify({
                "success": False,
                "error": "Room has no grouped light service"
            }), 200
        
        # Toggle the room
        new_state = not is_on
        hue._put_resource('grouped_light', grouped_light_id, {"on": {"on": new_state}})
        
        
        return jsonify({
            "success": True,
            "is_on": new_state
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to toggle room: {str(e)}"
        }), 200


@api_bp.route('/lights/<light_id>/toggle', methods=['POST'])
def toggle_light(light_id):
    """Toggle a light on or off."""
    hue = get_hue_controller()
    
    if not hue:
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured"
        }), 200
    
    try:
        light = hue.get_light(light_id)
        is_on = light.get('on', {}).get('on', False)
        
        # Toggle the light
        new_state = not is_on
        hue.set_light(light_id, {"on": {"on": new_state}})
        
        
        return jsonify({
            "success": True,
            "is_on": new_state
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to toggle light: {str(e)}"
        }), 200


@api_bp.route('/scenes/<scene_id>/activate', methods=['POST'])
def activate_scene(scene_id):
    """Activate a scene."""
    hue = get_hue_controller()
    
    if not hue:
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured"
        }), 200
    
    try:
        # Get scene info to find the room
        scene = hue.get_scene(scene_id)
        group_info = scene.get('group', {})
        
        # Activate the scene by setting recall action
        hue._put_resource('scene', scene_id, {"recall": {"action": "active"}})
        
        
        return jsonify({
            "success": True,
            "message": "Scene activated"
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to activate scene: {str(e)}"
        }), 200


@api_bp.route('/overview/counts', methods=['GET'])
def get_overview_counts():
    """Get counts of rooms, lights, and scenes from state manager."""
    config = bridge_controller.load_config()
    
    if not config or not config.get('ip'):
        return jsonify({
            "success": False,
            "error": "Hue Bridge not configured",
            "needs_config": True
        }), 200
    
    try:
        summary = hue_state_manager.get_current_state_summary()
        
        return jsonify({
            "success": True,
            "counts": {
                "rooms": summary.get('total_rooms', 0),
                "lights": summary.get('total_lights', 0),
                "scenes": summary.get('total_scenes', 0)
            }
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to fetch counts: {str(e)}"
        }), 200


@api_bp.route('/status/bridge', methods=['GET'])
def get_bridge_status():
    """Get Hue Bridge connection status."""
    config = bridge_controller.load_config()
    
    if not config:
        return jsonify({
            "configured": False,
            "connected": False
        })
    
    # Test connection
    try:
        hue = Hue(config['ip'], config['username'])
        bridge_data = hue.get_bridge()[0]
        
        return jsonify({
            "configured": True,
            "connected": True,
            "ip": config['ip'],
            "name": bridge_data.get('metadata', {}).get('name', 'Hue Bridge'),
            "bridgeid": bridge_data.get('id', 'N/A')
        })
    except Exception:
        return jsonify({
            "configured": True,
            "connected": False,
            "ip": config['ip']
        })


@api_bp.route('/status/tcp', methods=['GET'])
def get_tcp_status():
    """Get TCP Server status."""
    from servers.tcp_server import tcp_server
    
    # Check if server is running by checking the _running flag
    running = tcp_server._running
    port = tcp_server.port
    
    return jsonify({
        "running": running,
        "port": port
    })


# ===== Pairing Mode Endpoints =====

@api_bp.route('/pairing/start', methods=['POST'])
def start_pairing():
    """Start device pairing mode.
    
    Request body:
        {
            "duration": 60,  # seconds, optional, default 60
            "types": ["button", "light"]  # optional, default all
        }
    """
    try:
        from network.pairing_manager import pairing_manager
        
        data = request.get_json() or {}
        duration = data.get('duration', 60)
        device_types = data.get('types')  # None = all types
        
        # Validate duration
        if not isinstance(duration, int) or duration < 10 or duration > 300:
            return jsonify({
                "success": False,
                "error": "Duration must be between 10 and 300 seconds"
            }), 400
        
        # Validate device types
        if device_types is not None:
            if not isinstance(device_types, list):
                return jsonify({
                    "success": False,
                    "error": "Types must be an array"
                }), 400
            
            valid_types = ['gateway', 'button', 'light']
            for t in device_types:
                if t not in valid_types:
                    return jsonify({
                        "success": False,
                        "error": f"Invalid device type: {t}. Must be one of {valid_types}"
                    }), 400
        
        # Start pairing
        pairing_manager.start_pairing(duration, device_types)
        
        return jsonify({
            "success": True,
            "duration": duration,
            "types": device_types if device_types else ['all']
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@api_bp.route('/pairing/stop', methods=['POST'])
def stop_pairing():
    """Stop device pairing mode."""
    try:
        from network.pairing_manager import pairing_manager
        
        pairing_manager.stop_pairing()
        
        return jsonify({
            "success": True
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@api_bp.route('/pairing/status', methods=['GET'])
def get_pairing_status():
    """Get current pairing mode status."""
    try:
        from network.pairing_manager import pairing_manager
        
        status = pairing_manager.get_status()
        
        return jsonify({
            "success": True,
            **status
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@api_bp.route('/pairing/devices', methods=['GET'])
def get_pairing_devices():
    """Get devices found during pairing session."""
    try:
        from network.pairing_manager import pairing_manager
        
        devices = pairing_manager.get_devices_found()
        
        # Add type names for display
        type_names = {
            1: 'Gateway',
            2: 'Button',
            3: 'Light'
        }
        
        for device in devices:
            device['type_name'] = type_names.get(device.get('type'), 'Unknown')
        
        return jsonify({
            "success": True,
            "devices": devices
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "devices": []
        }), 500
