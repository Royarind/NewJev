"""
Core Vision System for E-commerce Automation
Provides OpenCV + OCR based visual analysis
"""

from .enhanced_vision import EnhancedVisionSystem
from .color_detector import ColorDetector

__all__ = [
    'EnhancedVisionSystem',
    'ColorDetector'
]
