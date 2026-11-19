---
name: no-hardcoded-business-data
description: >-
  Prohibits hardcoding business data in Black Cat code (account codes, FNS
  concept maps, DTE type codes as magic maps, status codes, RUT placeholders,
  IVA rates as scattered literals when a catalog exists). Use whenever creating
  or editing backend/frontend logic that maps external labels to accounts,
  catalogs, statuses, or similar configurable data.
---

# Prohibido código duro (business data)

**Read this skill before writing mappings, catalogs, or “magic” business values.**

## Rule

**Prohibido código en duro** for business configuration. Do not embed account codes, concept→account maps, SII/FNS label dictionaries, status maps, or similar domain tables as Python/JS constants.

| Put here | Not in source code |
|---|---|
| DB catalog tables (`accounting_accounts`, `dte_types`, …) | `_CONCEPT_ACCOUNT_CODES = (("aloj","4101"), …)` |
| Seed / JSON data files under `app/data/` | Hardcoded `4101`, `4190` as default income |
| Columns like `concept_labels` on accounts | Synonym maps in `fns_tax_documents.py` |
| Env / settings for credentials & URLs | Business rules as scattered string literals |

## Hard stops

- Do **not** hardcode chart-of-accounts codes (`4101`, `4102`, …) in services or APIs.
- Do **not** hardcode FNS Cobros column → account maps in Python/JS.
- Do **not** invent parallel “alias tables” in code when the DB/catalog already holds the mapping.
- Do **not** leave “temporary” hardcoded maps “to fix later”.

## Correct approach

1. Store mappings on the catalog row (e.g. `accounting_accounts.concept_labels`).
2. Resolve at runtime: load active accounts from DB → match external label → account id/code.
3. Defaults (catch-all / other income) come from catalog flags or a designated account (`concept_labels` includes `*`), never a literal code in code.
4. Seed/import from `app/data/*.json` so ops can change labels without a code deploy.

## Allowed literals

These are OK (not “business maps”):

- Protocol / HTTP details, SQL operators, date formats (`%Y-%m-%d`)
- Framework route paths that are product URLs (still English per english-code-identifiers)
- UI copy in Spanish shown to users
- One-off numeric tolerances (`0.5` for money compare) when not a business rate table

## When editing legacy hardcoded maps

1. Move data into catalog / JSON seed.
2. Add DB column + migration/patch in `main.py` if needed.
3. Replace call sites with a shared resolver service.
4. Delete the hardcoded tuple/dict from source.
