"""Service for listening to Philips Hue Bridge Server-Sent Events (SSE)."""
import logging
import requests
import json
import threading
import time
import urllib3
from typing import Dict, List, Optional, Tuple
from services import data_manager
from services.hue_state_manager import hue_state_manager
from services.hue_config_sync import sync_device_configs_with_hue
from constants import FILE_BRIDGE

# Disable SSL warnings for Hue Bridge self-signed certificate
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class HueSSEListener:
    """
    Listens to Philips Hue Bridge EventStream API for real-time updates.
    Updates HueStateManager with light, room, and scene changes.
    """
    
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._hue_controller = None  # Will be set during initialization
        self._last_reconciliation_time = 0  # Track periodic reconciliation
        self._reconciliation_interval = 300  # 5 minutes

        # device_id -> light ids / light_id -> model id cache (bridge lookups are
        # expensive, so membership resolution reuses a short-lived snapshot).
        self._device_map: Dict[str, List[str]] = {}
        self._light_model_map: Dict[str, str] = {}
        self._device_map_time = 0.0
        self._device_map_ttl = 60  # seconds
        self._device_map_lock = threading.Lock()

        # Debounced full reconciliation for topology changes (add/move/delete).
        self._reconcile_lock = threading.Lock()
        self._reconcile_timer: Optional[threading.Timer] = None
        self._reconcile_running = threading.Lock()
        self._topology_reconcile_delay = 2.0  # seconds

        logger.info("HueSSEListener initialized")
    
    def start(self, hue_controller=None):
        """
        Start listening to SSE events.
        
        Args:
            hue_controller: Optional HueController instance for fetching scene data
        """
        if self._running:
            if hue_controller is not None and hue_controller is not self._hue_controller:
                # The bridge was reconfigured: reconnect against the new one
                # instead of keeping the stale controller/connection around.
                logger.info("Hue controller changed, restarting SSE listener...")
                self.stop()
            else:
                logger.warning("HueSSEListener already running")
                return
        
        self._hue_controller = hue_controller
        self._running = True
        # Every run gets its own stop event so a thread that is still winding
        # down from a previous run can never keep controlling the new one.
        self._stop_event = threading.Event()
        stop_event = self._stop_event
        
        self._thread = threading.Thread(target=self._listen_loop, args=(stop_event,), daemon=True)
        self._thread.start()
        
        logger.info("HueSSEListener started")
    
    def stop(self):
        """Stop listening to SSE events."""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        self._cancel_topology_reconcile()
        
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        
        logger.info("HueSSEListener stopped")
    
    def _reconcile(self, reason: str):
        """Run a full state reconciliation, skipping it if one is already running."""
        if not self._hue_controller:
            return
        
        if not self._reconcile_running.acquire(blocking=False):
            logger.debug(f"Skipping {reason} reconciliation: another run is in progress")
            return
        
        try:
            logger.info(f"Running state reconciliation ({reason}) with bridge...")
            hue_state_manager.reconcile_with_bridge(self._hue_controller)
            self._last_reconciliation_time = time.time()
        except Exception as e:
            logger.warning(f"State reconciliation ({reason}) failed: {e}")
        finally:
            self._reconcile_running.release()
    
    def _schedule_topology_reconcile(self, delay: Optional[float] = None):
        """Schedule a debounced reconciliation after a topology change.

        Moving a light emits several events (room, zone, device, light); a single
        debounced run picks up the final state instead of racing each event.
        """
        wait = self._topology_reconcile_delay if delay is None else delay
        with self._reconcile_lock:
            if self._reconcile_timer is not None:
                self._reconcile_timer.cancel()
            timer = threading.Timer(wait, self._run_topology_reconcile)
            timer.daemon = True
            self._reconcile_timer = timer
            timer.start()

    def _run_topology_reconcile(self):
        with self._reconcile_lock:
            self._reconcile_timer = None
        if not self._running:
            return
        self._reconcile("topology-change")

    def _cancel_topology_reconcile(self):
        with self._reconcile_lock:
            if self._reconcile_timer is not None:
                self._reconcile_timer.cancel()
                self._reconcile_timer = None

    def _get_device_maps(self, force_refresh: bool = False) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        """Return (device_id -> light ids, light_id -> model id), refreshed at most once per TTL."""
        with self._device_map_lock:
            fresh = (time.time() - self._device_map_time) < self._device_map_ttl
            if not force_refresh and fresh and self._device_map_time:
                return dict(self._device_map), dict(self._light_model_map)

        controller = self._hue_controller
        if controller:
            try:
                devices = controller.get_devices()
                device_map: Dict[str, List[str]] = {}
                model_map: Dict[str, str] = {}
                for device in devices:
                    device_id = device.get('id')
                    if not device_id:
                        continue
                    model_id = (device.get('product_data') or {}).get('model_id') or 'LCT001'
                    light_ids = [
                        service.get('rid')
                        for service in device.get('services', [])
                        if service.get('rtype') == 'light' and service.get('rid')
                    ]
                    # Devices without lights are kept with an empty list so callers
                    # can tell "known device without lights" from "unknown device".
                    device_map[device_id] = light_ids
                    for light_id in light_ids:
                        model_map[light_id] = model_id
                with self._device_map_lock:
                    self._device_map = device_map
                    self._light_model_map = model_map
                    self._device_map_time = time.time()
                return dict(device_map), dict(model_map)
            except Exception as e:
                logger.debug(f"Failed to fetch devices for mapping: {e}")

        with self._device_map_lock:
            return dict(self._device_map), dict(self._light_model_map)
    
    def _listen_loop(self, stop_event):
        """Main loop for SSE connection with auto-reconnect."""
        retry_delay = 1
        max_retry_delay = 10  # Reduced from 60s to 10s to minimize missed updates during backoff
                
        while not stop_event.is_set():
            # Periodic reconciliation check (every 5 minutes)
            now = time.time()
            if self._hue_controller and (now - self._last_reconciliation_time) >= self._reconciliation_interval:
                self._reconcile('periodic')
             
            try:
                # Get bridge config
                config = data_manager.read_json(FILE_BRIDGE, default={})
                if not config:
                    logger.warning("Bridge not configured, retrying in 10s...")
                    time.sleep(10)
                    continue
                
                ip = config.get('ip')
                username = config.get('username')
                
                if not ip or not username:
                    logger.error("Bridge IP or username missing")
                    time.sleep(10)
                    continue
                
                # Connect to SSE endpoint
                url = f"https://{ip}/eventstream/clip/v2"
                headers = {
                    'hue-application-key': username,
                    'Accept': 'text/event-stream'  # Request SSE format
                }
                
                logger.debug(f"Connecting to Hue SSE at {ip}...")
                
                # Stream events
                response = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    verify=False,  # Hue uses self-signed cert
                    timeout=(5, None)
                )
                
                if response.status_code != 200:
                    logger.error(f"SSE connection failed: {response.status_code} - {response.text}")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
                    continue
                
                # Log reconnection success if this was a retry
                if retry_delay > 1:
                    logger.debug(f"SSE connection re-established (status={response.status_code})")
                    # Reconnected after disconnect - reconcile to catch any missed updates
                    if self._hue_controller:
                        self._reconcile('reconnect')
                else:
                    logger.info(f"SSE connection established (status={response.status_code})")
                retry_delay = 1  # Reset retry delay on success
                
                try:
                    # Read lines manually - Hue SSE seems to need raw byte reading
                    buffer = b""
                    chunk_count = 0
                    for chunk in response.iter_content(chunk_size=1024):
                        chunk_count += 1
                        if chunk_count == 1:
                            logger.debug(f"First chunk received: {len(chunk)} bytes")
                        
                        if stop_event.is_set():
                            logger.info("SSE listener stopping...")
                            break
                        
                        if not chunk:
                            logger.debug("Empty chunk received")
                            continue
                            
                        buffer += chunk
                        
                        # Process complete lines
                        while b'\n' in buffer:
                            line_bytes, buffer = buffer.split(b'\n', 1)
                            try:
                                line = line_bytes.decode('utf-8').strip()
                                
                                if not line:
                                    continue
                                
                                # SSE format: "data: {...}" or ": keep-alive"
                                if line.startswith('data: '):
                                    json_data = line[6:]  # Remove "data: " prefix
                                    self._process_event(json_data)
                                elif line.startswith(':'):
                                    # Keep-alive comment
                                    pass
                                else:
                                    # Event metadata like "id: ..."
                                    pass
                            
                            except Exception as e:
                                logger.error(f"Error processing SSE line: {e}", exc_info=True)
                    
                    logger.info(f"SSE event loop ended (processed {chunk_count} chunks)")
                
                finally:
                    response.close()
            
            except requests.exceptions.RequestException as e:
                if not stop_event.is_set():
                    # Timeouts are normal for long-lived SSE connections
                    log_func = logger.debug if "timed out" in str(e).lower() else logger.warning
                    log_func(f"SSE connection lost: {e}, reconnecting in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
            
            except Exception as e:
                logger.error(f"Unexpected error in SSE listener: {e}", exc_info=True)
                if not stop_event.is_set():
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
    
    def _process_event(self, json_data: str):
        """
        Process a single SSE event.
        
        Event structure from Hue Bridge:
        [
            {
                "creationtime": "2024-01-01T12:00:00Z",
                "data": [
                    {
                        "id": "...",
                        "type": "light" | "grouped_light" | "scene",
                        "owner": {...},
                        ...
                    }
                ]
            }
        ]
        """
        try:
            event = json.loads(json_data)
            
            # Event is a list of objects with "data" fields
            if isinstance(event, list):
                for event_obj in event:
                    if isinstance(event_obj, dict) and 'data' in event_obj:
                        data_items = event_obj.get('data', [])
                        event_type = event_obj.get('type', 'unknown')
                        logger.debug(f"Processing SSE event type: {event_type} with {len(data_items)} items")
                        if event_type == 'update':
                            for item in data_items:
                                self._process_update(item)
                        elif event_type == 'add':
                            for item in data_items:
                                self._process_add(item)
                        elif event_type == 'delete':
                            for item in data_items:
                                self._process_delete(item)
                        elif event_type == 'error':
                            logger.error(f"Hue SSE error event: {data_items}")
            else:
                # Fallback for unexpected structure
                logger.warning(f"Unexpected SSE event structure: {type(event)}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse SSE event JSON: {e}")
        except Exception as e:
            logger.error(f"Error handling SSE event: {e}", exc_info=True)
    
    def _process_update(self, item: dict):
        """Process a single data item from SSE event."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            if not isinstance(item_id, str) or not item_id:
                logger.debug(f"Skipping update with invalid id for type={item_type}: {item_id}")
                return

            logger.debug(f"SSE event received: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_update(item_id, item)
            
            elif item_type == 'grouped_light':
                self._handle_grouped_light_update(item_id, item)
            
            elif item_type == 'scene':
                self._handle_scene_update(item_id, item)

            elif item_type == 'room':
                self._handle_room_update(item_id, item)
            
            elif item_type == 'zone':
                self._handle_zone_update(item_id, item)

            elif item_type == 'device':
                self._handle_device_update(item_id, item)
            
            else:
                # Ignore other types (button, etc.)
                logger.debug(f"Ignoring SSE event type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing item {item.get('id')}: {e}", exc_info=True)
    
    def _process_add(self, item: dict):
        """Process add events for new resources."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            if not isinstance(item_id, str) or not item_id:
                logger.debug(f"Skipping add with invalid id for type={item_type}: {item_id}")
                return

            logger.info(f"Addition event: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_update(item_id, item, is_addition=True)
            
            elif item_type == 'grouped_light':
                self._handle_grouped_light_update(item_id, item)
            
            elif item_type == 'scene':
                self._handle_scene_addition(item_id, item)
            
            elif item_type == 'room':
                self._handle_room_addition(item_id, item)
            
            elif item_type == 'zone':
                self._handle_zone_addition(item_id, item)

            elif item_type == 'device':
                self._handle_device_addition(item_id, item)
            
            else:
                logger.debug(f"Ignoring addition of type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing addition for {item.get('type')} {item.get('id')}: {e}", exc_info=True)
    
    def _process_delete(self, item: dict):
        """Process deletion events for any resource type."""
        try:
            item_type = item.get('type')
            item_id = item.get('id')

            if not isinstance(item_id, str) or not item_id:
                logger.debug(f"Skipping delete with invalid id for type={item_type}: {item_id}")
                return
            
            logger.debug(f"Deletion event: type={item_type}, id={item_id}")

            if item_type == 'light':
                self._handle_light_deletion(item_id)
            elif item_type == 'grouped_light':
                self._handle_grouped_light_deletion(item_id)
            elif item_type == 'scene':
                self._handle_scene_deletion(item_id)
            elif item_type == 'room':
                self._handle_room_deletion(item_id)
            elif item_type == 'zone':
                self._handle_zone_deletion(item_id)
            elif item_type == 'device':
                self._handle_device_deletion(item_id)
            else:
                logger.debug(f"Ignoring deletion of type: {item_type}")
        
        except Exception as e:
            logger.error(f"Error processing deletion for {item_type} {item_id}: {e}", exc_info=True)
    
    def _handle_scene_deletion(self, scene_id: str):
        """Handle scene deletion from SSE."""
        try:
            logger.info(f"Scene deletion detected: {scene_id}")
            
            hue_state_manager.remove_scene(scene_id)
            sync_device_configs_with_hue(self._hue_controller)
        
        except Exception as e:
            logger.error(f"Error handling scene deletion {scene_id}: {e}", exc_info=True)
    
    def _handle_light_deletion(self, light_id: str):
        """Handle light deletion from SSE."""
        try:
            logger.info(f"Light deletion detected: {light_id}")
            hue_state_manager.remove_light(light_id)
            # The owning room/zone children changed too - reconcile shortly after.
            self._schedule_topology_reconcile()
        
        except Exception as e:
            logger.error(f"Error handling light deletion {light_id}: {e}", exc_info=True)

    def _handle_device_addition(self, device_id: str, data: dict):
        """Handle a newly added Hue device (typically a brand new light)."""
        try:
            logger.info(f"Device addition detected: {device_id}")

            # Refresh the device map first so the new light resolves to its room.
            device_map, _ = self._get_device_maps(force_refresh=True)

            light_ids = self._device_ids_from_payload(data) or list(device_map.get(device_id, []))
            for light_id in light_ids:
                if hue_state_manager.get_light_state(light_id) is None:
                    self._handle_light_addition(light_id, {})

            self._schedule_topology_reconcile()

        except Exception as e:
            logger.error(f"Error handling device addition {device_id}: {e}", exc_info=True)

    def _handle_device_update(self, device_id: str, data: dict):
        """Handle device updates that may change its light services."""
        try:
            if 'services' not in data:
                # Renames/config only: nothing topology related for us.
                logger.debug(f"Ignoring non-topology device update: {device_id}")
                return

            logger.debug(f"Device service update: {device_id}")
            device_map, _ = self._get_device_maps(force_refresh=True)

            light_ids = self._device_ids_from_payload(data) or list(device_map.get(device_id, []))
            for light_id in light_ids:
                if hue_state_manager.get_light_state(light_id) is None:
                    self._handle_light_addition(light_id, {})

            self._schedule_topology_reconcile()

        except Exception as e:
            logger.error(f"Error handling device update {device_id}: {e}", exc_info=True)

    @staticmethod
    def _device_ids_from_payload(data: dict) -> List[str]:
        """Extract the light service ids from a device payload."""
        light_ids = []
        for service in data.get('services') or []:
            if not isinstance(service, dict) or service.get('rtype') != 'light':
                continue
            light_id = service.get('rid')
            if light_id:
                light_ids.append(light_id)
        return light_ids

    def _handle_device_deletion(self, device_id: str):
        """Handle device deletion from SSE."""
        try:
            logger.info(f"Device deletion detected: {device_id}")

            # Use the cached mapping before refreshing: the device is already
            # gone from the bridge so a fresh lookup would no longer contain it.
            with self._device_map_lock:
                light_ids = list(self._device_map.get(device_id, []))
            for light_id in light_ids:
                hue_state_manager.remove_light(light_id)

            self._get_device_maps(force_refresh=True)
            self._schedule_topology_reconcile()

        except Exception as e:
            logger.error(f"Error handling device deletion {device_id}: {e}", exc_info=True)
    
    def _handle_grouped_light_deletion(self, grouped_light_id: str):
        """Handle grouped_light deletion from SSE."""
        try:
            logger.info(f"Grouped light deletion detected: {grouped_light_id}")
            owner = hue_state_manager.get_grouped_light_owner(grouped_light_id)
            if owner:
                owner_type, owner_id = owner
                logger.debug(f"Grouped light {grouped_light_id} was for {owner_type} {owner_id}")
        
        except Exception as e:
            logger.error(f"Error handling grouped_light deletion {grouped_light_id}: {e}", exc_info=True)
    
    def _handle_room_deletion(self, room_id: str):
        """Handle room deletion from SSE."""
        try:
            logger.info(f"Room deletion detected: {room_id}")
            hue_state_manager.remove_room(room_id)
            sync_device_configs_with_hue(self._hue_controller)
        
        except Exception as e:
            logger.error(f"Error handling room deletion {room_id}: {e}", exc_info=True)
    
    def _handle_zone_deletion(self, zone_id: str):
        """Handle zone deletion from SSE."""
        try:
            logger.info(f"Zone deletion detected: {zone_id}")
            hue_state_manager.remove_zone(zone_id)
            sync_device_configs_with_hue(self._hue_controller)
        
        except Exception as e:
            logger.error(f"Error handling zone deletion {zone_id}: {e}", exc_info=True)
    
    def _handle_scene_addition(self, scene_id: str, data: dict):
        """Handle new scene added from SSE."""
        try:
            logger.info(f"Scene addition detected: {scene_id}")
            
            # Extract scene metadata
            scene_name = data.get('metadata', {}).get('name', 'Unknown')
            group_info = data.get('group', {})
            group_id = group_info.get('rid')
            group_type = group_info.get('rtype')
            if group_type not in ('room', 'zone') and group_id:
                # Fallback: infer group type from known state
                if hue_state_manager.get_room_state(group_id):
                    group_type = 'room'
                elif hue_state_manager.get_zone_state(group_id):
                    group_type = 'zone'
            
            # Register the new scene
            hue_state_manager.register_scene(scene_id, {
                'name': scene_name,
                'room_id': group_id,
                'group_type': group_type or 'room'
            })

            # If this scene references a group we have never seen, hydrate its metadata now.
            if group_id and self._hue_controller:
                group_known = (
                    hue_state_manager.get_room_state(group_id)
                    if group_type != 'zone'
                    else hue_state_manager.get_zone_state(group_id)
                )
                if not group_known:
                    self._upsert_group_state(
                        group_id,
                        group_type or 'room',
                        {},
                        event_type='scene_addition'
                    )

            sync_device_configs_with_hue(self._hue_controller)
            logger.info(f"Registered new scene {scene_id} ({scene_name}) for {group_type or 'room'} {group_id}")
        
        except Exception as e:
            logger.error(f"Error handling scene addition {scene_id}: {e}", exc_info=True)

    def _handle_room_update(self, room_id: str, data: dict):
        """Handle room updates from SSE."""
        try:
            changed = self._upsert_group_state(room_id, 'room', data, event_type='update')
            if changed:
                sync_device_configs_with_hue(self._hue_controller)
        except Exception as e:
            logger.error(f"Error handling room update {room_id}: {e}", exc_info=True)
    
    def _handle_zone_update(self, zone_id: str, data: dict):
        """Handle zone updates from SSE."""
        try:
            changed = self._upsert_group_state(zone_id, 'zone', data, event_type='update')
            if changed:
                sync_device_configs_with_hue(self._hue_controller)
        except Exception as e:
            logger.error(f"Error handling zone update {zone_id}: {e}", exc_info=True)

    def _upsert_group_state(self, group_id: str, group_type: str, data: dict, event_type: str = 'update') -> bool:
        """Create/update room or zone state from SSE payload with bridge fallback for partial payloads."""
        # The event payload is authoritative for the fields it carries; the
        # bridge lookup below only fills in what the event left out.
        group_name = (data.get('metadata') or {}).get('name')

        grouped_light_id = None
        for service in data.get('services', []):
            if service.get('rtype') == 'grouped_light':
                grouped_light_id = service.get('rid')
                break

        children = data.get('children')

        # SSE payloads can be partial right after creation/update.
        full_group = None
        if self._hue_controller:
            try:
                full_group = self._hue_controller.get_group(group_id, group_type)
            except Exception:
                logger.debug(f"Failed to fetch full {group_type} details for {group_id}", exc_info=True)

        if full_group:
            if not group_name:
                group_name = (full_group.get('metadata') or {}).get('name')
            if not grouped_light_id:
                for service in full_group.get('services', []):
                    if service.get('rtype') == 'grouped_light':
                        grouped_light_id = service.get('rid')
                        break
            if children is None:
                children = full_group.get('children')

        membership_changed = False
        if isinstance(children, list):
            membership_changed = self._sync_group_membership(group_id, group_type, children)
        elif event_type in ('add', 'grouped_light_discovery'):
            # We could not resolve the children yet, let a full reconciliation fix it.
            self._schedule_topology_reconcile()

        is_on = None
        if grouped_light_id and self._hue_controller:
            try:
                grouped_light = self._hue_controller.get_grouped_light(grouped_light_id)
                is_on = grouped_light.get('on', {}).get('on')
            except Exception:
                logger.debug(f"Failed to fetch grouped_light state for {group_type} {group_id}", exc_info=True)

        if group_type == 'zone':
            old_state = hue_state_manager.get_zone_state(group_id) or {}
            hue_state_manager.update_zone(
                zone_id=group_id,
                name=group_name,
                is_on=is_on,
                grouped_light_id=grouped_light_id
            )
            new_state = hue_state_manager.get_zone_state(group_id) or {}
        else:
            old_state = hue_state_manager.get_room_state(group_id) or {}
            hue_state_manager.update_room(
                room_id=group_id,
                name=group_name,
                is_on=is_on,
                grouped_light_id=grouped_light_id
            )
            new_state = hue_state_manager.get_room_state(group_id) or {}

        if not grouped_light_id:
            logger.debug(
                f"{group_type.capitalize()} {group_id} ({group_name}) upsert from {event_type} still has no grouped_light_id"
            )

        return (
            membership_changed
            or not old_state
            or old_state.get('name') != new_state.get('name')
            or old_state.get('grouped_light_id') != new_state.get('grouped_light_id')
            or (is_on is not None and old_state.get('is_on') != new_state.get('is_on'))
        )

    def _sync_group_membership(self, group_id: str, group_type: str, children: List[dict]) -> bool:
        """Translate room/zone children (devices) into light membership.

        Returns True when the membership changed locally.
        """
        device_ids: List[str] = []
        light_ids: List[str] = []
        seen = set()

        for child in children:
            if not isinstance(child, dict):
                continue
            child_id = child.get('rid')
            if not child_id:
                continue
            child_type = child.get('rtype')
            if child_type == 'device':
                device_ids.append(child_id)
            elif child_type == 'light':
                if child_id not in seen:
                    seen.add(child_id)
                    light_ids.append(child_id)

        if device_ids:
            device_map, _ = self._get_device_maps()
            unknown = [did for did in device_ids if did not in device_map]
            if unknown:
                device_map, _ = self._get_device_maps(force_refresh=True)
                unknown = [did for did in device_ids if did not in device_map]
            if unknown:
                # Never wipe membership because of an incomplete device map;
                # a debounced reconciliation will apply the real state.
                logger.debug(
                    f"Deferring {group_type} {group_id} membership sync, unknown devices: {unknown}"
                )
                self._schedule_topology_reconcile()
                return False

            for device_id in device_ids:
                for light_id in device_map.get(device_id, []):
                    if light_id not in seen:
                        seen.add(light_id)
                        light_ids.append(light_id)

        changed = hue_state_manager.sync_group_membership(
            group_id, group_type, light_ids, device_ids=device_ids
        )
        if changed:
            logger.info(f"{group_type.capitalize()} {group_id} light membership updated: {light_ids}")
        return changed
    
    def _upsert_room_state(self, room_id: str, data: dict, event_type: str = 'update') -> bool:
        """Create/update room state from SSE payload (room-specific wrapper)."""
        return self._upsert_group_state(room_id, 'room', data, event_type=event_type)
    
    def _handle_room_addition(self, room_id: str, data: dict):
        """Handle new room added from SSE."""
        try:
            logger.info(f"Room addition detected: {room_id}")

            self._upsert_group_state(room_id, 'room', data, event_type='add')
            self._schedule_topology_reconcile()
            sync_device_configs_with_hue(self._hue_controller)
            room_state = hue_state_manager.get_room_state(room_id) or {}
            room_name = room_state.get('name', 'Unknown')
            logger.info(f"Registered new room {room_id} ({room_name})")
        
        except Exception as e:
            logger.error(f"Error handling room addition {room_id}: {e}", exc_info=True)

    def _handle_zone_addition(self, zone_id: str, data: dict):
        """Handle new zone added from SSE."""
        try:
            logger.info(f"Zone addition detected: {zone_id}")

            self._upsert_group_state(zone_id, 'zone', data, event_type='add')
            self._schedule_topology_reconcile()
            sync_device_configs_with_hue(self._hue_controller)
            zone_state = hue_state_manager.get_zone_state(zone_id) or {}
            zone_name = zone_state.get('name', 'Unknown')
            logger.info(f"Registered new zone {zone_id} ({zone_name})")
        
        except Exception as e:
            logger.error(f"Error handling zone addition {zone_id}: {e}", exc_info=True)

    
    def _handle_light_update(self, light_id: str, data: dict, is_addition: bool = False):
        """Handle light state update from SSE."""
        try:
            known = hue_state_manager.get_light_state(light_id) is not None

            if is_addition or not known:
                # New (or previously missed) light: hydrate the full resource so
                # name, dimming/colour support and room assignment get populated.
                if self._hydrate_light(light_id, data):
                    return
                if not known and not data:
                    return

            # A known light without a room may have been added/moved while its
            # assignment failed earlier; retry from the local caches (no HTTP).
            light_state = hue_state_manager.get_light_state(light_id) or {}
            if not light_state.get('room_id'):
                room_id = self._resolve_light_room(
                    light_id, refresh_device_map=False, allow_bridge_lookup=False
                )
                if room_id:
                    logger.info(f"Re-assigning light {light_id} to room {room_id}")
                    hue_state_manager.assign_light_room(light_id, room_id)

            # Extract light state - only include fields that are present
            is_on = None
            brightness = None
            supports_dimming = None
            xy_color = None
            ct_color = None
            color_mode = None
            name = None
            on_data = data.get('on')
            if on_data is not None:
                is_on = on_data.get('on')

            dimming_data = data.get('dimming')
            if dimming_data is not None:
                brightness = dimming_data.get('brightness')
                supports_dimming = True

            metadata = data.get('metadata')
            if isinstance(metadata, dict):
                name = metadata.get('name')

            # Get XY color if present
            color_data = data.get('color')
            if color_data is not None:
                color_xy = color_data.get('xy')
                if color_xy and 'x' in color_xy and 'y' in color_xy:
                    xy_color = {'x': color_xy['x'], 'y': color_xy['y']}
                    color_mode = 'xy'
            
            # Get color temperature if present
            color_temp_data = data.get('color_temperature')
            if color_temp_data is not None:
                ct_value = color_temp_data.get('mirek')
                if ct_value:
                    ct_color = ct_value
                    color_mode = 'ct'
            
            # Only update if we have at least one field
            if is_on is not None or brightness is not None or xy_color is not None or ct_color is not None or name:
                hue_state_manager.update_light(
                    light_id=light_id,
                    is_on=is_on,
                    brightness=brightness,
                    name=name,
                    xy=xy_color,
                    ct=ct_color,
                    color_mode=color_mode,
                    supports_dimming=supports_dimming
                )
        
        except Exception as e:
            logger.error(f"Error processing light update {light_id}: {e}", exc_info=True)

    def _handle_light_addition(self, light_id: str, data: dict):
        """Handle a light that was just added on the bridge."""
        self._handle_light_update(light_id, data, is_addition=True)

    def _hydrate_light(self, light_id: str, data: Optional[dict]) -> bool:
        """Fetch and apply the complete state of a light.

        Used for newly added lights and for lights we somehow never tracked, so
        they never end up as a half-initialized "unknown" entry.

        Returns:
            True when the light state was applied
        """
        controller = self._hue_controller
        if not controller:
            return False

        fetched: dict = {}
        try:
            fetched = controller.get_light(light_id) or {}
        except Exception as e:
            logger.debug(f"Failed to fetch light {light_id} for hydration: {e}")

        if not fetched and not data:
            return False

        # The event payload wins over the fetched copy: it is more recent.
        merged = dict(fetched)
        for key, value in (data or {}).items():
            if value is not None:
                merged[key] = value

        supports_dimming = isinstance(merged.get('dimming'), dict)
        brightness = merged.get('dimming', {}).get('brightness') if supports_dimming else None
        is_on = (merged.get('on') or {}).get('on')
        name = (merged.get('metadata') or {}).get('name')

        model_map = self._light_model_map
        if light_id not in model_map:
            _, model_map = self._get_device_maps(force_refresh=True)
        model_id = model_map.get(light_id)

        xy_color = None
        ct_color = None
        color_mode = None
        xy = (merged.get('color') or {}).get('xy') or {}
        if xy and 'x' in xy and 'y' in xy:
            xy_color = {'x': xy['x'], 'y': xy['y']}
            color_mode = 'xy'
        ct_value = (merged.get('color_temperature') or {}).get('mirek')
        if ct_value:
            ct_color = ct_value
            color_mode = 'ct'

        room_id = self._resolve_light_room(light_id)

        hue_state_manager.update_light(
            light_id=light_id,
            is_on=is_on,
            brightness=brightness,
            room_id=room_id,
            name=name,
            model_id=model_id,
            xy=xy_color,
            ct=ct_color,
            color_mode=color_mode,
            supports_dimming=supports_dimming
        )
        logger.info(f"Hydrated light {light_id} ({name or 'unnamed'}) in {room_id or 'no'} room")
        return True

    def _resolve_light_room(self, light_id: str, refresh_device_map: bool = True,
                            allow_bridge_lookup: bool = True) -> Optional[str]:
        """Find the room that owns a light via the device -> room mapping.

        Args:
            light_id: Light to resolve
            refresh_device_map: Allow refreshing the device cache when unknown
            allow_bridge_lookup: Allow a bridge request when local state misses
        """
        if refresh_device_map:
            device_map, _ = self._get_device_maps()
        else:
            with self._device_map_lock:
                device_map = dict(self._device_map)

        device_id = next((did for did, lids in device_map.items() if light_id in lids), None)

        if device_id is None and refresh_device_map:
            # A brand new light may not be in the cached map yet.
            device_map, _ = self._get_device_maps(force_refresh=True)
            device_id = next((did for did, lids in device_map.items() if light_id in lids), None)

        if device_id is None:
            return None

        # Prefer local room state (kept in sync from the bridge children).
        for room_id, room_state in hue_state_manager.get_all_rooms().items():
            if device_id in (room_state.get('device_ids') or []):
                return room_id

        if not allow_bridge_lookup:
            return None

        # Fall back to a direct bridge lookup.
        controller = self._hue_controller
        if controller:
            try:
                for room in controller.get_rooms():
                    for child in room.get('children') or []:
                        if child.get('rtype') == 'device' and child.get('rid') == device_id:
                            return room.get('id')
            except Exception as e:
                logger.debug(f"Failed to resolve room for light {light_id}: {e}")

        return None
    
    def _handle_grouped_light_update(self, grouped_light_id: str, data: dict):
        """Handle room/zone state update from SSE."""
        try:
            # Get the actual owner (room or zone) from the grouped_light ID
            owner = hue_state_manager.get_grouped_light_owner(grouped_light_id)
            owner_id = None
            owner_type = None
            if owner:
                owner_type, owner_id = owner

            # If mapping is missing (common right after group creation), discover and hydrate it.
            if owner_id is None and self._hue_controller:
                try:
                    for group_type, groups in (('room', self._hue_controller.get_rooms()),
                                               ('zone', self._hue_controller.get_zones())):
                        if owner_id is not None:
                            break
                        for group in groups:
                            for service in group.get('services', []):
                                if service.get('rtype') == 'grouped_light' and service.get('rid') == grouped_light_id:
                                    discovered_id = group.get('id')
                                    if discovered_id:
                                        self._upsert_group_state(
                                            discovered_id,
                                            group_type,
                                            group,
                                            event_type='grouped_light_discovery'
                                        )
                                        owner_id = discovered_id
                                        owner_type = group_type
                                        logger.info(
                                            f"Resolved grouped_light {grouped_light_id} to {group_type} {owner_id} via bridge lookup"
                                        )
                                    break
                            if owner_id is not None:
                                break
                except Exception:
                    logger.debug(
                        f"Failed to resolve grouped_light mapping for {grouped_light_id}",
                        exc_info=True
                    )
            
            if owner_id is None:
                return
            
            # Extract group state - only include fields that are present
            is_on = None
            brightness = None
            
            on_data = data.get('on')
            if on_data is not None:
                is_on = on_data.get('on')
            
            dimming_data = data.get('dimming')
            if dimming_data is not None:
                brightness = dimming_data.get('brightness')
            
            # Only update if we have at least one field
            if is_on is not None or brightness is not None:
                if owner_type == 'zone':
                    hue_state_manager.update_zone(
                        zone_id=owner_id,
                        is_on=is_on,
                        brightness=brightness
                    )
                else:
                    hue_state_manager.update_room(
                        room_id=owner_id,
                        is_on=is_on,
                        brightness=brightness
                    )
        
        except Exception as e:
            logger.error(f"Error processing grouped_light update {grouped_light_id}: {e}", exc_info=True)
    
    def _handle_scene_update(self, scene_id: str, data: dict):
        """Handle scene activation from SSE."""
        try:
            # Check if scene is becoming active or inactive
            status = data.get('status', {})
            active_status = status.get('active')
            
            logger.info(f"Scene {scene_id} status: {active_status}")
            
            # Handle scene becoming inactive
            if active_status == 'inactive':
                # Only clear the scene if this is the currently active scene in its group
                scene_info = hue_state_manager.get_scene_info(scene_id)
                if scene_info:
                    group_id = scene_info.get('room_id')
                    group_type = scene_info.get('group_type', 'room')
                    if group_id:
                        if group_type == 'zone':
                            zone_state = hue_state_manager.get_zone_state(group_id)
                            if zone_state and zone_state.get('current_scene_id') == scene_id:
                                hue_state_manager.set_zone_scene(group_id, None)
                                logger.info(f"Cleared scene {scene_id} from zone {group_id} (became inactive)")
                        else:
                            room_state = hue_state_manager.get_room_state(group_id)
                            if room_state and room_state.get('current_scene_id') == scene_id:
                                hue_state_manager.set_room_scene(group_id, None)
                                logger.info(f"Cleared scene {scene_id} from room {group_id} (became inactive)")
                return

            if active_status is None:
                logger.debug(f"Ignoring scene {scene_id} update without status")
                return

            # First, try to get the owning group from stored scene info
            scene_info = hue_state_manager.get_scene_info(scene_id)
            group_id = None
            group_type = None
            
            if scene_info:
                group_id = scene_info.get('room_id')
                group_type = scene_info.get('group_type', 'room')
                logger.debug(f"Found group_id {group_id} ({group_type}) from scene registry")
            
            # If we have a group, update its active scene
            if group_id:
                if group_type == 'zone':
                    hue_state_manager.set_zone_scene(
                        zone_id=group_id,
                        scene_id=scene_id
                    )
                else:
                    hue_state_manager.set_room_scene(
                        room_id=group_id,
                        scene_id=scene_id
                    )
                logger.info(f"Set scene {scene_id} as active for {group_type} {group_id}")
            else:
                logger.warning(f"Could not determine owning group for scene {scene_id}")
        
        except Exception as e:
            logger.error(f"Error processing scene update {scene_id}: {e}", exc_info=True)


# Global singleton instance
hue_sse_listener = HueSSEListener()
