"""Sign in to Gmail once (needs GMAIL_CREDENTIALS_PATH in .env):  .venv\\Scripts\\python tools\\connect_gmail.py"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

if __name__ == "__main__":
    from app.config import get_settings
    from app.shortcuts.mail import _gmail
    s = get_settings()
    if not s.gmail_credentials_path:
        sys.exit("Set GMAIL_CREDENTIALS_PATH in .env first.")
    _gmail(s.gmail_credentials_path, s.gmail_token_path)
    print("Gmail connected.")
