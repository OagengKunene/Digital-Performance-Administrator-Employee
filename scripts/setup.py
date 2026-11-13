"""Reproducible Codespaces setup. Does not start services or create accounts."""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not python.exists():
    subprocess.run([sys.executable, "-m", "venv", ".venv"], check=True)
subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements.lock.txt"], check=True)
subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "-e", "."], check=True)
subprocess.run([str(python), "-m", "pae.manage", "init"], check=True)
print("Setup complete. Read README.md, then run .venv/bin/python scripts/start.py")
