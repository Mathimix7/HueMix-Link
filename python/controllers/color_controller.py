"""Controller for color space conversions and color utilities."""
import math
from typing import Dict, Tuple, Optional
from rgbxy import Converter, get_light_gamut

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
    def ct_to_xy(ct: int) -> Tuple[float, float]:
        """
        Convert color temperature (Kelvin/Mired) to XY.
        
        Args:
            ct: Color temperature in Mired (153-500) or Kelvin (2000-6500)
        
        Returns:
            Tuple of (x, y) coordinates
        """
        # Convert Mired to Kelvin if needed
        kelvin = int(round(1e6/ct)) - 600
        if kelvin < 1000: 
            kelvin = 1000
        elif kelvin > 40000:
            kelvin = 40000
        tmp_internal = kelvin / 100.0
        if tmp_internal <= 66:
            red = 255
        else:
            tmp_red = 329.698727446 * math.pow(tmp_internal - 60, -0.1332047592)
            if tmp_red < 0:
                red = 0
            elif tmp_red > 255:
                red = 255
            else:
                red = tmp_red
        if tmp_internal <=66:
            tmp_green = 99.4708025861 * math.log(tmp_internal) - 161.1195681661
            if tmp_green < 0:
                green = 0
            elif tmp_green > 255:
                green = 255
            else:
                green = tmp_green
        else:
            tmp_green = 288.1221695283 * math.pow(tmp_internal - 60, -0.0755148492)
            if tmp_green < 0:
                green = 0
            elif tmp_green > 255:
                green = 255
            else:
                green = tmp_green
        if tmp_internal >=66:
            blue = 255
        elif tmp_internal <= 19:
            blue = 0
        else:
            tmp_blue = 138.5177312231 * math.log(tmp_internal - 10) - 305.0447927307
            if tmp_blue < 0:
                blue = 0
            elif tmp_blue > 255:
                blue = 255
            else:
                blue = tmp_blue
        return round(red), round(green), round(blue)
    
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
        r = r / 255.0
        g = g / 255.0
        b = b / 255.0
        
        max_val = max(r, g, b)
        min_val = min(r, g, b)
        diff = max_val - min_val
        
        # Hue calculation
        if diff == 0:
            h = 0
        elif max_val == r:
            h = 60 * (((g - b) / diff) % 6)
        elif max_val == g:
            h = 60 * (((b - r) / diff) + 2)
        else:
            h = 60 * (((r - g) / diff) + 4)
        
        # Saturation calculation
        s = 0 if max_val == 0 else (diff / max_val) * 100
        
        # Value calculation
        v = max_val * 100
        
        return {'h': round(h, 2), 's': round(s, 2), 'v': round(v, 2)}
    
    @staticmethod
    def hsv_to_rgb(h: float, s: float, v: float) -> Dict[str, int]:
        """
        Convert HSV to RGB.
        
        Args:
            h: Hue (0-360)
            s: Saturation (0-100)
            v: Value (0-100)
        
        Returns:
            Dict with r, g, b values (0-255)
        """
        s = s / 100.0
        v = v / 100.0
        
        c = v * s
        x = c * (1 - abs((h / 60) % 2 - 1))
        m = v - c
        
        if 0 <= h < 60:
            r, g, b = c, x, 0
        elif 60 <= h < 120:
            r, g, b = x, c, 0
        elif 120 <= h < 180:
            r, g, b = 0, c, x
        elif 180 <= h < 240:
            r, g, b = 0, x, c
        elif 240 <= h < 300:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        
        return {
            'r': int((r + m) * 255),
            'g': int((g + m) * 255),
            'b': int((b + m) * 255)
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
    def color_distance(color1: Tuple[int, int, int], color2: Tuple[int, int, int]) -> float:
        """
        Calculate Euclidean distance between two RGB colors.
        
        Args:
            color1: First color as (r, g, b) tuple
            color2: Second color as (r, g, b) tuple
        
        Returns:
            Distance between colors
        """
        r1, g1, b1 = color1
        r2, g2, b2 = color2
        return math.sqrt((r2 - r1) ** 2 + (g2 - g1) ** 2 + (b2 - b1) ** 2)
    
    @staticmethod
    def order_color_palette(colors: list) -> list:
        """
        Order a color palette by proximity to create smooth transitions.
        
        Args:
            colors: List of RGB color tuples [(r, g, b), ...]
        
        Returns:
            Ordered list of RGB color tuples
        """
        if not colors:
            return []
        
        sorted_colors = [colors[0]]
        remaining_colors = colors[1:]
        
        while remaining_colors:
            min_distance = float('inf')
            closest_color = None
            for color in remaining_colors:
                distance = min(ColorController.color_distance(color, sorted_color) 
                             for sorted_color in sorted_colors)
                if distance < min_distance:
                    min_distance = distance
                    closest_color = color
            sorted_colors.append(closest_color)
            remaining_colors = [color for color in remaining_colors if color != closest_color]
        
        return sorted_colors
    
    @staticmethod
    def generate_intermediate_colors(colors: list, num_colors: int) -> list:
        """
        Generate intermediate colors between a list of colors through interpolation.
        
        Args:
            colors: List of RGB color tuples [(r, g, b), ...]
            num_colors: Number of intermediate colors to generate
        
        Returns:
            List of interpolated RGB color tuples
        """
        def interpolate_color(start_color, end_color, ratio):
            r = int(start_color[0] + (end_color[0] - start_color[0]) * ratio)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * ratio)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * ratio)
            return (r, g, b)

        if num_colors <= 0 or len(colors) < 2:
            return []

        num_intervals = len(colors) - 1
        steps = num_colors / num_intervals

        intermediate_colors = []

        for i in range(num_intervals):
            start_color = colors[i]
            end_color = colors[i + 1]

            for j in range(int(steps) + 1):
                ratio = (j + 1) / (steps + 1)
                intermediate_color = interpolate_color(start_color, end_color, ratio)
                intermediate_colors.append(intermediate_color)

        intermediate_colors = intermediate_colors[:num_colors]

        return intermediate_colors


# Global singleton instance
color_controller = ColorController()
