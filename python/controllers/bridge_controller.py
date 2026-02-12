import requests
import json
from pathlib import Path
import urllib3
from controllers.hue_controller import Hue
from constants import FILE_BRIDGE

# Disable SSL warnings for Hue bridge (uses self-signed certificates)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class BridgeController:
    def __init__(self, config_file=None):
        """Initialize the Bridge Controller"""
        if config_file is None:
            self.config_file = Path(__file__).parent.parent / 'data' / FILE_BRIDGE
        else:
            self.config_file = Path(config_file)
        
        self.config = self.load_config()
    
    def load_config(self):
        """Load bridge configuration from file"""
        if self.config_file.exists():
            with open(self.config_file, 'r') as f:
                return json.load(f)
        return None
    
    def save_config(self, config):
        """Save bridge configuration to file"""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)
        self.config = config
    
    def delete_config(self):
        """Delete the bridge configuration"""
        if self.config_file.exists():
            self.config_file.unlink()
        self.config = None
    
    def discover_bridges(self, timeout=5):
        """Discover Hue bridges via Philips discovery service"""
        try:
            response = requests.get('https://discovery.meethue.com/', timeout=timeout)
            if response.status_code == 200:
                bridges = response.json()
                if bridges:
                    return {'success': True, 'bridges': bridges}
            
            return {'success': False, 'error': 'No bridges found. Please enter IP manually.'}
        except Exception as e:
            return {'success': False, 'error': f'Discovery failed: {str(e)}'}
    
    def verify_bridge(self, ip, timeout=5):
        """Verify that an IP address is a Hue bridge"""
        try:
            result = requests.get(url=f"https://{ip}/api/newdeveloper", timeout=timeout, verify=False).json()
            if result == [{"error":{"type":1,"address":"/","description":"unauthorized user"}}]:
                return {
                    'success': True,
                    'bridge': {
                        'ip': ip,
                        'verified': True
                    }
                }
            return {'success': False, 'error': 'Not a valid Hue bridge'}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': f'Connection failed: {str(e)}'}
        except json.JSONDecodeError:
            return {'success': False, 'error': 'Invalid response from device'}
    
    def pair_bridge(self, ip, app_name='hue_mix_link', device_name='server', timeout=10):
        """Create a new user on the Hue bridge (requires button press)"""
        try:
            payload = {
                'devicetype': f'{app_name}#{device_name}'
            }
            
            response = requests.post(f'https://{ip}/api', json=payload, timeout=timeout, verify=False)
            
            if response.status_code == 200:
                result = response.json()
                
                if isinstance(result, list) and len(result) > 0:
                    first_result = result[0]
                    
                    # Check for success
                    if 'success' in first_result:
                        username = first_result['success']['username']
                        
                        # Save bridge configuration
                        config = {
                            'ip': ip,
                            'username': username,
                            'app_name': app_name,
                            'device_name': device_name
                        }
                        self.save_config(config)
                        
                        return {
                            'success': True,
                            'username': username,
                            'message': 'Bridge paired successfully!'
                        }
                    
                    # Check for error (button not pressed)
                    elif 'error' in first_result:
                        error = first_result['error']
                        if error.get('type') == 101:
                            return {
                                'success': False,
                                'error': 'Link button not pressed',
                                'button_required': True
                            }
                        else:
                            return {
                                'success': False,
                                'error': error.get('description', 'Unknown error')
                            }
            
            return {'success': False, 'error': 'Unexpected response from bridge'}
        
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': f'Connection failed: {str(e)}'}
    
    def get_config_with_status(self, timeout=5):
        """Get the current bridge configuration with connection status"""
        if self.config:
            # Test connection to bridge
            try:
                response = requests.get(
                    f"https://{self.config['ip']}/api/{self.config['username']}/config",
                    timeout=timeout,
                    verify=False
                )
                if response.status_code == 200:
                    bridge_info = response.json()
                    return {
                        'success': True,
                        'configured': True,
                        'config': {
                            'ip': self.config['ip'],
                            'name': bridge_info.get('name'),
                            'bridgeid': bridge_info.get('bridgeid'),
                            'connected': True
                        }
                    }
            except:
                pass
            
            # If connection failed but we have config
            return {
                'success': True,
                'configured': True,
                'config': {
                    'ip': self.config['ip'],
                    'connected': False
                }
            }
        
        return {
            'success': True,
            'configured': False
        }
    
    def test_connection(self):
        """Test the bridge connection by fetching lights"""
        if not self.config:
            return {'success': False, 'error': 'Bridge not configured'}
        
        try:
            hue = Hue(self.config['ip'], self.config['username'])
            try:
                response = hue.get_lights()
                return {
                    'success': True,
                    'connected': True,
                    'light_count': len(response)
                }
            except Exception as e:
                return {
                    'success': False,
                    'error': 'Failed to connect to bridge'
                }
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': f'Connection failed: {str(e)}'}