import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import dash
import dash_mantine_components as dmc
from dash import Input, Output, State, callback, clientside_callback, dcc, html, no_update
from dash_iconify import DashIconify
from databricks.sdk import WorkspaceClient

# dash-mantine-components>=0.14 requires React 18 (uses React.useId).
# Databricks Apps can otherwise serve Dash's legacy React 16 bundle.
dash._dash_renderer._set_react_version("18.2.0")

try:
    from databricks_mcp import DatabricksMCPClient
except ImportError:  # pragma: no cover
    DatabricksMCPClient = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mcp-explorer")

PORT = int(os.getenv("DATABRICKS_APP_PORT", "8000"))


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerInfo:
    name: str
    url: str
    online: bool
    description: str
    tools: list[ToolInfo] = field(default_factory=list)
    error: str | None = None


@dataclass
class RegistryData:
    apps: list[ServerInfo] = field(default_factory=list)
    managed: list[ServerInfo] = field(default_factory=list)
    managed_genie: list[ServerInfo] = field(default_factory=list)
    managed_vector_search: list[ServerInfo] = field(default_factory=list)
    managed_uc_function: list[ServerInfo] = field(default_factory=list)
    managed_dbsql: list[ServerInfo] = field(default_factory=list)
    external: list[ServerInfo] = field(default_factory=list)
    error: str | None = None


def _app_url(app) -> str | None:
    for attr in ("url", "app_url"):
        v = getattr(app, attr, None)
        if v:
            return v
    return None


def discover_mcp_servers() -> list[ServerInfo]:
    """List workspace apps prefixed with 'mcp-' and probe each for tools."""
    w = WorkspaceClient()
    servers: list[ServerInfo] = []

    try:
        apps = list(w.apps.list())
    except Exception as e:
        log.exception("Failed to list workspace apps")
        return [ServerInfo(name="<error>", url="", online=False,
                           description="Could not list workspace apps", error=str(e))]

    mcp_apps = [a for a in apps if (getattr(a, "name", "") or "").startswith("mcp-")]

    for app in mcp_apps:
        name = app.name
        base = _app_url(app) or ""
        url = base.rstrip("/") + "/mcp" if base else ""
        description = getattr(app, "description", "") or "Custom MCP Server"
        tools: list[ToolInfo] = []
        online = False
        err: str | None = None

        if not url:
            err = "App has no URL yet (still deploying?)"
        elif DatabricksMCPClient is None:
            err = "databricks-mcp package not installed"
        else:
            try:
                client = DatabricksMCPClient(server_url=url)
                listed = client.list_tools()
                for t in listed:
                    t_name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else "tool")
                    t_desc = getattr(t, "description", None) or (t.get("description", "") if isinstance(t, dict) else "")
                    t_schema = (
                        getattr(t, "inputSchema", None)
                        or getattr(t, "input_schema", None)
                        or (t.get("inputSchema") or t.get("input_schema") if isinstance(t, dict) else {})
                        or {}
                    )
                    tools.append(ToolInfo(name=t_name, description=t_desc or "", input_schema=t_schema or {}))
                online = True
            except Exception as e:
                log.warning("Failed to list tools for %s: %s", name, e)
                err = str(e)

        servers.append(ServerInfo(
            name=name, url=url, online=online,
            description=description, tools=tools, error=err,
        ))

    return servers


def _build_scan_message(cap_hit: bool, warnings: list[str], per_type_cap: int) -> str | None:
    messages: list[str] = []
    if cap_hit:
        messages.append(f"MCP Scans capped at {per_type_cap} endpoints")
    if warnings:
        messages.extend(warnings)
    return " | ".join(messages) if messages else None


def _discover_managed_dbsql_servers() -> tuple[list[ServerInfo], str | None]:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    per_type_cap = int(os.getenv("MCP_PER_TYPE_SCAN_LIMIT", "100"))
    cap_hit = per_type_cap <= 0
    servers: list[ServerInfo] = []
    if not cap_hit:
        servers.append(
            ServerInfo(
                name="DBSQL",
                url=f"{host}/api/2.0/mcp/sql",
                online=True,
                description="Managed MCP: Databricks SQL",
            )
        )
    return servers, _build_scan_message(cap_hit, [], per_type_cap)


