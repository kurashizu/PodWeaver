import json
from pathlib import Path
from typing import Any, Dict

# ==========================================
# Core Paths Definition
# ==========================================
# Determine project root dynamically based on this file's location
# Path(__file__) -> src/config.py
# parent -> src/
# parent.parent -> Projects/ (Project Root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Core Directories
CONF_DIR = PROJECT_ROOT / "conf"
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
ASSETS_DIR = PROJECT_ROOT / "assets"
SRC_DIR = PROJECT_ROOT / "src"

# Workspace Subdirectories
CLIPS_DIR = WORKSPACE_DIR / "clips"
SEGMENTS_DIR = WORKSPACE_DIR / "segments"
SCRIPTS_DIR = WORKSPACE_DIR / "scripts"
OUTPUT_DIR = WORKSPACE_DIR / "output"

# Important File Paths
CONFIG_FILE_PATH = CONF_DIR / "config.json"


# ==========================================
# Configuration Loading
# ==========================================
def load_config() -> Dict[str, Any]:
    """Load the main configuration from config.json."""
    if not CONFIG_FILE_PATH.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE_PATH}")
    with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Global Configuration Dictionary
CONFIG = load_config()


# ==========================================
# Helper Methods
# ==========================================
def get_prompt_path(prompt_key: str, default: str) -> Path:
    """
    Safely resolve a prompt file path from the config.
    If the path in config is relative, it resolves relative to the PROJECT_ROOT.
    """
    prompt_cfg = CONFIG.get("prompt", {})
    path_str = prompt_cfg.get(prompt_key, default)

    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


def get_asset_path(filename: str) -> Path:
    """Safely get an absolute path to an asset in the assets directory."""
    return ASSETS_DIR / filename
