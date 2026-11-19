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
import fnmatch
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
    "version": (1, 2, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > AJ MCP",
    "description": "Ponte locale tra Blender e il server AJ Blender MCP",
    "category": "Interface",
}

BRIDGE_VERSION = "1.2.0"
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


def _render_png(max_size, opengl):
    """Render (o vista OpenGL) ridotto a max_size sul lato lungo, in PNG;
    le impostazioni di render tornano com'erano."""
    render = bpy.context.scene.render
    max_size = max(64, min(int(max_size), 2000))
    saved = (render.resolution_percentage, render.filepath, render.image_settings.file_format)
    path = os.path.join(tempfile.gettempdir(), f"aj_mcp_{os.getpid()}.png")
    try:
        longest = max(render.resolution_x, render.resolution_y)
        render.resolution_percentage = max(1, min(100, int(100 * max_size / longest)))
        render.image_settings.file_format = "PNG"
        render.filepath = path
        if opengl:
            window, area, region = _view3d()
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.opengl(write_still=True, view_context=True)
        else:
            with bpy.context.temp_override(window=_window()):
                bpy.ops.render.render(write_still=True)
        with open(path, "rb") as f:
            data = f.read()
    finally:
        render.resolution_percentage, render.filepath, render.image_settings.file_format = saved
        with contextlib.suppress(OSError):
            os.remove(path)
    return {"png_base64": base64.b64encode(data).decode("ascii")}


def cmd_viewport_screenshot(args):
    return _render_png(args.get("max_size", 800), opengl=True)


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


# ------------------------------------------------------- comandi aggiunti (1.1)
def _coll(name):
    c = bpy.data.collections.get(name)
    if c is None:
        raise ValueError(f"collezione non trovata: {name}")
    return c


def cmd_find_objects(args):
    pattern = (args.get("name") or "*").lower()
    kind = (args.get("type") or "").upper()
    coll = args.get("collection")
    limit = int(args.get("limit", 200))
    hits = [o for o in bpy.context.scene.objects
            if fnmatch.fnmatchcase(o.name.lower(), pattern)
            and (not kind or o.type == kind)
            and (not coll or coll in [c.name for c in o.users_collection])]
    return {"count": len(hits), "truncated": len(hits) > limit,
            "objects": [{"name": o.name, "type": o.type, "location": _vec(o.location)} for o in hits[:limit]]}


def cmd_duplicate_object(args):
    src = _obj(args["name"])
    count = max(1, min(int(args.get("count", 1)), 200))
    offset = _three(args.get("offset", 0), "offset")
    linked = bool(args.get("linked", False))
    colls = list(src.users_collection) or [bpy.context.scene.collection]
    made = []
    for i in range(1, count + 1):
        new = src.copy()
        if src.data is not None and not linked:
            new.data = src.data.copy()
        for c in colls:
            c.objects.link(new)
        new.location = tuple(src.location[k] + offset[k] * i for k in range(3))
        made.append(new.name)
    return {"source": src.name, "created": made, "linked": linked}


def cmd_set_parent(args):
    child = _obj(args["name"])
    parent = _obj(args["parent"]) if args.get("parent") else None
    if parent is not None and (parent == child or child in _ancestors(parent)):
        raise ValueError(f"{parent.name} non puo' essere genitore di {child.name} (ciclo)")
    world = child.matrix_world.copy()
    child.parent = parent
    if args.get("keep_transform", True):
        child.matrix_world = world
    return {"name": child.name, "parent": parent.name if parent else None, "location": _vec(child.location)}


def _ancestors(o):
    out = []
    while o.parent is not None:
        o = o.parent
        out.append(o)
    return out


def cmd_select_objects(args):
    objs = [_obj(n) for n in (args.get("names") or [])]  # validati prima di cambiare la selezione
    layer = bpy.context.view_layer
    if not args.get("extend"):
        for o in layer.objects:
            o.select_set(False)
    for o in objs:
        o.select_set(True)
    active = args.get("active") or (objs[0].name if objs else None)
    if active:
        layer.objects.active = _obj(active)
    return {"selected": [o.name for o in layer.objects if o.select_get()],
            "active": layer.objects.active.name if layer.objects.active else None}


def cmd_set_visibility(args):
    objs = [_obj(n) for n in args["names"]]
    for o in objs:
        if args.get("viewport") is not None:
            o.hide_viewport = not bool(args["viewport"])
        if args.get("render") is not None:
            o.hide_render = not bool(args["render"])
    return {"objects": [{"name": o.name, "viewport": not o.hide_viewport, "render": not o.hide_render}
                        for o in objs]}


def cmd_create_collection(args):
    name = str(args["name"])
    parent = _coll(args["parent"]) if args.get("parent") else bpy.context.scene.collection
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name)
        parent.children.link(c)
        return {"collection": c.name, "created": True}
    return {"collection": c.name, "created": False}


def cmd_move_to_collection(args):
    objs = [_obj(n) for n in args["names"]]
    target = bpy.data.collections.get(args["collection"])
    if target is None:
        if not args.get("create", True):
            raise ValueError(f"collezione non trovata: {args['collection']}")
        target = bpy.data.collections.new(args["collection"])
        bpy.context.scene.collection.children.link(target)
    for o in objs:
        if target not in o.users_collection:
            target.objects.link(o)
        if not args.get("keep_others"):
            for c in list(o.users_collection):
                if c != target:
                    c.objects.unlink(o)
    return {"collection": target.name, "moved": [o.name for o in objs]}


