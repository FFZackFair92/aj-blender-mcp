"""Verifica dal vivo, con Blender aperto e l'add-on AJ MCP Bridge attivo:

    uv run --script check_live.py      (oppure: python check_live.py)

Crea alcuni oggetti con il prefisso _aj_prova_, li controlla e alla fine li
elimina insieme al materiale di prova: il resto della scena non viene toccato.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_mcp as m  # noqa: E402

P = "_aj_prova_"
KINDS = ("cube", "plane", "sphere", "ico_sphere", "cylinder", "cone", "circle", "monkey",
         "empty", "camera", "text", "point_light", "sun", "spot_light", "area_light")

s = m.blender_status()
print(f"collegato: Blender {s['blender']}, scena {s['scene']}, {s['objects']} oggetti, ponte {s['bridge']}")
before = s["objects"]
made = []
try:
    for i, kind in enumerate(KINDS):
        made.append(m.create_object(kind, name=P + kind, location=[i * 3, 0, 0])["name"])
    print(f"creati {len(made)} oggetti: ok")

    cube = P + "cube"
    t = m.transform_object(cube, location=[0, 0, 2], rotation=[0, 0, 45], scale=[1, 1, 2])
    assert t["rotation"][2] == 45.0 and t["scale"] == [1.0, 1.0, 2.0], t
    info = m.object_info(cube)
    assert info["mesh"]["faces"] == 6, info
    print("trasforma + leggi: ok")

    m.set_material(cube, name=P + "mat", color=[1, 0.2, 0.1], metallic=0.1, roughness=0.6,
                   emission_color=[1, 0.5, 0], emission_strength=1.5)
    assert m.object_info(cube)["materials"] == [P + "mat"]
    m.add_modifier(cube, "SUBSURF", settings={"levels": 2})
    m.add_modifier(cube, "BEVEL", settings={"width": 0.05})
    assert len(m.object_info(cube)["modifiers"]) == 2
    print("materiale + modificatori: ok")

    out = m.run_python("result = len(bpy.context.scene.objects)")
    assert out["result"] == before + len(made), out
    print("python: ok")

    img = m.viewport_screenshot(480)
    assert img.data[:8] == b"\x89PNG\r\n\x1a\n", "lo screenshot non e' un PNG"
    print(f"vista 3D: ok ({len(img.data)} byte)")

    tmp = Path(tempfile.mkdtemp())
    for ext in ("glb", "obj", "stl", "fbx"):
        f = tmp / f"prova.{ext}"
        e = m.export_file(str(f))
        assert e["bytes"] > 0, e
        got = m.import_file(str(f))["imported"]
        assert got, f"l'import {ext} non ha aggiunto oggetti"
        m.delete_objects(got)
        print(f"esporta + importa {ext}: ok")
finally:
    m.delete_objects(made)
    m.run_python(f"[bpy.data.materials.remove(x) for x in list(bpy.data.materials) if x.name.startswith('{P}')]")
assert m.blender_status()["objects"] == before, "sono rimasti oggetti di prova nella scena"
print("tutto ok, oggetti di prova eliminati")
