"""The local editor server.

Serves the web UI and a small JSON API over a single project. Runs on
localhost, only ever binds the loopback address by default, and has no
authentication because it is not meant to be reachable from anywhere else.
"""

from .app import Workspace, make_server, serve

__all__ = ["Workspace", "make_server", "serve"]