def _modifier(o, name):
    mod = o.modifiers.get(name)
    if mod is None:
        raise ValueError(f"{o.name} non ha il modificatore {name} (ha: {[m.name for m in o.modifiers]})")
    return mod


def cmd_apply_modifier(args):
    o = _obj(args["object"])
    if o.type != "MESH":
        raise ValueError(f"{o.name} ({o.type}): applico modificatori solo alle mesh")
    mod = _modifier(o, args["modifier"])
    if o.data.users > 1:
        raise ValueError(f"la mesh di {o.name} e' condivisa con altri oggetti: duplicala prima")
    with bpy.context.temp_override(window=_window(), object=o, active_object=o, selected_objects=[o]):
        bpy.ops.object.modifier_apply(modifier=mod.name)
    return {"object": o.name, "applied": args["modifier"], "modifiers": [m.name for m in o.modifiers]}


def cmd_remove_modifier(args):
    o = _obj(args["object"])
    o.modifiers.remove(_modifier(o, args["modifier"]))
    return {"object": o.name, "modifiers": [m.name for m in o.modifiers]}


def cmd_add_constraint(args):
    o = _obj(args["object"])
    kind = str(args["type"]).upper()
    target = _obj(args["target"]) if args.get("target") else None
    c = o.constraints.new(kind)
    try:
        if target is not None:
            if not hasattr(c, "target"):
                raise ValueError(f"il vincolo {kind} non ha un target")
            c.target = target
        for key, value in (args.get("settings") or {}).items():
            if key.startswith("_") or not hasattr(c, key):
                raise ValueError(f"il vincolo {kind} non ha l'impostazione '{key}'")
            setattr(c, key, value)
    except Exception:
        o.constraints.remove(c)
        raise
    return {"object": o.name, "constraint": c.name, "type": c.type}


# --- proprieta' per percorso: "data.energy", "modifiers.Bevel.width" ...
def _target(spec):
    spec = str(spec)
    if ":" in spec:
        kind, name = spec.split(":", 1)
        store = {"material": bpy.data.materials, "collection": bpy.data.collections,
                 "object": bpy.data.objects, "light": bpy.data.lights,
                 "camera": bpy.data.cameras, "mesh": bpy.data.meshes}.get(kind)
        if store is None:
            raise ValueError(f"tipo di bersaglio sconosciuto: {kind} (material, collection, object, light, camera, mesh)")
        item = store.get(name)
        if item is None:
            raise ValueError(f"{kind} non trovato: {name}")
        return item
    if spec == "scene":
        return bpy.context.scene
    if spec == "world":
        if bpy.context.scene.world is None:
            raise ValueError("la scena non ha un world")
        return bpy.context.scene.world
    return _obj(spec)


def _step(cur, part):
    if part.startswith("_"):
        raise ValueError(f"parte di percorso non consentita: {part}")
    if hasattr(cur, part):
        return getattr(cur, part)
    getter = getattr(cur, "get", None)
    if callable(getter):
        found = getter(part)
        if found is not None:
            return found
    if part.lstrip("-").isdigit():
        return cur[int(part)]
    raise ValueError(f"'{part}' non trovato in {getattr(cur, 'name', type(cur).__name__)}")


def _resolve(root, path):
    parts = [p for p in str(path).split(".") if p]
    if not parts:
        raise ValueError("percorso vuoto")
    cur = root
    for part in parts[:-1]:
        cur = _step(cur, part)
    last = parts[-1]
    if last.startswith("_"):
        raise ValueError(f"parte di percorso non consentita: {last}")
    if not hasattr(cur, last):
        raise ValueError(f"'{last}' non trovato in {getattr(cur, 'name', type(cur).__name__)}")
    return cur, last


def cmd_get_property(args):
    owner, attr = _resolve(_target(args["target"]), args["path"])
    return {"target": args["target"], "path": args["path"], "value": _plain(getattr(owner, attr))}


def cmd_set_property(args):
    owner, attr = _resolve(_target(args["target"]), args["path"])
    setattr(owner, attr, args["value"])
    return {"target": args["target"], "path": args["path"], "value": _plain(getattr(owner, attr))}


# --- animazione
def cmd_set_keyframe(args):
    o = _obj(args["object"])
    path = {"rotation": "rotation_euler"}.get(args.get("property", "location"), args.get("property", "location"))
    scene = bpy.context.scene
    frame = float(args["frame"]) if args.get("frame") is not None else float(scene.frame_current)
    owner, attr = _resolve(o, path)
    if args.get("value") is not None:
        value = args["value"]
        if attr == "rotation_euler":
            value = tuple(math.radians(a) for a in _three(value, "rotation"))
        setattr(owner, attr, value)
    index = int(args.get("index", -1))
    owner.keyframe_insert(data_path=attr, frame=frame, index=index)
    return {"object": o.name, "path": path, "frame": frame, "value": _plain(getattr(owner, attr))}


def cmd_set_frame(args):
    scene = bpy.context.scene
    if args.get("start") is not None:
        scene.frame_start = int(args["start"])
    if args.get("end") is not None:
        scene.frame_end = int(args["end"])
    if args.get("fps") is not None:
        scene.render.fps = int(args["fps"])
    if args.get("current") is not None:
        scene.frame_set(int(args["current"]))
    return {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end,
            "fps": scene.render.fps}


