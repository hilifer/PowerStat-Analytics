"""配置加载器：读取 YAML 配置并解析环境变量。"""

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _resolve_env_vars(obj):
    """递归替换配置值中的 ${ENV_VAR} 为实际环境变量。"""
    if isinstance(obj, str):
        pattern = re.compile(r'\$\{(\w+)\}')
        matches = pattern.findall(obj)
        for var_name in matches:
            env_val = os.environ.get(var_name, "")
            obj = obj.replace(f"${{{var_name}}}", env_val)
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


class Config:
    """全局配置单例。"""

    _instance = None
    _data = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, config_path: str = None):
        """加载配置文件。"""
        project_root = Path(__file__).parent.parent
        load_dotenv(project_root / ".env")

        if config_path is None:
            config_path = project_root / "config" / "settings.yaml"

        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        self._data = _resolve_env_vars(raw)

        # 将相对路径转为绝对路径
        for key in ("database", "csv_export_dir", "archive_root"):
            if key in self._data.get("storage", {}):
                p = Path(self._data["storage"][key])
                if not p.is_absolute():
                    self._data["storage"][key] = str(project_root / p)

        for key in ("output_dir",):
            if key in self._data.get("visualization", {}):
                p = Path(self._data["visualization"][key])
                if not p.is_absolute():
                    self._data["visualization"][key] = str(project_root / p)

        temp_dir = self._data.get("attachments", {}).get("temp_dir")
        if temp_dir:
            p = Path(temp_dir)
            if not p.is_absolute():
                self._data["attachments"]["temp_dir"] = str(project_root / p)

        log_file = self._data.get("logging", {}).get("log_file")
        if log_file:
            p = Path(log_file)
            if not p.is_absolute():
                self._data["logging"]["log_file"] = str(project_root / p)

        return self

    @property
    def data(self) -> dict:
        if self._data is None:
            self.load()
        return self._data

    def get(self, *keys, default=None):
        """按层级键名获取配置值。如 config.get('email', 'filter', 'subject_keywords')。"""
        d = self.data
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
                if d is None:
                    return default
            else:
                return default
        return d


config = Config()
