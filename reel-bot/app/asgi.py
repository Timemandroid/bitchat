"""Точка входа для uvicorn: uvicorn app.asgi:app"""
import logging

from .config import Settings
from .main import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app(Settings.from_env())
