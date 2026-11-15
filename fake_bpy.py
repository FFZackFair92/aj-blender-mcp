"""Finto Blender per i test offline (test_offline.py e la CI).

Imita quel poco di `bpy` e `bmesh` che l'add-on usa, cosi' il ponte vero
(aj_blender_bridge.py) gira senza Blender: rete, token, coda sul "thread
principale" e logica dei comandi. Non sostituisce una prova con Blender vero.
"""
import contextlib
import sys
import threading
import time
import types as _t

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


class Store(dict):
    """Come bpy.data.objects: dizionario per nome con new/remove."""

    def __init__(self, factory):
        super().__init__()
        self.factory = factory

    def new(self, name, *args, **kwargs):
        base, n = name, 1
        while name in self:
            name, n = f"{base}.{n:03d}", n + 1
        item = self.factory(name, *args, **kwargs)
        self[name] = item
        return item

    def remove(self, item, do_unlink=False):
        self.pop(item.name, None)
        if isinstance(item, Object) and item in scene.objects:
            scene.objects.remove(item)


class Datablock:
    kind = "EMPTY"

    def __init__(self, name, type=None):
        self.name, self.subtype = name, type
        self.materials = []


class Mesh(Datablock):
    kind = "MESH"

    def __init__(self, name):
        super().__init__(name)
        self.vertices, self.edges, self.polygons = [], [], []


class Light(Datablock):
    kind = "LIGHT"


class Camera(Datablock):
    kind = "CAMERA"


class Curve(Datablock):
    kind = "FONT"
    body = ""


class Slot:
    def __init__(self, data, index):
        self._data, self._index = data, index

    @property
    def material(self):
        return self._data.materials[self._index]

    @material.setter
    def material(self, value):
        self._data.materials[self._index] = value


class Modifier:
    def __init__(self, name, kind):
        self.name, self.type, self.levels, self.width = name, kind, 1, 0.1


class Modifiers(list):
    def new(self, name, kind):
        if kind not in ("SUBSURF", "BEVEL", "ARRAY", "MIRROR", "SOLIDIFY"):
            raise TypeError(f"enum {kind!r} not found")
        mod = Modifier(name, kind)
        self.append(mod)
        return mod


class Object:
    def __init__(self, name, data):
        self.name, self.data = name, data
        self.type = data.kind if data is not None else "EMPTY"
        self.location, self.rotation_euler, self.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
        self.dimensions = (2.0, 2.0, 2.0)
        self.parent, self.children, self.hide_viewport = None, [], False
        self.modifiers = Modifiers()
        self.users_collection = []

    @property
    def material_slots(self):
        if self.data is None:
            return []
        return [Slot(self.data, i) for i in range(len(self.data.materials))]


class Socket:
    default_value = None


class Material:
    def __init__(self, name):
        self.name, self.use_nodes, self.diffuse_color = name, False, (0.8, 0.8, 0.8, 1.0)
        inputs = {n: Socket() for n in ("Base Color", "Metallic", "Roughness", "Emission Color", "Emission Strength")}
        bsdf = _t.SimpleNamespace(type="BSDF_PRINCIPLED", inputs=inputs)
        self.node_tree = _t.SimpleNamespace(nodes=[bsdf])


class Links:
    def link(self, obj):
        scene.objects.append(obj)
        obj.users_collection = [_t.SimpleNamespace(name="Scene Collection")]


scene = _t.SimpleNamespace(
    name="Scene", objects=[], collection=_t.SimpleNamespace(objects=Links()), camera=None,
    frame_start=1, frame_end=250, frame_current=1,
    render=_t.SimpleNamespace(
        engine="BLENDER_EEVEE", resolution_x=1920, resolution_y=1080, resolution_percentage=100,
        filepath="/tmp/", image_settings=_t.SimpleNamespace(file_format="PNG")),
)