def _discover_managed_genie_servers() -> tuple[list[ServerInfo], str | None]:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    per_type_cap = int(os.getenv("MCP_PER_TYPE_SCAN_LIMIT", "100"))
    cap_hit = False
    warnings: list[str] = []
    servers: list[ServerInfo] = []
    try:
        spaces = (w.genie.list_spaces().spaces or [])
        for s in spaces:
            if len(servers) >= per_type_cap:
                cap_hit = True
                break
            title = getattr(s, "title", None) or getattr(s, "space_id", "Genie Space")
            space_id = getattr(s, "space_id", None)
            if not space_id:
                continue
            servers.append(
                ServerInfo(
                    name=title,
                    url=f"{host}/api/2.0/mcp/genie/{quote(space_id, safe='')}",
                    online=True,
                    description="Managed MCP: Genie Space",
                )
            )
    except Exception as e:
        warnings.append(f"Genie discovery failed: {e}")
    return servers, _build_scan_message(cap_hit, warnings, per_type_cap)


def _discover_managed_vector_search_servers() -> tuple[list[ServerInfo], str | None]:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    per_type_cap = int(os.getenv("MCP_PER_TYPE_SCAN_LIMIT", "100"))
    cap_hit = False
    warnings: list[str] = []
    servers: list[ServerInfo] = []
    try:
        for endpoint in w.vector_search_endpoints.list_endpoints():
            if len(servers) >= per_type_cap:
                cap_hit = True
                break
            endpoint_name = getattr(endpoint, "name", None) or "<endpoint>"
            for idx in w.vector_search_indexes.list_indexes(endpoint_name=endpoint_name):
                if len(servers) >= per_type_cap:
                    cap_hit = True
                    break
                full_name = getattr(idx, "name", None)
                if not full_name:
                    continue
                parts = full_name.split(".")
                if len(parts) < 3:
                    continue
                catalog, schema = parts[0], parts[1]
                index_name = ".".join(parts[2:])
                servers.append(
                    ServerInfo(
                        name=full_name,
                        url=(
                            f"{host}/api/2.0/mcp/vector-search/"
                            f"{quote(catalog, safe='')}/{quote(schema, safe='')}/{quote(index_name, safe='')}"
                        ),
                        online=True,
                        description=f"Managed MCP: Vector Search (endpoint {endpoint_name})",
                    )
                )
    except Exception as e:
        warnings.append(f"Vector Search discovery failed: {e}")
    return servers, _build_scan_message(cap_hit, warnings, per_type_cap)


def _discover_managed_uc_function_servers() -> tuple[list[ServerInfo], str | None]:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    per_type_cap = int(os.getenv("MCP_PER_TYPE_SCAN_LIMIT", "100"))
    max_schemas = int(os.getenv("MCP_FUNCTION_SCHEMA_SCAN_LIMIT", "100"))
    max_functions_per_schema = int(os.getenv("MCP_FUNCTIONS_PER_SCHEMA_LIMIT", "100"))
    cap_hit = False
    scanned = 0
    warnings: list[str] = []
    servers: list[ServerInfo] = []
    try:
        for catalog in w.catalogs.list():
            if len(servers) >= per_type_cap:
                cap_hit = True
                break
            catalog_name = getattr(catalog, "name", None)
            if not catalog_name:
                continue
            for schema in w.schemas.list(catalog_name=catalog_name):
                if len(servers) >= per_type_cap:
                    cap_hit = True
                    break
                schema_name = getattr(schema, "name", None)
                if not schema_name:
                    continue
                if scanned >= max_schemas:
                    cap_hit = True
                    break
                scanned += 1
                functions = list(
                    w.functions.list(
                        catalog_name=catalog_name,
                        schema_name=schema_name,
                        max_results=max_functions_per_schema,
                    )
                )
                if not functions:
                    continue
                tools = [
                    ToolInfo(
                        name=getattr(fn, "name", None) or getattr(fn, "full_name", None) or "function",
                        description=getattr(fn, "comment", None) or "Unity Catalog function",
                        input_schema={},
                    )
                    for fn in functions
                ]
                servers.append(
                    ServerInfo(
                        name=f"{catalog_name}.{schema_name}",
                        url=(
                            f"{host}/api/2.0/mcp/functions/"
                            f"{quote(catalog_name, safe='')}/{quote(schema_name, safe='')}"
                        ),
                        online=True,
                        description=f"Managed MCP: Unity Catalog functions ({len(tools)} discovered)",
                        tools=tools,
                    )
                )
            if scanned >= max_schemas:
                cap_hit = True
                break
    except Exception as e:
        warnings.append(f"UC function discovery failed: {e}")
    return servers, _build_scan_message(cap_hit, warnings, per_type_cap)


