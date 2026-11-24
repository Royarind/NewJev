#!/usr/bin/env python3
"""
Color Detection and Matching using OpenCV
Detects colors visually without relying on text labels
"""

from __future__ import annotations

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    class _DummyNp:
        ndarray = object
    np = _DummyNp()
    CV2_AVAILABLE = False
from typing import List, Dict, Tuple, Optional
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ColorDetector:
    """Detect and match product colors using OpenCV color analysis"""
    
    # Comprehensive color mapping
    COLOR_MAP = {
        'red': (255, 0, 0),
        'dark_red': (139, 0, 0),
        'blue': (0, 0, 255),
        'navy': (0, 0, 128),
        'light_blue': (173, 216, 230),
        'green': (0, 128, 0),
        'dark_green': (0, 100, 0),
        'lime': (0, 255, 0),
        'black': (0, 0, 0),
        'white': (255, 255, 255),
        'gray': (128, 128, 128),
        'grey': (128, 128, 128),
        'dark_gray': (64, 64, 64),
        'light_gray': (192, 192, 192),
        'yellow': (255, 255, 0),
        'gold': (255, 215, 0),
        'purple': (128, 0, 128),
        'violet': (238, 130, 238),
        'orange': (255, 165, 0),
        'brown': (165, 42, 42),
        'tan': (210, 180, 140),
        'pink': (255, 192, 203),
        'hot_pink': (255, 105, 180),
        'beige': (245, 245, 220),
        'cream': (255, 253, 208),
        'olive': (128, 128, 0),
        'maroon': (128, 0, 0),
        'teal': (0, 128, 128),
        'cyan': (0, 255, 255),
        'magenta': (255, 0, 255),
        'turquoise': (64, 224, 208),
        'coral': (255, 127, 80),
        'salmon': (250, 128, 114),
        'khaki': (240, 230, 140),
        'lavender': (230, 230, 250),
        'indigo': (75, 0, 130),
        'slate': (112, 128, 144),
        'charcoal': (54, 69, 79)
    }
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.debug_dir = Path('color_debug')
        if debug_mode:
            self.debug_dir.mkdir(exist_ok=True)
    
    def detect_color_swatches(self, screenshot_path: str, 
                             element_list: List[Dict] = None) -> List[Dict]:
        """
        Find color swatches and extract their actual colors
        No text needed - pure visual analysis
        
        Args:
            screenshot_path: Path to screenshot
            element_list: Optional pre-detected elements from EnhancedVisionSystem
        
        Returns:
            List of detected swatches with color information
        """
        try:
            image = cv2.imread(screenshot_path)
            if image is None:
                raise ValueError(f"Could not load image: {screenshot_path}")
            
            # If elements not provided, detect them
            if element_list is None:
                from .enhanced_vision import EnhancedVisionSystem
                vision = EnhancedVisionSystem()
                processed = vision._preprocess_image(image)
                element_list = vision._detect_ui_elements(processed, image)
            
            # Filter for circular/small square elements (likely swatches)
            swatch_candidates = [
                e for e in element_list 
                if (e['shape'] == 'circle' or e['shape'] == 'square') 
                and 100 < e['area'] < 5000
            ]
            
            results = []
            for swatch in swatch_candidates:
                bbox = swatch['bbox']
                
                # Extract region
                region = image[bbox['y']:bbox['y']+bbox['height'],
                              bbox['x']:bbox['x']+bbox['width']]
                
                if region.size == 0:
                    continue
                
                # Get dominant color (already in swatch data)
                dominant_color = swatch.get('dominant_color', self._get_dominant_color(region))
                
                # Convert to color name
                color_name = self._color_to_name(dominant_color)
                
                results.append({
                    'bbox': bbox,
                    'rgb': dominant_color,
                    'color_name': color_name,
                    'coordinates': swatch['center'],
                    'area': swatch['area'],
                    'shape': swatch['shape']
                })
            
            if self.debug_mode:
                self._visualize_swatches(image, results)
            
            return results
            
        except Exception as e:
            logger.error(f"Color swatch detection failed: {e}")
            return []
    
    def _get_dominant_color(self, region: np.ndarray) -> Tuple[int, int, int]:
        """Extract dominant color using K-Means clustering"""
        try:
            # Reshape to list of pixels
            pixels = region.reshape(-1, 3)
            pixels = np.float32(pixels)
            
            # K-Means with k=1 (dominant color)
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
            _, _, centers = cv2.kmeans(pixels, 1, None, criteria, 10, cv2.KMEANS_RANDOM_CENTERS)
            
            # Return dominant color (BGR to RGB)
            bgr = centers[0].astype(int)
            return (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            
        except Exception as e:
            logger.warning(f"K-means failed, using mean: {e}")
            # Fallback to mean color
            mean_color = cv2.mean(region)[:3]
            return (int(mean_color[2]), int(mean_color[1]), int(mean_color[0]))
    
    def _color_to_name(self, rgb: Tuple[int, int, int]) -> str:
        """
        Convert RGB to color name using color distance
        Returns closest color name from COLOR_MAP
        """
        min_distance = float('inf')
        closest_color = 'unknown'
        
        for name, (r, g, b) in self.COLOR_MAP.items():
            # Euclidean distance in RGB space
            distance = np.sqrt((rgb[0]-r)**2 + (rgb[1]-g)**2 + (rgb[2]-b)**2)
            if distance < min_distance:
                min_distance = distance
                closest_color = name
        
        return closest_color
    
    def match_color_name(self, target_color: str, detected_swatches: List[Dict], 
                        text_elements: List[Dict] = None) -> Optional[Dict]:
        """
        Match a color name to detected swatches
        Handles complex color names like "Olive Navy Glen Plaid"
        Now includes text context for better accuracy
        
        Args:
            target_color: Color name to find (e.g., "Olive Navy Glen Plaid")
            detected_swatches: List of detected swatches with RGB colors
            text_elements: Optional OCR text elements for context matching
        
        Returns:
            Best matching swatch with confidence score
        """
        target_lower = target_color.lower()
        
        # Extract individual color words from target
        color_words = self._extract_color_words(target_lower)
        
        logger.info(f"Matching color: '{target_color}'")
        logger.info(f"Extracted color keywords: {color_words}")
        
        # Score each swatch
        scored_swatches = []
        
        for swatch in detected_swatches:
            score = 0.0
            reasons = []
            
            # 1. RGB color name match (40% weight)
            swatch_color = swatch['color_name']
            for word in color_words:
                if word in swatch_color or swatch_color in word:
                    score += 0.4
                    reasons.append(f"RGB matches '{swatch_color}'")
                    break
            
            # 2. Text proximity match (60% weight) - if text elements available
            if text_elements:
                nearby_text = self._find_text_near_swatch(swatch, text_elements, max_distance=50)
                nearby_text_lower = ' '.join([t['text'].lower() for t in nearby_text])
                
                # Check if any color words appear in nearby text
                text_matches = sum(1 for word in color_words if word in nearby_text_lower)
                
                if text_matches > 0:
                    # More matches = higher score
                    text_score = min(text_matches * 0.2, 0.6)
                    score += text_score
                    reasons.append(f"Text nearby matches {text_matches} keywords")
                
                # Exact match bonus
                if target_lower in nearby_text_lower:
                    score += 0.3
                    reasons.append(f"Exact text match: '{target_color}'")
            
            # Store score
            scored_swatches.append({
                'swatch': swatch,
                'score': score,
                'reasons': reasons
            })
            
            logger.debug(f"Swatch at ({swatch['coordinates']['x']}, {swatch['coordinates']['y']}): "
                        f"color={swatch_color}, score={score:.2f}, reasons={reasons}")
        
        # Sort by score (highest first)
        scored_swatches.sort(key=lambda x: x['score'], reverse=True)
        
        # Return best match if score > threshold
        if scored_swatches and scored_swatches[0]['score'] > 0.3:
            best = scored_swatches[0]
            logger.info(f"✓ Best match: score={best['score']:.2f}, "
                       f"color={best['swatch']['color_name']}, "
                       f"reasons={best['reasons']}")
            
            # Add confidence to swatch
            best['swatch']['match_confidence'] = best['score']
            return best['swatch']
        
        logger.warning(f"No confident color match found for '{target_color}'")
        logger.warning(f"Detected swatches: {[(s['swatch']['color_name'], s['score']) for s in scored_swatches[:3]]}")
        
        return None
    
    def _extract_color_words(self, color_text: str) -> List[str]:
        """Extract color keywords from a color name"""
        # Remove common non-color words
        stop_words = {'glen', 'plaid', 'check', 'stripe', 'solid', 'print', 'pattern'}
        
        words = color_text.lower().replace('_', ' ').split()
        
        # Keep only words that might be colors
        color_words = []
        for word in words:
            if word not in stop_words:
                # Check if it's a known color or could be a color modifier
                if word in self.COLOR_MAP or len(word) > 3:
                    color_words.append(word)
        
        return color_words
    
    def _find_text_near_swatch(self, swatch: Dict, text_elements: List[Dict], 
                              max_distance: int = 50) -> List[Dict]:
        """Find text elements near a color swatch"""
        swatch_x = swatch['coordinates']['x']
        swatch_y = swatch['coordinates']['y']
        
        nearby = []
        for text_elem in text_elements:
            text_x = text_elem['center']['x']
            text_y = text_elem['center']['y']
            
            distance = ((swatch_x - text_x)**2 + (swatch_y - text_y)**2)**0.5
            
            if distance < max_distance:
                nearby.append(text_elem)
        
        return nearby
    
    def find_by_rgb(self, target_rgb: Tuple[int, int, int], 
                   detected_swatches: List[Dict],
                   tolerance: int = 50) -> Optional[Dict]:
        """
        Find swatch by RGB color with tolerance
        
        Args:
            target_rgb: Target RGB color (r, g, b)
            detected_swatches: List of detected swatches
            tolerance: Color distance tolerance
        
        Returns:
            Closest matching swatch or None
        """
        closest_swatch = None
        min_distance = float('inf')
        
        for swatch in detected_swatches:
            distance = self._color_distance(target_rgb, swatch['rgb'])
            if distance < min_distance and distance < tolerance:
                min_distance = distance
                closest_swatch = swatch
        
        return closest_swatch
    
    def _color_distance(self, rgb1: Tuple[int, int, int], 
                       rgb2: Tuple[int, int, int]) -> float:
        """Calculate Euclidean distance between two RGB colors"""
        return np.sqrt(
            (rgb1[0] - rgb2[0])**2 + 
            (rgb1[1] - rgb2[1])**2 + 
            (rgb1[2] - rgb2[2])**2
        )
    
    def _visualize_swatches(self, image: np.ndarray, swatches: List[Dict]):
        """Save visualization of detected color swatches"""
        try:
            vis = image.copy()
            
            for swatch in swatches:
                bbox = swatch['bbox']
                
                # Draw rectangle
                cv2.rectangle(vis, 
                            (bbox['x'], bbox['y']), 
                            (bbox['x'] + bbox['width'], bbox['y'] + bbox['height']),
                            (0, 255, 0), 2)
                
                # Add color name label
                label = swatch['color_name']
                cv2.putText(vis, label, 
                          (bbox['x'], bbox['y'] - 5),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
                
                # Draw center point
                cx, cy = swatch['coordinates']['x'], swatch['coordinates']['y']
                cv2.circle(vis, (cx, cy), 3, (0, 0, 255), -1)
            
            # Save
            output_path = self.debug_dir / 'color_swatches_debug.png'
            cv2.imwrite(str(output_path), vis)
            logger.info(f"Color swatch visualization saved to {output_path}")
            
        except Exception as e:
            logger.warning(f"Could not save swatch visualization: {e}")
