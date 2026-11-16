"""Ponte AJ MCP per Blender: add-on che riceve i comandi del server MCP.

Installazione: Edit > Preferences > Add-ons > Install from Disk, scegli questo
file e attivalo. Il ponte parte con Blender e ascolta su 127.0.0.1:9991.

Protocollo: POST / con corpo JSON {"cmd": "...", "args": {...}} e header
X-AJ-Token. Risposta JSON {"ok": true, "result": ...} oppure
{"ok": false, "error": "..."}.

Le richieste arrivano su un thread di rete, ma Blender va toccato solo dal
thread principale: ogni comando entra in una coda che un timer di Blender
svuota, e il thread di rete aspetta la risposta.

Sicurezza: il ponte esegue codice Python in Blender, quindi ascolta solo su
127.0.0.1 e richiede il token salvato in ~/.aj-mcp/blender.token.
"""
import base64
import contextlib
import hmac
import io
import json
import math
import os
import queue
import secrets
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bmesh
import bpy

bl_info = {
    "name": "AJ MCP Bridge",
    "author": "AJ Consulting",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > AJ MCP",
    "description": "Ponte locale tra Blender e il server AJ Blender MCP",
    "category": "Interface",
}

BRIDGE_VERSION = "1.0.0"
PORT = int(os.environ.get("AJ_BLENDER_PORT", "9991"))
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".aj-mcp", "blender.token")
WAIT_SECONDS = 180

_jobs = queue.Queue()
_server = None


# ---------------------------------------------------------------- utilita'
def _ensure_token():
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            if f.read().strip():
                return
    except OSError:
        pass
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(secrets.token_urlsafe(32))


def _token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _plain(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    try:
        return [_plain(x) for x in v]
    except TypeError:
        return getattr(v, "name", None) or str(v)


def _vec(v, digits=4):
    return [round(float(x), digits) for x in v]


def _deg(euler):
    return [round(math.degrees(a), 3) for a in euler]


def _obj(name):
    o = bpy.data.objects.get(name)
    if o is None:
        raise ValueError(f"oggetto non trovato: {name}")
    return o


def _three(value, what):
    if isinstance(value, (int, float)):
        return (float(value),) * 3
    if len(value) != 3:
        raise ValueError(f"{what} vuole 3 numeri (x, y, z)")
    return tuple(float(x) for x in value)


def _placement(args):
    """Valida posizione, rotazione e scala PRIMA di creare o toccare l'oggetto."""
    out = {}
    if args.get("location") is not None:
        out["location"] = _three(args["location"], "location")
    if args.get("rotation") is not None:
        out["rotation_euler"] = tuple(math.radians(a) for a in _three(args["rotation"], "rotation"))
    if args.get("scale") is not None:
        out["scale"] = _three(args["scale"], "scale")
    return out


def _place(obj, placement):
    for key, value in placement.items():
        setattr(obj, key, value)


def _window():
    wm = bpy.context.window_manager
    if not wm.windows:
        raise RuntimeError("Blender non ha finestre aperte (modalita' background?)")
    return wm.windows[0]


def _view3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is not None:
                    return window, area, region
    raise RuntimeError("nessuna vista 3D aperta: apri un editor 3D Viewport")


def _op(*paths):
    """Il primo operatore che esiste in questa versione di Blender, es.
    _op("wm.obj_export", "export_scene.obj"): i nomi cambiano tra versioni."""
    for path in paths:
        group, name = path.split(".")
        try:
            op = getattr(getattr(bpy.ops, group), name)
            check = getattr(op, "get_rna_type", None)
            if check is not None:
                check()  # solleva se l'operatore non e' registrato
        except Exception:
            continue
        return op
    raise RuntimeError(f"operatore non disponibile in questa versione di Blender: {paths[0]}")


# ----------------------------------------------------------------- comandi
def cmd_status(args):
    scene = bpy.context.scene
    return {
        "bridge": BRIDGE_VERSION,
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or "(non salvato)",
        "scene": scene.name,
        "objects": len(scene.objects),
    }


def cmd_scene_info(args):
    scene = bpy.context.scene
    limit = int(args.get("limit", 100))
    objects = list(scene.objects)
    render = scene.render
    return {
        "scene": scene.name,
        "engine": render.engine,
        "resolution": [render.resolution_x, render.resolution_y],
        "frames": [scene.frame_start, scene.frame_end, scene.frame_current],
        "camera": scene.camera.name if scene.camera else None,
        "count": len(objects),
        "truncated": len(objects) > limit,
        "objects": [
            {
                "name": o.name,
                "type": o.type,
                "location": _vec(o.location),
                "hidden": bool(o.hide_viewport),
                "parent": o.parent.name if o.parent else None,
                "collections": [c.name for c in o.users_collection],
            }
            for o in objects[:limit]
        ],
    }


def cmd_object_info(args):
    o = _obj(args["name"])
    info = {
        "name": o.name,
        "type": o.type,
        "location": _vec(o.location),
        "rotation": _deg(o.rotation_euler),
        "scale": _vec(o.scale),
        "dimensions": _vec(o.dimensions),
        "parent": o.parent.name if o.parent else None,
        "children": [c.name for c in o.children],
        "materials": [s.material.name if s.material else None for s in o.material_slots],
        "modifiers": [{"name": m.name, "type": m.type} for m in o.modifiers],
        "hidden": bool(o.hide_viewport),
    }
    if o.type == "MESH":
        info["mesh"] = {
            "vertices": len(o.data.vertices),
            "edges": len(o.data.edges),
            "faces": len(o.data.polygons),
        }
    return info


def _mesh(kind, name, size):
    bm = bmesh.new()
    r = size / 2
    if kind == "cube":
        bmesh.ops.create_cube(bm, size=size)
    elif kind == "plane":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=r)
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=r)
    elif kind == "ico_sphere":
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=r)
    elif kind == "cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32, radius1=r, radius2=r, depth=size)
    elif kind == "cone":
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32, radius1=r, radius2=0, depth=size)
    elif kind == "circle":
        bmesh.ops.create_circle(bm, cap_ends=True, segments=32, radius=r)
    elif kind == "monkey":
        bmesh.ops.create_monkey(bm)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return mesh


