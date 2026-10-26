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
from constants import DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, DEV_MOTION, DEV_DOOR, GITHUB_OWNER, GITHUB_REPO
from network.network_server import network_server
from services.config_manager import config_manager
from services.plugin_manager import plugin_manager

logger = logging.getLogger(__name__)

ota_bp = Blueprint('ota', __name__)

# Firmware storage directory
FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'firmware')
os.makedirs(FIRMWARE_DIR, exist_ok=True)

# Local firmware metadata file
LOCAL_FIRMWARE_META = os.path.join(FIRMWARE_DIR, 'local_firmwares.json')

# GitHub repository for firmware updates
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}
HEADERS_ASSET = {
    "Accept": "application/octet-stream",
    "X-GitHub-Api-Version": "2022-11-28"
}

# Device type to firmware name mapping
DEVICE_TYPE_NAMES = {
    DEV_GATEWAY: 'gateway',
    DEV_BUTTON: 'button',
    DEV_LIGHT: 'lightstrip',
    DEV_REMOTE: 'remote',
    DEV_MOTION: 'motion_sensor',
    DEV_DOOR: 'door_sensor'
}

# Base device type configurations (single source of truth)
BASE_DEVICE_CONFIGS = {
    'gateway_net': {
        'device_code': DEV_GATEWAY,
        'label': 'Gateway Net Node (WiFi)',
        'group': 'Gateways'
    },
    'gateway_radio': {
        'device_code': DEV_GATEWAY,
        'label': 'Gateway Radio Node (ESP-NOW)',
        'group': 'Gateways'
    },
    'gateway_radio_python': {
        'device_code': DEV_GATEWAY,
        'label': 'Gateway Radio Node (Serial Python)',
        'group': 'Gateways'
    },
    'button_esp32': {
        'device_code': DEV_BUTTON,
        'label': 'Button ESP32',
        'group': 'Buttons'
    },
    'button_esp8266': {
        'device_code': DEV_BUTTON,
        'label': 'Button ESP8266',
        'group': 'Buttons'
    },
    'remote_esp32': {
        'device_code': DEV_REMOTE,
        'label': 'Remote ESP32',
        'group': 'Remotes'
    },
    'motion_sensor_esp32': {
        'device_code': DEV_MOTION,
        'label': 'Motion Sensor ESP32',
        'group': 'Motion Sensors'
    },
    'door_sensor_esp32': {
        'device_code': DEV_DOOR,
        'label': 'Door Sensor ESP32',
        'group': 'Door Sensors'
    }
}

# Lightstrip model configurations - odd=ESP32, even=ESP8266
LIGHTSTRIP_MODELS = {
    1: {'platform': 'ESP32', 'type': 'RGB', 'chip': 'WS2812B'},
    2: {'platform': 'ESP8266', 'type': 'RGB', 'chip': 'WS2812B'},
    3: {'platform': 'ESP32', 'type': 'RGBW', 'chip': 'SK6812'},
    4: {'platform': 'ESP8266', 'type': 'RGBW', 'chip': 'SK6812'},
    5: {'platform': 'ESP32', 'type': 'RGB+PWMW', 'chip': 'WS2812B'},
    6: {'platform': 'ESP8266', 'type': 'RGB+PWM', 'chip': 'WS2812B'}
}

# Auto-generate lightstrip device configs from models
for model_id, model_info in LIGHTSTRIP_MODELS.items():
    key = f'lightstrip_model{model_id}'
    label = f"Lightstrip Model {model_id} ({model_info['platform']}, {model_info['type']}, {model_info['chip']})"
    BASE_DEVICE_CONFIGS[key] = {
        'device_code': DEV_LIGHT,
        'label': label,
        'group': 'Lightstrips'
    }

# Auto-generate derived dictionaries from base configs
EXTENDED_DEVICE_TYPES = {
    key: (config['device_code'], key)
    for key, config in BASE_DEVICE_CONFIGS.items()
}

DEVICE_TYPE_LABELS = {
    key: config['label']
    for key, config in BASE_DEVICE_CONFIGS.items()
}

DEVICE_TYPE_GROUPS = {}
for key, config in BASE_DEVICE_CONFIGS.items():
    group = config['group']
    if group not in DEVICE_TYPE_GROUPS:
        DEVICE_TYPE_GROUPS[group] = []
    DEVICE_TYPE_GROUPS[group].append(key)

