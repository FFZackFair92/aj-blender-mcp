"""Test offline: server MCP -> HTTP -> add-on vero -> finto Blender.

    python test_offline.py

L'add-on aj_blender_bridge.py gira davvero (rete, token, coda, comandi) contro
fake_bpy.py. Il finto Blender controlla che ogni comando tocchi i dati solo dal
thread principale. Non prova le API vere di Blender: per quello serve
check_live.py con Blender aperto.
"""
import asyncio
import importlib
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fake_bpy  # noqa: E402

fake_bpy.install()
home = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = home
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
os.environ["AJ_BLENDER_PORT"] = str(port)
os.environ["AJ_BLENDER_URL"] = f"http://127.0.0.1:{port}"
os.environ.pop("AJ_BLENDER_TOKEN", None)

bridge = importlib.import_module("aj_blender_bridge")
bridge.register()
token_file = Path(home) / ".aj-mcp" / "blender.token"
assert len(token_file.read_text(encoding="utf-8")) > 30

m = importlib.import_module("blender_mcp")


def do(fn, *args, **kwargs):
    """Chiama un tool da un thread, mentre questo thread fa da ciclo principale di Blender."""
    box = {}

    def work():
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=work)
    t.start()
    fake_bpy.run_main_loop(lambda: not t.is_alive())
    if "error" in box:
        raise box["error"]
    return box["value"]


def fails(needle, fn, *args, **kwargs):
    try:
        do(fn, *args, **kwargs)
    except RuntimeError as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError(f"mancava l'errore: {needle}")


# --- token
os.environ["AJ_BLENDER_TOKEN"] = "sbagliato"
fails("401", m.blender_status)
os.environ.pop("AJ_BLENDER_TOKEN")

# --- stato
s = do(m.blender_status)
assert s["blender"] == "4.5.0 Finto" and s["objects"] == 0 and s["file"] == "(non salvato)", s

# --- crea
cube = do(m.create_object, "cube", name="Cubo", location=[1, 2, 3], rotation=[0, 0, 90], scale=[1, 1, 2])
assert cube == {"name": "Cubo", "type": "MESH", "location": [1.0, 2.0, 3.0]}, cube
assert do(m.create_object, "cube", name="Cubo")["name"] == "Cubo.001"
for kind, typ in (("sphere", "MESH"), ("cone", "MESH"), ("point_light", "LIGHT"), ("camera", "CAMERA"),
                  ("empty", "EMPTY"), ("text", "FONT")):
    assert do(m.create_object, kind)["type"] == typ, kind
fails("tipo sconosciuto", m.create_object, "astronave")
fails("3 numeri", m.create_object, "cube", location=[1, 2])

info = do(m.object_info, "Cubo")
assert info["rotation"] == [0.0, 0.0, 90.0] and info["scale"] == [1.0, 1.0, 2.0], info
assert info["mesh"] == {"vertices": 8, "edges": 12, "faces": 6}, info
fails("oggetto non trovato", m.object_info, "Manca")

# --- trasforma
t = do(m.transform_object, "Cubo", location=[0, 0, 5])
assert t["location"] == [0.0, 0.0, 5.0] and t["rotation"] == [0.0, 0.0, 90.0], t

# --- materiale
mat = do(m.set_material, "Cubo", name="Rosso", color=[1, 0, 0], metallic=0.2, roughness=0.5,
         emission_color=[1, 0.5, 0], emission_strength=2.0)
assert mat == {"object": "Cubo", "material": "Rosso"}, mat
bsdf = fake_bpy.data.materials["Rosso"].node_tree.nodes[0]
assert bsdf.inputs["Base Color"].default_value == [1.0, 0.0, 0.0, 1.0]
assert bsdf.inputs["Emission Strength"].default_value == 2.0
do(m.set_material, "Cubo", name="Blu", color=[0, 0, 1, 1])
assert do(m.object_info, "Cubo")["materials"] == ["Blu"]
fails("non puo' avere materiali", m.set_material, "Empty", color=[1, 1, 1])
fails("3 o 4 numeri", m.set_material, "Cubo", color=[1, 1])

