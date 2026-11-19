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

    def __iter__(self):  # come Blender: si scorrono gli elementi, non i nomi
        return iter(list(self.values()))

    def new(self, name, *args, **kwargs):
        base, n = name, 1
        if name in self and name[-4:-3] == "." and name[-3:].isdigit():
            base = name[:-4]  # come Blender: "Cubo.001" -> "Cubo.002"
        while name in self:
            name, n = f"{base}.{n:03d}", n + 1
        item = self.factory(name, *args, **kwargs)
        self[name] = item
        return item

    def remove(self, item, do_unlink=False):
        self.pop(item.name, None)
        if isinstance(item, Object):
            for c in list(item.users_collection):
                c.objects.unlink(item)

    def load(self, path, check_existing=False):
        return self.new(path.replace("\\", "/").split("/")[-1])


class Datablock:
    kind = "EMPTY"

    def __init__(self, name, type=None):
        self.name, self.type = name, type
        self.materials = []
        self.users = 1
        self.keys = []

    def keyframe_insert(self, data_path, frame=None, index=-1):
        self.keys.append((data_path, frame, index))

    def copy(self):
        return type(self)(self.name + ".001", *([self.type] if isinstance(self, Light) else []))


class Mesh(Datablock):
    kind = "MESH"

    def __init__(self, name):
        super().__init__(name)
        self.vertices, self.edges, self.polygons = [], [], []
        self.uv_layers = []

    def update(self):
        pass

    def copy(self):
        m = Mesh(self.name + ".001")
        m.vertices, m.edges, m.polygons = list(self.vertices), list(self.edges), list(self.polygons)
        return m


class Light(Datablock):
    kind = "LIGHT"

    def __init__(self, name, type="POINT"):
        super().__init__(name, type)
        self.energy, self.color, self.shadow_soft_size, self.size, self.spot_size = 1000.0, (1.0, 1.0, 1.0), 0.25, 1.0, 0.78


class Camera(Datablock):
    kind = "CAMERA"

    def __init__(self, name):
        super().__init__(name)
        self.lens = 50.0
        self.dof = _t.SimpleNamespace(use_dof=False, focus_object=None, aperture_fstop=2.8)


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
        self.operation, self.object, self.solver, self.node_group = "DIFFERENCE", None, "FAST", None
        self._items = {}

    def __setitem__(self, k, v):
        self._items[k] = v

    def __getitem__(self, k):
        if k not in self._items:
            g = self.node_group
            for it in (g.interface.items_tree if g else []):
                if it.identifier == k:
                    return it.default_value
            raise KeyError(k)
        return self._items[k]


class Modifiers(list):
    def get(self, name):
        return next((m for m in self if m.name == name), None)

    def new(self, name, kind):
        if kind not in ("SUBSURF", "BEVEL", "ARRAY", "MIRROR", "SOLIDIFY", "BOOLEAN", "NODES"):
            raise TypeError(f"enum {kind!r} not found")
        mod = Modifier(name, kind)
        self.append(mod)
        return mod


class Constraint:
    def __init__(self, kind):
        self.type, self.name, self.influence = kind, kind.replace("_", " ").title(), 1.0
        if kind != "LIMIT_ROTATION":
            self.target = None
        if kind == "TRACK_TO":
            self.track_axis = "TRACK_NEGATIVE_Z"


class Constraints(list):
    def new(self, kind):
        if kind not in ("TRACK_TO", "COPY_LOCATION", "LIMIT_ROTATION"):
            raise TypeError(f"enum {kind!r} not found")
        c = Constraint(kind)
        self.append(c)
        return c


class Matrix:
    def __init__(self, obj):
        self.translation = tuple(obj.location)

    def copy(self):
        return self


class Object:
    def __init__(self, name, data):
        self.name, self.data = name, data
        self.type = data.kind if data is not None else "EMPTY"
        self.location, self.rotation_euler, self.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
        self.dimensions = (2.0, 2.0, 2.0)
        self.parent, self.children, self.hide_viewport, self.hide_render = None, [], False, False
        self.modifiers = Modifiers()
        self.constraints = Constraints()
        self.users_collection = []
        self.selected = False
        self.keys = []
        self.rigid_body = None
        self.animation_data = None

    @property
    def matrix_world(self):
        return Matrix(self)

    @matrix_world.setter
    def matrix_world(self, m):
        self.location = m.translation

    def copy(self):
        new = data.objects.new(self.name, self.data)
        new.location, new.rotation_euler, new.scale = self.location, self.rotation_euler, self.scale
        return new

    def select_set(self, value):
        self.selected = bool(value)

    def select_get(self):
        return self.selected

    def keyframe_insert(self, data_path, frame=None, index=-1):
        self.keys.append((data_path, frame, index))
        if getattr(self, "animation_data", None) is None:
            self.animation_data = _t.SimpleNamespace(action=_t.SimpleNamespace(fcurves=_FCurves()))
        fcs = self.animation_data.action.fcurves
        n = 3 if data_path in ("location", "rotation_euler", "scale") else 1
        for i in range(n):
            fc = next((f for f in fcs if f.data_path == data_path and f.array_index == i), None)
            if fc is None:
                fc = _t.SimpleNamespace(data_path=data_path, array_index=i, keyframe_points=[], modifiers=_FModifiers())
                fcs.append(fc)
            fc.keyframe_points.append(_t.SimpleNamespace(co=(frame, 0.0), interpolation="BEZIER", easing="AUTO"))

    @property
    def material_slots(self):
        if self.data is None:
            return []
        return [Slot(self.data, i) for i in range(len(self.data.materials))]


