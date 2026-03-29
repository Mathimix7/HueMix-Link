"""Synchronize local automation configs with current Hue rooms/scenes."""

import logging
from typing import Dict, Optional

from constants import FILE_BUTTONS, FILE_MOTION_SENSORS, FILE_DOOR_SENSORS
from services import data_manager
from services.hue_service import hue_service

logger = logging.getLogger(__name__)


def _normalize_room_name(name: Optional[str]) -> str:
    return (name or "").strip().lower()


def _resolve_room_id(
    room_id: Optional[str],
    room_name: Optional[str],
    valid_room_ids: set,
    room_name_to_id: Dict[str, str],
) -> Optional[str]:
    if room_id and room_id in valid_room_ids:
        return room_id

    normalized = _normalize_room_name(room_name)
    if normalized and normalized in room_name_to_id:
        return room_name_to_id[normalized]

    return None


def _filter_scene_ids(scene_ids, valid_scene_ids: set, scene_to_room: Dict[str, Optional[str]], room_id: Optional[str]):
    filtered = []
    for scene_id in scene_ids or []:
        if scene_id not in valid_scene_ids:
            continue
        if room_id and scene_to_room.get(scene_id) not in (None, room_id):
            continue
        filtered.append(scene_id)
    return filtered


def sync_device_configs_with_hue(hue_controller=None) -> Dict[str, int]:
    """Reconcile stored button/motion/door configs with current Hue room/scene topology."""
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
        scenes = hue.get_scenes()
    except Exception as exc:
        logger.error("Failed fetching Hue rooms/scenes during config sync: %s", exc, exc_info=True)
        return stats

    room_id_to_name = {room.get("id"): room.get("metadata", {}).get("name", "") for room in rooms}
    valid_room_ids = set(room_id_to_name)

    room_name_to_id: Dict[str, str] = {}
    for rid, rname in room_id_to_name.items():
        if not rid:
            continue
        key = _normalize_room_name(rname)
        if key and key not in room_name_to_id:
            room_name_to_id[key] = rid

    valid_scene_ids = set()
    scene_to_room: Dict[str, Optional[str]] = {}
    scene_id_to_name: Dict[str, str] = {}
    for scene in scenes:
        sid = scene.get("id")
        if not sid:
            continue
        valid_scene_ids.add(sid)
        scene_to_room[sid] = scene.get("group", {}).get("rid")
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

                resolved_room_id = _resolve_room_id(
                    btn_cfg.get("room_id"),
                    btn_cfg.get("room_name"),
                    valid_room_ids,
                    room_name_to_id,
                )
                if resolved_room_id != btn_cfg.get("room_id"):
                    if resolved_room_id and btn_cfg.get("room_id"):
                        stats["remapped_rooms"] += 1
                    btn_cfg["room_id"] = resolved_room_id
                    device_changed = True

                if resolved_room_id:
                    room_name = room_id_to_name.get(resolved_room_id)
                    if room_name and btn_cfg.get("room_name") != room_name:
                        btn_cfg["room_name"] = room_name
                        device_changed = True

                old_scenes = list(btn_cfg.get("scenes", []))
                new_scenes = _filter_scene_ids(old_scenes, valid_scene_ids, scene_to_room, resolved_room_id)
                if old_scenes != new_scenes:
                    stats["removed_scene_refs"] += len(set(old_scenes) - set(new_scenes))
                    btn_cfg["scenes"] = new_scenes
                    device_changed = True

        else:
            resolved_room_id = _resolve_room_id(
                config.get("room_id"),
                config.get("room_name"),
                valid_room_ids,
                room_name_to_id,
            )
            if resolved_room_id != config.get("room_id"):
                if resolved_room_id and config.get("room_id"):
                    stats["remapped_rooms"] += 1
                config["room_id"] = resolved_room_id
                device_changed = True

            if resolved_room_id:
                room_name = room_id_to_name.get(resolved_room_id)
                if room_name and config.get("room_name") != room_name:
                    config["room_name"] = room_name
                    device_changed = True

            old_scenes = list(config.get("scenes", []))
            new_scenes = _filter_scene_ids(old_scenes, valid_scene_ids, scene_to_room, resolved_room_id)
            if old_scenes != new_scenes:
                stats["removed_scene_refs"] += len(set(old_scenes) - set(new_scenes))
                config["scenes"] = new_scenes
                device_changed = True

            if not resolved_room_id or not new_scenes:
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

        resolved_room_id = _resolve_room_id(
            config.get("room_id"),
            config.get("room_name"),
            valid_room_ids,
            room_name_to_id,
        )
        if resolved_room_id != config.get("room_id"):
            if resolved_room_id and config.get("room_id"):
                stats["remapped_rooms"] += 1
            config["room_id"] = resolved_room_id
            sensor_changed = True

        if resolved_room_id:
            room_name = room_id_to_name.get(resolved_room_id)
            if room_name and config.get("room_name") != room_name:
                config["room_name"] = room_name
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
                    elif scene_id and resolved_room_id and scene_to_room.get(scene_id) not in (None, resolved_room_id):
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
                    elif after_scene_id and resolved_room_id and scene_to_room.get(after_scene_id) not in (None, resolved_room_id):
                        slot["after_scene_id"] = ""
                        slot["after_scene_name"] = ""
                        slot["after_action"] = "nothing"
                        stats["removed_scene_refs"] += 1
                        sensor_changed = True

        if not resolved_room_id and sensor.get("configured"):
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

        resolved_room_id = _resolve_room_id(
            config.get("room_id"),
            config.get("room_name"),
            valid_room_ids,
            room_name_to_id,
        )
        if resolved_room_id != config.get("room_id"):
            if resolved_room_id and config.get("room_id"):
                stats["remapped_rooms"] += 1
            config["room_id"] = resolved_room_id
            sensor_changed = True

        if resolved_room_id:
            room_name = room_id_to_name.get(resolved_room_id)
            if room_name and config.get("room_name") != room_name:
                config["room_name"] = room_name
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
                            resolved_room_id
                            and scene_to_room.get(open_scene_id) not in (None, resolved_room_id)
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
                            resolved_room_id
                            and scene_to_room.get(close_scene_id) not in (None, resolved_room_id)
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

        if not resolved_room_id and sensor.get("configured"):
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