# For backwards compatibility and convenience in templates
LIGHTSTRIP_MODEL_NAMES = {
    model_id: f"{info['platform']}, {info['type']}, {info['chip']}"
    for model_id, info in LIGHTSTRIP_MODELS.items()
}


def _is_serial_gateway(gateway: dict) -> bool:
    """Return True when gateway is connected through USB serial transport."""
    endpoint = gateway.get('transport_endpoint') or gateway.get('ip_address')
    return (gateway.get('transport') == 'usb_serial') or (
        isinstance(endpoint, str) and endpoint.startswith('serial://')
    )


def infer_platform(key: str, filename: str = '', ota_platform: str | None = None) -> str | None:
    """Best-effort inference of the target platform for a firmware entry.

    Args:
        key: Firmware type key (e.g. 'button_esp32', 'lightstrip_model2')
        filename: Firmware filename, checked for embedded platform hints
        ota_platform: Optional explicit platform declared by a plugin manifest ota entry

    Returns:
        'esp32', 'esp8266', or None when the platform cannot be determined
    """
    if ota_platform:
        platform = str(ota_platform).strip().lower()
        if platform in ('esp32', 'esp8266'):
            return platform

    name = (filename or '').lower()
    if 'esp8266' in name:
        return 'esp8266'
    if 'esp32' in name:
        return 'esp32'

    if key.endswith('_esp8266'):
        return 'esp8266'
    if key.endswith('_esp32'):
        return 'esp32'

    if key.startswith('lightstrip_model'):
        try:
            model_id = int(key[len('lightstrip_model'):])
            return str(LIGHTSTRIP_MODELS[model_id]['platform']).lower()
        except (ValueError, KeyError):
            return None

    # Core gateway firmwares are ESP32-based (radio_python assets are esp32-only)
    if key.startswith('gateway_'):
        return 'esp32'

    return None


def resolve_plugin_display(plugin_id: str, device_type: int | None = None) -> dict:
    """Resolve display metadata for a plugin (and optionally one of its device types).

    Args:
        plugin_id: Plugin UUID
        device_type: Optional device type to resolve a device-specific name for

    Returns:
        Dict with keys: name (device name falls back to plugin name), plugin_name,
        device_name, color and icon
    """
    result = {
        'name': plugin_id,
        'plugin_name': plugin_id,
        'device_name': None,
        'color': '#6366f1',
        'icon': 'fa-puzzle-piece',
    }

    for entry in plugin_manager.list_registered_plugins():
        if str(entry.get('plugin_id', '')).strip() != plugin_id:
            continue

        home_box = entry.get('home_box')
        if isinstance(home_box, dict):
            result['color'] = home_box.get('color', result['color'])
            result['icon'] = home_box.get('icon', result['icon'])
            result['plugin_name'] = home_box.get('name', result['plugin_name'])

        if device_type is not None:
            for device in entry.get('devices', []):
                if device.get('device_type') == device_type:
                    result['device_name'] = device.get('name')
                    break
        break

    result['name'] = result['device_name'] or result['plugin_name']
    return result


def parse_plugin_firmware_key(key: str) -> tuple[str, int] | None:
    """Parse a plugin firmware key into (plugin_id, device_type).

    Args:
        key: Firmware key of the form plugin_{plugin_id}_devtype_{device_type}

    Returns:
        Tuple of (plugin_id, device_type) or None if the key is not a plugin key
    """
    match = re.match(r'^plugin_(.+)_devtype_(\d+)$', str(key))
    if not match:
        return None
    return match.group(1), int(match.group(2))


def find_plugin_ota_entry(plugin_id: str, device_type: int) -> dict | None:
    """Find the manifest ota declaration for a plugin device type.

    Args:
        plugin_id: Plugin UUID
        device_type: Device type code declared by the plugin

    Returns:
        The raw ota entry dict if declared, None otherwise
    """
    for entry in plugin_manager.list_registered_plugins():
        if str(entry.get('plugin_id', '')).strip() != plugin_id:
            continue
        for ota_entry in entry.get('ota', []) or []:
            if ota_entry.get('device_type') == device_type:
                return ota_entry
        return None
    return None


