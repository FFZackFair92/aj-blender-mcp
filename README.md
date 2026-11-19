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
> Non provati dal vivo: `save_file`, il formato usd e i tool della 1.1 (vedi sotto). I test offline girano
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

## Tool (51)

**Avvio** — `open_blender(file, wait_seconds)`: apre Blender (o un .blend) e aspetta
il ponte; se e' gia' aperto non ne apre un altro

**Lettura** — `blender_status` · `scene_info(limit)` · `object_info(name)` ·
`find_objects(name, type, collection)` (name con `*` e `?`) · `viewport_screenshot(max_size)` ·
`missing_files` (immagini, librerie, suoni e font che non si trovano piu')

**Oggetti** — `create_object(kind, name, location, rotation, scale, size, text)` ·
`transform_object(name, location, rotation, scale)` · `duplicate_object(name, count, offset, linked)`
(una fila di copie in un colpo) · `delete_objects(names)` · `set_parent(name, parent)` ·
`select_objects(names, active)` · `set_visibility(names, viewport, render)`

`kind`: cube, plane, sphere, ico_sphere, cylinder, cone, circle, monkey, empty,
camera, text, point_light, sun, spot_light, area_light. Le rotazioni sono in gradi.

**Collezioni** — `create_collection(name, parent)` · `move_to_collection(names, collection)`

**Aspetto** — `set_material(object, name, color, metallic, roughness, emission_color, emission_strength)` ·
`add_modifier(object, type, name, settings)` · `apply_modifier` · `remove_modifier` ·
`add_constraint(object, type, target, settings)` (TRACK_TO, COPY_LOCATION, CHILD_OF...)

**Qualsiasi proprieta'** — `get_property(target, path)` · `set_property(target, path, value)`:
target e' un oggetto, `scene`, `world` o `material:Nome`, `collection:Nome`...; path come
`data.energy` o `modifiers.Bevel.segments` · `api_info(name)`: proprieta' e valori ammessi
di un tipo di Blender (`SubsurfModifier`, `Light`...) o elenco con `*Modifier`

**Animazione** — `set_keyframe(object, property, frame, value)` · `set_frame(current, start, end, fps)`

**Camera, luci, ambiente** — `look_at(name, target | point)` · `set_camera(name, lens, active, dof_object, fstop)` ·
`set_light(name, energy, color, size, spot_size)` · `set_world(color, strength, hdri)`

**Render** — `set_render(engine, resolution, percentage, samples, fps, transparent)` ·
`render_image(path)` (png, jpg, exr, tif...) · `render_preview(max_size)` (render vero in
piccolo, come immagine) · `set_viewport(shading, frame, camera_view)`

**File** — `import_file(path)` · `export_file(path, selected_only)` (glb, gltf,
fbx, obj, stl, usd) · `save_file(path)`

**Checkpoint (1.2)** — `checkpoint(action, name)`: punti di ripristino dell'intera scena oltre
l'undo (save fa una copia del file senza toccare quello aperto, restore la riapre), list, delete

**Modellazione (1.2)** — `mesh_ops(object, op, settings)`: subdivide, bevel, triangulate,
merge_by_distance, recalc_normals, flip_normals, dissolve_limited, inset, smooth_vertices,
shade_smooth / shade_flat · `uv_unwrap(object, method)` (smart, unwrap, cube, sphere, cylinder,
lightmap) · `boolean(object, cutter, operation, apply)` (difference, union, intersect)

**Materiali, nodi, fisica (1.2)** — `material_nodes(object, inputs, texture, slot)`: qualsiasi
ingresso del Principled BSDF per nome e texture da file collegate (base_color, roughness,
metallic, normal con Normal Map, bump, emission, alpha) · `geometry_nodes(action, object,
group, inputs)`: elenca i gruppi, aggiunge il modificatore, imposta gli ingressi per nome ·
`rigid_body(action, objects, type, mass, shape)`: active/passive, remove, bake

**Animazione in un colpo (1.2)** — `animate(object, keys, property, interpolation, cycles)`:
tutte le chiavi insieme, interpolazione (linear, bezier, ease, bounce, elastic…), ciclo
infinito; funziona anche con le azioni a strati di Blender 4.4+/5.x

**Render lunghi e controlli (1.2)** — `background_render(action, output, frame_start, frame_end)`:
renderizza una copia della scena con un secondo Blender in background (start, status con
percentuale e file salvati, cancel, list) mentre continui a lavorare · `scene_stats(top)`: cosa
pesa nella scena · `view_object(name, shading)`: inquadra un oggetto e ne restituisce l'immagine

**Annulla e lotti** — ogni comando che cambia la scena e' un passo di annullamento
(Ctrl+Z) · `undo(steps, redo)` · `batch(commands)`: piu' comandi in una chiamata, si
ferma al primo errore, un solo passo di annullamento

**Tutto il resto** — `run_python(code)`: codice Python dentro Blender, con `bpy`
e `bmesh`. Quello che assegni a `result` torna indietro.

**Prompt** — `costruisci_scena(descrizione)` · `controlla_file`

> I tool aggiunti nella 1.1 e nella 1.2 sono provati contro il finto Blender;
> `check_live.py` ne prova dal vivo la maggior parte (non `checkpoint restore`, `geometry_nodes`
> e `background_render`). `undo` dal ponte non e'
> ancora stato provato su Blender vero. Dopo l'aggiornamento reinstalla l'add-on
> (passo 2) e riavvia Blender.

Confronto (novembre 2026): StraySpark Blender MCP (a pagamento, 404 tool) ha checkpoint, lavori
asincroni e visione economica; i fork gratuiti (6xvl ~270, naab007 92, emeryporter 86) hanno tool
tipizzati per mesh, UV, rig ed export. Dalla 1.2 qui ci sono checkpoint, render in background
con avanzamento, operazioni mesh, UV, boolean, Geometry Nodes, materiali a nodi e fisica.

Niente servizi esterni di proposito: niente download da Poly Haven, Sketchfab o
generatori 3D online come in altri MCP per Blender. Per gli HDRI usa file sul disco
(`set_world(hdri=...)`).

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
