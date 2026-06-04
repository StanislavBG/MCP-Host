# Enhancement 003 — Managed-dataset providers (agent declares a data shape, host stores + serves it)

Lets an agent register a **data** MCP with zero human involvement: declare a dataset, publish
rows, and the host stores them and **auto-generates the retrieval tools** so consumers can query.
No external endpoint to run (unlike enh-002's declarative-proxy), no guest code — the host owns
storage, retrieval, auth, billing, metering, and audit.

## Two self-serve provider kinds (a provider is one or the other)

| Kind | Agent supplies | Who runs the tool body | Use when |
|------|----------------|------------------------|----------|
| `declarative-proxy` (enh-002) | `backend.endpoint` | the agent's own HTTPS service | the agent has live compute/logic |
| `managed-dataset` (enh-003) | `datasets[]` | the host (reads its own store) | the agent just has data to publish + serve |

Detection at publish: `backend.endpoint` → proxy; `datasets` → managed-dataset; neither → reject.

## Decisions (locked)

- **Schema = free-form JSON documents.** Each row is arbitrary JSON with one designated **key**
  field. No column types to declare. Filtering/sorting works on JSON paths inside the document
  (`json_extract(doc, ?)` in SQLite, `doc->>'field'` in Postgres).
- **Retrieval = host-generated `query` + `get`.** From each dataset the host synthesizes
  `<ds>.query {filters, sort, limit, cursor}` and `<ds>.get {key}`. The agent writes no tool code.
- **Write = owner-gated tool + REST.** `<ds>.publish` (tools/call, `:admin` → owner-gated) and
  `POST /mcp/<id>/datasets/<ds>/data`. Modes: `replace` | `append` | `upsert` (by key).
- Storage is the existing RLS-isolated tenant DB (`<id>.*`), one table per dataset.

## The API sequence (all agent-driven, only the registration api_key needed)

```
1. POST /register                       → { owner_id, api_key }            # enh-002, once
2. POST /providers   (x-api-key)        → host expands datasets → tools,   # declare shape
     body: terse manifest with datasets[]   provisions <id>.*, mounts, 201
3. POST /mcp/<id>/datasets/<ds>/data    → { written, mode }                # publish rows
     (x-api-key, owner-gated)  body: { mode, rows:[ {<key>:..., ...} ] }
     — or tools/call <ds>.publish with the same api_key
4. POST /mcp/<id>  tools/call <ds>.query / <ds>.get   (consumer :read token) # retrieve
     host reads the tenant store, returns { payload, built_at, schema_version }; meters the call
```

## Terse manifest the agent submits (host fills auth/data/tools)

```jsonc
{
  "id": "social-signals-trader", "display_name": "Social Signals Trader",
  "discipline": "social-trading-signals", "version": "1.0.0",
  "summary": "Buy/sell trading signals derived from social sentiment, per ticker.",
  "transport": "streamable-http",
  "datasets": [{
    "name": "signals", "key": "ticker",
    "description": "Latest buy/sell signal and confidence per ticker with an as_of timestamp.",
    "indexed": ["ticker", "as_of"]
  }]
}
```
The host expands this into a full, schema-valid manifest:
`auth.scopes = ["<id>:read","<id>:admin"]`, `data.postgres_schema`, and generated
`tools = [signals.query, signals.get, signals.publish]` (each with a TDQS-passing description,
annotations, and input_schema). The **expanded** manifest is what is validated, persisted, and
re-mounted at boot.

## Query semantics (MVP)

- `filters`: `{ field: value }` (equality) or `{ field: { op: value } }`, `op ∈ {eq,ne,gt,gte,lt,lte,in}`.
  Field names validated `^[A-Za-z_][A-Za-z0-9_]*$` (single-level); values parameterized.
- `sort`: `"field"` (asc) or `"-field"` (desc).
- `limit`: default 50, max 200. `cursor`: opaque offset (base64) — keyset paging is a later upgrade.
- Returns `{ payload: { rows, next_cursor }, built_at, schema_version }`.

## PRDs

| PRD | Title |
|-----|-------|
| 01 | Dataset storage layer — `dataset_sql.py` + tenant `dataset_*` ops (SQLite + Pg) |
| 02 | `ManagedDatasetProvider` + manifest expansion + schema `datasets` block |
| 03 | Deploy dispatch + REST publish endpoint + boot-load both kinds |

## Out of scope (noted)

- Keyset/seek pagination (offset is fine at alpha scale), full-text search, JSON nested-path filters.
- Per-provider signing secret for proxy verification (separate follow-up, enh-002).
- Schema migration/versioning of a published dataset (drop + re-publish for now).
- Cross-dataset joins; aggregation tools (`count`/`groupBy`) — add when a consumer needs them.
