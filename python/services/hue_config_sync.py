"""Synchronize local automation configs with current Hue rooms/zones/scenes."""

import logging
from typing import Dict, Optional, Tuple

from constants import FILE_BUTTONS, FILE_MOTION_SENSORS, FILE_DOOR_SENSORS
from services import data_manager
from services.hue_service import hue_service

logger = logging.getLogger(__name__)


def _normalize_group_name(name: Optional[str]) -> str:
    return (name or "").strip().lower()


def _build_name_map(group_id_to_name: Dict[str, str]) -> Dict[str, str]:
    name_to_id: Dict[str, str] = {}
    for gid, gname in group_id_to_name.items():
        if not gid:
            continue
        key = _normalize_group_name(gname)
        if key and key not in name_to_id:
            name_to_id[key] = gid
    return name_to_id


def _resolve_group_id(
    config: Dict,
    valid_room_ids: set,
    room_name_to_id: Dict[str, str],
    valid_zone_ids: set,
    zone_name_to_id: Dict[str, str],
) -> Tuple[Optional[str], str]:
    """Resolve (target_id, target_type) for a config.

    Supports the generic target format (target_id/target_type) with a legacy
    fallback to room_id/room_name (treated as a room target). Name-based
    resolution only matches within the config's own group type.
    """
    target_type = config.get('target_type', 'room')
    if target_type not in ('room', 'zone'):
        target_type = 'room'

    target_id = config.get('target_id') or config.get('room_id')
    target_name = config.get('room_name')

    if target_type == 'zone':
        if target_id and target_id in valid_zone_ids:
            return target_id, 'zone'
        normalized = _normalize_group_name(target_name)
        if normalized and normalized in zone_name_to_id:
            return zone_name_to_id[normalized], 'zone'
        return None, 'zone'

    if target_id and target_id in valid_room_ids:
        return target_id, 'room'
    normalized = _normalize_group_name(target_name)
    if normalized and normalized in room_name_to_id:
        return room_name_to_id[normalized], 'room'
    return None, 'room'


def _apply_resolution(config: Dict, resolved_id: Optional[str], resolved_type: str,
                      group_id_to_name: Dict[str, str]) -> bool:
    """Write resolved target into config (new + legacy fields). Returns True if changed."""
    changed = False
    current_id = config.get('target_id') or config.get('room_id')
    current_type = config.get('target_type', 'room')

    if resolved_id != current_id:
        config['target_id'] = resolved_id
        config['room_id'] = resolved_id
        changed = True

    if resolved_type != current_type:
        config['target_type'] = resolved_type
        changed = True

    if resolved_id:
        group_name = group_id_to_name.get(resolved_id)
        if group_name and config.get('room_name') != group_name:
            config['room_name'] = group_name
            changed = True

    return changed


def _filter_scene_ids(scene_ids, valid_scene_ids: set, scene_to_group: Dict[str, Tuple[Optional[str], Optional[str]]],
                      group_id: Optional[str], group_type: str):
    filtered = []
    for scene_id in scene_ids or []:
        if scene_id not in valid_scene_ids:
            continue
        if group_id:
            scene_group_id, scene_group_type = scene_to_group.get(scene_id, (None, None))
            if scene_group_id not in (None, group_id):
                continue
            if scene_group_type not in (None, group_type):
                continue
        filtered.append(scene_id)
    return filtered


