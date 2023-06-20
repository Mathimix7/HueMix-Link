import os
import logging
import sys
import json

PATH = os.path.join(os.getcwd()) + "/DataFiles/"

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] - %(message)s', datefmt='<%Y-%m-%d %H:%M:%S>')
handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)

logger.info("staring HueMix Link...")
logger.info("starting on path: " + PATH)

if not os.path.isdir(PATH):
    logger.warning("folder was not found! Attempting to create folder on directory: " + PATH)
    os.makedirs(PATH)

JSONFILES = ["settings.json", "devices.json", "servers.json", "macAddresses.json"]
file_data = [{"hue_bridge_ip": "", "hue_bridge_token": "", "tcp_port": 7777, "website_port": 80}, [], [], []]
for file, data in zip(JSONFILES, file_data):
    file_path = os.path.join(PATH, file)
    if not os.path.exists(file_path):
        logger.warning(f"{file} was not found! Attempting to create it on directory: " + PATH)
        with open(file_path, 'w') as f:
            json.dump(data, f)
        os.chmod(file_path, 0o777)
        logger.warning(f"file {file} succesfully created!")