# --- camera e luci
def cmd_look_at(args):
    o = _obj(args["name"])
    if args.get("target"):
        point = tuple(_obj(args["target"]).matrix_world.translation)
    elif args.get("point") is not None:
        point = _three(args["point"], "point")
    else:
        raise ValueError("indica target (un oggetto) o point [x, y, z]")
    dx, dy, dz = (point[k] - o.location[k] for k in range(3))
    if abs(dx) + abs(dy) + abs(dz) < 1e-9:
        raise ValueError("l'oggetto e' gia' nel punto da guardare")
    # camere, luci spot e sun guardano lungo -Z locale
    o.rotation_euler = (math.atan2(math.hypot(dx, dy), -dz), 0.0, math.atan2(dy, dx) - math.pi / 2)
    return {"name": o.name, "rotation": _deg(o.rotation_euler)}


def cmd_set_camera(args):
    o = _obj(args["name"])
    if o.type != "CAMERA":
        raise ValueError(f"{o.name} non e' una camera ({o.type})")
    if args.get("lens") is not None:
        o.data.lens = float(args["lens"])
    if args.get("dof_object"):
        o.data.dof.use_dof = True
        o.data.dof.focus_object = _obj(args["dof_object"])
    if args.get("fstop") is not None:
        o.data.dof.use_dof = True
        o.data.dof.aperture_fstop = float(args["fstop"])
    if args.get("active", True):
        bpy.context.scene.camera = o
    return {"name": o.name, "lens": o.data.lens, "active": bpy.context.scene.camera == o}


def cmd_set_light(args):
    o = _obj(args["name"])
    if o.type != "LIGHT":
        raise ValueError(f"{o.name} non e' una luce ({o.type})")
    light = o.data
    if args.get("energy") is not None:
        light.energy = float(args["energy"])
    if args.get("color") is not None:
        light.color = _rgba(args["color"], "color")[:3]
    if args.get("size") is not None:
        setattr(light, "size" if light.type == "AREA" else "shadow_soft_size", float(args["size"]))
    if args.get("spot_size") is not None:
        if light.type != "SPOT":
            raise ValueError("spot_size vale solo per le luci spot")
        light.spot_size = math.radians(float(args["spot_size"]))
    return {"name": o.name, "type": light.type, "energy": light.energy, "color": _vec(light.color)}


def _node_of(tree, kind, bl_idname):
    node = next((n for n in tree.nodes if n.type == kind), None)
    return node or tree.nodes.new(bl_idname)


def cmd_set_world(args):
    scene = bpy.context.scene
    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    world = scene.world
    world.use_nodes = True
    tree = world.node_tree
    bg = _node_of(tree, "BACKGROUND", "ShaderNodeBackground")
    out = _node_of(tree, "OUTPUT_WORLD", "ShaderNodeOutputWorld")
    if not bg.outputs["Background"].links:
        tree.links.new(bg.outputs["Background"], out.inputs["Surface"])
    if args.get("hdri"):
        path = os.path.abspath(os.path.expanduser(args["hdri"]))
        if not os.path.isfile(path):
            raise ValueError(f"file non trovato: {path}")
        env = _node_of(tree, "TEX_ENVIRONMENT", "ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(path, check_existing=True)
        tree.links.new(env.outputs["Color"], bg.inputs["Color"])
    elif args.get("color") is not None:
        for link in list(bg.inputs["Color"].links):
            tree.links.remove(link)
        bg.inputs["Color"].default_value = _rgba(args["color"], "color")
    if args.get("strength") is not None:
        bg.inputs["Strength"].default_value = float(args["strength"])
    return {"world": world.name, "hdri": args.get("hdri"), "strength": bg.inputs["Strength"].default_value}


# --- render
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF",
           ".tiff": "TIFF", ".bmp": "BMP", ".webp": "WEBP"}
ENGINES = {"eevee": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"), "cycles": ("CYCLES",),
           "workbench": ("BLENDER_WORKBENCH",)}


def cmd_set_render(args):
    scene = bpy.context.scene
    render = scene.render
    if args.get("engine"):
        wanted = ENGINES.get(str(args["engine"]).lower(), (str(args["engine"]).upper(),))
        for name in wanted:
            try:
                render.engine = name
                break
            except TypeError:
                continue
        else:
            raise ValueError(f"motore non disponibile: {args['engine']} (eevee, cycles, workbench)")
    if args.get("resolution") is not None:
        w, h = (int(x) for x in args["resolution"])
        render.resolution_x, render.resolution_y = w, h
    if args.get("percentage") is not None:
        render.resolution_percentage = max(1, min(int(args["percentage"]), 100))
    if args.get("fps") is not None:
        render.fps = int(args["fps"])
    if args.get("samples") is not None:
        samples = int(args["samples"])
        if render.engine == "CYCLES":
            scene.cycles.samples = samples
        elif hasattr(scene, "eevee"):
            scene.eevee.taa_render_samples = samples
    if args.get("transparent") is not None:
        render.film_transparent = bool(args["transparent"])
    return {"engine": render.engine, "resolution": [render.resolution_x, render.resolution_y],
            "percentage": render.resolution_percentage, "fps": render.fps,
            "transparent": bool(render.film_transparent)}


