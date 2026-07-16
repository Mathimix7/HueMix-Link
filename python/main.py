"""Main application entry point."""
import logging
from services.logging_service import logging_service
from servers.flask_server import app
from services import config_manager, data_manager
from services.home_id_manager import home_id_manager
from services.automation_service import automation_service
from services.plugin_manager import plugin_manager
from network.network_server import network_server
from network.pairing_manager import pairing_manager
from constants import FILE_BUTTONS, FILE_LIGHTSTRIPS, FILE_GATEWAYS, FILE_BRIDGE, FILE_PAIRING_HISTORY, DEFAULT_WEB_PORT, FILE_MOTION_SENSORS, FILE_DOOR_SENSORS, FILE_PLUGINS
from waitress import serve
from services.hue_config_sync import sync_device_configs_with_hue

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        logging_service.start()
        
        defaults = {
            FILE_BUTTONS: [],
            FILE_LIGHTSTRIPS: [],
            FILE_GATEWAYS: [],
            FILE_BRIDGE: {},
            FILE_PAIRING_HISTORY: [],
            FILE_MOTION_SENSORS: [],
            FILE_DOOR_SENSORS: [],
            FILE_PLUGINS: {'plugins': []},
        }

        for fname, default_content in defaults.items():
            try:
                fp = data_manager._get_filepath(fname)
                if not fp.exists():
                    data_manager.write_json(fname, default_content)
                    logger.info(f"Created missing data file: {fp}")
            except Exception as e:
                logger.error(f"Failed to ensure data file {fname}: {e}")

        home_id_manager.get_or_create_home_id()

        # Load configuration
        udp_port = config_manager.get_udp_port()
        
        # Configure and start UDP network server
        logger.info(f"Starting UDP network server on port {udp_port}...")
        
        # Reconfigure port if different from default
        network_server.udp_port = udp_port
        network_server.sock = None
        
        # Set pairing handler
        network_server.set_pairing_handler(
            lambda mac, dev_type, rssi: pairing_manager.is_pairing_allowed(mac, dev_type, rssi)
        )
        network_server.set_plugin_manager(plugin_manager)
        
        network_server.start()
        
        # Set network server for automation service and start
        logger.info("Starting automation service...")
        automation_service.set_network_server(network_server)
        automation_service.start()
        
        # Reconcile local automation configs with current Hue rooms/scenes.
        logger.info("Synchronizing button/motion/door configurations with Hue...")
        try:
            sync_device_configs_with_hue()
        except Exception as e:
            logger.error(f"Error during startup Hue config sync: {e}", exc_info=True)

        # Load optional plugins after the core services are ready.
        logger.info("Loading optional plugins...")
        plugin_context = plugin_manager.create_host(
            app=app,
            config_manager=config_manager,
            data_manager=data_manager,
            pairing_manager=pairing_manager,
            network_server=network_server,
            packet_transport=network_server,
            automation_service=automation_service,
            home_id_manager=home_id_manager,
            plugin_manager=plugin_manager,
            logger=logger,
        )
        plugin_manager.load_enabled_plugins(plugin_context)
        
        # Start Flask web server (blocking) — bind to localhost only; proxy will expose externally
        logger.info(f"Starting Flask web server on port {DEFAULT_WEB_PORT} (127.0.0.1)...")
        serve(app, host='127.0.0.1', port=DEFAULT_WEB_PORT, threads=8)
        # app.run(host='127.0.0.1', port=DEFAULT_WEB_PORT, debug=False, use_reloader=False)
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Error starting servers: {e}", exc_info=True)
    finally:
        try:
            plugin_manager.stop_loaded_plugins(plugin_context if 'plugin_context' in locals() else None)
        except Exception as e:
            logger.error(f"Error stopping plugins: {e}")

        try:
            automation_service.stop()
        except Exception as e:
            logger.error(f"Error stopping automation service: {e}")
        
        logger.info("Stopping network server...")
        try:
            network_server.stop()
        except Exception as e:
            logger.error(f"Error stopping network server: {e}")
