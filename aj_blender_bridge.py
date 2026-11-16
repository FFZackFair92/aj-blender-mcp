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
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > AJ MCP",
    "description": "Ponte locale tra Blender e il server AJ Blender MCP",
    "category": "Interface",
}

BRIDGE_VERSION = "1.1.0"
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


COMMANDS = {name[4:]: fn for name, fn in list(globals().items()) if name.startswith("cmd_")}


READ_ONLY = {"status", "scene_info", "object_info", "viewport_screenshot", "find_objects",
             "get_property", "api_info", "missing_files", "render_preview", "undo", "export_file",
             "render_image", "save_file"}


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