# --- modificatore
mod = do(m.add_modifier, "Cubo", "subsurf", settings={"levels": 2})
assert mod["type"] == "SUBSURF" and fake_bpy.data.objects["Cubo"].modifiers[0].levels == 2, mod
fails("non ha l'impostazione", m.add_modifier, "Cubo", "BEVEL", settings={"inventata": 1})
assert len(fake_bpy.data.objects["Cubo"].modifiers) == 1, "il modificatore sbagliato doveva essere tolto"

# --- scena
sc = do(m.scene_info)
assert sc["count"] == 8 and sc["resolution"] == [1920, 1080], sc
assert do(m.scene_info, limit=3)["truncated"] is True

# --- Python
out = do(m.run_python, "print('ciao'); result = sorted(bpy.data.objects.keys())[:2]")
assert out == {"result": ["Camera", "Cone"], "stdout": "ciao\n"}, out
fails("ZeroDivisionError", m.run_python, "1/0")

# --- vista: la percentuale di render torna com'era
img = do(m.viewport_screenshot, max_size=480)
assert img.data[:8] == b"\x89PNG\r\n\x1a\n"
assert fake_bpy.calls[-1] == ("opengl", 25), fake_bpy.calls[-1]
assert fake_bpy.scene.render.resolution_percentage == 100 and fake_bpy.scene.render.filepath == "/tmp/"

# --- file
work = Path(tempfile.mkdtemp())
src = work / "modello.glb"
src.write_bytes(b"x")
assert do(m.import_file, str(src))["imported"] == ["Importato"]
fails("file non trovato", m.import_file, str(work / "manca.glb"))
fails("formato non supportato", m.import_file, __file__)
exp = do(m.export_file, str(work / "scena.glb"), selected_only=True)
assert exp["bytes"] == 5 and fake_bpy.calls[-1] == ("export", {"use_selection": True, "export_format": "GLB"})
do(m.export_file, str(work / "scena.obj"))
assert fake_bpy.calls[-1] == ("export", {"export_selected_objects": False})
fails("cartella non trovata", m.export_file, str(work / "nessuna" / "a.glb"))
fails("mai stato salvato", m.save_file)
fails(".blend", m.save_file, str(work / "scena.txt"))
assert do(m.save_file, str(work / "scena.blend"))["file"].endswith("scena.blend")

# --- 1.1: ogni comando che cambia la scena e' un passo di annullamento
pushes = [c for c in fake_bpy.calls if c[0] == "undo_push"]
assert pushes and pushes[-1] == ("undo_push", {"message": "AJ MCP import_file"}), pushes[-1:]
n = len(pushes)
do(m.object_info, "Cubo")
assert len([c for c in fake_bpy.calls if c[0] == "undo_push"]) == n, "le letture non devono creare passi"
assert do(m.undo, steps=2) == {"action": "undo", "steps": 2}
assert fake_bpy.calls[-1] == ("undo", {}) and fake_bpy.calls[-2] == ("undo", {})

# --- ricerca
assert [o["name"] for o in do(m.find_objects, "cubo*")["objects"]] == ["Cubo", "Cubo.001"]
assert do(m.find_objects, type="light")["count"] == 1
assert do(m.find_objects, collection="Scene Collection")["count"] == 9

# --- duplica in fila
dup = do(m.duplicate_object, "Cubo.001", count=3, offset=[2, 0, 0])
assert dup["created"] == ["Cubo.002", "Cubo.003", "Cubo.004"], dup
assert fake_bpy.data.objects["Cubo.004"].location == (6.0, 0.0, 0.0)
assert fake_bpy.data.objects["Cubo.002"].data is not fake_bpy.data.objects["Cubo.001"].data
assert do(m.duplicate_object, "Cubo.001", linked=True)["linked"] is True
assert fake_bpy.data.objects["Cubo.005"].data is fake_bpy.data.objects["Cubo.001"].data

