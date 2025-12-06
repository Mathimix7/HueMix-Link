"""Controller for color space conversions and color utilities."""
import math
from typing import Dict, Tuple, Optional


class ColorController:
    """
    Handles color space conversions between XY, RGB, HSV, and Color Temperature.
    Optimized for Philips Hue color gamut.
    """
    
    # Philips Hue color gamut (wide gamut bulbs like Hue Color)
    # Gamut C - most modern bulbs
    GAMUT_C = {
        'red': (0.6915, 0.3083),
        'green': (0.17, 0.7),
        'blue': (0.1532, 0.0475)
    }
    
    # Gamut B - older color bulbs
    GAMUT_B = {
        'red': (0.675, 0.322),
        'green': (0.409, 0.518),
        'blue': (0.167, 0.04)
    }
    
    # Gamut A - very old bulbs
    GAMUT_A = {
        'red': (0.704, 0.296),
        'green': (0.2151, 0.7106),
        'blue': (0.138, 0.08)
    }
    
    @staticmethod
    def xy_to_rgb(x: float, y: float, brightness: float = 100) -> Dict[str, int]:
        """
        Convert CIE 1931 XY color space to RGB.
        
        Args:
            x: X coordinate (0.0 - 1.0)
            y: Y coordinate (0.0 - 1.0)
            brightness: Brightness level (0-100)
        
        Returns:
            Dict with r, g, b values (0-255)
        """
        # Convert brightness from 0-100 to 0-1
        bri = brightness / 100.0
        
        # Calculate XYZ
        z = 1.0 - x - y
        Y = bri
        X = (Y / y) * x if y > 0 else 0
        Z = (Y / y) * z if y > 0 else 0
        
        # Convert to RGB using Wide RGB D65 conversion
        r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
        g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
        b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
        
        # Apply reverse gamma correction
        r = ColorController._reverse_gamma(r)
        g = ColorController._reverse_gamma(g)
        b = ColorController._reverse_gamma(b)
        
        # Normalize and convert to 0-255
        r = max(0, min(255, int(r * 255)))
        g = max(0, min(255, int(g * 255)))
        b = max(0, min(255, int(b * 255)))
        
        return {'r': r, 'g': g, 'b': b}
    
    @staticmethod
    def _reverse_gamma(value: float) -> float:
        """Apply reverse gamma correction."""
        if value <= 0.0031308:
            return 12.92 * value
        else:
            return 1.055 * math.pow(value, 1.0 / 2.4) - 0.055
    
    @staticmethod
    def rgb_to_xy(r: int, g: int, b: int, gamut: str = 'C') -> Tuple[float, float]:
        """
        Convert RGB to CIE 1931 XY color space.
        
        Args:
            r: Red value (0-255)
            g: Green value (0-255)
            b: Blue value (0-255)
            gamut: Hue bulb gamut ('A', 'B', or 'C')
        
        Returns:
            Tuple of (x, y) coordinates
        """
        # Normalize to 0-1
        r = r / 255.0
        g = g / 255.0
        b = b / 255.0
        
        # Apply gamma correction
        r = ColorController._apply_gamma(r)
        g = ColorController._apply_gamma(g)
        b = ColorController._apply_gamma(b)
        
        # Convert to XYZ using Wide RGB D65
        X = r * 0.664511 + g * 0.154324 + b * 0.162028
        Y = r * 0.283881 + g * 0.668433 + b * 0.047685
        Z = r * 0.000088 + g * 0.072310 + b * 0.986039
        
        # Calculate xy
        total = X + Y + Z
        if total == 0:
            return (0.0, 0.0)
        
        x = X / total
        y = Y / total
        
        # Check if color is within gamut and correct if necessary
        gamut_dict = getattr(ColorController, f'GAMUT_{gamut}', ColorController.GAMUT_C)
        x, y = ColorController._check_point_in_gamut(x, y, gamut_dict)
        
        return (round(x, 4), round(y, 4))
    
    @staticmethod
    def _apply_gamma(value: float) -> float:
        """Apply gamma correction."""
        if value > 0.04045:
            return math.pow((value + 0.055) / 1.055, 2.4)
        else:
            return value / 12.92
    
    @staticmethod
    def _check_point_in_gamut(x: float, y: float, gamut: Dict) -> Tuple[float, float]:
        """
        Check if a point is within the color gamut triangle.
        If not, find the closest point on the triangle.
        """
        red = gamut['red']
        green = gamut['green']
        blue = gamut['blue']
        
        # Check if point is in triangle
        v1 = (green[0] - red[0], green[1] - red[1])
        v2 = (blue[0] - red[0], blue[1] - red[1])
        q = (x - red[0], y - red[1])
        
        s = ColorController._cross_product(q, v2) / ColorController._cross_product(v1, v2)
        t = ColorController._cross_product(v1, q) / ColorController._cross_product(v1, v2)
        
        if s >= 0.0 and t >= 0.0 and s + t <= 1.0:
            # Point is in triangle
            return (x, y)
        
        # Find closest point on triangle edge
        closest = ColorController._closest_point_on_line(x, y, red, green)
        dist = ColorController._distance(x, y, closest[0], closest[1])
        
        closest_green_blue = ColorController._closest_point_on_line(x, y, green, blue)
        dist_gb = ColorController._distance(x, y, closest_green_blue[0], closest_green_blue[1])
        if dist_gb < dist:
            closest = closest_green_blue
            dist = dist_gb
        
        closest_blue_red = ColorController._closest_point_on_line(x, y, blue, red)
        dist_br = ColorController._distance(x, y, closest_blue_red[0], closest_blue_red[1])
        if dist_br < dist:
            closest = closest_blue_red
        
        return closest
    
    @staticmethod
    def _cross_product(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
        """Calculate cross product of two 2D vectors."""
        return p1[0] * p2[1] - p1[1] * p2[0]
    
    @staticmethod
    def _closest_point_on_line(px: float, py: float, 
                               a: Tuple[float, float], 
                               b: Tuple[float, float]) -> Tuple[float, float]:
        """Find closest point on line segment to given point."""
        ap_x = px - a[0]
        ap_y = py - a[1]
        ab_x = b[0] - a[0]
        ab_y = b[1] - a[1]
        
        ab_squared = ab_x * ab_x + ab_y * ab_y
        ap_ab = ap_x * ab_x + ap_y * ab_y
        
        if ab_squared == 0:
            return a
        
        t = max(0, min(1, ap_ab / ab_squared))
        
        return (a[0] + ab_x * t, a[1] + ab_y * t)
    
    @staticmethod
    def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
        """Calculate Euclidean distance between two points."""
        dx = x1 - x2
        dy = y1 - y2
        return math.sqrt(dx * dx + dy * dy)
    
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
        if ct <= 500:  # Likely Mired
            kelvin = 1000000 / ct
        else:  # Kelvin
            kelvin = ct
        
        # Clamp to valid range
        kelvin = max(2000, min(6500, kelvin))
        
        # Calculate x
        if kelvin <= 4000:
            x = -0.2661239 * (1000000000 / (kelvin ** 3)) - 0.2343589 * (1000000 / (kelvin ** 2)) + 0.8776956 * (1000 / kelvin) + 0.179910
        else:
            x = -3.0258469 * (1000000000 / (kelvin ** 3)) + 2.1070379 * (1000000 / (kelvin ** 2)) + 0.2226347 * (1000 / kelvin) + 0.240390
        
        # Calculate y from x
        if kelvin <= 2222:
            y = -1.1063814 * (x ** 3) - 1.34811020 * (x ** 2) + 2.18555832 * x - 0.20219683
        elif kelvin <= 4000:
            y = -0.9549476 * (x ** 3) - 1.37418593 * (x ** 2) + 2.09137015 * x - 0.16748867
        else:
            y = 3.0817580 * (x ** 3) - 5.87338670 * (x ** 2) + 3.75112997 * x - 0.37001483
        
        return (round(x, 4), round(y, 4))
    
    @staticmethod
    def xy_to_ct(x: float, y: float) -> int:
        """
        Convert XY to approximate color temperature in Kelvin.
        
        Args:
            x: X coordinate
            y: Y coordinate
        
        Returns:
            Color temperature in Kelvin (approximate)
        """
        # Calculate CCT using McCamy's formula
        n = (x - 0.3320) / (0.1858 - y)
        cct = 449 * (n ** 3) + 3525 * (n ** 2) + 6823.3 * n + 5520.33
        
        return max(2000, min(6500, int(cct)))
    
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


# Global singleton instance
color_controller = ColorController()
