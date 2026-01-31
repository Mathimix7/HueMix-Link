"""
OTA (Over-The-Air) firmware update API endpoints.

Provides REST API for checking firmware updates, uploading binaries,
initiating updates, and monitoring progress.
"""
from flask import Blueprint, request, jsonify, render_template, send_file
import os
import logging
import requests
import re
import json
from werkzeug.utils import secure_filename
from services.ota_manager import ota_manager
from network.device_manager import device_manager
from constants import DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, GITHUB_OWNER, GITHUB_REPO
from network.network_server import network_server
from services.config_manager import config_manager
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

ota_bp = Blueprint('ota', __name__)

# Firmware storage directory
FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'firmware')
os.makedirs(FIRMWARE_DIR, exist_ok=True)

# Local firmware metadata file
LOCAL_FIRMWARE_META = os.path.join(FIRMWARE_DIR, 'local_firmwares.json')

# GitHub repository for firmware updates
token = os.getenv("GITHUB_TOKEN", "").strip()
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {token}",
    "X-GitHub-Api-Version": "2022-11-28"
}
HEADERS_ASSET = {
    "Accept": "application/octet-stream",
    "Authorization": f"Bearer {token}",
    "X-GitHub-Api-Version": "2022-11-28"
}

# Device type to firmware name mapping
DEVICE_TYPE_NAMES = {
    DEV_GATEWAY: 'gateway',
    DEV_BUTTON: 'button',
    DEV_LIGHT: 'lightstrip',
    DEV_REMOTE: 'remote'
}

# Extended mapping for gateway subtypes (net vs radio) and button/remote/lightstrip platforms (ESP32 vs ESP8266)
# Lightstrip model IDs:
#   Model 1: ESP32, RGB, GRB, WS2812B
#   Model 2: ESP32, RGBW, GRB, SK6812
#   Model 3: ESP8266, RGB, GRB, WS2812B
#   Model 4: ESP8266, RGBW, GRB, SK6812
EXTENDED_DEVICE_TYPES = {
    'gateway_net': (DEV_GATEWAY, 'gateway_net'),
    'gateway_radio': (DEV_GATEWAY, 'gateway_radio'),
    'button_esp32': (DEV_BUTTON, 'button_esp32'),
    'button_esp8266': (DEV_BUTTON, 'button_esp8266'),
    'remote_esp32': (DEV_REMOTE, 'remote_esp32'),
    # 'remote_esp8266': (DEV_REMOTE, 'remote_esp8266'),
    'lightstrip_model1': (DEV_LIGHT, 'lightstrip_model1'),  # ESP32, RGB, WS2812B
    'lightstrip_model2': (DEV_LIGHT, 'lightstrip_model2'),  # ESP32, RGBW, SK6812
    'lightstrip_model3': (DEV_LIGHT, 'lightstrip_model3'),  # ESP8266, RGB, WS2812B
    'lightstrip_model4': (DEV_LIGHT, 'lightstrip_model4'),  # ESP8266, RGBW, SK6812
}


def get_firmware_filename(device_type: int, version: str) -> str:
    """Generate firmware filename for device type and version.
    
    Args:
        device_type: Device type constant
        version: Version string (e.g., "3.7.4")
        
    Returns:
        Firmware filename
    """
    type_name = DEVICE_TYPE_NAMES.get(device_type, 'unknown')
    return f"huemixlink-{type_name}-v{version}.bin"


