"""MCP server for Modbus TCP/UDP/serial devices."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("modbus-mcp")
except PackageNotFoundError:  # a source tree that has not been installed
    __version__ = "0.0.0+unknown"
