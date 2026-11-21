"""
Connect Microsoft 365 (Teams messages, calendar, OneDrive / SharePoint) once:
    .venv\\Scripts\\python tools\\connect_microsoft.py
Needs GRAPH_CLIENT_ID in .env (an Entra ID app registration with "Allow public client flows" = Yes).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def main() -> None:
    from app.config import get_settings
    from app.shortcuts.msgraph import Graph
    s = get_settings()
    if not s.graph_client_id:
        print("Set GRAPH_CLIENT_ID in .env first (see README: Connecting Microsoft 365).")
        sys.exit(2)
    g = Graph(s.graph_client_id, s.graph_tenant, s.graph_token_path)
    g.device_login(lambda m: print("\n" + m + "\n"))
    me = g._req("GET", "/me")
    print(f"Connected as {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>. Token saved to "
          f"{s.graph_token_path}.")


if __name__ == "__main__":
    main()
