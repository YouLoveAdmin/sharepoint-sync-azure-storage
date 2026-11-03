import logging

from app.api import create_app

logging.basicConfig(level=logging.WARNING)
logging.getLogger("app").setLevel(logging.INFO)

app = create_app()
