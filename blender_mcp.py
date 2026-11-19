# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<3"]
# ///
"""AJ Blender MCP: server MCP che pilota Blender.

Parla via HTTP locale con l'add-on aj_blender_bridge.py (porta 9991).
Lavora solo sulla scena locale: nessun servizio esterno, nessuna chiave API.

Config (variabili d'ambiente, tutte facoltative):
  AJ_BLENDER_URL    indirizzo del ponte  (default http://127.0.0.1:9991)
  AJ_BLENDER_TOKEN  token                (default: letto da ~/.aj-mcp/blender.token)
  AJ_BLENDER_EXE    eseguibile di Blender per open_blender (default: cercato da solo)
"""
import base64
import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # mcp SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP, Image

URL = os.environ.get("AJ_BLENDER_URL", "http://127.0.0.1:9991").rstrip("/")
TOKEN_FILE = Path.home() / ".aj-mcp" / "blender.token"

mcp = FastMCP("blender")


def _token() -> str:
    env = os.environ.get("AJ_BLENDER_TOKEN")
    if env:
        return env.strip()
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError(
            f"Token non trovato in {TOKEN_FILE}: installa e attiva l'add-on AJ MCP Bridge in Blender (vedi README)."
        ) from None


def call(cmd: str, **args: Any) -> Any:
    """Manda un comando al ponte e ritorna il risultato, o solleva RuntimeError."""
    body = json.dumps({"cmd": cmd, "args": {k: v for k, v in args.items() if v is not None}}).encode()
    req = urllib.request.Request(
        URL + "/", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-AJ-Token": _token()},
    )
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = e.reason
        raise RuntimeError(f"Il ponte ha rifiutato la richiesta ({e.code}): {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"Blender non risponde su {URL}: apri Blender con l'add-on AJ MCP Bridge attivo. ({e})"
        ) from None
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "errore sconosciuto"))
    return reply["result"]


# -------------------------------------------------------------------- avvio
def _version_key(path: str) -> list[int]:
    digits = "".join(c if c.isdigit() else " " for c in path).split()
    return [int(d) for d in digits]


def find_blender() -> str | None:
    """Percorso dell'eseguibile di Blender: AJ_BLENDER_EXE, poi il PATH, poi le
    cartelle di installazione abituali (la versione piu' recente)."""
    env = os.environ.get("AJ_BLENDER_EXE")
    if env:
        return env if os.path.isfile(env) else None
    found = shutil.which("blender")
    if found:
        return found
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")]
        hits = [p for r in roots if r for p in glob.glob(os.path.join(r, "Blender Foundation", "Blender*", "blender.exe"))]
    elif sys.platform == "darwin":
        hits = glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        hits = glob.glob("/opt/blender*/blender") + glob.glob(os.path.expanduser("~/blender*/blender"))
    return max(hits, key=_version_key) if hits else None


def _launch(command: list[str]) -> None:
    """Avvia un programma staccato da questo processo, cosi' resta aperto."""
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, creationflags=flags, close_fds=True, **quiet)
    else:
        subprocess.Popen(command, start_new_session=True, close_fds=True, **quiet)


@mcp.tool()
def open_blender(file: str = "", wait_seconds: int = 60) -> dict:
    """Apre Blender su questo computer e aspetta che il ponte risponda.

    Se Blender e' gia' aperto con il ponte attivo non ne apre un altro.
    file: un .blend da aprire (facoltativo). wait_seconds: quanto aspettare il ponte.
    """
    try:
        return {"opened": False, "note": "Blender era gia' aperto", **call("status")}
    except RuntimeError as e:
        if "non risponde" not in str(e) and "Token non trovato" not in str(e):
            raise
    exe = find_blender()
    if exe is None:
        raise RuntimeError("Blender non trovato: installalo o indica il percorso nella variabile AJ_BLENDER_EXE.")
    command = [exe]
    if file:
        path = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(path):
            raise RuntimeError(f"file non trovato: {path}")
        command.append(path)
    _launch(command)
    deadline = time.time() + max(1, min(int(wait_seconds), 300))
    while time.time() < deadline:
        time.sleep(1)
        try:
            return {"opened": True, "executable": exe, **call("status")}
        except RuntimeError:
            continue
    raise RuntimeError(
        f"Blender e' stato avviato ({exe}) ma il ponte non risponde: controlla che l'add-on AJ MCP Bridge sia attivo."
    )


