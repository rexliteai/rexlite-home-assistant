"""YAML packages for HACS integrations without a config flow.

Real files in a temporary config directory; HA's configuration check, its
integration loader and its loaded components are injected.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

import voluptuous as vol

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_hacs_yaml_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in ("knx_project_deployment", "hacs_yaml"):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
m = sys.modules[f"{package.__name__}.hacs_yaml"]

SENSOR = "sensor:\n  - platform: yaml_sensor\n    api_key: !secret yaml_key\n"


class Hub:
    """A config directory, HA's check_config result and loaded components."""

    def __init__(self, configuration: str = "default_config:\n") -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "configuration.yaml").write_text(configuration)
        self.issues: list[str] = []
        # Issues HA reports once a package file holds this text.
        self.issues_for: dict[str, list[str]] = {}
        self.known = {"yaml_sensor"}
        self.components: set[str] = set()
        self.checks = 0
        self.lock = asyncio.Lock()

    def file(self, relative: str) -> str | None:
        path = self.root / relative
        return path.read_text() if path.exists() else None

    def packages(self):
        async def io(function, *args):
            return function(*args)

        async def check_config():
            self.checks += 1
            issues = list(self.issues)
            for path in (self.root / ".rexlite_hacs").glob("*.yaml"):
                for marker, extra in self.issues_for.items():
                    if marker in path.read_text():
                        issues.extend(extra)
            return [], issues

        async def integration_exists(domain):
            return domain in self.known

        return m.HacsYamlPackages(
            files=m.SafeFiles(self.root),
            io=io,
            lock=self.lock,
            check_config=check_config,
            integration_exists=integration_exists,
            components=lambda: self.components,
        )


def run(coroutine):
    return asyncio.run(coroutine)


class HubTest(unittest.TestCase):
    def hub(self, configuration: str = "default_config:\n") -> Hub:
        hub = Hub(configuration)
        self.addCleanup(hub.directory.cleanup)
        return hub


class PackageIssuesTest(unittest.TestCase):
    def test_accepts_own_domain_and_platform_lists(self):
        self.assertEqual(m.package_issues("yaml_sensor", SENSOR), [])
        self.assertEqual(m.package_issues("yaml_sensor", "yaml_sensor:\n"), [])
        self.assertEqual(
            m.package_issues(
                "yaml_sensor",
                "yaml_sensor:\n  host: 1.2.3.4\nnotify:\n  platform: yaml_sensor\n",
            ),
            [],
        )

    def test_refuses_other_integrations_tags_and_aliases(self):
        self.assertIn(
            "shell_command",
            m.package_issues("yaml_sensor", "shell_command:\n  x: rm -rf /\n")[0],
        )
        self.assertIn(
            "sensor",
            m.package_issues("yaml_sensor", "sensor:\n  - platform: template\n")[0],
        )
        self.assertIn(
            "homeassistant",
            m.package_issues(
                "yaml_sensor", "homeassistant:\n  - platform: yaml_sensor\n"
            )[0],
        )
        self.assertIn(
            "!secret",
            m.package_issues("yaml_sensor", "yaml_sensor: !include ../secrets.yaml\n")[
                0
            ],
        )
        self.assertIn(
            "錨點",
            m.package_issues("yaml_sensor", "a: &x 1\nyaml_sensor:\n  b: *x\n")[0],
        )
        self.assertIn(
            "第 2 行", m.package_issues("yaml_sensor", "yaml_sensor:\n  a: b: c\n")[0]
        )
        self.assertTrue(m.package_issues("yaml_sensor", "- just a list\n"))

    def test_new_issues_ignore_shifted_line_numbers(self):
        before = ["Invalid config for 'x' at configuration.yaml, line 9: bad"]
        after = [
            "Invalid config for 'x' at configuration.yaml, line 12: bad",
            "Invalid config for 'yaml_sensor' at .rexlite_hacs/yaml_sensor.yaml, "
            "line 3: required key 'api_key' not provided",
        ]
        self.assertEqual(m.new_issues(before, after), [after[1]])


