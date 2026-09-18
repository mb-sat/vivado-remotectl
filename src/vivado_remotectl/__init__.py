"""vivado-remotectl: inject Tcl into running Vivado sessions."""

__version__ = "0.1.0"

from .client import ConnectFailed, Session
from .launcher import LaunchError, plugin_path, start_session, stop_session
from .protocol import ProtocolError, RemoteTclError, SessionDied
from .registry import RegistryError, SessionInfo, list_sessions, resolve, state_dir

__all__ = [
    "ConnectFailed",
    "LaunchError",
    "ProtocolError",
    "RemoteTclError",
    "RegistryError",
    "Session",
    "SessionDied",
    "SessionInfo",
    "list_sessions",
    "plugin_path",
    "resolve",
    "start_session",
    "state_dir",
    "stop_session",
]
