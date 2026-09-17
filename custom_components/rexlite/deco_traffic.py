"""Read Deco reports without Home Assistant's cached state serialization."""

from __future__ import annotations

from typing import Any

DECO_ATTRIBUTES = (
    "device_type",
    "mac",
    "deco_mac",
    "friendly_name",
    "ui_device_name",
    "deco_device",
    "device_model",
    "master",
    "connection_type",
    "interface",
    "down_kilobytes_per_s",
    "up_kilobytes_per_s",
)


def deco_state_reports(hass: Any, registry: Any) -> list[dict]:
    """Read values and actual report timestamps together, without polling routers.

    HA updates State.last_reported on identical reports but can retain an older
    serialized as_dict_json. A quiet online client must not expire merely because
    its speed stays zero. Do not substitute request time for the report time.
    """
    reports = []
    for entity in registry.entities.values():
        if entity.platform != "tplink_deco" or not entity.entity_id.startswith(
            "device_tracker."
        ):
            continue
        state = hass.states.get(entity.entity_id)
        if state is None:
            continue
        stamp = getattr(state, "last_reported", None) or state.last_updated
        reports.append(
            {
                "entity_id": entity.entity_id,
                "state": state.state,
                "last_reported": stamp.isoformat(),
                "attributes": {
                    key: state.attributes[key]
                    for key in DECO_ATTRIBUTES
                    if key in state.attributes
                },
            }
        )
    return reports