def _discover_external_mcp_servers() -> tuple[list[ServerInfo], str | None]:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    per_type_cap = int(os.getenv("MCP_PER_TYPE_SCAN_LIMIT", "100"))
    cap_hit = False
    warnings: list[str] = []
    servers: list[ServerInfo] = []
    try:
        for conn in w.connections.list(max_results=10000):
            if len(servers) >= per_type_cap:
                cap_hit = True
                break
            d = conn.as_dict()
            opts = d.get("options") or {}
            if str(opts.get("is_mcp_connection", "")).lower() != "true":
                continue
            conn_name = d.get("name") or d.get("full_name")
            if not conn_name:
                continue
            target_url = d.get("url") or ""
            conn_type = d.get("connection_type") or "HTTP"
            servers.append(
                ServerInfo(
                    name=f"{conn_name}",
                    url=f"{host}/api/2.0/mcp/external/{quote(conn_name, safe='')}",
                    online=True,
                    description=f"External MCP ({conn_type}) proxied to {target_url}",
                )
            )
    except Exception as e:
        warnings.append(f"External MCP discovery failed: {e}")
    return servers, _build_scan_message(cap_hit, warnings, per_type_cap)


# ---------------- UI ----------------

def _status_badge(online: bool) -> dmc.Badge:
    return dmc.Badge(
        "Online" if online else "Offline",
        color="green" if online else "red",
        variant="light",
        leftSection=DashIconify(
            icon="mdi:circle" if online else "mdi:circle-outline",
            width=10,
        ),
    )


def _tool_card(tool: ToolInfo) -> dmc.Paper:
    schema_text = json.dumps(tool.input_schema or {}, indent=2)
    return dmc.Paper(
        withBorder=True,
        radius="md",
        p="sm",
        children=[
            dmc.Group(
                [
                    DashIconify(icon="mdi:tools", width=18),
                    dmc.Text(tool.name, fw=700),
                ],
                gap="xs",
            ),
            dmc.Text(tool.description or "No description provided.", size="sm", c="dimmed", mt=4),
            dmc.Spoiler(
                showLabel="Show schema",
                hideLabel="Hide schema",
                maxHeight=0,
                children=dmc.Code(schema_text, block=True),
                mt="xs",
            ),
        ],
    )


def _server_item(server: ServerInfo) -> dmc.AccordionItem:
    label = dmc.Group(
        [
            DashIconify(icon="mdi:server-network", width=20),
            dmc.Text(server.name, fw=600),
            _status_badge(server.online),
            dmc.Text(f"{len(server.tools)} tool(s)", size="xs", c="dimmed"),
        ],
        gap="sm",
    )

    body: list = [
        dmc.Text(server.description, size="sm", c="dimmed"),
        dmc.Text(server.url or "(no url)", size="xs", c="dimmed", style={"fontFamily": "monospace"}),
    ]
    if server.error:
        body.append(dmc.Alert(server.error, title="Probe failed", color="red", variant="light", mt="xs"))
    if server.tools:
        body.append(dmc.Stack([_tool_card(t) for t in server.tools], gap="xs", mt="sm"))
    else:
        body.append(dmc.Text("No tools reported.", size="sm", c="dimmed", mt="sm"))

    return dmc.AccordionItem(
        value=server.name,
        children=[
            dmc.AccordionControl(label),
            dmc.AccordionPanel(dmc.Stack(body, gap="xs")),
        ],
    )


