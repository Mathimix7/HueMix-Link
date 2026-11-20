"""
Hue Service - Manages Hue controller instance lifecycle.

Initializes and reinitializes the Hue controller when bridge configuration changes.
"""
import logging
from typing import Optional
from controllers.hue_controller import Hue
from controllers.bridge_controller import BridgeController
from services.config_change_notifier import config_notifier

logger = logging.getLogger(__name__)


class HueService:
    """Manages Hue controller lifecycle with automatic reinitialization on config changes."""
    
    def __init__(self):
        """Initialize Hue service."""
        self._hue: Optional[Hue] = None
        self._bridge_controller = BridgeController()
        self._initialized = False
        
        # Subscribe to bridge config changes
        config_notifier.subscribe('bridge_config', self._on_bridge_config_changed)
        config_notifier.subscribe('bridge_config_deleted', self._on_bridge_config_deleted)
        
        # Initialize immediately
        self._initialize_hue()
        
        logger.info("HueService initialized")
    
    def _on_bridge_config_changed(self, notification):
        """Handle bridge config change notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        logger.info("Bridge configuration changed, reinitializing Hue controller...")
        self._initialize_hue()
    
    def _on_bridge_config_deleted(self, notification):
        """Handle bridge config deletion notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        logger.info("Bridge configuration deleted, clearing Hue controller...")
        self._hue = None
        self._initialized = False
    
    def _initialize_hue(self):
        """Initialize or reinitialize the Hue controller."""
        try:
            bridge_config = self._bridge_controller.load_config()
            
            if bridge_config and bridge_config.get('ip') and bridge_config.get('username'):
                logger.info(f"Initializing Hue controller with bridge at {bridge_config['ip']}...")
                self._hue = Hue(bridge_config['ip'], bridge_config['username'])
                self._initialized = True
                logger.info("Hue controller initialized successfully")
            else:
                logger.warning("Hue Bridge not configured - controller not available")
                self._hue = None
                self._initialized = False
                
        except Exception as e:
            logger.error(f"Failed to initialize Hue controller: {e}", exc_info=True)
            self._hue = None
            self._initialized = False
    
    def get_controller(self) -> Optional[Hue]:
        """Get the current Hue controller instance.
        
        Returns:
            Hue controller instance or None if not configured
        """
        return self._hue
    
    def is_initialized(self) -> bool:
        """Check if Hue controller is initialized and ready.
        
        Returns:
            True if controller is ready, False otherwise
        """
        return self._initialized and self._hue is not None
    
    def reinitialize(self):
        """Manually trigger reinitialization of the Hue controller."""
        logger.info("Manual reinitialization requested")
        self._initialize_hue()


# Global singleton instance
hue_service = HueService()
