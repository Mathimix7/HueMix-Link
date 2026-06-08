"""Plugin installation and lifecycle management for admin dashboard.

Adds install session tracking so the admin UI can poll progress, and
attempts to load newly-installed plugins immediately when enabled.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from constants import FILE_PAIRING_HISTORY, FILE_PLUGINS
from services.data_manager import data_manager
from services.plugin_manager import FILE_PLUGIN_DEVICE_BINDINGS, plugin_manager
from flask import current_app

logger = logging.getLogger(__name__)


class PluginInstallService:
    """Handles install/uninstall operations for external plugin repositories."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workspace_root = Path(__file__).resolve().parents[1]
        self._plugins_dir = self._workspace_root / "plugins"
        # In-memory install sessions for progress reporting
        self._sessions: dict[str, dict[str, Any]] = {}
        # Clean up any duplicate registry entries on startup
        try:
            self._dedupe_registry()
        except Exception:
            pass

    def _dedupe_registry(self) -> None:
        """Remove duplicate plugin entries in plugins.json keeping the first seen."""
        registry = self._load_registry()
        plugins = registry.get("plugins", []) if isinstance(registry.get("plugins"), list) else []
        seen = set()
        result = []
        changed = False
        for entry in plugins:
            try:
                entry_id = str((entry or {}).get("id") or "").strip()
                entry_uuid = str((entry or {}).get("plugin_id") or entry_id).strip()
            except Exception:
                entry_id = entry_uuid = ""
            key = entry_uuid or entry_id
            if not key:
                result.append(entry)
                continue
            if key in seen:
                changed = True
                continue
            seen.add(key)
            result.append(entry)

        if changed:
            registry["plugins"] = result
            data_manager.write_json(FILE_PLUGINS, registry)

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return enriched plugin records for the admin dashboard."""
        records: list[dict[str, Any]] = []
        registered = plugin_manager.list_registered_plugins()
        loaded_ids = {
            str(runtime.definition.plugin_id)
            for runtime in getattr(plugin_manager, "_loaded_plugins", [])
            if getattr(runtime, "definition", None)
        }

        for entry in registered:
            if not isinstance(entry, dict):
                continue

            plugin_id = str(entry.get("id") or "").strip()
            plugin_uuid = str(entry.get("plugin_id") or plugin_id).strip()
            module_name = str(entry.get("module") or "").strip()
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            package_name = self._extract_package_name(module_name)
            package_exists = bool(package_name and (self._plugins_dir / package_name).exists())

            records.append(
                {
                    "id": plugin_id,
                    "plugin_id": plugin_uuid,
                    "module": module_name,
                    "enabled": bool(entry.get("enabled", False)),
                    "name": self._display_name(entry),
                    "description": str((entry.get("home_box") or {}).get("description") or metadata.get("description") or ""),
                    "source_repo": str(metadata.get("source_repo") or ""),
                    "installed_at": str(metadata.get("installed_at") or ""),
                    "package_name": package_name,
                    "package_exists": package_exists,
                    "loaded": plugin_uuid in loaded_ids or plugin_id in loaded_ids,
                    "has_requirements": bool(metadata.get("has_requirements", False)),
                }
            )

        records.sort(key=lambda x: x.get("name", "").lower())
        return records

    def install_from_repo(self, repo_url: str, branch: str | None = None) -> dict[str, Any]:
        """Start an install session for a Git repository and return an install id.

        The actual work runs in a background thread; callers should poll
        `get_install_status(install_id)` to observe progress and final result.
        """
        repo_url = (repo_url or "").strip()
        branch = (branch or "").strip() or None

        if not self._is_supported_repo(repo_url):
            raise ValueError("Only GitHub repository URLs are supported (https://github.com/... or git@github.com:...)")

        install_id = uuid.uuid4().hex
        session = {
            "id": install_id,
            "repo_url": repo_url,
            "branch": branch,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "step": "queued",
            "logs": [],
            "success": None,
            "error": None,
            "result": None,
        }
        self._sessions[install_id] = session

        # Capture Flask app if available so plugins can register blueprints
        app_obj = None
        try:
            app_obj = current_app._get_current_object()
        except Exception:
            app_obj = None

        thread = threading.Thread(target=self._install_worker, args=(install_id, repo_url, branch, app_obj), daemon=True)
        thread.start()

        return {"success": True, "install_id": install_id, "message": "Install started"}

    def get_install_status(self, install_id: str) -> dict[str, Any]:
        """Return current install session status and logs."""
        sess = self._sessions.get(str(install_id))
        if not sess:
            raise ValueError("Install session not found")
        # Return a shallow copy to avoid callers mutating internal state
        return dict(sess)

    def _append_session_log(self, install_id: str, text: str) -> None:
        sess = self._sessions.get(str(install_id))
        if not sess:
            return
        entry = f"[{datetime.now(timezone.utc).isoformat()}] {text}"
        sess["logs"].append(entry)

    def _update_session_step(self, install_id: str, step: str) -> None:
        sess = self._sessions.get(str(install_id))
        if not sess:
            return
        sess["step"] = step

    def _install_worker(self, install_id: str, repo_url: str, branch: str | None, app_obj=None) -> None:
        """Background worker performing the install and reporting progress."""
        try:
            self._update_session_step(install_id, "cloning")
            self._append_session_log(install_id, f"Cloning {repo_url} (branch={branch or 'default'})")

            with tempfile.TemporaryDirectory(prefix="huemix_plugin_") as tmp:
                checkout_dir = Path(tmp) / "repo"
                clone_cmd = ["git", "clone", "--depth", "1"]
                if branch:
                    clone_cmd.extend(["--branch", branch])
                clone_cmd.extend([repo_url, str(checkout_dir)])
                subprocess.run(clone_cmd, cwd=str(self._workspace_root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900)
                self._append_session_log(install_id, "Repository cloned")

                self._update_session_step(install_id, "loading-manifest")
                manifest, manifest_source = self._load_manifest(checkout_dir)
                self._validate_manifest(manifest)
                self._append_session_log(install_id, f"Loaded manifest from {manifest_source}")

                plugin_id = str(manifest.get("id") or "").strip()
                plugin_uuid = str(manifest.get("plugin_id") or plugin_id).strip()
                module_name = str(manifest.get("module") or "").strip()
                package_name = self._extract_package_name(module_name)
                if not package_name:
                    raise RuntimeError("Manifest module must be in format plugins.<package>.plugin")

                package_src = self._resolve_package_source(checkout_dir, package_name)
                package_dest = self._plugins_dir / package_name

                existing = self._find_registry_entry(plugin_id, plugin_uuid)
                if existing:
                    raise RuntimeError(f"Plugin '{plugin_id}' is already installed. Uninstall it before reinstalling.")
                if package_dest.exists():
                    raise RuntimeError(f"Plugin package directory already exists: {package_dest.name}")

                requirements_path = checkout_dir / "requirements.txt"
                if requirements_path.exists():
                    self._update_session_step(install_id, "installing-requirements")
                    self._append_session_log(install_id, f"Installing requirements from {requirements_path.name}")
                    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements_path)], cwd=str(checkout_dir), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900)
                    self._append_session_log(install_id, "Requirements installed")

                self._update_session_step(install_id, "copying-files")
                shutil.copytree(package_src, package_dest)
                self._append_session_log(install_id, f"Copied package to plugins/{package_name}")

                metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
                metadata = dict(metadata)
                metadata.update(
                    {
                        "source_repo": repo_url,
                        "source_branch": branch or "default",
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                        "manifest_source": manifest_source,
                        "has_requirements": requirements_path.exists(),
                    }
                )
                manifest["metadata"] = metadata
                manifest["enabled"] = bool(manifest.get("enabled", True))

                self._update_session_step(install_id, "writing-registry")
                self._append_session_log(install_id, "Updating plugins registry")
                self._append_manifest(manifest)


                self._append_session_log(install_id, "Plugin registered. Restart required to load into runtime.")
                restart_required = True

                result = {
                    "success": True,
                    "plugin": {
                        "id": plugin_id,
                        "plugin_id": plugin_uuid,
                        "module": module_name,
                        "package_name": package_name,
                    },
                    "message": "Plugin installed",
                    "restart_required": restart_required,
                }

                self._update_session_step(install_id, "completed")
                sess = self._sessions.get(install_id)
                if sess is not None:
                    sess["success"] = True
                    sess["result"] = result
                    sess["ended_at"] = datetime.now(timezone.utc).isoformat()
                    self._append_session_log(install_id, "Install completed")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
            self._append_session_log(install_id, f"Command failed: {err}")
            sess = self._sessions.get(install_id)
            if sess is not None:
                sess["success"] = False
                sess["error"] = err
                sess["ended_at"] = datetime.now(timezone.utc).isoformat()
                sess["step"] = "failed"
        except Exception as exc:
            self._append_session_log(install_id, f"Install error: {exc}")
            sess = self._sessions.get(install_id)
            if sess is not None:
                sess["success"] = False
                sess["error"] = str(exc)
                sess["ended_at"] = datetime.now(timezone.utc).isoformat()
                sess["step"] = "failed"

    def uninstall_plugin(self, plugin_identifier: str) -> dict[str, Any]:
        """Uninstall a plugin by id or plugin_id from registry and filesystem."""
        with self._lock:
            plugin_identifier = (plugin_identifier or "").strip()
            if not plugin_identifier:
                raise ValueError("plugin_identifier is required")

            registry = self._load_registry()
            plugins = registry.get("plugins", []) if isinstance(registry.get("plugins"), list) else []

            target: dict[str, Any] | None = None
            kept: list[dict[str, Any]] = []
            for entry in plugins:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("id") or "").strip()
                entry_uuid = str(entry.get("plugin_id") or entry_id).strip()
                if entry_id == plugin_identifier or entry_uuid == plugin_identifier:
                    target = entry
                else:
                    kept.append(entry)

            if target is None:
                raise ValueError("Plugin not found in plugins.json")

            registry["plugins"] = kept
            data_manager.write_json(FILE_PLUGINS, registry)

            plugin_id = str(target.get("id") or "").strip()
            plugin_uuid = str(target.get("plugin_id") or plugin_id).strip()
            module_name = str(target.get("module") or "").strip()
            package_name = self._extract_package_name(module_name)

            folder_removed = False
            if package_name:
                package_path = (self._plugins_dir / package_name).resolve()
                if package_path.exists() and str(package_path).startswith(str(self._plugins_dir.resolve())):
                    shutil.rmtree(package_path, ignore_errors=True)
                    folder_removed = True

            bindings_removed = self._remove_plugin_bindings(plugin_id=plugin_id, plugin_uuid=plugin_uuid)
            pairing_removed = self._remove_plugin_pairing_history(plugin_id=plugin_id, plugin_uuid=plugin_uuid)

            restart_required = True
            message = "Plugin uninstalled. Restart required to fully unload runtime."

            return {
                "success": True,
                "message": message,
                "plugin": {
                    "id": plugin_id,
                    "plugin_id": plugin_uuid,
                    "module": module_name,
                    "package_name": package_name,
                },
                "folder_removed": folder_removed,
                "bindings_removed": bindings_removed,
                "pairing_entries_removed": pairing_removed,
                "restart_required": restart_required,
            }

    def set_enabled(self, plugin_identifier: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable a plugin in registry."""
        with self._lock:
            plugin_identifier = (plugin_identifier or "").strip()
            if not plugin_identifier:
                raise ValueError("plugin_identifier is required")

            registry = self._load_registry()
            plugins = registry.get("plugins", []) if isinstance(registry.get("plugins"), list) else []
            found = None
            for entry in plugins:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("id") or "").strip()
                entry_uuid = str(entry.get("plugin_id") or entry_id).strip()
                if entry_id == plugin_identifier or entry_uuid == plugin_identifier:
                    entry["enabled"] = bool(enabled)
                    found = entry
                    break

            if found is None:
                raise ValueError("Plugin not found")
            data_manager.write_json(FILE_PLUGINS, registry)

            # Changing enabled state requires a restart to apply safely
            restart_required = True
            message = f"Plugin {'enabled' if enabled else 'disabled'}. Restart required to apply changes."

            return {
                "success": True,
                "message": message,
                "restart_required": restart_required,
            }

    def _load_registry(self) -> dict[str, Any]:
        registry = data_manager.read_json(FILE_PLUGINS, default={"schema_version": 1, "plugins": []})
        if not isinstance(registry, dict):
            registry = {"schema_version": 1, "plugins": []}
        if not isinstance(registry.get("plugins"), list):
            registry["plugins"] = []
        registry["schema_version"] = int(registry.get("schema_version", 1) or 1)
        return registry

    def _append_manifest(self, manifest: dict[str, Any]) -> None:
        registry = self._load_registry()
        plugins = registry.setdefault("plugins", [])

        # Normalize keys for comparison
        new_id = str(manifest.get("id") or "").strip()
        new_uuid = str(manifest.get("plugin_id") or new_id).strip()

        # Remove any existing entries matching id or plugin_id to avoid duplicates
        filtered = []
        for entry in plugins:
            try:
                entry_id = str((entry or {}).get("id") or "").strip()
                entry_uuid = str((entry or {}).get("plugin_id") or entry_id).strip()
            except Exception:
                entry_id = entry_uuid = ""
            if entry_id == new_id or entry_uuid == new_uuid:
                continue
            filtered.append(entry)

        filtered.append(manifest)
        registry["plugins"] = filtered
        data_manager.write_json(FILE_PLUGINS, registry)

    def _find_registry_entry(self, plugin_id: str, plugin_uuid: str) -> dict[str, Any] | None:
        for entry in plugin_manager.list_registered_plugins():
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "").strip()
            entry_uuid = str(entry.get("plugin_id") or entry_id).strip()
            if entry_id == plugin_id or entry_uuid == plugin_uuid:
                return entry
        return None

    def _load_manifest(self, checkout_dir: Path) -> tuple[dict[str, Any], str]:
        candidates = [
            checkout_dir / "plugin.json",
            checkout_dir / "plugin-manifest.json",
            checkout_dir / "plugins.json",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            with candidate.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if candidate.name == "plugins.json":
                if isinstance(payload, dict) and isinstance(payload.get("plugins"), list) and payload["plugins"]:
                    first = payload["plugins"][0]
                    if isinstance(first, dict):
                        return first, candidate.name
                raise ValueError("plugins.json must contain a non-empty 'plugins' array")
            if isinstance(payload, dict):
                return payload, candidate.name
            raise ValueError(f"{candidate.name} must contain a JSON object")
        raise ValueError("No plugin manifest found. Expected plugin.json, plugin-manifest.json, or plugins.json")

    def _validate_manifest(self, manifest: dict[str, Any]) -> None:
        plugin_id = str(manifest.get("id") or "").strip()
        plugin_uuid = str(manifest.get("plugin_id") or plugin_id).strip()
        module_name = str(manifest.get("module") or "").strip()

        if not plugin_id:
            raise ValueError("Manifest must contain 'id'")
        if not plugin_uuid:
            raise ValueError("Manifest must contain 'plugin_id' (or id)")
        if not module_name:
            raise ValueError("Manifest must contain 'module'")

        package_name = self._extract_package_name(module_name)
        if not package_name:
            raise ValueError("Manifest module must be in format plugins.<package>.plugin")

    def _resolve_package_source(self, checkout_dir: Path, package_name: str) -> Path:
        candidates = [
            checkout_dir / package_name,
            checkout_dir / "plugins" / package_name,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        raise ValueError(
            f"Could not find plugin package folder '{package_name}' in repo root or repo/plugins/"
        )

    def _extract_package_name(self, module_name: str) -> str:
        parts = [p for p in module_name.split(".") if p]
        if len(parts) < 3:
            return ""
        if parts[0] != "plugins":
            return ""
        return parts[1]

    def _display_name(self, entry: dict[str, Any]) -> str:
        home_box = entry.get("home_box") if isinstance(entry.get("home_box"), dict) else {}
        if home_box.get("name"):
            return str(home_box.get("name"))
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        if metadata.get("name"):
            return str(metadata.get("name"))
        return str(entry.get("id") or entry.get("plugin_id") or "Plugin")

    def _is_supported_repo(self, repo_url: str) -> bool:
        lowered = repo_url.lower()
        return lowered.startswith("https://github.com/") or lowered.startswith("http://github.com/") or lowered.startswith("git@github.com:")

    def _run_command(self, command: list[str], cwd: Path) -> None:
        try:
            subprocess.run(
                command,
                cwd=str(cwd),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            raise RuntimeError(detail) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Command not found: {command[0]}") from exc

    def _remove_plugin_bindings(self, plugin_id: str, plugin_uuid: str) -> int:
        bindings = data_manager.read_json(FILE_PLUGIN_DEVICE_BINDINGS, default={})
        if not isinstance(bindings, dict):
            return 0

        removed = 0
        updated: dict[str, str] = {}
        for mac, value in bindings.items():
            bound = str(value or "").strip()
            if bound == plugin_id or bound == plugin_uuid:
                removed += 1
                continue
            updated[mac] = value

        if removed:
            data_manager.write_json(FILE_PLUGIN_DEVICE_BINDINGS, updated)
        return removed

    def _remove_plugin_pairing_history(self, plugin_id: str, plugin_uuid: str) -> int:
        history = data_manager.read_json(FILE_PAIRING_HISTORY, default=[])
        if not isinstance(history, list):
            return 0

        filtered: list[dict[str, Any]] = []
        removed = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            raw_type = str(item.get("type") or "")
            if ":" in raw_type:
                _, suffix = raw_type.split(":", 1)
                if suffix == plugin_uuid or suffix == plugin_id:
                    removed += 1
                    continue
            filtered.append(item)

        if removed:
            data_manager.write_json(FILE_PAIRING_HISTORY, filtered)
        return removed


plugin_install_service = PluginInstallService()
