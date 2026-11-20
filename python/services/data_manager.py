"""
Data Manager - Handles all JSON file read/write operations.

This service provides thread-safe JSON file operations for storing configuration data
like button configs, server configs, bridge settings, etc.
"""
import json
import shutil
from pathlib import Path
from typing import Any, Optional
import threading
import logging

logger = logging.getLogger(__name__)


class DataManager:
    """Manages JSON data files with thread-safe operations."""
    
    def __init__(self, data_dir: Optional[str] = None):
        """Initialize the data manager.
        
        Args:
            data_dir: Directory to store JSON files. Defaults to 'data' folder at project root.
        """
        if data_dir is None:
            # Get the data directory relative to this file
            # data_manager.py is in services/, so go up one level to project root, then into data
            current_dir = Path(__file__).parent.parent
            self.data_dir = current_dir / 'data'
        else:
            self.data_dir = Path(data_dir)
        
        self.data_dir.mkdir(exist_ok=True)
        self._locks = {}
    
    def _get_lock(self, filename: str) -> threading.Lock:
        """Get or create a lock for a specific file."""
        if filename not in self._locks:
            self._locks[filename] = threading.Lock()
        return self._locks[filename]
    
    def _get_filepath(self, filename: str) -> Path:
        """Get the full path for a JSON file."""
        return self.data_dir / filename
    
    def read_json(self, filename: str, default: Any = None) -> Any:
        """Read data from a JSON file.
        
        Args:
            filename: Name of the JSON file (e.g., 'buttons.json')
            default: Default value if file doesn't exist
            
        Returns:
            Data from the file or default value
        """
        filepath = self._get_filepath(filename)
        backup_path = filepath.with_suffix('.json.bak')
        lock = self._get_lock(filename)
        
        with lock:
            if not filepath.exists():
                return default if default is not None else []
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.warning(f"Failed to read {filename}: {e}")
                
                # Try to recover from backup
                if backup_path.exists():
                    logger.info(f"Attempting to recover {filename} from backup...")
                    try:
                        with open(backup_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        # Restore from backup
                        shutil.copy2(backup_path, filepath)
                        logger.info(f"Successfully recovered {filename} from backup")
                        return data
                    except Exception as backup_error:
                        logger.error(f"Backup recovery failed for {filename}: {backup_error}")
                
                return default if default is not None else []
    
    def write_json(self, filename: str, data: Any):
        """Write data to a JSON file with atomic write and backup.
        
        Args:
            filename: Name of the JSON file (e.g., 'buttons.json')
            data: Data to write
        """
        filepath = self._get_filepath(filename)
        backup_path = filepath.with_suffix('.json.bak')
        temp_path = filepath.with_suffix('.json.tmp')
        lock = self._get_lock(filename)
        
        with lock:
            # Create backup if file exists
            if filepath.exists():
                try:
                    shutil.copy2(filepath, backup_path)
                except Exception as e:
                    logger.warning(f"Failed to create backup for {filename}: {e}")
            
            # Write to temp file first
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                
                # Atomic rename (overwrites existing file)
                temp_path.replace(filepath)
            except Exception as e:
                # Clean up temp file if it exists
                if temp_path.exists():
                    temp_path.unlink()
                logger.error(f"Failed to write {filename}: {e}")
                raise

    def update_json(self, filename: str, update_func):
        """Update a JSON file using a function.
        
        Args:
            filename: Name of the JSON file (e.g., 'buttons.json')
            update_func: Function that takes current data and returns updated data
        """
        filepath = self._get_filepath(filename)
        lock = self._get_lock(filename)
        
        with lock:
            # Read current data without acquiring lock again
            if not filepath.exists():
                current_data = []
            else:
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        current_data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    current_data = []
            
            # Apply update function
            updated_data = update_func(current_data)
            
            # Write back without acquiring lock again
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(updated_data, f, indent=2)
            
            return updated_data


# Global singleton instance - use this everywhere
data_manager = DataManager()
