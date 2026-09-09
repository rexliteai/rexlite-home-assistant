"""Safe ETS-to-YAML mapping behavior without a Home Assistant runtime."""

from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components/rexlite/knx_project_mapper.py"
)
SPEC = importlib.util.spec_from_file_location("rexlite_knx_mapper_test", MODULE_PATH)
assert SPEC and SPEC.loader
mapper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mapper
SPEC.loader.exec_module(mapper)


def ga(address, name, main, sub):
    return {
        "address": address,
        "name": name,
        "dpt": {"main": main, "sub": sub},
        "communication_object_ids": [],
    }


def project(*groups, functions=None, objects=None):
    return {
        "info": {"guid": "test-project-guid"},
        "group_addresses": {g["address"]: g for g in groups},
        "functions": functions or {},
        "communication_objects": objects or {},
    }


def function(kind, **roles):
    return {
        "name": "Defined function",
        "function_type": kind,
        "group_addresses": {r: {"address": a, "role": r} for r, a in roles.items()},
    }


def sample_conventions():
    # Synthetic independent names using the supplied project's naming convention.
    # No user project, location, device addresses or real installation data stored.
    return project(
        ga("1/0/1", "測試主燈 開關", 1, 1),
        ga("1/0/2", "測試主燈 狀態", 1, 11),
        ga("1/0/3", "測試主燈 相對調光", 3, 7),
        ga("1/0/4", "測試主燈 亮度", 5, 1),
        ga("1/0/5", "測試主燈 亮度狀態", 5, 1),
        ga("1/1/1", "測試次燈 開關", 1, 1),
        ga("1/1/2", "測試次燈 狀態", 1, 11),
        ga("2/0/1", "測試窗簾 上下", 1, 8),
        ga("2/0/2", "測試窗簾 停止/微調", 1, 7),
        ga("2/0/3", "測試窗簾 位置", 5, 1),
        ga("2/0/4", "測試窗簾 位置狀態", 5, 1),
        ga("3/0/1", "測試 溫度", 9, 1),
        ga("3/0/2", "測試 濕度", 9, 7),
        ga("4/0/1", "測試 情境", 18, 1),
    )


def has_ha_distribution():
    try:
        importlib.metadata.version("homeassistant")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