def _filter_servers(servers: list[ServerInfo], query: str) -> list[ServerInfo]:
    if not query:
        return servers
    q = query.lower()
    out: list[ServerInfo] = []
    for s in servers:
        if q in s.name.lower():
            out.append(s)
            continue
        matched_tools = [t for t in s.tools if q in t.name.lower() or q in (t.description or "").lower()]
        if matched_tools:
            out.append(ServerInfo(
                name=s.name, url=s.url, online=s.online,
                description=s.description, tools=matched_tools, error=s.error,
            ))
    return out


def _render_accordion(servers: list[ServerInfo], empty_message: str) -> Any:
    if not servers:
        return dmc.Alert(
            empty_message,
            title="Empty registry",
            color="blue",
            variant="light",
            icon=DashIconify(icon="mdi:information-outline"),
        )
    return dmc.Accordion(
        chevronPosition="left",
        variant="separated",
        radius="md",
        children=[_server_item(s) for s in servers],
    )


def _section_to_json(servers: list[ServerInfo], error: str | None = None) -> dict[str, Any]:
    return {"servers": _servers_to_json(servers), "error": error}


def _section_from_json(data: dict[str, Any] | None) -> tuple[list[ServerInfo], str | None, bool]:
    if data is None:
        return [], None, False
    return _servers_from_json(data.get("servers", [])), data.get("error"), True


def _render_loadable_section(
    data: dict[str, Any] | None,
    query: str,
    empty_message: str,
    button_id: str,
) -> Any:
    servers, error, loaded = _section_from_json(data)
    load_button = dmc.Center(
        dmc.Button(
            "Load",
            id=button_id,
            leftSection=DashIconify(icon="mdi:download", width=16),
            variant="light",
            size="sm",
            style={"width": "120px"},
        )
    )
    body: list[Any] = [load_button]
    if not loaded:
        body.append(dmc.Text("Not loaded yet.", c="dimmed", size="sm", ta="center"))
        return dmc.Stack(body, gap="md", mt="xl", style={"minHeight": "200px"})
    else:
        if error:
            body.append(dmc.Alert(error, color="yellow", variant="light"))
        body.append(_render_accordion(_filter_servers(servers, query), empty_message))
    return dmc.Stack(body, gap="sm")


def _render_tabs(
    apps_data: dict[str, Any] | None,
    managed_genie_data: dict[str, Any] | None,
    managed_vector_data: dict[str, Any] | None,
    managed_uc_function_data: dict[str, Any] | None,
    managed_dbsql_data: dict[str, Any] | None,
    external_data: dict[str, Any] | None,
    active_top_tab: str | None,
    active_managed_tab: str | None,
    query: str,
) -> Any:
    apps, apps_error, _apps_loaded = _section_from_json(apps_data)
    return dmc.Tabs(
        id="top-tabs",
        value=active_top_tab or "apps",
        children=[
            dmc.TabsList(
                [
                    dmc.TabsTab("Apps", value="apps"),
                    dmc.TabsTab(
                        "Managed MCPs",
                        value="managed",
                    ),
                    dmc.TabsTab("External MCPs", value="external"),
                ],
                grow=True,
            ),
            dmc.TabsPanel(
                dmc.Stack(
                    [
                        dmc.Alert(apps_error, color="yellow", variant="light")
                        if apps_error
                        else dmc.Space(h=0),
                        _render_accordion(
                            _filter_servers(apps, query),
                            "No MCP app servers found. Deploy an app prefixed with 'mcp-' to register one.",
                        ),
                    ],
                    gap="sm",
                ),
                value="apps",
                pt="md",
            ),
            dmc.TabsPanel(
                dmc.Tabs(
                    id="managed-tabs",
                    value=active_managed_tab or "genie",
                    children=[
                        dmc.TabsList(
                            [
                                dmc.TabsTab("Genie Spaces", value="genie"),
                                dmc.TabsTab("Vector Search", value="vector-search"),
                                dmc.TabsTab("UC Functions", value="uc-function"),
                                dmc.TabsTab("DBSQL", value="dbsql"),
                            ]
                        ),
                        dmc.TabsPanel(
                            _render_loadable_section(
                                managed_genie_data,
                                query,
                                "No managed Genie Space MCP servers found.",
                                "load-managed-genie-btn",
                            ),
                            value="genie",
                            pt="md",
                        ),
                        dmc.TabsPanel(
                            _render_loadable_section(
                                managed_vector_data,
                                query,
                                "No managed Vector Search MCP servers found.",
                                "load-managed-vector-search-btn",
                            ),
                            value="vector-search",
                            pt="md",
                        ),
                        dmc.TabsPanel(
                            _render_loadable_section(
                                managed_uc_function_data,
                                query,
                                "No managed UC Function MCP servers found.",
                                "load-managed-uc-function-btn",
                            ),
                            value="uc-function",
                            pt="md",
                        ),
                        dmc.TabsPanel(
                            _render_loadable_section(
                                managed_dbsql_data,
                                query,
                                "No managed DBSQL MCP servers found.",
                                "load-managed-dbsql-btn",
                            ),
                            value="dbsql",
                            pt="md",
                        ),
                    ],
                ),
                value="managed",
                pt="md",
            ),
            dmc.TabsPanel(
                _render_loadable_section(
                    external_data,
                    query,
                    "No external MCP servers found in this workspace.",
                    "load-external-btn",
                ),
                value="external",
                pt="md",
            ),
        ],
    )


