#!/usr/bin/env python
"""
houdini_mcp_server.py

This is the "bridge" or "driver" script that Claude will run via `uv run`.
It uses the MCP library (fastmcp) to communicate with Claude over stdio,
and relays each command to the local Houdini plugin on port 9876.
"""
import sys
import os
import site

# Get the directory where the script is located
script_dir = os.path.dirname(os.path.abspath(__file__))
# Add the virtual environment's site-packages to Python's path
venv_site_packages = os.path.join(script_dir, '.venv', 'Lib', 'site-packages')
sys.path.insert(0, venv_site_packages)

# For debugging
import sys
print("Python path:", sys.path, file=sys.stderr)
import sys
import json
import socket
import logging
from dataclasses import dataclass
from typing import Dict, Any
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HoudiniMCP_StdioServer")


@dataclass
class HoudiniConnection:
    host: str
    port: int
    sock: socket.socket = None

    def connect(self) -> bool:
        """Connect to the Houdini plugin (which is listening on self.host:self.port)."""
        if self.sock is not None:
            return True  # Already connected
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Houdini at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Houdini: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Close socket if open."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Houdini: {str(e)}")
            self.sock = None

    def send_command(self, cmd_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Send a JSON command to Houdini's server and wait for the JSON response.
        Returns the parsed Python dict (e.g. {"status": "success", "result": {...}})
        """
        if not self.connect():
            raise ConnectionError("Could not connect to Houdini on port 9876.")
        command = {"type": cmd_type, "params": params or {}}
        data_out = json.dumps(command).encode("utf-8")

        try:
            # Send the command
            self.sock.sendall(data_out)
            logger.info(f"Sent command to Houdini: {command}")

            # Read response. We'll accumulate chunks until we can parse a full JSON.
            chunks = []
            self.sock.settimeout(10.0)
            while True:
                chunk = self.sock.recv(8192)
                if not chunk:
                    # No more data -> possibly an incomplete read
                    break
                chunks.append(chunk)
                try:
                    combined = b"".join(chunks)
                    parsed = json.loads(combined.decode("utf-8"))
                    # Successfully parsed JSON
                    return parsed
                except json.JSONDecodeError:
                    # We haven't read a complete JSON block yet, keep going
                    continue

            raise Exception("No (or incomplete) data from Houdini; EOF reached.")
        except Exception as e:
            logger.error(f"Error sending command '{cmd_type}': {str(e)}")
            # Invalidate socket so we reconnect next time
            self.disconnect()
            raise


# A global Houdini connection object
_houdini_connection: HoudiniConnection = None

def get_houdini_connection() -> HoudiniConnection:
    """Get or create a persistent HoudiniConnection object."""
    global _houdini_connection
    if _houdini_connection is not None:
        return _houdini_connection

    _houdini_connection = HoudiniConnection(host="localhost", port=9876)
    if not _houdini_connection.connect():
        raise RuntimeError("Could not connect to Houdini on localhost:9876. Is the plugin running?")
    return _houdini_connection


# Now define the MCP server that Claude will talk to over stdio
mcp = FastMCP(
    "HoudiniMCP",
    description="A bridging server that connects Claude to Houdini via MCP stdio + TCP"
)

@asynccontextmanager
async def server_lifespan(app: FastMCP):
    """Startup/shutdown logic. Called automatically by fastmcp."""
    logger.info("Houdini MCP server starting up (stdio).")
    # Attempt to connect right away
    try:
        get_houdini_connection()
        logger.info("Successfully connected to Houdini on startup.")
    except Exception as e:
        logger.warning(f"Could not connect to Houdini: {e}")
        logger.warning("Make sure Houdini is running with the plugin on port 9876.")
    yield {}
    logger.info("Houdini MCP server shutting down.")
    global _houdini_connection
    if _houdini_connection is not None:
        _houdini_connection.disconnect()
        _houdini_connection = None
    logger.info("Connection to Houdini closed.")

mcp.lifespan = server_lifespan


# -------------------------------------------------------------------
# Examples of "tools" that Claude can call
# -------------------------------------------------------------------
@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """
    Ask Houdini for scene info. Returns JSON as a string.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("get_scene_info")
        # response should look like {"status": "success", "result": {...}}
        if response.get("status") == "error":
            return f"Houdini error: {response.get('message')}"
        return json.dumps(response.get("result"), indent=2)
    except Exception as e:
        return f"Error retrieving scene info: {str(e)}"

@mcp.tool()
def create_node(ctx: Context, node_type: str, parent_path: str = "/obj", name: str = None) -> str:
    """
    Create a new node in Houdini (example).
    """
    try:
        conn = get_houdini_connection()
        params = {
            "node_type": node_type,
            "parent_path": parent_path
        }
        if name:
            params["name"] = name
        response = conn.send_command("create_node", params)
        if response.get("status") == "error":
            return f"Error: {response.get('message')}"
        return f"Node created: {json.dumps(response.get('result'), indent=2)}"
    except Exception as e:
        return f"Error creating node: {str(e)}"

@mcp.tool()
def execute_houdini_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in Houdini's environment.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("execute_code", {"code": code})
        if response.get("status") == "error":
            return f"Error: {response.get('message')}"
        return "Code executed successfully in Houdini."
    except Exception as e:
        return f"Error executing code: {str(e)}"

# ... you can define more tools that forward commands to "modify_node", "delete_node", etc.


def main():
    """Run the MCP server on stdio."""
    mcp.run()

if __name__ == "__main__":
    main()
