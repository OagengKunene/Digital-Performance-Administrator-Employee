"""Reproducible Codespaces setup. Does not start services or create accounts."""
import os
import shutil
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
if not shutil.which("tesseract"):
    if os.environ.get("CODESPACES") == "true" and shutil.which("sudo"):
        print("Installing local photo reader for the keyless marked-script demo...", flush=True)
        try:
            subprocess.run(["sudo", "-n", "apt-get", "update"], check=True, timeout=180)
            subprocess.run(["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends",
                            "tesseract-ocr", "tesseract-ocr-eng"], check=True, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("Local OCR installation did not finish. Configured Gemini/Groq vision can still read photos. "
                  "For the keyless sample, install tesseract-ocr and tesseract-ocr-eng.")
    else:
        print("For local photo OCR install tesseract-ocr and tesseract-ocr-eng, or configure Gemini/Groq vision.")
print("Setup complete. Read README.md, then run .venv/bin/python scripts/start.py")
