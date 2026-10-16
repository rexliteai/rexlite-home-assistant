"""YAML setup for HACS integrations that have no config flow.

A HACS integration with ``config_flow: false`` is set up from configuration
YAML only. The MAX'Is operation platform writes that YAML as one Home
Assistant package per integration, ``.rexlite_hacs/<domain>.yaml``, linked from
configuration.yaml the way the KNX deployment links its own package
(``rexlite_hacs_<domain>: !include .rexlite_hacs/<domain>.yaml``). The
directory is hidden so a ``packages: !include_dir_*`` that the hub already has
never picks the files up a second time.

A package may only configure its own integration: the ``<domain>`` key, and
entity platform lists whose every item is ``platform: <domain>``; ``!secret``
is the only tag. Each save is journaled and checked with Home Assistant's own
configuration check, and a save that adds a configuration error or warning is
rolled back, so it never blocks a restart. HA applies the YAML on its next
restart. Removing the integration leaves an empty package behind (the include
stays valid).

``get`` also returns the integration's own configuration schemas as forms (the
``<domain>`` key and each entity platform's ``PLATFORM_SCHEMA``), serialized the
way HA serializes config flow forms, so the platform can offer a form instead
of a YAML editor. A schema with a required field that has no form type is left
out.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
import yaml

from .knx_project_deployment import DeploymentError, SafeFiles

DATA_KEY = "rexlite_hacs_yaml"
MANAGED_DIR = ".rexlite_hacs"
JOURNAL = f"{MANAGED_DIR}/transaction.json"
PACKAGE_PREFIX = "rexlite_hacs_"
MAX_YAML_CHARS = 65536
MAX_ISSUES = 5
MAX_ISSUE_CHARS = 300
# A first check may install the integration's requirements.
CHECK_TIMEOUT = 100
HEADER = "# MAX'Is運營平台管理（社群商店 YAML 設定），請由平台修改。\n"
EMPTY_PACKAGE = "{}\n"
# HA integration domains that are also valid package names (cv.slug).
_DOMAIN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_LINE = re.compile(r"\bline \d+")
# Platform keys the platform fills itself or that only YAML experts use.
_FORM_SKIPPED = {"platform", "entity_namespace"}
MAX_FORMS = 3
MAX_FORM_FIELDS = 40

_LOGGER = logging.getLogger(__name__)


class HacsYamlError(Exception):
    """User-facing failure; ``code`` is stable for the operation platform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Secret(str):
    """A ``!secret`` reference; HA resolves it from secrets.yaml."""


class _PackageLoader(yaml.SafeLoader):
    """Safe YAML plus ``!secret``; any other tag (``!include``, …) is refused."""


_PackageLoader.add_constructor(
    "!secret", lambda loader, node: _Secret(loader.construct_scalar(node))
)


def _clip(message: str) -> str:
    message = " ".join(str(message).split())
    if len(message) > MAX_ISSUE_CHARS:
        return message[:MAX_ISSUE_CHARS] + "…"
    return message


def _yaml_error(err: yaml.YAMLError) -> str:
    mark = getattr(err, "problem_mark", None)
    problem = getattr(err, "problem", None) or str(err)
    if isinstance(err, yaml.constructor.ConstructorError) and "tag" in problem:
        problem = "只支援 !secret 標籤"
    return _clip((f"第 {mark.line + 1} 行：" if mark else "") + problem)


def _platform_items(value: Any, domain: str) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(
        isinstance(item, dict) and item.get("platform") == domain for item in items
    )


def package_issues(domain: str, text: str) -> list[str]:
    """Why ``text`` cannot be this integration's package; [] when it can."""

    try:
        for event in yaml.parse(text, Loader=_PackageLoader):
            if isinstance(event, yaml.AliasEvent):
                return ["不支援 YAML 錨點（*）"]
        data = yaml.load(text, Loader=_PackageLoader)  # noqa: S506 - SafeLoader
    except yaml.YAMLError as err:
        return [_yaml_error(err)]
    if not isinstance(data, dict) or not data:
        return [f"請以「{domain}:」或「sensor:」等鍵開頭"]
    issues = [
        _clip(f"「{key}」不屬於 {domain}：只能設定 {domain}: 或 platform: {domain}")
        for key, value in data.items()
        if key != domain
        and (
            not isinstance(key, str)
            or not _DOMAIN.match(key)
            or key == "homeassistant"
            or not _platform_items(value, domain)
        )
    ]
    return issues[:MAX_ISSUES]