# ------------------------------------------------------------------- lettura
@mcp.tool()
def blender_status() -> dict:
    """Verifica il collegamento: versione di Blender, file aperto, scena, numero di oggetti."""
    return call("status")


@mcp.tool()
def scene_info(limit: int = 100) -> dict:
    """La scena attiva: motore di render, risoluzione, fotogrammi, camera e gli
    oggetti (nome, tipo, posizione, visibilita', genitore, collezioni)."""
    return call("scene_info", limit=limit)


@mcp.tool()
def object_info(name: str) -> dict:
    """Dettagli di un oggetto: posizione, rotazione in gradi, scala, dimensioni,
    materiali, modificatori e, per le mesh, vertici, spigoli e facce."""
    return call("object_info", name=name)


@mcp.tool()
def viewport_screenshot(max_size: int = 800) -> Image:
    """Immagine della vista 3D, per controllare a occhio il risultato.
    max_size e' il lato lungo in pixel (64-2000)."""
    r = call("viewport_screenshot", max_size=max_size)
    return Image(data=base64.b64decode(r["png_base64"]), format="png")


# ----------------------------------------------------------------- scrittura
@mcp.tool()
def create_object(kind: str, name: str = "", location: list[float] | None = None,
                  rotation: list[float] | None = None, scale: list[float] | None = None,
                  size: float = 2.0, text: str = "") -> dict:
    """Crea un oggetto nella scena.

    kind: cube, plane, sphere, ico_sphere, cylinder, cone, circle, monkey,
    empty, camera, text, point_light, sun, spot_light, area_light.
    location e scale sono [x, y, z]; rotation e' in gradi [x, y, z];
    size e' il lato o il diametro delle mesh; text e' il contenuto per kind="text".
    """
    return call("create_object", kind=kind, name=name or None, location=location,
                rotation=rotation, scale=scale, size=size, text=text or None)


@mcp.tool()
def delete_objects(names: list[str]) -> dict:
    """Elimina gli oggetti indicati. Ritorna quali sono stati eliminati e quali mancavano."""
    return call("delete_objects", names=names)


@mcp.tool()
def transform_object(name: str, location: list[float] | None = None,
                     rotation: list[float] | None = None, scale: list[float] | None = None) -> dict:
    """Sposta, ruota (gradi) o scala un oggetto. Valori assoluti [x, y, z];
    quello che non indichi resta com'e'."""
    return call("transform_object", name=name, location=location, rotation=rotation, scale=scale)


@mcp.tool()
def set_material(object: str, name: str = "", color: list[float] | None = None,
                 metallic: float | None = None, roughness: float | None = None,
                 emission_color: list[float] | None = None,
                 emission_strength: float | None = None) -> dict:
    """Assegna a un oggetto un materiale Principled BSDF, creandolo se manca.
    I colori sono [r, g, b] o [r, g, b, a] tra 0 e 1."""
    return call("set_material", object=object, name=name or None, color=color, metallic=metallic,
                roughness=roughness, emission_color=emission_color, emission_strength=emission_strength)


@mcp.tool()
def add_modifier(object: str, type: str, name: str = "", settings: dict[str, Any] | None = None) -> dict:
    """Aggiunge un modificatore a un oggetto. type e' il nome di Blender
    (SUBSURF, BEVEL, ARRAY, MIRROR, SOLIDIFY...); settings imposta le sue
    proprieta', es. {"levels": 2} per SUBSURF."""
    return call("add_modifier", object=object, type=type, name=name or None, settings=settings)


@mcp.tool()
def run_python(code: str) -> dict:
    """Esegue codice Python dentro Blender, per tutto quello che gli altri tool
    non coprono. Disponibili: bpy, bmesh. Assegna a `result` il valore da
    restituire; cio' che stampi con print torna in `stdout`."""
    return call("run_python", code=code)


# --------------------------------------------------------------------- file
@mcp.tool()
def import_file(path: str) -> dict:
    """Importa nella scena un file dal disco: glb, gltf, fbx, obj, stl, usd.
    Ritorna i nomi degli oggetti aggiunti."""
    return call("import_file", path=path)