class _FCurves(list):
    pass


class _FModifiers(list):
    def new(self, kind):
        m = _t.SimpleNamespace(type=kind)
        self.append(m)
        return m


class Socket:
    default_value = None

    def __init__(self, node=None):
        self.node = node

    @property
    def links(self):
        trees = [w.node_tree for w in data.worlds] + [m.node_tree for m in data.materials]
        return [lk for t in trees for lk in t.links if self in (lk.from_socket, lk.to_socket)]


class Material:
    def __init__(self, name):
        self.name, self.use_nodes, self.diffuse_color = name, False, (0.8, 0.8, 0.8, 1.0)
        inputs = {n: Socket() for n in ("Base Color", "Metallic", "Roughness", "Emission Color", "Emission Strength",
                                         "Alpha", "Normal", "IOR", "Transmission Weight")}
        bsdf = _t.SimpleNamespace(type="BSDF_PRINCIPLED", inputs=inputs)
        self.node_tree = NodeTree()
        self.node_tree.nodes.append(bsdf)


class CollectionObjects(list):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def link(self, obj):
        if obj in self:
            raise RuntimeError(f"Object '{obj.name}' already in collection '{self.owner.name}'")
        self.append(obj)
        obj.users_collection.append(self.owner)
        if obj not in scene.objects:
            scene.objects.append(obj)

    def unlink(self, obj):
        self.remove(obj)
        obj.users_collection.remove(self.owner)
        if not obj.users_collection and obj in scene.objects:
            scene.objects.remove(obj)


class Collection:
    def __init__(self, name):
        self.name, self.objects, self.children = name, CollectionObjects(self), _Children()


class _Children(list):
    def link(self, c):
        self.append(c)


class Render:
    ENGINES = ("BLENDER_EEVEE_NEXT", "CYCLES", "BLENDER_WORKBENCH")

    def __init__(self):
        self._engine = "BLENDER_EEVEE_NEXT"
        self.resolution_x, self.resolution_y, self.resolution_percentage = 1920, 1080, 100
        self.filepath, self.fps, self.film_transparent, self.use_file_extension = "/tmp/", 24, False, True
        self.image_settings = _t.SimpleNamespace(file_format="PNG")

    @property
    def engine(self):
        return self._engine

    @engine.setter
    def engine(self, value):
        if value not in self.ENGINES:
            raise TypeError(f"enum {value!r} not found")
        self._engine = value


class Node:
    def __init__(self, kind, inputs=(), outputs=()):
        self.type = kind
        self.inputs = {n: Socket(self) for n in inputs}
        self.outputs = {n: Socket(self) for n in outputs}
        self.image = None


class NodeTree:
    KINDS = {"ShaderNodeBackground": ("BACKGROUND", ("Color", "Strength"), ("Background",)),
             "ShaderNodeOutputWorld": ("OUTPUT_WORLD", ("Surface",), ()),
             "ShaderNodeTexEnvironment": ("TEX_ENVIRONMENT", (), ("Color",)),
             "ShaderNodeTexImage": ("TEX_IMAGE", ("Vector",), ("Color", "Alpha")),
             "ShaderNodeNormalMap": ("NORMAL_MAP", ("Color", "Strength"), ("Normal",)),
             "ShaderNodeBump": ("BUMP", ("Height", "Strength"), ("Normal",))}

    def __init__(self):
        self.nodes = _NodeList(self)
        self.links = _Links()


class _NodeList(list):
    def __init__(self, tree):
        super().__init__()
        self.tree = tree

    def new(self, bl_idname):
        kind, ins, outs = NodeTree.KINDS[bl_idname]
        n = Node(kind, ins, outs)
        self.append(n)
        return n


class _Link:
    def __init__(self, a, b):
        self.from_socket, self.to_socket = a, b


class _Links(list):
    def new(self, a, b):
        for old in list(b.links):
            self.remove(old)
        link = _Link(a, b)
        self.append(link)
        return link

    def remove(self, link):
        super().remove(link)


