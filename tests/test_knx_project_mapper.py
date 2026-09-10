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


def abbreviated_light(base="Example-Dali-Light", roles=None):
    """Independent, synthetic model of a six-role actuator plus a display."""
    roles = roles or [
        ("SW", 1, 1),
        ("SW-FB", 1, 11),
        ("VAL", 5, 1),
        ("VAL-FB", 5, 1),
        ("CT", 7, 600),
        ("CT-FB", 7, 600),
    ]
    groups, objects = [], {}
    for number, (role, main, sub) in enumerate(roles, start=1):
        address = f"8/0/{number}"
        groups.append(ga(address, f"{base}-{role}", main, sub))
        objects[f"2.1.5/MD-1_M-1_MI-1_O-{number}_R-{number}"] = {
            "device_address": "2.1.5",
            "channel": "CH-3",
            "text": "Output group A",
            "dpts": [{"main": main, "sub": sub}],
            "flags": {
                "communication": True,
                "read": role.endswith("-FB"),
                "write": not role.endswith("-FB"),
                "transmit": role.endswith("-FB"),
            },
            "group_address_links": [address],
        }
    return project(*groups, objects=objects)


def has_ha_distribution():
    try:
        importlib.metadata.version("homeassistant")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


class KNXProjectMappingTests(unittest.TestCase):
    def test_abbreviated_roles_merge_full_light_with_display_receivers(self):
        p = abbreviated_light()
        # Both group and receiver declarations may use Status while the
        # producing actuator calls its equivalent on/off feedback Switch.
        p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"]["dpts"] = [
            {"main": 1, "sub": 1}
        ]
        p["communication_objects"]["display"] = {
            "group_address_links": ["8/0/2"],
            "dpts": [{"main": 1, "sub": 1}],
            "flags": {"communication": True, "write": True, "transmit": True},
        }
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["mappedAddressCount"], 6)
        light = result["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")
        self.assertEqual(light["brightness_address"], "8/0/3")
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertEqual(light["color_temperature_address"], "8/0/5")
        self.assertEqual(light["color_temperature_state_address"], "8/0/6")
        self.assertEqual(light["color_temperature_mode"], "absolute")
        self.assertEqual(result["entities"][0]["source"], "exact-name-object-channel")

    def test_abbreviated_pair_requires_same_device_and_module_instance(self):
        for incompatible in ("3.1.5", "2.1.5/MD-1_M-1_MI-2_O-2_R-2"):
            with self.subTest(incompatible=incompatible):
                p = abbreviated_light()
                objects = p["communication_objects"]
                obj = objects.pop("2.1.5/MD-1_M-1_MI-1_O-2_R-2")
                if "/" in incompatible:
                    objects[incompatible] = obj
                else:
                    obj["device_address"] = incompatible
                    objects["2.1.5/MD-1_M-1_MI-1_O-2_R-2"] = obj
                result = mapper.plan_project(p)
                self.assertNotIn("state_address", result["config"]["light"][0])
                self.assertEqual(
                    result["config"]["binary_sensor"][0]["state_address"], "8/0/2"
                )

    def test_channel_text_fallback_distinguishes_non_modular_outputs(self):
        p = abbreviated_light()
        objects = p["communication_objects"]
        for number in (1, 2):
            obj = objects.pop(f"2.1.5/MD-1_M-1_MI-1_O-{number}_R-{number}")
            obj["channel"] = None
            obj["text"] = "Channel A"
            objects[f"2.1.5/O-{number}"] = obj
        first = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(first["state_address"], "8/0/2")
        objects["2.1.5/O-2"]["text"] = "Channel B"
        second = mapper.plan_project(p)["config"]["switch"][0]
        self.assertNotIn("state_address", second)

    def test_switch_status_conflict_not_relaxed_without_same_channel_proof(self):
        p = abbreviated_light()
        obj = p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"]
        obj["dpts"] = [{"main": 1, "sub": 1}]
        obj["device_address"] = "3.1.5"
        result = mapper.plan_project(p)
        self.assertNotIn("state_address", result["config"]["light"][0])
        self.assertIn(
            {"address": "8/0/2", "reason": "conflicting_datapoint_type"},
            result["skipped"],
        )

    def test_true_optional_dpt_conflict_does_not_erase_proven_light(self):
        for main, sub in ((5, 4), (9, 1)):
            with self.subTest(dpt=(main, sub)):
                p = abbreviated_light()
                p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-3_R-3"]["dpts"] = [
                    {"main": main, "sub": sub}
                ]
                result = mapper.plan_project(p)
                light = result["config"]["light"][0]
                self.assertEqual(light["state_address"], "8/0/2")
                self.assertNotIn("brightness_address", light)
                self.assertNotIn("brightness_state_address", light)
                self.assertEqual(light["color_temperature_address"], "8/0/5")
                self.assertIn(
                    {"address": "8/0/3", "reason": "conflicting_datapoint_type"},
                    result["skipped"],
                )

    def test_brightness_color_spelling_maps_the_same_as_val_ct(self):
        p = abbreviated_light(
            base="2F-01G",
            roles=[
                ("SW", 1, 1),
                ("SW-FB", 1, 11),
                ("Brightness", 5, 1),
                ("Brightness-FB", 5, 1),
                ("Color", 7, 600),
                ("Color-FB", 7, 600),
            ],
        )
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertEqual(light["color_temperature_address"], "8/0/5")

    def test_terse_name_with_a_proven_brightness_channel_is_a_light(self):
        p = abbreviated_light(
            base="Loop-A CC-1CT",
            roles=[("SW", 1, 1), ("SW-FB", 1, 11), ("Value", 5, 1), ("Value-FB", 5, 1)],
        )
        result = mapper.plan_project(p)
        self.assertNotIn("switch", result["config"])
        light = result["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")

    def test_value_carrying_a_temperature_datapoint_never_becomes_brightness(self):
        p = abbreviated_light(
            base="AC-01",
            roles=[("SW", 1, 1), ("SW-FB", 1, 11), ("Value", 9, 1), ("Value-FB", 9, 1)],
        )
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        switch = result["config"]["switch"][0]
        self.assertEqual(switch["address"], "8/0/1")
        self.assertNotIn("brightness_address", switch)

    def test_relative_color_temperature_maps_with_relative_mode(self):
        p = abbreviated_light()
        for address in ("8/0/5", "8/0/6"):
            p["group_addresses"][address]["dpt"] = {"main": 5, "sub": 1}
            p["communication_objects"][
                f"2.1.5/MD-1_M-1_MI-1_O-{address[-1]}_R-{address[-1]}"
            ]["dpts"] = [{"main": 5, "sub": 1}]
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["color_temperature_address"], "8/0/5")
        self.assertEqual(light["color_temperature_state_address"], "8/0/6")
        self.assertEqual(light["color_temperature_mode"], "relative")

    def test_switch_feedback_tolerates_a_bool_listener_on_the_same_channel(self):
        p = abbreviated_light()
        # The State object stays; a logic-block Bool listener also subscribes.
        p["communication_objects"]["logic"] = {
            "device_address": "2.1.9",
            "group_address_links": ["8/0/2"],
            "dpts": [{"main": 1, "sub": 2}],
            "flags": {"communication": True, "write": True, "transmit": True},
        }
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")

    def test_incompatible_color_temperature_preserves_brightness_channel(self):
        p = abbreviated_light()
        p["group_addresses"]["8/0/5"]["dpt"] = {"main": 5, "sub": 1}
        result = mapper.plan_project(p)
        light = result["config"]["light"][0]
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertNotIn("color_temperature_address", light)
        self.assertNotIn("color_temperature_state_address", light)
        self.assertNotIn("color_temperature_mode", light)

    def test_explicit_ga_selects_declared_alternative_but_never_incompatible_object(
        self,
    ):
        p = abbreviated_light()
        obj = p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-3_R-3"]
        obj["dpts"] = [{"main": 5, "sub": 1}, {"main": 5, "sub": 4}]
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")
        p["communication_objects"]["other"] = {
            "group_address_links": ["8/0/3"],
            "dpts": [{"main": 5, "sub": 4}],
        }
        self.assertNotIn(
            "brightness_address", mapper.plan_project(p)["config"]["light"][0]
        )

    def test_feedback_input_with_write_flag_is_observation_only(self):
        p = project(
            ga("8/1/1", "Example-ReedSensor-ACTIVE-FB", 1, 1),
            objects={
                "contact": {
                    "group_address_links": ["8/1/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {
                        "communication": True,
                        "write": True,
                        "transmit": True,
                        "read": False,
                    },
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertNotIn("switch", result["config"])
        self.assertEqual(result["config"]["binary_sensor"][0]["sync_state"], False)
        p["communication_objects"]["contact"]["flags"]["transmit"] = False
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_duplicate_optional_role_is_not_selected_arbitrarily(self):
        p = abbreviated_light()
        p["group_addresses"]["8/0/7"] = ga("8/0/7", "Example-Dali-Light-VAL", 5, 1)
        result = mapper.plan_project(p)
        light = result["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")
        self.assertNotIn("brightness_address", light)
        self.assertEqual({s["address"] for s in result["skipped"]}, {"8/0/3", "8/0/7"})

    def test_abbreviations_without_actuator_objects_never_join_by_name_alone(self):
        p = abbreviated_light()
        p["communication_objects"] = {}
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])

    def test_fanout_command_never_uses_one_actuators_feedback_as_aggregate(self):
        p = abbreviated_light()
        extra = copy.deepcopy(p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-1_R-1"])
        extra["device_address"] = "2.1.6"
        p["communication_objects"]["2.1.6/MD-1_M-1_MI-1_O-1_R-1"] = extra
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        self.assertNotIn("state_address", result["config"]["switch"][0])

    def test_multiple_feedback_producers_never_pick_first_matching_channel(self):
        p = abbreviated_light()
        extra = copy.deepcopy(p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"])
        extra["device_address"] = "2.1.6"
        p["communication_objects"]["2.1.6/MD-1_M-1_MI-1_O-2_R-2"] = extra
        result = mapper.plan_project(p)
        self.assertNotIn("state_address", result["config"]["light"][0])

    def test_command_only_abbreviation_preserves_existing_switch_fallback(self):
        p = abbreviated_light()
        p["group_addresses"] = {"8/0/1": p["group_addresses"]["8/0/1"]}
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        self.assertEqual(result["config"]["switch"][0]["address"], "8/0/1")

    def test_explicit_sender_scene_parameter_allows_recall_compatible_types(self):
        group = ga("8/2/1", "Example arbitrary scene label", 18, 1)
        group.update(
            scene_number=6,
            scene_number_source="ets-sender-parameter",
            scene_sender_ids=["sender"],
        )
        p = project(
            group,
            objects={
                "sender": {
                    "group_address_links": ["8/2/1"],
                    "dpts": [{"main": 18, "sub": 1}],
                    "flags": {"communication": True, "transmit": True},
                },
                "receiver": {
                    "group_address_links": ["8/2/1"],
                    "dpts": [{"main": 17, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                },
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["config"]["scene"][0]["scene_number"], 6)
        self.assertEqual(result["entities"][0]["source"], "ets-sender-scene-parameter")
        for invalid in (None, 0, 65, True, "6"):
            group["scene_number"] = invalid
            self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        group["scene_number"] = 6
        for change in (
            {"scene_number_source": "name"},
            {"scene_sender_ids": []},
            {"scene_sender_ids": ["not-linked"]},
            {"scene_sender_ids": [None]},
        ):
            broken = copy.deepcopy(p)
            broken["group_addresses"]["8/2/1"].update(change)
            self.assertEqual(mapper.plan_project(broken)["entityCount"], 0)
        for change in (
            {"transmit": False},
            {"communication": False},
        ):
            broken = copy.deepcopy(p)
            broken["communication_objects"]["sender"]["flags"].update(change)
            self.assertEqual(mapper.plan_project(broken)["entityCount"], 0)
        p["communication_objects"]["receiver"]["flags"]["write"] = False
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        p["communication_objects"]["receiver"]["flags"]["write"] = True
        p["communication_objects"]["receiver"]["dpts"] = [{"main": 5, "sub": 1}]
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

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

    def test_shade_close_step_pos_labels_map_one_cover(self):
        result = mapper.plan_project(
            project(
                ga("2/1/0", "Example-MatterShade-Blind-Close", 1, 8),
                ga("2/1/1", "Example-MatterShade-Blind-Step", 1, 7),
                ga("2/1/2", "Example-MatterShade-Blind-POS", 5, 1),
            )
        )
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["skipped"], [])
        cover = result["config"]["cover"][0]
        self.assertEqual(cover["move_long_address"], "2/1/0")
        self.assertEqual(cover["move_short_address"], "2/1/1")
        self.assertEqual(cover["position_address"], "2/1/2")

    def test_shade_close_label_with_wrong_datapoint_type_is_not_a_cover(self):
        # An impulse-relay "Close" (DPT 1.001) is not the bi-directional up/down
        # datapoint; the role guard rejects it instead of inventing a cover.
        result = mapper.plan_project(
            project(
                ga("2/1/0", "Relay-Blind-Close", 1, 1),
                ga("2/1/1", "Relay-Blind-Open", 1, 1),
            )
        )
        self.assertNotIn("cover", result["config"])

    def test_fb_named_binary_address_without_objects_is_a_read_only_sensor(self):
        result = mapper.plan_project(
            project(ga("1/1/0", "Zone-B-DoorSensor-B-Sensor-ACTIVE-FB", 1, 1))
        )
        sensor = result["config"]["binary_sensor"][0]
        self.assertEqual(sensor["state_address"], "1/1/0")
        self.assertIs(sensor["sync_state"], False)
        self.assertNotIn("switch", result["config"])

    def test_fb_name_alone_never_infers_a_sensor_when_objects_disagree(self):
        # A writable command object on the same address still wins over the name.
        p = project(
            ga("1/1/0", "Relay-FB", 1, 1),
            objects={
                "cmd": {
                    "group_address_links": ["1/1/0"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "write": True, "transmit": False},
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertNotIn("binary_sensor", result["config"])

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
            abbreviated_light(),
            project(
                ga("2/1/0", "Example-MatterShade-Blind-Close", 1, 8),
                ga("2/1/1", "Example-MatterShade-Blind-Step", 1, 7),
                ga("2/1/2", "Example-MatterShade-Blind-POS", 5, 1),
            ),
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
