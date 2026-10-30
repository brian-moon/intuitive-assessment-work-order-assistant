"""Entry point for ``uvicorn app.main:app``. Kept separate so importing the API in tests creates nothing.

``APP_RESET_ON_START=1`` empties and re-seeds the database at start (demo and browser test only).
"""
import os

from .api import create_app

app = create_app(reset=os.environ.get("APP_RESET_ON_START") == "1")