def cmd_render_image(args):
    path = os.path.abspath(os.path.expanduser(args["path"]))
    fmt = FORMATS.get(_ext(path))
    if fmt is None:
        raise ValueError(f"estensione non supportata: {_ext(path)} (validi: {', '.join(FORMATS)})")
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError(f"cartella non trovata: {os.path.dirname(path)}")
    render = bpy.context.scene.render
    saved = (render.filepath, render.image_settings.file_format, render.use_file_extension)
    try:
        render.filepath, render.image_settings.file_format, render.use_file_extension = path, fmt, False
        with bpy.context.temp_override(window=_window()):
            bpy.ops.render.render(write_still=True)
    finally:
        render.filepath, render.image_settings.file_format, render.use_file_extension = saved
    return {"file": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "resolution": [render.resolution_x * render.resolution_percentage // 100,
                           render.resolution_y * render.resolution_percentage // 100]}


def cmd_render_preview(args):
    return _render_png(args.get("max_size", 640), opengl=False)


def cmd_set_viewport(args):
    window, area, region = _view3d()
    space = area.spaces.active
    if args.get("shading"):
        kind = str(args["shading"]).upper()
        if kind not in ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"):
            raise ValueError("shading: wireframe, solid, material o rendered")
        space.shading.type = kind
    frame = args.get("frame")
    if frame:
        with bpy.context.temp_override(window=window, area=area, region=region):
            if frame == "all":
                bpy.ops.view3d.view_all()
            else:
                cmd_select_objects({"names": [frame]})
                bpy.ops.view3d.view_selected()
    if args.get("camera_view"):
        if bpy.context.scene.camera is None:
            raise ValueError("la scena non ha una camera attiva")
        space.region_3d.view_perspective = "CAMERA"
    return {"shading": space.shading.type, "framed": frame}


# --- documentazione e controlli
def cmd_api_info(args):
    """Proprieta' di un tipo RNA di Blender (SubsurfModifier, Light, Object...),
    o elenco dei tipi che corrispondono a un modello con * e ?."""
    name = str(args["name"])
    if "*" in name or "?" in name:
        names = sorted(n for n in dir(bpy.types) if fnmatch.fnmatchcase(n.lower(), name.lower()))
        return {"pattern": name, "count": len(names), "types": names[:300]}
    cls = getattr(bpy.types, name, None)
    if cls is None or not hasattr(cls, "bl_rna"):
        raise ValueError(f"tipo sconosciuto: {name} (prova con un modello, es. '*Modifier')")
    rna = cls.bl_rna
    needle = (args.get("filter") or "").lower()
    props = []
    for p in rna.properties:
        if p.identifier == "rna_type" or (needle and needle not in p.identifier.lower()):
            continue
        item = {"name": p.identifier, "type": p.type, "label": p.name,
                "description": (p.description or "")[:200], "read_only": bool(p.is_readonly)}
        if p.type == "ENUM":
            item["items"] = [e.identifier for e in p.enum_items][:60]
        props.append(item)
    return {"type": name, "description": rna.description, "properties": props}


def cmd_missing_files(args):
    missing = []
    for kind in ("images", "libraries", "sounds", "movieclips", "fonts", "volumes", "cache_files"):
        for item in getattr(bpy.data, kind, ()):
            raw = getattr(item, "filepath", "") or ""
            if not raw or raw.startswith("<") or getattr(item, "packed_file", None) is not None:
                continue
            if getattr(item, "source", "FILE") not in ("FILE", "SEQUENCE", "MOVIE", "TILED"):
                continue
            full = bpy.path.abspath(raw)
            if not os.path.exists(full):
                missing.append({"kind": kind, "name": item.name, "path": full})
    return {"count": len(missing), "missing": missing}


def cmd_undo(args):
    steps = max(1, min(int(args.get("steps", 1)), 50))
    op = bpy.ops.ed.redo if args.get("redo") else bpy.ops.ed.undo
    with bpy.context.temp_override(window=_window()):
        for _ in range(steps):
            op()
    return {"action": "redo" if args.get("redo") else "undo", "steps": steps}


def cmd_batch(args):
    """Piu' comandi in fila; si ferma al primo errore."""
    results = []
    for i, step in enumerate(args.get("commands") or []):
        name = step.get("cmd")
        if name in ("batch", "undo"):
            raise ValueError(f"passo {i}: {name} non si puo' usare dentro batch")
        fn = COMMANDS.get(name)
        if fn is None:
            raise ValueError(f"passo {i}: comando sconosciuto {name}")
        try:
            results.append(fn(step.get("args") or {}))
        except Exception as e:
            raise RuntimeError(f"passo {i} ({name}) fallito dopo {i} riusciti: {type(e).__name__}: {e}") from None
    return {"steps": len(results), "results": results}


# ------------------------------------------------------- comandi aggiunti (1.2)
CHECKPOINT_DIR = os.path.join(os.path.expanduser("~"), ".aj-mcp", "blender-checkpoints")


def _cp_name(name):
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name or "")).strip("_")
    if not clean:
        raise ValueError("serve un nome per il checkpoint (lettere, numeri, - e _)")
    return clean[:60]


