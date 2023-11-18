from flask import Blueprint, jsonify, request
from HueConfig import getDevices, getRoomFromMacAddress, getScenesFromMacAddress, getNameFromMacAddress, getRooms, getScenes, confirmConfig, getServers, getServerData, getServerStatus, serversWriteNewName, writeNewName, removeServer, removeButton
import requests

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
        devices[macAddress] = {"macAddress": macAddress, "name": name, "room": roomNum, "scene_names": scene_names, "scene_ids": scene_ids}
    return jsonify(devices)

@apiView.route("/button/<macAddress>")
def button(macAddress):
    name = getNameFromMacAddress(macAddress)
    if not name:
        return jsonify({"error": "Button not found"}), 400
    roomNum = getRoomFromMacAddress(macAddress)
    scene_names, scene_ids = getScenesFromMacAddress(macAddress)
    device = {"macAddress": macAddress, "name": name, "room": roomNum, "scene_names": scene_names, "scene_ids": scene_ids}
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


@apiView.route("/rename/button/<macAddress>", methods=["POST"])
def rename_button(macAddress):
    name = getNameFromMacAddress(macAddress)
    if not name:
        return jsonify({"error": "Button not found"}), 400
    data = request.get_json()
    name = data.get('name', '')
    if len(name) <= 0:
        return jsonify({"error": "Name is too short"}), 400
    if '- ' in name:
        return jsonify({"error": "Invalid name"}), 400
    writeNewName("- "+macAddress, name)
    return jsonify({"sucess": "name updated"})


@apiView.route("/delete/button/<macAddress>", methods=["POST"])
def delete_button(macAddress):
    name = getNameFromMacAddress(macAddress)
    if not name:
        return jsonify({"error": "Button not found"}), 400
    removeButton("- "+macAddress)
    return jsonify({"sucess": "Button deleted"})


@apiView.route("/servers")
def servers():
    devices = {}
    for device in getServers().values():
        name, macAddress = device.split(" - ") 
        macAddress = macAddress.split(" ", 1)[0]
        data = getServerData(macAddress)
        devices[macAddress] = {"macAddress": macAddress, "name": name, 'ip': data["ip"], 'lastUsed': data["lastUsed"]}
    return jsonify(devices)


@apiView.route("/server/<macAddress>")
def server(macAddress):
    data = getServerData(macAddress)
    if not data:
        return jsonify({"error": "Server not found"}), 400
    status = getServerStatus(data["ip"])
    print("asd", status)
    if status["status"] == "Offline":
        device =  {"macAddress": macAddress, "name": data["deviceName"], 'ip': data["ip"], 'lastUsed': data["lastUsed"], "status": "Offline"}
    else:
        device =  {"macAddress": macAddress, "name": data["deviceName"], 'ip': data["ip"], 'lastUsed': data["lastUsed"], "status": "Online", "uptime": status["uptime"], "TCP_port": status["port"], "led_on_time": status["led_on_time"], "led_off_time": status["led_off_time"]}
    return jsonify(device)


@apiView.route("/server/led/<macAddress>", methods=["POST"])
def server_led(macAddress):
    data = getServerData(macAddress)
    if not data:
        return jsonify({"error": "Server not found"}), 400
    status = getServerStatus(data["ip"])
    if status["status"] == "Offline":
        return jsonify({"error": "Server offline"}), 400
    
    json = request.get_json()
    led_on = json.get('led_on', '')
    led_off = json.get('led_off', '')
    if not led_on.isnumeric() or not led_off.isnumeric():
        return jsonify({"error": "Invalid arguments"}), 400
    try:
        response = requests.get(f"http://{data['ip']}/led_off_times?led_on_time={led_on}&led_off_time={led_off}")
        if response.text == "OK":
           return jsonify({"sucess": "Leds times updated"}) 
        return jsonify({"error": "Server error"}), 400
    except:
        return jsonify({"error": "Server error"}), 400
    

@apiView.route("/server/port/<macAddress>", methods=["POST"])
def server_port(macAddress):
    data = getServerData(macAddress)
    if not data:
        return jsonify({"error": "Server not found"}), 400
    status = getServerStatus(data["ip"])
    if status["status"] == "Offline":
        return jsonify({"error": "Server offline"}), 400
    
    json = request.get_json()
    new_port = json.get('port', '')

    if not new_port.isnumeric() or not 0 <= int(new_port) <= 65535:
        return jsonify({"error": "Invalid arguments"}), 400
    try:
        response = requests.get(f"http://{data['ip']}/new_port?port={new_port}")
        if response.text == "OK":
           return jsonify({"sucess": "Hue Mix Link port updated"}) 
        return jsonify({"error": "Server error"}), 400
    except:
        return jsonify({"error": "Server error"}), 400


@apiView.route("/rename/server/<macAddress>", methods=["POST"])
def rename_server(macAddress):
    data = getServerData(macAddress)
    if not data:
        return jsonify({"error": "Server not found"}), 400
    data = request.get_json()
    name = data.get('name', '')
    if len(name) <= 0:
        return jsonify({"error": "Name is too short"}), 400
    if '- ' in name:
        return jsonify({"error": "Invalid name"}), 400
    serversWriteNewName("- "+macAddress+" ", name)
    return jsonify({"sucess": "name updated"})


@apiView.route("/delete/server/<macAddress>", methods=["POST"])
def delete_server(macAddress):
    data = getServerData(macAddress)
    if not data:
        return jsonify({"error": "Server not found"}), 400
    removeServer("- "+macAddress+" ")
    return jsonify({"sucess": "Server deleted"})
