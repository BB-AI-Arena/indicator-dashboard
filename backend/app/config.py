from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = "/app/config/config.yml"


class ConfigManager:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path or os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH)
        self._config: dict[str, Any] = {}
        self.loaded = False
        self.error: str | None = None
        self.reload()

    def reload(self) -> dict[str, Any]:
        path = Path(self.config_path)
        try:
            if not path.exists():
                self.error = f"Config file not found: {self.config_path}"
                self.loaded = False
                self._config = {}
                return self._config
            self._config = yaml.safe_load(path.read_text()) or {}
            self.loaded = True
            self.error = None
        except Exception as exc:
            self.loaded = False
            self.error = str(exc)
            self._config = {}
        return self._config

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def get(self, *path: str, default: Any = None) -> Any:
        value: Any = self._config
        for key in path:
            if not isinstance(value, dict):
                return default
            value = value.get(key)
            if value is None:
                return default
        return value


config_manager = ConfigManager()