class World:
    def __init__(self, name):
        self.name, self.use_nodes, self.node_tree = name, False, NodeTree()


scene_collection = Collection("Scene Collection")
scene = _t.SimpleNamespace(
    name="Scene", objects=[], collection=scene_collection, camera=None, world=None,
    frame_start=1, frame_end=250, frame_current=1, render=Render(),
    cycles=_t.SimpleNamespace(samples=4096), eevee=_t.SimpleNamespace(taa_render_samples=64),
)


def _frame_set(n):
    scene.frame_current = n


scene.frame_set = _frame_set


class Image:
    def __init__(self, name):
        self.name, self.filepath, self.source, self.packed_file = name, "", "FILE", None


class NodeGroup:
    def __init__(self, name, bl_idname="GeometryNodeTree"):
        self.name, self.bl_idname = name, bl_idname
        self.interface = _t.SimpleNamespace(items_tree=[
            _t.SimpleNamespace(item_type="SOCKET", in_out="INPUT", name="Geometry", identifier="Socket_0", socket_type="NodeSocketGeometry", default_value=None),
            _t.SimpleNamespace(item_type="SOCKET", in_out="INPUT", name="Count", identifier="Socket_1", socket_type="NodeSocketInt", default_value=10),
            _t.SimpleNamespace(item_type="SOCKET", in_out="OUTPUT", name="Geometry", identifier="Socket_2", socket_type="NodeSocketGeometry", default_value=None)])


data = _t.SimpleNamespace(
    node_groups=Store(NodeGroup),
    objects=Store(Object), meshes=Store(Mesh), lights=Store(Light), cameras=Store(Camera),
    curves=Store(Curve), materials=Store(Material), collections=Store(Collection),
    worlds=Store(World), images=Store(Image), filepath="",
)

_space = _t.SimpleNamespace(shading=_t.SimpleNamespace(type="SOLID"),
                            region_3d=_t.SimpleNamespace(view_perspective="PERSP"))
_view = _t.SimpleNamespace(type="VIEW_3D", regions=[_t.SimpleNamespace(type="WINDOW")],
                           spaces=_t.SimpleNamespace(active=_space))
_window = _t.SimpleNamespace(screen=_t.SimpleNamespace(areas=[_view]))
main_thread = threading.current_thread()
calls = []


def _main_only():
    assert threading.current_thread() is main_thread, "Blender toccato fuori dal thread principale"


_override = {}


@contextlib.contextmanager
def _temp_override(**kwargs):
    _main_only()
    old = dict(_override)
    _override.update(kwargs)
    try:
        yield
    finally:
        _override.clear()
        _override.update(old)


class _LayerObjects:
    active = None

    def __iter__(self):
        return iter(list(scene.objects))


context = _t.SimpleNamespace(
    scene=scene, window_manager=_t.SimpleNamespace(windows=[_window]), temp_override=_temp_override,
    view_layer=_t.SimpleNamespace(objects=_LayerObjects()))


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


def _render(write_still=False):
    _main_only()
    calls.append(("render", scene.render.resolution_percentage, scene.render.image_settings.file_format))
    with open(scene.render.filepath, "wb") as f:
        f.write(PNG_1X1)


def _modifier_apply(modifier):
    _main_only()
    o = context.view_layer.objects.active
    calls.append(("modifier_apply", modifier))
    for ob in scene.objects:  # il finto non ha il contesto: cerca chi ha quel modificatore
        mod = ob.modifiers.get(modifier)
        if mod is not None:
            ob.modifiers.remove(mod)
            return
    raise RuntimeError(f"modifier {modifier} not found ({o})")


def _record(name):
    def op(**kwargs):
        _main_only()
        calls.append((name, kwargs))
    return op


def _save_as(filepath, copy=False):
    _main_only()
    with open(filepath, "wb") as f:
        f.write(b"BLENDER-finto")
    calls.append(("save_as", filepath, copy))
    if not copy:
        data.filepath = filepath


def _ctx_obj():
    return _override.get("object") or context.view_layer.objects.active


def _uv_op(name):
    def op(**kwargs):
        _main_only()
        calls.append((name, kwargs))
        o = _ctx_obj()
        if o is not None and not o.data.uv_layers:
            o.data.uv_layers.append(_t.SimpleNamespace(name="UVMap"))
    return op


def _rb_add(type="ACTIVE"):
    _main_only()
    o = _ctx_obj()
    o.rigid_body = _t.SimpleNamespace(type=type, mass=1.0, friction=0.5, restitution=0.0, collision_shape="CONVEX_HULL")
    calls.append(("rigidbody_add", o.name))


def _rb_remove():
    _main_only()
    _ctx_obj().rigid_body = None


