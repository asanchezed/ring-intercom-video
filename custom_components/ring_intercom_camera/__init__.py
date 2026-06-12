"""Ring Intercom Video Camera integration.

Adds a WebRTC live-stream camera entity for Ring Intercom Video
(intercom_handset_video) devices. The official Ring integration only creates
lock/ding entities for intercoms; this component adds the missing camera.

Architecture:
- Hooks into the existing Ring integration's data/auth
- Monkey-patches RingOther to add WebRTC stream methods (same as RingDoorBell)
- Exposes a native HA WebRTC camera entity (browser does the WebRTC, no aiortc needed)
- When user opens the camera in Lovelace, the browser establishes WebRTC directly
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA]
RING_DOMAIN = "ring"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _patch_ring_other() -> None:
    """Add WebRTC stream methods to RingOther (intercom) class.

    RingOther doesn't inherit from RingDoorBell so it lacks WebRTC methods,
    even though the intercom_handset_video hardware supports WebRTC live view
    via the exact same signaling protocol.
    """
    from ring_doorbell.other import RingOther
    from ring_doorbell.webrtcstream import RingWebRtcStream

    if hasattr(RingOther, "generate_async_webrtc_stream"):
        return  # Already patched

    def _get_streams(self):
        """Lazy-init _webrtc_streams for already-instantiated objects."""
        if not hasattr(self, "_webrtc_streams"):
            self._webrtc_streams = {}
        return self._webrtc_streams

    async def generate_async_webrtc_stream(self, sdp_offer, session_id,
                                            on_message_callback, *,
                                            keep_alive_timeout=60 * 5):
        streams = _get_streams(self)

        async def _close_callback():
            await self.close_webrtc_stream(session_id)

        stream = RingWebRtcStream(
            self._ring,
            self.device_api_id,
            on_message_callback=on_message_callback,
            keep_alive_timeout=keep_alive_timeout,
            on_close_callback=_close_callback,
        )
        streams[session_id] = stream
        await stream.generate(sdp_offer)

    async def on_webrtc_candidate(self, session_id, candidate, multi_line_index):
        streams = _get_streams(self)
        if stream := streams.get(session_id):
            await stream.on_ice_candidate(candidate, multi_line_index)

    async def close_webrtc_stream(self, session_id):
        streams = _get_streams(self)
        stream = streams.pop(session_id, None)
        if stream:
            await stream.close()

    def sync_close_webrtc_stream(self, session_id):
        streams = _get_streams(self)
        stream = streams.pop(session_id, None)
        if stream:
            stream.sync_close()
    RingOther.generate_async_webrtc_stream = generate_async_webrtc_stream
    RingOther.on_webrtc_candidate = on_webrtc_candidate
    RingOther.close_webrtc_stream = close_webrtc_stream
    RingOther.sync_close_webrtc_stream = sync_close_webrtc_stream

    _LOGGER.info("Patched RingOther with WebRTC stream methods")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Handle legacy YAML configuration by importing it as a config entry."""
    if DOMAIN in config:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "deprecated_yaml",
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key="deprecated_yaml",
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data={}
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ring Intercom Video Camera from a config entry."""
    ring_entries = hass.config_entries.async_entries(RING_DOMAIN)
    if not ring_entries:
        raise ConfigEntryNotReady("The Ring integration is not configured")
    if not any(e.state is ConfigEntryState.LOADED for e in ring_entries):
        raise ConfigEntryNotReady("The Ring integration has not finished loading")

    _patch_ring_other()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
