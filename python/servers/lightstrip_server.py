"""Lightstrip server that manages SSE listener and lightstrip sync services."""
import logging
from services.lightstrip_service import lightstrip_service
from services.hue_sse_listener import hue_sse_listener
from services.hue_state_manager import hue_state_manager
from services import data_manager
from controllers.hue_controller import Hue

logger = logging.getLogger(__name__)


class LightstripServer:
    """
    Server that manages background services for lightstrip synchronization.
    Coordinates the SSE listener and lightstrip service.
    """
    
    def __init__(self):
        self._running = False
        logger.info("LightstripServer initialized")
    
    def start(self):
        """Start all background services."""
        if self._running:
            logger.warning("LightstripServer already running")
            return
        
        try:
            logger.info("Starting lightstrip background services...")
            
            # Initialize state manager with current Hue data
            config = data_manager.read_json('bridge.json', default={})
            hue = None
            if config and config.get('ip') and config.get('username'):
                try:
                    logger.info("Initializing state manager with current Hue data...")
                    hue = Hue(config['ip'], config['username'])
                    hue_state_manager.initialize_from_bridge(hue)
                    logger.info("State manager initialized successfully")
                except Exception as e:
                    logger.warning(f"Could not initialize state manager: {e}")
                    logger.info("Continuing without initial state (will sync via SSE)")
            else:
                logger.warning("Bridge not configured, skipping state initialization")
            
            # Start lightstrip service (subscribes to state manager)
            lightstrip_service.start()
            
            # Start SSE listener (updates state manager from Hue Bridge)
            # Pass hue controller so it can fetch scenes when needed
            hue_sse_listener.start(hue_controller=hue)
            
            self._running = True
            logger.info("LightstripServer started successfully")
        
        except Exception as e:
            logger.error(f"Error starting LightstripServer: {e}")
            # Try to clean up if something failed
            self.stop()
            raise
    
    def stop(self):
        """Stop all background services."""
        if not self._running:
            return
        
        try:
            logger.info("Stopping lightstrip background services...")
            
            # Stop services in reverse order
            hue_sse_listener.stop()
            lightstrip_service.stop()
            
            self._running = False
            logger.info("LightstripServer stopped successfully")
        
        except Exception as e:
            logger.error(f"Error stopping LightstripServer: {e}")
    
    @property
    def is_running(self):
        """Check if server is running."""
        return self._running


# Global singleton instance
lightstrip_server = LightstripServer()
