<p align="center"><img src="" alt="HueMix-Link's logo"/></p>

<h1 align="center">HueMix-Link</h1>

## Project Overview

HueMix Link provides a convenient way to control Hue lights using a physical button. The ESP-based button, configured as an ESP-NOW device, sends data to a server running on another ESP. The server then communicates with a TCP server running on a Linux machine. The Linux machine hosts a website that allows you to configure buttons, servers, the Hue bridge, & more. The project includes various components, such as the ESP code, the TCP server, the website, & more.

## File Organization

The project repository is organized as follows:

- DataFiles/: Contains JSON files for storing device, MAC addresses, server, and settings information.
- static/: Holds CSS files and other static assets for the website.
- templates/: Contains HTML templates for the website.
- HueActivations.py: Python script for managing Hue light activations.
- tcp_server.py: TCP server implementation for receiving data from the ESP server.
- HueConfig.py: Python script for handling Hue configurations.
- website.py: Python script for running the website.
- utils.py: Utility functions used by various components.

## Setup and Configuration

To set up the HueMix Link project, follow these steps:

1. Connect the ESP-based button according to the provided PCB and wire connections.
2. Flash the button.ino sketch to the ESP-based button.
3. Flash the server.ino sketch to the ESP server.
4. Run the tcp_server.py script on the Linux machine to start the TCP server.
5. Configure the necessary settings in the settings.json file located in the DataFiles/ directory.
6. Run the website.py script on the Linux machine to start the website.

## Usage

Once the project is set up and configured, follow these steps to use HueMix Link:

1. Access the website hosted on the Linux machine.
2. Use the website's interface to configure buttons, servers, and the Hue bridge.
3. Press the physical button connected to the ESP-based button to control the Hue lights. :bulb:

## Website

The HueMix Link website provides a user-friendly interface for configuring buttons, servers, and the Hue bridge. It allows you to:

- :gear: Add and manage buttons: Assign actions to different buttons and customize their behavior.
- :satellite: Configure servers: Define the ESP server's IP address and port to establish communication with the TCP server.
- :bulb: Set up the Hue bridge: Specify the IP address and authentication credentials of the Hue bridge to control the lights.

![HueMix Link Website](website-screenshot.png)

## Linux Server Setup

To set up the Linux server for hosting the HueMix Link website and running the TCP server, follow these steps:

1. Install the required dependencies by running the setup.sh script provided in the repository.
2. Modify the config.ini file to specify the necessary configurations for the TCP server.
3. Run the tcp_server.py script to start the TCP server.

## Contributing

Contributions to HueMix Link are welcome! If you would like to contribute, please follow these steps:

1. Fork the repository.
2. Create a new branch for your feature or bug fix.
3. Make your changes and commit them with descriptive commit messages.
4. Push your changes to your forked repository.
5. Open a pull request, providing a detailed description of your changes.

## License

This project is licensed under the MIT License.