MESH_KINDS = ("cube", "plane", "sphere", "ico_sphere", "cylinder", "cone", "circle", "monkey")
LIGHT_KINDS = {"point_light": "POINT", "sun": "SUN", "spot_light": "SPOT", "area_light": "AREA"}
ALL_KINDS = MESH_KINDS + tuple(LIGHT_KINDS) + ("empty", "camera", "text")


def cmd_create_object(args):
    kind = str(args["kind"]).lower()
    if kind not in ALL_KINDS:
        raise ValueError(f"tipo sconosciuto: {kind} (validi: {', '.join(ALL_KINDS)})")
    name = args.get("name") or kind.replace("_", " ").title().replace(" ", "")
    size = float(args.get("size", 2.0))
    placement = _placement(args)
    if kind in MESH_KINDS:
        data = _mesh(kind, name, size)
    elif kind in LIGHT_KINDS:
        data = bpy.data.lights.new(name, type=LIGHT_KINDS[kind])
    elif kind == "camera":
        data = bpy.data.cameras.new(name)
    elif kind == "text":
        data = bpy.data.curves.new(name, type="FONT")
        data.body = str(args.get("text") or "Testo")
    else:  # empty
        data = None
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    _place(obj, placement)
    return {"name": obj.name, "type": obj.type, "location": _vec(obj.location)}


def cmd_delete_objects(args):
    deleted, missing = [], []
    for name in args["names"]:
        o = bpy.data.objects.get(name)
        if o is None:
            missing.append(name)
        else:
            bpy.data.objects.remove(o, do_unlink=True)
            deleted.append(name)
    return {"deleted": deleted, "missing": missing}


def cmd_transform_object(args):
    o = _obj(args["name"])
    _place(o, _placement(args))
    return {"name": o.name, "location": _vec(o.location), "rotation": _deg(o.rotation_euler),
            "scale": _vec(o.scale)}


def _rgba(value, what):
    value = [float(x) for x in value]
    if len(value) == 3:
        value.append(1.0)
    if len(value) != 4:
        raise ValueError(f"{what} vuole 3 o 4 numeri tra 0 e 1 (r, g, b, a)")
    return value