ops = _t.SimpleNamespace(
    render=_t.SimpleNamespace(opengl=_opengl, render=_render),
    object=_t.SimpleNamespace(modifier_apply=_modifier_apply, mode_set=_record("mode_set"),
                              shade_auto_smooth=_record("shade_auto_smooth")),
    mesh=_t.SimpleNamespace(select_all=_record("select_all")),
    uv=_t.SimpleNamespace(**{n: _uv_op(n) for n in ("smart_project", "unwrap", "cube_project", "sphere_project",
                                                     "cylinder_project", "lightmap_pack")}),
    rigidbody=_t.SimpleNamespace(object_add=_rb_add, object_remove=_rb_remove),
    ptcache=_t.SimpleNamespace(bake_all=_record("bake_all")),
    ed=_t.SimpleNamespace(undo=_record("undo"), redo=_record("redo"), undo_push=_record("undo_push")),
    view3d=_t.SimpleNamespace(view_all=_record("view_all"), view_selected=_record("view_selected")),
    import_scene=_t.SimpleNamespace(gltf=_import, fbx=_import),
    export_scene=_t.SimpleNamespace(gltf=_export, fbx=_export),
    wm=_t.SimpleNamespace(obj_import=_import, obj_export=_export, stl_import=_import, stl_export=_export,
                             usd_import=_import, usd_export=_export,
                             save_as_mainfile=_save_as, save_mainfile=lambda: None,
                             open_mainfile=_record("open_mainfile")),
)

_timers = []


class _Timers:
    @staticmethod
    def register(fn, first_interval=0, persistent=False):
        _timers.append(fn)


app = _t.SimpleNamespace(version_string="4.5.0 Finto", timers=_Timers)
path = _t.SimpleNamespace(abspath=lambda p: p.replace("//", "/tmp/", 1) if p.startswith("//") else p)
utils = _t.SimpleNamespace(register_class=lambda c: None, unregister_class=lambda c: None)


def _prop(identifier, kind="INT", items=()):
    return _t.SimpleNamespace(identifier=identifier, type=kind, name=identifier.title(), description=f"{identifier} finto",
                              is_readonly=False, enum_items=[_t.SimpleNamespace(identifier=i) for i in items])


class _Types(_t.SimpleNamespace):
    Panel = object
    SubsurfModifier = _t.SimpleNamespace(bl_rna=_t.SimpleNamespace(
        description="Subdivision surface finto",
        properties=[_prop("rna_type", "POINTER"), _prop("levels"), _prop("render_levels"),
                    _prop("subdivision_type", "ENUM", ("CATMULL_CLARK", "SIMPLE"))]))
    BevelModifier = _t.SimpleNamespace(bl_rna=_t.SimpleNamespace(description="", properties=[_prop("width", "FLOAT")]))


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
        mesh.vertices, mesh.edges = [0] * self.counts[0], [0] * self.counts[1]
        mesh.polygons = [_t.SimpleNamespace(use_smooth=False) for _ in range(self.counts[2])]

    def from_mesh(self, mesh):
        self.counts = (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))

    @property
    def verts(self):
        return [0] * self.counts[0]

    @property
    def edges(self):
        return [0] * self.counts[1]

    @property
    def faces(self):
        return [0] * self.counts[2]

    def free(self):
        pass


def _maker(counts):
    def make(bm, **kwargs):
        bm.counts = counts
    return make


def _edit(rule):
    def op(bm, **kwargs):
        _main_only()
        calls.append(("bmesh", kwargs.get("cuts"), sorted(kwargs)))
        bm.counts = rule(*bm.counts, kwargs)
    return op


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
        create_circle=_maker((32, 32, 1)), create_monkey=_maker((507, 1005, 500)),
        subdivide_edges=_edit(lambda v, e, f, k: (v + e * k.get("cuts", 1) + f * k.get("cuts", 1) ** 2, e * 2 * (k.get("cuts", 1) + 1), f * (k.get("cuts", 1) + 1) ** 2)),
        bevel=_edit(lambda v, e, f, k: (v * 3, e * 3, f + e)), triangulate=_edit(lambda v, e, f, k: (v, e + f, f * 2)),
        remove_doubles=_edit(lambda v, e, f, k: (v, e, f)), recalc_face_normals=_edit(lambda v, e, f, k: (v, e, f)),
        reverse_faces=_edit(lambda v, e, f, k: (v, e, f)), dissolve_limit=_edit(lambda v, e, f, k: (v, e, f)),
        inset_region=_edit(lambda v, e, f, k: (v * 2, e * 2, f * 2)), smooth_vert=_edit(lambda v, e, f, k: (v, e, f)))
    return m


def install():
    """Registra questo modulo come `bpy` (e un finto `bmesh`)."""
    me = sys.modules[__name__]
    me.types = _Types()
    sys.modules["bpy"] = me
    sys.modules["bmesh"] = _bmesh_module()