def _cp_list():
    out = []
    if os.path.isdir(CHECKPOINT_DIR):
        for f in sorted(os.listdir(CHECKPOINT_DIR)):
            if f.endswith(".blend"):
                meta = {}
                with contextlib.suppress(Exception), open(os.path.join(CHECKPOINT_DIR, f[:-6] + ".json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
                out.append({"name": f[:-6], "bytes": os.path.getsize(os.path.join(CHECKPOINT_DIR, f)), **meta})
    return out


def cmd_checkpoint(args):
    """Punti di ripristino dell'intera scena: save (copia del file, la scena aperta non cambia), list,
    restore (riapre quella copia: tutto torna com'era), delete."""
    action = args.get("action", "list")
    if action == "list":
        return {"folder": CHECKPOINT_DIR, "checkpoints": _cp_list()}
    name = _cp_name(args.get("name"))
    path = os.path.join(CHECKPOINT_DIR, name + ".blend")
    if action == "save":
        os.makedirs(CHECKPOINT_DIR, exist_ok=True)
        with bpy.context.temp_override(window=_window()):
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        import time as _time
        meta = {"saved_at": _time.strftime("%Y-%m-%d %H:%M:%S"), "original_file": bpy.data.filepath or None,
                "objects": len(bpy.context.scene.objects), "note": args.get("note") or ""}
        with open(path[:-6] + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        return {"saved": name, "file": path, **meta}
    if not os.path.isfile(path):
        raise ValueError(f"checkpoint non trovato: {name} (ci sono: {[c['name'] for c in _cp_list()]})")
    if action == "delete":
        for p in (path, path[:-6] + ".json"):
            with contextlib.suppress(OSError):
                os.remove(p)
        return {"deleted": name}
    if action == "restore":
        meta = next((c for c in _cp_list() if c["name"] == name), {})

        def _load():
            with contextlib.suppress(Exception), bpy.context.temp_override(window=_window()):
                bpy.ops.wm.open_mainfile(filepath=path)
            return None

        bpy.app.timers.register(_load, first_interval=0.3)  # dopo la risposta: l'apertura azzera il contesto
        return {"restoring": name, "file": path, "original_file": meta.get("original_file"),
                "note": "Blender riapre il checkpoint tra un attimo. Per tenerlo sul file originale: save_file(path=original_file)."}
    raise ValueError("action: save | list | restore | delete")


def cmd_save_copy(args):
    """Copia del file attuale per il render in background (la scena aperta non cambia)."""
    folder = os.path.join(tempfile.gettempdir(), "aj-mcp-render")
    os.makedirs(folder, exist_ok=True)
    import time as _time
    path = os.path.join(folder, f"render_{int(_time.time() * 1000)}.blend")
    with bpy.context.temp_override(window=_window()):
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    scene = bpy.context.scene
    return {"file": path, "frames": [scene.frame_start, scene.frame_end], "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage]}


def _mesh_obj(name):
    o = _obj(name)
    if o.type != "MESH":
        raise ValueError(f"{o.name} e' {o.type}: serve una mesh")
    return o


def _counts(me):
    return {"vertices": len(me.vertices), "edges": len(me.edges), "faces": len(me.polygons)}


MESH_OPS = ("subdivide", "bevel", "triangulate", "merge_by_distance", "recalc_normals", "flip_normals",
            "dissolve_limited", "inset", "shade_smooth", "shade_flat", "smooth_vertices")


def cmd_mesh_ops(args):
    """Operazioni sulla mesh senza modalita' modifica (bmesh): subdivide, bevel, triangulate, merge_by_distance,
    recalc_normals, flip_normals, dissolve_limited, inset, smooth_vertices, shade_smooth, shade_flat."""
    o = _mesh_obj(args["object"])
    op = str(args["op"]).lower()
    if op not in MESH_OPS:
        raise ValueError(f"op: {', '.join(MESH_OPS)}")
    s = args.get("settings") or {}
    me = o.data
    if me.users > 1:
        raise ValueError(f"la mesh di {o.name} e' condivisa con altri oggetti: duplicala prima")
    before = _counts(me)
    if op in ("shade_smooth", "shade_flat"):
        smooth = op == "shade_smooth"
        for p in me.polygons:
            p.use_smooth = smooth
        if smooth and s.get("auto_angle") is not None:
            with contextlib.suppress(Exception), bpy.context.temp_override(window=_window(), object=o, active_object=o, selected_objects=[o]):
                bpy.ops.object.shade_auto_smooth(angle=math.radians(float(s["auto_angle"])))
        return {"object": o.name, "op": op, **_counts(me)}
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        ops = bmesh.ops
        if op == "subdivide":
            ops.subdivide_edges(bm, edges=bm.edges[:], cuts=int(s.get("cuts", 1)), use_grid_fill=True,
                                smooth=float(s.get("smooth", 0.0)))
        elif op == "bevel":
            ops.bevel(bm, geom=bm.verts[:] + bm.edges[:], offset=float(s.get("width", 0.05)),
                      segments=int(s.get("segments", 2)), affect="EDGES", profile=float(s.get("profile", 0.5)))
        elif op == "triangulate":
            ops.triangulate(bm, faces=bm.faces[:])
        elif op == "merge_by_distance":
            ops.remove_doubles(bm, verts=bm.verts[:], dist=float(s.get("distance", 0.0001)))
        elif op == "recalc_normals":
            ops.recalc_face_normals(bm, faces=bm.faces[:])
        elif op == "flip_normals":
            ops.reverse_faces(bm, faces=bm.faces[:])
        elif op == "dissolve_limited":
            ops.dissolve_limit(bm, angle_limit=math.radians(float(s.get("angle", 5))), verts=bm.verts[:], edges=bm.edges[:])
        elif op == "inset":
            ops.inset_region(bm, faces=bm.faces[:], thickness=float(s.get("thickness", 0.05)), depth=float(s.get("depth", 0.0)))
        elif op == "smooth_vertices":
            for _ in range(max(1, min(int(s.get("repeat", 1)), 50))):
                ops.smooth_vert(bm, verts=bm.verts[:], factor=float(s.get("factor", 0.5)),
                                use_axis_x=True, use_axis_y=True, use_axis_z=True)
        bm.to_mesh(me)
    finally:
        bm.free()
    with contextlib.suppress(Exception):
        me.update()
    return {"object": o.name, "op": op, "before": before, "after": _counts(me)}


UV_METHODS = {"smart": "smart_project", "unwrap": "unwrap", "cube": "cube_project", "sphere": "sphere_project",
              "cylinder": "cylinder_project", "lightmap": "lightmap_pack"}


def cmd_uv_unwrap(args):
    """UV di una mesh in un colpo: smart (Smart UV Project), unwrap (sulle cuciture), cube, sphere, cylinder, lightmap."""
    o = _mesh_obj(args["object"])
    method = str(args.get("method", "smart")).lower()
    if method not in UV_METHODS:
        raise ValueError(f"method: {', '.join(UV_METHODS)}")
    window, area, region = _view3d()
    layer = bpy.context.view_layer
    for ob in layer.objects:
        ob.select_set(False)
    o.select_set(True)
    layer.objects.active = o
    kwargs = {}
    if method == "smart":
        kwargs = {"angle_limit": math.radians(float(args.get("angle", 66))), "island_margin": float(args.get("margin", 0.02))}
    elif method == "unwrap":
        kwargs = {"margin": float(args.get("margin", 0.001))}
    elif method == "cube":
        kwargs = {"cube_size": float(args.get("size", 1.0))}
    with bpy.context.temp_override(window=window, area=area, region=region, object=o, active_object=o):
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            getattr(bpy.ops.uv, UV_METHODS[method])(**kwargs)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
    layers = [uv.name for uv in getattr(o.data, "uv_layers", [])]
    return {"object": o.name, "method": method, "uv_layers": layers}


BOOL_OPS = {"difference": "DIFFERENCE", "union": "UNION", "intersect": "INTERSECT"}


def cmd_boolean(args):
    """Boolean tra due mesh: difference (buca), union (unisce), intersect. apply=true la applica subito;
    hide_cutter nasconde l'oggetto che taglia."""
    o, cutter = _mesh_obj(args["object"]), _mesh_obj(args["cutter"])
    if o == cutter:
        raise ValueError("object e cutter devono essere diversi")
    kind = BOOL_OPS.get(str(args.get("operation", "difference")).lower())
    if kind is None:
        raise ValueError("operation: difference | union | intersect")
    mod = o.modifiers.new(args.get("name") or f"Bool_{cutter.name}", "BOOLEAN")
    mod.operation, mod.object = kind, cutter
    with contextlib.suppress(Exception):
        mod.solver = "EXACT"
    if args.get("hide_cutter", True):
        cutter.hide_viewport = cutter.hide_render = True
    out = {"object": o.name, "cutter": cutter.name, "operation": kind.lower(), "modifier": mod.name, "applied": False}
    if args.get("apply"):
        cmd_apply_modifier({"object": o.name, "modifier": mod.name})
        out["applied"] = True
        if o.type == "MESH":
            out["mesh"] = _counts(o.data)
    return out


def _fcurves(ad):
    """F-curve dell'azione: API classica fino a 4.x, channelbag delle azioni a strati (4.4+, unica in 5.x)."""
    if ad is None or ad.action is None:
        return []
    fc = getattr(ad.action, "fcurves", None)
    if fc is not None:
        with contextlib.suppress(Exception):
            len(fc)
            return fc
    with contextlib.suppress(Exception):
        from bpy_extras import anim_utils
        bag = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        if bag is not None:
            return bag.fcurves
    return []


INTERP = {"linear": "LINEAR", "bezier": "BEZIER", "constant": "CONSTANT", "ease": "BEZIER", "bounce": "BOUNCE",
          "elastic": "ELASTIC", "back": "BACK", "sine": "SINE"}


def cmd_animate(args):
    """Piu' chiavi in un colpo: keys=[{"frame": 1, "value": [0,0,0]}, {"frame": 48, "value": [0,0,3]}].
    interpolation: linear, bezier, constant, bounce, elastic, back, sine. cycles=true ripete all'infinito.
    clear=true cancella prima l'animazione di quella proprieta'."""
    o = _obj(args["object"])
    prop = {"rotation": "rotation_euler"}.get(args.get("property", "location"), args.get("property", "location"))
    keys = args.get("keys") or []
    if not keys and not args.get("clear"):
        raise ValueError("servono keys [{frame, value}, ...]")
    interp = INTERP.get(str(args.get("interpolation", "bezier")).lower())
    if interp is None:
        raise ValueError(f"interpolation: {', '.join(INTERP)}")
    owner, attr = _resolve(o, prop)
    ad = getattr(o, "animation_data", None)
    if args.get("clear"):
        fcs = _fcurves(ad)
        for fc in [f for f in fcs if f.data_path == attr]:
            fcs.remove(fc)
    for k in keys:
        if "frame" not in k or "value" not in k:
            raise ValueError("ogni chiave vuole frame e value")
    for k in keys:
        value = k["value"]
        if attr == "rotation_euler":
            value = tuple(math.radians(a) for a in _three(value, "rotation"))
        setattr(owner, attr, value)
        owner.keyframe_insert(data_path=attr, frame=float(k["frame"]))
    ad = getattr(o, "animation_data", None)
    curves = 0
    for fc in _fcurves(ad):
        if fc.data_path != attr:
            continue
        curves += 1
        for kp in fc.keyframe_points:
            kp.interpolation = interp
            if str(args.get("interpolation", "")).lower() == "ease":
                kp.easing = "EASE_IN_OUT"
        if args.get("cycles") and not any(m.type == "CYCLES" for m in fc.modifiers):
            fc.modifiers.new("CYCLES")
    frames = [float(k["frame"]) for k in keys]
    if frames and args.get("fit_range"):
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = int(min(frames)), int(max(frames))
    return {"object": o.name, "property": prop, "keys": len(keys), "curves": curves, "interpolation": interp.lower(),
            "range": [min(frames), max(frames)] if frames else None}


def _gn_inputs(group):
    items = []
    iface = getattr(group, "interface", None)
    if iface is not None:  # Blender 4.0+
        for it in iface.items_tree:
            if getattr(it, "item_type", "SOCKET") == "SOCKET" and getattr(it, "in_out", "INPUT") == "INPUT":
                items.append({"name": it.name, "identifier": it.identifier, "type": getattr(it, "socket_type", "")})
    else:  # 3.x
        for it in group.inputs:
            items.append({"name": it.name, "identifier": it.identifier, "type": it.type})
    return items


def cmd_geometry_nodes(args):
    """Geometry Nodes su un oggetto: list (gruppi nel file e ingressi del modificatore), add (modificatore con un
    gruppo esistente), set (valori degli ingressi per nome: {"Count": 20, "Radius": 0.5})."""
    action = args.get("action", "list")
    groups = [g for g in bpy.data.node_groups if getattr(g, "bl_idname", "") == "GeometryNodeTree"]
    if action == "list" and not args.get("object"):
        return {"groups": [{"name": g.name, "inputs": _gn_inputs(g)} for g in groups]}
    o = _obj(args["object"])
    mods = [m for m in o.modifiers if m.type == "NODES"]
    if action == "add":
        gname = args.get("group")
        g = bpy.data.node_groups.get(gname) if gname else None
        if g is None:
            raise ValueError(f"gruppo Geometry Nodes non trovato: {gname} (ci sono: {[x.name for x in groups]})")
        mod = o.modifiers.new(args.get("name") or g.name, "NODES")
        mod.node_group = g
        mods = [mod]
    mod = None
    if args.get("modifier"):
        mod = _modifier(o, args["modifier"])
    elif mods:
        mod = mods[-1]
    if mod is None:
        raise ValueError(f"{o.name} non ha modificatori Geometry Nodes: usa action='add'")
    inputs = _gn_inputs(mod.node_group) if mod.node_group else []
    if action == "set":
        by_name = {i["name"].lower(): i["identifier"] for i in inputs}
        values = args.get("inputs") or {}
        bad = [k for k in values if k.lower() not in by_name and k not in {i["identifier"] for i in inputs}]
        if bad:
            raise ValueError(f"ingressi sconosciuti: {bad} (ci sono: {[i['name'] for i in inputs]})")
        for k, v in values.items():
            mod[by_name.get(k.lower(), k)] = tuple(v) if isinstance(v, list) else v
        with contextlib.suppress(Exception):
            o.update_tag()
    elif action not in ("list", "add"):
        raise ValueError("action: list | add | set")
    current = {}
    for i in inputs:
        with contextlib.suppress(Exception):
            current[i["name"]] = _plain(mod[i["identifier"]])
    return {"object": o.name, "modifier": mod.name, "group": mod.node_group.name if mod.node_group else None,
            "inputs": inputs, "values": current}


TEX_SLOTS = {"base_color": "Base Color", "roughness": "Roughness", "metallic": "Metallic", "normal": "Normal",
             "emission": "Emission Color", "alpha": "Alpha", "bump": "Normal"}


def cmd_material_nodes(args):
    """Materiale avanzato: qualsiasi ingresso del Principled BSDF per nome (inputs={"Alpha": 0.5,
    "Transmission Weight": 1, "IOR": 1.45}) e texture da file collegate al posto giusto
    (texture=percorso, slot: base_color, roughness, metallic, normal, bump, emission, alpha)."""
    if args.get("object"):
        o = _obj(args["object"])
        if not hasattr(o.data, "materials"):
            raise ValueError(f"{o.name} ({o.type}) non puo' avere materiali")
        if not len(o.material_slots) or o.material_slots[0].material is None:
            cmd_set_material({"object": o.name, "name": args.get("material") or None})
        mat = o.material_slots[0].material
    else:
        mat = bpy.data.materials.get(args.get("material") or "")
        if mat is None:
            raise ValueError("indica object oppure il nome di un material esistente")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        raise RuntimeError(f"il materiale {mat.name} non ha un nodo Principled BSDF")
    inputs = args.get("inputs") or {}
    bad = [k for k in inputs if k not in bsdf.inputs]
    if bad:
        raise ValueError(f"ingressi non trovati nel Principled BSDF: {bad} (ci sono: {list(bsdf.inputs.keys())[:40]})")
    for k, v in inputs.items():
        bsdf.inputs[k].default_value = _rgba(v, k) if isinstance(v, list) and len(v) in (3, 4) and "Color" in k else v
    linked = None
    if args.get("texture"):
        path = os.path.abspath(os.path.expanduser(args["texture"]))
        if not os.path.isfile(path):
            raise ValueError(f"texture non trovata: {path}")
        slot = str(args.get("slot", "base_color")).lower()
        target = TEX_SLOTS.get(slot)
        if target is None or target not in bsdf.inputs:
            raise ValueError(f"slot: {', '.join(TEX_SLOTS)}")
        img = bpy.data.images.load(path, check_existing=True)
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = img
        if slot not in ("base_color", "emission"):
            with contextlib.suppress(Exception):
                img.colorspace_settings.name = "Non-Color"
        if slot in ("normal", "bump"):
            mid = nodes.new("ShaderNodeNormalMap" if slot == "normal" else "ShaderNodeBump")
            links.new(tex.outputs["Color"], mid.inputs["Color" if slot == "normal" else "Height"])
            links.new(mid.outputs["Normal"], bsdf.inputs["Normal"])
        else:
            links.new(tex.outputs["Alpha" if slot == "alpha" else "Color"], bsdf.inputs[target])
        linked = {"image": img.name, "slot": slot}
    return {"material": mat.name, "set": list(inputs), "texture": linked}


def cmd_rigid_body(args):
    """Fisica rigida: add (active = cade e urta, passive = pavimento/ostacolo, con mass, shape, friction, bounce),
    remove, bake (calcola la simulazione nel range della scena)."""
    action = args.get("action", "add")
    if action == "bake":
        with bpy.context.temp_override(window=_window()):
            bpy.ops.ptcache.bake_all(bake=True)
        return {"baked": True, "frames": [bpy.context.scene.frame_start, bpy.context.scene.frame_end]}
    objs = [_mesh_obj(n) for n in args.get("objects") or []]
    if not objs:
        raise ValueError("servono objects")
    kind = str(args.get("type", "active")).upper()
    if kind not in ("ACTIVE", "PASSIVE"):
        raise ValueError("type: active | passive")
    done = []
    for o in objs:
        with bpy.context.temp_override(window=_window(), object=o, active_object=o, selected_objects=[o]):
            if action == "remove":
                if getattr(o, "rigid_body", None) is not None:
                    bpy.ops.rigidbody.object_remove()
                done.append(o.name)
                continue
            if action != "add":
                raise ValueError("action: add | remove | bake")
            if getattr(o, "rigid_body", None) is None:
                bpy.ops.rigidbody.object_add(type=kind)
        rb = o.rigid_body
        rb.type = kind
        for key, attr in (("mass", "mass"), ("friction", "friction"), ("bounce", "restitution")):
            if args.get(key) is not None:
                setattr(rb, attr, float(args[key]))
        if args.get("shape"):
            rb.collision_shape = str(args["shape"]).upper()
        done.append(o.name)
    return {"action": action, "type": kind.lower(), "objects": done}


def cmd_scene_stats(args):
    """Quanto pesa la scena: vertici e facce totali e per oggetto (i piu' pesanti), modificatori che moltiplicano
    la geometria, materiali, immagini e loro risoluzione."""
    rows, tv, tf = [], 0, 0
    for o in bpy.context.scene.objects:
        if o.type != "MESH":
            continue
        v, f = len(o.data.vertices), len(o.data.polygons)
        tv, tf = tv + v, tf + f
        heavy = [f"{m.type}:{getattr(m, 'levels', '')}" for m in o.modifiers if m.type in ("SUBSURF", "MULTIRES", "ARRAY", "REMESH")]
        rows.append({"name": o.name, "vertices": v, "faces": f, **({"multiplying_modifiers": heavy} if heavy else {})})
    rows.sort(key=lambda r: r["faces"], reverse=True)
    imgs = []
    for im in bpy.data.images:
        size = list(getattr(im, "size", (0, 0)) or (0, 0))
        imgs.append({"name": im.name, "size": size})
    top = max(1, min(int(args.get("top", 10)), 100))
    return {"objects": len(bpy.context.scene.objects), "meshes": len(rows), "vertices": tv, "faces": tf,
            "heaviest": rows[:top], "materials": len(bpy.data.materials), "images": imgs[:50]}



COMMANDS = {name[4:]: fn for name, fn in list(globals().items()) if name.startswith("cmd_")}


READ_ONLY = {"status", "scene_info", "object_info", "viewport_screenshot", "find_objects",
             "get_property", "api_info", "missing_files", "render_preview", "undo", "export_file",
             "render_image", "save_file", "checkpoint", "save_copy", "scene_stats"}


def _undo_push(name):
    """Un passo di annullamento per ogni comando che cambia la scena (Ctrl+Z)."""
    with contextlib.suppress(Exception), bpy.context.temp_override(window=_window()):
        bpy.ops.ed.undo_push(message=f"AJ MCP {name}")


# ------------------------------------------------- coda e thread principale
def _run(body):
    try:
        fn = COMMANDS.get(body.get("cmd"))
        if fn is None:
            raise ValueError(f"comando sconosciuto: {body.get('cmd')}")
        result = fn(body.get("args") or {})
        if body.get("cmd") not in READ_ONLY:
            _undo_push(body.get("cmd"))
        return {"ok": True, "result": result}
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