# --- genitore, selezione, visibilita'
assert do(m.set_parent, "Cubo.002", "Cubo.001")["parent"] == "Cubo.001"
fails("ciclo", m.set_parent, "Cubo.001", "Cubo.002")
assert do(m.set_parent, "Cubo.002")["parent"] is None
sel = do(m.select_objects, ["Cubo.003", "Cubo.004"])
assert sel == {"selected": ["Cubo.003", "Cubo.004"], "active": "Cubo.003"}, sel
assert do(m.select_objects, ["Sphere"], extend=True)["selected"] == ["Sphere", "Cubo.003", "Cubo.004"]
fails("oggetto non trovato", m.select_objects, ["Cubo.003", "Manca"])
assert len(do(m.select_objects, [], extend=True)["selected"]) == 3, "un nome sbagliato non deve cambiare la selezione"
v = do(m.set_visibility, ["Cubo.003"], viewport=False, render=False)
assert v["objects"] == [{"name": "Cubo.003", "viewport": False, "render": False}], v

# --- collezioni
assert do(m.create_collection, "Fila") == {"collection": "Fila", "created": True}
assert do(m.create_collection, "Fila")["created"] is False
assert do(m.create_collection, "Sotto", parent="Fila")["created"] is True
assert fake_bpy.data.collections["Fila"].children[0].name == "Sotto"
do(m.move_to_collection, ["Cubo.002", "Cubo.003"], "Fila")
assert [c.name for c in fake_bpy.data.objects["Cubo.002"].users_collection] == ["Fila"]
assert do(m.find_objects, collection="Fila")["count"] == 2
do(m.move_to_collection, ["Cubo.004"], "Nuova", keep_others=True)
assert [c.name for c in fake_bpy.data.objects["Cubo.004"].users_collection] == ["Scene Collection", "Nuova"]
fails("collezione non trovata", m.move_to_collection, ["Cubo.004"], "Inesistente", create=False)

# --- modificatori e vincoli
do(m.add_modifier, "Cubo.002", "BEVEL", name="Smusso")
assert do(m.apply_modifier, "Cubo.002", "Smusso")["modifiers"] == []
fails("non ha il modificatore", m.apply_modifier, "Cubo.002", "Smusso")
fails("solo alle mesh", m.apply_modifier, "Camera", "x")
do(m.add_modifier, "Cubo.003", "ARRAY", name="Fila")
assert do(m.remove_modifier, "Cubo.003", "Fila")["modifiers"] == []
ct = do(m.add_constraint, "Camera", "track_to", target="Cubo", settings={"track_axis": "TRACK_NEGATIVE_Z"})
assert ct["type"] == "TRACK_TO" and fake_bpy.data.objects["Camera"].constraints[0].target.name == "Cubo"
fails("non ha l'impostazione", m.add_constraint, "Camera", "COPY_LOCATION", target="Cubo", settings={"boh": 1})
fails("non ha un target", m.add_constraint, "Camera", "LIMIT_ROTATION", target="Cubo")
assert len(fake_bpy.data.objects["Camera"].constraints) == 1, "i vincoli sbagliati vanno tolti"

# --- proprieta' per percorso
assert do(m.get_property, "Cubo", "modifiers.Subsurf.levels")["value"] == 2
assert do(m.set_property, "Cubo", "modifiers.Subsurf.levels", 3)["value"] == 3
assert do(m.set_property, "PointLight", "data.energy", 50)["value"] == 50
assert do(m.get_property, "scene", "render.fps")["value"] == 24
assert do(m.get_property, "material:Blu", "use_nodes")["value"] is True
fails("non trovato", m.get_property, "Cubo", "modifiers.Manca.levels")
fails("non consentita", m.get_property, "Cubo", "data.__class__")
fails("tipo di bersaglio", m.get_property, "astronave:X", "name")
fails("material non trovato", m.get_property, "material:Verde", "name")

