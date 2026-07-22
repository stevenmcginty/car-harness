"""Launch car-harness with `python run.py`.

Serves the app on port 8765. Equivalent to double-clicking start.bat (which
also creates the venv, installs deps and opens a browser).

Binds 0.0.0.0 so the phone app can reach it over the LAN -- non-localhost
requests are rejected unless the phone has paired (bearer token), enforced by
the middleware in backend/main.py. Windows Firewall will ask to allow Python
the first time; choose Allow on private networks or the phone can't connect.
"""

import os
import sys

# Ensure the project root is importable before we import the app.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from backend.main import app  # noqa: E402

HOST = "0.0.0.0"
PORT = 8765

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
