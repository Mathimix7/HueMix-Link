"""
OTA (Over-The-Air) firmware update manager.

Handles firmware update sessions, progress tracking, and state management
for all device types in the HueMixLink system.
"""
import logging
import threading
import json
import os
import hashlib
from typing import Optional, Dict, Tuple
from datetime import datetime
from enum import Enum
import re
from services.data_manager import data_manager
from constants import (
    FILE_OTA_SESSIONS, OTA_READY_TIMEOUT,
    OTA_POST_UPDATE_TIMEOUT, OTA_MAX_FIRMWARE_SIZE,
    DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE, DEV_MOTION
)

logger = logging.getLogger(__name__)


class OTAState(Enum):
    """OTA session states."""
    IDLE = "idle"
    QUEUED = "queued"
    WAITING_READY = "waiting_ready"
    TRANSFERRING = "transferring"
    VALIDATING = "validating"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"


ACTIVE_OR_QUEUED_STATES = (
    OTAState.QUEUED,
    OTAState.WAITING_READY,
    OTAState.TRANSFERRING,
    OTAState.VALIDATING,
)


class OTASession:
    """Represents an active or queued OTA update session for a single device."""
    
    def __init__(self, device_mac: str, device_type: int, firmware_path: str, 
                 firmware_size: int, sha256_hash: bytes, version: Tuple[int, int, int, int],
                 wake_triggered: bool = False, preferred_gateway_mac: Optional[str] = None,
                 depends_on_mac: Optional[str] = None):
        """Initialize OTA session.
        
        Args:
            device_mac: Target device MAC address
            device_type: Device type (DEV_GATEWAY, DEV_BUTTON, DEV_LIGHT, DEV_REMOTE)
            firmware_path: Path to firmware binary file
            firmware_size: Size of firmware in bytes
            sha256_hash: SHA256 hash of firmware
            version: Firmware version tuple (major, minor, patch, build)
            wake_triggered: True for v2 battery devices that start on next wake
            preferred_gateway_mac: Best gateway MAC for routing chunks
            depends_on_mac: Optional MAC that must complete OTA before this session starts
        """
        self.device_mac = device_mac
        self.device_type = device_type
        self.firmware_path = firmware_path
        self.firmware_size = firmware_size
        self.sha256_hash = sha256_hash
        self.version = version
        
        self.state = OTAState.QUEUED
        self.wake_triggered = wake_triggered
        self.depends_on_mac = depends_on_mac
        self.queued_at = datetime.now()
        self.ready_timeout_token = 0
        self.chunks_sent = 0
        self.total_chunks = 0
        self.bytes_sent = 0
        self.start_time = None  # When session became active (WAITING_READY)
        self.transfer_start_time = None  # When actual transfer began (TRANSFERRING)
        self.end_time = None
        self.validating_start_time = None  # Track when validation phase started
        self.last_update_time = None  # Track time of last progress update for instantaneous speed
        self.last_bytes_sent = 0  # Bytes sent at last progress update
        self.failure_reason = None
        self.battery_mv = None
        self.preferred_gateway_mac = preferred_gateway_mac  # Best gateway for routing chunks to this device
        
        self.lock = threading.Lock()
    
    def to_dict(self) -> Dict:
        """Convert session to dictionary for JSON serialization.
        
        Returns:
            Dictionary representation of session
        """
        with self.lock:
            elapsed = 0
            transfer_speed = 0
            
            if self.start_time:
                # Elapsed time always keeps counting from session start (includes WAITING_READY)
                elapsed = (datetime.now() - self.start_time).total_seconds()
            
            # Speed calculation: only count time during actual transfer (excludes WAITING_READY)
            if self.transfer_start_time:
                if self.validating_start_time or self.end_time:
                    # Transfer complete (validating or finished) - show final average speed (frozen)
                    time_point = self.validating_start_time or self.end_time
                    elapsed_at_completion = (time_point - self.transfer_start_time).total_seconds()
                    if elapsed_at_completion > 0:
                        transfer_speed = self.bytes_sent / elapsed_at_completion
                else:
                    # Transfer in progress - show instantaneous speed
                    if self.last_update_time and self.state == OTAState.TRANSFERRING:
                        time_since_last = (datetime.now() - self.last_update_time).total_seconds()
                        if 0.1 < time_since_last < 10:  # Valid recent update
                            bytes_since_last = self.bytes_sent - self.last_bytes_sent
                            transfer_speed = bytes_since_last / time_since_last
                        else:
                            transfer_elapsed = (datetime.now() - self.transfer_start_time).total_seconds()
                            if transfer_elapsed > 0:
                                transfer_speed = self.bytes_sent / transfer_elapsed  # Fallback to average
                    else:
                        transfer_elapsed = (datetime.now() - self.transfer_start_time).total_seconds()
                        if transfer_elapsed > 0:
                            transfer_speed = self.bytes_sent / transfer_elapsed
            
            return {
                'device_mac': self.device_mac,
                'device_type': self.device_type,
                'firmware_size': self.firmware_size,
                'version': f"{self.version[0]}.{self.version[1]}.{self.version[2]}.{self.version[3]}",
                'state': self.state.value,
                'wake_triggered': self.wake_triggered,
                'depends_on_mac': self.depends_on_mac,
                'chunks_sent': self.chunks_sent,
                'total_chunks': self.total_chunks,
                'bytes_sent': self.bytes_sent,
                'progress_percent': round((self.chunks_sent / self.total_chunks * 100) if self.total_chunks > 0 else 0, 1),
                'elapsed_seconds': round(elapsed, 1),
                'transfer_speed_kbps': round(transfer_speed / 1024, 1) if transfer_speed > 0 else 0,
                'failure_reason': self.failure_reason,
                'battery_mv': self.battery_mv
            }