# --- documentazione
api = do(m.api_info, "SubsurfModifier")
assert [p["name"] for p in api["properties"]] == ["levels", "render_levels", "subdivision_type"], api
assert api["properties"][2]["items"] == ["CATMULL_CLARK", "SIMPLE"]
assert do(m.api_info, "*modifier")["types"] == ["BevelModifier", "SubsurfModifier"]
fails("tipo sconosciuto", m.api_info, "Astronave")

# --- animazione
k = do(m.set_keyframe, "Cubo", "rotation", frame=10, value=[0, 0, 45])
assert k["path"] == "rotation_euler" and fake_bpy.data.objects["Cubo"].keys[-1] == ("rotation_euler", 10.0, -1), k
do(m.set_keyframe, "PointLight", "data.energy", frame=1, value=10)
assert fake_bpy.data.lights["PointLight"].energy == 10
assert do(m.set_frame, current=5, start=1, end=96, fps=25) == {"current": 5, "start": 1, "end": 96, "fps": 25}

# --- camera e luci
do(m.transform_object, "Camera", location=[0, -10, 0])
la = do(m.look_at, "Camera", point=[0, 0, 0])
assert la["rotation"] == [90.0, 0.0, 0.0], la  # orizzontale verso +Y
do(m.transform_object, "Camera", location=[0, 0, 10])
assert do(m.look_at, "Camera", target="Cubo")["rotation"][0] == 0.0  # dall'alto, Cubo a z=5
fails("indica target", m.look_at, "Camera")
cam = do(m.set_camera, "Camera", lens=35, dof_object="Cubo", fstop=1.8)
assert cam == {"name": "Camera", "lens": 35.0, "active": True} and fake_bpy.scene.camera.name == "Camera"
fails("non e' una camera", m.set_camera, "Cubo")
li = do(m.set_light, "PointLight", energy=500, color=[1, 0.5, 0.2], size=0.5)
assert li["energy"] == 500 and fake_bpy.data.lights["PointLight"].shadow_soft_size == 0.5, li
fails("solo per le luci spot", m.set_light, "PointLight", spot_size=30)
fails("non e' una luce", m.set_light, "Cubo", energy=1)
w = do(m.set_world, color=[0.05, 0.05, 0.1], strength=0.5)
assert w["strength"] == 0.5 and fake_bpy.scene.world.use_nodes
hdr = Path(tempfile.mkdtemp()) / "cielo.hdr"
hdr.write_bytes(b"x")
do(m.set_world, hdri=str(hdr))
tree = fake_bpy.scene.world.node_tree
assert len(tree.links) == 2 and any(n.type == "TEX_ENVIRONMENT" for n in tree.nodes)
do(m.set_world, color=[1, 1, 1])
assert len(tree.links) == 1, "il colore deve staccare l'HDRI"
fails("file non trovato", m.set_world, hdri=str(hdr) + ".manca")

# --- render
r = do(m.set_render, engine="eevee", resolution=[1280, 720], samples=32, transparent=True)
assert r["engine"] == "BLENDER_EEVEE_NEXT" and r["resolution"] == [1280, 720] and r["transparent"], r
assert fake_bpy.scene.eevee.taa_render_samples == 32
do(m.set_render, engine="cycles", samples=128)
assert fake_bpy.scene.cycles.samples == 128
fails("motore non disponibile", m.set_render, engine="povray")
out = Path(tempfile.mkdtemp())
ri = do(m.render_image, str(out / "frame.jpg"))
assert ri["bytes"] > 0 and fake_bpy.calls[-1] == ("render", 100, "JPEG"), (ri, fake_bpy.calls[-1])
assert fake_bpy.scene.render.image_settings.file_format == "PNG" and fake_bpy.scene.render.filepath == "/tmp/"
fails("estensione non supportata", m.render_image, str(out / "frame.gif"))
prev = do(m.render_preview, max_size=320)
assert prev.data[:8] == b"\x89PNG\r\n\x1a\n" and fake_bpy.calls[-1] == ("render", 25, "PNG")
vp = do(m.set_viewport, shading="material", frame="all", camera_view=True)
assert vp == {"shading": "MATERIAL", "framed": "all"} and fake_bpy._space.region_3d.view_perspective == "CAMERA"
fails("shading:", m.set_viewport, shading="cartone")

