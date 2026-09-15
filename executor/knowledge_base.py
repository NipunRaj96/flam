"""
Knowledge Base loader for known form platforms.

Loads structured, deterministic JSON configs on demand from /form_knowledge_base/.
Never loads all configs into memory at once — only the single matching platform
needed for the active application.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

KB_DIR = Path(__file__).resolve().parent.parent / "form_knowledge_base"

# In-memory cache for loaded platform configs
_CONFIG_CACHE: dict[str, dict[str, Any]] = {}


def load_platform_config(platform_id: str) -> dict[str, Any]:
    """
    Load a single platform's JSON configuration from /form_knowledge_base/.
    Caches the config in-memory after first load.

    Args:
        platform_id: Platform identifier (e.g. 'google_forms', 'ms_forms', 'typeform', 'notion_forms')

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the platform JSON file does not exist in /form_knowledge_base/.
    """
    if platform_id in _CONFIG_CACHE:
        return _CONFIG_CACHE[platform_id]

    config_path = KB_DIR / f"{platform_id}.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Platform config '{platform_id}.json' not found in {KB_DIR}."
        )

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        _CONFIG_CACHE[platform_id] = config
        logger.info("Loaded form knowledge base config for '%s'", platform_id)
        return config
    except Exception as exc:
        logger.error("Failed to parse config for '%s': %s", platform_id, exc)
        raise


def discover_platform_patterns() -> dict[str, list[str]]:
    """
    Scan /form_knowledge_base/*.json and return a mapping of:
      {platform_id: [url_pattern_1, url_pattern_2, ...]}
    Used by classifier/link.py for dynamic URL pattern matching.
    """
    patterns: dict[str, list[str]] = {}
    if not KB_DIR.exists():
        return patterns

    for json_file in sorted(KB_DIR.glob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                pid = data.get("platform_id") or json_file.stem
                pats = data.get("url_patterns", [])
                if pid and pats:
                    patterns[pid] = pats
        except Exception as exc:
            logger.warning("Could not read URL patterns from %s: %s", json_file, exc)

    return patterns


def get_supported_platforms() -> list[str]:
    """Return a list of all currently configured platform IDs in the knowledge base."""
    if not KB_DIR.exists():
        return []
    return [f.stem for f in sorted(KB_DIR.glob("*.json"))]
