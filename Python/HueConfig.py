import requests
import json
import os
import datetime
import socket

PATH = os.path.join(os.getcwd()) + "/DataFiles/"

def updateAPI(updateTCP=True):
    global API_ENDPOINT, GROUP_ENDPOINT, SCENES_ENDPOINT, APISTATUS
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        settings = json.load(f)
    if settings['hue_bridge_ip']:
        APISTATUS = True
    else:
        APISTATUS = False
    API_ENDPOINT = f"http://{settings['hue_bridge_ip']}/api/{settings['hue_bridge_token']}"
    GROUP_ENDPOINT = API_ENDPOINT + "/groups"
    SCENES_ENDPOINT = API_ENDPOINT + "/scenes"
    try:
        if updateTCP:
            s = socket.socket()
            s.connect(("127.0.0.1", settings["tcp_port"]))
            s.send("updateBridge,-p".encode())
            s.close()
    except: pass

updateAPI(False)

def getWebsitePort():
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        data = json.load(f)
    return data["website_port"]

def hueServer():
    return APISTATUS

def getPorts():
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        settings = json.load(f)
    return settings["tcp_port"], settings["website_port"]  

def savePorts(tcp, website):
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        settings = json.load(f)
    oldPort = settings["tcp_port"] 
    settings["tcp_port"] = int(tcp)
    settings["website_port"] = int(website)
    with open(os.path.join(PATH, "settings.json"), "w") as f:
        json.dump(settings, f)
    try:
        s = socket.socket()
        s.connect(("127.0.0.1", oldPort))
        s.send("resetPort,-p".encode())
        s.close()
    except: pass