# --- file mancanti
img = fake_bpy.data.images.new("texture.png")
img.filepath = "//textures/texture.png"
mf = do(m.missing_files)
assert mf["count"] == 1 and mf["missing"][0]["path"] == "/tmp/textures/texture.png", mf

# --- batch
before = len([c for c in fake_bpy.calls if c[0] == "undo_push"])
b = do(m.batch, [{"cmd": "create_object", "args": {"kind": "cube", "name": "Lotto"}},
                 {"cmd": "set_material", "args": {"object": "Lotto", "color": [0, 1, 0]}}])
assert b["steps"] == 2 and do(m.object_info, "Lotto")["materials"] == ["Lotto_mat"], b
assert len([c for c in fake_bpy.calls if c[0] == "undo_push"]) == before + 1, "batch = un solo passo"
fails("passo 1", m.batch, [{"cmd": "delete_objects", "args": {"names": ["Lotto"]}},
                           {"cmd": "object_info", "args": {"name": "Lotto"}}])
fails("non si puo' usare dentro batch", m.batch, [{"cmd": "undo"}])
do(m.delete_objects, ["Cubo.002", "Cubo.003", "Cubo.004", "Cubo.005"])

# --- 1.2: checkpoint, mesh, UV, boolean, animazione, geometry nodes, materiali, fisica, statistiche, render
cp = do(m.checkpoint, "save", "prima_dei_buchi", note="prova")
assert cp["saved"] == "prima_dei_buchi" and os.path.isfile(cp["file"]) and cp["note"] == "prova", cp
assert any(c[0] == "save_as" and c[2] is True for c in fake_bpy.calls), "il checkpoint deve essere una copia"
assert [c["name"] for c in do(m.checkpoint)["checkpoints"]] == ["prima_dei_buchi"]
r = do(m.checkpoint, "restore", "prima_dei_buchi")
assert r["restoring"] == "prima_dei_buchi"
fake_bpy.run_main_loop(lambda: any(c[0] == "open_mainfile" for c in fake_bpy.calls))
fails("checkpoint non trovato", m.checkpoint, "restore", "manca")
fails("serve un nome", m.checkpoint, "save", "")
do(m.checkpoint, "delete", "prima_dei_buchi")
assert do(m.checkpoint)["checkpoints"] == []

do(m.create_object, "cube", name="Blocco")
do(m.create_object, "cylinder", name="Punta")
r = do(m.mesh_ops, "Blocco", "subdivide", {"cuts": 2})
assert r["before"]["faces"] == 6 and r["after"]["faces"] == 54, r
r = do(m.mesh_ops, "Blocco", "shade_smooth")
assert all(p.use_smooth for p in fake_bpy.data.objects["Blocco"].data.polygons), r
fails("op:", m.mesh_ops, "Blocco", "esplodi")
r = do(m.uv_unwrap, "Blocco")
assert r["uv_layers"] == ["UVMap"] and any(c[0] == "smart_project" for c in fake_bpy.calls), r
assert [c[1]["mode"] for c in fake_bpy.calls if c[0] == "mode_set"][-2:] == ["EDIT", "OBJECT"]
fails("method:", m.uv_unwrap, "Blocco", "boh")
r = do(m.boolean, "Blocco", "Punta")
assert r["modifier"] == "Bool_Punta" and not r["applied"] and fake_bpy.data.objects["Punta"].hide_render, r
r = do(m.boolean, "Blocco", "Punta", "union", apply=True, hide_cutter=False)
assert r["applied"] and r["operation"] == "union", r
fails("diversi", m.boolean, "Blocco", "Blocco")
r = do(m.animate, "Blocco", [{"frame": 1, "value": [0, 0, 0]}, {"frame": 48, "value": [0, 0, 3]}],
       interpolation="linear", cycles=True, fit_range=True)