app = dash.Dash(__name__, title="Workspace MCP Registry Listing", suppress_callback_exceptions=True)
server = app.server  # for gunicorn-style runners if ever needed

app.layout = dmc.MantineProvider(
    theme={"primaryColor": "indigo", "defaultRadius": "md"},
    children=dmc.AppShell(
        padding="md",
        children=[
            dcc.Store(id="apps-store", data=None),
            dcc.Store(id="managed-genie-store", data=None),
            dcc.Store(id="managed-vector-search-store", data=None),
            dcc.Store(id="managed-uc-function-store", data=None),
            dcc.Store(id="managed-dbsql-store", data=None),
            dcc.Store(id="external-store", data=None),
            dcc.Store(id="active-top-tab-store", data="apps"),
            dcc.Store(id="active-managed-tab-store", data="genie"),
            dcc.Interval(id="initial-apps-load", interval=1, n_intervals=0, max_intervals=1),
            dmc.Container(
                size="lg",
                children=[
                    dmc.Group(
                        justify="space-between",
                        align="center",
                        mt="md",
                        children=[
                            dmc.Group(
                                [
                                    DashIconify(icon="mdi:hub", width=28),
                                    dmc.Title("Workspace MCP Registry Listing", order=2),
                                ],
                                gap="sm",
                            ),
                            dmc.Button(
                                "Refresh",
                                id="refresh-btn",
                                leftSection=DashIconify(icon="mdi:refresh", width=18),
                                variant="light",
                            ),
                        ],
                    ),
                    dmc.Text(
                        "Discover MCP Servers in your Databricks workspace.",
                        c="dimmed",
                        mt=4,
                    ),
                    dmc.TextInput(
                        id="search",
                        placeholder="Filter by server or tool name…",
                        leftSection=DashIconify(icon="mdi:magnify", width=18),
                        mt="md",
                    ),
                    dmc.Space(h="md"),
                    dcc.Loading(
                        type="dot",
                        children=html.Div(
                            id="server-list",
                            children=dmc.Text("Loading Apps...", c="dimmed", ta="center", mt="lg"),
                        ),
                    ),
                ],
            ),
        ],
    ),
)


def _servers_to_json(servers: list[ServerInfo]) -> list[dict]:
    return [
        {
            "name": s.name, "url": s.url, "online": s.online,
            "description": s.description, "error": s.error,
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in s.tools
            ],
        }
        for s in servers
    ]


def _servers_from_json(data: list[dict]) -> list[ServerInfo]:
    return [
        ServerInfo(
            name=d["name"], url=d["url"], online=d["online"],
            description=d["description"], error=d.get("error"),
            tools=[ToolInfo(**t) for t in d.get("tools", [])],
        )
        for d in (data or [])
    ]


