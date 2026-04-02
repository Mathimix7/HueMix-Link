<h1 align="center">HueMix-Link</h1>
<p align="center"><img src="/images/logo.png" alt="HueMix-Link's logo", width="250" ></p>

## Project Overview

HueMix-Link is a local smart home bridge that connects custom ESP-based devices (gateways, buttons, remotes, motion sensors, door sensors, and lightstrips) to Philips Hue rooms and scenes.

It provides:

- A Flask web UI for setup and management
- A UDP server for low-latency gateway communication
- Automation logic for buttons, remotes, and motion sensors
- OTA firmware update workflows for all device types
- Backup and restore tools for configuration data

## What Is In This Repository

- `python/`: Main backend service (Flask UI + UDP network + automations + OTA)
- `esp-firmware/`: PlatformIO firmware sources for all device types (gateway, buttons, remotes, motion sensors, door sensors, lightstrips)
- `Hardware-designs/`: KiCad design files for all device types (gateway, buttons, remotes, motion sensors, door sensors, lightstrips)
- `install.sh`: Linux installer/updater
- `VERSION`: Version file for installer/update workflows

## System Overview

### High-level architecture

1. **All ESP devices use ESP-NOW**: Low-power mesh protocol for device-to-device communication
2. **Gateways bridge ESP-NOW to UDP**: Gateways receive ESP-NOW messages and forward them to the backend via UDP
3. **Backend (UDP server) receives events**: Button presses, remote actions, motion detections, device hellos, delivery reports
4. **Automation engine maps events to Hue actions**: Scene activation, toggle on/off, brightness adjust, and lightstrip sync
5. **Hue state is cached and synchronized**: Automations and lightstrips stay aligned with current room/scene state
6. **Web UI for management**: Device pairing, automation configuration, OTA updates, monitoring, and backups

### Main runtime services

- **Flask + Waitress**: Web UI server `127.0.0.1:5001` (default)
- **UDP listener**: Receives gateway messages on `0.0.0.0:7777` (default, configurable)
- **Automation service**: Listens to device events and Hue state changes, applies configured logic
- **OTA manager**: Handles firmware distribution, chunking, and acknowledgment tracking

## Prerequisites

### Software

- Python 3.9+ (3.10+ recommended)
- `pip` and virtual environment support (`venv`)
- Network access to your Hue Bridge (same LAN)
- Linux OS with `systemd` (for production install)
- `sudo` access and `apt` package manager

### Hardware

- At least one Hue Bridge with configured rooms/scenes
- Supported HueMix-Link devices (gateways, buttons, remotes, motion sensors, door sensors, lightstrips)
- Firmware binaries matching your device type and model

## Installation

### Linux

From the repository root:

```bash
sudo bash install.sh
```

The installer will:

- Deploy the Python app to `/opt/huemix-link`
- Create a dedicated service user and Python virtual environment
- Set up a `systemd` service (`huemix-link`) to run on boot
- Configure HAProxy to expose the web app (HTTP port 80, HTTPS port 443 by default)
- Store all runtime data in `/opt/huemix-link/data`

Useful installer options:

```bash
sudo bash install.sh --help           # Show all options
sudo bash install.sh --yes            # Non-interactive install
sudo bash install.sh --dry-run        # Preview without making changes
sudo bash install.sh --force          # Force recreation of venv
sudo bash install.sh --show-version   # Show installed and source versions
sudo bash install.sh --port 8080      # Expose on custom HTTP port
sudo bash install.sh --no-https       # Disable HTTPS (HTTP only)
sudo bash install.sh --no-local       # Skip huemixlink.local hostname setup
```

After installation, the web UI is accessible at:

- `https://huemixlink.local` (or your server IP)
- `http://huemixlink.local` (or your server IP)

### Updating

To update the backend to a newer version, pull the latest code and re-run the installer:

```bash
git pull
sudo bash install.sh
```

The installer detects the existing installation, updates the files, and restarts the service automatically.

## First-time Commissioning Workflow

1. **Start the backend** (automatic on boot if installed via installer)
2. **Open the web UI** at `http://huemixlink.local` (or your server IP)
3. **Configure Hue Bridge**:
   - Go to the Bridge configuration page
   - Auto-discover or enter bridge IP manually
   - Press the Hue Bridge link button
   - Pair app with bridge (saves credentials)
4. **Pair ESP devices**:
   - Go to Pairing page
   - Start pairing window (choose device type if desired)
   - Power on or reset target devices
   - Devices announce themselves via ESP-NOW → gateway → backend
