"""Small, dependency-free persistence helpers used by the control plane.

The bot is normally run on an ephemeral CI worker, so in-memory state is not a
reliable source of truth across reconnects.  These stores deliberately keep only
operator configuration and channel metadata; credentials and transient webhook
messages are never written here.  Writes use a same-directory temporary file
and ``os.replace`` so a process interruption cannot leave half a JSON document.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable


class JsonStateStore:
    """Thread-safe atomic JSON document store.

    A missing, malformed, or non-object file is treated as the supplied default
    rather than being allowed to crash the bot during startup.  The caller can
    decide whether the path is durable (for example a mounted volume) or only
    process-local; the file format remains the same in both cases.
    """

    def __init__(self, path: str | os.PathLike[str], default: dict[str, Any] | None = None):
        self.path = Path(path).expanduser()
        self.default = copy.deepcopy(default or {})
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
                value = json.loads(raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                return copy.deepcopy(self.default)
            return value if isinstance(value, dict) else copy.deepcopy(self.default)

    def save(self, value: dict[str, Any]) -> bool:
        if not isinstance(value, dict):
            return False
        with self._lock:
            temp_name: str | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
                temp_name = None
                return True
            except (OSError, TypeError, ValueError):
                return False
            finally:
                if temp_name:
                    try:
                        os.unlink(temp_name)
                    except OSError as _ignored_exc:
                        print(f"[STATE] save: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> tuple[bool, Any]:
        """Load, mutate, and atomically save one document under one lock."""
        with self._lock:
            value = self.load()
            try:
                result = mutator(value)
            except Exception as exc:  # callers receive the failure, file is untouched
                return False, exc
            if not self.save(value):
                return False, "Could not atomically persist JSON state."
            return True, result


def _clean_text(value: Any, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:limit]


def _numeric_id(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw.isdigit() else ""


def _channel_record(channel: dict[str, Any], guild_id: str, guild_name: str, now: float) -> dict[str, Any] | None:
    if not isinstance(channel, dict):
        return None
    cid = _numeric_id(channel.get("id"))
    if not cid:
        return None
    # Text and announcement channels are the only safe advertising targets.
    try:
        channel_type = int(channel.get("type", 0))
    except (TypeError, ValueError):
        channel_type = 0
    if channel_type not in (0, 5):
        return None
    try:
        slowmode = max(0, int(channel.get("rate_limit_per_user", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        slowmode = 0
    return {
        "id": cid,
        "name": _clean_text(channel.get("name"), 80, cid),
        "guild_id": guild_id,
        "guild_name": _clean_text(guild_name, 120, guild_id),
        "type": channel_type,
        "slowmode": slowmode,
        "last_seen": now,
    }


class ChannelRegistryStore:
    """Persistent per-alt guild/channel catalogue and target reconciliation.

    ``live_servers`` is an iterable of dictionaries in this form::

        {"id": "guild-id", "name": "Server", "channels": [{...}]}

    The complete live catalogue is retained in ``servers``/``channels``.  The
    smaller ``targets`` list is what the sender should post to.  On a first
    connection callers may provide explicit target IDs; if they do not, all
    eligible text channels are selected.  On later scans, newly created IDs are
    selected only when their channel name matches an existing target name.  That
    prevents an unrelated #general channel from silently becoming an ad target
    while still recovering a deleted-and-recreated trading channel.
    """

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]):
        self.store = JsonStateStore(path, {"version": self.VERSION, "alts": {}})
        self._lock = threading.RLock()

    @staticmethod
    def _empty_alt(alt_id: int) -> dict[str, Any]:
        return {
            "alt_id": int(alt_id),
            "first_connected_at": 0.0,
            "updated_at": 0.0,
            "servers": {},
            "channels": {},
            "targets": [],
            "target_names": [],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = self.store.load()
            value.setdefault("version", self.VERSION)
            value.setdefault("alts", {})
            return value

    def snapshot_for_alt(self, alt_id: int) -> dict[str, Any]:
        with self._lock:
            data = self.snapshot()
            raw = data.get("alts", {}).get(str(int(alt_id)), self._empty_alt(alt_id))
            return copy.deepcopy(raw) if isinstance(raw, dict) else self._empty_alt(alt_id)

    def restore_alt_snapshot(self, alt_id: int, snapshot: dict[str, Any]) -> bool:
        """Merge one remote per-alt snapshot into the local registry."""
        if not isinstance(snapshot, dict):
            return False
        with self._lock:
            data = self.store.load()
            data.setdefault("version", self.VERSION)
            data.setdefault("alts", {})
            entry = copy.deepcopy(snapshot)
            entry["alt_id"] = int(alt_id)
            if not isinstance(entry.get("servers"), dict):
                entry["servers"] = {}
            if not isinstance(entry.get("channels"), dict):
                entry["channels"] = {}
            if not isinstance(entry.get("targets"), list):
                entry["targets"] = []
            if not isinstance(entry.get("target_names"), list):
                entry["target_names"] = []
            entry["targets"] = [cid for cid in (_numeric_id(item) for item in entry["targets"]) if cid][:100]
            entry["updated_at"] = float(entry.get("updated_at") or time.time())
            data["alts"][str(int(alt_id))] = entry
            return self.store.save(data)

    def set_targets(self, alt_id: int, channel_ids: Iterable[Any], names: dict[str, str] | None = None) -> tuple[bool, str]:
        """Persist an explicit target replacement without touching the catalogue."""
        clean_ids = list(dict.fromkeys(cid for cid in (_numeric_id(x) for x in channel_ids) if cid))[:100]
        now = time.time()

        def mutate(data: dict[str, Any]) -> str:
            data.setdefault("version", self.VERSION)
            data.setdefault("alts", {})
            entry = data["alts"].setdefault(str(int(alt_id)), self._empty_alt(alt_id))
            if not isinstance(entry, dict):
                entry = self._empty_alt(alt_id)
                data["alts"][str(int(alt_id))] = entry
            catalogue = entry.setdefault("channels", {})
            target_names = []
            for cid in clean_ids:
                name = _clean_text((names or {}).get(cid), 80, "")
                prior = catalogue.get(cid) if isinstance(catalogue.get(cid), dict) else {}
                if name:
                    prior["name"] = name
                prior.setdefault("id", cid)
                catalogue[cid] = prior
                if prior.get("name"):
                    target_names.append(_clean_text(prior["name"], 80))
            entry["targets"] = clean_ids
            entry["target_names"] = list(dict.fromkeys(target_names))
            entry["updated_at"] = now
            return ",".join(clean_ids)

        with self._lock:
            ok, result = self.store.update(mutate)
        return ok, str(result)

    def reconcile(
        self,
        alt_id: int,
        live_servers: Iterable[dict[str, Any]],
        *,
        configured_ids: Iterable[Any] | None = None,
        target_names: Iterable[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Compare live Discord inventory to the saved inventory and persist it.

        The result always contains exact ``added``, ``removed``, ``changed``,
        ``replaced``, and final ``targets`` lists so callers can emit a precise
        audit line even when the API response is empty or partially malformed.
        """
        timestamp = time.time() if now is None else float(now)
        normalized_servers: dict[str, dict[str, Any]] = {}
        live_channels: dict[str, dict[str, Any]] = {}
        for raw_server in live_servers or []:
            if not isinstance(raw_server, dict):
                continue
            gid = _numeric_id(raw_server.get("id"))
            if not gid:
                continue
            gname = _clean_text(raw_server.get("name"), 120, gid)
            records: dict[str, dict[str, Any]] = {}
            raw_channels = raw_server.get("channels")
            if not isinstance(raw_channels, list):
                raw_channels = []
            for raw_channel in raw_channels:
                record = _channel_record(raw_channel, gid, gname, timestamp)
                if record:
                    records[record["id"]] = record
                    live_channels[record["id"]] = record
            normalized_servers[gid] = {"id": gid, "name": gname, "channels": records}

        with self._lock:
            data = self.store.load()
            data.setdefault("version", self.VERSION)
            data.setdefault("alts", {})
            key = str(int(alt_id))
            old_entry = data["alts"].get(key)
            first_connection = not isinstance(old_entry, dict) or not old_entry.get("updated_at")
            entry = copy.deepcopy(old_entry) if isinstance(old_entry, dict) else self._empty_alt(alt_id)
            old_channels = entry.get("channels") if isinstance(entry.get("channels"), dict) else {}
            old_channels = {
                str(cid): raw for cid, raw in old_channels.items() if isinstance(raw, dict) and _numeric_id(cid)
            }
            old_targets = [cid for cid in entry.get("targets", []) if _numeric_id(cid)] if isinstance(entry.get("targets"), list) else []
            old_target_names = [
                _clean_text(item, 80).casefold()
                for item in (entry.get("target_names", []) if isinstance(entry.get("target_names"), list) else [])
                if _clean_text(item, 80)
            ]
            if target_names is not None:
                old_target_names = list(dict.fromkeys(_clean_text(item, 80).casefold() for item in target_names if _clean_text(item, 80)))

            old_ids = set(old_channels)
            live_ids = set(live_channels)
            added = sorted(live_ids - old_ids)
            removed = sorted(old_ids - live_ids)
            changed: list[str] = []
            for cid in sorted(old_ids & live_ids):
                before = old_channels[cid]
                after = live_channels[cid]
                if before.get("name") != after.get("name") or before.get("guild_id") != after.get("guild_id") or before.get("slowmode", 0) != after.get("slowmode", 0):
                    changed.append(cid)

            explicit_ids = None if configured_ids is None else list(dict.fromkeys(cid for cid in (_numeric_id(x) for x in configured_ids) if cid))[:100]
            explicit_replacements: list[dict[str, str]] = []
            if explicit_ids is not None:
                targets = [cid for cid in explicit_ids if cid in live_ids]
                # On the first connection, explicit workflow targets are the
                # initial posting set. Thereafter every newly discovered
                # eligible channel is added automatically, while removed IDs
                # disappear from the active target list.
                if not first_connection:
                    targets.extend(cid for cid in added if cid not in targets)
                # A workflow may still carry a deleted ID in CHANNEL_IDS.
                # Prefer a unique same-name replacement from the live catalog.
                for cid in explicit_ids:
                    if cid in live_ids:
                        continue
                    old_name = str(old_channels.get(cid, {}).get("name") or "").casefold()
                    candidates = [
                        live_cid for live_cid in added
                        if str(live_channels[live_cid].get("name") or "").casefold() == old_name
                    ] if old_name else []
                    if len(candidates) == 1:
                        new_cid = candidates[0]
                        if new_cid not in targets:
                            targets.append(new_cid)
                        explicit_replacements.append({"old_id": cid, "new_id": new_cid, "name": str(live_channels[new_cid].get("name") or old_name)})
            elif first_connection:
                targets = sorted(live_ids)
            else:
                targets = [cid for cid in old_targets if cid in live_ids]
                # The documented autorescan contract is additive: every new
                # eligible channel is now a target, and every removed target
                # is dropped. Same-name recreation is still represented in the
                # replacement audit record below.
                targets.extend(cid for cid in added if cid not in targets)
                targets = [cid for cid in targets if cid in live_ids]
            targets = list(dict.fromkeys(targets))[:100]

            replaced: list[dict[str, str]] = list(explicit_replacements)
            old_target_records = {cid: old_channels.get(cid, {}) for cid in old_targets}
            for old_cid in removed:
                old_name = str(old_target_records.get(old_cid, {}).get("name") or "").casefold()
                if not old_name:
                    continue
                candidates = [cid for cid in added if str(live_channels[cid].get("name") or "").casefold() == old_name]
                if len(candidates) == 1:
                    replacement = {"old_id": old_cid, "new_id": candidates[0], "name": str(live_channels[candidates[0]].get("name") or old_name)}
                    if not any(item.get("old_id") == old_cid and item.get("new_id") == candidates[0] for item in replaced):
                        replaced.append(replacement)

            entry["alt_id"] = int(alt_id)
            entry["first_connected_at"] = float(entry.get("first_connected_at") or timestamp)
            entry["updated_at"] = timestamp
            entry["servers"] = normalized_servers
            entry["channels"] = live_channels
            entry["targets"] = targets
            entry["target_names"] = list(dict.fromkeys(
                [str(live_channels[cid].get("name") or "") for cid in targets if cid in live_channels and live_channels[cid].get("name")]
                + [str(item) for item in old_target_names if item]
            ))[:100]
            data["alts"][key] = entry
            persisted = self.store.save(data)

        return {
            "ok": persisted,
            "alt_id": int(alt_id),
            "first_connection": first_connection,
            "servers": normalized_servers,
            "catalogue": live_channels,
            "added": added,
            "removed": removed,
            "changed": changed,
            "replaced": replaced,
            "targets": targets,
            "error": "" if persisted else "Could not persist channel registry atomically.",
        }
