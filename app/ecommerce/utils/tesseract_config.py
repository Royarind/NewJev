"""
Tesseract Configuration
Auto-configures pytesseract to use the correct Tesseract path
Import this at the start of vision modules
"""

import pytesseract
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Configure Tesseract path
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if Path(TESSERACT_PATH).exists():
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    logger.info(f"Tesseract configured: {TESSERACT_PATH}")
else:
    logger.warning(f"⚠ Tesseract not found at {TESSERACT_PATH}")
    logger.warning("OCR functionality will be limited")