5. **Configure each device**:
    - **Buttons/Remotes**: Map to room + scenes + action behavior (normal, toggle, brightness, scene-cycle)
    - **Motion Sensors**: Set room, time slots, motion actions, after-actions, cooldown periods
    - **Door Sensors**: Set room, open/close actions, and any automation triggers tied to magnetic state changes
    - **Lightstrips**: Assign room, configure LED count/type, optional color scene overrides
    - **Gateways**: Verify online status, mesh routing, LED schedule (day/night auto-off)
6. **Validate behavior** by triggering physical events and checking Hue room/scene changes

## ESP Firmware: Build and Flash

### Flashing ESP devices

**Option 1: Serial flash via OTA page**

1. Go to the **OTA** page in the web UI
2. Select firmware type
3. Connect ESP device via serial in programming mode
4. Click **Flash** and select serial port
5. Monitor progress on the page

**Option 2: Serial port flash**

Use PlatformIO to flash directly via USB:

```bash
cd esp-firmware
pio run -e esp32_net_node --target upload
```

Or use `esptool.py` directly:

```bash
esptool.py --port /dev/ttyUSB0 write_flash 0x0 huemixlink-esp32-net-vX.Y.Z.bin
```

Pre-built firmware binaries are available in the project's GitHub releases. Download the `.bin` file(s) matching your device type and platform.

## Firmware Updates (OTA Page)

The **OTA** page in the web UI lets you:

- **Check available versions** from GitHub releases
- **Select device** type/model and target device
- **Battery-powered devices**: double-press the reset button before OTA update
- **Initiate flash** with progress monitoring
- **Monitor errors** and retry failed updates

## Configuration and Data Storage

All runtime configuration is stored in `python/data/` (or `/opt/huemix-link/data` in production) as JSON files:

- `bridge.json`: Hue bridge IP and app username
- `config.json`: Server settings (UDP port, dev mode)
- `gateways.json`: Known gateway devices
- `buttons.json`: Button and remote configurations
- `motion_sensors.json`: Motion sensor configurations and time slots
- `lightstrips.json`: Lightstrip configurations and color overrides
- `pairing_history.json`: Last 5 paired devices
- `home_id`: Unique identifier for this HueMix-Link instance. This value is embedded in every packet as a shared secret and used to verify that incoming messages belong to your installation. All devices must be paired while this ID is set — re-pairing is required if it changes.

Backups are created and restored through the settings page; they are stored in `data/backups/`.

## Networking and Ports

- **Web server (internal)**: `127.0.0.1:5001` (Flask)
- **UDP listener (internal)**: `0.0.0.0:7777` (default, receives gateway messages)
- **External HTTP/HTTPS (production)**: Exposed by HAProxy (default HTTP 80 + HTTPS 443)

UDP port can be changed in settings; the backend will restart UDP listening with the new port.

## How Automation Works

1. **Device sends event** via **ESP-NOW** (button press, remote action, motion detection)
2. **Gateway receives** via ESP-NOW and forwards message to backend via **UDP**
3. **UDP server** decodes and validates packet (signature check using HomeID)
4. **Device manager** resolves device identity; **pairing manager** verifies eligibility
5. **Automation engine** applies configured logic:
   - **Buttons/Remotes**: Scene cycle, toggle, brightness adjust, hold-to-adjust
   - **Motion Sensors**: Trigger scenes/lights on detection; post-motion fade/off actions; cooldown throttling
6. **Hue API** is called to apply scene/room/light changes
7. **Hue state manager** caches current room, light, and scene state
8. **Lightstrip sync**: Checks if any lightstrips should mirror current scene; sends raw LED commands via **ESP-NOW → gateway → UDP**

On startup, config sync reconciles saved room/scene references against live Hue topology to avoid stale mappings.

## Devices

### Gateway

<table><tr>
  <td><img src="images/gateway.png" alt="Gateway" width="350"></td>
  <td><img src="images/gateway-open.png" alt="Gateway" width="350"></td>
</tr></table>

The Gateway is a single board that contains two ESP32 modules working together:

- **Net Node** — connects to your home WiFi and bridges between the backend (UDP) and the ESP-NOW mesh. Runs WiFiManager for wireless configuration and relays commands to the radio node over a high-speed UART link.
- **Radio Node** — handles all ESP-NOW communication with peripheral devices (buttons, remotes, sensors, lightstrips) and forwards packets to/from the net node over UART. Extends ESP-NOW coverage independently of WiFi.

**LED indicators**

