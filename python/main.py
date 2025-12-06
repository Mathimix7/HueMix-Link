"""Main application entry point."""
import logging
from servers.flask_server import app
from servers.tcp_server import tcp_server
from servers.lightstrip_server import lightstrip_server
from services import config_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        # Load configuration
        tcp_port = config_manager.get_tcp_port()
        web_port = config_manager.get_web_port()
        
        # Set TCP server port
        tcp_server.port = tcp_port
        
        # Start TCP server for ESP32 button communication
        logger.info(f"Starting TCP server on port {tcp_port}...")
        tcp_server.start()
        
        # Start lightstrip server (SSE listener + lightstrip sync service)
        logger.info("Starting lightstrip server...")
        lightstrip_server.start()
        
        # Start Flask web server (blocking)
        logger.info(f"Starting Flask web server on port {web_port}...")
        app.run(debug=True, port=web_port, use_reloader=False)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        lightstrip_server.stop()
        tcp_server.stop()
    except Exception as e:
        logger.error(f"Error starting servers: {e}")
        lightstrip_server.stop()
        tcp_server.stop()
        raise