def parse_github_url(url: str) -> tuple[str | None, str | None]:
    """Parse GitHub URL to extract owner and repo names.
    
    Args:
        url: GitHub URL (e.g., https://github.com/owner/repo)
        
    Returns:
        Tuple of (owner, repo) or (None, None) if not parseable
    """
    match = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:\/|$)', url)
    if match:
        return match.group(1), match.group(2).rstrip('/')
    return None, None


def extract_version_from_filename(filename: str) -> str | None:
    """Extract version string from firmware filename.
    
    Looks for patterns like -v1.2.3.bin or -v1.2.3.4.bin at end of filename.
    
    Args:
        filename: Firmware filename
        
    Returns:
        Version string or None
    """
    match = re.search(r'-v(\d+\.\d+\.\d+(?:\.\d+)?)\.bin$', filename)
    if match:
        return match.group(1)
    return None


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


def _plugin_firmware_options() -> list[dict]:
    """Build upload-modal options for plugin-declared firmware types.

    Returns:
        List of dicts with key, label and group for each plugin ota declaration
    """
    options = []
    for entry in plugin_manager.list_registered_plugins():
        plugin_id = str(entry.get('plugin_id', '')).strip()
        if not plugin_id:
            continue
        for ota_entry in entry.get('ota', []) or []:
            device_type = ota_entry.get('device_type')
            if device_type is None:
                continue
            display = resolve_plugin_display(plugin_id, device_type)
            options.append({
                'key': f'plugin_{plugin_id}_devtype_{device_type}',
                'label': display['name'],
                'group': display['plugin_name'],
            })
    return options


@ota_bp.route('/ota')
def ota_page():
    """Render OTA management page."""
    dev_mode = config_manager.get_dev_mode()
    return render_template('ota.html', 
                         dev_mode=dev_mode,
                         device_type_groups=DEVICE_TYPE_GROUPS,
                         device_type_labels=DEVICE_TYPE_LABELS,
                         lightstrip_model_names=LIGHTSTRIP_MODEL_NAMES,
                         plugin_firmware_options=_plugin_firmware_options())


def collect_available_firmwares() -> tuple[dict, dict | None]:
    """Collect available firmwares from GitHub releases (core and plugins) and local uploads.

    Returns:
        Tuple of (available firmwares keyed by firmware type, release info or None)
    """
    available_firmwares = {}
    release_info = None

    try:
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
                    download_url = asset.get('browser_download_url', '')

                    # Dedicated serial-host radio firmware
                    match_radio_python = re.match(
                        r'huemixlink-(esp32|esp8266)-radio-python-v([\d\.]+)\.bin',
                        name,
                    )
                    if match_radio_python:
                        platform, version = match_radio_python.groups()
                        if platform == 'esp32':
                            available_firmwares['gateway_radio_python'] = {
                                'version': version,
                                'firmware_type': 'gateway_radio_python',
                                'download_url': download_url,
                                'filename': name,
                                'source': 'github',
                                'platform': infer_platform('gateway_radio_python', name)
                            }
                        continue
                    
                    # Match firmware files (including motion_sensor and door_sensor)
                    match = re.match(r'huemixlink-(esp32|esp8266)-(net|radio|button|lightstrip|remote|motion_sensor|door_sensor)(?:-([0-9]+))?-v([\d\.]+)\.bin', name)
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
                            'source': 'github',
                            'platform': infer_platform(firmware_type, name)
                        }
        except Exception as e:
            logger.warning(f"Failed to fetch GitHub releases: {e}")
        
        # Also check plugin repositories for firmware updates
        try:
            plugins = plugin_manager.list_registered_plugins()
            for plugin_entry in plugins:
                ota_entries = plugin_entry.get('ota', [])
                if not ota_entries:
                    continue

                metadata = plugin_entry.get('metadata', {})
                source_repo = metadata.get('source_repo', '')
                if not source_repo:
                    continue

                owner, repo = parse_github_url(source_repo)
                if not owner or not repo:
                    continue

                plugin_id = str(plugin_entry.get('plugin_id', '')).strip()
                if not plugin_id:
                    continue

                try:
                    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
                    resp = requests.get(url, timeout=10, headers=HEADERS)

                    if resp.status_code == 200:
                        release_data = resp.json()
                        tag = release_data.get('tag_name', '')
                        assets = release_data.get('assets', [])

                        for ota_entry in ota_entries:
                            dev_type = ota_entry.get('device_type')
                            asset_pattern = str(ota_entry.get('name', '')).strip()
                            if not asset_pattern or dev_type is None:
                                continue

                            for asset in assets:
                                aname = asset.get('name', '')
                                segments = aname.rsplit('.', 1)[0].split('-')
                                if any(asset_pattern == seg for seg in segments):
                                    version = extract_version_from_filename(aname)
                                    if not version:
                                        version = tag.lstrip('v')
                                    if not version:
                                        version = '0.0.0'

                                    key = f"plugin_{plugin_id}_devtype_{dev_type}"
                                    available_firmwares[key] = {
                                        'version': version,
                                        'firmware_type': key,
                                        'download_url': asset.get('browser_download_url', ''),
                                        'filename': aname,
                                        'source': 'github',
                                        'plugin_id': plugin_id,
                                        'plugin_device_type': dev_type,
                                        'source_repo': source_repo,
                                        'platform': infer_platform(key, aname, ota_entry.get('platform')),
                                    }
                                    break
                except Exception as e:
                    logger.warning(f"Failed to check plugin {plugin_id} updates from {source_repo}: {e}")
        except Exception as e:
            logger.warning(f"Failed to iterate plugins for OTA check: {e}")
        
        # Load and merge locally uploaded firmwares
        local_firmwares = load_local_firmwares()
        for firmware_type_key, firmware_info in local_firmwares.items():
            firmware_info.setdefault(
                'platform',
                infer_platform(firmware_type_key, firmware_info.get('filename', ''))
            )
            # Use local if no GitHub version or if local is newer
            if firmware_type_key not in available_firmwares:
                available_firmwares[firmware_type_key] = firmware_info
            else:
                # Compare versions and use newer one
                github_ver = available_firmwares[firmware_type_key]['version']
                local_ver = firmware_info['version']
                if compare_versions(local_ver, github_ver) > 0:
                    available_firmwares[firmware_type_key] = firmware_info
        
    except Exception as e:
        logger.error(f"Failed to collect available firmwares: {e}")
        raise

    return available_firmwares, release_info


