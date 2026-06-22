"""Diagnostics support for Ring Intercom Video Camera."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import RingIntercomConfigEntry

RING_DOMAIN = "ring"
INTERCOM_KIND = "intercom_handset_video"
TO_REDACT = {"device_api_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RingIntercomConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)

    ring_entries: list[dict[str, Any]] = []
    intercoms: list[dict[str, Any]] = []
    for ring_entry in hass.config_entries.async_entries(RING_DOMAIN):
        ring_entries.append(
            {"entry_id": ring_entry.entry_id, "state": str(ring_entry.state)}
        )
        ring_data = getattr(ring_entry, "runtime_data", None)
        if ring_data is None:
            continue
        try:
            for device in ring_data.devices.other:
                if device.kind == INTERCOM_KIND:
                    intercoms.append(
                        {
                            "name": device.name,
                            "device_api_id": device.device_api_id,
                            "kind": device.kind,
                        }
                    )
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            intercoms.append({"error": "could not read ring runtime_data.devices"})

    data: dict[str, Any] = {
        "ring_other_patched": getattr(runtime, "patched", None),
        "ring_entries": ring_entries,
        "intercoms": intercoms,
    }
    return async_redact_data(data, TO_REDACT)
