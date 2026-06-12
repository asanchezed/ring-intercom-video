"""Camera platform for Ring Intercom Video.

Three modes of operation:
1. LIVE STREAM (browser WebRTC) — user opens camera card in Lovelace,
   browser establishes WebRTC peer connection directly to Ring.
   This entity acts as signaling bridge only.

2. SNAPSHOT (server-side WebRTC) — camera.snapshot service or
   async_camera_image() triggers a server-side WebRTC connection
   using aiortc, captures a stabilized video frame, returns JPEG.
   Works from automations without needing a browser open.

3. RECORD (server-side WebRTC) — ring_intercom_camera.record service
   opens a server-side WebRTC connection and writes the video track
   to an MP4 file for the requested duration. The standard
   camera.record service can't be used: it requires an RTSP/HLS
   stream_source, which a WebRTC-only camera doesn't have.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from functools import partial
from io import BytesIO
from typing import Any

import voluptuous as vol
from ring_doorbell.webrtcstream import RingWebRtcMessage

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_DURATION, ATTR_FILENAME, SERVICE_RECORD

_LOGGER = logging.getLogger(__name__)

RING_DOMAIN = "ring"

# Server-side snapshot capture settings
SNAPSHOT_MAX_FRAMES = 75         # Max frames to examine (~3s at 25fps)
SNAPSHOT_BRIGHTNESS_THRESHOLD = 25  # Min brightness to consider "real" video
SNAPSHOT_STABILIZE_FRAMES = 5    # Consecutive bright frames before capture
SNAPSHOT_CACHE_SECONDS = 10      # Don't re-capture within this window
# HA's camera.snapshot service gives async_camera_image() 10 s total
# (CAMERA_IMAGE_TIMEOUT in homeassistant.components.camera), so the whole
# session — ticket + signaling + frames — must finish inside that budget.
SNAPSHOT_SESSION_MAX_SECONDS = 8
SNAPSHOT_FRAME_TIMEOUT = 5       # Max wait for a single frame

# Server-side clip recording settings
RECORD_DEFAULT_DURATION = 20     # Default clip length (seconds)
RECORD_MAX_DURATION = 300        # Max clip length accepted by the service
RECORD_SETUP_MARGIN = 30         # Extra wall time allowed for session setup


def _remove_quietly(path: str) -> None:
    """Remove a file, ignoring errors (e.g. it was never created)."""
    try:
        os.remove(path)
    except OSError:
        pass


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ring Intercom camera entities from a config entry."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RECORD,
        {
            vol.Required(ATTR_FILENAME): cv.string,
            vol.Optional(ATTR_DURATION, default=RECORD_DEFAULT_DURATION): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=RECORD_MAX_DURATION)
            ),
        },
        "async_record_clip",
    )

    ring_entries = hass.config_entries.async_entries(RING_DOMAIN)
    if not ring_entries:
        _LOGGER.warning("Ring integration not configured")
        return

    entities = []
    for ring_entry in ring_entries:
        if ring_entry.state is not ConfigEntryState.LOADED:
            continue
        ring_data = getattr(ring_entry, "runtime_data", None)
        if ring_data is None:
            continue

        try:
            devices = ring_data.devices
            for device in devices.other:
                if device.kind == "intercom_handset_video":
                    _LOGGER.info(
                        "Found Ring Intercom Video: %s (id: %s)",
                        device.name, device.device_api_id,
                    )
                    entities.append(RingIntercomCamera(device))
        except Exception:
            _LOGGER.exception("Error discovering Ring Intercom Video devices")

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ring Intercom Video camera(s)", len(entities))
    else:
        _LOGGER.info("No Ring Intercom Video devices found")


class RingIntercomCamera(Camera):
    """WebRTC live-stream camera + server-side snapshot for Ring Intercom Video."""

    def __init__(self, device) -> None:
        """Initialize the camera."""
        super().__init__()
        self._device = device
        self._attr_name = f"{device.name} Camera"
        self._attr_unique_id = f"ring_intercom_camera_{device.device_api_id}"
        self._attr_brand = "Ring"
        self._attr_model = "Intercom Video"
        self._attr_supported_features = CameraEntityFeature.STREAM

        # Snapshot cache
        self._last_image: bytes | None = None
        self._last_image_time: float = 0
        self._capturing: bool = False

        # Clip recording state
        self._recording: bool = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def motion_detection_enabled(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device.device_api_id,
            "device_kind": self._device.kind,
            "stream_method": "webrtc_native",
            "last_snapshot": self._last_image_time or None,
        }

    # ---- Snapshot (server-side WebRTC capture) ----

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture a snapshot via server-side WebRTC.

        Returns cached image if recent, otherwise starts a new
        WebRTC session with aiortc to grab a stabilized frame.
        """
        # Return cache if fresh
        if (
            self._last_image
            and (time.time() - self._last_image_time) < SNAPSHOT_CACHE_SECONDS
        ):
            return self._last_image

        # Avoid concurrent captures
        if self._capturing:
            return self._last_image

        self._capturing = True
        try:
            image = await self._capture_snapshot()
            if image and len(image) > 500:
                self._last_image = image
                self._last_image_time = time.time()
                _LOGGER.debug(
                    "Snapshot captured for %s (%d bytes)",
                    self._device.name, len(image),
                )
        except Exception:
            _LOGGER.exception("Snapshot capture failed for %s", self._device.name)
        finally:
            self._capturing = False

        return self._last_image

    async def _capture_snapshot(self) -> bytes | None:
        """Server-side WebRTC snapshot using aiortc."""
        try:
            from aiortc import RTCPeerConnection
        except ImportError:
            _LOGGER.error(
                "aiortc not available — snapshot capture requires aiortc. "
                "It should be installed automatically via requirements."
            )
            return None

        pc = RTCPeerConnection()
        # "frame" holds the best PIL image seen so far, published incrementally
        # so a session/HA timeout still returns the best frame instead of None
        snapshot_data: dict[str, Any] = {"frame": None}
        capture_done = asyncio.Event()

        @pc.on("track")
        async def on_track(track):
            if track.kind != "video":
                return

            frame_count = 0
            best_brightness = 0.0
            bright_streak = 0
            prev_brightness = 0.0

            try:
                while frame_count < SNAPSHOT_MAX_FRAMES:
                    frame = await asyncio.wait_for(
                        track.recv(), timeout=SNAPSHOT_FRAME_TIMEOUT
                    )
                    frame_count += 1

                    img = frame.to_image()
                    w, h = img.size
                    # Sample 9 points for brightness
                    points = [
                        (w // 4, h // 4), (w // 2, h // 4), (3 * w // 4, h // 4),
                        (w // 4, h // 2), (w // 2, h // 2), (3 * w // 4, h // 2),
                        (w // 4, 3 * h // 4), (w // 2, 3 * h // 4), (3 * w // 4, 3 * h // 4),
                    ]
                    total = sum(sum(img.getpixel(p)) / 3 for p in points)
                    brightness = total / len(points)

                    if brightness > best_brightness:
                        best_brightness = brightness
                        snapshot_data["frame"] = img

                    # Wait for stabilized frame
                    if brightness > SNAPSHOT_BRIGHTNESS_THRESHOLD:
                        bright_streak += 1
                        if (
                            bright_streak >= SNAPSHOT_STABILIZE_FRAMES
                            and prev_brightness > 0
                            and abs(brightness - prev_brightness)
                            < brightness * 0.15
                        ):
                            snapshot_data["frame"] = img
                            break
                    else:
                        bright_streak = 0

                    prev_brightness = brightness

            except asyncio.TimeoutError:
                _LOGGER.debug("Frame timeout after %d frames", frame_count)
            except Exception as exc:
                _LOGGER.debug("Frame capture error: %s", exc)

            capture_done.set()

        await self._run_webrtc_session(
            pc, done=capture_done, max_seconds=SNAPSHOT_SESSION_MAX_SECONDS
        )

        if (frame := snapshot_data["frame"]) is not None:
            buf = BytesIO()
            frame.save(buf, "JPEG", quality=85)
            return buf.getvalue()
        return None

    async def _run_webrtc_session(
        self, pc, *, done: asyncio.Event, max_seconds: float
    ) -> None:
        """Drive a server-side WebRTC session over Ring signaling.

        Track handlers must be registered on ``pc`` before calling.
        Runs until ``done`` is set, the remote closes, or ``max_seconds``
        elapses; always closes the peer connection on the way out.
        """
        from aiortc import RTCSessionDescription
        from ring_doorbell.const import (
            APP_API_URI,
            RTC_STREAMING_TICKET_ENDPOINT,
            RTC_STREAMING_WEB_SOCKET_ENDPOINT,
        )

        import json
        import ssl
        import uuid

        from websockets.asyncio.client import connect as ws_connect

        # 1. Get signaling ticket
        try:
            resp = await self._device._ring.async_query(
                RTC_STREAMING_TICKET_ENDPOINT,
                method="POST",
                base_uri=APP_API_URI,
            )
            ticket = resp.json()["ticket"]
        except Exception:
            _LOGGER.debug("Failed to get WebRTC ticket", exc_info=True)
            await pc.close()
            return

        # 2. Setup peer connection offer
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # 3. WebSocket signaling
        ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
        dialog_id = str(uuid.uuid4())
        session_id = None

        ssl_ctx = ssl.create_default_context()

        try:
            async with ws_connect(
                ws_uri,
                user_agent_header="android:com.ringapp",
                ssl=ssl_ctx,
            ) as ws:
                await ws.send(json.dumps({
                    "method": "live_view",
                    "dialog_id": dialog_id,
                    "body": {
                        "doorbot_id": self._device.device_api_id,
                        "stream_options": {
                            "audio_enabled": False,
                            "video_enabled": True,
                        },
                        "sdp": pc.localDescription.sdp,
                        "type": "offer",
                    },
                }))

                start = time.time()
                while time.time() - start < max_seconds and not done.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3)
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        body = msg.get("body", {})

                        if method == "sdp":
                            sdp = body.get("sdp", "")
                            if sdp:
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(
                                        sdp=sdp, type="answer"
                                    )
                                )
                        elif method == "session_created":
                            session_id = body.get("session_id")
                        elif (
                            method == "notification"
                            and body.get("text") == "camera_connected"
                        ):
                            if session_id:
                                await ws.send(json.dumps({
                                    "method": "activate_session",
                                    "dialog_id": dialog_id,
                                    "body": {
                                        "doorbot_id": self._device.device_api_id,
                                        "session_id": session_id,
                                    },
                                }))
                        elif method == "close":
                            break
                    except asyncio.TimeoutError:
                        if done.is_set():
                            break

                # Clean close
                try:
                    await ws.send(json.dumps({
                        "method": "close",
                        "dialog_id": dialog_id,
                        "body": {
                            "session_id": session_id or "",
                            "reason": {"code": 0, "text": ""},
                        },
                    }))
                except Exception:
                    pass

        except Exception:
            _LOGGER.debug("WebRTC signaling error", exc_info=True)
        finally:
            await pc.close()

    # ---- Record (server-side WebRTC capture to MP4) ----

    async def async_record_clip(self, filename: str, duration: int) -> None:
        """Record a video clip (ring_intercom_camera.record service)."""
        if not self.hass.config.is_allowed_path(filename):
            raise HomeAssistantError(
                f"Cannot write {filename}, no access to path; "
                "add it to allowlist_external_dirs"
            )
        if self._recording:
            raise HomeAssistantError(f"{self.entity_id} is already recording")

        try:
            from aiortc import RTCPeerConnection
            from aiortc.contrib.media import MediaRecorder
        except ImportError as err:
            raise HomeAssistantError(
                "aiortc not available — recording requires aiortc. "
                "It should be installed automatically via requirements."
            ) from err

        await self.hass.async_add_executor_job(
            partial(os.makedirs, os.path.dirname(filename), exist_ok=True)
        )

        pc = RTCPeerConnection()
        # MediaRecorder opens the output container on creation (blocking I/O)
        recorder = await self.hass.async_add_executor_job(MediaRecorder, filename)
        record_done = asyncio.Event()
        recording = {"started": False}

        @pc.on("track")
        async def on_track(track):
            if track.kind != "video" or recording["started"]:
                return
            recording["started"] = True
            recorder.addTrack(track)
            await recorder.start()
            _LOGGER.debug(
                "Recording %s for %d s to %s",
                self.entity_id, duration, filename,
            )
            asyncio.get_running_loop().call_later(duration, record_done.set)

        self._recording = True
        self.async_write_ha_state()
        try:
            await self._run_webrtc_session(
                pc, done=record_done, max_seconds=duration + RECORD_SETUP_MARGIN
            )
        finally:
            self._recording = False
            self.async_write_ha_state()
            try:
                await recorder.stop()
            except Exception:
                _LOGGER.debug("Error finalizing recording", exc_info=True)

        if not recording["started"]:
            await self.hass.async_add_executor_job(_remove_quietly, filename)
            raise HomeAssistantError(
                f"No video received from {self.entity_id}; clip not saved"
            )

        _LOGGER.info(
            "Saved %d s clip from %s to %s", duration, self.entity_id, filename
        )

    # ---- Live stream (browser WebRTC signaling bridge) ----

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle WebRTC offer from the HA frontend."""
        def _message_wrapper(ring_msg: RingWebRtcMessage) -> None:
            if ring_msg.error_code:
                msg = ring_msg.error_message or ""
                send_message(WebRTCError(ring_msg.error_code, msg))
            elif ring_msg.answer:
                send_message(WebRTCAnswer(ring_msg.answer))
            elif ring_msg.candidate:
                send_message(
                    WebRTCCandidate(
                        RTCIceCandidateInit(
                            ring_msg.candidate,
                            sdp_m_line_index=ring_msg.sdp_m_line_index or 0,
                        )
                    )
                )

        await self._device.generate_async_webrtc_stream(
            offer_sdp, session_id, _message_wrapper, keep_alive_timeout=None
        )

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward an ICE candidate from the browser to Ring."""
        if candidate.sdp_m_line_index is None:
            _LOGGER.warning("ICE candidate without sdp_m_line_index, ignoring")
            return
        await self._device.on_webrtc_candidate(
            session_id, candidate.candidate, candidate.sdp_m_line_index
        )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session."""
        self._device.sync_close_webrtc_stream(session_id)
