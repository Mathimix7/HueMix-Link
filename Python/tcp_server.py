from utils import PATH, logger
import os
import socket
from datetime import datetime
import time
from HueActivations import goThruScenes, TurnOffScene, decreaseBrightness, increaseBrightness, checkStatus, updateAPI
from HueActivations import savedBridge
import json

def startSocket(sock:socket.socket=None):
    if sock:
        sock.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        settings = json.load(f)
    server_address = ('', settings["tcp_port"])
    logger.info("starting up on port %s%s" % server_address)
    sock.bind(server_address)
    sock.listen(1)
    return sock

sock = startSocket()

NotFound = True
ButtonNumberTimes = {}
ButtonHoldingStatus = {}
ButtonTimeUpdate = {}

with open(os.path.join(PATH, "devices.json"), "r") as f:
    obj = json.load(f)
for i in range(len(obj)):
    mac = obj[i]["macAddress"]
    ButtonNumberTimes[mac] = 0
    ButtonHoldingStatus[mac] = 0
    ButtonTimeUpdate[mac] = 0

while True:
    logger.info('waiting for a connection')
    connection, client_address = sock.accept()
    try:
        logger.info('connection from ' + str(client_address))
        data = connection.recv(50)
        if data:
            logger.info('received ' + str(data))
            if data.decode().endswith(",-p"):
                if data.decode().split(",")[0] == "resetPort":
                    sock = startSocket(sock)
                elif data.decode().split(",")[0] == "updateBridge":
                    updateAPI()
                    from HueActivations import savedBridge
                elif data.decode().split(",")[0] == "svMacs":
                    mac = data.decode().split(",")[1]
                    with open(os.path.join(PATH, "servers.json"), "r") as f:
                        servers = json.load(f)
                    serversMacs = []
                    for server in servers:
                        serversMacs.append(server["macAddress"])
                        if mac in server["macAddress"]:
                            server["lastUsed"] = round(datetime.timestamp(datetime.now()))
                    if mac not in serversMacs:
                        serversMacs.append(mac)
                        servers.append({"deviceName": "UNKNOWN", "macAddress": mac, "lastUsed": round(datetime.timestamp(datetime.now()))})
                        logger.info(f"Server not registered, {mac} succesfully added!")
                    result = ",".join(serversMacs)
                    connection.sendall(result.encode())
                    with open(os.path.join(PATH, "servers.json"), "w") as f:
                        json.dump(servers, f)
                continue
            macAddress, mode, macSV = data.decode().split(',')
            connection.close()
            with open(os.path.join(PATH, "servers.json"), "r") as f:
                servers = json.load(f)
            for server in servers:
                if macSV == server["macAddress"]:
                    server["lastUsed"] = round(datetime.timestamp(datetime.now()))
                    break
            else:
                servers.append({"deviceName": "UNKNOWN", "macAddress": macSV, "lastUsed": round(datetime.timestamp(datetime.now()))})
            with open(os.path.join(PATH, "servers.json"), "w") as f:
                json.dump(servers, f)
            with open(os.path.join(PATH, "devices.json"), "r") as f:
                obj = json.load(f)
            alreadyRegistered = False
            for i in range(len(obj)):
                if obj[i]["macAddress"] == macAddress:
                    alreadyRegistered = True
            if not alreadyRegistered:
                logger.info(f"Button not registered, {macAddress} succesfully added!")
                new_data = {"deviceName":f"UNKNOWN",
                            "macAddress":f"{macAddress}"}
                with open(PATH + "devices.json",'r+') as file:
                    file_data = json.load(file)
                    file_data.append(new_data)
                    file.seek(0)
                    json.dump(file_data, file, indent=1)
                ButtonNumberTimes[macAddress] = 0
                ButtonHoldingStatus[macAddress] = 0
                ButtonTimeUpdate[macAddress] = 0
            with open(os.path.join(PATH, "macAddresses.json"), "r") as f:
                obj = json.load(f)
            for i in range(len(obj)):
                if obj[i]["macAddress"] == macAddress:
                    NotFound = False
                    break
            if NotFound:
                continue
            NotFound = True
            ClickTime = time.time()
            if not savedBridge:
                logger.error("no Hue Bridge found")
                continue
            if mode == "Once":
                logger.debug(f"Going through scenes for {macAddress}")
                statusON = checkStatus(macAddress)
                if ButtonNumberTimes[macAddress] == 0:
                    ButtonTimeUpdate[macAddress] = time.time()
                if statusON == False and ButtonNumberTimes[macAddress] != 0:
                    ButtonTimeUpdate[macAddress] = time.time()
                    ButtonNumberTimes[macAddress] = 0
                if ButtonNumberTimes[macAddress] == 0 and statusON == True:
                    TurnOffScene(macAddress)
                elif ClickTime - ButtonTimeUpdate[macAddress] <= 2:
                    ButtonTimeUpdate[macAddress] = time.time()
                    ButtonHoldingStatus[macAddress] = 0
                    ButtonNumberTimes[macAddress] = goThruScenes(macAddress, ButtonNumberTimes[macAddress])
                else: 
                    ButtonNumberTimes[macAddress] = 0
                    TurnOffScene(macAddress)
                    ButtonTimeUpdate[macAddress] = time.time()
            elif mode == "Holding":
                #0 decrease brightness - 1 increase brightness
                if ButtonHoldingStatus[macAddress] == 0:
                    decreaseBrightness(macAddress)
                    logger.debug(f"Decreasing brightness for {macAddress}")
                elif ButtonHoldingStatus[macAddress] == 1:
                    increaseBrightness(macAddress)
                    logger.debug(f"Increasing brightness for {macAddress}")
            
            elif mode == "HoldingStopped":
                if ButtonHoldingStatus[macAddress] == 0:
                    ButtonHoldingStatus[macAddress] = 1
                elif ButtonHoldingStatus[macAddress] == 1:
                    ButtonHoldingStatus[macAddress] = 0

    except Exception as e:
        logger.warning("Unexpected error occurred. " + str(e))
        continue