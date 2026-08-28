"""
Admin blueprint for backup and system management.
Provides endpoints for creating, listing, downloading, and restoring backups.
Also manages HOME_ID operations.
"""

from flask import Blueprint, request, jsonify, send_from_directory, render_template
import os
import shutil
import subprocess
import threading
from werkzeug.utils import secure_filename
from services.backup_manager import backup_manager
from services.home_id_manager import home_id_manager
from services.config_manager import config_manager
from services.plugin_install_service import plugin_install_service

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/settings')
def settings_page():
    """Render the settings page."""
    return render_template('settings.html')


@admin_bp.route('/plugins')
def plugins_page():
    """Render plugin management dashboard."""
    return render_template('admin_plugins.html')


@admin_bp.route('/api/plugins', methods=['GET'])
def list_plugins():
    """List currently registered plugins with install metadata."""
    try:
        plugins = plugin_install_service.list_plugins()
        return jsonify({
            'success': True,
            'plugins': plugins,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'plugins': [],
        }), 500


@admin_bp.route('/api/plugins/install', methods=['POST'])
def install_plugin_from_repo():
    """Install a plugin from a GitHub repository URL."""
    try:
        data = request.get_json(silent=True) or {}
        repo_url = str(data.get('repo_url') or '').strip()
        branch = str(data.get('branch') or '').strip() or None

        if not repo_url:
            return jsonify({
                'success': False,
                'error': 'repo_url is required',
            }), 400

        # Start install in background and return an install id for polling
        result = plugin_install_service.install_from_repo(repo_url=repo_url, branch=branch)
        return jsonify(result)
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@admin_bp.route('/api/plugins/install/status/<install_id>', methods=['GET'])
def install_status(install_id: str):
    """Return status and logs for an ongoing or completed install session."""
    try:
        status = plugin_install_service.get_install_status(install_id)
        return jsonify({
            'success': True,
            'status': status,
        })
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@admin_bp.route('/api/plugins/uninstall', methods=['POST'])
def uninstall_plugin():
    """Uninstall a plugin and clean related registry/binding/history entries."""
    try:
        data = request.get_json(silent=True) or {}
        plugin_identifier = str(data.get('plugin_id') or data.get('id') or '').strip()
        if not plugin_identifier:
            return jsonify({
                'success': False,
                'error': 'plugin_id or id is required',
            }), 400

        result = plugin_install_service.uninstall_plugin(plugin_identifier)
        return jsonify(result)
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@admin_bp.route('/api/plugins/enable', methods=['POST'])
def toggle_plugin_enabled():
    """Enable/disable a plugin entry in plugins.json."""
    try:
        data = request.get_json(silent=True) or {}
        plugin_identifier = str(data.get('plugin_id') or data.get('id') or '').strip()
        enabled = bool(data.get('enabled', False))

        if not plugin_identifier:
            return jsonify({
                'success': False,
                'error': 'plugin_id or id is required',
            }), 400

        result = plugin_install_service.set_enabled(plugin_identifier, enabled)
        return jsonify(result)
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500

@admin_bp.route('/api/plugins/official', methods=['GET'])
def list_official_plugins():
    """Return curated list of official plugins available for one-click install."""
    official = [
        {
            "name": "Temperature Tracking",\
            "id": "temperature_tracking",
            "description": "Monitor and log temperature sensor data from your HueMix-Link devices.",
            "repo_url": "https://github.com/Mathimix7/HueMixLink-Temperature",
            "branch": "main",
            "icon": "thermometer-half",
            "color": "emerald",
        }
    ]
    # Enrich with already-installed status
    installed_ids = {
        p.get("source_repo")
        for p in plugin_install_service.list_plugins()
    }

    for plugin in official:
        plugin["installed"] = any(
            pid in installed_ids for pid in [plugin.get("repo_url")]
        )
    return jsonify({"success": True, "plugins": official})


@admin_bp.route('/api/plugins/updates', methods=['GET'])
def check_all_plugin_updates():
    """Check all plugins for available updates."""
    try:
        results = plugin_install_service.check_all_plugins_for_updates()
        return jsonify({
            "success": True,
            "results": results,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "results": [],
        }), 500