| LED | State | Meaning |
|-----|-------|---------|
| WiFi | Off | No power / booting |
| WiFi | Fast blink (0.2s) | Not yet configured — WiFi config portal is open |
| WiFi | Slow blink (0.5s) | Connecting or retrying WiFi |
| WiFi | Solid on | Online and operational |
| Data | Brief flash | Packet received or forwarded |
| Data | Breathing (fade in/out) | Net node OTA firmware update in progress |
| Radio | Brief flash | ESP-NOW packet relayed |
| Radio | Breathing (fade in/out) | Radio node OTA firmware update in progress |

**Buttons**

- **Main button**: Acts as a single button that can be mapped to a Hue room in the web UI
- **Aux button**: Hold 5+ seconds to factory-reset and re-open the WiFi config portal

**Initial setup**

1. Power on the gateway — the WiFi LED fast-blinks and it broadcasts SSID `HueMix Link - XXXXXXXX`
2. Connect to that SSID (password: `HueMixLink`) with a phone or laptop
3. Enter your home WiFi credentials and the backend server IP + UDP port
4. The gateway connects, the WiFi LED goes solid, and it sends a HELLO to the backend

**Pairing**

Gateways pair automatically once configured.

**OTA**

Both nodes are updated from the OTA page. Gateways receive OTA directly (no double-press needed).

---

### Radio Gateway (Serial Python)

The Radio Gateway can also run as a serial-connected ESP32 node that pairs with the Python backend instead of WiFi.

- **Net/host side**: Python maintains the gateway state, serial transport, OTA scheduling, and dashboard telemetry
- **Radio side**: ESP32 radio node handles ESP-NOW traffic, OLED status display, and forwards packets over UART
- **LED indicators**: Follows the configured gateway LED schedule; the OLED display also respects the same on/off timing
- **OTA**: Updated from the OTA page using the `gateway_radio_python` firmware target

**Setup**

1. Flash the `esp32_radio_python` firmware
2. Connect the radio node to the backend host over USB serial
3. Configure the serial port in the web UI
4. The backend will sync HOME_ID, gateway state, and dashboard telemetry automatically

---

### Button

<table><tr>
  <td><img src="images/button.png" alt="Button" width="350"></td>
  <td><img src="images/button-open.png" alt="Button" width="350"></td>
</tr></table>

The Button is a single pushbutton device that sends click, hold, and release events via ESP-NOW. The ESP32 version is battery-powered and deep-sleeps between presses, while the ESP8266 version is always-on and runs continuously.

> [!WARNING]
> The `normal_button` firmware is deprecated. The ESP32 Normal Button board v3 and later should use the `remote_button` firmware configured as 1, 2, or 3 buttons.

**Events sent**

| Action | Trigger |
|--------|---------|
| Click | Short press and release |
| Holding | Held beyond 500 ms, repeated every 500 ms |
| Release | Released after a hold |

**Default automation** (configurable in web UI): Click cycles scenes in the assigned room. Hold adjusts brightness. Release stops brightness adjustment.

**LED indicators**

| State | Meaning |
|-------|---------|
| Brief flash | Action sent via ESP-NOW |
| 2 fast blinks | No acknowledgment — gateway may be out of range |
| 2 slow blinks | Failed to pair device |
| 3 blinks | OTA mode |
| 5 blinks | Device powered on |
| Breathing | OTA in progress |

**Setup and pairing**

1. Flash firmware via serial or the OTA page
2. Open the Pairing page, start a pairing window, then press the button to wake the device
3. After pairing, assign the button to a room in the device settings

**OTA**

**Double-press the reset button** to enter OTA mode — the LED will blink 3 times to confirm. Then initiate the flash from the OTA page.

---

### Remote

<table><tr>
  <td><img src="images/remote.png" alt="Remote" width="350"></td>
  <td><img src="images/remote-open.png" alt="Remote" width="350"></td>
</tr></table>

The Remote is a battery-powered ESP32 device with up to 4 independently configurable buttons. Each button can target a different room with its own action type. The number of active buttons (1–4) is configured via hardware at flash time.

**Action types per button** (configurable in web UI)

| Type | Click | Hold |
|------|-------|------|
| Normal | Cycle scenes / turn room off | Adjust brightness |
| Toggle | Toggle room on/off | — |
| Brightness Up | Increase brightness | Increase continuously |
| Brightness Down | Decrease brightness | Decrease continuously |
| Scene Cycle | Cycle scenes (no off) | Adjust brightness |

**LED indicators**

| State | Meaning |
|-------|---------|
| Brief flash | Button action sent |
| 2 fast blinks | No acknowledgment — gateway may be out of range |
| 2 slow blinks | Failed to pair device |
| 3 blinks | OTA mode |
| 5 blinks | Device powered on |
| On 2 seconds | Button count configuration saved |
| Breathing | OTA in progress |

