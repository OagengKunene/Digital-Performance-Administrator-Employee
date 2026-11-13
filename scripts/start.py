"""Run API and interface together, terminating both when either exits."""
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
from pae.config import settings

config = settings()
print(f"AI model: {config.gemini_model}; report time limit: {config.ai_timeout_seconds}s", flush=True)
children = []
exit_code = 0
try:
    children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "pae.api:app", "--host", "0.0.0.0", "--port", "8000"]))
    children.append(subprocess.Popen([sys.executable, "-m", "streamlit", "run", "ui/app.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--browser.gatherUsageStats", "false"]))
    while all(child.poll() is None for child in children):
        time.sleep(0.5)
    exit_code = next((child.returncode for child in children if child.returncode is not None), 1) or 1
except KeyboardInterrupt:
    pass
finally:
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
sys.exit(exit_code)
