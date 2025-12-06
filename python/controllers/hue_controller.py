from typing import List, Dict
import urllib3
import requests

class Hue:
    def __init__(self, bridge_ip, token):
        self.bridge_ip = bridge_ip
        self.token = token
        self.base_url = f"https://{self.bridge_ip}/clip/v2/resource"
        self.headers = {
            "hue-application-key": self.token,
        }

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get_resource(self, resource_type, resource_id=None):
        url = f"{self.base_url}/{resource_type}"
        if resource_id:
            url = f"{url}/{resource_id}"
        response = requests.get(url, headers=self.headers, verify=False, timeout=5)
        response.raise_for_status()

        response_data = response.json()

        if len(response_data.get('errors', [])) == 1:
            raise ConnectionError(response_data['error'])
        
        data = response_data.get('data', {} if resource_id else [])
        
        # If resource_id is specified and data is a list, return the first item
        if resource_id and isinstance(data, list) and len(data) > 0:
            return data[0]
        
        return data
    
    def _put_resource(self, resource_type, resource_id, payload):
        url = f"{self.base_url}/{resource_type}/{resource_id}"
        response = requests.put(url, headers=self.headers, json=payload, verify=False, timeout=5)
        response.raise_for_status()

        response_data = response.json()

        if len(response_data.get('errors', [])) == 1:
            raise ConnectionError(response_data['error'])
        return response_data.get('data', {})
    
    def _post_resource(self, resource_type, payload):
        url = f"{self.base_url}/{resource_type}"
        response = requests.post(url, headers=self.headers, json=payload, verify=False, timeout=5)
        response.raise_for_status()

        response_data = response.json()

        if len(response_data.get('errors', [])) == 1:
            raise ConnectionError(response_data['error'])
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
        try:
            room = self.get_room(room_id)
            # Check the grouped_light service for the room
            services = room.get('services', [])
            for service in services:
                if service.get('rtype') == 'grouped_light':
                    grouped_light_id = service.get('rid')
                    if grouped_light_id:
                        grouped_light = self.get_grouped_light(grouped_light_id)
                        return grouped_light.get('on', {}).get('on', False)
            return False
        except Exception as e:
            raise Exception(f"Failed to check room status: {e}")

    def get_device_power(self, device_id: str) -> dict:
        return self._get_resource("device_power", device_id)