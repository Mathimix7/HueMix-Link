"""
Automation Service - Manages automation engine lifecycle.

Initializes and reinitializes the automation engine when Hue bridge or config changes.
"""
import logging
from typing import Optional
from network.automation_engine import AutomationEngine
from services.hue_service import hue_service
from services.config_change_notifier import config_notifier
from services.hue_state_manager import hue_state_manager
from services.hue_sse_listener import hue_sse_listener

logger = logging.getLogger(__name__)


class AutomationService:
    """Manages automation engine lifecycle with automatic reinitialization."""
    
    def __init__(self):
        """Initialize automation service."""
        self._engine: Optional[AutomationEngine] = None
        self._network_server = None
        self._initialized = False
        
        # Subscribe to config changes
        config_notifier.subscribe('bridge_config', self._on_bridge_config_changed)
        config_notifier.subscribe('bridge_config_deleted', self._on_bridge_config_deleted)
        
        logger.info("AutomationService initialized")
    
    def _on_bridge_config_changed(self, notification):
        """Handle bridge config change notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        logger.info("Bridge configuration changed, reinitializing automation engine...")
        self._initialize_engine()
    
    def _on_bridge_config_deleted(self, notification):
        """Handle bridge config deletion notification.
        
        Args:
            notification: Notification dict with type, data, timestamp
        """
        logger.info("Bridge configuration deleted, stopping automation engine...")
        if self._engine:
            self._engine.stop()
            self._engine = None
        self._initialized = False
    
    def set_network_server(self, network_server):
        """Set network server for automation engine.
        
        Args:
            network_server: NetworkServer instance
        """
        self._network_server = network_server
        
        # If engine already exists, update its network server
        if self._engine:
            self._engine.set_network_server(network_server)
            self._network_server.set_button_event_handler(self._engine.handle_button_event)
            self._network_server.set_motion_event_handler(self._engine.handle_motion_event)
            self._network_server.set_door_event_handler(self._engine.handle_door_event)
            self._network_server.set_automation_engine(self._engine)
            logger.info("Network server updated in automation engine")
    
    def _initialize_engine(self):
        """Initialize or reinitialize the automation engine."""
        try:
            # Get Hue controller from service
            hue_controller = hue_service.get_controller()
            
            if not hue_controller:
                logger.warning("Hue controller not available - automation engine not started")
                if self._engine:
                    logger.info("Stopping existing automation engine...")
                    self._engine.stop()
                    self._engine = None
                self._initialized = False
                return
            
            # Stop existing engine if running
            if self._engine:
                logger.info("Stopping existing automation engine...")
                self._engine.stop()
            
            # Initialize state manager with bridge data
            logger.info("Initializing HueStateManager from bridge...")
            hue_state_manager.initialize_from_bridge(hue_controller)
            
            # Start SSE listener for real-time updates
            logger.info("Starting Hue SSE listener...")
            hue_sse_listener.start(hue_controller=hue_controller)
            
            # Create new engine
            logger.info("Creating automation engine...")
            self._engine = AutomationEngine(hue_controller)
            
            # Connect network server if available
            if self._network_server:
                self._engine.set_network_server(self._network_server)
                self._network_server.set_button_event_handler(self._engine.handle_button_event)
                self._network_server.set_motion_event_handler(self._engine.handle_motion_event)
                self._network_server.set_door_event_handler(self._engine.handle_door_event)
                self._network_server.set_automation_engine(self._engine)
            
            # Start the engine
            self._engine.start()
            self._initialized = True
            
            logger.info("Automation engine initialized and started successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize automation engine: {e}", exc_info=True)
            self._engine = None
            self._initialized = False
    
    def start(self):
        """Start the automation service and engine."""
        if self._initialized:
            logger.warning("Automation engine already running")
            return
        
        self._initialize_engine()
    
    def stop(self):
        """Stop the automation engine."""
        if self._engine:
            logger.info("Stopping automation engine...")
            self._engine.stop()
            self._engine = None
            self._initialized = False
    
    def get_engine(self) -> Optional[AutomationEngine]:
        """Get the current automation engine instance.
        
        Returns:
            AutomationEngine instance or None if not initialized
        """
        return self._engine
    
    def is_initialized(self) -> bool:
        """Check if automation engine is initialized and ready.
        
        Returns:
            True if engine is ready, False otherwise
        """
        return self._initialized and self._engine is not None


# Global singleton instance
automation_service = AutomationService()
