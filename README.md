# AJ Blender MCP

Server MCP che pilota Blender a parole: legge la scena, crea e trasforma
oggetti, assegna materiali e modificatori, importa ed esporta file, guarda la
vista 3D ed esegue Python. Scritto da zero, stesso stampo del Resolume MCP e
del MadMapper MCP.

Lavora solo sulla scena locale: nessun servizio esterno, nessuna chiave API,
nessuna telemetria.

```
Claude  <--stdio-->  blender_mcp.py  <--HTTP locale :9991-->  aj_blender_bridge.py (add-on di Blender)
```

> **Stato:** provato dal vivo con Blender 5.2.1 LTS su Windows (`check_live.py`):
> oggetti, materiali, modificatori, vista 3D, import ed export glb/obj/stl/fbx.
> Non provati dal vivo: `save_file` e il formato usd. I test offline girano
> contro un finto Blender (`fake_bpy.py`). Pensato per Blender 3.6 o successivo.

## 1. Installa uv

```powershell
winget install --id=astral-sh.uv -e
```

## 2. Installa l'add-on in Blender

**Edit > Preferences > Add-ons > Install from Disk**, scegli
`aj_blender_bridge.py` e attiva **AJ MCP Bridge**. Il ponte parte con Blender;
nella barra laterale della vista 3D (tasto N) la scheda **AJ MCP** mostra se e'
attivo. Al primo avvio crea il token in `~/.aj-mcp/blender.token`.

## 3. Registra il server in Claude

In `claude_desktop_config.json`, dentro `mcpServers`:

```json
"blender": {
  "command": "uv",
  "args": ["run", "--script", "C:\\percorso\\blender-mcp\\blender_mcp.py"]
}
```

Riavvia Claude.

## 4. Prova

```powershell
uv run --script check_live.py
```

Crea oggetti di prova, verifica ogni funzione e li elimina.

## Tool

**Avvio** — `open_blender(file, wait_seconds)`: apre Blender (o un .blend) e aspetta
il ponte; se e' gia' aperto non ne apre un altro

**Lettura** — `blender_status` · `scene_info(limit)` · `object_info(name)` ·
`viewport_screenshot(max_size)`

**Oggetti** — `create_object(kind, name, location, rotation, scale, size, text)` ·
`transform_object(name, location, rotation, scale)` · `delete_objects(names)`

`kind`: cube, plane, sphere, ico_sphere, cylinder, cone, circle, monkey, empty,
camera, text, point_light, sun, spot_light, area_light. Le rotazioni sono in gradi.

**Aspetto** — `set_material(object, name, color, metallic, roughness, emission_color, emission_strength)` ·
`add_modifier(object, type, name, settings)`

**File** — `import_file(path)` · `export_file(path, selected_only)` (glb, gltf,
fbx, obj, stl, usd) · `save_file(path)`

**Tutto il resto** — `run_python(code)`: codice Python dentro Blender, con `bpy`
e `bmesh`. Quello che assegni a `result` torna indietro.

## Sicurezza

Il ponte esegue codice Python in Blender, quindi:

- ascolta solo su `127.0.0.1`, non e' raggiungibile dalla rete;
- richiede il token di `~/.aj-mcp/blender.token` (header `X-AJ-Token`);
- senza token non parte nessun comando.

Chi ha quel file puo' pilotare Blender: non condividerlo.

## Configurazione

| Variabile | Default | A cosa serve |
|---|---|---|
| `AJ_BLENDER_URL` | `http://127.0.0.1:9991` | indirizzo del ponte (lato server MCP) |
| `AJ_BLENDER_TOKEN` | letto dal file | token, se vuoi passarlo a mano |
| `AJ_BLENDER_EXE` | cercato da solo | eseguibile di Blender per `open_blender` |
| `AJ_BLENDER_PORT` | `9991` | porta del ponte (va impostata prima di avviare Blender) |

## File

- `blender_mcp.py` — il server MCP
- `aj_blender_bridge.py` — l'add-on, gira dentro Blender
- `check_live.py` — prova dal vivo
- `fake_bpy.py`, `test_offline.py` — test senza Blender (li usa la CI)

## Licenza

MIT, vedi [LICENSE](LICENSE).
