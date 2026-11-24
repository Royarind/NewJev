#!/usr/bin/env python3
"""
Enhanced Vision System using OpenCV + OCR
No VLM required - uses computer vision + text extraction
"""

from __future__ import annotations
from typing import List, Dict, Any, Tuple, Optional

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
from PIL import Image
from typing import List, Dict, Any, Tuple, Optional
import logging
from pathlib import Path

# Configure Tesseract path
try:
    from ..utils.tesseract_config import TESSERACT_PATH
except ImportError:
    pass  # Will use system PATH

try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

logger = logging.getLogger(__name__)


class EnhancedVisionSystem:
    """
    Advanced vision system using OCR + OpenCV
    Provides structured visual data for LLM reasoning
    """
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.debug_dir = Path('vision_debug')
        if debug_mode:
            self.debug_dir.mkdir(exist_ok=True)
    
    async def analyze_page(self, screenshot_path: str) -> Dict:
        """
        Complete page analysis using OpenCV + OCR
        Returns structured data that LLM can reason over
        
        Returns:
            {
                'buttons': List[Dict],
                'inputs': List[Dict],
                'dropdowns': List[Dict],
                'swatches': List[Dict],
                'text_elements': List[Dict],
                'layout': Dict
            }
        """
        try:
            # 1. Load and preprocess
            image = cv2.imread(screenshot_path)
            if image is None:
                raise ValueError(f"Could not load image: {screenshot_path}")
            
            processed = self._preprocess_image(image)
            
            # 2. Detect UI elements
            elements = self._detect_ui_elements(processed, image)
            
            # 3. Extract text with bounding boxes
            text_data = self._extract_text_with_boxes(processed)
            
            # 4. Classify element types
            classified = self._classify_elements(elements, text_data)
            
            # 5. Build structured data for LLM
            result = {
                'buttons': classified['buttons'],
                'inputs': classified['inputs'],
                'dropdowns': classified['dropdowns'],
                'swatches': classified['swatches'],
                'text_elements': text_data,
                'layout': self._analyze_layout(elements)
            }
            
            if self.debug_mode:
                self._save_debug_visualization(image, result)
            
            return result
            
        except Exception as e:
            logger.error(f"Vision analysis failed: {e}")
            return {
                'buttons': [],
                'inputs': [],
                'dropdowns': [],
                'swatches': [],
                'text_elements': [],
                'layout': {}
            }
    
    def _preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """OpenCV preprocessing for better OCR accuracy"""
        # Convert to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        
        # Adaptive thresholding
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 11, 2
        )
        
        # Denoise
        denoised = cv2.fastNlMeansDenoising(binary)
        
        # Sharpen
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        sharpened = cv2.filter2D(denoised, -1, kernel)
        
        return sharpened
    
    def _detect_ui_elements(self, processed_image: np.ndarray, 
                           original_image: np.ndarray) -> List[Dict]:
        """
        Detect UI elements using OpenCV contour detection
        No ML needed - pure computer vision
        """
        elements = []
        
        # Find contours
        contours, hierarchy = cv2.findContours(
            processed_image, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        
        for i, contour in enumerate(contours):
            # Filter by size
            area = cv2.contourArea(contour)
            if area < 100 or area > 500000:  # Too small or too large
                continue
            
            # Get bounding box
            x, y, w, h = cv2.boundingRect(contour)
            
            # Skip very thin elements (likely divider lines)
            if w < 10 or h < 10:
                continue
            
            # Calculate shape properties
            perimeter = cv2.arcLength(contour, True)
            circularity = 4 * np.pi * area / (perimeter * perimeter) if perimeter > 0 else 0
            aspect_ratio = float(w) / h if h > 0 else 0
            
            # Extract color from original image
            roi = original_image[y:y+h, x:x+w]
            dominant_color = self._get_dominant_color(roi)
            
            elements.append({
                'bbox': {'x': int(x), 'y': int(y), 'width': int(w), 'height': int(h)},
                'area': int(area),
                'circularity': float(circularity),
                'aspect_ratio': float(aspect_ratio),
                'shape': self._classify_shape(circularity, aspect_ratio),
                'dominant_color': dominant_color,
                'center': {'x': int(x + w/2), 'y': int(y + h/2)}
            })
        
        return elements
    
    def _classify_shape(self, circularity: float, aspect_ratio: float) -> str:
        """Classify element shape using heuristics"""
        if circularity > 0.8:
            return 'circle'  # Likely color swatch
        elif 0.9 < aspect_ratio < 1.1:
            return 'square'  # Likely size button
        elif aspect_ratio > 5:
            return 'horizontal_bar'  # Likely button or input
        elif aspect_ratio < 0.2:
            return 'vertical_bar'
        else:
            return 'rectangle'  # Generic button
    
    def _get_dominant_color(self, roi: np.ndarray) -> Tuple[int, int, int]:
        """Extract dominant color using mean (faster than K-means)"""
        if roi.size == 0:
            return (0, 0, 0)
        
        # Calculate mean color in BGR
        mean_color = cv2.mean(roi)[:3]
        
        # Convert BGR to RGB
        return (int(mean_color[2]), int(mean_color[1]), int(mean_color[0]))
    
    def _extract_text_with_boxes(self, image: np.ndarray) -> List[Dict]:
        """
        Extract all text with bounding boxes using pytesseract
        Enhanced version with better filtering
        """
        try:
            # Use pytesseract with detailed output
            ocr_data = pytesseract.image_to_data(
                image, 
                output_type=pytesseract.Output.DICT,
                config='--psm 11'  # Sparse text
            )
            
            text_elements = []
            n_boxes = len(ocr_data['text'])
            
            for i in range(n_boxes):
                # Skip low confidence
                if int(ocr_data['conf'][i]) < 30:
                    continue
                
                text = ocr_data['text'][i].strip()
                if not text:
                    continue
                
                text_elements.append({
                    'text': text,
                    'bbox': {
                        'x': ocr_data['left'][i],
                        'y': ocr_data['top'][i],
                        'width': ocr_data['width'][i],
                        'height': ocr_data['height'][i]
                    },
                    'confidence': ocr_data['conf'][i],
                    'center': {
                        'x': ocr_data['left'][i] + ocr_data['width'][i] // 2,
                        'y': ocr_data['top'][i] + ocr_data['height'][i] // 2
                    }
                })
            
            return text_elements
            
        except pytesseract.TesseractNotFoundError:
            logger.warning("Tesseract OCR not found. Install with: choco install tesseract")
            logger.warning("See INSTALL_TESSERACT.md for instructions")
            return []
        except Exception as e:
            logger.warning(f"OCR extraction failed: {e}")
            return []
    
    def _classify_elements(self, ui_elements: List[Dict], 
                          text_elements: List[Dict]) -> Dict:
        """
        Classify UI elements by combining shape + text + color
        Uses spatial proximity to associate text with shapes
        """
        classified = {
            'buttons': [],
            'inputs': [],
            'dropdowns': [],
            'swatches': []
        }
        
        for element in ui_elements:
            # Find text inside or near this element
            associated_text = self._find_nearby_text(
                element['bbox'], text_elements, max_distance=20
            )
            
            # Classification logic
            shape = element['shape']
            text = ' '.join([t['text'] for t in associated_text])
            text_lower = text.lower()
            
            element_data = {
                **element,
                'text': text,
                'has_text': len(text) > 0
            }
            
            # Classify based on shape, size, and text
            if shape == 'circle' and element['area'] < 2000:
                # Small circles = color swatches
                classified['swatches'].append({
                    **element_data,
                    'type': 'color_swatch'
                })
            elif shape in ['square', 'rectangle']:
                # Check text content for classification
                if any(keyword in text_lower for keyword in ['add', 'buy', 'cart', 'checkout', 'proceed']):
                    classified['buttons'].append({
                        **element_data,
                        'type': 'action_button'
                    })
                elif any(size in text.upper() for size in ['XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', '3XL']):
                    classified['swatches'].append({
                        **element_data,
                        'type': 'size_swatch'
                    })
                elif element['aspect_ratio'] > 5 and element['area'] > 1000:
                    # Long thin rectangles = likely inputs
                    classified['inputs'].append({
                        **element_data,
                        'type': 'text_input'
                    })
                elif 'select' in text_lower or 'choose' in text_lower:
                    classified['dropdowns'].append({
                        **element_data,
                        'type': 'dropdown'
                    })
                else:
                    # Generic button
                    classified['buttons'].append({
                        **element_data,
                        'type': 'button'
                    })
        
        return classified
    
    def _find_nearby_text(self, bbox: Dict, text_elements: List[Dict], 
                         max_distance: int = 20) -> List[Dict]:
        """Find text elements inside or near a bounding box"""
        nearby = []
        
        for text_elem in text_elements:
            # Check if text is inside bbox
            if self._is_inside(text_elem['bbox'], bbox):
                nearby.append(text_elem)
            # Check if text is nearby
            elif self._distance_bbox(text_elem['bbox'], bbox) < max_distance:
                nearby.append(text_elem)
        
        return nearby
    
    def _is_inside(self, inner: Dict, outer: Dict) -> bool:
        """Check if inner bbox is inside outer bbox"""
        return (inner['x'] >= outer['x'] and 
                inner['y'] >= outer['y'] and
                inner['x'] + inner['width'] <= outer['x'] + outer['width'] and
                inner['y'] + inner['height'] <= outer['y'] + outer['height'])
    
    def _distance_bbox(self, bbox1: Dict, bbox2: Dict) -> float:
        """Calculate distance between two bounding boxes"""
        cx1 = bbox1['x'] + bbox1['width'] / 2
        cy1 = bbox1['y'] + bbox1['height'] / 2
        cx2 = bbox2['x'] + bbox2['width'] / 2
        cy2 = bbox2['y'] + bbox2['height'] / 2
        return np.sqrt((cx1 - cx2)**2 + (cy1 - cy2)**2)
    
    def _analyze_layout(self, elements: List[Dict]) -> Dict:
        """Analyze spatial layout of elements"""
        if not elements:
            return {'clusters': {}, 'num_clusters': 0}
        
        try:
            # Find element clusters (likely related elements)
            from sklearn.cluster import DBSCAN
            
            bboxes = np.array([[e['bbox']['x'], e['bbox']['y']] for e in elements])
            
            # Use DBSCAN to find clusters
            clustering = DBSCAN(eps=50, min_samples=2).fit(bboxes)
            
            clusters = {}
            for i, label in enumerate(clustering.labels_):
                if label not in clusters:
                    clusters[label] = []
                clusters[label].append(elements[i])
            
            return {
                'clusters': {str(k): v for k, v in clusters.items()},
                'num_clusters': len(set(clustering.labels_)) - (1 if -1 in clustering.labels_ else 0)
            }
        except Exception as e:
            logger.warning(f"Layout analysis failed: {e}")
            return {'clusters': {}, 'num_clusters': 0}
    
    def _save_debug_visualization(self, image: np.ndarray, analysis: Dict):
        """Save visualization of detected elements for debugging"""
        try:
            vis = image.copy()
            
            # Draw swatches in green
            for swatch in analysis['swatches']:
                bbox = swatch['bbox']
                cv2.rectangle(vis, (bbox['x'], bbox['y']), 
                            (bbox['x'] + bbox['width'], bbox['y'] + bbox['height']),
                            (0, 255, 0), 2)
                cv2.putText(vis, swatch.get('type', 'swatch'), 
                          (bbox['x'], bbox['y'] - 5),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            
            # Draw buttons in blue
            for button in analysis['buttons'][:10]:  # Limit to avoid clutter
                bbox = button['bbox']
                cv2.rectangle(vis, (bbox['x'], bbox['y']), 
                            (bbox['x'] + bbox['width'], bbox['y'] + bbox['height']),
                            (255, 0, 0), 2)
            
            # Save
            cv2.imwrite(str(self.debug_dir / 'vision_debug.png'), vis)
            logger.info(f"Debug visualization saved to {self.debug_dir}")
            
        except Exception as e:
            logger.warning(f"Could not save debug visualization: {e}")
