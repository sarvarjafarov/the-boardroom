"""The Boardroom — agent definitions.

Public exports:
  - root_agent: the LlmAgent that wires the four director sub-agents +
    chairman synthesis + tool surface. Imported by main.py and deploy.py.
"""
from .root_agent import root_agent  # noqa: F401