@callback(
    Output("apps-store", "data"),
    Input("refresh-btn", "n_clicks"),
    Input("initial-apps-load", "n_intervals"),
    running=[
        (Output("refresh-btn", "loading"), True, False),
        (Output("refresh-btn", "disabled"), True, False),
    ],
)
def _load_apps(_n, _init):
    return _section_to_json(discover_mcp_servers())


@callback(
    Output("managed-genie-store", "data"),
    Input("load-managed-genie-btn", "n_clicks"),
    running=[
        (Output("load-managed-genie-btn", "loading"), True, False),
        (Output("load-managed-genie-btn", "disabled"), True, False),
    ],
)
def _load_managed_genie(n_clicks):
    if not n_clicks:
        return no_update
    servers, error = _discover_managed_genie_servers()
    return _section_to_json(servers, error)


@callback(
    Output("managed-vector-search-store", "data"),
    Input("load-managed-vector-search-btn", "n_clicks"),
    running=[
        (Output("load-managed-vector-search-btn", "loading"), True, False),
        (Output("load-managed-vector-search-btn", "disabled"), True, False),
    ],
)
def _load_managed_vector_search(n_clicks):
    if not n_clicks:
        return no_update
    servers, error = _discover_managed_vector_search_servers()
    return _section_to_json(servers, error)


@callback(
    Output("managed-uc-function-store", "data"),
    Input("load-managed-uc-function-btn", "n_clicks"),
    running=[
        (Output("load-managed-uc-function-btn", "loading"), True, False),
        (Output("load-managed-uc-function-btn", "disabled"), True, False),
    ],
)
def _load_managed_uc_function(n_clicks):
    if not n_clicks:
        return no_update
    servers, error = _discover_managed_uc_function_servers()
    return _section_to_json(servers, error)


@callback(
    Output("managed-dbsql-store", "data"),
    Input("load-managed-dbsql-btn", "n_clicks"),
    running=[
        (Output("load-managed-dbsql-btn", "loading"), True, False),
        (Output("load-managed-dbsql-btn", "disabled"), True, False),
    ],
)
def _load_managed_dbsql(n_clicks):
    if not n_clicks:
        return no_update
    servers, error = _discover_managed_dbsql_servers()
    return _section_to_json(servers, error)


@callback(
    Output("external-store", "data"),
    Input("load-external-btn", "n_clicks"),
    running=[
        (Output("load-external-btn", "loading"), True, False),
        (Output("load-external-btn", "disabled"), True, False),
    ],
)
def _load_external(n_clicks):
    if not n_clicks:
        return no_update
    servers, error = _discover_external_mcp_servers()
    return _section_to_json(servers, error)


@callback(
    Output("server-list", "children"),
    Input("apps-store", "data"),
    Input("managed-genie-store", "data"),
    Input("managed-vector-search-store", "data"),
    Input("managed-uc-function-store", "data"),
    Input("managed-dbsql-store", "data"),
    Input("external-store", "data"),
    Input("active-top-tab-store", "data"),
    Input("active-managed-tab-store", "data"),
    Input("search", "value"),
)
def _render(
    apps_data,
    managed_genie_data,
    managed_vector_data,
    managed_uc_function_data,
    managed_dbsql_data,
    external_data,
    active_top_tab,
    active_managed_tab,
    query,
):
    if apps_data is None:
        return dmc.Text("Loading Apps...", c="dimmed", ta="center", mt="lg")
    return _render_tabs(
        apps_data,
        managed_genie_data,
        managed_vector_data,
        managed_uc_function_data,
        managed_dbsql_data,
        external_data,
        active_top_tab,
        active_managed_tab,
        query or "",
    )


@callback(
    Output("active-top-tab-store", "data"),
    Input("top-tabs", "value"),
)
def _remember_top_tab(value):
    return value or "apps"


@callback(
    Output("active-managed-tab-store", "data"),
    Input("managed-tabs", "value"),
)
def _remember_managed_tab(value):
    return value or "genie"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
