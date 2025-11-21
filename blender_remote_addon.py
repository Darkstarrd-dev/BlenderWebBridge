bl_info = {
    "name": "Remote Bridge",
    "author": "Gemini & User",
    "version": (4, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Header",
    "description": "Sync Mesh, Lights and Code",
    "category": "Development",
}

import bpy
import sys
import subprocess
import threading
import traceback
import queue
import ast
import json
import asyncio
from io import StringIO
import concurrent.futures

# --- 依赖管理 ---
DEPENDENCY_INSTALLED = False
try:
    import websockets
    import rlcompleter
    DEPENDENCY_INSTALLED = True
except ImportError:
    DEPENDENCY_INSTALLED = False

# --- 全局状态 ---
SERVER_LOOP = None
STOP_FUTURE = None
SERVER_THREAD = None
EXECUTION_NAMESPACE = {
    "__name__": "__main__",
    "bpy": bpy,
    "C": bpy.context,
    "D": bpy.data,
    "math": sys.modules.get('math'),
}
execution_queue = queue.Queue()

# =================================================================================
# 1. 依赖安装
# =================================================================================
class REMOTE_OT_install_dependencies(bpy.types.Operator):
    bl_idname = "remote.install_dependencies"
    bl_label = "Install Dependencies"
    bl_description = "Install 'websockets'"

    def execute(self, context):
        try:
            self.report({'INFO'}, "Installing websockets...")
            python_exe = sys.executable
            subprocess.check_call([python_exe, "-m", "pip", "install", "websockets"])
            global DEPENDENCY_INSTALLED
            DEPENDENCY_INSTALLED = True
            import site; import importlib; importlib.reload(site)
            global websockets; import websockets
            self.report({'INFO'}, "Ready!")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed: {str(e)}")
            return {'CANCELLED'}

# =================================================================================
# 2. 数据提取逻辑 (Mesh & Light)
# =================================================================================

def extract_mesh_data(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    vertices = [c for v in mesh.vertices for c in v.co]
    indices = [i for tri in mesh.loop_triangles for i in tri.vertices]
    return {
        "name": obj.name,
        "type": "MESH",
        "vertices": vertices,
        "indices": indices,
        "pos": obj.location[:],
        "rot": obj.rotation_euler[:],
        "scl": obj.scale[:]
    }

def extract_light_data(obj):
    light = obj.data
    # Blender Light Types: POINT, SUN, SPOT, AREA
    data = {
        "name": obj.name,
        "type": "LIGHT",
        "light_type": light.type,
        "color": light.color[:],
        "energy": light.energy,
        "pos": obj.location[:],
        "rot": obj.rotation_euler[:],
        "scl": obj.scale[:]
    }
    if light.type == 'SPOT':
        data['spot_size'] = light.spot_size
        data['spot_blend'] = light.spot_blend
    if light.type == 'AREA':
        data['shape'] = light.shape # SQUARE, RECTANGLE, DISK, ELLIPSE
        data['size'] = light.size
    return data

def main_thread_executor():
    while not execution_queue.empty():
        try:
            task = execution_queue.get_nowait()
            task_type = task.get('type')
            future = task.get('future')
        except queue.Empty: break

        if task_type == 'get_scene':
            try:
                scene_objects = []
                mesh_count = 0
                light_count = 0

                for obj in bpy.context.scene.objects:
                    if not obj.visible_get(): continue

                    if obj.type == 'MESH':
                        if len(obj.data.vertices) < 200000: # Increased limit
                            scene_objects.append(extract_mesh_data(obj))
                            mesh_count += 1
                    elif obj.type == 'LIGHT':
                        scene_objects.append(extract_light_data(obj))
                        light_count += 1

                print(f"[Remote] Exporting {mesh_count} Meshes, {light_count} Lights")

                if not future.done():
                    future.set_result({"status": "done", "type": "scene_data", "objects": scene_objects})
            except Exception as e:
                print(f"Export Error: {e}")
                if not future.done(): future.set_result({"status": "error", "msg": str(e)})

        elif task_type == 'update_object':
            try:
                obj = bpy.data.objects.get(task.get('name'))
                if obj:
                    if 'pos' in task: obj.location = task['pos']
                    if 'rot' in task: obj.rotation_euler = task['rot']
                    if 'scl' in task: obj.scale = task['scl']
                    # Sync light properties back if needed (optional, simplistic for now)
                    if obj.type == 'LIGHT' and 'energy' in task:
                        obj.data.energy = task['energy']
                    if obj.type == 'LIGHT' and 'color' in task:
                        obj.data.color = task['color']

                if not future.done(): future.set_result({"status": "done", "type": "update_ack"})
            except: pass

        elif task_type == 'complete':
            try:
                import rlcompleter
                completer = rlcompleter.Completer(EXECUTION_NAMESPACE)
                matches = [completer.complete(task.get('text', ''), i) for i in range(50)]
                matches = [m for m in matches if m]
                if not future.done(): future.set_result({"status": "done", "type": "completion_result", "matches": matches})
            except Exception as e:
                 if not future.done(): future.set_result({"status": "error", "msg": str(e)})

        else: # execute
            code = task.get('code', '')
            old_stdout = sys.stdout
            redirected_output = sys.stdout = StringIO()
            result_status = "Success"
            try:
                should_fallback = False
                try:
                    tree = ast.parse(code)
                    last_node = None
                    if tree.body and isinstance(tree.body[-1], ast.Expr): last_node = tree.body.pop()
                    if tree.body: exec(compile(tree, filename="<web>", mode="exec"), EXECUTION_NAMESPACE)
                    if last_node:
                        res = eval(compile(ast.Expression(last_node.value), filename="<web>", mode="eval"), EXECUTION_NAMESPACE)
                        if res is not None: print(repr(res))
                except: should_fallback = True
                if should_fallback: exec(code, EXECUTION_NAMESPACE)
            except:
                traceback.print_exc(file=sys.stdout)
                traceback.print_exc(file=redirected_output)
                result_status = "Error"
            finally:
                sys.stdout = old_stdout

            output_log = redirected_output.getvalue()
            if output_log.strip(): print(f"📄 [Web Log]:\n{output_log}\n" + "-"*20)

            if not future.done():
                future.set_result({"status": "done", "type": "execution_result", "result": result_status, "output": output_log})

    return 0.05

# --- Server Logic ---
async def handle_command(websocket):
    print(f"[Remote] Connected")
    try:
        async for message in websocket:
            data = json.loads(message)
            mtype = data.get("type", "execute")
            py_future = concurrent.futures.Future()

            if mtype == "update_object": execution_queue.put({"type": "update_object", **data, "future": py_future})
            elif mtype == "get_scene": execution_queue.put({"type": "get_scene", "future": py_future})
            elif mtype == "complete": execution_queue.put({"type": "complete", "text": data.get("text"), "future": py_future})
            else: execution_queue.put({"type": "execute", "code": data.get("code"), "future": py_future})

            while not py_future.done(): await asyncio.sleep(0.01)
            await websocket.send(json.dumps(py_future.result()))
    except: pass

async def async_server_main():
    global STOP_FUTURE
    print("[Remote] Starting 8081...")
    STOP_FUTURE = asyncio.Future()
    try:
        async with websockets.serve(handle_command, "localhost", 8081, ping_interval=None, max_size=100*1024*1024): # 100MB
            print("✅ Ready.")
            await STOP_FUTURE
    except Exception as e: print(f"❌ Error: {e}")

def server_thread_target():
    global SERVER_LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    SERVER_LOOP = loop
    try: loop.run_until_complete(async_server_main())
    finally: loop.close(); SERVER_LOOP = None

# --- Register ---
def start_server():
    global SERVER_THREAD
    if SERVER_THREAD and SERVER_THREAD.is_alive(): return
    SERVER_THREAD = threading.Thread(target=server_thread_target, daemon=True)
    SERVER_THREAD.start()

def stop_server():
    global SERVER_LOOP, STOP_FUTURE
    if SERVER_LOOP and STOP_FUTURE and not STOP_FUTURE.done():
        try: SERVER_LOOP.call_soon_threadsafe(STOP_FUTURE.set_result, True)
        except: pass

def update_toggle(self, context):
    if self.remote_connection_active: start_server()
    else: stop_server()

def draw_header_button(self, context):
    if DEPENDENCY_INSTALLED:
        icon = 'URL' if context.window_manager.remote_connection_active else 'UNLINKED'
        label = "Remote: ON" if context.window_manager.remote_connection_active else "Remote: OFF"
        self.layout.row(align=True).prop(context.window_manager, "remote_connection_active", text=label, icon=icon, toggle=True)
    else:
        self.layout.operator("remote.install_dependencies", icon="IMPORT", text="Install Libs")

def register():
    bpy.utils.register_class(REMOTE_OT_install_dependencies)
    bpy.types.WindowManager.remote_connection_active = bpy.props.BoolProperty(name="Remote", default=False, update=update_toggle)
    bpy.types.VIEW3D_HT_header.append(draw_header_button)
    if not bpy.app.timers.is_registered(main_thread_executor): bpy.app.timers.register(main_thread_executor)

def unregister():
    stop_server()
    if bpy.app.timers.is_registered(main_thread_executor): bpy.app.timers.unregister(main_thread_executor)
    bpy.types.VIEW3D_HT_header.remove(draw_header_button)
    del bpy.types.WindowManager.remote_connection_active
    bpy.utils.unregister_class(REMOTE_OT_install_dependencies)

if __name__ == "__main__": register()
