"""Controller for color space conversions and color utilities."""
import hashlib
import math
from typing import Dict, List, Tuple, Union
from rgbxy import Converter, GamutA, GamutB, GamutC
import logging
import colorsys

logger = logging.getLogger(__name__)

def get_light_gamut(modelId):
    if modelId in ('LST001', 'LLC010', 'LLC011', 'LLC012', 'LLC005', 'LLC006', 'LLC007', 'LLC013', 'LLC014'):
        return GamutA
    elif modelId in ('LCT001', 'LCT007', 'LCT002', 'LCT003', 'LLM001', 'LCA005'):
        return GamutB
    elif modelId in ('LCT010', 'LCT014', 'LCT015', 'LCT016', 'LCT011', 'LLC020', 'LST002', 'LCT012', 'LCL001', 'LCA003', '440400982841'):
        return GamutC
    else:
        logger.debug(f"Unknown light model ID '{modelId}', defaulting to Gamut C")
        return GamutC  # Default to Gamut C for unknown models


class ColorController:
    """
    Handles color space conversions between XY, RGB, HSV, and Color Temperature.
    Optimized for Philips Hue color gamut.
    """
    
    @staticmethod
    def xy_to_rgb(x: float, y: float, light_type) -> Dict[str, int]:
        """
        Convert CIE 1931 XY color space to RGB.
        
        Args:
            x: X coordinate (0.0 - 1.0)
            y: Y coordinate (0.0 - 1.0)
            light_type: Hue bulb gamut ('A', 'B', or 'C')
        
        Returns:
            Tuple of (r, g, b) values (0-255)
        """

        converter = Converter(gamut=get_light_gamut(light_type))
        r, g, b = converter.xy_to_rgb(x, y)
        
        return {'r': r, 'g': g, 'b': b}
    
    @staticmethod
    def ct_to_rgb(ct: int) -> Tuple[int, int, int]:
        """
        Convert color temperature (Kelvin/Mired) to RGB.
        
        Args:
            ct: Color temperature in Mired (153-500) or Kelvin (2000-6500)
        
        Returns:
            Tuple of (r, g, b) values (0-255)
        """
            
        ANCHORS = [
            (500, 255, 150, 20),  
            (370, 255, 200, 65),  
            (333, 255, 225, 110),  
            (250, 255, 255, 200),  
            (200, 250, 255, 235),  
            (153, 255, 255, 215),  
        ]

        if ct > 500: ct = 500
        if ct < 153: ct = 153

        # Find which two points we are between
        upper = ANCHORS[0]
        lower = ANCHORS[-1]

        # Scan the list to find the upper and lower bounds
        for i in range(len(ANCHORS) - 1):
            curr_point = ANCHORS[i]
            next_point = ANCHORS[i+1]
            
            # The list goes from High Mired (Warm) to Low Mired (Cool)
            if curr_point[0] >= ct >= next_point[0]:
                upper = curr_point
                lower = next_point
                break

        # Linear Interpolation (Lerp) logic
        mired_range = upper[0] - lower[0]
        if mired_range == 0: return upper[1:] # Avoid division by zero

        # How far are we between the two points? (0.0 to 1.0)
        fraction = (upper[0] - ct) / mired_range

        r = upper[1] + (lower[1] - upper[1]) * fraction
        g = upper[2] + (lower[2] - upper[2]) * fraction
        b = upper[3] + (lower[3] - upper[3]) * fraction

        return round(r), round(g), round(b)
    
    @staticmethod
    def rgb_to_hex(r: int, g: int, b: int) -> str:
        """
        Convert RGB to hex color string.
        
        Args:
            r: Red value (0-255)
            g: Green value (0-255)
            b: Blue value (0-255)
        
        Returns:
            Hex color string (e.g., "#FF0000")
        """
        return f"#{r:02x}{g:02x}{b:02x}"
    
    @staticmethod
    def hex_to_rgb(hex_color: str) -> Dict[str, int]:
        """
        Convert hex color string to RGB.
        
        Args:
            hex_color: Hex color string (e.g., "#FF0000" or "FF0000")
        
        Returns:
            Dict with r, g, b values (0-255)
        """
        hex_color = hex_color.lstrip('#')
        return {
            'r': int(hex_color[0:2], 16),
            'g': int(hex_color[2:4], 16),
            'b': int(hex_color[4:6], 16)
        }
    
    @staticmethod
    def rgb_to_hsv(r: int, g: int, b: int) -> Dict[str, float]:
        """
        Convert RGB to HSV.
        
        Args:
            r: Red value (0-255)
            g: Green value (0-255)
            b: Blue value (0-255)
        
        Returns:
            Dict with h (0-360), s (0-100), v (0-100)
        """
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        return {'h': round(h * 360, 2), 's': round(s * 100, 2), 'v': round(v * 100, 2)}
    
    @staticmethod
    def hsv_to_rgb(h: float, s: float, v: float) -> Dict[str, int]:
        r, g, b = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, v / 100.0)
        
        return {
            'r': int(r * 255),
            'g': int(g * 255),
            'b': int(b * 255)
        }
    
    @staticmethod
    def get_color_name(r: int, g: int, b: int) -> str:
        """
        Get a human-readable color name from RGB.
        
        Args:
            r: Red value (0-255)
            g: Green value (0-255)
            b: Blue value (0-255)
        
        Returns:
            Color name string
        """
        hsv = ColorController.rgb_to_hsv(r, g, b)
        h, s, v = hsv['h'], hsv['s'], hsv['v']
        
        # Check for grayscale
        if s < 10:
            if v < 20:
                return "Black"
            elif v < 40:
                return "Dark Gray"
            elif v < 60:
                return "Gray"
            elif v < 80:
                return "Light Gray"
            else:
                return "White"
        
        # Color names based on hue
        if h < 15 or h >= 345:
            return "Red"
        elif h < 45:
            return "Orange"
        elif h < 70:
            return "Yellow"
        elif h < 150:
            return "Green"
        elif h < 190:
            return "Cyan"
        elif h < 270:
            return "Blue"
        elif h < 330:
            return "Purple"
        else:
            return "Pink"

    @staticmethod
    def sort_palette_by_hue(palette: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
        return sorted(palette, key=lambda c: colorsys.rgb_to_hsv(c[0]/255, c[1]/255, c[2]/255)[0])
    
    @staticmethod
    def lerp_color(c1: Tuple[int, int, int], c2: Tuple[int, int, int], fraction: float) -> Tuple[int, int, int]:
        """Simple RGB linear interpolation."""
        r = int(c1[0] + (c2[0] - c1[0]) * fraction)
        g = int(c1[1] + (c2[1] - c1[1]) * fraction)
        b = int(c1[2] + (c2[2] - c1[2]) * fraction)
        return (r, g, b)
    
    @staticmethod
    def get_color_from_palette(palette: List[Tuple[int, int, int]], pos: float, distortion: float) -> Tuple[int, int, int]:
        """Maps a 0.0-1.0 value to the palette, treating it as a circular loop."""
        pos = pos % 1.0
        n = len(palette)
        float_index = pos * n
        index1 = int(float_index) % n
        index2 = (index1 + 1) % n
        fraction = float_index - int(float_index)

        if distortion < 1.0:
            # We use a power function to create a "steep" transition.
            # As distortion approaches 0, the 'p' exponent grows, making the middle transition faster.
            p = 1.0 / max(distortion, 0.01) 
            if fraction < 0.5:
                fraction = 0.5 * math.pow(2 * fraction, p)
            else:
                fraction = 1.0 - 0.5 * math.pow(2 * (1.0 - fraction), p)

        
        return ColorController.lerp_color(palette[index1], palette[index2], fraction)
    
    @staticmethod
    def _ensure_int_seed(seed: Union[int, str]) -> int:
        """Converts macAddress to a stable integer seed."""
        if isinstance(seed, str):
            return int(hashlib.sha256(seed.encode()).hexdigest(), 16)
        return seed
    
    @staticmethod
    def generate_strip(
        palette: List[Tuple[int, int, int]], 
        num_leds: int, 
        seed: Union[int, str] = 0, 
        coverage: float = 1.5,
        distortion: float = 0.3
    ) -> List[Tuple[int, int, int]]:
        """
        Args:
            palette: RGB colors
            num_leds: Length of the strip
            seed: Unique ID for the light (must be an integer)
            coverage: How many times the pattern 'cycles' through the palette.
                      1.0 = strip shows the palette once. 2.0 = cycles through it twice.
        """
        sorted_palette = ColorController.sort_palette_by_hue(palette)
        strip = []

        numeric_seed = ColorController._ensure_int_seed(seed)
        offset = (numeric_seed * 0.618033) % 1.0 

        for i in range(num_leds):
            t = i / num_leds

            pos = offset + (t * coverage)
            wiggle = math.sin(t * math.pi * 2 + (numeric_seed * 1.5))
            pos += wiggle
            
            color = ColorController.get_color_from_palette(sorted_palette, pos, distortion)
            strip.append(color)
            
        return strip


# Global singleton instance
color_controller = ColorController()