@mcp.tool()
def export_file(path: str, selected_only: bool = False) -> dict:
    """Esporta la scena (o solo la selezione) in glb, gltf, fbx, obj, stl o usd.
    Il formato viene dall'estensione; la cartella deve esistere."""
    return call("export_file", path=path, selected_only=selected_only)


@mcp.tool()
def save_file(path: str = "") -> dict:
    """Salva il file .blend. Senza path salva sul file aperto; con path salva
    con nome (deve finire in .blend)."""
    return call("save_file", path=path or None)


# ------------------------------------------------------- oggetti e scena (1.1)
@mcp.tool()
def find_objects(name: str = "*", type: str = "", collection: str = "", limit: int = 200) -> dict:
    """Cerca oggetti nella scena. name accetta * e ? ("Cubo*"); type e' il tipo
    di Blender (MESH, LIGHT, CAMERA, EMPTY, CURVE, FONT...); collection filtra
    per collezione."""
    return call("find_objects", name=name, type=type or None, collection=collection or None, limit=limit)


@mcp.tool()
def duplicate_object(name: str, count: int = 1, offset: list[float] | None = None,
                     linked: bool = False) -> dict:
    """Duplica un oggetto `count` volte, spostando ogni copia di `offset`
    [x, y, z] rispetto alla precedente (una fila, una scala...). linked=true
    condivide la mesh con l'originale."""
    return call("duplicate_object", name=name, count=count, offset=offset, linked=linked)


@mcp.tool()
def set_parent(name: str, parent: str = "", keep_transform: bool = True) -> dict:
    """Rende `parent` genitore di `name` (senza parent lo stacca). Con
    keep_transform l'oggetto resta dov'e' nel mondo."""
    return call("set_parent", name=name, parent=parent or None, keep_transform=keep_transform)


@mcp.tool()
def select_objects(names: list[str], active: str = "", extend: bool = False) -> dict:
    """Seleziona gli oggetti indicati (e ne rende attivo uno). Utile prima di
    export_file con selected_only. extend=true aggiunge alla selezione."""
    return call("select_objects", names=names, active=active or None, extend=extend)


@mcp.tool()
def set_visibility(names: list[str], viewport: bool | None = None, render: bool | None = None) -> dict:
    """Mostra o nasconde oggetti nella vista 3D e/o nel render."""
    return call("set_visibility", names=names, viewport=viewport, render=render)


@mcp.tool()
def create_collection(name: str, parent: str = "") -> dict:
    """Crea una collezione (dentro `parent`, o nella scena). Se esiste gia' la lascia com'e'."""
    return call("create_collection", name=name, parent=parent or None)


@mcp.tool()
def move_to_collection(names: list[str], collection: str, keep_others: bool = False,
                       create: bool = True) -> dict:
    """Sposta oggetti in una collezione (creandola se manca). keep_others=true li
    lascia anche nelle collezioni dove sono gia'."""
    return call("move_to_collection", names=names, collection=collection,
                keep_others=keep_others, create=create)


# ------------------------------------------------- modificatori e vincoli (1.1)
@mcp.tool()
def apply_modifier(object: str, modifier: str) -> dict:
    """Applica un modificatore a una mesh (diventa geometria vera)."""
    return call("apply_modifier", object=object, modifier=modifier)


@mcp.tool()
def remove_modifier(object: str, modifier: str) -> dict:
    """Toglie un modificatore senza applicarlo."""
    return call("remove_modifier", object=object, modifier=modifier)


@mcp.tool()
def add_constraint(object: str, type: str, target: str = "", settings: dict[str, Any] | None = None) -> dict:
    """Aggiunge un vincolo: TRACK_TO, COPY_LOCATION, COPY_ROTATION, CHILD_OF,
    FOLLOW_PATH, LIMIT_DISTANCE, DAMPED_TRACK... con `target` (un oggetto) e
    altre impostazioni in settings."""
    return call("add_constraint", object=object, type=type, target=target or None, settings=settings)


# ------------------------------------------------------ proprieta' (1.1)
@mcp.tool()
def get_property(target: str, path: str) -> dict:
    """Legge una proprieta' qualsiasi per percorso. target: nome di un oggetto,
    "scene", "world" o "material:Nome" / "collection:Nome" / "light:Nome" /
    "camera:Nome" / "mesh:Nome". path: "data.energy",
    "modifiers.Bevel.width", "render.fps" (con target "scene")."""
    return call("get_property", target=target, path=path)


