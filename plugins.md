# HueMix-Link Plugin System

HueMix-Link supports runtime-loaded plugins that extend the core platform with
custom device types, packet handlers, web UI pages, OTA firmware updates, and
more — all without modifying the core codebase.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Plugin Lifecycle](#plugin-lifecycle)
- [Plugin Manifest (`plugin.json`)](#plugin-manifest-pluginjson)
- [Entry Points](#entry-points)
- [PluginHost API](#pluginhost-api)
- [Lifecycle Hooks](#lifecycle-hooks)
- [Event System](#event-system)
- [Device Registration & Packet Routing](#device-registration--packet-routing)
- [MAC Binding](#mac-binding)
- [Home Box Integration](#home-box-integration)
- [Flask Blueprint Registration](#flask-blueprint-registration)
- [OTA Firmware Support](#ota-firmware-support)
- [Complete Example: Temperature Tracking Plugin](#complete-example-temperature-tracking-plugin)
- [Installation](#installation)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│                    HueMix-Link Core                        │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │PluginManager│  │ NetworkServer│  │ PairingManager    │  │
│  │             │  │              │  │                   │  │
│  │ • load      │  │ • UDP packets│  │ • pair/unpair     │  │
│  │ • lifecycle │  │ • MAC routing│  │ • device rename   │  │
│  │ • events    │  │ • message IDs│  │ • pairing history │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬──────────┘  │
│         │                │                   │             │
│         └────────────────┼───────────────────┘             │
│                          │                                 │
│              PluginHost (injected services)                │
└──────────────────────────┼─────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Plugin A │ │ Plugin B │ │ Plugin C │
        │ (device) │ │ (service)│ │ (web UI) │
        └──────────┘ └──────────┘ └──────────┘
```

The **PluginManager** loads enabled plugins at startup from the registry
(`python/data/plugins.json`). Each plugin receives a **PluginHost** object that
provides access to core services (network server, pairing manager, config
manager, etc.) and an event bus for inter-plugin communication.

Plugins can:

- Register **custom device types** — when a device HELLO advertises a
  `plugin_id` matching the plugin, the network server routes its packets to the
  plugin.
- Handle **raw packet events** — subscribe to `packet_received` and inspect
  packet types, source MACs, and payloads.
- Expose **Flask blueprints** — add web UI endpoints under
  `/plugins/<your-plugin>/...`.
- Provide **OTA firmware metadata** — list devices eligible for over-the-air
  updates.
- Define a **home box card** — a tile rendered on the main dashboard for quick
  status at a glance.
- Subscribe to **core events** — react to pairing, device rename, and other
  system events.

---

## Plugin Lifecycle

Each plugin progresses through a well-defined lifecycle. All hooks are optional
— implement only what your plugin needs.

```
  ┌──────────────┐
  │  Registered  │  (entry exists in plugins.json)
  └──────┬───────┘
         │ [enabled = true]
         ▼
  ┌──────────────┐
  │    Import    │  module imported via importlib
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │   Create     │  create_plugin() or Plugin() called
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ configure /  │  hook: prepare internal state
  │   setup      │
  └──────┬───────┘
         ▼
  ┌──────────────┐                                    ┌─────────────┐
  │   register   │  hook: register Flask blueprint    │  As many    │
  │              │  with the core app object          │  events as  │
  └──────┬───────┘                                    │  desired    │
         ▼                                            └──────▲──────┘
  ┌──────────────┐                                           │
  │ start / run  │  hook: subscribe to events, start         │
  │ / initialize │  background threads, open resources       │
  └──────┬───────┘                                           │
         │                                                   │
         ▼  (normal operation)                               │
  ┌──────────────┐  dispatch_event() ────────────────────────┘
  │   Running    │  on_packet_received()
  └──────┬───────┘  on_plugin_hello()
         │
         │ [shutdown]
         ▼
  ┌──────────────┐
  │  stop /      │  hook: flush state, close resources,
  │  shutdown /  │  stop background threads
  │  teardown    │
  └──────────────┘
```

The PluginManager calls hooks with flexible signatures. It tries multiple
argument combinations so your hook methods can accept only the parameters they
need:

```python
# All of these signatures work for register():
def register(self, app, context, host, manifest, options): ...
def register(self, app, context, host): ...
def register(self, context, host): ...
def register(self, app): ...
def register(self): ...
```

---

## Plugin Manifest (`plugin.json`)

Every plugin must provide a manifest as a standalone `plugin.json`
file at the root of the repository. The manifest is stored in the global registry at
`python/data/plugins.json` after installation.

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | `string` | Human-readable identifier (e.g. `"temperature_tracking"`) |
| `plugin_id` | `string` | UUID v4 that uniquely identifies this plugin. Must match the UUID compiled into supporting device firmware. |
| `module` | `string` | Python import path: `"plugins.<package_name>.plugin"` |
| `version` | `string` | Semantic version of the plugin code (e.g. `"1.2.0"`). Used for update checking. |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Whether the plugin loads at startup |
| `plugin_api_version` | `integer` | `1` | Plugin API version this plugin targets. The core rejects plugins with mismatched API versions. |
| `kind` | `string` | `"generic"` | Plugin category |
| `capabilities` | `string[]` | `[]` | Feature flags like `"devices"`, `"web_ui"`, `"ota"` |
| `options` | `object` | `{}` | Free-form configuration passed to `create_plugin()` |
| `devices` | `array` | `[]` | Device type declarations for routing (see below) |
| `ota` | `array` | `[]` | OTA firmware target declarations |
| `home_box` | `object` | `null` | Dashboard card definition |
| `metadata` | `object` | `{}` | Install-time metadata (source_repo, installed_at, etc.) |

### Device Declaration

Each entry in the `devices` array tells the core:

- What `device_type` integer this plugin claims.
- Which `packet_types` it handles.
- How the device is displayed in the pairing UI.

```json
{
  "id": "temperature_tracking",
  "plugin_id": "550e8400-e29b-41d4-a716-446655440000",
  "module": "plugins.temperature_tracking.plugin",
  "enabled": true,
  "devices": [
    {
      "device_type": 0,
      "name": "Temperature Sensor",
      "type": "Sensor",
      "icon": "🌡️"
    },
    {
      "device_type": 1,
      "name": "Battery Temp Sensor",
      "type": "Sensor",
      "icon": "🌡️"
    }
  ]
}
```

When the network server receives a HELLO packet advertising a `plugin_id` that
matches this plugin's UUID, it routes the device to this plugin. The
`device_type` value is used for pairing rules and display.

### Home Box Card

The `home_box` field defines a tile card rendered on the main dashboard:

```json
{
  "home_box": {
    "name": "Temperature Tracking",
    "description": "View sensor temperatures and configure devices",
    "icon": "thermometer-half",
    "color": "emerald",
    "link": "/plugins/temperature"
  }
}
```

If `home_box` is absent, no dashboard card is shown.

---

## Entry Points

Your plugin module must expose one of these factory callables:

### `create_plugin(options=None, context=None, host=None, manifest=None)`

A factory function that returns your plugin instance:

```python
def create_plugin(options=None, context=None, host=None, manifest=None, **_kwargs):
    return MyPlugin(options=options or {}, context=context or host, manifest=manifest)
```

### `Plugin` class

A class that the PluginManager instantiates directly:

```python
class Plugin:
    def __init__(self, options=None, context=None, host=None, manifest=None):
        self.options = options or {}
        self.context = context or host
        self.manifest = manifest
```

The PluginManager tries multiple argument combinations so your factory can
accept only the parameters it needs:

```python
# All supported signatures:
create_plugin(options, context, host, manifest)
create_plugin(options, context, host)
create_plugin(options, context)
create_plugin(context)
create_plugin()
```

---

## PluginHost API

The `PluginHost` object is the bridge between your plugin and the core system.
It is passed to your plugin as `context` or `host` in lifecycle hooks and
events.

### Service Access

```python
# Get a service, returning None if unavailable
network_server = host.service('network_server')

# Get a service, raising KeyError if unavailable
network_server = host.require('network_server')

# Check if a service exists
if host.has('pairing_manager'):
    ...
```

### Available Services

| Service Name | Object | Description |
|---|---|---|
| `app` | Flask application | Register blueprints, access config |
| `config_manager` | ConfigManager | Read/write app configuration |
| `data_manager` | DataManager | Read/write JSON data files |
| `network_server` | NetworkServer | Send/recv UDP packets to devices |
| `pairing_manager` | PairingManager | Pair/unpair devices, manage history |
| `automation_service` | AutomationService | Trigger automations |
| `home_id_manager` | HomeIDManager | Read/write HOME_ID |
| `plugin_manager` | PluginManager | Query other plugins, MAC bindings |
| `logger` | Logger | Standard Python logger |

### Event Bus

```python
# Publish an event that other plugins or the core can react to
host.publish('temperature_reading', {'sensor_mac': ..., 'temperature_c': ...})

# Subscribe to events from other plugins
host.subscribe('device_renamed', self.on_device_renamed)
```

---

## Lifecycle Hooks

All hooks are optional. Implement only what you need.

### `configure(options, context, host, manifest)` / `setup(...)`

Called first, before anything else. Use this to initialize internal state from
`options` and `manifest`.

```python
def setup(self, options=None, context=None, host=None, manifest=None):
    self.plugin_uuid = manifest.get('plugin_id') if manifest else None
    self.history_limit = options.get('history_limit', 100) if options else 100
```

### `register(app, context, host, manifest, options)`

Called after `configure/setup`. Use this to register Flask blueprints with the
core application.

```python
def register(self, app, context=None):
    self.context = context or self.context
    app.register_blueprint(self.blueprint)
```

### `start(context, host, manifest, app)` / `run(...)` / `initialize(...)`

Called last, after `register`. Use this to subscribe to events, start
background threads, open file handles, or begin network operations.

```python
def start(self, context=None):
    self.context = context or self.context
    self._subscribe_to_events()
    self._start_background_worker()
```

### `stop(context, host)` / `shutdown(...)` / `teardown(...)`

Called during graceful shutdown (reverse order of loading). Use this to flush
state, close resources, and stop threads.

```python
def stop(self, context=None):
    self._stop_background_worker()
    self._flush_readings_to_disk()
```

---

## Event System

Plugins receive events through several mechanisms:

### `on_event(event_name, payload, host)`

Generic event handler — receives all events the core dispatches:

```python
def on_event(self, event_name, payload, host=None):
    if event_name == 'packet_received':
        self._handle_packet(payload)
```

### `handle_event(event_name, payload, host)`

Alias for `on_event` with the same semantics.

### `on_{event_name}(payload, host)`

Specific event handler — only called for the named event. Takes priority over
`on_event`/`handle_event`:

```python
def on_packet_received(self, payload, host=None):
    # Only receives 'packet_received' events
    packet_type = payload.get('type')
    source_mac = payload.get('source_mac')
    ...
```

### Events Dispatched by the Core

| Event | Payload | When |
|---|---|---|
| `packet_received` | `{type, payload, source_mac, sender_ip, gateway_radio_mac}` | Any UDP packet arrives |
| `plugin_hello` | `{device_mac, plugin_uuid, plugin_device_type, rssi, version, platform, sender_ip, gateway_radio_mac, is_paired}` | A plugin device sends a HELLO |
| `device_renamed` | `{device_id, plugin_id, old_name, new_name, device_type}` | A paired device is renamed |

### Publishing Custom Events

Plugins can publish their own events for other plugins to consume:

```python
# In plugin A:
host.publish('temperature_reading', {
    'sensor_mac': 'AA:BB:CC:DD:EE:FF',
    'temperature_c': 23.5,
})

# In plugin B:
def start(self, context=None):
    context.subscribe('temperature_reading', self.on_temp_reading)

def on_temp_reading(self, event_name, payload, host=None):
    print(f"Got reading: {payload['temperature_c']}°C")
```

---

## Device Registration & Packet Routing

This is the most powerful feature of the plugin system — plugins can define
their own device types and handle raw packets.

### How Routing Works

```
Device HELLO ──► NetworkServer
                    │
                    ▼
              Has plugin_id in HELLO?
              ┌─────┴─────┐
             Yes           No
              │             │
              ▼             ▼
        PluginManager   Standard
        .get_plugin_    pairing
        for_mac()       flow
              │
              ▼
        Route all packets
        from that MAC to
        the owning plugin
```

### Declaring Device Types

In your manifest's `devices` array, list each device type your plugin owns:

```json
{
  "devices": [
    {"device_type": 42, "name": "My Custom Sensor", "type": "Sensor", "icon": "🔵"}
  ]
}
```

### Handling Packets

Implement `on_packet_received` in your plugin:

```python
MY_PACKET_TYPE = 0x55

def on_packet_received(self, event_name, payload, host=None):
    packet_type = payload.get('type')
    packet_payload = payload.get('payload') or b''
    source_mac = payload.get('source_mac')

    if packet_type != MY_PACKET_TYPE:
        return

    # Parse your custom protocol
    temp, humidity = struct.unpack('<hH', packet_payload[:4])
    self._store_reading(source_mac, temp / 10.0, humidity / 10.0)
```

### Sending Packets

Use `network_server.send_raw_packet_to_device()` to send data:

```python
def _send_config(self, mac):
    network_server = self.context.require('network_server')
    packet = self._build_packet(mac)
    network_server.send_raw_packet_to_device(
        mac,
        packet,
        wait_for_delivery=True,
        msg_id=network_server.get_message_id(),
        gateway_preference=gateway_mac,
    )
```

Because routing is MAC-based, multiple plugins can safely declare the same
`device_type` without conflicts.

---

## MAC Binding

When a plugin device is paired, the core creates a persistent `MAC → plugin_id`
binding stored in `python/data/plugin_device_bindings.json`. This binding
ensures that all future packets from that MAC are routed to the correct plugin.

```python
# Core side (called during pairing):
plugin_manager.bind_mac_to_plugin("AA:BB:CC:DD:EE:FF", "550e8400-...")

# Plugin side (check if a MAC belongs to you):
plugin_manager = host.require('plugin_manager')
plugin_for_mac = plugin_manager.get_plugin_for_mac(source_mac)
if plugin_for_mac != self.plugin_uuid:
    return  # Not my device
```

Your plugin should always verify that incoming packets are from a device bound
to it:

```python
def on_packet_received(self, event_name, payload, host=None):
    source_mac = payload.get('source_mac')
    plugin_manager = self._get_plugin_manager(host)
    if not plugin_manager:
        return

    plugin_for_mac = plugin_manager.get_plugin_for_mac(source_mac)
    if not plugin_for_mac or plugin_for_mac != self.plugin_uuid:
        return  # Not our device, skip

    # Safe to process
    ...
```

---

## Home Box Integration

A "home box" is a card rendered on the main dashboard (`/`). Define it in your
manifest:

```json
{
  "home_box": {
    "name": "Temperature Tracking",
    "description": "View sensor temperatures and configure devices",
    "icon": "thermometer-half",
    "color": "emerald",
    "link": "/plugins/temperature"
  }
}
```

- `icon`: Font Awesome icon name (e.g., `"thermometer-half"`, `"microchip"`)
- `color`: Tailwind color name (e.g., `"emerald"`, `"sky"`, `"violet"`)
- `link`: URL path to your plugin's main page

---

## Flask Blueprint Registration

Plugins can serve their own web pages by registering a Flask blueprint during
the `register` hook:

```python
class MyPlugin:
    def __init__(self, **kwargs):
        self.blueprint = Blueprint(
            'my_plugin',
            __name__,
            url_prefix='/plugins/my-plugin',
            template_folder='templates',
            static_folder='static',
        )
        self.blueprint.add_url_rule('/', 'index', self.index)
        self.blueprint.add_url_rule('/status', 'status', self.status)
        self.blueprint.add_url_rule('/api/data', 'api_data', self.api_data)

    def register(self, app, context=None):
        app.register_blueprint(self.blueprint)

    def index(self):
        return render_template('my_plugin_home.html')

    def status(self):
        return jsonify({'enabled': True, 'status': 'running'})
```

Directory structure for a plugin with web UI:

```
my_plugin/
├── __init__.py
├── plugin.py
├── templates/
│   └── my_plugin_home.html
└── static/
    ├── style.css
    └── script.js
```

---

## OTA Firmware Support

Plugins that manage devices can expose OTA metadata by implementing
`get_ota_devices()`:

```python
def get_ota_devices(self):
    return [
        {
            'mac_address': 'AA:BB:CC:DD:EE:FF',
            'version': '1.2.3',
            'platform': 'esp32',
            'last_gateway_mac': '11:22:33:44:55:66',
            'name': 'Temperature Sensor',
            'device_type': 0,
        }
    ]
```

The core collects these across all plugins and presents them in the OTA
management interface. The `plugin_id` is injected automatically by the core.

---

## Complete Example: Temperature Tracking Plugin

A full working example is included at `python/plugins/temperature_tracking/`.
It demonstrates:

- **Plugin manifest** — declares two device types (standard and battery
  temperature sensors), UUID matching firmware.
- **`create_plugin()` factory** — accepts `options`, `context`, `manifest`,
  and `host`.
- **Flask blueprint** — serves a dashboard (`/plugins/temperature/`), REST API
  endpoints (`/plugins/temperature/api/devices/...`), and a graphs page.
- **`on_packet_received`** — handles `0x55` temperature readings, parses a
  binary protocol with struct, stores readings with timestamps.
- **`on_plugin_hello`** — registers sensors when they come online, sends
  pending configuration.
- **`on_device_renamed`** — keeps internal sensor names in sync with the core
  pairing manager.
- **`get_ota_devices()`** — exposes all known sensors for OTA updates.
- **Host service usage** — uses `network_server`, `plugin_manager`, and
  `pairing_manager` from the host context.
- **Config push** — sends device configuration packets over UDP with delivery
  confirmation.

### Key Files

| File | Purpose |
|---|---|
| `plugins/temperature_tracking/plugin.py` | Main plugin class, packet handling, web UI |
| `plugins/temperature_tracking/storage.py` | In-memory + file-backed data storage (JSON) |
| `plugins/temperature_tracking/templates/` | Jinja2 templates for web UI |
| `plugins/temperature_tracking/static/` | Static assets (CSS, JS) |

---

## Installation

### Via the Admin UI

1. Navigate to **Admin → Plugin Manager**.
2. Paste the Git repository URL (GitHub only) and optionally a branch.
3. Click **Install Plugin** — the system clones the repo, validates the
   manifest, installs Python dependencies, copies the package folder, and
   registers the plugin.
4. **Restart the service** (or restart from the admin UI) for the plugin to
   load.

### Manual Installation

1. Add your plugin package to `python/plugins/<package_name>/`.
2. Add an entry to `python/data/plugins.json`:

   ```json
   {
     "plugins": [
       {
         "id": "my_plugin",
         "plugin_id": "your-uuid-here",
         "module": "plugins.my_plugin.plugin",
         "enabled": true
       }
     ]
   }
   ```

3. Restart the HueMix-Link service.

---

## Plugin Versioning & Updates

### Version Field

Every plugin should declare a `version` field in its `plugin.json` manifest using
[semantic versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, e.g., `"1.2.0"`).

- **MAJOR**: Breaking changes to the plugin's device protocol, API, or data storage.
- **MINOR**: New features that are backward-compatible.
- **PATCH**: Bug fixes and minor improvements.

The version is stored in the registry at `python/data/plugins.json` and displayed
in the admin plugin manager UI.

### Plugin API Version

The `plugin_api_version` field declares which version of the PluginHost API the
plugin was built against. The current core API version is **1**.

If the core's `PLUGIN_API_VERSION` does not match the plugin's declared
`plugin_api_version`, the plugin is **skipped at load time** with a warning in
the logs. This prevents plugins written for a different API from crashing at
runtime.

```json
{
  "id": "my_plugin",
  "plugin_id": "550e8400-...",
  "module": "plugins.my_plugin.plugin",
  "version": "1.2.0",
  "plugin_api_version": 1
}
```

### How Updates Work

The update mechanism uses **GitHub Releases**. When you check for updates:

1. The system fetches the latest release from the plugin's `source_repo` via the
   [GitHub Releases API](https://docs.github.com/en/rest/releases/releases).
2. It compares the release tag (parsed as semver) against the installed version.
3. If a newer version is found, an **Update** button appears in the admin UI.

### Applying an Update

When you click **Update**:

1. The system clones the repository at the latest release tag.
2. Validates the new manifest (`plugin.json`).
3. Removes the old plugin package directory.
4. Copies the new package files.
5. Re-installs Python dependencies from `requirements.txt` (if present).
6. Updates the registry entry with the new version and metadata — preserving
   `enabled` state, device bindings, and plugin options.
7. A **server restart** is required for the updated plugin code to load.

### Release Workflow for Plugin Developers

To make your plugin updatable via the admin UI:

1. Tag each release with a semver tag: `v1.0.0`, `v1.1.0`, `v2.0.0`, etc.
2. Create a **GitHub Release** from the tag. The release body is shown as
   release notes in the admin UI.
3. Update the `version` field in your `plugin.json` to match the release tag.

The system automatically picks up the latest release. If no formal releases
exist, it falls back to checking Git tags for the highest semver tag.

### What Gets Preserved During an Update

| Field | Preserved? |
|-------|-----------|
| `enabled` state | Yes |
| Plugin `options` | Yes |
| MAC → plugin device bindings | Yes |
| Pairing history entries | Yes |
| Device names and metadata | Yes |
| `version` | Updated from new manifest |
| `metadata.source_repo` | Yes |
| `metadata.updated_at` | Set to current time |

---

## Best Practices

### Plugin UUID

Use a proper UUID v4 for `plugin_id` (generated via `uuid.uuid4()`). This UUID
must be compiled into any device firmware that should be routed to your plugin.

### Packet Protocol Design

- Use a dedicated `packet_type` byte (0x50–0x7F range for plugins).
- Include a signature/hash for integrity if the packet is mutated in transit.
- Pad payloads to a consistent size (185 bytes is standard in this system).

### Error Handling

- Wrap all event handlers in try/except — an unhandled exception in one plugin
  does not crash other plugins.
- Log failures with `context.logger` or a dedicated logger.
- Validate all incoming data from the network.

### State Management

- Use `data_manager.read_json()` / `write_json()` for persistent state.
- Keep internal state thread-safe (use locks if accessing from background
  threads).
- Flush state in `stop()` / `shutdown()`.

### Background Threads

- Use `threading.Thread(daemon=True)` for background workers so they don't
  block shutdown.
- Name your threads for easier debugging.

### Flask Blueprints

- Prefix all routes with `/plugins/<your-plugin-name>/`.
- Use `template_folder` and `static_folder` relative to your plugin package.
- Register the blueprint in `register()`, not `__init__()`.

---

## Troubleshooting

### Plugin not loading

- Check `python/data/plugins.json` for correct `id`, `plugin_id`, and `module`.
- Verify the module path exists: `python/plugins/<package>/plugin.py`.
- Check the server logs for import errors or exception tracebacks.

### Packets not reaching my plugin

- Verify the device firmware sends the correct `plugin_id` UUID in HELLO
  packets.
- Verify the device MAC is bound to your plugin (check
  `plugin_device_bindings.json`).
- Check `on_packet_received` signature — it must accept the event parameters.

### Blueprint 404 errors

- Ensure the blueprint is registered in `register()`, not `__init__()`.
- The core must call `register()` before the Flask server starts serving
  requests.
- Verify the `url_prefix` and route paths.

### Changes not taking effect

- Plugin changes (install, uninstall, enable, disable) require a full service
  restart — there is no hot-reload mechanism. Use the admin panel's restart
  button or run `sudo systemctl restart huemix-link`.
