<p align="center"><img src="" alt="HueMix-Link's logo"/></p>

<h1 align="center">HueMix-Link</h1>

## Project Overview

HueMix Link provides a convenient way to control Hue lights using a physical button. The ESP-based button, configured as an ESP-NOW device, sends data to a server running on another ESP. The server then communicates with a TCP server running on a Linux machine. The Linux machine hosts a website that allows you to configure buttons, servers, the Hue bridge, & more. The project includes various components, such as the ESP code, the TCP server, the website, & more.

<!-- ## File Organization

The project repository is organized as follows:

- DataFiles/: Contains JSON files for storing device, MAC addresses, server, and settings information.
- static/: Holds CSS files and other static assets for the website.
- templates/: Contains HTML templates for the website.
- HueActivations.py: Python script for managing Hue light activations.
- tcp_server.py: TCP server implementation for receiving data from the ESP server.
- HueConfig.py: Python script for handling Hue configurations.
- website.py: Python script for running the website.
- utils.py: Utility functions used by various components. -->

## Setup and Configuration

To set up the HueMix Link project, follow these steps:

1. Run the tcp_server.py script and the website.py on the Linux machine. [Set-up service](https://link-url-here.org)
2. Configure the hue bridge and assign ports according to your preferences.
3. Connect the ESP-based button according to the provided PCB and wire connections.
4. Connect the ESP-based server according to the provided PCB and wire connections.
5. Flash the esp_now_server.ino sketch to the ESP NOW server and tcp_client_server.ino sketch to the TCP server.
6. Wait until blue led is flashing, connect to "HueMix Link - id" network with password "HueMixLink". Follow the steps at "192.168.4.1".
7. Flash the esp_now_button.ino sketch to the ESP-based button (esp8266 version does not include deep sleep).
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

## Linux Server Setup

To set up the Linux server for hosting the HueMix Link website and running the TCP server, follow these steps:

1. Run `sudo nano /etc/systemd/system/huemixlinktcp.service`
2. Paste code provided at [HueMix-Link TCP service](../systemd-services/huemixlinktcp.service).
3. Change paths as necessary then save and close the file.
4. Run `sudo systemctl enable huemixlinktcp` and `sudo systemctl start huemixlinktcp`. 
5. TCP server should be up and running. To check status run `sudo systemctl status huemixlinktcp`.

## Contributing

Contributions to HueMix Link are welcome! If you would like to contribute, please follow these steps:

1. Fork the repository.
2. Create a new branch for your feature or bug fix.
3. Make your changes and commit them with descriptive commit messages.
4. Push your changes to your forked repository.
5. Open a pull request, providing a detailed description of your changes.

## License

This project is licensed under the MIT License.