data = _t.SimpleNamespace(
    objects=Store(Object), meshes=Store(Mesh), lights=Store(Light), cameras=Store(Camera),
    curves=Store(Curve), materials=Store(Material), filepath="",
)

_view = _t.SimpleNamespace(type="VIEW_3D", regions=[_t.SimpleNamespace(type="WINDOW")])
_window = _t.SimpleNamespace(screen=_t.SimpleNamespace(areas=[_view]))
main_thread = threading.current_thread()
calls = []


def _main_only():
    assert threading.current_thread() is main_thread, "Blender toccato fuori dal thread principale"


@contextlib.contextmanager
def _temp_override(**kwargs):
    _main_only()
    yield


context = _t.SimpleNamespace(
    scene=scene, window_manager=_t.SimpleNamespace(windows=[_window]), temp_override=_temp_override)


def _opengl(write_still=False, view_context=True):
    _main_only()
    calls.append(("opengl", scene.render.resolution_percentage))
    with open(scene.render.filepath, "wb") as f:
        f.write(PNG_1X1)


def _import(filepath):
    _main_only()
    scene.collection.objects.link(data.objects.new("Importato", data.meshes.new("Importato")))


def _export(filepath, **kwargs):
    _main_only()
    calls.append(("export", kwargs))
    with open(filepath, "wb") as f:
        f.write(b"finto")


def _save_as(filepath):
    data.filepath = filepath


ops = _t.SimpleNamespace(
    render=_t.SimpleNamespace(opengl=_opengl),
    import_scene=_t.SimpleNamespace(gltf=_import, fbx=_import),
    export_scene=_t.SimpleNamespace(gltf=_export, fbx=_export),
    wm=_t.SimpleNamespace(obj_import=_import, obj_export=_export, stl_import=_import, stl_export=_export,
                             usd_import=_import, usd_export=_export,
                             save_as_mainfile=_save_as, save_mainfile=lambda: None),
)

_timers = []


class _Timers:
    @staticmethod
    def register(fn, persistent=False):
        _timers.append(fn)


app = _t.SimpleNamespace(version_string="4.5.0 Finto", timers=_Timers)
utils = _t.SimpleNamespace(register_class=lambda c: None, unregister_class=lambda c: None)


class _Types(_t.SimpleNamespace):
    Panel = object
    WM_OT_obj_import = WM_OT_obj_export = WM_OT_stl_import = WM_OT_stl_export = object


def run_main_loop(until, timeout=30):
    """Fa girare i timer sul thread principale finche' `until()` e' vero."""
    end = time.time() + timeout
    while not until():
        assert time.time() < end, "tempo scaduto nel finto ciclo principale"
        for fn in list(_timers):
            if fn() is None:
                _timers.remove(fn)
        time.sleep(0.01)


class _BMesh:
    def to_mesh(self, mesh):
        mesh.vertices, mesh.edges, mesh.polygons = [0] * self.counts[0], [0] * self.counts[1], [0] * self.counts[2]

    def free(self):
        pass


def _maker(counts):
    def make(bm, **kwargs):
        bm.counts = counts
    return make


def _bmesh_module():
    m = _t.ModuleType("bmesh")

    def new():
        bm = _BMesh()
        bm.counts = (0, 0, 0)
        return bm

    m.new = new
    m.ops = _t.SimpleNamespace(
        create_cube=_maker((8, 12, 6)), create_grid=_maker((4, 4, 1)), create_uvsphere=_maker((482, 992, 512)),
        create_icosphere=_maker((42, 120, 80)), create_cone=_maker((64, 96, 34)),
        create_circle=_maker((32, 32, 1)), create_monkey=_maker((507, 1005, 500)))
    return m


def install():
    """Registra questo modulo come `bpy` (e un finto `bmesh`)."""
    me = sys.modules[__name__]
    me.types = _Types()
    sys.modules["bpy"] = me
    sys.modules["bmesh"] = _bmesh_module()