def discoverBridge():
    HEADER = b"""M-SEARCH * HTTP/1.1\r
    HOST: 239.255.255.250:1900\r
    MAN: "ssdp:discover"\r
    ST: ssdp:all\r
    MX: 3\r
    \r
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(HEADER, ('239.255.255.250',1900))
    s.settimeout(3)
    IP = None
    while IP == None:
        data, addr = s.recvfrom(1024)
        lines = data.split(b'\r\n')
        for l in lines:
            tokens = l.split(b' ')
            if tokens[0] == b'SERVER:':
                product = tokens[3].split(b'/')
                if product[0] == b'IpBridge':
                    IP = str(addr[0])
                    break
    s.close()
    return IP

def format_duration(seconds):
    duration = datetime.timedelta(seconds=seconds)
    days = duration.days
    hours, remainder = divmod(duration.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} {'day' if days == 1 else 'days'}")
    if hours > 0:
        parts.append(f"{hours} {'hour' if hours == 1 else 'hours'}")
    if minutes > 0:
        parts.append(f"{minutes} {'minute' if minutes == 1 else 'minutes'}")
    
    if len(parts) == 0:
        return "just now"
    elif len(parts) == 1:
        return f"{parts[0]} ago"
    else:
        return f"{', '.join(parts[:-1])} and {parts[-1]} ago"

def getServerData(macAddress):
    with open(os.path.join(PATH, "servers.json"), "r") as f:
        servers = json.load(f)
    for data in servers:
        if data["macAddress"].lower() == macAddress.lower():
            return data

def getServerStatus(ip):
    try:
        data = requests.get(f"http://{ip}/status", timeout=5)
    except:
        return {"status": "Offline"}
    if data.status_code == 200:
        data = data.json()
        data.update({"status":f"Online ({format_duration(data['uptime'])})"})
        return data
    else:
        return {"status": "Offline"}

def saveBridge(ip, token):
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        data = json.load(f)
    data["hue_bridge_ip"] = ip
    data["hue_bridge_token"] = token
    with open(os.path.join(PATH, "settings.json"), "w") as f:
        json.dump(data, f)

def removeServer(macAddress):
    with open(os.path.join(PATH, "servers.json"), "r") as f:
        data = json.load(f)
    onlyMacAddress = macAddress.split('- ')[1].split(' ')[0].upper()
    for i,server in enumerate(data):
        if server["macAddress"] == onlyMacAddress:
            data.pop(i)
            break
    with open(os.path.join(PATH, "servers.json"), "w") as f:
        json.dump(data, f, sort_keys=True, indent=1, separators=(',', ': '))

def removeButton(macAddress):
    with open(os.path.join(PATH, "devices.json"), "r") as f:
        data = json.load(f)
    onlyMacAddress = macAddress.split('- ')[1]
    for i,button in enumerate(data):
        if button["macAddress"] == onlyMacAddress:
            data.pop(i)
            break
    with open(os.path.join(PATH, "devices.json"), "w") as f:
        json.dump(data, f, sort_keys=True, indent=1, separators=(',', ': '))
    if os.path.exists(os.path.join(PATH, f"{onlyMacAddress.replace(':', '-')}Scenes.json")):
        os.remove(os.path.join(PATH, f"{onlyMacAddress.replace(':', '-')}Scenes.json"))
    with open(os.path.join(PATH, "macAddresses.json"), "r") as f:
        data = json.load(f)
    for i,button in enumerate(data):
        if button["macAddress"] == onlyMacAddress:
            data.pop(i)
            break
    with open(os.path.join(PATH, "macAddresses.json"), "w") as f:
        json.dump(data, f, sort_keys=True, indent=1, separators=(',', ': '))

def getDevices():
    DevicesList = {}
    with open(os.path.join(PATH, 'devices.json'), "r") as f:
        obj = json.load(f)
    for i in range(len(obj)):
        devicesMacAddress = obj[i]["macAddress"]
        devicesName = obj[i]["deviceName"]
        DevicesList[i] = f"{devicesName} - {devicesMacAddress.lower()}"
    return DevicesList

def time_ago(timestamp):
    current_time = datetime.datetime.now()
    time_difference = current_time - datetime.datetime.fromtimestamp(timestamp)
    days = time_difference.days
    hours = time_difference.seconds // 3600
    minutes = (time_difference.seconds // 60) % 60
    seconds = time_difference.seconds % 60
    if days > 0:
        return f"{days} day(s) ago"
    elif hours > 0:
        return f"{hours} hour(s) ago"
    elif minutes > 0:
        return f"{minutes} minute(s) ago"
    else:
        return f"{seconds} second(s) ago"

def getServers():
    DevicesList = {}
    with open(os.path.join(PATH, 'servers.json'), "r") as f:
        obj = json.load(f)
    for i in range(len(obj)):
        devicesMacAddress = obj[i]["macAddress"]
        devicesName = obj[i]["deviceName"]
        lastUsed = obj[i]["lastUsed"]
        DevicesList[i] = f"{devicesName} - {devicesMacAddress.lower()}" + f" Last used: {time_ago(lastUsed)}"
    return DevicesList

def getRooms():
    groups = requests.get(GROUP_ENDPOINT).json()
    number = 0
    RoomList = {}
    RoomNumber = {}
    for id in groups:
        if groups[id].get('type') == 'Room':
            group_name = groups[id].get('name')
            number += 1
            RoomList[number] = group_name
            RoomNumber[group_name] = id
    return RoomList, RoomNumber

def getRoomsNumber():
    groups = requests.get(GROUP_ENDPOINT).json()
    number = 0
    RoomNumber = {}
    for id in groups:
        if groups[id].get('type') == 'Room':
            number += 1
            RoomNumber[number] = id
    return RoomNumber

def getScenes(RoomID):
    scenes = requests.get(SCENES_ENDPOINT).json()
    SceneList = {}
    SceneCodeList = {}
    number = 0
    for id in scenes:
        if scenes[id].get('group') == RoomID:
            scene_name = scenes[id].get('name')
            number += 1
            SceneList[number] = scene_name
            SceneCodeList[scene_name] = id
    return SceneList, SceneCodeList

def checkRooms(RoomID):
    validRoomID = False
    RoomList,RoomIDList = getRooms()
    for i in range(len(RoomIDList)):
        if RoomIDList[RoomList[i+1]] == RoomID:
            validRoomID = True
    return validRoomID

def checkMacAddress(macAddress):
    validMacAddress = False
    DevicesList = getDevices()
    for i in range(len(DevicesList)):
        if DevicesList[i] == macAddress:
            validMacAddress = True
    return validMacAddress

def ScenesIDtoName(ScenesIDs):
    scenes = requests.get(SCENES_ENDPOINT).json()
    SceneList = {}
    number = 0
    for sceneCodes in ScenesIDs:
        number += 1
        for id in scenes:
            if sceneCodes == id:
                SceneList[number] = scenes[id].get('name')
    return SceneList

def confirmConfig(FullmacAddress, RoomNumber, listScene, listSceneName):
    _, macAddress = FullmacAddress.split('- ')
    new_data = {"macAddress":f"{macAddress}",
            "groupNumber":f"{RoomNumber}"}
    with open(os.path.join(PATH, 'macAddresses.json'), "r") as f:
        obj = json.load(f)
    try:
        for i in range(len(obj)):
            if obj[i]["macAddress"] == macAddress:
                obj.pop(i)
                with open(os.path.join(PATH, 'macAddresses.json'), "w") as f:
                    f.write(json.dumps(obj, sort_keys=True, indent=1, separators=(',', ': ')))
    except:
        pass
    with open(os.path.join(PATH, "macAddresses.json"), 'r+') as file:
        file_data = json.load(file)
        file_data.append(new_data)
        file.seek(0)
        json.dump(file_data, file, indent=1)
    macAddressNoDot = macAddress.replace(":", "-")
    file_exists = os.path.exists(PATH + macAddressNoDot + "Scenes.json")
    if file_exists == False:
        with open(PATH + macAddressNoDot + "Scenes.json", "w") as f:
            f.write(json.dumps([], sort_keys=True, indent=1, separators=(',', ': ')))
    with open(PATH + macAddressNoDot + "Scenes.json", 'r+') as f:
        f.truncate()
    with open(PATH + macAddressNoDot + "Scenes.json", 'r+') as f:
        f.write(json.dumps([], sort_keys=True, indent=1, separators=(',', ': ')))
    def write_json(new_data):
        with open(PATH + macAddressNoDot + "Scenes.json", 'r+') as f:
            file_data = json.load(f)
            file_data.append(new_data)
            f.seek(0)
            json.dump(file_data, f, indent=1)
    for i in range(len(listScene)):
        y = {"sceneName":f"{listSceneName[i+1]}",
            "sceneID":f"{listScene[i]}"}
        write_json(y)

def writeNewName(macAddress, newName):
    with open(os.path.join(PATH, 'devices.json')) as f:
        obj = json.load(f)      
    _, onlyMacAddress = macAddress.split('- ')
    for i in range(len(obj)):
        if obj[i]["macAddress"] == onlyMacAddress:
            obj[i]["deviceName"] = newName
            with open(os.path.join(PATH, "devices.json"), "w") as f:
                f.write(json.dumps(obj, sort_keys=True, indent=1, separators=(',', ': ')))

def serversWriteNewName(macAddress, newName):
    with open(os.path.join(PATH, 'servers.json')) as f:
        obj = json.load(f)       
    _, onlyMacAddress = macAddress.split('- ')
    onlyMacAddress = onlyMacAddress.split(' ')[0]
    for i in range(len(obj)):
        if obj[i]["macAddress"].lower() == onlyMacAddress:
            obj[i]["deviceName"] = newName
            with open(os.path.join(PATH, "servers.json"), "w") as f:
                f.write(json.dumps(obj, sort_keys=True, indent=1, separators=(',', ': ')))