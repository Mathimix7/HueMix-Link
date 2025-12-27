"""
Admin blueprint for backup and system management.
Provides endpoints for creating, listing, downloading, and restoring backups.
Also manages HOME_ID operations.
"""

from flask import Blueprint, request, jsonify, send_from_directory, render_template
import os
import threading
from werkzeug.utils import secure_filename
from services.backup_manager import backup_manager
from services.home_id_manager import home_id_manager
from services.config_manager import config_manager

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/settings')
def settings_page():
    """Render the settings page."""
    return render_template('settings.html')

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
