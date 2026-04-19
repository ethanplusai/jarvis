"""REST API routers for JARVIS. Each submodule exposes a build_*_router()."""

from .control import build_control_router
from .core import build_core_router
from .settings import build_settings_router
from .voice import build_voice_router

__all__ = ["build_control_router", "build_core_router", "build_settings_router", "build_voice_router"]
