<h1 align="center">HueMix-Link</h1>
<p align="center"><img src="/images/logo.png" alt="HueMix-Link's logo", width="250" ></p>

## Project Overview

HueMix Link provides a convenient way to control Hue lights using a physical button. The ESP-based button, configured as an ESP-NOW device, sends data to a server running on another ESP. The server then communicates with a TCP server running on a Linux machine. The Linux machine hosts a website that allows you to configure buttons, servers, the Hue bridge, & more. The project includes various components, such as the ESP code, the TCP server, the website, & more.
## Setup and Configuration

To set up the HueMix Link project, follow these steps:

1. Run the `tcp_server.py` script and the `website.py` on the Linux machine. [Set-up service](#linux-service-setup)
2. Configure the hue bridge and assign ports according to your preferences.
3. Connect the ESP-based button according to the provided PCB and wire connections. [Schematics and PCB](#button-pcb)
4. Connect the ESP-based server according to the provided PCB and wire connections.[Schematics and PCB](#server-pcb)
5. Flash the `esp_now_server.ino` sketch to the ESP NOW server and `tcp_client_server.ino` sketch to the TCP server.
6. Flash the `esp_now_button.ino` sketch to the ESP-based button *(esp8266 version does not include deep sleep)*.
7. Wait until blue led is flashing, connect to ``HueMix Link - id`` network with password ``HueMixLink``. Follow the steps at ``192.168.4.1``.
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

## ESP Server

The ESP server is responsible for receiving data from the ESP-NOW device and sending it to the TCP server. It consists of two ESP devices connected via UART communication. One ESP device receives the data, and the other ESP device sends the data to the TCP server.

### LED Indicators

The ESP server has three LED indicators:

- Blue LED: Indicates the Wi-Fi connection status.
  - On: The device is connected to Wi-Fi.
  - Blinking: The device is in Wi-Fi Manager mode.
- White LED: Indicates data reception via ESP-NOW.
- Red LED: Indicates successful transmission of the message to the TCP server.

### Wi-Fi Manager Mode

When the blue LED is blinking, it means the ESP server is in Wi-Fi Manager mode. Follow these steps to connect to the device:

1. Connect to the network named `HueMix Link - id`.
2. Use the password `HueMixLink`.
3. Access the website at `192.168.4.1`.
4. Enter the Wi-Fi credentials and TCP server IP and port on the website to configure the ESP server.

### Resetting TCP Data and Wi-Fi Credentials

To reset the TCP data or Wi-Fi credentials, follow these steps:

1. Locate the button under the ESP server device.
2. Hold the button for 5 seconds to initiate the reset process.

### Updating Servers

To update all the servers that are running and let the buttons know which servers to connect to, follow these steps:

1. Press the button under the ESP server device once.
   - This action will update the servers that are currently running. This helps the button know which server to connect to in order to get a more reliable connection.

### Server Button

The ESP server also has a button that can be used to control the lights. The main button of the server functions as a normal button.

**Note:** It is recommended to press the button under the device once every time a new server is created to ensure the buttons are aware of the server's existence.

## ESP Button

The ESP button is based on the ESP-NOW protocol and operates in deep sleep mode to optimize battery life. With a single main button, it provides convenient control over the Hue lights. Here are the key features and functionalities of the button:
### Deep Sleep Mode

The ESP-NOW button is designed to operate in deep sleep mode at all times. This feature helps to maximize battery life by minimizing power consumption during idle periods.
### Button Actions

The main button on the ESP button serves various functions:

1. First Click Interaction:
   - If it's the first time the button is clicked and it's in proximity to a server, it automatically gets added to the entire system and the website.
2. Single Click:
   - It cycles through the scenes that can be configured on the website.
   - If the button is clicked again after two seconds of the last click, it turns off the lights or it transitions to the first scene depending on the state of the lights.
3. Holding the Button:
   - Holding the button allows you to increase or decrease the brightness level of the lights.
   - When you start holding for the first time it will always start decreasing the brightness and if you hold again it will increase the brightness. 

### LED Indication

The ESP button features LED indicators to provide feedback on the status of data transmission:

- Single Blink: The LED blinks once to indicate that the message was successfully sent to the server.
- Double Blink: If the LED blinks twice, it means the data was not successfully sent. Please ensure that the server is connected and within close proximity to the button.

Note: It is recommended to check the server connection and ensure they are not too far apart if you encounter issues with data transmission.

By utilizing the ESP button, you can conveniently control the Hue lights and enjoy extended battery life due to the efficient deep sleep mode.

## Linux Service Setup

To set up the Linux service for hosting the HueMix Link website and running the TCP server, follow these steps:
#### TCP service:
1. Run `sudo nano /etc/systemd/system/huemixlinktcp.service`
2. Paste code provided at [HueMix-Link TCP service](systemd-services/huemixlinktcp.service).
3. Change paths as necessary then save and close the file.
4. Run `sudo systemctl enable huemixlinktcp` and `sudo systemctl start huemixlinktcp`. 
5. TCP server should be up and running. To check status run `sudo systemctl status huemixlinktcp`.
#### Website service:
1. Run `sudo nano /etc/systemd/system/huemixlinkwebsite.service`
2. Paste code provided at [HueMix-Link Website service](systemd-services/huemixlinkwebsite.service).
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
- [PCB Image](Schematics%20and%20PCB/esp_now_server/esp_now_server_front.png): View the image of the PCB designs.
- [Schematics PDF](Schematics%20and%20PCB/esp_now_server/esp_now_server.pdf): Access the detailed schematics in PDF format.
- [KiCad Files](Schematics%20and%20PCB/esp_now_server/): Download the KiCad project files for further exploration and customization.