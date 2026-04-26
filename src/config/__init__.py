# pylint: disable=wrong-import-position
import logging

from dotenv import load_dotenv
from pathlib import Path

# Load .env from project root (parent of src directory)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)
# Also load from src directory if exists there
env_path_src = Path(__file__).parent.parent / ".env"
load_dotenv(env_path_src)

from config.config import *
from config.constant import *

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(filename)s:%(lineno)d %(levelname).1s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