class KNXProjectMappingTests(unittest.TestCase):
    def test_exact_conventions_map_five_entities_and_leave_scene_unresolved(self):
        result = mapper.plan_project(sample_conventions())
        self.assertEqual(result["entityCount"], 5)
        self.assertEqual(
            {k: len(v) for k, v in result["config"].items()},
            {"light": 2, "cover": 1, "sensor": 2},
        )
        self.assertEqual(result["config"]["light"][0]["brightness_address"], "1/0/4")
        self.assertEqual(result["config"]["light"][0]["state_address"], "1/0/2")
        cover = result["config"]["cover"][0]
        self.assertEqual(cover["move_short_address"], "2/0/2")
        self.assertNotIn("stop_address", cover)  # DPT 1.007 is step/stop, not 1.010.
        self.assertEqual({s["address"] for s in result["skipped"]}, {"1/0/3", "4/0/1"})
        self.assertEqual(
            next(s for s in result["skipped"] if s["address"] == "4/0/1")["reason"],
            "missing_explicit_scene_number",
        )

    def test_no_dpt_one_switch_or_scene_number_guessed_from_address(self):
        result = mapper.plan_project(
            project(ga("1/0/1", "Unknown", 1, 1), ga("4/0/13", "Scene 13", 18, 1))
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(len(result["skipped"]), 2)

    def test_conflicting_named_feedback_blocks_whole_channel(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light status", 9, 1),
            )
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertTrue(
            all(
                s["reason"] == "role_datapoint_type_mismatch" for s in result["skipped"]
            )
        )

    def test_duplicate_role_does_not_pick_first_address(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light switch", 1, 1),
            )
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(
            result["skipped"][0]["reason"], "ambiguous_duplicate_function_role"
        )

    def test_missing_name_never_infers_control_pair(self):
        result = mapper.plan_project(
            project(ga("1/0/1", "", 1, 1), ga("1/0/2", "", 1, 11))
        )
        self.assertNotIn("switch", result["config"])
        self.assertNotIn("light", result["config"])
        self.assertEqual(result["entityCount"], 1)  # Observation-only DPT Status.

    def test_similar_names_do_not_merge_channels(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light 2 status", 1, 11),
            )
        )
        self.assertNotIn("state_address", result["config"]["light"][0])
        self.assertEqual(result["config"]["binary_sensor"][0]["state_address"], "1/0/2")

    def test_formal_ets_function_roles_are_preferred_to_names(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            ga("1/0/2", "B", 1, 11),
            ga("1/0/3", "C", 5, 1),
            ga("1/0/4", "D", 5, 1),
            functions={
                "F1": function(
                    "DimmableLight",
                    SwitchOnOff="1/0/1",
                    InfoOnOff="1/0/2",
                    AbsoluteSetvalueControl="1/0/3",
                    ActualDimmingValue="1/0/4",
                )
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(
            result["config"]["light"][0]["brightness_state_address"], "1/0/4"
        )
        self.assertEqual(result["entities"][0]["source"], "ets-function-role")

    def test_missing_subtype_never_defaults_to_switch(self):
        result = mapper.plan_project(project(ga("1/0/1", "Main light switch", 1, None)))
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "missing_exact_datapoint_type")

    def test_conflicting_object_dpt_cannot_override_group_metadata(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 9, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "conflicting_datapoint_type")

    def test_switch_flags_prove_write_and_unanimous_dpt_can_fill_missing_group_dpt(
        self,
    ):
        group = ga("1/0/1", "Actuator", 1, 1)
        group["dpt"] = None
        p = project(
            group,
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                }
            },
        )
        self.assertEqual(
            mapper.plan_project(p)["config"]["switch"][0]["address"], "1/0/1"
        )

    def test_transmit_only_object_never_becomes_write_control(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "transmit": True, "write": False},
                }
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_climate_requires_actual_and_target_feedback_plus_target_control(self):
        p = project(
            ga("1/0/1", "AC 室內溫度", 9, 1),
            ga("1/0/2", "AC 目標溫度", 9, 1),
            ga("1/0/3", "AC 目標溫度狀態", 9, 1),
            ga("1/0/4", "AC 模式", 20, 105),
            ga("1/0/5", "AC 模式狀態", 20, 105),
            ga("1/0/6", "AC 開關", 1, 1),
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        climate = result["config"]["climate"][0]
        self.assertEqual(climate["temperature_address"], "1/0/1")
        self.assertEqual(climate["target_temperature_state_address"], "1/0/3")
        self.assertEqual(climate["on_off_address"], "1/0/6")
        del p["group_addresses"]["1/0/3"]
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_scene_explicit_structured_number_is_required_and_bounded(self):
        p = project(
            ga("1/0/1", "Scene bus", 18, 1),
            functions={"F1": function("Scene", Scene="1/0/1")},
        )
        for invalid in (None, 0, 65, True, "13"):
            p["functions"]["F1"]["scene_number"] = invalid
            self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        p["functions"]["F1"]["scene_number"] = 13
        self.assertEqual(
            mapper.plan_project(p)["config"]["scene"][0]["scene_number"], 13
        )

    def test_equivalent_address_styles_have_same_identity(self):
        a = mapper.plan_project(project(ga("1/0/1", "A switch", 1, 1)))
        b = mapper.plan_project(project(ga("2049", "A switch", 1, 1)))
        self.assertEqual(a["entities"][0]["uniqueId"], b["entities"][0]["uniqueId"])
        self.assertEqual(b["config"]["switch"][0]["address"], "1/0/1")

    def test_output_is_json_compatible_stable_and_does_not_mutate_input(self):
        p = sample_conventions()
        original = copy.deepcopy(p)
        a = mapper.plan_project(p)
        p["group_addresses"] = dict(reversed(list(p["group_addresses"].items())))
        b = mapper.plan_project(p)
        self.assertEqual(a, b)
        self.assertEqual(json.loads(json.dumps(a)), a)
        self.assertEqual(p, original)
        for entity in a["entities"]:
            item = next(
                c
                for c in a["config"][entity["platform"]]
                if c["name"] == entity["name"]
            )
            self.assertEqual(item["unique_id"], entity["uniqueId"])

    def test_disagreeing_function_claims_reject_both(self):
        p = project(
            ga("1/0/1", "Command", 1, 1),
            ga("1/0/2", "Status", 1, 11),
            functions={
                "F1": function("SwitchableLight", SwitchOnOff="1/0/1"),
                "F2": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="1/0/2"
                ),
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_invalid_address_and_bool_dpt_rejected(self):
        for address in ("32/0/1", "1/8/1", "1/0/256", "0/0/0", "1/-1/1", "__bad__"):
            self.assertEqual(
                mapper.plan_project(project(ga(address, "Main light switch", 1, 1)))[
                    "entityCount"
                ],
                0,
            )
        self.assertEqual(
            mapper.plan_project(project(ga("1/0/1", "Main light switch", True, 1)))[
                "entityCount"
            ],
            0,
        )

    def test_missing_feedback_dpt_blocks_the_whole_named_control(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            ga("1/0/2", "Main light status", 1, None),
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_missing_project_id_keeps_entities_stable_when_an_address_is_added(self):
        p = project(ga("1/0/1", "A switch", 1, 1))
        p.pop("info")
        first = mapper.plan_project(p)
        p["group_addresses"]["3/0/1"] = ga("3/0/1", "Temperature", 9, 1)
        second = mapper.plan_project(p)
        self.assertEqual(first["projectId"], second["projectId"])
        self.assertEqual(
            first["config"]["switch"][0]["unique_id"],
            second["config"]["switch"][0]["unique_id"],
        )

    def test_missing_formal_feedback_reference_cannot_create_partial_control(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            functions={
                "F1": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="no-such-address"
                )
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_invalid_second_function_blocks_previously_valid_command(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            ga("1/0/2", "B", 9, 1),
            functions={
                "F1": function("SwitchableLight", SwitchOnOff="1/0/1"),
                "F2": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="1/0/2"
                ),
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    @unittest.skipUnless(has_ha_distribution(), "HA runtime not installed")
    def test_generated_platforms_pass_the_installed_official_ha_knx_schema(self):
        scene = function("Scene", Scene="4/0/1")
        scene["scene_number"] = 13
        samples = [
            sample_conventions(),
            project(
                ga("5/0/1", "AC 室內溫度", 9, 1),
                ga("5/0/2", "AC 目標溫度", 9, 1),
                ga("5/0/3", "AC 目標溫度狀態", 9, 1),
            ),
            project(ga("4/0/1", "Scene bus", 18, 1), functions={"S1": scene}),
            project(ga("6/0/1", "Relay switch", 1, 1)),
            project(ga("6/0/2", "Status", 1, 11)),
        ]
        plans = [mapper.plan_project(sample) for sample in samples]
        self.assertTrue(all(plan["entityCount"] for plan in plans))
        # Existing isolated component tests stub HA in sys.modules. Run the real
        # validator in a fresh interpreter so those stubs cannot mask a failure.
        script = """
import importlib.util, json, sys, types
from pathlib import Path
from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.const import __version__
base = Path(sys.argv[1])
pkg = types.ModuleType("real_writer_validation")
pkg.__path__ = [str(base)]
sys.modules[pkg.__name__] = pkg
spec = importlib.util.spec_from_file_location(
    pkg.__name__ + ".knx_project_deployment", base / "knx_project_deployment.py"
)
writer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = writer
spec.loader.exec_module(writer)
mode = writer.core_compatibility(__version__)["identityMode"]
validated = set()
for plan in json.load(sys.stdin):
    from xknx.telegram.address import GroupAddress
    address_format = GroupAddress.address_format.name if mode == "legacy" else None
    plan = writer.adapt_plan_identity(plan, mode, address_format)
    result = CONFIG_SCHEMA({"knx": plan["config"]})["knx"]
    for platform, rows in plan["config"].items():
        assert {writer.row_identity(platform, row, address_format) for row in rows} == {
            writer.row_identity(platform, row, address_format)
            for row in result[platform]
        }
        validated.add(platform)
print(json.dumps(sorted(validated)))
"""
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path(__file__).parents[1] / "custom_components/rexlite"),
            ],
            input=json.dumps(plans),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            set(json.loads(process.stdout)),
            {"light", "cover", "sensor", "climate", "scene", "switch", "binary_sensor"},
        )

    def test_entity_limit_rejects_without_partial_silent_truncation(self):
        saved = mapper.MAX_ENTITIES
        try:
            mapper.MAX_ENTITIES = 1
            with self.assertRaisesRegex(ValueError, "entity limit"):
                mapper.plan_project(sample_conventions())
        finally:
            mapper.MAX_ENTITIES = saved


if __name__ == "__main__":
    unittest.main()
