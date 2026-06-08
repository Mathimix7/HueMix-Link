"""Manifest-driven plugin manager for HueMix-Link extensions."""
from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from constants import FILE_PLUGINS, FILE_PLUGIN_DEVICE_BINDINGS
from services.data_manager import data_manager

logger = logging.getLogger(__name__)

PLUGIN_SCHEMA_VERSION = 1

@dataclass
class PluginDevice:
    """Device type declared by a plugin."""
    device_type: int
    packet_types: list[int]
    name: str
    icon: str
    auto_pair: bool = True
    pairing_rules: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginDevice":
        return cls(
            device_type=int(data.get("device_type", 0)),
            name=str(data.get("name", "Unknown")),
            icon=str(data.get("icon", "❓")),
            type=str(data.get("type", "Unknown")),
        )


@dataclass
class PluginOTA:
    """OTA (firmware) entry declared by a plugin."""
    targets: list[int]
    url: str
    version: str
    checksum: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginOTA":
        return cls(
            targets=[int(x) for x in (data.get("targets") or [])],
            url=str(data.get("url", "")),
            version=str(data.get("version", "0.0.0")),
            checksum=str(data.get("checksum", "")),
        )


@dataclass
class PluginManifest:
    """Parsed plugin manifest with device and OTA declarations."""
    plugin_id: str  # Unique UUID or string
    id: str  # Display id (from 'id' field in manifest)
    module: str
    enabled: bool
    devices: list[PluginDevice] = field(default_factory=list)
    ota: list[PluginOTA] = field(default_factory=list)
    home_box: list[dict[str, Any]] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        plugin_id = str(data.get("plugin_id", "")).strip()
        if not plugin_id:
            # Fallback: generate from id field if plugin_id not present
            plugin_id = str(data.get("id", "")).strip()
        
        devices = [PluginDevice.from_dict(d) for d in (data.get("devices") or [])]
        ota = [PluginOTA.from_dict(o) for o in (data.get("ota") or [])]
        home_box = data.get("home_box") if isinstance(data.get("home_box"), (dict, list)) else None
        
        return cls(
            plugin_id=plugin_id,
            id=str(data.get("id", "unknown")).strip(),
            module=str(data.get("module", "")).strip(),
            enabled=bool(data.get("enabled", False)),
            devices=devices,
            ota=ota,
            home_box=home_box,
        )


@dataclass
class PluginDefinition:
    """Normalized plugin registry entry."""

    plugin_id: str
    module: str
    enabled: bool = False
    kind: str = "generic"
    capabilities: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginDefinition":
        plugin_id = str(data.get("plugin_id") or data.get("id") or "").strip()
        module = str(data.get("module") or data.get("import") or "").strip()
        if not plugin_id or not module:
            raise ValueError("Plugin entries must define both id and module.")

        capabilities = data.get("capabilities") or []
        if not isinstance(capabilities, list):
            capabilities = []

        options = data.get("options") or {}
        if not isinstance(options, dict):
            options = {}

        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        return cls(
            plugin_id=plugin_id,
            module=module,
            enabled=bool(data.get("enabled", False)),
            kind=str(data.get("kind") or "generic").strip() or "generic",
            capabilities=[str(item).strip() for item in capabilities if str(item).strip()],
            options=options,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.plugin_id,
            "module": self.module,
            "enabled": self.enabled,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "options": dict(self.options),
            "metadata": dict(self.metadata),
        }


@dataclass
class PluginRuntime:
    """Runtime state for a loaded plugin."""

    definition: PluginDefinition
    instance: Any
    module_name: str
    context: "PluginHost"


@dataclass
class PluginHost:
    """Generic host API exposed to plugins."""

    app: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    event_handlers: dict[str, list[Callable[[str, Any, "PluginHost"], None]]] = field(default_factory=dict)

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def require(self, name: str) -> Any:
        if name not in self.services:
            raise KeyError(f"Host service '{name}' is not available.")
        return self.services[name]

    def has(self, name: str) -> bool:
        return name in self.services

    def register_service(self, name: str, value: Any) -> None:
        self.services[name] = value

    def subscribe(self, event_name: str, handler: Callable[[str, Any, "PluginHost"], None]) -> None:
        self.event_handlers.setdefault(event_name, []).append(handler)

    def publish(self, event_name: str, payload: Any = None) -> None:
        for handler in list(self.event_handlers.get(event_name, [])):
            try:
                handler(event_name, payload, self)
            except Exception:
                logger.exception("Plugin event handler failed for %s", event_name)