def cmd_set_material(args):
    o = _obj(args["object"])
    if not hasattr(o.data, "materials"):
        raise ValueError(f"{o.name} ({o.type}) non puo' avere materiali")
    name = args.get("name") or f"{o.name}_mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        raise RuntimeError(f"il materiale {name} non ha un nodo Principled BSDF")

    def put(names, value):
        for n in names:  # i nomi degli ingressi cambiano tra versioni di Blender
            if n in bsdf.inputs:
                bsdf.inputs[n].default_value = value
                return
        raise RuntimeError(f"ingresso non trovato nel Principled BSDF: {names[0]}")

    if args.get("color") is not None:
        rgba = _rgba(args["color"], "color")
        put(("Base Color",), rgba)
        mat.diffuse_color = rgba
    if args.get("metallic") is not None:
        put(("Metallic",), float(args["metallic"]))
    if args.get("roughness") is not None:
        put(("Roughness",), float(args["roughness"]))
    if args.get("emission_color") is not None:
        put(("Emission Color", "Emission"), _rgba(args["emission_color"], "emission_color"))
    if args.get("emission_strength") is not None:
        put(("Emission Strength",), float(args["emission_strength"]))

    if len(o.material_slots):
        o.material_slots[0].material = mat
    else:
        o.data.materials.append(mat)
    return {"object": o.name, "material": mat.name}


def cmd_add_modifier(args):
    o = _obj(args["object"])
    kind = str(args["type"]).upper()
    mod = o.modifiers.new(args.get("name") or kind.title(), kind)
    if mod is None:
        raise ValueError(f"il modificatore {kind} non si puo' aggiungere a {o.name} ({o.type})")
    for key, value in (args.get("settings") or {}).items():
        if not hasattr(mod, key):
            o.modifiers.remove(mod)
            raise ValueError(f"il modificatore {kind} non ha l'impostazione '{key}'")
        setattr(mod, key, value)
    return {"object": o.name, "modifier": mod.name, "type": mod.type}


def cmd_run_python(args):
    scope = {"bpy": bpy, "bmesh": bmesh, "result": None}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(args["code"], scope)
    return {"result": _plain(scope.get("result")), "stdout": out.getvalue()[-8000:]}


def cmd_viewport_screenshot(args):
    window, area, region = _view3d()
    render = bpy.context.scene.render
    max_size = max(64, min(int(args.get("max_size", 800)), 2000))
    saved = (render.resolution_percentage, render.filepath, render.image_settings.file_format)
    path = os.path.join(tempfile.gettempdir(), f"aj_mcp_{os.getpid()}.png")
    try:
        longest = max(render.resolution_x, render.resolution_y)
        render.resolution_percentage = max(1, min(100, int(100 * max_size / longest)))
        render.image_settings.file_format = "PNG"
        render.filepath = path
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
        with open(path, "rb") as f:
            data = f.read()
    finally:
        render.resolution_percentage, render.filepath, render.image_settings.file_format = saved
        with contextlib.suppress(OSError):
            os.remove(path)
    return {"png_base64": base64.b64encode(data).decode("ascii")}


def _ext(path):
    return os.path.splitext(path)[1].lower()


def cmd_import_file(args):
    path = os.path.abspath(os.path.expanduser(args["path"]))
    if not os.path.isfile(path):
        raise ValueError(f"file non trovato: {path}")
    ext = _ext(path)
    before = set(bpy.data.objects.keys())
    with bpy.context.temp_override(window=_window()):
        if ext in (".glb", ".gltf"):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".fbx":
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext == ".obj":
            _op("wm.obj_import", "import_scene.obj")(filepath=path)
        elif ext == ".stl":
            _op("wm.stl_import", "import_mesh.stl")(filepath=path)
        elif ext in (".usd", ".usda", ".usdc", ".usdz"):
            bpy.ops.wm.usd_import(filepath=path)
        else:
            raise ValueError(f"formato non supportato: {ext} (validi: glb, gltf, fbx, obj, stl, usd)")
    return {"file": path, "imported": sorted(set(bpy.data.objects.keys()) - before)}


