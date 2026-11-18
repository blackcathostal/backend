---
name: english-code-identifiers
description: >-
  Enforces English-only for non-visual code identifiers across Black Cat
  (filenames, folders, variables, functions, classes, modules, routes, DB
  columns, env keys, commits). Spanish is allowed only in user-facing UI copy.
  Use whenever creating or editing code, files, APIs, schemas, scripts, or
  commits in this project.
---

# English-only code identifiers

**Read this skill before writing or renaming any non-visual code.**

## Rule

Everything that is **not** end-user visible text must be **English**:

| Must be English | May be Spanish |
|---|---|
| File and folder names | UI labels, buttons, placeholders |
| Variables, functions, classes, types | Toast / error messages shown to users |
| Module / package names | Email / Instagram / Facebook guest copy |
| API paths, query params, JSON keys (new) | Admin panel visible strings |
| DB tables/columns (new), env var names | FAQ / hostel content for guests |
| Comments in code, commit messages, PR titles | |

## Hard stops

- Do **not** create files like `habitaciones.py`, `recibidas_vista.vue`, `descargar_pdf.js`
- Do **not** name symbols `razonSocial`, `folioEmitida`, `cargarFoto`, `actualizarRecibidas`
- Do **not** invent Spanish route segments for new APIs

## Correct examples

```text
OK file:    room_photos.py / tax_documents.py / billing_portal_web.py
OK symbol:  upload_room_photo, has_pdf, counterpart_name
OK UI:      "Habitaciones", "Descargar PDF", "Actualizar Recibidas"
BAD file:   fotos_habitacion.py
BAD symbol: cargar_foto_habitacion
```

## When touching Spanish legacy names

- Prefer English for **new** code.
- Do not mass-rename production APIs/DB columns unless the user explicitly asks.
- If you must reference a legacy Spanish field, keep the external contract and use an English alias in new code when practical.

## Probe / test scripts

- Never commit `scripts/probe_*`, `scripts/debug_*`, or other one-off test scripts.
- Keep them local only (they are gitignored).
