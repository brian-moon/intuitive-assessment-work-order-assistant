"""Entry point for ``uvicorn app.main:app``. Kept separate so importing the API in tests creates nothing."""
from .api import create_app

app = create_app()