fc = fake_bpy.data.objects["Blocco"].animation_data.action.fcurves
assert r["keys"] == 2 and r["curves"] == 3 and all(k.interpolation == "LINEAR" for f in fc for k in f.keyframe_points), r
assert all(f.modifiers and f.modifiers[0].type == "CYCLES" for f in fc) and fake_bpy.scene.frame_end == 48
fails("interpolation", m.animate, "Blocco", [{"frame": 1, "value": [0, 0, 0]}], interpolation="strana")
fails("frame e value", m.animate, "Blocco", [{"frame": 1}])
fake_bpy.data.node_groups.new("Sparpaglia")
r = do(m.geometry_nodes)
assert r["groups"][0]["name"] == "Sparpaglia" and [i["name"] for i in r["groups"][0]["inputs"]] == ["Geometry", "Count"], r
r = do(m.geometry_nodes, "add", "Blocco", group="Sparpaglia")
assert r["values"]["Count"] == 10, r
r = do(m.geometry_nodes, "set", "Blocco", inputs={"count": 25})
assert r["values"]["Count"] == 25, r
fails("ingressi sconosciuti", m.geometry_nodes, "set", "Blocco", inputs={"Raggio": 1})
fails("non trovato", m.geometry_nodes, "add", "Blocco", group="Manca")
tex = Path(tempfile.mkdtemp()) / "legno.png"
tex.write_bytes(fake_bpy.PNG_1X1)
r = do(m.material_nodes, "Blocco", inputs={"Alpha": 0.5, "IOR": 1.45}, texture=str(tex), slot="base_color")
mat = fake_bpy.data.objects["Blocco"].material_slots[0].material
bsdf = mat.node_tree.nodes[0]
assert r["texture"]["slot"] == "base_color" and bsdf.inputs["Alpha"].default_value == 0.5, r
assert any(lk.to_socket is bsdf.inputs["Base Color"] for lk in mat.node_tree.links)
do(m.material_nodes, "Blocco", texture=str(tex), slot="normal")
assert any(n.type == "NORMAL_MAP" for n in mat.node_tree.nodes)
fails("ingressi non trovati", m.material_nodes, "Blocco", inputs={"Brillantezza": 1})
fails("texture non trovata", m.material_nodes, "Blocco", texture="/non/esiste.png")
do(m.create_object, "plane", name="Pavimento")
r = do(m.rigid_body, "add", ["Blocco"], mass=2.0, bounce=0.3)
assert fake_bpy.data.objects["Blocco"].rigid_body.mass == 2.0 and fake_bpy.data.objects["Blocco"].rigid_body.restitution == 0.3, r
do(m.rigid_body, "add", ["Pavimento"], type="passive")
assert fake_bpy.data.objects["Pavimento"].rigid_body.type == "PASSIVE"
assert do(m.rigid_body, "bake")["baked"]
do(m.rigid_body, "remove", ["Pavimento"])
assert fake_bpy.data.objects["Pavimento"].rigid_body is None
r = do(m.scene_stats, top=2)
assert r["faces"] > 0 and len(r["heaviest"]) == 2 and r["heaviest"][0]["faces"] >= r["heaviest"][1]["faces"], r
img = do(m.view_object, "Blocco", shading="material")
assert img.data[:4] == b"\x89PNG" and fake_bpy._space.shading.type == "MATERIAL"