def _export_mesh(new, old, path, sel):
    """OBJ e STL: operatore nuovo se c'e', altrimenti quello delle versioni vecchie."""
    try:
        op = _op(new)
    except RuntimeError:
        return _op(old)(filepath=path, use_selection=sel)
    return op(filepath=path, export_selected_objects=sel)


def cmd_export_file(args):
    path = os.path.abspath(os.path.expanduser(args["path"]))
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        raise ValueError(f"cartella non trovata: {folder}")
    ext, sel = _ext(path), bool(args.get("selected_only", False))
    with bpy.context.temp_override(window=_window()):
        if ext in (".glb", ".gltf"):
            bpy.ops.export_scene.gltf(filepath=path, use_selection=sel,
                                      export_format="GLB" if ext == ".glb" else "GLTF_SEPARATE")
        elif ext == ".fbx":
            bpy.ops.export_scene.fbx(filepath=path, use_selection=sel)
        elif ext == ".obj":
            _export_mesh("wm.obj_export", "export_scene.obj", path, sel)
        elif ext == ".stl":
            _export_mesh("wm.stl_export", "export_mesh.stl", path, sel)
        elif ext in (".usd", ".usda", ".usdc"):
            bpy.ops.wm.usd_export(filepath=path, selected_objects_only=sel)
        else:
            raise ValueError(f"formato non supportato: {ext} (validi: glb, gltf, fbx, obj, stl, usd)")
    return {"file": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}


def cmd_save_file(args):
    path = args.get("path")
    with bpy.context.temp_override(window=_window()):
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            if not path.lower().endswith(".blend"):
                raise ValueError("il file deve finire in .blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
        elif bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
        else:
            raise ValueError("il file non e' mai stato salvato: indica un percorso .blend")
    return {"file": bpy.data.filepath}


COMMANDS = {name[4:]: fn for name, fn in list(globals().items()) if name.startswith("cmd_")}


# ------------------------------------------------- coda e thread principale
def _run(body):
    try:
        fn = COMMANDS.get(body.get("cmd"))
        if fn is None:
            raise ValueError(f"comando sconosciuto: {body.get('cmd')}")
        return {"ok": True, "result": fn(body.get("args") or {})}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:]}


def _pump():
    """Timer di Blender: esegue sul thread principale i comandi in coda."""
    if _server is None:
        return None
    for _ in range(8):
        try:
            job = _jobs.get_nowait()
        except queue.Empty:
            break
        job["reply"] = _run(job["body"])
        job["done"].set()
    return 0.05


# -------------------------------------------------------------------- rete
class _Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        token = _token()
        if not token or not hmac.compare_digest(self.headers.get("X-AJ-Token", ""), token):
            return self._send(401, {"ok": False, "error": "token mancante o errato"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "error": "corpo JSON non valido"})
        job = {"body": body, "done": threading.Event(), "reply": None}
        _jobs.put(job)
        if not job["done"].wait(WAIT_SECONDS):
            return self._send(504, {"ok": False, "error": "Blender non ha risposto in tempo (occupato?)"})
        self._send(200, job["reply"])

    def do_GET(self):
        self._send(405, {"ok": False, "error": "usa POST"})

    def log_message(self, *args):
        pass


def start():
    global _server
    if _server is not None:
        return
    _ensure_token()
    _server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, name="aj-mcp-bridge", daemon=True).start()
    bpy.app.timers.register(_pump, persistent=True)


def stop():
    global _server
    server, _server = _server, None
    if server is not None:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- pannello
class AJMCP_PT_panel(bpy.types.Panel):
    bl_label = "AJ MCP"
    bl_idname = "AJMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AJ MCP"

    def draw(self, context):
        col = self.layout.column()
        if _server is not None:
            col.label(text=f"Ponte attivo su 127.0.0.1:{PORT}", icon="CHECKMARK")
        else:
            col.label(text="Ponte fermo", icon="ERROR")
        col.label(text=f"Versione {BRIDGE_VERSION}")


def register():
    bpy.utils.register_class(AJMCP_PT_panel)
    try:
        start()
    except OSError as e:
        print(f"AJ MCP: impossibile aprire la porta {PORT}: {e}")


def unregister():
    stop()
    bpy.utils.unregister_class(AJMCP_PT_panel)


if __name__ == "__main__":
    register()