**Setting the button count**

Triple-press the reset button within 1 second to enter button-count config mode, while holding the target number of buttons simultaneously:

- 1 button: hold only button 4
- 2 buttons: hold buttons 1 + 2
- 3 buttons: hold buttons 1 + 2 + 4
- 4 buttons: hold all four buttons or None

**Setup and pairing**

1. Flash firmware via serial or the OTA page
2. Open the Pairing page, start a pairing window, then press any button to wake the device
3. After pairing, configure each button's room and action type in the device settings

**OTA**

Battery-powered. **Double-press the reset button** to enter OTA mode. The LED will blink 3 times to confirm. Then flash from the OTA page.

---

### Motion Sensor

<table><tr>
  <td><img src="images/motion-sensor.png" alt="Motion Sensor" width="350"></td>
  <td><img src="images/motion-sensor-open.png" alt="Motion Sensor" width="350"></td>
</tr></table>

The Motion Sensor is a battery-powered ESP32 device with a PIR sensor and an ambient light level sensor (LDR). It sleeps in deep sleep and wakes when the PIR fires. After sending a motion event it enters a configurable cooldown period (default 60s) before re-arming.

**Data sent on motion detection**

- Motion detected flag
- Ambient light level (1-10)
- Battery voltage

**LED indicators**

| State | Meaning |
|-------|---------|
| Brief flash | Motion detected, event sent |
| 2 fast blinks | No acknowledgment — gateway may be out of range |
| 3 blinks | OTA mode |
| 5 blinks | Device powered on |
| Breathing | OTA in progress |

**Automation options** (configurable in web UI)

- Assign to a room and one or more scenes
- Set time slots (e.g., a different scene during the night)
- Configure an after-motion action (turn off, dim, change scene) and its delay
- Set the cooldown period (minimum seconds before re-triggering)

**Setup and pairing**

1. Flash firmware via serial or the OTA page
2. Open the Pairing page, start a pairing window, then single-press the reset button to wake the device
3. After pairing, configure the room, scenes, and time slots in the device settings

**OTA**

Battery-powered. **Double-press the reset button** to enter OTA mode. The LED will blink 3 times to confirm. Then flash from the OTA page.

---

### Door Sensor

The Door Sensor is a battery-powered ESP32 device that reports magnetic open/close state changes and ambient light level over ESP-NOW.

**Events sent**

- Door opened
- Door closed
- Ambient light level (1-10)
- Battery voltage
- Firmware version / platform metadata

**LED indicators**

| State | Meaning |
|-------|---------|
| Brief flash | Door event sent |
| 2 fast blinks | No acknowledgment — gateway may be out of range |
| 3 blinks | OTA mode |
| 5 blinks | Device powered on |
| Breathing | OTA in progress |

**Setup and pairing**

1. Flash the door sensor firmware via serial or the OTA page
2. Open the Pairing page, start a pairing window, then single-press the reset button or activate the reed switch to wake the device
3. After pairing, assign the sensor to a room and add any open/close automations

**OTA**

Battery-powered. **Double-press the reset button** to enter OTA mode. Then flash from the OTA page.

---

### Lightstrip

<table><tr>
  <td><img src="images/lightstrip.png" alt="Lightstrip" width="350"></td>
  <td><img src="images/lightstrip-open.png" alt="Lightstrip" width="350"></td>
</tr></table>

The Lightstrip is an ESP32 or ESP8266 device that drives WS2812B or SK6812 LED strips (up to 60 LEDs). It mirrors the scene colors of an assigned Hue room in real-time, syncing color, brightness, and saturation from the Hue state cache.

**Hardware models**

| Model | MCU | LED type | Notes |
|-------|-----|----------|-------|
| 1 | ESP32 | WS2812B | Standard RGB |
| 2 | ESP8266 | WS2812B | Standard RGB |
| 3 | ESP32 | SK6812 | RGBW |
| 4 | ESP8266 | SK6812 | RGBW |
| 5 | ESP32 | WS2812B | RGB + warm white PWM channel |
| 6 | ESP8266 | WS2812B | RGB + warm white PWM channel |

**LED strip behavior**

- **On**: Displays current Hue scene colors
- **Off**: All LEDs off
- **Scene change**: Immediately transitions to new colors when the Hue room scene changes

**Setup and pairing**

1. Flash the correct model firmware to match your LED strip hardware
2. Open the Pairing page, start a pairing window, then power on or reset the device
3. After pairing, assign the lightstrip to a room and set the LED count in the device settings

**OTA**

Lightstrips receive OTA directly (no double-press needed).

---

## License

This project is licensed under the terms in `LICENSE`.