class OTAManager:
    """Manages OTA update sessions and single-active-transfer queue for all devices."""
    
    def __init__(self):
        """Initialize OTA manager."""
        self.sessions: Dict[str, OTASession] = {}
        self.lock = threading.Lock()
        self.active_session_mac: Optional[str] = None
        self._on_slot_released = None
        
        # Load persisted sessions
        self._load_sessions()

    def set_slot_released_callback(self, callback):
        """Register callback invoked whenever the active OTA slot becomes free."""
        self._on_slot_released = callback

    def _notify_slot_released(self):
        cb = self._on_slot_released
        if cb:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in OTA slot-released callback: {e}")
    
    def _load_sessions(self):
        """Load OTA sessions from persistent storage."""
        try:
            sessions_data = data_manager.read_json(FILE_OTA_SESSIONS, default=[])
            logger.info(f"Loaded {len(sessions_data)} OTA session(s) from storage")
        except Exception as e:
            logger.error(f"Failed to load OTA sessions: {e}")
    
    def _save_sessions(self):
        """Save OTA sessions to persistent storage (snapshot under lock to avoid AB-BA deadlock)."""
        try:
            with self.lock:
                sessions_snapshot = list(self.sessions.values())
            sessions_data = [session.to_dict() for session in sessions_snapshot]
            data_manager.write_json(FILE_OTA_SESSIONS, sessions_data)
        except Exception as e:
            logger.error(f"Failed to save OTA sessions: {e}")
    
    def create_session(self, device_mac: str, device_type: int, firmware_path: str,
                       wake_triggered: bool = False,
                       preferred_gateway_mac: Optional[str] = None,
                       depends_on_mac: Optional[str] = None) -> Optional[OTASession]:
        """Create a new queued OTA session for a device.
        
        Multiple devices may be queued concurrently; only one device holds
        ``active_session_mac`` at a time via :meth:`claim_active_session`.
        
        Args:
            device_mac: Target device MAC address
            device_type: Device type constant
            firmware_path: Path to firmware binary file
            wake_triggered: True for v2 battery devices that start on next wake
            preferred_gateway_mac: Optional preferred gateway MAC
            depends_on_mac: Optional dependency MAC that must complete first
            
        Returns:
            OTASession instance if successful, None if failed
        """
        with self.lock:
            # Check if this device already has an active or queued session
            if device_mac in self.sessions:
                old_session = self.sessions[device_mac]
                if old_session.state in ACTIVE_OR_QUEUED_STATES:
                    logger.warning(f"Cannot queue OTA for {device_mac}: Session already {old_session.state.value}")
                    return None
        
        try:
            # Read firmware file and calculate hash
            with open(firmware_path, 'rb') as f:
                firmware_data = f.read()
            
            firmware_size = len(firmware_data)
            
            if firmware_size > OTA_MAX_FIRMWARE_SIZE:
                logger.error(f"Firmware too large: {firmware_size} bytes (max {OTA_MAX_FIRMWARE_SIZE})")
                return None
            
            sha256_hash = hashlib.sha256(firmware_data).digest()
            
            # Parse version from firmware metadata or filename
            version = (0, 0, 0, 0)  # Default version            
            # First, try to get version from uploaded firmware metadata
            try:
                metadata_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'firmware', 'local_firmwares.json')
                if os.path.exists(metadata_path):
                    with open(metadata_path, 'r') as f:
                        local_firmwares = json.load(f)
                        
                    # Try to find matching firmware by filepath
                    for fw_type, fw_data in local_firmwares.items():
                        if fw_data.get('filepath') == firmware_path:
                            version_str = fw_data.get('version', '')
                            if version_str:
                                parts = version_str.split('.')
                                if len(parts) >= 3:
                                    version = (int(parts[0]), int(parts[1]), int(parts[2]), 0)
                                    logger.info(f"Found version {version_str} from firmware metadata")
                                    break
            except Exception as e:
                logger.warning(f"Could not read firmware metadata: {e}")
            
            if version == (0, 0, 0, 0):
                version_match = re.search(r'-v(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\.bin$', os.path.basename(firmware_path))
                if version_match:
                    major = int(version_match.group(1))
                    minor = int(version_match.group(2))
                    patch = int(version_match.group(3))
                    build = int(version_match.group(4)) if version_match.group(4) else 0
                    version = (major, minor, patch, build)
            
            # Create queued session
            session = OTASession(
                device_mac=device_mac,
                device_type=device_type,
                firmware_path=firmware_path,
                firmware_size=firmware_size,
                sha256_hash=sha256_hash,
                version=version,
                wake_triggered=wake_triggered,
                preferred_gateway_mac=preferred_gateway_mac,
                depends_on_mac=depends_on_mac,
            )
            
            with self.lock:
                # Re-check under lock in case of concurrent requests for the same MAC
                existing = self.sessions.get(device_mac)
                if existing and existing.state in ACTIVE_OR_QUEUED_STATES:
                    logger.warning(f"Cannot queue OTA for {device_mac}: Session already {existing.state.value}")
                    return None
                self.sessions[device_mac] = session
            
            self._save_sessions()
            logger.info(f"Created queued OTA session for {device_mac}: {firmware_size} bytes, version {version}, wake_triggered={wake_triggered}")
            
            return session
            
        except Exception as e:
            logger.error(f"Failed to create OTA session: {e}")
            return None

    def claim_active_session(self, device_mac: str) -> bool:
        """Atomically claim the single active OTA transfer slot for ``device_mac``."""
        with self.lock:
            if self.active_session_mac is not None and self.active_session_mac != device_mac:
                return False
            session = self.sessions.get(device_mac)
            if not session or session.state not in (OTAState.QUEUED, OTAState.WAITING_READY):
                return False
            self.active_session_mac = device_mac
            return True

    def requeue_session(self, device_mac: str, reason: Optional[str] = None) -> bool:
        """Return a WAITING_READY session back to QUEUED and release the active slot."""
        session = self.get_session(device_mac)
        if not session:
            return False

        slot_released = False
        with session.lock:
            if session.state != OTAState.WAITING_READY:
                return False
            session.state = OTAState.QUEUED
            session.start_time = None
            session.transfer_start_time = None
            session.validating_start_time = None
            with self.lock:
                if self.active_session_mac == device_mac:
                    self.active_session_mac = None
                    slot_released = True
            logger.info(f"OTA {device_mac}: waiting_ready → queued" + (f" ({reason})" if reason else ""))

        self._save_sessions()
        if slot_released:
            self._notify_slot_released()
        return True

    def get_next_queued_session(self) -> Optional[OTASession]:
        """Return the next eligible queued always-on session in FIFO order, or None."""
        failed_dependents = []
        selected: Optional[OTASession] = None

        with self.lock:
            if self.active_session_mac is not None:
                return None

            candidates = [
                s for s in self.sessions.values()
                if s.state == OTAState.QUEUED and not s.wake_triggered
            ]
            candidates.sort(key=lambda s: s.queued_at)

            for candidate in candidates:
                dep_mac = candidate.depends_on_mac
                if dep_mac:
                    dep_session = self.sessions.get(dep_mac)
                    if dep_session:
                        if dep_session.state in ACTIVE_OR_QUEUED_STATES:
                            continue  # Wait for dependency (e.g. gateway_radio) to finish
                        if dep_session.state in (OTAState.FAILED, OTAState.ABORTED):
                            failed_dependents.append((candidate.device_mac, dep_mac))
                            continue
                selected = candidate
                break

        for mac, dep_mac in failed_dependents:
            self.update_session_state(
                mac,
                OTAState.FAILED,
                f"Dependency {dep_mac} did not complete",
            )

        return selected

    def has_active_or_queued_session(self, device_mac: str) -> bool:
        """Return True if ``device_mac`` already has an active or queued OTA session."""
        with self.lock:
            session = self.sessions.get(device_mac)
            return bool(session and session.state in ACTIVE_OR_QUEUED_STATES)
    
    def get_session(self, device_mac: str) -> Optional[OTASession]:
        """Get OTA session for a device.
        
        Args:
            device_mac: Device MAC address
            
        Returns:
            OTASession instance if exists, None otherwise
        """
        with self.lock:
            return self.sessions.get(device_mac)
    
    def update_session_state(self, device_mac: str, new_state: OTAState, failure_reason: str = None):
        """Update OTA session state.
        
        Args:
            device_mac: Device MAC address
            new_state: New OTA state
            failure_reason: Optional failure reason
        """
        session = self.get_session(device_mac)
        if not session:
            return
        
        slot_released = False
        with session.lock:
            old_state = session.state
            session.state = new_state
            
            # Start overall timer when session becomes active
            if new_state == OTAState.WAITING_READY:
                session.ready_timeout_token += 1
                if not session.start_time:
                    session.start_time = datetime.now()
            
            # Start transfer timer when actual data transfer begins
            if new_state == OTAState.TRANSFERRING and not session.transfer_start_time:
                session.transfer_start_time = datetime.now()
            
            if new_state == OTAState.VALIDATING:
                session.validating_start_time = datetime.now()
            
            if new_state in [OTAState.COMPLETE, OTAState.FAILED, OTAState.ABORTED]:
                session.end_time = datetime.now()
                
                # Release active session lock
                with self.lock:
                    if self.active_session_mac == device_mac:
                        self.active_session_mac = None
                        slot_released = True
            
            if failure_reason:
                session.failure_reason = failure_reason
            
            logger.info(f"OTA {device_mac}: {old_state.value} → {new_state.value}" + 
                       (f" ({failure_reason})" if failure_reason else ""))
        
        self._save_sessions()
        if slot_released:
            self._notify_slot_released()
    
    def update_progress(self, device_mac: str, chunks_sent: int, total_chunks: int, bytes_sent: int):
        """Update OTA transfer progress.
        
        Args:
            device_mac: Device MAC address
            chunks_sent: Number of chunks sent
            total_chunks: Total number of chunks
            bytes_sent: Number of bytes sent
        """
        session = self.get_session(device_mac)
        if not session:
            return
        
        with session.lock:
            session.last_bytes_sent = session.bytes_sent  # Store previous value
            session.last_update_time = datetime.now()  # Track update time
            session.chunks_sent = chunks_sent
            session.total_chunks = total_chunks
            session.bytes_sent = bytes_sent
        
        # Save periodically (every 10 chunks)
        if chunks_sent % 10 == 0:
            self._save_sessions()
    
    def set_battery_voltage(self, device_mac: str, battery_mv: int):
        """Set device battery voltage from PKT_OTA_READY response.
        
        Args:
            device_mac: Device MAC address
            battery_mv: Battery voltage in millivolts
        """
        session = self.get_session(device_mac)
        if not session:
            return
        
        with session.lock:
            session.battery_mv = battery_mv
        
        logger.info(f"OTA {device_mac}: Battery voltage {battery_mv}mV")
    
    def clear_session(self, device_mac: str):
        """Clear OTA session for a device.
        
        Args:
            device_mac: Device MAC address
        """
        slot_released = False
        with self.lock:
            if device_mac in self.sessions:
                del self.sessions[device_mac]
                
                if self.active_session_mac == device_mac:
                    self.active_session_mac = None
                    slot_released = True
        
        self._save_sessions()
        logger.info(f"Cleared OTA session for {device_mac}")
        if slot_released:
            self._notify_slot_released()
    
    def get_all_sessions(self) -> Dict[str, Dict]:
        """Get all OTA sessions as dictionaries (snapshot under lock to avoid AB-BA deadlock).
        
        Returns:
            Dictionary mapping device MAC to session data
        """
        with self.lock:
            items = list(self.sessions.items())
        return {mac: session.to_dict() for mac, session in items}
    
    def is_update_in_progress(self) -> bool:
        """Check if any update is in progress.
        
        Returns:
            True if an update is active
        """
        with self.lock:
            return self.active_session_mac is not None


# Global singleton instance
ota_manager = OTAManager()