@mcp.tool()
def set_property(target: str, path: str, value: Any) -> dict:
    """Scrive una proprieta' qualsiasi per percorso (vedi get_property), es.
    target="Cubo", path="modifiers.Bevel.segments", value=4. Ritorna il valore riletto.
    Per sapere quali proprieta' esistono: api_info."""
    return call("set_property", target=target, path=path, value=value)


@mcp.tool()
def api_info(name: str, filter: str = "") -> dict:
    """Documentazione dell'API di Blender: proprieta' di un tipo (es.
    "SubsurfModifier", "Light", "Object") con tipo, descrizione e valori
    ammessi. Con * e ? elenca i tipi: "*Modifier", "*Constraint"."""
    return call("api_info", name=name, filter=filter or None)


# ------------------------------------------------------------ animazione (1.1)
@mcp.tool()
def set_keyframe(object: str, property: str = "location", frame: float | None = None,
                 value: Any = None, index: int = -1) -> dict:
    """Inserisce un fotogramma chiave. property: location, rotation (gradi),
    scale o un percorso come "data.energy". Con value imposta prima il valore;
    senza frame usa il fotogramma corrente; index sceglie un solo asse (0, 1, 2)."""
    return call("set_keyframe", object=object, property=property, frame=frame, value=value, index=index)


@mcp.tool()
def set_frame(current: int | None = None, start: int | None = None, end: int | None = None,
              fps: int | None = None) -> dict:
    """Fotogramma corrente, intervallo dell'animazione e fps della scena."""
    return call("set_frame", current=current, start=start, end=end, fps=fps)


# ------------------------------------------------------- camera e luci (1.1)
@mcp.tool()
def look_at(name: str, target: str = "", point: list[float] | None = None) -> dict:
    """Orienta una camera o una luce verso un oggetto (`target`) o un punto
    [x, y, z]."""
    return call("look_at", name=name, target=target or None, point=point)


@mcp.tool()
def set_camera(name: str, lens: float | None = None, active: bool = True,
               dof_object: str = "", fstop: float | None = None) -> dict:
    """Imposta una camera: focale in mm, camera attiva della scena,
    profondita' di campo (oggetto a fuoco e apertura f)."""
    return call("set_camera", name=name, lens=lens, active=active, dof_object=dof_object or None, fstop=fstop)


@mcp.tool()
def set_light(name: str, energy: float | None = None, color: list[float] | None = None,
              size: float | None = None, spot_size: float | None = None) -> dict:
    """Imposta una luce: potenza (W, per il sole W/m2), colore [r, g, b],
    dimensione (morbidezza delle ombre) e apertura dello spot in gradi."""
    return call("set_light", name=name, energy=energy, color=color, size=size, spot_size=spot_size)


@mcp.tool()
def set_world(color: list[float] | None = None, strength: float | None = None, hdri: str = "") -> dict:
    """Sfondo e luce ambiente: un colore [r, g, b] oppure un'immagine HDRI dal
    disco (.hdr, .exr), con la sua intensita'."""
    return call("set_world", color=color, strength=strength, hdri=hdri or None)


# --------------------------------------------------------------- render (1.1)
@mcp.tool()
def set_render(engine: str = "", resolution: list[int] | None = None, percentage: int | None = None,
               samples: int | None = None, fps: int | None = None,
               transparent: bool | None = None) -> dict:
    """Impostazioni di render: motore (eevee, cycles, workbench), risoluzione
    [larghezza, altezza], percentuale, campioni, fps, sfondo trasparente."""
    return call("set_render", engine=engine or None, resolution=resolution, percentage=percentage,
                samples=samples, fps=fps, transparent=transparent)


@mcp.tool()
def render_image(path: str) -> dict:
    """Renderizza il fotogramma corrente con le impostazioni della scena e lo
    salva (png, jpg, exr, tif, bmp, webp, dall'estensione). Puo' richiedere
    tempo: per un controllo veloce usa render_preview."""
    return call("render_image", path=path)


