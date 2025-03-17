import hou
import json
import threading
import socket
import time
import requests
import tempfile
import traceback
import os
import shutil
import sys
from PySide2 import QtWidgets, QtCore

# Info about the extension (optional metadata)
EXTENSION_NAME = "Houdini MCP"
EXTENSION_VERSION = (0, 1)
EXTENSION_DESCRIPTION = "Connect Houdini to Claude via MCP"

class HoudiniMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b''  # Buffer for incomplete data
        self.timer = None

    def start(self):
        """Begin listening on the given port; sets up a QTimer to poll for data."""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)
            
            # Create a timer in the main thread to process server events
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._process_server)
            self.timer.start(100)  # 100ms interval
            
            print(f"HoudiniMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            
    def stop(self):
        """Stop listening; close sockets and timers."""
        self.running = False
        if self.timer:
            self.timer.stop()
            self.timer = None
        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()
        self.socket = None
        self.client = None
        print("HoudiniMCP server stopped")

    def _process_server(self):
        """
        Timer callback to accept connections and process any incoming data.
        This runs in the main Houdini thread to avoid concurrency issues.
        """
        if not self.running:
            return
        
        try:
            # Accept new connections if we don't already have a client
            if not self.client and self.socket:
                try:
                    self.client, address = self.socket.accept()
                    self.client.setblocking(False)
                    print(f"Connected to client: {address}")
                except BlockingIOError:
                    pass  # No connection waiting
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
            
            # Process data from existing client
            if self.client:
                try:
                    data = self.client.recv(8192)
                    if data:
                        self.buffer += data
                        try:
                            # Attempt to parse JSON
                            command = json.loads(self.buffer.decode('utf-8'))
                            # If successful, clear the buffer and process
                            self.buffer = b''
                            response = self.execute_command(command)
                            response_json = json.dumps(response)
                            self.client.sendall(response_json.encode('utf-8'))
                        except json.JSONDecodeError:
                            # Incomplete data; keep appending to buffer
                            pass
                    else:
                        # Connection closed by client
                        print("Client disconnected")
                        self.client.close()
                        self.client = None
                        self.buffer = b''
                except BlockingIOError:
                    pass  # No data available
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    self.client.close()
                    self.client = None
                    self.buffer = b''

        except Exception as e:
            print(f"Server error: {str(e)}")

    # -------------------------------------------------------------------------
    # Command Handling
    # -------------------------------------------------------------------------
    
    def execute_command(self, command):
        """Entry point for executing a JSON command from the client."""
        try:
            return self._execute_command_internal(command)
        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """
        Internal dispatcher that looks up 'cmd_type' from the JSON,
        calls the relevant function, and returns a JSON-friendly dict.
        """
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Always-available handlers
        handlers = {
            "get_scene_info": self.get_scene_info,
            "create_node": self.create_node,
            "modify_node": self.modify_node,
            "delete_node": self.delete_node,
            "get_node_info": self.get_node_info,
            "execute_code": self.execute_code,
            "set_material": self.set_material,
            "get_asset_lib_status": self.get_asset_lib_status,
        }
        
        # If user has toggled asset library usage
        if getattr(hou.session, "houdinimcp_use_assetlib", False):
            asset_handlers = {
                "get_asset_categories": self.get_asset_categories,
                "search_assets": self.search_assets,
                "import_asset": self.import_asset,
            }
            handlers.update(asset_handlers)

        handler = handlers.get(cmd_type)
        if not handler:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        
        print(f"Executing handler for {cmd_type}")
        result = handler(**params)
        print(f"Handler execution complete for {cmd_type}")
        return {"status": "success", "result": result}

    # -------------------------------------------------------------------------
    # Basic Info & Node Operations
    # -------------------------------------------------------------------------

    def get_asset_lib_status(self):
        """Checks if the user toggled asset library usage in hou.session."""
        use_assetlib = getattr(hou.session, "houdinimcp_use_assetlib", False)
        msg = ("Asset library usage is enabled." 
               if use_assetlib 
               else "Asset library usage is disabled.")
        return {"enabled": use_assetlib, "message": msg}

    def get_scene_info(self):
        """Returns basic info about the current .hip file and a few top-level nodes."""
        try:
            hip_file = hou.hipFile.name()
            scene_info = {
                "name": os.path.basename(hip_file) if hip_file else "Untitled",
                "filepath": hip_file or "",
                "node_count": len(hou.node("/").allSubChildren()),
                "nodes": [],
                "fps": hou.fps(),
                "start_frame": hou.playbar.frameRange()[0],
                "end_frame": hou.playbar.frameRange()[1],
            }
            
            # Collect limited node info from key contexts
            root = hou.node("/")
            contexts = ["obj", "shop", "out", "ch", "vex", "stage"]
            top_nodes = []
            
            for ctx_name in contexts:
                ctx_node = root.node(ctx_name)
                if ctx_node:
                    children = ctx_node.children()
                    for node in children:
                        if len(top_nodes) >= 10:
                            break
                        top_nodes.append({
                            "name": node.name(),
                            "path": node.path(),
                            "type": node.type().name(),
                            "category": ctx_name,
                        })
                    if len(top_nodes) >= 10:
                        break
            
            scene_info["nodes"] = top_nodes
            return scene_info
        
        except Exception as e:
            traceback.print_exc()
            return {"error": str(e)}

    def create_node(self, node_type, parent_path="/obj", name=None, position=None, parameters=None):
        """Creates a new node in the specified parent."""
        try:
            parent = hou.node(parent_path)
            if not parent:
                raise ValueError(f"Parent path not found: {parent_path}")
            
            node = parent.createNode(node_type, node_name=name)
            if position and len(position) >= 2:
                node.setPosition([position[0], position[1]])
            if parameters:
                for p_name, p_val in parameters.items():
                    parm = node.parm(p_name)
                    if parm:
                        parm.set(p_val)
            
            return {
                "name": node.name(),
                "path": node.path(),
                "type": node.type().name(),
                "position": list(node.position()),
            }
        except Exception as e:
            raise Exception(f"Failed to create node: {str(e)}")

    def modify_node(self, path, parameters=None, position=None, name=None):
        """Modifies an existing node."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        
        changes = []
        old_name = node.name()
        
        if name and name != old_name:
            node.setName(name)
            changes.append(f"Renamed from {old_name} to {name}")
        
        if position and len(position) >= 2:
            node.setPosition([position[0], position[1]])
            changes.append(f"Position set to {position}")
        
        if parameters:
            for p_name, p_val in parameters.items():
                parm = node.parm(p_name)
                if parm:
                    old_val = parm.eval()
                    parm.set(p_val)
                    changes.append(f"Parameter {p_name} changed from {old_val} to {p_val}")
        
        return {"path": node.path(), "changes": changes}

    def delete_node(self, path):
        """Deletes a node from the scene."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        node_path = node.path()
        node_name = node.name()
        node.destroy()
        return {"deleted": node_path, "name": node_name}

    def get_node_info(self, path):
        """Returns detailed information about a single node."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        
        node_info = {
            "name": node.name(),
            "path": node.path(),
            "type": node.type().name(),
            "category": node.type().category().name(),
            "position": [node.position()[0], node.position()[1]],
            "color": list(node.color()) if node.color() else None,
            "is_bypassed": node.isBypassed(),
            "is_displayed": getattr(node, "isDisplayFlagSet", lambda: None)(),
            "is_rendered": getattr(node, "isRenderFlagSet", lambda: None)(),
            "parameters": [],
            "inputs": [],
            "outputs": []
        }

        # Limit to 20 parameters for brevity
        for i, parm in enumerate(node.parms()):
            if i >= 20:
                break
            node_info["parameters"].append({
                "name": parm.name(),
                "label": parm.label(),
                "value": str(parm.eval()),
                "raw_value": parm.rawValue(),
                "type": parm.parmTemplate().type().name()
            })

        # Inputs
        for i, in_node in enumerate(node.inputs()):
            if in_node:
                node_info["inputs"].append({
                    "index": i,
                    "name": in_node.name(),
                    "path": in_node.path(),
                    "type": in_node.type().name()
                })

        # Outputs
        for i, out_conn in enumerate(node.outputConnections()):
            out_node = out_conn.outputNode()
            node_info["outputs"].append({
                "index": i,
                "name": out_node.name(),
                "path": out_node.path(),
                "type": out_node.type().name(),
                "input_index": out_conn.inputIndex()
            })

        return node_info

    def execute_code(self, code):
        """Executes arbitrary Python code within Houdini."""
        try:
            namespace = {"hou": hou}
            exec(code, namespace)
            return {"executed": True}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")

    # -------------------------------------------------------------------------
    # set_material (now completed)
    # -------------------------------------------------------------------------
    def set_material(self, node_path, material_type="principledshader", name=None, parameters=None):
        """
        Creates or applies a material to an OBJ node. 
        For example, we can create a Principled Shader in /mat 
        and assign it to a geometry node or set the 'shop_materialpath'.
        """
        try:
            target_node = hou.node(node_path)
            if not target_node:
                raise ValueError(f"Node not found: {node_path}")
            
            # Verify it's an OBJ node (i.e., category Object)
            if target_node.type().category().name() != "Object":
                raise ValueError(
                    f"Node {node_path} is not an OBJ-level node and cannot accept direct materials."
                )

            # Attempt to create/find a material in /mat (or /shop)
            mat_context = hou.node("/mat")
            if not mat_context:
                # Fallback: try /shop if /mat doesn't exist
                mat_context = hou.node("/shop")
                if not mat_context:
                    raise RuntimeError("No /mat or /shop context found to create materials.")

            mat_name = name or (f"{material_type}_auto")
            mat_node = mat_context.node(mat_name)
            if not mat_node:
                # Create a new material node
                mat_node = mat_context.createNode(material_type, mat_name)

            # Apply any parameter overrides
            if parameters:
                for k, v in parameters.items():
                    p = mat_node.parm(k)
                    if p:
                        p.set(v)

            # Now assign this material to the OBJ node
            # Typically, you either set a “shop_materialpath” parameter 
            # or inside the geometry, you create a Material SOP.
            mat_parm = target_node.parm("shop_materialpath")
            if mat_parm:
                mat_parm.set(mat_node.path())
            else:
                # If there's a geometry node inside, we might make or update a Material SOP
                geo_sop = target_node.node("geometry")
                if not geo_sop:
                    raise RuntimeError("No 'geometry' node found inside OBJ to apply material to.")
                
                material_sop = geo_sop.node("material1")
                if not material_sop:
                    material_sop = geo_sop.createNode("material", "material1")
                    # Hook it up to the chain
                    # For a brand-new geometry node, there's often a 'file1' SOP or similar
                    first_sop = None
                    for c in geo_sop.children():
                        if c.isDisplayFlagSet():
                            first_sop = c
                            break
                    if first_sop:
                        material_sop.setFirstInput(first_sop)
                    material_sop.setDisplayFlag(True)
                    material_sop.setRenderFlag(True)

                # The Material SOP typically has shop_materialpath1, shop_materialpath2, etc.
                mat_sop_parm = material_sop.parm("shop_materialpath1")
                if mat_sop_parm:
                    mat_sop_parm.set(mat_node.path())
                else:
                    raise RuntimeError(
                        "No shop_materialpath1 on Material SOP to assign the material."
                    )

            return {
                "status": "ok",
                "material_node": mat_node.path(),
                "applied_to": target_node.path(),
            }

        except Exception as e:
            traceback.print_exc()
            return {"status": "error", "message": str(e), "node": node_path}

    # -------------------------------------------------------------------------
    # Placeholder asset library methods
    # -------------------------------------------------------------------------
    def get_asset_categories(self):
        """Placeholder for an asset library feature (e.g., Poly Haven)."""
        return {"error": "get_asset_categories not implemented"}

    def search_assets(self):
        """Placeholder for asset search logic."""
        return {"error": "search_assets not implemented"}

    def import_asset(self):
        """Placeholder for asset import logic."""
        return {"error": "import_asset not implemented"}