class SaveTest(HubTest):
    def test_first_save_writes_the_package_and_links_it(self):
        hub = self.hub()
        result = run(hub.packages().save("yaml_sensor", SENSOR))
        self.assertTrue(result["saved"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["configured"])
        self.assertTrue(result["restartRequired"])
        self.assertEqual(result["content"], SENSOR)
        self.assertEqual(hub.file(".rexlite_hacs/yaml_sensor.yaml"), m.HEADER + SENSOR)
        self.assertIn(
            "rexlite_hacs_yaml_sensor: !include .rexlite_hacs/yaml_sensor.yaml",
            hub.file("configuration.yaml"),
        )
        self.assertIsNone(hub.file(".rexlite_hacs/transaction.json"))
        self.assertEqual(hub.checks, 2)

    def test_second_package_joins_the_existing_packages_mapping(self):
        hub = self.hub(
            "default_config:\nhomeassistant:\n  packages:\n"
            "    rexlite_knx_auto: !include .rexlite_knx/entities.yaml\n"
        )
        hub.known.add("other_thing")
        packages = hub.packages()
        run(packages.save("yaml_sensor", SENSOR))
        run(packages.save("other_thing", "other_thing:\n  host: x\n"))
        configuration = hub.file("configuration.yaml")
        self.assertIn("rexlite_knx_auto: !include", configuration)
        self.assertIn("    rexlite_hacs_yaml_sensor: !include", configuration)
        self.assertIn("    rexlite_hacs_other_thing: !include", configuration)

    def test_dir_named_packages_get_a_stub(self):
        hub = self.hub("homeassistant:\n  packages: !include_dir_named packages\n")
        (hub.root / "packages").mkdir()
        run(hub.packages().save("yaml_sensor", SENSOR))
        self.assertEqual(
            hub.file("packages/rexlite_hacs_yaml_sensor.yaml"),
            '!include "../.rexlite_hacs/yaml_sensor.yaml"\n',
        )
        self.assertNotIn("rexlite_hacs", hub.file("configuration.yaml"))

    def test_unchanged_save_skips_the_check(self):
        hub = self.hub()
        packages = hub.packages()
        run(packages.save("yaml_sensor", SENSOR))
        result = run(packages.save("yaml_sensor", SENSOR + "\n\n"))
        self.assertEqual((result["saved"], result["changed"]), (True, False))
        self.assertEqual(hub.checks, 2)

    def test_new_check_issues_roll_back_every_file(self):
        hub = self.hub()
        original = hub.file("configuration.yaml")
        hub.issues = ["Integration error: broken_elsewhere - not found"]
        hub.issues_for["api_key"] = [
            "Invalid config for 'sensor.yaml_sensor' at .rexlite_hacs/yaml_sensor.yaml,"
            " line 3: expected str for dictionary value 'api_key'"
        ]
        result = run(hub.packages().save("yaml_sensor", SENSOR))
        self.assertFalse(result["saved"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("api_key", result["errors"][0])
        self.assertEqual(hub.file("configuration.yaml"), original)
        self.assertIsNone(hub.file(".rexlite_hacs/yaml_sensor.yaml"))
        self.assertIsNone(hub.file(".rexlite_hacs/transaction.json"))

    def test_rollback_keeps_the_previous_package(self):
        hub = self.hub()
        packages = hub.packages()
        run(packages.save("yaml_sensor", SENSOR))
        hub.issues_for["bad"] = ["Invalid config for 'yaml_sensor': bad"]
        result = run(packages.save("yaml_sensor", "yaml_sensor:\n  bad: 1\n"))
        self.assertFalse(result["saved"])
        self.assertEqual(hub.file(".rexlite_hacs/yaml_sensor.yaml"), m.HEADER + SENSOR)

    def test_invalid_yaml_never_touches_files(self):
        hub = self.hub()
        result = run(hub.packages().save("yaml_sensor", "shell_command:\n  x: ls\n"))
        self.assertFalse(result["saved"])
        self.assertEqual(hub.checks, 0)
        self.assertIsNone(hub.file(".rexlite_hacs/yaml_sensor.yaml"))

    def test_an_integration_ha_has_not_loaded_needs_a_restart_first(self):
        hub = self.hub()
        hub.known.clear()
        with self.assertRaises(m.HacsYamlError) as caught:
            run(hub.packages().save("yaml_sensor", SENSOR))
        self.assertEqual(caught.exception.code, "hacs_yaml_restart_required")

    def test_empty_bad_domain_and_unsupported_configuration(self):
        hub = self.hub()
        packages = hub.packages()
        with self.assertRaises(m.HacsYamlError) as caught:
            run(packages.save("yaml_sensor", "  \n"))
        self.assertEqual(caught.exception.code, "hacs_yaml_empty")
        with self.assertRaises(m.HacsYamlError) as caught:
            run(packages.handle({"action": "get", "domain": "../etc"}))
        self.assertEqual(caught.exception.code, "hacs_yaml_domain_invalid")
        (hub.root / "configuration.yaml").write_text(
            "homeassistant:\n  packages: !include packages.yaml\n"
        )
        with self.assertRaises(m.HacsYamlError) as caught:
            run(packages.save("yaml_sensor", SENSOR))
        self.assertEqual(caught.exception.code, "hacs_yaml_config_unsupported")

    def test_interrupted_save_is_rolled_back(self):
        hub = self.hub()
        packages = hub.packages()
        original = hub.file("configuration.yaml")
        entries = packages._prepare("yaml_sensor", (m.HEADER + SENSOR).encode())
        packages.files.write_json(m.JOURNAL, {"files": entries})
        packages._apply({"files": entries})
        run(packages.recover())
        self.assertEqual(hub.file("configuration.yaml"), original)
        self.assertIsNone(hub.file(".rexlite_hacs/yaml_sensor.yaml"))
        self.assertIsNone(hub.file(".rexlite_hacs/transaction.json"))


def convert(schema):
    """voluptuous_serialize's shape for the types these tests use."""

    ((key, value),) = schema.schema.items()
    types = {str: "string", int: "integer", bool: "boolean"}
    if isinstance(value, vol.In):
        field = {"type": "select", "options": [(o, o) for o in value.container]}
    elif value in types:
        field = {"type": types[value]}
    else:
        raise ValueError(f"Unable to convert schema: {value}")
    field.update(name=key.schema, required=isinstance(key, vol.Required))
    if not isinstance(key.default, vol.Undefined):
        field["default"] = key.default()
    return [field]


class FormTest(HubTest):
    def test_schema_fields_skip_what_a_form_cannot_show(self):
        schema = vol.Schema(
            {
                vol.Required("platform"): str,
                vol.Required("api_key"): str,
                vol.Optional("port", default=80): int,
                vol.Optional("mode", default="eco"): vol.In(["eco", "boost"]),
                vol.Optional("scan_interval"): lambda value: value,
                vol.Optional("entity_namespace"): str,
            },
            extra=vol.ALLOW_EXTRA,
        )
        self.assertEqual(
            [field["name"] for field in m.form_fields(schema, convert)],
            ["api_key", "port", "mode"],
        )
        self.assertEqual(m.form_fields(schema, convert)[1]["default"], 80)
        # A required field without a form type: no form at all.
        self.assertIsNone(
            m.form_fields({vol.Required("hosts"): [lambda v: v]}, convert)
        )
        self.assertIsNone(m.form_fields(lambda config: config, convert))
        self.assertEqual(m.form_fields(vol.Schema({}), convert), [])

    def test_get_offers_the_forms_and_survives_a_failing_one(self):
        hub = self.hub()
        packages = hub.packages()
        form = {"key": "sensor", "fields": [{"name": "api_key", "type": "string"}]}

        async def forms(domain):
            return [form] * 5

        packages._forms = forms
        self.assertEqual(run(packages.get("yaml_sensor"))["forms"], [form] * 3)

        async def broken(domain):
            raise ImportError("platform import failed")

        packages._forms = broken
        self.assertEqual(run(packages.get("yaml_sensor"))["forms"], [])


class StateTest(HubTest):
    def test_get_reports_content_loaded_and_restart(self):
        hub = self.hub()
        packages = hub.packages()
        self.assertEqual(
            run(packages.get("yaml_sensor")),
            {
                "domain": "yaml_sensor",
                "configured": False,
                "content": "",
                "loaded": False,
                "restartRequired": False,
                "forms": [],
            },
        )
        run(packages.save("yaml_sensor", SENSOR))
        # HA restarts: it loads the package and the platform.
        restarted = hub.packages()
        run(restarted.snapshot())
        hub.components = {"sensor", "yaml_sensor.sensor"}
        state = run(restarted.get("yaml_sensor"))
        self.assertTrue(state["configured"])
        self.assertTrue(state["loaded"])
        self.assertFalse(state["restartRequired"])
        self.assertEqual(state["content"], SENSOR)

    def test_remove_leaves_an_empty_package(self):
        hub = self.hub()
        packages = hub.packages()
        run(packages.save("yaml_sensor", SENSOR))
        result = run(packages.remove("yaml_sensor"))
        self.assertTrue(result["removed"])
        self.assertFalse(result["configured"])
        self.assertEqual(
            hub.file(".rexlite_hacs/yaml_sensor.yaml"), m.HEADER + m.EMPTY_PACKAGE
        )
        self.assertIn("rexlite_hacs_yaml_sensor", hub.file("configuration.yaml"))
        self.assertFalse(run(packages.remove("yaml_sensor"))["removed"])

    def test_status_is_the_capability_probe(self):
        self.assertEqual(
            run(self.hub().packages().handle({"action": "status"})), {"supported": True}
        )


if __name__ == "__main__":
    unittest.main()
