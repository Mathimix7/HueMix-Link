"""Main application entry point."""
import logging
from services.logging_service import logging_service
from servers.flask_server import app
from services import config_manager, data_manager
from services.home_id_manager import home_id_manager
from services.automation_service import automation_service
from network.network_server import network_server
from network.pairing_manager import pairing_manager
from constants import FILE_BUTTONS, FILE_LIGHTSTRIPS, FILE_GATEWAYS, FILE_BRIDGE, FILE_PAIRING_HISTORY, DEFAULT_WEB_PORT
from waitress import serve
from servers.blueprints.buttons import validate_scenes_at_startup

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        logging_service.start()
        
        defaults = {
            FILE_BUTTONS: [],
            FILE_LIGHTSTRIPS: [],
            FILE_GATEWAYS: [],
            FILE_BRIDGE: {},
            FILE_PAIRING_HISTORY: []
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
        
        network_server.start()
        
        # Set network server for automation service and start
        logger.info("Starting automation service...")
        automation_service.set_network_server(network_server)
        automation_service.start()
        
        # This cleans up any scenes that were deleted while the system was offline
        logger.info("Validating button scene configurations...")
        try:
            validate_scenes_at_startup()
        except Exception as e:
            logger.error(f"Error during startup scene validation: {e}", exc_info=True)
        
        # Start Flask web server (blocking) — bind to localhost only; proxy will expose externally
        logger.info(f"Starting Flask web server on port {DEFAULT_WEB_PORT} (127.0.0.1)...")
        serve(app, host='127.0.0.1', port=DEFAULT_WEB_PORT, threads=8)
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Error starting servers: {e}", exc_info=True)
    finally:
        try:
            automation_service.stop()
        except Exception as e:
            logger.error(f"Error stopping automation service: {e}")
        
        logger.info("Stopping network server...")
        try:
            network_server.stop()
        except Exception as e:
            logger.error(f"Error stopping network server: {e}")