@ota_bp.route('/api/ota/check', methods=['GET'])
def check_updates():
    """Check for available firmware updates from GitHub and local uploads.

    Returns:
        JSON with available firmware versions per device type
    """
    try:
        available_firmwares, release_info = collect_available_firmwares()

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


@ota_bp.route('/api/ota/serial_firmwares', methods=['GET'])
def get_serial_firmwares():
    """List firmware entries that can be flashed with the browser Serial Flash Tool.

    Combines built-in device firmwares with plugin-provided firmwares so the
    serial flash modal can offer every flashable image, including plugin ones.

    Returns:
        JSON with a flat list of firmware entries. Each entry carries display
        metadata (key, label, group), source info usable by /api/ota/binary,
        and a platform hint ('esp32' | 'esp8266' | null).
    """
    try:
        available_firmwares, _ = collect_available_firmwares()
        entries = []

        # Built-in firmware types, in declared group order
        for group, keys in DEVICE_TYPE_GROUPS.items():
            for key in keys:
                firmware = available_firmwares.get(key)
                if not firmware:
                    continue
                entries.append({
                    'key': key,
                    'label': DEVICE_TYPE_LABELS.get(key, key),
                    'group': group,
                    'version': firmware.get('version', '0.0.0'),
                    'source': firmware.get('source', 'github'),
                    'download_url': firmware.get('download_url'),
                    'filename': firmware.get('filename'),
                    'filepath': firmware.get('filepath'),
                    'platform': firmware.get('platform') or infer_platform(key, firmware.get('filename', '')),
                    'is_plugin': False,
                })

        # Plugin firmwares, grouped under the plugin display name
        for key, firmware in available_firmwares.items():
            if not key.startswith('plugin_'):
                continue
            parts = parse_plugin_firmware_key(key)
            if not parts:
                continue
            plugin_id, device_type = parts
            display = resolve_plugin_display(plugin_id, device_type)
            entries.append({
                'key': key,
                'label': display['name'],
                'group': display['plugin_name'],
                'version': firmware.get('version', '0.0.0'),
                'source': firmware.get('source', 'github'),
                'download_url': firmware.get('download_url'),
                'filename': firmware.get('filename'),
                'filepath': firmware.get('filepath'),
                'platform': firmware.get('platform') or infer_platform(key, firmware.get('filename', '')),
                'is_plugin': True,
                'plugin_id': plugin_id,
                'plugin_display_name': display['plugin_name'],
            })

        return jsonify({
            'success': True,
            'firmwares': entries
        })

    except Exception as e:
        logger.error(f"Failed to list serial firmwares: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        plugin_parts = None
        if device_type_str in EXTENDED_DEVICE_TYPES:
            device_type_code, firmware_type = EXTENDED_DEVICE_TYPES[device_type_str]
        else:
            # Allow plugin-declared firmware types (plugin_{plugin_id}_devtype_{device_type})
            plugin_parts = parse_plugin_firmware_key(device_type_str)
            if not plugin_parts:
                return jsonify({'success': False, 'error': f'Invalid device type: {device_type_str}'}), 400
            plugin_id, device_type_code = plugin_parts
            if find_plugin_ota_entry(plugin_id, device_type_code) is None:
                return jsonify({
                    'success': False,
                    'error': f'Plugin does not declare an OTA target for: {device_type_str}'
                }), 400
            firmware_type = device_type_str
        
        # Secure filename
        filename = secure_filename(file.filename or '')
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
        
        # Validate .bin extension
        if not filename.endswith('.bin'):
            return jsonify({'success': False, 'error': 'Only .bin files allowed'}), 400
        
        # Save file with firmware_type prefix to prevent overwriting
        base_name = os.path.splitext(filename)[0]
        filename = f"{firmware_type}_{base_name}.bin"
        
        os.makedirs(FIRMWARE_DIR, exist_ok=True)
        filepath = os.path.join(FIRMWARE_DIR, filename)
        file.save(filepath)
        
        logger.info(f"Uploaded {firmware_type} firmware: {filename} ({os.path.getsize(filepath)} bytes)")
        
        # Store metadata for this upload
        ota_entry = find_plugin_ota_entry(*plugin_parts) if plugin_parts else None
        local_firmwares = load_local_firmwares()
        local_firmwares[firmware_type] = {
            'version': version_str,
            'filename': filename,
            'filepath': filepath,
            'source': 'local',
            'firmware_type': firmware_type,
            'platform': infer_platform(
                firmware_type,
                filename,
                ota_entry.get('platform') if ota_entry else None
            )
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
        os.makedirs(FIRMWARE_DIR, exist_ok=True)
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
                os.makedirs(FIRMWARE_DIR, exist_ok=True)
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
        data = request.get_json(silent=True) or {}
        firmware_path = data.get('firmware_path')
        firmware_type = data.get('firmware_type')
        
        
        if not firmware_path:
            return jsonify({'success': False, 'error': 'firmware_path required'}), 400
        
        # Validate file exists
        if not os.path.exists(firmware_path):
            return jsonify({'success': False, 'error': 'Firmware file not found'}), 404
        
        # Determine device type
        device = None
        device_type = None
        matched_gateway = None
        
        # Check gateways
        gateways = device_manager.get_all_gateways()
        for gw in gateways:
            if gw.get('mac_address') == device_mac or gw.get('radio_mac') == device_mac:
                device = gw
                matched_gateway = gw
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
        
        # Check motion sensors
        if not device:
            sensor = device_manager.get_motion_sensor_by_mac(device_mac)
            if sensor:
                device = sensor
                device_type = DEV_MOTION

        # Check door sensors
        if not device:
            sensor = device_manager.get_door_sensor_by_mac(device_mac)
            if sensor:
                device = sensor
                device_type = DEV_DOOR
        
        # Check plugin devices
        last_gateway_mac = None
        if not device:
            plugin_id_for_mac = plugin_manager.get_plugin_for_mac(device_mac)
            if plugin_id_for_mac:
                plugin_devices = plugin_manager.collect_plugin_ota_devices()
                for pd in plugin_devices:
                    if pd.get('mac_address', '').upper() == device_mac.upper():
                        device = {'mac_address': device_mac, 'plugin_id': plugin_id_for_mac}
                        device_type = pd.get('device_type', 0)
                        last_gateway_mac = pd.get('last_gateway_mac')
                        break
        
        if not device:
            return jsonify({'success': False, 'error': 'Device not found'}), 404

        if device_type is None:
            return jsonify({'success': False, 'error': 'Unsupported device type'}), 400

        if device_type == DEV_GATEWAY and matched_gateway:
            is_serial_gateway = _is_serial_gateway(matched_gateway)

            if is_serial_gateway and firmware_type != 'gateway_radio_python':
                return jsonify({
                    'success': False,
                    'error': 'Serial gateway OTA only supports gateway_radio_python firmware'
                }), 400

            if not is_serial_gateway and firmware_type == 'gateway_radio_python':
                return jsonify({
                    'success': False,
                    'error': 'gateway_radio_python firmware is only valid for serial gateways'
                }), 400
        
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
                            2: 'ESP8266, RGB, WS2812B',
                            3: 'ESP32, RGBW, SK6812',
                            4: 'ESP8266, RGBW, SK6812',
                            5: 'ESP32, RGB+PWMW, WS2812B',
                            6: 'ESP8266, RGB+PWMW, WS2812B'
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

        if device_type is None:
            return jsonify({
                'success': False,
                'error': 'Unable to resolve device type for OTA'
            }), 400
        
        # Start OTA update (pass gateway preference for plugin devices)
        success = network_server.start_ota_update(device_mac, device_type, firmware_path, gateway_preference=last_gateway_mac)
        
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
            is_serial = _is_serial_gateway(gw)

            if not is_serial:
                # Add Net Node entry (WiFi MAC) only for standard UDP gateways.
                devices.append({
                    'mac_address': gw.get('mac_address'),
                    'radio_mac': gw.get('radio_mac'),
                    'name': gw.get('name', f"Gateway {gw.get('mac_address', '')[-8:]}"),
                    'type': 'gateway_net',
                    'device_type': DEV_GATEWAY,
                    'version': gw.get('version_net', '0.0.0')
                })

            # Add Radio Node entry (Radio MAC) - serial gateways use dedicated firmware type.
            if gw.get('radio_mac'):
                devices.append({
                    'mac_address': gw.get('radio_mac'),
                    'wifi_mac': gw.get('mac_address'),
                    'name': gw.get('name', f"Gateway {gw.get('mac_address', '')[-8:]}"),
                    'type': 'gateway_radio_python' if is_serial else 'gateway_radio',
                    'device_type': DEV_GATEWAY,
                    'version': gw.get('version_radio', '0.0.0'),
                    'is_serial': is_serial,
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
                'model_id': light.get('model_id')
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
                'platform': platform
            })
        
        # Get all motion sensors
        motion_sensors = device_manager.get_all_motion_sensors()
        for sensor in motion_sensors:
            devices.append({
                'mac_address': sensor.get('mac_address'),
                'name': sensor.get('name', f"Motion Sensor {sensor.get('mac_address', '')[-8:]}"),
                'type': 'motion_sensor',
                'device_type': DEV_MOTION,
                'version': sensor.get('version', '0.0.0'),
                'platform': sensor.get('platform', 'esp32')
            })

        # Get all door sensors
        door_sensors = device_manager.get_all_door_sensors()
        for sensor in door_sensors:
            devices.append({
                'mac_address': sensor.get('mac_address'),
                'name': sensor.get('name', f"Door Sensor {sensor.get('mac_address', '')[-8:]}"),
                'type': 'door_sensor',
                'device_type': DEV_DOOR,
                'version': sensor.get('version', '0.0.0'),
                'platform': sensor.get('platform', 'esp32')
            })
        
        # Collect plugin devices from loaded plugins
        plugin_devices = plugin_manager.collect_plugin_ota_devices()
        for pd in plugin_devices:
            plugin_id = pd.get('plugin_id', '')
            dev_type = pd.get('device_type', 0)
            # Resolve display metadata from the plugin registry
            display = resolve_plugin_display(plugin_id, dev_type)
            devices.append({
                'mac_address': pd.get('mac_address'),
                'name': pd.get('name', 'Plugin Device'),
                'type': f"plugin_{plugin_id}_devtype_{dev_type}",
                'device_type': dev_type,
                'version': pd.get('version', '0.0.0'),
                'platform': pd.get('platform', 'esp32'),
                'plugin_id': plugin_id,
                'is_plugin': True,
                'home_box_color': display['color'],
                'home_box_icon': display['icon'],
                'plugin_display_name': display['name'],
            })
        
        return jsonify({
            'success': True,
            'devices': devices
        })
        
    except Exception as e:
        logger.error(f"Failed to get devices: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
