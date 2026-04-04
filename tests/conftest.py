"""Shared test configuration and fixtures for JARVIS tests."""

import os
import sys
from pathlib import Path

# Add project root to path so all test files can import project modules
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set dummy API keys before any project module imports —
# server.py creates an anthropic client at module level that needs a key
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("FISH_API_KEY", "test-fish-key")
