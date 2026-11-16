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

    # --- 1.1
    assert {o["name"] for o in m.find_objects(P + "*", type="LIGHT")["objects"]} == {
        P + k for k in ("point_light", "sun", "spot_light", "area_light")}
    copies = m.duplicate_object(cube, count=2, offset=[0, 3, 0])["created"]
    made += copies
    m.apply_modifier(copies[0], "Bevel")
    assert [x["name"] for x in m.object_info(copies[0])["modifiers"]] == ["Subsurf"]
    m.set_parent(copies[1], cube)
    assert m.object_info(copies[1])["parent"] == cube
    m.move_to_collection(copies, P + "coll")
    assert m.find_objects(collection=P + "coll")["count"] == 2
    assert m.set_property(cube, "modifiers.Subsurf.levels", 1)["value"] == 1
    assert m.get_property(P + "point_light", "data.type")["value"] == "POINT"
    assert any(p["name"] == "levels" for p in m.api_info("SubsurfModifier")["properties"])
    m.set_keyframe(cube, "location", frame=1)
    m.set_keyframe(cube, "location", frame=24, value=[0, 0, 4])
    m.add_constraint(P + "camera", "TRACK_TO", target=cube)
    m.look_at(P + "spot_light", target=cube)
    m.set_light(P + "spot_light", energy=800, color=[1, 0.8, 0.6], spot_size=40)
    m.set_camera(P + "camera", lens=35, active=False)
    r = m.batch([{"cmd": "select_objects", "args": {"names": copies}},
                 {"cmd": "set_visibility", "args": {"names": copies, "render": False}}])
    assert r["steps"] == 2, r
    print(f"ricerca, duplica, collezioni, proprieta', animazione, vincoli, luci, batch: ok "
          f"(file mancanti: {m.missing_files()['count']})")
    prev = m.render_preview(256)
    assert prev.data[:8] == b"\x89PNG\r\n\x1a\n", "il render di prova non e' un PNG"
    print(f"render_preview: ok ({len(prev.data)} byte)")

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
    m.run_python(f"[bpy.data.collections.remove(x) for x in list(bpy.data.collections) if x.name.startswith('{P}')]")
assert m.blender_status()["objects"] == before, "sono rimasti oggetti di prova nella scena"
print("tutto ok, oggetti di prova eliminati")
