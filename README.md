<h1 align="center">HueMix-Link</h1>
<p align="center"><img src="/images/logo.png" alt="HueMix-Link's logo", width="250" ></p>

## Project Overview

HueMix Link provides a convenient way to control Hue lights using a physical button. The ESP-based button, configured as an ESP-NOW device, sends data to a server running on another ESP. The server then communicates with a TCP server running on a Linux machine. The Linux machine hosts a website that allows you to configure buttons, servers, the Hue bridge, & more. The project includes various components, such as the ESP code, the TCP server, the website, & more.
## Setup and Configuration

To set up the HueMix Link project, follow these steps:

1. Run the `tcp_server.py` script and the `website.py` on the Linux machine. [Set-up service](#linux-service-setup)
2. Configure the hue bridge and assign ports according to your preferences.
3. Connect the ESP-based button according to the provided PCB and wire connections.
4. Connect the ESP-based server according to the provided PCB and wire connections.
5. Flash the `esp_now_server.ino` sketch to the ESP NOW server and `tcp_client_server.ino` sketch to the TCP server.
6. Wait until blue led is flashing, connect to ``HueMix Link - id`` network with password ``HueMixLink``. Follow the steps at ``192.168.4.1``.
7. Flash the `esp_now_button.ino` sketch to the ESP-based button *(esp8266 version does not include deep sleep)*.
8. When you press the button it should be automaticaly added to the website to set-up the scenes.

## Usage

Once the project is set up and configured, follow these steps to use HueMix Link:

1. Access the website hosted on the Linux machine.
2. Use the website's interface to configure buttons, servers, and the Hue bridge.
3. Press the physical button connected to the ESP-based button to control the Hue lights. :bulb:
4. Press the button once to go through the scenes. Hold to increase or decrease brightness and press once with light on to turn them off.

## Website

The HueMix Link website provides a user-friendly interface for configuring buttons, servers, and the Hue bridge. It allows you to:

- :gear: Add and manage buttons: Assign scenes to different buttons depending on the room and customize their behavior.
- :satellite: Configure servers: Define the ESP server's name and delete servers that are no longer in use.
- :bulb: Set up the Hue bridge: Specify the IP address and authentication credentials of the Hue bridge to control the lights.

![HueMix Link Website](website-screenshot.png)

## Linux Service Setup

To set up the Linux service for hosting the HueMix Link website and running the TCP server, follow these steps:
#### TCP service:
1. Run `sudo nano /etc/systemd/system/huemixlinktcp.service`
2. Paste code provided at [HueMix-Link TCP service](../main/systemd-services/huemixlinktcp.service).
3. Change paths as necessary then save and close the file.
4. Run `sudo systemctl enable huemixlinktcp` and `sudo systemctl start huemixlinktcp`. 
5. TCP server should be up and running. To check status run `sudo systemctl status huemixlinktcp`.
#### Website service:
1. Run `sudo nano /etc/systemd/system/huemixlinkwebsite.service`
2. Paste code provided at [HueMix-Link Website service](../main/systemd-services/huemixlinkwebsite.service).
3. Change paths as necessary then save and close the file.
4. Run `sudo systemctl enable huemixlinkwebsite` and `sudo systemctl start huemixlinkwebsite`. 
5. Website server should be up and running. To check status run `sudo systemctl status huemixlinkwebsite`.

## 3D Model
- This 3D model represents the physical design for the HueMix-Link button and server.
- Dimensions: 10cm x 5cm x 2cm
- Works with the provided PCB layout.

![HueMix-Link 3D Model](3d-model.png)

## PCB and Schematics

If you are interested in printing the PCB and reviewing the schematics, you can find the necessary files and resources below:
#### Button PCB:
- [PCB Image](Schematics%20and%20PCB/esp_now_button/esp_now_button_front.png): View the image of the PCB designs.
- [Schematics PDF](Schematics%20and%20PCB/esp_now_button/esp_now_button.pdf): Access the detailed schematics in PDF format.
- [KiCad Files](Schematics%20and%20PCB/esp_now_button/): Download the KiCad project files for further exploration and customization.
#### Server PCB:
- [PCB Image](Schematics%20and%20PCB/esp_now_server/esp_now_button_front.png): View the image of the PCB designs.
- [Schematics PDF](Schematics%20and%20PCB/esp_now_server/esp_now_button.pdf): Access the detailed schematics in PDF format.
- [KiCad Files](Schematics%20and%20PCB/esp_now_server/): Download the KiCad project files for further exploration and customization.

Feel free to utilize these resources to understand the PCB design and schematics, make modifications if needed, and proceed with printing the PCB for your project.