def form_fields(schema: Any, convert: Callable[[Any], Any]) -> list[dict] | None:
    """A mapping schema as form fields, one ``convert`` per key.

    ``convert`` is voluptuous_serialize with HA's serializer. A field it cannot
    express is left out when optional; a required one makes the schema
    unusable as a form (None).
    """

    if isinstance(schema, vol.Schema):
        schema = schema.schema
    if not isinstance(schema, dict):
        return None
    fields = []
    for key, value in schema.items():
        if key is vol.Extra:
            continue
        name = key.schema if isinstance(key, vol.Marker) else key
        if not isinstance(name, str):
            return None
        if name in _FORM_SKIPPED:
            continue
        try:
            converted = convert(vol.Schema({key: value}))
            field = converted[0] if isinstance(converted, list) and converted else None
            json.dumps(field)
        except (TypeError, ValueError):
            field = None
        if not isinstance(field, dict) or field.get("name") != name:
            if isinstance(key, vol.Required):
                return None
            continue
        fields.append(field)
    return fields if len(fields) <= MAX_FORM_FIELDS else None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(value: str | None) -> bytes | None:
    return base64.b64decode(value) if value is not None else None


def _encode(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode() if value is not None else None


def _content(data: bytes | None) -> str:
    """The operator's YAML in a package file; "" for none or an empty package."""

    if data is None:
        return ""
    text = data.decode("utf-8", "replace")
    if text.startswith(HEADER):
        text = text[len(HEADER) :]
    return "" if text.strip() in ("", "{}") else text


def _issue_key(message: str) -> str:
    # Inserting the include moves configuration.yaml's later lines down.
    return _LINE.sub("line N", " ".join(message.split()))


def new_issues(before: list[str], after: list[str]) -> list[str]:
    """Configuration errors and warnings the save added, newest first kept."""

    seen = {_issue_key(message) for message in before}
    issues: list[str] = []
    for message in after:
        key = _issue_key(message)
        if key not in seen:
            seen.add(key)
            issues.append(_clip(message))
    return issues[:MAX_ISSUES]


class HacsYamlPackages:
    """Read, save and empty integration packages. HA specifics are injected."""

    def __init__(
        self,
        *,
        files: SafeFiles,
        io: Callable[..., Awaitable[Any]],
        lock: asyncio.Lock,
        check_config: Callable[[], Awaitable[tuple[list[str], list[str]]]],
        integration_exists: Callable[[str], Awaitable[bool]],
        components: Callable[[], set[str]],
        forms: Callable[[str], Awaitable[list[dict]]] | None = None,
    ) -> None:
        self.files = files
        self._io = io
        # configuration.yaml has one writer at a time (shared with KNX deployment).
        self._lock = lock
        self._check_config = check_config
        self._integration_exists = integration_exists
        self._components = components
        self._forms = forms
        # Package digests HA loaded at startup; a different file needs a restart.
        self._loaded: dict[str, str] = {}

    @staticmethod
    def _domain(value: Any) -> str:
        domain = value.strip() if isinstance(value, str) else ""
        if len(domain) > 64 or not _DOMAIN.match(domain):
            raise HacsYamlError("hacs_yaml_domain_invalid", "套件的整合名稱不正確")
        return domain

    @staticmethod
    def _relative(domain: str) -> str:
        return f"{MANAGED_DIR}/{domain}.yaml"

    async def handle(self, msg: dict) -> dict:
        action = msg["action"]
        if action == "status":
            return {"supported": True}
        domain = self._domain(msg.get("domain"))
        if action == "get":
            return await self.get(domain)
        if action == "save":
            return await self.save(domain, msg.get("yaml", ""))
        return await self.remove(domain)

    async def snapshot(self) -> None:
        """Remember the packages HA loaded while starting."""

        self._loaded = await self._io(self._read_digests)

    def _read_digests(self) -> dict[str, str]:
        directory = self.files.path(MANAGED_DIR)
        if not directory.is_dir():
            return {}
        digests = {}
        for path in directory.glob("*.yaml"):
            if _DOMAIN.match(path.stem):
                data = self.files.read(self._relative(path.stem))
                if data is not None:
                    digests[path.stem] = _digest(data)
        return digests

    def _loaded_now(self, domain: str) -> bool:
        return any(
            component == domain
            or component.startswith(f"{domain}.")
            or component.endswith(f".{domain}")
            for component in self._components()
        )

    def _state(self, domain: str, data: bytes | None) -> dict[str, Any]:
        content = _content(data)
        return {
            "domain": domain,
            "configured": bool(content),
            "content": content,
            "loaded": self._loaded_now(domain),
            "restartRequired": (_digest(data) if data is not None else None)
            != self._loaded.get(domain),
        }

    async def get(self, domain: str) -> dict:
        data = await self._read(self._relative(domain))
        state = self._state(domain, data)
        state["forms"] = []
        if self._forms is not None:
            try:
                state["forms"] = (await self._forms(domain))[:MAX_FORMS]
            except Exception:  # noqa: BLE001 - the YAML editor still works
                _LOGGER.debug("No form for %s", domain, exc_info=True)
        return state

    async def _read(self, relative: str) -> bytes | None:
        try:
            return await self._io(self.files.read, relative)
        except DeploymentError as err:
            raise HacsYamlError(
                "hacs_yaml_file_unsafe", f"設定檔無法讀取（{err}）"
            ) from err

    async def recover(self) -> None:
        """Undo a save that HA stopped in the middle of."""

        async with self._lock:
            await self._recover_pending()

    async def _recover_pending(self) -> None:
        journal = await self._io(self.files.read_json, JOURNAL)
        if journal:
            _LOGGER.warning("Rolling back an interrupted HACS YAML save")
            await self._io(self._restore, journal)

    def _restore(self, journal: dict) -> None:
        # Newest first: the include goes before the file it points to.
        for relative, item in reversed(list(journal["files"].items())):
            current = self.files.read(relative)
            before, after = _decode(item["before"]), _decode(item["after"])
            if current == before:
                continue
            if current != after:
                raise HacsYamlError(
                    "hacs_yaml_rollback_blocked", "設定檔已被其他程式修改，無法還原"
                )
            self.files.write(relative, before)
        self.files.write(JOURNAL, None)

    def _prepare(self, domain: str, data: bytes) -> dict[str, dict]:
        """The journal entries (before/after) this save needs; {} when none."""

        relative = self._relative(domain)
        root = self.files.read("configuration.yaml")
        activation = self.files.activation_changes(PACKAGE_PREFIX + domain, relative)
        if self.files.read("configuration.yaml") != root:
            raise HacsYamlError("hacs_yaml_changed", "設定檔同時被修改，請重試")
        changes = {relative: data, **activation}
        entries = {}
        for path, after in changes.items():
            before = root if path == "configuration.yaml" else self.files.read(path)
            if before != after:
                entries[path] = {"before": _encode(before), "after": _encode(after)}
        return entries

    def _apply(self, journal: dict) -> None:
        written: dict[str, dict] = {}
        for relative, item in journal["files"].items():
            if self.files.read(relative) != _decode(item["before"]):
                self._restore({"files": written})
                raise HacsYamlError("hacs_yaml_changed", "設定檔同時被修改，請重試")
            self.files.write(relative, _decode(item["after"]))
            written[relative] = item

    async def _issues(self) -> list[str]:
        try:
            async with asyncio.timeout(CHECK_TIMEOUT):
                errors, warnings = await self._check_config()
        except TimeoutError as err:
            raise HacsYamlError(
                "hacs_yaml_check_timeout", "設定檢查逾時，請稍後再試"
            ) from err
        return [*errors, *warnings]

    async def save(self, domain: str, text: Any) -> dict:
        text = text.replace("\r\n", "\n") if isinstance(text, str) else ""
        if not text.strip():
            raise HacsYamlError("hacs_yaml_empty", "請輸入 YAML 設定")
        if len(text) > MAX_YAML_CHARS:
            raise HacsYamlError("hacs_yaml_too_large", "YAML 設定過長")
        if issues := package_issues(domain, text):
            return {"saved": False, "errors": issues}
        # A newly downloaded integration only loads, and validates, after a restart.
        if not await self._integration_exists(domain):
            raise HacsYamlError(
                "hacs_yaml_restart_required", "中樞主機尚未載入此套件，請先重新啟動"
            )
        data = (HEADER + text.rstrip("\n") + "\n").encode()
        async with self._lock:
            await self._recover_pending()
            try:
                entries = await self._io(self._prepare, domain, data)
            except DeploymentError as err:
                raise HacsYamlError(
                    "hacs_yaml_config_unsupported",
                    f"configuration.yaml 的結構無法自動加入套件（{err}）",
                ) from err
            if not entries:
                return {"saved": True, "changed": False, **self._state(domain, data)}
            before = await self._issues()
            journal = {"domain": domain, "files": entries}
            await self._io(self.files.write_json, JOURNAL, journal)
            try:
                await self._io(self._apply, journal)
                after = await self._issues()
            except HacsYamlError as err:
                if err.code != "hacs_yaml_changed":
                    await self._io(self._restore, journal)
                raise
            except BaseException:
                await self._io(self._restore, journal)
                raise
            if added := new_issues(before, after):
                await self._io(self._restore, journal)
                return {"saved": False, "errors": added}
            await self._io(self.files.write, JOURNAL, None)
            return {"saved": True, "changed": True, **self._state(domain, data)}

    async def remove(self, domain: str) -> dict:
        """Empty the package of an integration that is being removed."""

        relative = self._relative(domain)
        async with self._lock:
            await self._recover_pending()
            current = await self._read(relative)
            if not _content(current):
                return {"removed": False, **self._state(domain, current)}
            data = (HEADER + EMPTY_PACKAGE).encode()
            await self._io(self.files.write, relative, data)
            return {"removed": True, **self._state(domain, data)}


def register_hacs_yaml(hass: Any, lock: asyncio.Lock) -> HacsYamlPackages:
    """Register the admin-only ``rexlite/hacs/yaml_package`` command once."""

    from functools import partial

    import voluptuous_serialize
    from homeassistant.components import websocket_api
    from homeassistant.const import Platform
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.check_config import async_check_ha_config_file
    from homeassistant.loader import IntegrationNotFound, async_get_integration

    convert = partial(
        voluptuous_serialize.convert, custom_serializer=cv.custom_serializer
    )

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    async def check_config() -> tuple[list[str], list[str]]:
        result = await async_check_ha_config_file(hass)
        return (
            [error.message for error in result.errors],
            [warning.message for warning in result.warnings],
        )

    async def integration_exists(domain: str) -> bool:
        try:
            await async_get_integration(hass, domain)
        except IntegrationNotFound:
            return False
        return True

    async def forms(domain: str) -> list[dict]:
        """The ``<domain>`` key, then each entity platform the integration has."""
        try:
            integration = await async_get_integration(hass, domain)
        except IntegrationNotFound:
            return []
        component = await integration.async_get_component()
        schemas: list[tuple[str, Any]] = []
        config_schema = getattr(component, "CONFIG_SCHEMA", None)
        if isinstance(config_schema, vol.Schema) and isinstance(
            config_schema.schema, dict
        ):
            for key, value in config_schema.schema.items():
                if (key.schema if isinstance(key, vol.Marker) else key) == domain:
                    schemas.append((domain, value))
        for platform in integration.platforms_exists([p.value for p in Platform]):
            module = await integration.async_get_platform(platform)
            schema = getattr(module, "PLATFORM_SCHEMA", None)
            if schema is not None:
                schemas.append((platform, schema))
        result = []
        for key, schema in schemas:
            fields = form_fields(schema, convert)
            if fields is not None:
                result.append({"key": key, "fields": fields})
        return result

    packages = hass.data[DATA_KEY] = HacsYamlPackages(
        files=SafeFiles(hass.config.config_dir),
        io=hass.async_add_executor_job,
        lock=lock,
        check_config=check_config,
        integration_exists=integration_exists,
        components=lambda: set(hass.config.components),
        forms=forms,
    )

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/hacs/yaml_package",
            vol.Required("action"): vol.In(("status", "get", "save", "remove")),
            vol.Optional("domain"): str,
            vol.Optional("yaml"): vol.All(str, vol.Length(max=MAX_YAML_CHARS)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def yaml_package(hass: Any, connection: Any, msg: dict) -> None:
        # A client disconnect must not stop a save between write and rollback.
        task = hass.async_create_task(packages.handle(msg), "REXLiTE HACS YAML package")
        try:
            result = await asyncio.shield(task)
        except HacsYamlError as err:
            connection.send_error(msg["id"], err.code, err.message)
        except Exception:  # noqa: BLE001 - never break the admin websocket
            _LOGGER.exception("HACS YAML package %s failed", msg["action"])
            connection.send_error(
                msg["id"], "hacs_yaml_failed", "YAML 設定失敗，請稍後再試"
            )
        else:
            connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, yaml_package)
    return packages