def sync_device_configs_with_hue(hue_controller=None) -> Dict[str, int]:
    """Reconcile stored button/motion/door configs with current Hue room/zone/scene topology."""
    hue = hue_controller or hue_service.get_controller()
    stats = {
        "buttons_updated": 0,
        "motions_updated": 0,
        "doors_updated": 0,
        "remapped_rooms": 0,
        "removed_scene_refs": 0,
        "disabled_configs": 0,
    }

    if not hue:
        logger.info("Hue not configured, skipping config sync")
        return stats

    try:
        rooms = hue.get_rooms()
        zones = hue.get_zones()
        scenes = hue.get_scenes()
    except Exception as exc:
        logger.error("Failed fetching Hue rooms/zones/scenes during config sync: %s", exc, exc_info=True)
        return stats

    room_id_to_name = {room.get("id"): room.get("metadata", {}).get("name", "") for room in rooms}
    valid_room_ids = set(room_id_to_name)
    room_name_to_id = _build_name_map(room_id_to_name)

    zone_id_to_name = {zone.get("id"): zone.get("metadata", {}).get("name", "") for zone in zones}
    valid_zone_ids = set(zone_id_to_name)
    zone_name_to_id = _build_name_map(zone_id_to_name)

    group_id_to_name = dict(room_id_to_name)
    group_id_to_name.update(zone_id_to_name)

    valid_scene_ids = set()
    scene_to_group: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    scene_id_to_name: Dict[str, str] = {}
    for scene in scenes:
        sid = scene.get("id")
        if not sid:
            continue
        valid_scene_ids.add(sid)
        scene_group = scene.get("group", {})
        scene_to_group[sid] = (scene_group.get("rid"), scene_group.get("rtype"))
        scene_id_to_name[sid] = scene.get("metadata", {}).get("name", "")

    buttons = data_manager.read_json(FILE_BUTTONS, default=[])
    buttons_changed = False

    for device in buttons:
        config = device.get("config")
        if not device.get("configured") or not isinstance(config, dict):
            continue

        device_changed = False

        if config.get("device_type") == "remote" and isinstance(config.get("buttons"), list):
            for btn_cfg in config.get("buttons", []):
                if not isinstance(btn_cfg, dict):
                    continue

                resolved_id, resolved_type = _resolve_group_id(
                    btn_cfg,
                    valid_room_ids,
                    room_name_to_id,
                    valid_zone_ids,
                    zone_name_to_id,
                )
                if _apply_resolution(btn_cfg, resolved_id, resolved_type, group_id_to_name):
                    if resolved_id and (btn_cfg.get("room_id") or btn_cfg.get("target_id")):
                        stats["remapped_rooms"] += 1
                    device_changed = True

                old_scenes = list(btn_cfg.get("scenes", []))
                new_scenes = _filter_scene_ids(old_scenes, valid_scene_ids, scene_to_group, resolved_id, resolved_type)
                if old_scenes != new_scenes:
                    stats["removed_scene_refs"] += len(set(old_scenes) - set(new_scenes))
                    btn_cfg["scenes"] = new_scenes
                    device_changed = True

        else:
            resolved_id, resolved_type = _resolve_group_id(
                config,
                valid_room_ids,
                room_name_to_id,
                valid_zone_ids,
                zone_name_to_id,
            )
            if _apply_resolution(config, resolved_id, resolved_type, group_id_to_name):
                if resolved_id and (config.get("room_id") or config.get("target_id")):
                    stats["remapped_rooms"] += 1
                device_changed = True

            old_scenes = list(config.get("scenes", []))
            new_scenes = _filter_scene_ids(old_scenes, valid_scene_ids, scene_to_group, resolved_id, resolved_type)
            if old_scenes != new_scenes:
                stats["removed_scene_refs"] += len(set(old_scenes) - set(new_scenes))
                config["scenes"] = new_scenes
                device_changed = True

            if not resolved_id or not new_scenes:
                if device.get("configured"):
                    device["configured"] = False
                    device["config"] = None
                    stats["disabled_configs"] += 1
                    device_changed = True

        if device_changed:
            stats["buttons_updated"] += 1
            buttons_changed = True

    if buttons_changed:
        data_manager.write_json(FILE_BUTTONS, buttons)

    motions = data_manager.read_json(FILE_MOTION_SENSORS, default=[])
    motions_changed = False

    for sensor in motions:
        config = sensor.get("config")
        if not isinstance(config, dict):
            continue

        sensor_changed = False

        resolved_id, resolved_type = _resolve_group_id(
            config,
            valid_room_ids,
            room_name_to_id,
            valid_zone_ids,
            zone_name_to_id,
        )
        if _apply_resolution(config, resolved_id, resolved_type, group_id_to_name):
            if resolved_id and (config.get("room_id") or config.get("target_id")):
                stats["remapped_rooms"] += 1
            sensor_changed = True

        slots = config.get("time_slots", [])
        if isinstance(slots, list):
            for slot in slots:
                if not isinstance(slot, dict):
                    continue

                if slot.get("motion_action") == "scene":
                    scene_id = slot.get("scene_id")
                    if scene_id and scene_id not in valid_scene_ids:
                        slot["scene_id"] = ""
                        slot["scene_name"] = ""
                        slot["motion_action"] = "nothing"
                        stats["removed_scene_refs"] += 1
                        sensor_changed = True
                    elif scene_id and resolved_id and scene_to_group.get(scene_id) not in (None, (resolved_id, resolved_type)):
                        slot["scene_id"] = ""
                        slot["scene_name"] = ""
                        slot["motion_action"] = "nothing"
                        stats["removed_scene_refs"] += 1
                        sensor_changed = True

                if slot.get("after_action") == "scene":
                    after_scene_id = slot.get("after_scene_id")
                    if after_scene_id and after_scene_id not in valid_scene_ids:
                        slot["after_scene_id"] = ""
                        slot["after_scene_name"] = ""
                        slot["after_action"] = "nothing"
                        stats["removed_scene_refs"] += 1
                        sensor_changed = True
                    elif after_scene_id and resolved_id and scene_to_group.get(after_scene_id) not in (None, (resolved_id, resolved_type)):
                        slot["after_scene_id"] = ""
                        slot["after_scene_name"] = ""
                        slot["after_action"] = "nothing"
                        stats["removed_scene_refs"] += 1
                        sensor_changed = True

        if not resolved_id and sensor.get("configured"):
            sensor["configured"] = False
            stats["disabled_configs"] += 1
            sensor_changed = True

        if sensor_changed:
            stats["motions_updated"] += 1
            motions_changed = True

    if motions_changed:
        data_manager.write_json(FILE_MOTION_SENSORS, motions)

    doors = data_manager.read_json(FILE_DOOR_SENSORS, default=[])
    doors_changed = False

    for sensor in doors:
        config = sensor.get("config")
        if not isinstance(config, dict):
            continue

        sensor_changed = False

        resolved_id, resolved_type = _resolve_group_id(
            config,
            valid_room_ids,
            room_name_to_id,
            valid_zone_ids,
            zone_name_to_id,
        )
        if _apply_resolution(config, resolved_id, resolved_type, group_id_to_name):
            if resolved_id and (config.get("room_id") or config.get("target_id")):
                stats["remapped_rooms"] += 1
            sensor_changed = True

        slots = config.get("time_slots", [])
        if isinstance(slots, list):
            for slot in slots:
                if not isinstance(slot, dict):
                    continue

                if slot.get("open_action") == "scene":
                    open_scene_id = slot.get("open_scene_id")
                    scene_invalid = (
                        not open_scene_id
                        or open_scene_id not in valid_scene_ids
                        or (
                            resolved_id
                            and scene_to_group.get(open_scene_id) not in (None, (resolved_id, resolved_type))
                        )
                    )
                    if scene_invalid:
                        slot["open_scene_id"] = ""
                        slot["open_scene_name"] = ""
                        slot["open_action"] = "nothing"
                        if open_scene_id:
                            stats["removed_scene_refs"] += 1
                        sensor_changed = True
                    else:
                        current_name = slot.get("open_scene_name") or ""
                        expected_name = scene_id_to_name.get(open_scene_id, "")
                        if expected_name and current_name != expected_name:
                            slot["open_scene_name"] = expected_name
                            sensor_changed = True

                if slot.get("close_action") == "scene":
                    close_scene_id = slot.get("close_scene_id")
                    scene_invalid = (
                        not close_scene_id
                        or close_scene_id not in valid_scene_ids
                        or (
                            resolved_id
                            and scene_to_group.get(close_scene_id) not in (None, (resolved_id, resolved_type))
                        )
                    )
                    if scene_invalid:
                        slot["close_scene_id"] = ""
                        slot["close_scene_name"] = ""
                        slot["close_action"] = "nothing"
                        if close_scene_id:
                            stats["removed_scene_refs"] += 1
                        sensor_changed = True
                    else:
                        current_name = slot.get("close_scene_name") or ""
                        expected_name = scene_id_to_name.get(close_scene_id, "")
                        if expected_name and current_name != expected_name:
                            slot["close_scene_name"] = expected_name
                            sensor_changed = True

        if not resolved_id and sensor.get("configured"):
            sensor["configured"] = False
            stats["disabled_configs"] += 1
            sensor_changed = True

        if sensor_changed:
            stats["doors_updated"] += 1
            doors_changed = True

    if doors_changed:
        data_manager.write_json(FILE_DOOR_SENSORS, doors)

    if any(stats.values()):
        logger.info("Hue config sync completed: %s", stats)
    else:
        logger.debug("Hue config sync completed with no changes")

    return stats