@mcp.tool()
def render_preview(max_size: int = 640) -> Image:
    """Render vero (luci, materiali) in piccolo, per vedere il risultato finale.
    Piu' lento di viewport_screenshot."""
    r = call("render_preview", max_size=max_size)
    return Image(data=base64.b64decode(r["png_base64"]), format="png")


@mcp.tool()
def set_viewport(shading: str = "", frame: str = "", camera_view: bool = False) -> dict:
    """Vista 3D: shading (wireframe, solid, material, rendered), inquadra un
    oggetto (frame="Nome") o tutto (frame="all"), guarda dalla camera."""
    return call("set_viewport", shading=shading or None, frame=frame or None, camera_view=camera_view or None)


# ------------------------------------------------------- controlli (1.1)
@mcp.tool()
def missing_files() -> dict:
    """File esterni (immagini, librerie, suoni, video, font) che il .blend usa
    ma che non si trovano piu' sul disco."""
    return call("missing_files")


@mcp.tool()
def undo(steps: int = 1, redo: bool = False) -> dict:
    """Annulla (o ripete) gli ultimi passi. Ogni comando di questo server che
    cambia la scena e' un passo, batch compreso."""
    return call("undo", steps=steps, redo=redo or None)


@mcp.tool()
def batch(commands: list[dict[str, Any]]) -> dict:
    """Esegue piu' comandi in una sola chiamata, es.
    [{"cmd": "create_object", "args": {"kind": "cube", "name": "A"}},
     {"cmd": "set_material", "args": {"object": "A", "color": [1, 0, 0]}}].
    I nomi sono quelli dei tool (blender_status -> status). Si ferma al primo
    errore; un solo undo annulla tutto."""
    return call("batch", commands=commands)


# ------------------------------------------------- checkpoint, mesh, fisica (1.2)
@mcp.tool()
def checkpoint(action: str = "list", name: str = "", note: str = "") -> dict:
    """Punti di ripristino dell'INTERA scena, oltre l'undo: save (copia del file in ~/.aj-mcp/blender-checkpoints,
    la scena aperta non cambia), list, restore (riapre la copia: tutto torna com'era), delete.
    Salva un checkpoint prima di lavori grossi (boolean, applicare modificatori, fisica)."""
    return call("checkpoint", action=action, name=name or None, note=note or None)


@mcp.tool()
def mesh_ops(object: str, op: str, settings: dict[str, Any] | None = None) -> dict:
    """Operazioni sulla mesh intera, senza modalita' modifica: subdivide {cuts, smooth}, bevel {width, segments,
    profile}, triangulate, merge_by_distance {distance}, recalc_normals, flip_normals, dissolve_limited {angle},
    inset {thickness, depth}, smooth_vertices {factor, repeat}, shade_smooth {auto_angle}, shade_flat.
    Diversamente da un modificatore cambia la mesh davvero (Ctrl+Z la riporta indietro)."""
    return call("mesh_ops", object=object, op=op, settings=settings)


@mcp.tool()
def uv_unwrap(object: str, method: str = "smart", angle: float = 66, margin: float = 0.02) -> dict:
    """Crea le UV di una mesh in un colpo: smart (Smart UV Project, va bene quasi sempre), unwrap (sulle
    cuciture), cube, sphere, cylinder, lightmap. Serve prima di applicare texture con material_nodes."""
    return call("uv_unwrap", object=object, method=method, angle=angle, margin=margin)


@mcp.tool()
def boolean(object: str, cutter: str, operation: str = "difference", apply: bool = False,
            hide_cutter: bool = True) -> dict:
    """Boolean tra due mesh: difference (fa un buco con la forma di cutter), union, intersect. Con apply=true la
    applica subito; hide_cutter nasconde l'oggetto che taglia. Consiglio: checkpoint prima di applicare."""
    return call("boolean", object=object, cutter=cutter, operation=operation, apply=apply or None,
                hide_cutter=hide_cutter)


@mcp.tool()
def animate(object: str, keys: list[dict[str, Any]] | None = None, property: str = "location",
            interpolation: str = "bezier", cycles: bool = False, clear: bool = False, fit_range: bool = False) -> dict:
    """Animazione in una chiamata: keys=[{"frame": 1, "value": [0, 0, 0]}, {"frame": 48, "value": [0, 0, 3]}].
    property: location, rotation (gradi), scale o un percorso. interpolation: linear, bezier, ease, constant,
    bounce, elastic, back, sine. cycles=true ripete all'infinito, clear=true cancella prima le chiavi di quella
    proprieta', fit_range=true adatta inizio/fine della scena."""
    return call("animate", object=object, keys=keys, property=property, interpolation=interpolation,
                cycles=cycles or None, clear=clear or None, fit_range=fit_range or None)


