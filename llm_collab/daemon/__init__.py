"""Inert local daemon lifecycle and control protocol."""

from .server import DaemonServer, ProtocolError

__all__ = ["DaemonServer", "ProtocolError"]