def parse_version(version_str: str) -> tuple:
    """Parse version string to tuple.
    
    Args:
        version_str: Version string like "3.7.4"
        
    Returns:
        Tuple of (major, minor, patch)
    """
    try:
        parts = version_str.split('.')
        return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except Exception:
        return (0, 0, 0)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings.
    
    Args:
        v1: First version string
        v2: Second version string
        
    Returns:
        -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
    """
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    
    if t1 < t2:
        return -1
    elif t1 > t2:
        return 1
    else:
        return 0


def load_local_firmwares():
    """Load locally uploaded firmware metadata.
    
    Returns:
        Dict of firmware metadata by device type
    """
    try:
        if os.path.exists(LOCAL_FIRMWARE_META):
            with open(LOCAL_FIRMWARE_META, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load local firmware metadata: {e}")
    return {}


def save_local_firmwares(firmwares):
    """Save locally uploaded firmware metadata.
    
    Args:
        firmwares: Dict of firmware metadata by device type
    """
    try:
        with open(LOCAL_FIRMWARE_META, 'w') as f:
            json.dump(firmwares, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save local firmware metadata: {e}")


@ota_bp.route('/ota')
def ota_page():
    """Render OTA management page."""
    dev_mode = config_manager.get_dev_mode()
    return render_template('ota.html', dev_mode=dev_mode)


@ota_bp.route('/api/ota/check', methods=['GET'])
def check_updates():
    """Check for available firmware updates from GitHub and local uploads.
    
    Returns:
        JSON with available firmware versions per device type
    """
    try:
        available_firmwares = {}
        release_info = None
        
        # First, try to fetch GitHub releases
        try:
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
            response = requests.get(url, timeout=10, headers=HEADERS)
            
            if response.status_code == 200:
                release_data = response.json()
                tag_name = release_data.get('tag_name', 'v0.0.0')
                
                release_info = {
                    'tag': tag_name,
                    'version': "github",
                    'name': release_data.get('name', ''),
                    'published_at': release_data.get('published_at', ''),
                    'html_url': release_data.get('html_url', '')
                }
                
                # Extract firmware assets
                assets = release_data.get('assets', [])
                
                for asset in assets:
                    name = asset.get('name', '')
                    download_url = asset.get('url', '')
                    
                    # Match firmware files
                    match = re.match(r'huemixlink-(esp32|esp8266)-(net|radio|button|lightstrip|remote)(?:-([0-9]+))?-v([\d\.]+)\.bin', name)
                    if match:
                        platform, fw_type, model, version = match.groups()
                        if fw_type == "lightstrip":
                            firmware_type = f"{fw_type}_model{model}"
                        elif fw_type in ["radio", "net"]:
                            firmware_type = f"gateway_{fw_type}"
                        else:
                            firmware_type = f"{fw_type}_{platform}"
                        
                        # Use device_type_name as key (gateway, lightstrip, button, remote)
                        available_firmwares[firmware_type] = {
                            'version': version,
                            'firmware_type': firmware_type,
                            'download_url': download_url,
                            'filename': name,
                            'source': 'github'
                        }
        except Exception as e:
            logger.warning(f"Failed to fetch GitHub releases: {e}")
        
        # Load and merge locally uploaded firmwares
        local_firmwares = load_local_firmwares()
        for firmware_type_key, firmware_info in local_firmwares.items():
            # Use local if no GitHub version or if local is newer
            if firmware_type_key not in available_firmwares:
                available_firmwares[firmware_type_key] = firmware_info
            else:
                # Compare versions and use newer one
                github_ver = available_firmwares[firmware_type_key]['version']
                local_ver = firmware_info['version']
                if compare_versions(local_ver, github_ver) > 0:
                    available_firmwares[firmware_type_key] = firmware_info
        
        if not release_info:
            release_info = {
                'tag': 'local',
                'version': 'local',
                'name': 'Local Uploads',
                'published_at': '',
                'html_url': ''
            }
        
        return jsonify({
            'success': True,
            'release': release_info,
            'firmwares': available_firmwares
        })
        
    except Exception as e:
        logger.error(f"Error checking updates: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@ota_bp.route('/api/ota/upload', methods=['POST'])
def upload_firmware():
    """Upload firmware binary file.
    
    Request:
        multipart/form-data with 'file', 'device_type', and 'version'
        device_type can be: gateway_net, gateway_radio, or numeric codes (1-4)
        version: semantic version string (e.g., "3.7.4")
        
    Returns:
        JSON with upload status
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        device_type_str = request.form.get('device_type', '')
        version_str = request.form.get('version', '')
        
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        if not device_type_str:
            return jsonify({'success': False, 'error': 'Device type required'}), 400
        
        if not version_str:
            return jsonify({'success': False, 'error': 'Version required'}), 400
        
        # Validate version format
        if not re.match(r'^\d+\.\d+\.\d+$', version_str):
            return jsonify({'success': False, 'error': 'Version must be in format X.Y.Z (e.g., 3.7.4)'}), 400
        
        # Map device_type string to code and firmware type
        if device_type_str in EXTENDED_DEVICE_TYPES:
            device_type_code, firmware_type = EXTENDED_DEVICE_TYPES[device_type_str]
        else:
            return jsonify({'success': False, 'error': f'Invalid device type: {device_type_str}'}), 400
        
        # Secure filename
        filename = secure_filename(file.filename)
        
        # Validate .bin extension
        if not filename.endswith('.bin'):
            return jsonify({'success': False, 'error': 'Only .bin files allowed'}), 400
        
        # Save file with firmware_type prefix to prevent overwriting
        base_name = os.path.splitext(filename)[0]
        filename = f"{firmware_type}_{base_name}.bin"
        
        filepath = os.path.join(FIRMWARE_DIR, filename)
        file.save(filepath)
        
        logger.info(f"Uploaded {firmware_type} firmware: {filename} ({os.path.getsize(filepath)} bytes)")
        
        # Store metadata for this upload
        local_firmwares = load_local_firmwares()
        local_firmwares[firmware_type] = {
            'version': version_str,
            'filename': filename,
            'filepath': filepath,
            'source': 'local',
            'firmware_type': firmware_type
        }
        save_local_firmwares(local_firmwares)
        
        return jsonify({
            'success': True,
            'filename': filename,
            'filepath': filepath,
            'size': os.path.getsize(filepath),
            'device_type_code': device_type_code,
            'firmware_type': firmware_type,
            'version': version_str
        })
        
    except Exception as e:
        logger.error(f"Failed to upload firmware: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/download', methods=['POST'])
def download_firmware():
    """Download firmware from GitHub for specific device type.
    
    Args:
        device_type: Device type constant
        
    Request JSON:
        {
            "download_url": "https://...",
            "filename": "firmware.bin"
        }
        
    Returns:
        JSON with download status
    """
    try:
        data = request.get_json()
        download_url = data.get('download_url')
        filename = data.get('filename')
        
        if not download_url or not filename:
            return jsonify({'success': False, 'error': 'Missing download_url or filename'}), 400
        
        # Download firmware
        logger.info(f"Downloading firmware from {download_url}...")
        response = requests.get(download_url, timeout=60, stream=True, headers=HEADERS_ASSET)
        
        if response.status_code != 200:
            return jsonify({
                'success': False,
                'error': f"Download failed: {response.status_code}"
            }), 500
        
        # Save to firmware directory
        filepath = os.path.join(FIRMWARE_DIR, secure_filename(filename))
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        logger.info(f"Downloaded firmware: {filename} ({os.path.getsize(filepath)} bytes)")
        
        return jsonify({
            'success': True,
            'filename': filename,
            'filepath': filepath,
            'size': os.path.getsize(filepath)
        })
        
    except Exception as e:
        logger.error(f"Failed to download firmware: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/binary', methods=['POST'])
def get_firmware_binary():
    """Get firmware binary for esptool-js serial flashing.
    
    Handles both local and GitHub firmware sources, downloading if necessary.
    
    Args:
        firmware_key: Firmware identifier (e.g., 'gateway_net', 'lightstrip_model1')
        
    Request JSON:
        {
            "source": "local" | "github",
            "download_url": "https://..." (required for github source),
            "filename": "firmware.bin" (required for github source),
            "filepath": "/path/to/file.bin" (required for local source)
        }
        
    Returns:
        Binary file data or JSON error
    """
    try:
        data = request.get_json()
        source = data.get('source')
        
        if source == 'local':
            filepath = data.get('filepath')
            if not filepath:
                return jsonify({'success': False, 'error': 'Missing filepath for local firmware'}), 400
            
            if not os.path.exists(filepath):
                return jsonify({'success': False, 'error': f'Firmware file not found: {filepath}'}), 404
            
            logger.info(f"Serving local firmware: {filepath}")
            return send_file(filepath, mimetype='application/octet-stream', as_attachment=False)
        
        elif source == 'github':
            download_url = data.get('download_url')
            filename = data.get('filename')
            
            if not download_url or not filename:
                return jsonify({'success': False, 'error': 'Missing download_url or filename for github source'}), 400
            
            # Check if already downloaded
            filepath = os.path.join(FIRMWARE_DIR, secure_filename(filename))
            
            if not os.path.exists(filepath):
                logger.info(f"Downloading firmware from GitHub: {download_url}...")
                response = requests.get(download_url, timeout=60, stream=True, headers=HEADERS_ASSET)
                
                if response.status_code != 200:
                    return jsonify({
                        'success': False,
                        'error': f"GitHub download failed: {response.status_code}"
                    }), 500
                
                # Save to firmware directory
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                logger.info(f"Downloaded firmware: {filename} ({os.path.getsize(filepath)} bytes)")
            else:
                logger.info(f"Using cached firmware: {filepath}")
            
            return send_file(filepath, mimetype='application/octet-stream', as_attachment=False)
        
        else:
            return jsonify({'success': False, 'error': 'Invalid source. Must be "local" or "github"'}), 400
        
    except Exception as e:
        logger.error(f"Failed to get firmware binary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/start/<device_mac>', methods=['POST'])
def start_ota(device_mac):
    """Start OTA update for a device.
    
    Args:
        device_mac: Device MAC address
        
    Request JSON:
        {
            "firmware_path": "/path/to/firmware.bin",
            "firmware_type": "gateway_net" | "gateway_radio" | "lightstrip" | "button" | "remote" (optional)
        }
        
    Returns:
        JSON with start status
    """
    try:        
        data = request.get_json()
        firmware_path = data.get('firmware_path')
        
        
        if not firmware_path:
            return jsonify({'success': False, 'error': 'firmware_path required'}), 400
        
        # Validate file exists
        if not os.path.exists(firmware_path):
            return jsonify({'success': False, 'error': 'Firmware file not found'}), 404
        
        # Determine device type
        device = None
        device_type = None
        
        # Check gateways
        gateways = device_manager.get_all_gateways()
        for gw in gateways:
            if gw.get('mac_address') == device_mac or gw.get('radio_mac') == device_mac:
                device = gw
                device_type = DEV_GATEWAY
                break
        
        # Check lightstrips
        if not device:
            light = device_manager.get_light_by_mac(device_mac)
            if light:
                device = light
                device_type = DEV_LIGHT
        
        # Check buttons/remotes
        if not device:
            button = device_manager.get_button_by_mac(device_mac)
            if button:
                device = button
                device_type = button.get('device_type', DEV_BUTTON)
        
        if not device:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        
        # Validate model_id for lightstrips
        if device_type == DEV_LIGHT:
            device_model_id = device.get('model_id')
            # Extract firmware type from filename (format: lightstrip_modelX_*.bin)
            firmware_name = os.path.basename(firmware_path)
            
            # Check if firmware is model-specific
            if 'lightstrip_model' in firmware_name:
                # Extract model number from firmware filename
                match = re.search(r'lightstrip_model(\d+)', firmware_name)
                if match:
                    firmware_model_id = int(match.group(1))
                    
                    # Validate model_id matches
                    if device_model_id and device_model_id != firmware_model_id:
                        model_names = {
                            1: 'ESP32, RGB, WS2812B',
                            2: 'ESP32, RGBW, SK6812',
                            3: 'ESP8266, RGB, WS2812B',
                            4: 'ESP8266, RGBW, SK6812'
                        }
                        return jsonify({
                            'success': False,
                            'error': f'Firmware mismatch: Device is Model {device_model_id} ({model_names.get(device_model_id, "Unknown")}), firmware is for Model {firmware_model_id} ({model_names.get(firmware_model_id, "Unknown")})'
                        }), 400
                    elif not device_model_id:
                        logger.warning(f"Device {device_mac} has no model_id, allowing firmware upload")
        
        # Check if update already in progress
        if ota_manager.is_update_in_progress():
            return jsonify({
                'success': False,
                'error': 'Another update is already in progress'
            }), 409
        
        # Start OTA update
        success = network_server.start_ota_update(device_mac, device_type, firmware_path)
        
        if success:
            return jsonify({
                'success': True,
                'message': f'OTA update started for {device_mac}'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to start OTA update'
            }), 500
        
    except Exception as e:
        logger.error(f"Failed to start OTA: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/status/<device_mac>', methods=['GET'])
def get_ota_status(device_mac):
    """Get OTA update status for a device.
    
    Args:
        device_mac: Device MAC address
        
    Returns:
        JSON with OTA session status
    """
    try:
        session = ota_manager.get_session(device_mac)
        
        if not session:
            return jsonify({
                'success': True,
                'has_session': False
            })
        
        return jsonify({
            'success': True,
            'has_session': True,
            'session': session.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Failed to get OTA status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/abort/<device_mac>', methods=['POST'])
def abort_ota(device_mac):
    """Abort OTA update for a device.
    
    Args:
        device_mac: Device MAC address
        
    Returns:
        JSON with abort status
    """
    try:        
        network_server.abort_ota_update(device_mac, "User cancelled")
        
        return jsonify({
            'success': True,
            'message': f'OTA update aborted for {device_mac}'
        })
        
    except Exception as e:
        logger.error(f"Failed to abort OTA: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/sessions', methods=['GET'])
def get_all_sessions():
    """Get all OTA sessions.
    
    Returns:
        JSON with all OTA sessions
    """
    try:
        sessions = ota_manager.get_all_sessions()
        
        return jsonify({
            'success': True,
            'sessions': sessions
        })
        
    except Exception as e:
        logger.error(f"Failed to get OTA sessions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ota_bp.route('/api/ota/devices', methods=['GET'])
def get_devices_with_versions():
    """Get all devices with their current firmware versions.
    
    Returns:
        JSON with device list and versions
    """
    try:
        devices = []
        
        # Get all gateways
        gateways = device_manager.get_all_gateways()
        for gw in gateways:
            # Add Net Node entry (WiFi MAC)
            devices.append({
                'mac_address': gw.get('mac_address'),
                'radio_mac': gw.get('radio_mac'),
                'name': gw.get('name', f"Gateway {gw.get('mac_address', '')[-8:]}"),
                'type': 'gateway_net',
                'device_type': DEV_GATEWAY,
                'version': gw.get('version_net', '0.0.0'),
                'online': True
            })
            
            # Add Radio Node entry (Radio MAC) - only if radio_mac exists
            if gw.get('radio_mac'):
                devices.append({
                    'mac_address': gw.get('radio_mac'),  # Use radio MAC as identifier
                    'wifi_mac': gw.get('mac_address'),   # Keep reference to WiFi MAC
                    'name': gw.get('name', f"Gateway {gw.get('mac_address', '')[-8:]}"),
                    'type': 'gateway_radio',
                    'device_type': DEV_GATEWAY,
                    'version': gw.get('version_radio', '0.0.0'),
                    'online': True
                })
        
        # Get all lightstrips
        lights = device_manager.get_all_lights()
        for light in lights:
            devices.append({
                'mac_address': light.get('mac_address'),
                'name': light.get('name', f"Light {light.get('mac_address', '')[-8:]}"),
                'type': 'lightstrip',
                'device_type': DEV_LIGHT,
                'version': light.get('version', '0.0.0'),
                'platform': light.get('platform', 'unknown'),
                'model_id': light.get('model_id'),
                'online': True
            })
        
        # Get all buttons/remotes
        buttons = device_manager.get_all_buttons()
        
        # Create set of gateway WiFi MACs to filter out gateway buttons
        gateway_wifi_macs = {gw.get('mac_address', '').upper() for gw in gateways}
        
        for btn in buttons:
            btn_mac = btn.get('mac_address', '').upper()
            
            # Skip buttons that are part of gateways (net node button)
            if btn_mac in gateway_wifi_macs:
                continue
                
            btn_type = btn.get('device_type', DEV_BUTTON)
            type_name = 'remote' if btn_type == DEV_REMOTE else 'button'
            platform = btn.get('platform', 'unknown')
            
            devices.append({
                'mac_address': btn.get('mac_address'),
                'name': btn.get('name', f"{type_name.capitalize()} {btn.get('mac_address', '')[-8:]}"),
                'type': type_name,
                'device_type': btn_type,
                'version': btn.get('version', '0.0.0'),
                'platform': platform,
                'online': True
            })
        
        return jsonify({
            'success': True,
            'devices': devices
        })
        
    except Exception as e:
        logger.error(f"Failed to get devices: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
