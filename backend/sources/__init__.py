"""Plugin-based job source system for Hunter."""
from .registry import PluginRegistry, PluginConfig

__all__ = ["PluginRegistry", "PluginConfig"]