@mcp.tool()
def geometry_nodes(action: str = "list", object: str = "", group: str = "", modifier: str = "",
                   inputs: dict[str, Any] | None = None) -> dict:
    """Geometry Nodes: list (senza object: i gruppi nel file con i loro ingressi; con object: il modificatore e i
    valori), add (aggiunge a object il modificatore con un gruppo esistente), set (valori degli ingressi per nome,
    es. {"Count": 20, "Radius": 0.5})."""
    return call("geometry_nodes", action=action, object=object or None, group=group or None,
                modifier=modifier or None, inputs=inputs)


@mcp.tool()
def material_nodes(object: str = "", material: str = "", inputs: dict[str, Any] | None = None,
                   texture: str = "", slot: str = "base_color") -> dict:
    """Materiale avanzato: qualsiasi ingresso del Principled BSDF per nome ({"Alpha": 0.5, "Transmission Weight":
    1, "IOR": 1.45, "Coat Weight": 0.3}) e una texture da file collegata al posto giusto: slot base_color,
    roughness, metallic, normal (con Normal Map), bump, emission, alpha. Le texture vogliono le UV (uv_unwrap)."""
    return call("material_nodes", object=object or None, material=material or None, inputs=inputs,
                texture=texture or None, slot=slot)


@mcp.tool()
def rigid_body(action: str = "add", objects: list[str] | None = None, type: str = "active", mass: float | None = None,
               shape: str = "", friction: float | None = None, bounce: float | None = None) -> dict:
    """Fisica rigida: add (type active = cade e urta, passive = pavimento o ostacolo; mass, shape: convex_hull,
    box, sphere, mesh..., friction, bounce), remove, bake (calcola la simulazione nel range della scena)."""
    return call("rigid_body", action=action, objects=objects, type=type, mass=mass, shape=shape or None,
                friction=friction, bounce=bounce)


@mcp.tool()
def scene_stats(top: int = 10) -> dict:
    """Quanto pesa la scena: vertici e facce totali, gli oggetti piu' pesanti, i modificatori che moltiplicano la
    geometria (subsurf, array...), materiali e immagini. Per capire perche' il viewport o il render sono lenti."""
    return call("scene_stats", top=top)


@mcp.tool()
def view_object(name: str, shading: str = "", max_size: int = 800) -> Image:
    """Inquadra un oggetto nella vista 3D (eventualmente con shading: solid, material, rendered) e ne restituisce
    l'immagine: per controllare un pezzo senza cambiare la camera."""
    call("set_viewport", shading=shading or None, frame=name)
    r = call("viewport_screenshot", max_size=max_size)
    return Image(data=base64.b64decode(r["png_base64"]), format="png")


# ------------------------------------------------------- render in background (1.2)
_render_jobs: dict[str, dict[str, Any]] = {}


def _watch_render(job: dict[str, Any]) -> None:
    proc = job["proc"]
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line:
            continue
        job["log"].append(line)
        del job["log"][:-40]
        if line.startswith("Fra:"):
            with contextlib.suppress(ValueError, IndexError):
                job["frame"] = int(line.split()[0][4:])
        if line.startswith("Saved:"):
            job["saved"].append(line.split(":", 1)[1].strip().strip("'\""))
    proc.wait()
    job["exit_code"] = proc.returncode
    job["ended"] = time.time()


def _job_view(jid: str, job: dict[str, Any]) -> dict[str, Any]:
    first, last = job["frames"]
    total = last - first + 1
    done = len(job["saved"])
    state = "running" if job.get("exit_code") is None else ("done" if job["exit_code"] == 0 and not job.get("cancelled") else
                                                             ("cancelled" if job.get("cancelled") else "failed"))
    return {"job": jid, "state": state, "frames": [first, last], "current_frame": job.get("frame"),
            "saved": done, "total": total, "percent": round(100 * done / total, 1) if total else None,
            "elapsed_s": round((job.get("ended") or time.time()) - job["started"], 1),
            "last_files": job["saved"][-3:], "output": job["output"],
            **({"log_tail": job["log"][-8:]} if state == "failed" else {})}


