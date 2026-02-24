#!/usr/bin/env python3
import argparse
import configparser
import subprocess
import shutil
from pathlib import Path
import re
import sys
import os

# -------------------------
# CLI Arguments
# -------------------------
parser = argparse.ArgumentParser(description="Build and rename HueMixLink firmware binaries")
parser.add_argument("--run", action="store_true", help="Run 'pio run' before renaming binaries")
parser.add_argument("--envs", type=str, help="Comma-separated list of envs to process (default: all)")
parser.add_argument("--clean", action="store_true", help="Clean the release folder before copying")
parser.add_argument("--release-dir", type=str, default="release", help="Output directory for renamed binaries")
args = parser.parse_args()

# -------------------------
# Paths
# -------------------------
PIO_BUILD_DIR = Path(".pio/build")
OUT_DIR = Path(args.release_dir)
INI_FILE = Path("platformio.ini")

if args.clean and OUT_DIR.exists():
    shutil.rmtree(OUT_DIR)
OUT_DIR.mkdir(exist_ok=True)

# -------------------------
# Load platformio.ini
# -------------------------
config = configparser.ConfigParser(strict=False)
config.read(INI_FILE)

# -------------------------
# Helpers: Get output name and platform
# -------------------------
def get_output_name(env_name, firmware_version, lightstrip_model):
    platform_str = get_platform(env_name)

    if "lightstrip" in env_name:
        if not lightstrip_model:
            raise ValueError(f"No LIGHTSTRIP_MODEL defined for {env_name}")
        return f"huemixlink-{platform_str}-lightstrip-{lightstrip_model}-v{firmware_version}.bin"
    elif "motion" in env_name:
        return f"huemixlink-{platform_str}-motion_sensor-v{firmware_version}.bin"
    elif "node" in env_name or "remote" in env_name:
        type_str = env_name.split("_")[1]  # still works for your env names
        return f"huemixlink-{platform_str}-{type_str}-v{firmware_version}.bin"
    elif "button" in env_name:
        return f"huemixlink-{platform_str}-button-v{firmware_version}.bin"
    else:
        return f"{platform_str}-{env_name}-v{firmware_version}.bin"

def get_platform(env_name):
    section = f"env:{env_name}"
    platform = config.get(section, "platform", fallback="")
    if "32" in platform.lower():
        return "esp32"
    elif "8266" in platform.lower():
        return "esp8266"
    else:
        return "unknown"

# -------------------------
# Determine environments
# -------------------------
if args.envs:
    env_list = [e.strip() for e in args.envs.split(",")]
else:
    # All envs except goove
    default_envs_str = config.get("platformio", "default_envs", fallback="")
    env_list = [e.strip() for e in default_envs_str.splitlines() if e.strip()]

if not env_list:
    print("[!] No environments to process")
    sys.exit(1)

# -------------------------
# Run PlatformIO if requested
# -------------------------
if args.run:
    print("[*] Running PlatformIO build...")
    command = f"pio run -j {os.cpu_count()} -e " + " -e ".join(env_list)
    result = subprocess.run(command, shell=True, check=True)
    if result.returncode != 0:
        print("[!] PlatformIO build failed")
        sys.exit(1)

# -------------------------
# Copy and rename binaries
# -------------------------
for env_name in env_list:
    env_dir = PIO_BUILD_DIR / env_name
    bin_file = env_dir / "firmware.bin"
    if not bin_file.exists():
        print(f"[!] No firmware found for {env_name}")
        continue

    # Read FIRMWARE_VERSION and LIGHTSTRIP_MODEL from ini
    section = f"env:{env_name}"
    build_flags = config.get(section, "build_flags", fallback="").replace("\n", " ")

    # Extract version (handles optional quotes)
    match_version = re.search(r'-DFIRMWARE_VERSION=[^\d]*([\d.]+)', build_flags)
    firmware_version = match_version.group(1) if match_version else "0.0.0"

    # Extract model (if any)
    match_model = re.search(r'-DLIGHTSTRIP_MODEL=([0-9]+)', build_flags)
    lightstrip_model = match_model.group(1) if match_model else None

    try:
        out_name = get_output_name(env_name, firmware_version, lightstrip_model)
    except ValueError as e:
        print(f"[!] {e}")
        continue

    shutil.copy(bin_file, OUT_DIR / out_name)
    print(f"✔ {out_name}")