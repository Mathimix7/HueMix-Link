"""
Backup manager service for creating and restoring system backups.
"""

import os
import tarfile
import tempfile
import shutil
from datetime import datetime
from typing import List, Dict, Optional
from services.config_change_notifier import config_notifier
from services.data_manager import data_manager
from constants import FILE_BRIDGE, FILE_BUTTONS, FILE_LIGHTSTRIPS
import threading
from network.network_server import network_server
import logging

logger = logging.getLogger(__name__)

class BackupManager:
    """Manages backup creation and restoration of the data directory."""
    
    def __init__(self):
        self.data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        self.data_dir = os.path.abspath(self.data_dir)
        self.backup_dir = os.path.join(self.data_dir, 'backups')
        os.makedirs(self.backup_dir, exist_ok=True)
    
    def create_backup(self, out_path: Optional[str] = None) -> str:
        """
        Create a tar.gz backup of the data directory.
        
        Args:
            out_path: Optional custom output path. If None, creates in backups/ with timestamp.
        
        Returns:
            Path to the created backup file.
        """
        if out_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'huemix_link_backup_{timestamp}.tar.gz'
            out_path = os.path.join(self.backup_dir, filename)
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        
        # Create tar.gz archive
        with tarfile.open(out_path, 'w:gz') as tar:
            # Add all files from data directory except backups, temp files, etc.
            for item in os.listdir(self.data_dir):
                item_path = os.path.join(self.data_dir, item)
                
                # Skip backups directory, temp files, and cache
                if item in ['backups', '__pycache__'] or item.endswith(('.bak', '.tmp')):
                    continue
                
                # Add to archive with relative path
                tar.add(item_path, arcname=item)

        logger.info(f"Backup created at: {out_path}")

        return out_path
    
    def list_backups(self) -> List[Dict[str, any]]:
        """
        List all available backup files.
        
        Returns:
            List of dicts containing backup info (name, size, mtime).
        """
        backups = []
        
        if not os.path.exists(self.backup_dir):
            return backups
        
        for filename in os.listdir(self.backup_dir):
            if filename.endswith('.tar.gz') or filename.endswith('.tgz'):
                filepath = os.path.join(self.backup_dir, filename)
                stat = os.stat(filepath)
                backups.append({
                    'name': filename,
                    'size': stat.st_size,
                    'mtime': stat.st_mtime
                })
        
        # Sort by modification time, newest first
        backups.sort(key=lambda x: x['mtime'], reverse=True)
        
        return backups
    
    def restore_backup(self, archive_path: str):
        """
        Restore a backup, replacing current data directory contents.
        
        This performs an atomic restore:
        1. Extract to temporary directory
        2. Validate extraction
        3. Move current data to backup location
        4. Move new data into place
        
        Args:
            archive_path: Path to the backup tar.gz file.
        
        Raises:
            Exception if restore fails.
        """

        logger.info(f"Restoring backup from: {archive_path}")
        if not os.path.exists(archive_path):
            raise FileNotFoundError(f"Backup file not found: {archive_path}")
        
        # Create temporary directory for extraction
        temp_dir = tempfile.mkdtemp(prefix='restore_')
        
        try:
            # Extract archive to temp directory
            with tarfile.open(archive_path, 'r:gz') as tar:
                tar.extractall(temp_dir)
            
            # Validate extraction - check if essential files exist
            # (This is a basic check, you can add more validation)
            if not os.listdir(temp_dir):
                raise Exception("Backup archive is empty")
            
            # Create backup of current data
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            old_data_path = f"{self.data_dir}.old-{timestamp}"
            
            # Move current data to backup location
            if os.path.exists(self.data_dir):
                # Move everything except the backups directory
                os.makedirs(old_data_path, exist_ok=True)
                for item in os.listdir(self.data_dir):
                    if item == 'backups':
                        continue
                    src = os.path.join(self.data_dir, item)
                    dst = os.path.join(old_data_path, item)
                    shutil.move(src, dst)
            
            # Move restored data into place
            for item in os.listdir(temp_dir):
                src = os.path.join(temp_dir, item)
                dst = os.path.join(self.data_dir, item)
                
                # Remove existing if present
                if os.path.exists(dst):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                
                # Move new data
                shutil.move(src, dst)

            logger.info(f"Backup restored successfully from: {archive_path}, previous data backed up at: {old_data_path}")

            # Notify services about restored configuration so they can reinitialize
            try:
                try:
                    bridge_cfg = data_manager.read_json(FILE_BRIDGE, default={})
                    config_notifier.notify_change('bridge_config', {'config': bridge_cfg})
                except Exception:
                    logger.warning("Failed to notify bridge config change after restore", exc_info=True)

                # Notify button and lightstrip config changes
                try:
                    buttons = data_manager.read_json(FILE_BUTTONS, default=[])
                    config_notifier.notify_change('button_config', {'count': len(buttons)})
                except Exception:
                    logger.warning("Failed to notify button config change after restore", exc_info=True)

                try:
                    strips = data_manager.read_json(FILE_LIGHTSTRIPS, default=[])
                    config_notifier.notify_change('lightstrip_config', {'count': len(strips)})
                except Exception:
                    logger.warning("Failed to notify lightstrip config change after restore", exc_info=True)
                    
                try:
                    config_notifier.notify_change('gateways_reload', {})
                except Exception:
                    logger.warning("Failed to notify gateway reload after restore", exc_info=True)
            except Exception:
                logger.error("Error during post-restore notifications", exc_info=True)

        except Exception as e:
            logger.error(f"Error restoring backup: {e}", exc_info=True)
            raise

        finally:
            # Clean up temp directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.debug(f"Cleaned up temporary directory: {temp_dir}")


# Global singleton instance
backup_manager = BackupManager()