@mcp.tool()
def background_render(action: str = "start", output: str = "", frame_start: int | None = None,
                      frame_end: int | None = None, job: str = "") -> dict:
    """Render LUNGHI senza bloccare Blender: salva una copia della scena e la renderizza con un secondo Blender in
    background. action: start (output = cartella o modello come C:/render/shot_####.png; senza frame usa il range
    della scena; frame_start = frame_end per un fotogramma solo), status (avanzamento in %, file salvati),
    cancel, list. Mentre gira puoi continuare a lavorare nella scena."""
    if action == "list":
        return {"jobs": [_job_view(j, x) for j, x in _render_jobs.items()]}
    if action in ("status", "cancel"):
        jid = job or (list(_render_jobs)[-1] if _render_jobs else "")
        x = _render_jobs.get(jid)
        if x is None:
            raise RuntimeError(f"lavoro non trovato: {jid or '(nessuno avviato)'}")
        if action == "cancel" and x.get("exit_code") is None:
            x["cancelled"] = True
            x["proc"].terminate()
            with contextlib.suppress(Exception):
                x["proc"].wait(timeout=10)
        return _job_view(jid, x)
    if action != "start":
        raise RuntimeError("action: start | status | cancel | list")
    exe = find_blender()
    if not exe:
        raise RuntimeError("eseguibile di Blender non trovato: imposta AJ_BLENDER_EXE")
    if not output:
        raise RuntimeError("serve output: una cartella o un modello come C:/render/shot_####.png")
    out = os.path.abspath(os.path.expanduser(output))
    if os.path.isdir(out) or out.endswith(("/", "\\")):
        out = os.path.join(out, "frame_####")
    if not os.path.isdir(os.path.dirname(out)):
        raise RuntimeError(f"cartella non trovata: {os.path.dirname(out)}")
    copy = call("save_copy")
    first = int(frame_start if frame_start is not None else copy["frames"][0])
    last = int(frame_end if frame_end is not None else (frame_start if frame_start is not None else copy["frames"][1]))
    if last < first:
        raise RuntimeError("frame_end prima di frame_start")
    cmd = [exe, "-b", copy["file"], "-o", out, "-s", str(first), "-e", str(last), "-a"]
    if exe.endswith(".py"):  # test: finto Blender scritto in Python
        cmd = [sys.executable] + cmd
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
    jid = f"r{len(_render_jobs) + 1}"
    x = {"proc": proc, "frames": [first, last], "output": out, "log": [], "saved": [], "started": time.time(),
         "exit_code": None, "blend": copy["file"]}
    _render_jobs[jid] = x
    threading.Thread(target=_watch_render, args=(x,), daemon=True).start()
    return {"job": jid, "started": True, "frames": [first, last], "output": out, "engine": copy["engine"],
            "hint": "controlla con background_render(action='status')"}


# -------------------------------------------------------------- prompt (1.1)
@mcp.prompt()
def costruisci_scena(descrizione: str) -> str:
    """Costruzione guidata di una scena dalla descrizione."""
    return (f"Costruisci in Blender questa scena: {descrizione}\n"
            "Procedi cosi': scene_info per vedere cosa c'e'; create_object e duplicate_object per la "
            "geometria; set_material; luci con create_object + set_light + look_at; una camera con "
            "set_camera e look_at; set_world per l'ambiente. Usa batch per i gruppi di comandi. "
            "Controlla con viewport_screenshot dopo ogni gruppo e con render_preview alla fine. "
            "Prima di passi pesanti (boolean applicati, fisica) checkpoint save. Se sbagli, undo.")


@mcp.prompt()
def controlla_file() -> str:
    """Controllo di un .blend prima di consegnarlo."""
    return ("Controlla il file aperto in Blender: blender_status e scene_info per il quadro; "
            "missing_files per i file esterni mancanti; find_objects per oggetti nascosti o senza "
            "materiale; set_render per motore e risoluzione. Riassumi i problemi in ordine di gravita' "
            "e proponi le correzioni prima di applicarle.")


if __name__ == "__main__":
    mcp.run()
