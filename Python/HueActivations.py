import requests
import json
import os
import socket
from utils import PATH, logger

savedBridge = False

def updateAPI():
    global API_ENDPOINT, GROUP_ENDPOINT, SCENES_ENDPOINT, savedBridge
    with open(os.path.join(PATH, "settings.json"), "r") as f:
        settings = json.load(f)
    if settings["hue_bridge_ip"]:
        savedBridge = True
        logger.info("found hue bridge saved at " + settings["hue_bridge_ip"])
    else:
        ip_address = socket.gethostbyname(socket.gethostname())
        savedBridge = False
        logger.error(f"no Hue Bridge found, set-up at http://{ip_address}:{settings['website_port']}/discover")
    API_ENDPOINT = f"http://{settings['hue_bridge_ip']}/api/{settings['hue_bridge_token']}"
    GROUP_ENDPOINT = API_ENDPOINT + "/groups"
    SCENES_ENDPOINT = API_ENDPOINT + "/scenes"

updateAPI()

def getGroupEndpoint(num):
    return GROUP_ENDPOINT + "/" + str(num)

def getaActionEndpoint(num):
    return GROUP_ENDPOINT + "/" + str(num) + "/action"

def checkStatus(macAddress):
    obj  = json.load(open(PATH + "macAddresses.json"))
    for i in range(len(obj)):
        if obj[i]["macAddress"] == macAddress:
            groupNumber = obj[i]['groupNumber']
    try:
        stateJson = requests.get(getGroupEndpoint(groupNumber)).json()
        logger.debug("Message sent to the Hue Bridge API")
        lightsON = stateJson['state']['any_on']
    except Exception as e:
        logger.warning("Hue Bridge API not working" + str(e))
        lightsON = False
    return lightsON

def goThruScenes(macAddress, sceneNum):
    macAddressNoDot = macAddress.replace(":", "-")
    obj  = json.load(open(PATH + macAddressNoDot +"Scenes.json"))
    try:
        scene = obj[sceneNum]['sceneID']
    except:
        sceneNum = 0
        scene = obj[sceneNum]['sceneID']
    obj  = json.load(open(PATH + "macAddresses.json"))
    for i in range(len(obj)):
        if obj[i]["macAddress"] == macAddress:
            groupNumber = obj[i]['groupNumber']
    try:
        requests.put(getaActionEndpoint(groupNumber), data=json.dumps({"scene": scene}))
        logger.debug("Message sent to the Hue Bridge API")
    except Exception as e:
        logger.warning("Hue Bridge API not working" + str(e))
    sceneNum += 1
    return sceneNum

def TurnOffScene(macAddress):
    obj  = json.load(open(PATH + "macAddresses.json"))
    for i in range(len(obj)):
        if obj[i]["macAddress"] == macAddress:
            groupNumber = obj[i]['groupNumber']
    try:
        requests.put(getaActionEndpoint(groupNumber), data=json.dumps({"on":False}))
        logger.debug("Message sent to the Hue Bridge API")
    except Exception as e:
        logger.warning("Hue Bridge API not working" + str(e))

def decreaseBrightness(macAddress):
    obj  = json.load(open(PATH + "macAddresses.json"))
    for i in range(len(obj)):
        if obj[i]["macAddress"] == macAddress:
            groupNumber = obj[i]['groupNumber']
    groupBrightness = requests.get(getGroupEndpoint(groupNumber)).json()
    groupBrightness = groupBrightness['action']['bri']
    groupBrightnessDecrease = 31
    try:
        requests.put(getaActionEndpoint(groupNumber), data=json.dumps({"bri": groupBrightness-groupBrightnessDecrease}))
        logger.debug("Message sent to the Hue Bridge API")
    except Exception as e:
        logger.warning("Hue Bridge API not working" + str(e))

def increaseBrightness(macAddress):
    obj  = json.load(open(PATH + "macAddresses.json"))
    for i in range(len(obj)):
        if obj[i]["macAddress"] == macAddress:
            groupNumber = obj[i]['groupNumber']
    groupBrightness = requests.get(getGroupEndpoint(groupNumber)).json()
    groupBrightness = groupBrightness['action']['bri']
    groupBrightnessincrease = 31
    try:
        requests.put(getaActionEndpoint(groupNumber), data=json.dumps({"bri": groupBrightness + groupBrightnessincrease}))
        logger.debug("Message sent to the Hue Bridge API")
    except Exception as e:
        logger.warning("Hue Bridge API not working" + str(e))