@admin_bp.route('/api/plugins/update', methods=['POST'])
def update_plugin():
    """Update a plugin to its latest GitHub release version."""
    try:
        data = request.get_json(silent=True) or {}
        plugin_identifier = str(data.get('plugin_id') or data.get('id') or '').strip()
        release_tag = str(data.get('release_tag') or '').strip() or None

        if not plugin_identifier:
            return jsonify({
                'success': False,
                'error': 'plugin_id or id is required',
            }), 400

        result = plugin_install_service.update_plugin(plugin_identifier, release_tag=release_tag)
        return jsonify(result)
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@admin_bp.route('/api/service/check', methods=['GET'])
def check_service():
    """Check if the systemd service exists and can be restarted."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "huemix-link"],
            capture_output=True, text=True, timeout=10
        )
        stdout = (result.stdout or "").strip()
        service_exists = stdout in ("active", "inactive", "failed") or result.returncode == 0
        service_active = stdout == "active"
        return jsonify({
            "success": True,
            "service_exists": service_exists,
            "service_active": service_active,
            "can_restart": service_exists,
        })
    except FileNotFoundError:
        return jsonify({
            "success": True,
            "service_exists": False,
            "service_active": False,
            "can_restart": False,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@admin_bp.route('/api/dev-mode', methods=['GET'])
def get_dev_mode():
    """Get dev mode setting."""
    try:
        dev_mode = config_manager.get_dev_mode()
        return jsonify({
            'success': True,
            'dev_mode': dev_mode
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/api/dev-mode', methods=['POST'])
def set_dev_mode():
    """Set dev mode setting."""
    try:
        data = request.get_json(silent=True) or {}
        enabled = data.get('enabled', False)
        config_manager.set_dev_mode(enabled)
        return jsonify({
            'success': True,
            'dev_mode': enabled
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@admin_bp.route('/api/serial-gateway', methods=['GET'])
def get_serial_gateway_settings():
    """Get USB serial gateway settings."""
    try:
        cfg = config_manager.get_serial_gateway_config()
        return jsonify({
            'success': True,
            'serial_gateway': cfg,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@admin_bp.route('/api/serial-ports', methods=['GET'])
def get_serial_ports():
    """Get available USB serial ports for dropdown selection."""
    try:
        cfg = config_manager.get_serial_gateway_config()
        configured_port = str(cfg.get('port', '') or '').strip()

        ports = []
        try:
            from serial.tools import list_ports  # type: ignore

            for port in list_ports.comports():
                ports.append({
                    'device': port.device,
                    'description': port.description,
                    'hwid': port.hwid,
                    'available': True,
                })
        except Exception:
            ports = []

        if configured_port and not any((p.get('device') or '').upper() == configured_port.upper() for p in ports):
            ports.append({
                'device': configured_port,
                'description': 'Configured port (currently not detected)',
                'hwid': '',
                'available': False,
            })

        ports.sort(key=lambda p: (not bool(p.get('available')), (p.get('device') or '').upper()))

        return jsonify({
            'success': True,
            'ports': ports,
            'configured_port': configured_port,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'ports': [],
            'configured_port': '',
        }), 500


@admin_bp.route('/api/serial-gateway', methods=['POST'])
def set_serial_gateway_settings():
    """Set USB serial gateway settings."""
    try:
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get('enabled', False))
        port = str(data.get('port', '') or '').strip()
        baudrate = int(data.get('baudrate', 460800) or 460800)

        if enabled and not port:
            return jsonify({
                'success': False,
                'error': 'Port is required when enabling serial gateway'
            }), 400

        config_manager.set_serial_gateway_config(enabled=enabled, port=port, baudrate=baudrate)

        return jsonify({
            'success': True,
            'serial_gateway': config_manager.get_serial_gateway_config(),
            'message': 'Serial gateway settings applied.'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/backups', methods=['GET'])
def list_backups():
    """List all available backups."""
    try:
        backups = backup_manager.list_backups()
        return jsonify({
            'success': True,
            'backups': backups
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/backups', methods=['POST'])
def create_backup():
    """Create a new backup."""
    try:
        data = request.get_json(silent=True) or {}
        out_path = data.get('out') if data else None
        
        backup_path = backup_manager.create_backup(out_path)
        
        return jsonify({
            'success': True,
            'path': backup_path
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/backups/download/<filename>', methods=['GET'])
def download_backup(filename):
    """Download a specific backup file."""
    try:
        # Secure the filename to prevent directory traversal
        filename = secure_filename(filename)
        backup_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backups')
        backup_dir = os.path.abspath(backup_dir)
        
        return send_from_directory(backup_dir, filename, as_attachment=True)
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 404

@admin_bp.route('/backups/upload-restore', methods=['POST'])
def upload_and_restore():
    """Upload a backup file and restore it."""
    try:
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided'
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400
        
        # Save uploaded file temporarily
        filename = secure_filename(file.filename)
        backup_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        temp_path = os.path.join(backup_dir, f'uploaded_{filename}')
        file.save(temp_path)
        
        # Restore in background thread
        def _restore():
            try:
                backup_manager.restore_backup(temp_path)
                # Clean up uploaded file
                try:
                    os.remove(temp_path)
                except:
                    pass
            except Exception as e:
                print(f"Background restore error: {e}")
        
        thread = threading.Thread(target=_restore, daemon=True)
        thread.start()
        
        return jsonify({
            'success': True,
            'message': 'Restore started'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/backups/restore', methods=['POST'])
def restore_backup():
    """Restore from an existing backup file."""
    try:
        data = request.get_json()
        if not data or 'filename' not in data:
            return jsonify({
                'success': False,
                'error': 'No filename provided'
            }), 400
        
        filename = secure_filename(data['filename'])
        backup_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backups')
        backup_path = os.path.join(backup_dir, filename)
        
        if not os.path.exists(backup_path):
            return jsonify({
                'success': False,
                'error': 'Backup file not found'
            }), 404
        
        # Restore in background thread to avoid HTTP timeout
        def _restore():
            try:
                backup_manager.restore_backup(backup_path)
            except Exception as e:
                print(f"Background restore error: {e}")
        
        thread = threading.Thread(target=_restore, daemon=True)
        thread.start()
        
        return jsonify({
            'success': True,
            'message': 'Restore started'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/backups/delete', methods=['POST'])
def delete_backup():
    """Delete a specific backup file."""
    try:
        data = request.get_json()
        if not data or 'filename' not in data:
            return jsonify({
                'success': False,
                'error': 'No filename provided'
            }), 400

        filename = secure_filename(data['filename'])
        backup_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backups')
        backup_path = os.path.join(backup_dir, filename)

        if not os.path.exists(backup_path):
            return jsonify({
                'success': False,
                'error': 'Backup file not found'
            }), 404

        os.remove(backup_path)

        return jsonify({
            'success': True,
            'message': f'Backup {filename} deleted.'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@admin_bp.route('/api/reset', methods=['POST'])
def reset_everything():
    """Reset everything. Creates a backup first, then deletes all files in data/ except config.json and the new backup."""
    try:
        from services.data_manager import data_manager as dm
        data_dir = dm.data_dir

        # Step 1: Create a backup before resetting
        backup_path = backup_manager.create_backup()
        backup_filename = os.path.basename(backup_path)

        # Step 2: Delete every file and directory in data/ except config.json and the new backup
        for item in data_dir.iterdir():
            # Keep config.json
            if item.is_file() and item.name == 'config.json':
                continue

            # For the backups directory: keep only the backup just created
            if item.is_dir() and item.name == 'backups':
                for backup_item in item.iterdir():
                    if backup_item.is_file() and backup_item.name == backup_filename:
                        continue
                    try:
                        backup_item.unlink()
                    except Exception as e:
                        print(f"Failed to delete {backup_item.name}: {e}")
                continue

            # Delete everything else (files and directories)
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except Exception as e:
                print(f"Failed to delete {item.name}: {e}")

        return jsonify({
            'success': True,
            'message': 'All devices and configurations have been reset.',
            'backup_filename': backup_filename,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@admin_bp.route('/home_id', methods=['GET'])
def get_home_id():
    """Get the current HOME_ID."""
    try:
        home_id = home_id_manager.read_home_id()
        return jsonify({
            'success': True,
            'home_id': home_id
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