# render in background con un finto Blender da riga di comando
fb = Path(tempfile.mkdtemp()) / "finto_blender.py"
fb.write_text("""import sys, time
a = sys.argv
out, s, e = a[a.index("-o") + 1], int(a[a.index("-s") + 1]), int(a[a.index("-e") + 1])
for f in range(s, e + 1):
    print(f"Fra:{f} Mem:10M | Rendering", flush=True)
    time.sleep(0.05)
    print(f"Saved: '{out.replace('####', str(f).zfill(4))}.png'", flush=True)
""")
os.environ["AJ_BLENDER_EXE"] = str(fb)
outdir = Path(tempfile.mkdtemp())
r = do(m.background_render, "start", str(outdir), frame_start=1, frame_end=3)
assert r["started"] and r["frames"] == [1, 3] and r["output"].endswith("frame_####"), r
end = time.time() + 15
while m.background_render("status")["state"] == "running" and time.time() < end:
    time.sleep(0.1)
st = m.background_render("status", job=r["job"])
assert st["state"] == "done" and st["saved"] == 3 and st["percent"] == 100.0, st
fb.write_text("import time\nprint('Fra:1', flush=True)\ntime.sleep(30)\n")
r2 = do(m.background_render, "start", str(outdir), frame_start=5)
time.sleep(0.5)
st = m.background_render("cancel", job=r2["job"])
assert st["state"] == "cancelled" and st["frames"] == [5, 5], st
assert len(m.background_render("list")["jobs"]) == 2
try:
    m.background_render("start")
    raise AssertionError("serviva output")
except RuntimeError as e:
    assert "serve output" in str(e)
os.environ.pop("AJ_BLENDER_EXE")
do(m.delete_objects, ["Blocco", "Punta", "Pavimento"])

# --- elimina
d = do(m.delete_objects, ["Cubo", "Manca"])
assert d == {"deleted": ["Cubo"], "missing": ["Manca"]}, d
assert do(m.blender_status)["objects"] == 8

# --- avvio: con il ponte attivo non lancia niente
os.environ["AJ_BLENDER_EXE"] = str(HERE / "non_esiste.exe")
already = do(m.open_blender)
assert already["opened"] is False and "gia' aperto" in already["note"], already

# --- Blender chiuso
bridge.unregister()
try:
    m.blender_status()
    raise AssertionError("mancava l'errore di collegamento")
except RuntimeError as e:
    assert "non risponde" in str(e), e

# --- avvio a programma chiuso: eseguibile mancante, poi eseguibile finto che non apre il ponte
try:
    m.open_blender(wait_seconds=1)
    raise AssertionError("mancava l'errore di eseguibile non trovato")
except RuntimeError as e:
    assert "non trovato" in str(e), e
os.environ["AJ_BLENDER_EXE"] = sys.executable
finto = Path(tempfile.mkdtemp()) / "finto.py"
finto.write_text("pass\n", encoding="utf-8")
try:
    m.open_blender(str(finto), wait_seconds=1)
    raise AssertionError("mancava l'errore di ponte che non risponde")
except RuntimeError as e:
    assert "avviato" in str(e) and "ponte non risponde" in str(e), e
try:
    m.open_blender(str(finto) + ".manca", wait_seconds=1)
    raise AssertionError("mancava l'errore di file non trovato")
except RuntimeError as e:
    assert "file non trovato" in str(e), e
os.environ.pop("AJ_BLENDER_EXE")

tools = asyncio.run(m.mcp.list_tools())
names = sorted(t.name for t in tools)
assert len(names) == 51, names
prompts = asyncio.run(m.mcp.list_prompts())
assert sorted(p.name for p in prompts) == ["controlla_file", "costruisci_scena"], prompts
print(f"ok: {len(names)} tool -> {', '.join(names)}")
