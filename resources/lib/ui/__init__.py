"""Kodi UI owns all visual updates and user confirmations on its main thread."""
from pathlib import Path

NOTIFICATION_ICON = str(Path(__file__).resolve().parents[2] / 'icon-assistant-transparent.png')
