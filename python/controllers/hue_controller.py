from typing import List, Dict
import requests
import time
import logging
import urllib3
from constants import TIMEOUT_HTTP_REQUEST, HTTP_MAX_RETRIES, HTTP_RETRY_BACKOFF_BASE

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class Hue:
    def __init__(self, bridge_ip, token):
        self.bridge_ip = bridge_ip
        self.token = token
        self.base_url = f"https://{self.bridge_ip}/clip/v2/resource"
        
        # Create a session for connection pooling and SSL configuration
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "hue-application-key": self.token,
        })
        
    def _make_request(self, method, url, json_data=None, max_retries=HTTP_MAX_RETRIES, timeout=TIMEOUT_HTTP_REQUEST):
        """Make HTTP request with retry logic.
        
        Args:
            method: HTTP method (GET, PUT, POST)
            url: Full URL to request
            json_data: JSON payload for PUT/POST
            max_retries: Maximum number of retry attempts
            timeout: Request timeout in seconds
            
        Returns:
            Response data dict
            
        Raises:
            ConnectionError: If all retries fail
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if method == 'GET':
                    response = self.session.get(url, timeout=timeout)
                elif method == 'PUT':
                    response = self.session.put(url, json=json_data, timeout=timeout)
                elif method == 'POST':
                    response = self.session.post(url, json=json_data, timeout=timeout)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                
                response.raise_for_status()
                response_data = response.json()
                
                if len(response_data.get('errors', [])) == 1:
                    raise ConnectionError(response_data['errors'][0])
                
                return response_data
                
            except (requests.exceptions.RequestException, ConnectionError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    # Exponential backoff
                    sleep_time = HTTP_RETRY_BACKOFF_BASE * (2 ** attempt)
                    logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Request failed after {max_retries} attempts: {e}")
        
        raise ConnectionError(f"Failed after {max_retries} retries: {last_error}")

    def _get_resource(self, resource_type, resource_id=None):
        url = f"{self.base_url}/{resource_type}"
        if resource_id:
            url = f"{url}/{resource_id}"
        
        response_data = self._make_request('GET', url)
        data = response_data.get('data', {} if resource_id else [])
        
        # If resource_id is specified and data is a list, return the first item
        if resource_id and isinstance(data, list) and len(data) > 0:
            return data[0]
        
        return data
    
    def _put_resource(self, resource_type, resource_id, payload):
        url = f"{self.base_url}/{resource_type}/{resource_id}"
        response_data = self._make_request('PUT', url, json_data=payload)
        return response_data.get('data', {})
    
    def _post_resource(self, resource_type, payload):
        url = f"{self.base_url}/{resource_type}"
        response_data = self._make_request('POST', url, json_data=payload)
        return response_data.get('data', [])

    def get_lights(self) -> List[dict]:
        return self._get_resource("light")
    
    def get_light(self, light_id: str) -> dict:
        return self._get_resource("light", light_id)
    
    def set_light(self, light_id: str, payload: Dict) -> dict:
        return self._put_resource("light", light_id, payload)
    
    def get_grouped_lights(self) -> List[dict]:
        return self._get_resource("grouped_light")
    
    def get_grouped_light(self, grouped_light_id: str) -> dict:
        return self._get_resource("grouped_light", grouped_light_id)
    
    def get_rooms(self) -> List[dict]:
        return self._get_resource("room")
    
    def get_room(self, room_id: str) -> dict:
        return self._get_resource("room", room_id)
    
    def set_room(self, room_id: str, payload: Dict) -> dict:
        return self._put_resource("room", room_id, payload)
    
    def get_zones(self) -> List[dict]:
        return self._get_resource("zone")
    
    def get_zone(self, zone_id: str) -> dict:
        return self._get_resource("zone", zone_id)
    
    def set_zone(self, zone_id: str, payload: Dict) -> dict:
        return self._put_resource("zone", zone_id, payload)
    
    def get_group(self, group_id: str, group_type: str = 'room') -> dict:
        """Get a room or zone resource based on group_type."""
        if group_type == 'zone':
            return self.get_zone(group_id)
        return self.get_room(group_id)
    
    def get_scenes(self) -> List[dict]:
        return self._get_resource("scene")
    
    def get_scene(self, scene_id: str) -> dict:
        return self._get_resource("scene", scene_id)
    
    def set_scene(self, scene_id: str, payload: Dict) -> dict:
        return self._put_resource("scene", scene_id, payload)
    
    def get_bridge_home(self) -> dict:
        return self._get_resource("bridge_home")
    
    def get_devices(self) -> List[dict]:
        return self._get_resource("device")
    
    def get_device(self, device_id: str) -> dict:
        return self._get_resource("device", device_id)
    
    def get_bridge(self) -> dict:
        return self._get_resource("bridge")
    
    def get_devices_power(self) -> List[dict]:
        return self._get_resource("device_power")
    
    def is_room_on(self, room_id: str) -> bool:
        """
        Check if any lights in a room are currently on.
        
        Args:
            room_id: The room ID to check
            
        Returns:
            True if any light in the room is on, False otherwise
        """
        return self.is_group_on(room_id, 'room')
    
    def is_group_on(self, group_id: str, group_type: str = 'room') -> bool:
        """
        Check if any lights in a group (room or zone) are currently on.
        
        Args:
            group_id: The room/zone ID to check
            group_type: 'room' or 'zone'
            
        Returns:
            True if any light in the group is on, False otherwise
        """
        try:
            group = self.get_group(group_id, group_type)
            # Check the grouped_light service for the group
            services = group.get('services', [])
            for service in services:
                if service.get('rtype') == 'grouped_light':
                    grouped_light_id = service.get('rid')
                    if grouped_light_id:
                        grouped_light = self.get_grouped_light(grouped_light_id)
                        return grouped_light.get('on', {}).get('on', False)
            return False
        except Exception as e:
            raise Exception(f"Failed to check group status: {e}")

    def get_device_power(self, device_id: str) -> dict:
        return self._get_resource("device_power", device_id)