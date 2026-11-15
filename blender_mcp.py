# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""AJ Blender MCP: server MCP che pilota Blender.

Parla via HTTP locale con l'add-on aj_blender_bridge.py (porta 9991).
Lavora solo sulla scena locale: nessun servizio esterno, nessuna chiave API.

Config (variabili d'ambiente, tutte facoltative):
  AJ_BLENDER_URL    indirizzo del ponte  (default http://127.0.0.1:9991)
  AJ_BLENDER_TOKEN  token                (default: letto da ~/.aj-mcp/blender.token)
"""
import base64
import json
import os
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


if __name__ == "__main__":
    mcp.run()
