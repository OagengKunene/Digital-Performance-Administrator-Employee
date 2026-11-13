"""One command for a presentation: demo school + all services, offline agents by default.

    .venv/bin/python scripts/demo.py

- Creates the fictional demo school once (sign-in details: .local/demo-login.txt).
- Starts the API, worker and interface exactly like scripts/start.py.
- Uses the offline engine unless AI_PROVIDER is already set, so the demo
  needs no API key and cannot hit provider limits. Live providers with a
  configured key still appear in the Engine menu.
"""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
env = dict(os.environ)
env.setdefault("AI_PROVIDER", "offline")
subprocess.run([sys.executable, "-m", "pae.demo_data"], check=True, env=env)
print("Open the PORTS tab and click the globe next to port 8501 (School application).", flush=True)
sys.exit(subprocess.call([sys.executable, "scripts/start.py"], env=env))
