# MCP Explorer App

A Dash app for Databricks that helps you discover and inspect MCP servers across:

- Custom MCP apps in the workspace (`mcp-*` app names)
- Managed MCP endpoints (Genie Spaces, Vector Search, UC Functions, DBSQL)
- External MCP endpoints (Unity Catalog connections marked as MCP)

## What The App Does

- Shows a tabbed registry:
  - `Apps`
  - `Managed MCPs`
  - `External MCPs`
- Provides Managed sub-tabs:
  - `Genie Spaces`
  - `Vector Search`
  - `UC Functions` (grouped by `catalog.schema`)
  - `DBSQL`
- Loads `Apps` on initial page load for faster startup.
- Uses on-demand `Load` buttons for each Managed sub-tab and External tab.
- Supports search filtering by server/tool name.
- Shows loading animations on `Refresh` and each section `Load` button while callbacks run.

## Discovery Behavior

### Apps

- Lists Databricks Apps in the current workspace.
- Filters to app names starting with `mcp-`.
- Probes each app at `<app_url>/mcp` to enumerate tools.

### Managed MCPs

- `DBSQL`: uses workspace endpoint `/api/2.0/mcp/sql`
- `Genie Spaces`: derives endpoints from `genie.list_spaces()`
- `Vector Search`: derives endpoints from vector search indexes
- `UC Functions`: scans schemas and groups entries at `catalog.schema`

### External MCPs

- Lists Unity Catalog connections and keeps those where `options.is_mcp_connection == "true"`.
- Builds MCP proxy endpoints using `/api/2.0/mcp/external/{connection_name}`.

## Scan Limits

To keep scans responsive, per-section discovery is capped by environment variables:

- `MCP_PER_TYPE_SCAN_LIMIT` (default: `100`)
- `MCP_FUNCTION_SCHEMA_SCAN_LIMIT` (default: `100`)
- `MCP_FUNCTIONS_PER_SCHEMA_LIMIT` (default: `100`)

When a cap is hit, the UI shows:

- `MCP Scans capped at <N> endpoints`

## Project Structure

- `src/app`: Dash application source code
- `src/app/app.py`: main application entrypoint
- `src/app/app.yaml`: Databricks App runtime command
- `src/app/requirements.txt`: Python dependencies
- `resources/list_mcp_servers.app.yml`: Databricks App resource definition
- `databricks.yml`: Databricks Asset Bundle configuration

## Prerequisites

- Python 3.11+
- Databricks CLI (recent version)
- Authenticated Databricks CLI profile (this project uses `DEFAULT`)

## Run Locally

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r src/app/requirements.txt
python src/app/app.py
```

Open `http://localhost:8000`.

## Deploy To Databricks (Asset Bundles)

This repository is configured as a Databricks Asset Bundle named `list-mcp-servers`.

### 1) Validate

```bash
databricks bundle validate
```

### 2) Deploy

Deploy to `dev`:

```bash
databricks bundle deploy -t dev
```

Deploy to `prod`:

```bash
databricks bundle deploy -t prod
```

App names by target:

- `list-mcp-servers-dev`
- `list-mcp-servers-prod`

### 3) Open The App

Open it from the Databricks workspace `Apps` UI (or via CLI app commands).