from flask import Blueprint, jsonify, request
from HueConfig import getDevices, getRoomFromMacAddress, getScenesFromMacAddress, getNameFromMacAddress, getRooms, getScenes, confirmConfig

apiView = Blueprint('apiView', __name__)

@apiView.route("/")
def api():
    return jsonify({"status": True, "message": "HueMixLink, v1.0"})

@apiView.route("/buttons")
def buttons():
    devices = {}
    for device in getDevices().values():
        print(device)
        name, macAddress = device.split(" - ") 
        roomNum = getRoomFromMacAddress(macAddress)
        scene_names, scene_ids = getScenesFromMacAddress(macAddress)
        devices[macAddress] = {"name": name, "room": roomNum, "scene_names": scene_names, "scene_ids": scene_ids}
    return jsonify(devices)

@apiView.route("/button/<macAddress>")
def button(macAddress):
    name = getNameFromMacAddress(macAddress)
    roomNum = getRoomFromMacAddress(macAddress)
    scene_names, scene_ids = getScenesFromMacAddress(macAddress)
    device = {"name": name, "room": roomNum, "scene_names": scene_names, "scene_ids": scene_ids}
    return jsonify(device)

@apiView.route("/rooms")
def rooms():
    _, rooms = getRooms()
    return jsonify(rooms)


@apiView.route("/room/<name>")
def room(name):
    _, rooms = getRooms()
    return jsonify(rooms[name])


@apiView.route("/scenes")
def scenes():
    _, rooms = getRooms()
    scenes = {}
    for room in rooms:
        name, id = getScenes(rooms[room])
        scenes[room] = id
    return jsonify(scenes)


@apiView.route("/scene/<room>")
def scene(room):
    _, rooms = getRooms()
    name, id = getScenes(rooms[room])
    return jsonify(id)

@apiView.route("/button/<macAddress>", methods=['PUT'])
def update_button(macAddress):
    if macAddress in [device.split(" - ") for device in getDevices().values()]:
        return jsonify({"status": False, "message": "Button not found"}), 404
    data = request.get_json()
    if not "room" in data or data["room"] not in getRooms()[1].values():
        return jsonify({"status": False, "message": "Room not found"}), 400
    if not "scene_names" in data or not "scene_ids" in data:
        return jsonify({"status": False, "message": "Scenes not found"}), 400
    scenes = getScenes(data["room"])[1]
    if not all(scene in scenes.keys() for scene in data["scene_names"]) or not all(scene in scenes.values() for scene in data["scene_ids"]):
        return jsonify({"status": False, "message": "Invalid scene"}), 400
    if not all(item in scenes.items() for item in dict(zip(data["scene_names"], data["scene_ids"])).items()):
        return jsonify({"status": False, "message": "Invalid scene name or id"}), 400
    confirmConfig(macAddress, data["room"], data["scene_names"], data["scene_ids"])
    return jsonify({"status": True, "message": "Button information updated successfully"})