"""Entry point for Posit Connect Cloud: pick this file as the "primary file" when publishing.
It runs streamlit_app/app.py (password screen, then the full map). Set MAP_PASSWORD as a secret variable."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "streamlit_app" / "app.py"), run_name="__main__")