class PluginManager:
    """Loads optional plugins from a manifest registry and exposes host services."""

    def __init__(self):
        self._loaded_plugins: list[PluginRuntime] = []
        self._registry_cache: dict[str, Any] | None = None
        # Host instance passed into load_enabled_plugins; used as default for event dispatch
        self._default_host: PluginHost | None = None
        # Device/packet claim tracking: device_type (int) -> primary_plugin_id
        self._device_claims: dict[int, str] = {}
        # Packet subscribers: packet_type (int) -> list of plugin_ids
        self._packet_subscribers: dict[int, list[str]] = {}
        # MAC -> plugin_id binding (persisted for pairing)
        self._mac_bindings: dict[str, str] = {}
        self._load_mac_bindings()

    def _default_registry(self) -> dict[str, Any]:
        return {
            "schema_version": PLUGIN_SCHEMA_VERSION,
            "plugins": [],
        }

    def _normalize_registry(self, registry: Any) -> dict[str, Any]:
        if isinstance(registry, list):
            return {
                "schema_version": PLUGIN_SCHEMA_VERSION,
                "plugins": registry,
            }

        if not isinstance(registry, dict):
            return self._default_registry()

        normalized = dict(registry)
        normalized["schema_version"] = int(normalized.get("schema_version", PLUGIN_SCHEMA_VERSION) or PLUGIN_SCHEMA_VERSION)
        plugins = normalized.get("plugins", [])
        if not isinstance(plugins, list):
            plugins = []
        normalized["plugins"] = plugins
        return normalized

    def _load_registry(self) -> dict[str, Any]:
        registry = data_manager.read_json(FILE_PLUGINS, default=self._default_registry())
        return self._normalize_registry(registry)

    def _save_registry(self, registry: dict[str, Any]) -> None:
        data_manager.write_json(FILE_PLUGINS, self._normalize_registry(registry))

    def ensure_registry(self) -> dict[str, Any]:
        """Ensure the plugins registry file exists and return its current content."""
        registry = self._load_registry()
        filepath = data_manager._get_filepath(FILE_PLUGINS)
        if not filepath.exists():
            self._save_registry(registry)
        self._registry_cache = registry
        return registry

    def list_registered_plugins(self) -> list[dict[str, Any]]:
        """Return plugin entries as raw manifest dictionaries."""
        registry = self.ensure_registry()
        plugins = registry.get("plugins", [])
        return plugins if isinstance(plugins, list) else []

    def get_definition(self, plugin_id: str) -> Optional[PluginDefinition]:
        """Find a plugin definition by id."""
        for entry in self.list_registered_plugins():
            try:
                definition = PluginDefinition.from_dict(entry)
            except Exception:
                continue
            if definition.plugin_id == plugin_id:
                return definition
        return None

    def get_plugin_by_uuid(self, plugin_uuid: str) -> Optional[dict[str, Any]]:
        """Find a plugin by UUID (for unpaired plugin HELLO handling).
        
        Args:
            plugin_uuid: Plugin UUID as hex string (from HELLO payload)
            
        Returns:
            Raw manifest entry dict if found, None otherwise
        """
        for entry in self.list_registered_plugins():
            if not entry.get('enabled'):
                continue
            manifest_uuid = entry.get('plugin_id', '')
            # Match UUID as-is (16 bytes hex = 32 chars), normalize by removing dashes
            if manifest_uuid.replace('-', '').lower() == plugin_uuid.lower():
                return entry
        return None

    def upsert_plugin(self, definition: PluginDefinition) -> PluginDefinition:
        """Add or update a plugin definition in the registry."""
        registry = self.ensure_registry()
        plugins = registry.setdefault("plugins", [])
        replaced = False
        for index, entry in enumerate(plugins):
            if isinstance(entry, dict) and str(entry.get("id") or entry.get("plugin_id") or "").strip() == definition.plugin_id:
                plugins[index] = definition.to_dict()
                replaced = True
                break

        if not replaced:
            plugins.append(definition.to_dict())

        self._save_registry(registry)
        self._registry_cache = registry
        return definition

    def remove_plugin(self, plugin_id: str) -> bool:
        """Remove a plugin from the registry by id."""
        registry = self.ensure_registry()
        plugins = registry.get("plugins", [])
        if not isinstance(plugins, list):
            return False

        original_length = len(plugins)
        registry["plugins"] = [entry for entry in plugins if str((entry or {}).get("id") or (entry or {}).get("plugin_id") or "").strip() != plugin_id]
        changed = len(registry["plugins"]) != original_length
        if changed:
            self._save_registry(registry)
            self._registry_cache = registry
        return changed

    def _load_mac_bindings(self) -> None:
        """Load persisted MAC -> plugin_id bindings."""
        try:
            bindings = data_manager.read_json(FILE_PLUGIN_DEVICE_BINDINGS, default={})
            self._mac_bindings = dict(bindings) if isinstance(bindings, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to load MAC bindings: {e}")
            self._mac_bindings = {}

    def _save_mac_bindings(self) -> None:
        """Persist MAC -> plugin_id bindings."""
        try:
            data_manager.write_json(FILE_PLUGIN_DEVICE_BINDINGS, self._mac_bindings)
        except Exception as e:
            logger.error(f"Failed to save MAC bindings: {e}")

    def bind_mac_to_plugin(self, device_mac: str, plugin_id: str) -> None:
        """Bind a device MAC to a plugin (called on successful pairing)."""
        mac_upper = device_mac.upper()
        self._mac_bindings[mac_upper] = plugin_id
        self._save_mac_bindings()
        logger.info(f"Bound device {mac_upper} to plugin {plugin_id}")

    def get_plugin_for_mac(self, device_mac: str) -> Optional[str]:
        """Get plugin_id for a device MAC, or None if not bound."""
        return self._mac_bindings.get(device_mac.upper())

    def collect_plugin_ota_devices(self) -> list[dict]:
        """Collect OTA device info from all loaded plugins that expose get_ota_devices().
        
        Each plugin's get_ota_devices() should return a list of dicts with:
            mac_address, version, platform, last_gateway_mac, name, device_type
        
        Returns:
            List of device dicts with plugin_id injected
        """
        devices = []
        for runtime in self._loaded_plugins:
            instance = runtime.instance
            method = getattr(instance, 'get_ota_devices', None)
            if callable(method):
                try:
                    result = method()
                    if isinstance(result, list):
                        plugin_id = runtime.definition.plugin_id
                        for dev in result:
                            dev['plugin_id'] = plugin_id
                        devices.extend(result)
                except Exception:
                    logger.exception(f"Error collecting OTA devices from {runtime.definition.plugin_id}")
        return devices

    def get_device_claims(self) -> dict[int, str]:
        """Return device_type -> plugin_id mapping."""
        return dict(self._device_claims)

    def get_packet_subscribers(self, packet_type: int) -> list[str]:
        """Return list of plugin_ids that handle a packet_type."""
        return list(self._packet_subscribers.get(packet_type, []))

    def create_host(self, **services: Any) -> PluginHost:
        """Create a generic host object passed into plugins."""
        host_services = dict(services)
        app = host_services.pop("app", None)
        return PluginHost(app=app, services=host_services)

    def load_enabled_plugins(self, host: PluginHost | None = None) -> list[PluginRuntime]:
        """Import and initialize enabled plugins."""
        host = host or self.create_host()
        # Remember the provided host so dispatch_event can reuse it when callers omit a host
        self._default_host = host
        self._loaded_plugins = []

        for raw_entry in self.list_registered_plugins():
            try:
                definition = PluginDefinition.from_dict(raw_entry)
            except Exception as exc:
                logger.warning("Skipping invalid plugin entry: %s", exc)
                continue

            if not definition.enabled:
                continue

            try:
                module = importlib.import_module(definition.module)
            except Exception:
                logger.exception("Failed to import plugin %s from %s.", definition.plugin_id, definition.module)
                continue

            try:
                instance = self._create_plugin_instance(module, definition, host)
                self._call_named_hook_if_present(instance, ["configure", "setup"], host, definition)
                self._call_named_hook_if_present(instance, ["register"], host, definition, prefer_app=True)
                self._call_named_hook_if_present(instance, ["start", "run", "initialize"], host, definition)

                runtime = PluginRuntime(
                    definition=definition,
                    instance=instance,
                    module_name=definition.module,
                    context=host,
                )
                self._loaded_plugins.append(runtime)
                logger.info("Loaded plugin: %s (%s)", definition.plugin_id, definition.module)
            except Exception:
                logger.exception("Plugin %s failed during initialization.", definition.plugin_id)

        return list(self._loaded_plugins)

    def stop_loaded_plugins(self, host: PluginHost | None = None) -> None:
        """Stop loaded plugins in reverse order."""
        host = host or self.create_host()
        for runtime in reversed(self._loaded_plugins):
            instance = runtime.instance
            self._call_named_hook_if_present(instance, ["stop", "shutdown", "teardown"], host, runtime.definition, swallow_type_error=True)
        self._loaded_plugins = []

    def dispatch_event(self, event_name: str, payload: Any = None, host: PluginHost | None = None) -> None:
        """Broadcast an event to loaded plugins that implement on_event-like hooks."""
        # Use provided host, otherwise fall back to the host supplied during plugin load
        host = host or self._default_host or self.create_host()
        for runtime in list(self._loaded_plugins):
            instance = runtime.instance
            for hook_name in ("on_event", "handle_event", f"on_{event_name}"):
                hook = getattr(instance, hook_name, None)
                if callable(hook):
                    try:
                        self._invoke_event_hook(hook, event_name, payload, host)
                    except Exception:
                        logger.exception("Plugin %s failed while handling event %s", runtime.definition.plugin_id, event_name)
                    break

    def _create_plugin_instance(self, module: Any, definition: PluginDefinition, host: PluginHost) -> Any:
        for factory_name in ("create_plugin", "Plugin"):
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                continue
            try:
                return self._invoke_with_candidates(
                    factory,
                    [
                        ((), {"options": definition.options, "context": host, "host": host, "manifest": definition}),
                        ((), {"options": definition.options, "context": host}),
                        ((), {"options": definition.options}),
                        ((), {"context": host}),
                        ((), {}),
                    ],
                )
            except TypeError:
                continue

        raise RuntimeError(f"Plugin module {module.__name__} does not expose create_plugin() or Plugin.")

    def _call_named_hook_if_present(
        self,
        instance: Any,
        method_names: list[str],
        host: PluginHost,
        definition: PluginDefinition,
        *,
        prefer_app: bool = False,
        swallow_type_error: bool = False,
    ) -> None:
        for method_name in method_names:
            hook = getattr(instance, method_name, None)
            if callable(hook):
                try:
                    self._invoke_named_hook(hook, host, definition, prefer_app=prefer_app, instance=instance)
                except TypeError:
                    if not swallow_type_error:
                        raise
                return

    def _invoke_event_hook(self, hook: Callable[..., Any], event_name: str, payload: Any, host: PluginHost) -> Any:
        definition = PluginDefinition(plugin_id=event_name, module="event", enabled=True)
        return self._invoke_with_candidates(
            hook,
            [
                ((), {"event_name": event_name, "payload": payload, "host": host, "context": host, "manifest": definition}),
                ((), {"event_name": event_name, "payload": payload, "host": host}),
                ((), {"payload": payload, "host": host}),
                ((), {"host": host}),
                ((event_name, payload, host), {}),
                ((event_name, payload), {}),
                ((payload, host), {}),
                ((host,), {}),
                ((), {}),
            ],
        )

    def _invoke_named_hook(
        self,
        hook: Callable[..., Any],
        host: PluginHost,
        definition: PluginDefinition,
        *,
        prefer_app: bool,
        instance: Any = None,
    ) -> Any:
        candidates = [
            ((), {"app": host.app, "context": host, "host": host, "manifest": definition, "definition": definition, "plugin": instance, "options": definition.options}),
            ((), {"app": host.app, "context": host, "host": host, "manifest": definition, "options": definition.options}),
            ((), {"app": host.app, "context": host, "host": host}),
            ((), {"context": host, "host": host}),
            ((), {"app": host.app} if prefer_app else {}),
            ((host.app, host) if prefer_app and host.app is not None else (host,), {}),
            ((host.app,) if prefer_app and host.app is not None else tuple(), {}),
            ((), {}),
        ]
        return self._invoke_with_candidates(hook, candidates)

    def _invoke_with_candidates(self, hook: Callable[..., Any], candidates: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
        try:
            signature = inspect.signature(hook)
        except (TypeError, ValueError):
            signature = None

        for args, kwargs in candidates:
            if signature is not None:
                try:
                    signature.bind_partial(*args, **kwargs)
                except TypeError:
                    continue
            return hook(*args, **kwargs)

        raise TypeError(f"No supported argument combination found for {hook!r}")


plugin_manager = PluginManager()
