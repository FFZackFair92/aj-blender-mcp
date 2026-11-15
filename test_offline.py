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

# --- elimina
d = do(m.delete_objects, ["Cubo", "Manca"])
assert d == {"deleted": ["Cubo"], "missing": ["Manca"]}, d
assert do(m.blender_status)["objects"] == 8

# --- Blender chiuso
bridge.unregister()
try:
    m.blender_status()
    raise AssertionError("mancava l'errore di collegamento")
except RuntimeError as e:
    assert "non risponde" in str(e), e

tools = asyncio.run(m.mcp.list_tools())
names = sorted(t.name for t in tools)
assert len(names) == 13, names
print(f"ok: {len(names)} tool -> {', '.join(names)}")
