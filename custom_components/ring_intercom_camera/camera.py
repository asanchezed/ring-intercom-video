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
import fractions
import logging
import os
import tempfile
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
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_DURATION,
    ATTR_ENABLE_AUDIO,
    ATTR_ENGINE,
    ATTR_FILENAME,
    ATTR_GAIN,
    ATTR_LANGUAGE,
    ATTR_MEDIA,
    ATTR_MESSAGE,
    ATTR_OPTIONS,
    ATTR_TIMEOUT,
    DOMAIN,
    SERVICE_PLAY_MEDIA,
    SERVICE_RECORD,
    SERVICE_SAY,
)

_LOGGER = logging.getLogger(__name__)

RING_DOMAIN = "ring"

# This camera has no polling (live view is browser-driven, snapshots/records are
# explicit), so updates never need serializing. Declared explicitly per the
# quality-scale `parallel-updates` rule.
PARALLEL_UPDATES = 0

# Server-side snapshot capture settings
SNAPSHOT_MAX_FRAMES = 75         # Max frames to examine (~3s at 25fps)
SNAPSHOT_BRIGHTNESS_THRESHOLD = 25  # Min brightness to consider "real" video
SNAPSHOT_STABILIZE_FRAMES = 5    # Consecutive bright frames before capture
SNAPSHOT_CACHE_SECONDS = 10      # Don't re-capture within this window
# HA's camera.snapshot service gives async_camera_image() 10 s total
# (CAMERA_IMAGE_TIMEOUT in homeassistant.components.camera). This bounds the
# WHOLE server-side session — ticket fetch + websocket handshake + signaling +
# frames — measured from the very start of _run_webrtc_session, leaving headroom
# under 10 s for the bounded ws.recv() tail and the bounded pc.close() teardown.
SNAPSHOT_SESSION_MAX_SECONDS = 7
SNAPSHOT_RECV_TIMEOUT = 1        # Snapshot: poll signaling often, short recv tail
SNAPSHOT_CLOSE_TIMEOUT = 1.5     # Max wait for pc.close() teardown
SNAPSHOT_FRAME_TIMEOUT = 5       # Max wait for a single frame

# Server-side clip recording settings
RECORD_DEFAULT_DURATION = 20     # Default clip length (seconds)
RECORD_MAX_DURATION = 300        # Max clip length accepted by the service
RECORD_SETUP_MARGIN = 30         # Extra wall time allowed for session setup

# Server-side outgoing audio (TTS / media playback) settings
AUDIO_SAMPLE_RATE = 48000        # Opus operates at 48 kHz
AUDIO_FORMAT = "s16"             # 16-bit PCM (what the Opus encoder consumes)
AUDIO_LAYOUT = "stereo"          # Opus encoder layout
AUDIO_PTIME = 0.02               # 20 ms frames (Opus frame size)
AUDIO_SAMPLES_PER_FRAME = int(AUDIO_SAMPLE_RATE * AUDIO_PTIME)  # 960
PLAY_DEFAULT_TIMEOUT = 60        # Max wall time for a standalone play/say session
PLAY_MAX_TIMEOUT = 300           # Upper bound accepted by the services
# Outgoing audio tends to arrive quiet at the panel (HA TTS is low level), so a
# gain boost is applied by default. Hard-clipped to s16 full scale.
AUDIO_DEFAULT_GAIN = 6.0         # Default volume multiplier for say/play_media
AUDIO_MAX_GAIN = 32.0            # Upper bound accepted by the services

# Built lazily so importing this module never pulls in aiortc/av (the browser
# live-stream path needs neither). Cached after first use.
_INJECTOR_CLASS = None


def _get_injector_class():
    """Return the IntercomAudioInjector class, importing aiortc/av lazily."""
    global _INJECTOR_CLASS
    if _INJECTOR_CLASS is not None:
        return _INJECTOR_CLASS

    import numpy as np

    from aiortc import MediaStreamTrack
    from aiortc.contrib.media import MediaPlayer
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.fifo import AudioFifo
    from av.audio.resampler import AudioResampler

    class IntercomAudioInjector(MediaStreamTrack):
        """Outgoing audio track: silence when idle, plays a clip on demand.

        Everything it emits is normalized to s16 / 48 kHz / stereo, 960-sample
        (20 ms) frames with a continuous, monotonically increasing PTS, so the
        downstream Opus encoder never sees a format change or a PTS jump (either
        would glitch the audio). Source clips of any rate/format are resampled
        and re-chunked through an AudioResampler + AudioFifo.

        While a session is alive the track keeps producing frames forever
        (silence between clips) so the RTP flow — and therefore the WebRTC
        session — stays up; this is what lets ``say`` be injected repeatedly
        into a single recording session.
        """

        kind = "audio"

        def __init__(self, hass) -> None:
            super().__init__()
            self._hass = hass
            self._player = None
            self._source = None          # current MediaPlayer.audio track
            self._resampler = None
            self._fifo = None
            self._pts = 0                # monotonic, in samples
            self._start = None           # wall-clock anchor for pacing
            self._audio_frames = 0       # non-silence frames actually pulled out
            self._gain = 1.0             # volume multiplier for the current clip
            # NOTE: must NOT be named self._lock — the pyee EventEmitter base
            # (via MediaStreamTrack) uses self._lock internally as a threading
            # lock for emit()/on(); shadowing it with an asyncio.Lock breaks
            # emit("ended") on stop() with a TypeError.
            self._play_lock = asyncio.Lock()
            self._idle = asyncio.Event()
            self._idle.set()             # idle at start

        @property
        def frames_sent(self) -> int:
            """Count of real (non-silence) audio frames the sender has pulled.

            Stays at 0 if the sender never calls recv() — i.e. Ring answered the
            audio m-line as not send-capable from our side, so nothing is going
            out. Used to report whether audio was actually delivered.
            """
            return self._audio_frames

        async def play(self, media: str, gain: float = 1.0) -> None:
            """Start (or replace) playback of an audio source.

            ``gain`` multiplies the audio level (hard-clipped to s16 full scale)
            so a quiet TTS clip can be made audible on the panel speaker.
            """
            player = await self._hass.async_add_executor_job(MediaPlayer, media)
            if player.audio is None:
                await self._hass.async_add_executor_job(self._stop_player, player)
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="no_audio_track",
                    translation_placeholders={"media": media},
                )
            async with self._play_lock:
                self._stop_source()
                self._player = player
                self._source = player.audio
                self._resampler = AudioResampler(
                    format=AUDIO_FORMAT, layout=AUDIO_LAYOUT, rate=AUDIO_SAMPLE_RATE
                )
                self._fifo = AudioFifo()
                self._gain = max(0.0, min(float(gain), AUDIO_MAX_GAIN))
                self._idle.clear()

        def _apply_gain(self, frame: "AudioFrame") -> "AudioFrame":
            """Scale a frame's PCM by self._gain, hard-clipped to s16 range."""
            if self._gain == 1.0:
                return frame
            arr = frame.to_ndarray().astype(np.float32) * self._gain
            np.clip(arr, -32768.0, 32767.0, out=arr)
            out = AudioFrame.from_ndarray(
                arr.astype(np.int16), format=AUDIO_FORMAT, layout=AUDIO_LAYOUT
            )
            out.sample_rate = AUDIO_SAMPLE_RATE
            return out

        async def wait_idle(self, timeout: float | None = None) -> None:
            """Wait until the current clip has finished playing."""
            await asyncio.wait_for(self._idle.wait(), timeout)

        def stop(self) -> None:
            # Release the MediaPlayer (ffmpeg) too — the base stop() only ends
            # the track, which would otherwise leak the decoder when the session
            # ends before the clip does (e.g. on timeout).
            self._stop_source()
            self._idle.set()
            super().stop()

        @staticmethod
        def _stop_player(player) -> None:
            try:
                if player.audio is not None:
                    player.audio.stop()
            except Exception:  # noqa: BLE001
                pass

        def _stop_source(self) -> None:
            if self._player is not None:
                self._stop_player(self._player)
            self._player = None
            self._source = None
            self._resampler = None
            self._fifo = None

        def _silence_frame(self) -> "AudioFrame":
            frame = AudioFrame(
                format=AUDIO_FORMAT,
                layout=AUDIO_LAYOUT,
                samples=AUDIO_SAMPLES_PER_FRAME,
            )
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            return frame

        async def recv(self) -> "AudioFrame":
            if self.readyState != "live":
                raise MediaStreamError

            if self._start is None:
                self._start = time.time()

            # Snapshot the current source under the lock, then work on the local
            # references only. This keeps the (potentially blocking) source.recv()
            # OUT of the lock, so play() never stalls and a concurrent stop()/play()
            # that nulls/swaps self._* can't crash this in-flight frame.
            async with self._play_lock:
                source = self._source
                resampler = self._resampler
                fifo = self._fifo

            frame = None
            if source is not None and fifo is not None and resampler is not None:
                ended = False
                # MediaPlayer throttles file playback to real time, so this await
                # also paces us; the explicit sleep below only matters for silence
                # and for draining MediaPlayer's pre-buffer.
                while fifo.samples < AUDIO_SAMPLES_PER_FRAME:
                    try:
                        src_frame = await source.recv()
                    except MediaStreamError:
                        # Source exhausted: flush the resampler's internal buffer
                        # so the tail isn't lost, then stop pulling.
                        for resampled in resampler.resample(None):
                            resampled.pts = None
                            fifo.write(resampled)
                        ended = True
                        break
                    for resampled in resampler.resample(src_frame):
                        resampled.pts = None  # fifo owns timing; we re-stamp on out
                        fifo.write(resampled)

                frame = fifo.read(AUDIO_SAMPLES_PER_FRAME)
                if frame is not None:
                    frame = self._apply_gain(frame)  # boost volume (clipped)
                    self._audio_frames += 1
                    # [audio-diag] confirm we're emitting real audio, not silence
                    if self._audio_frames <= 3 or self._audio_frames % 40 == 0:
                        try:
                            peak = int(abs(frame.to_ndarray()).max())
                        except Exception:  # noqa: BLE001
                            peak = -1
                        _LOGGER.debug(
                            "[audio-diag] out frame #%d peak=%d (0=silence, "
                            "~32767=full scale s16)",
                            self._audio_frames, peak,
                        )
                elif ended:
                    # Fully drained (incl. flushed tail); only a sub-20 ms remainder
                    # is left — drop it so every emitted frame is a uniform 960.
                    async with self._play_lock:
                        if self._source is source:  # not already swapped by play()
                            self._stop_source()
                            self._idle.set()

            if frame is None:
                frame = self._silence_frame()

            frame.pts = self._pts
            frame.sample_rate = AUDIO_SAMPLE_RATE
            frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
            self._pts += AUDIO_SAMPLES_PER_FRAME

            # Single real-time pacing point, absolute-anchored so it self-corrects
            # (no cumulative drift) and time spent awaiting the source counts
            # against the wait — never double-paced.
            wait = self._start + self._pts / AUDIO_SAMPLE_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

            return frame

    _INJECTOR_CLASS = IntercomAudioInjector
    return _INJECTOR_CLASS


def _remove_quietly(path: str) -> None:
    """Remove a file, ignoring errors (e.g. it was never created)."""
    try:
        os.remove(path)
    except OSError:
        pass


def _audio_result(delivered: bool, reason: str | None, frames_sent: int = 0) -> dict:
    """Build the response returned by play_media / say.

    ``delivered``    — True if audio frames actually went out to the intercom.
    ``reason``       — None on success, else a machine-readable code:
                       ``recording_without_audio`` (a recording is running but
                       was started without enable_audio), ``no_audio_channel``
                       (the session ran but Ring never pulled any audio), or
                       ``still_playing`` (clip outlived the timeout).
    ``frames_sent``  — number of audio frames pulled (diagnostic).
    """
    return {
        "delivered": delivered,
        "reason": reason,
        "frames_sent": frames_sent,
    }


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
            vol.Optional(ATTR_ENABLE_AUDIO, default=False): cv.boolean,
        },
        "async_record_clip",
    )
    platform.async_register_entity_service(
        SERVICE_PLAY_MEDIA,
        {
            vol.Required(ATTR_MEDIA): cv.string,
            vol.Optional(ATTR_TIMEOUT, default=PLAY_DEFAULT_TIMEOUT): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=PLAY_MAX_TIMEOUT)
            ),
            vol.Optional(ATTR_GAIN, default=AUDIO_DEFAULT_GAIN): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=AUDIO_MAX_GAIN)
            ),
        },
        "async_play_media",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_SAY,
        {
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_LANGUAGE): cv.string,
            vol.Optional(ATTR_ENGINE): cv.string,
            vol.Optional(ATTR_OPTIONS): dict,
            vol.Optional(ATTR_TIMEOUT, default=PLAY_DEFAULT_TIMEOUT): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=PLAY_MAX_TIMEOUT)
            ),
            vol.Optional(ATTR_GAIN, default=AUDIO_DEFAULT_GAIN): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=AUDIO_MAX_GAIN)
            ),
        },
        "async_say",
        supports_response=SupportsResponse.OPTIONAL,
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

    _attr_has_entity_name = True
    _attr_translation_key = "intercom"

    def __init__(self, device) -> None:
        """Initialize the camera."""
        super().__init__()
        self._device = device
        self._attr_unique_id = f"ring_intercom_camera_{device.device_api_id}"
        self._attr_supported_features = CameraEntityFeature.STREAM
        # Attach to the EXISTING Ring device so the camera groups under it
        # instead of floating. The official ring integration registers the
        # device with identifiers={(DOMAIN, device.device_id)} (raw, the mac),
        # so we MUST use the exact same value — using device_api_id or wrapping
        # in str() would create a separate empty device and rename the entity.
        self._attr_device_info = DeviceInfo(
            identifiers={(RING_DOMAIN, device.device_id)},
        )

        # Snapshot cache
        self._last_image: bytes | None = None
        self._last_image_time: float = 0
        self._capturing: bool = False

        # Clip recording state
        self._recording: bool = False

        # Outgoing-audio state. _audio_injector is set whenever an audio-capable
        # session (a recording started with enable_audio, or a standalone
        # play/say) is live, so say/play can reuse that single session instead
        # of opening a second one (the device allows only one live view).
        self._audio_injector: Any | None = None
        self._audio_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Available only while the Ring integration (auth/devices) is loaded."""
        return any(
            entry.state is ConfigEntryState.LOADED
            for entry in self.hass.config_entries.async_entries(RING_DOMAIN)
        )

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
        # While recording, the cache is refreshed live from the recording's
        # own video track (_feed_snapshot_cache) — opening a second WebRTC
        # session would conflict with the device's single live-view session.
        # At the very start of a recording the cache is still cold (or holds a
        # stale frame from a previous session) because no frame has been
        # decoded yet, so wait for a fresh frame instead of returning None.
        if self._recording:
            deadline = time.time() + SNAPSHOT_SESSION_MAX_SECONDS
            while (
                self._recording
                and (time.time() - self._last_image_time) >= SNAPSHOT_CACHE_SECONDS
                and time.time() < deadline
            ):
                await asyncio.sleep(0.25)
            return self._last_image

        # A standalone outgoing-audio session (play/say without recording) holds
        # the single live view; don't open a second session — serve the last
        # cached frame (may be stale or None).
        if self._audio_injector is not None:
            return self._last_image

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
        snapshot_data: dict[str, Any] = {"frame": None, "frames": 0, "track": False}
        capture_done = asyncio.Event()

        @pc.on("track")
        async def on_track(track):
            if track.kind != "video":
                return
            snapshot_data["track"] = True
            _LOGGER.debug("Video track received for %s", self._device.name)

            frame_count = 0
            best_brightness = -1.0  # keep the first frame even if pure black
            bright_streak = 0
            prev_brightness = 0.0

            try:
                while frame_count < SNAPSHOT_MAX_FRAMES:
                    frame = await asyncio.wait_for(
                        track.recv(), timeout=SNAPSHOT_FRAME_TIMEOUT
                    )
                    frame_count += 1
                    snapshot_data["frames"] = frame_count

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
            pc,
            done=capture_done,
            max_seconds=SNAPSHOT_SESSION_MAX_SECONDS,
            recv_timeout=SNAPSHOT_RECV_TIMEOUT,
        )

        if (frame := snapshot_data["frame"]) is not None:
            buf = BytesIO()
            frame.save(buf, "JPEG", quality=85)
            return buf.getvalue()

        if not snapshot_data["track"]:
            _LOGGER.warning(
                "Snapshot failed for %s: WebRTC session ended without a video "
                "track — signaling did not complete within %d s or the HA host "
                "could not establish the media connection (outbound UDP)",
                self._device.name, SNAPSHOT_SESSION_MAX_SECONDS,
            )
        else:
            _LOGGER.warning(
                "Snapshot failed for %s: video track opened but no frame was "
                "decoded (%d frames received)",
                self._device.name, snapshot_data["frames"],
            )
        return None

    async def _feed_snapshot_cache(self, track) -> None:
        """Refresh the snapshot cache (~1 fps) from a live video track.

        Runs while a recording is active so async_camera_image() can serve
        fresh frames without opening a second WebRTC session to the device.
        Exits when the track ends or the task is cancelled.
        """
        last_encode = 0.0
        try:
            while True:
                frame = await track.recv()
                now = time.time()
                if now - last_encode < 1.0:
                    continue
                last_encode = now
                img = frame.to_image()
                buf = BytesIO()
                img.save(buf, "JPEG", quality=85)
                self._last_image = buf.getvalue()
                self._last_image_time = now
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Snapshot cache feed ended", exc_info=True)

    async def _run_webrtc_session(
        self,
        pc,
        *,
        done: asyncio.Event,
        max_seconds: float,
        recv_timeout: float = 3,
        audio_out_track=None,
    ) -> None:
        """Drive a server-side WebRTC session over Ring signaling.

        Track handlers must be registered on ``pc`` before calling.
        Runs until ``done`` is set, the remote closes, or ``max_seconds``
        elapses; always closes the peer connection on the way out.

        ``max_seconds`` bounds the WHOLE session, measured from entry (so the
        ticket fetch and websocket handshake count against it), not just the
        signaling loop. ``recv_timeout`` caps each ``ws.recv()`` so the loop's
        tail past ``max_seconds`` stays small — the snapshot path needs this to
        finish inside HA's 10 s CAMERA_IMAGE_TIMEOUT; recording keeps the default.

        ``audio_out_track`` (optional): an outgoing audio MediaStreamTrack. When
        given, the offer carries a send(recv) audio m-line and ``audio_enabled``
        is set so Ring routes the audio to the intercom's speaker. Must be added
        before the offer is created — WebRTC fixes m-line directions at
        offer/answer time and Ring expects audio in the initial offer (it does
        not renegotiate mid-session). When omitted, the session is receive-only
        (snapshot/record behaviour, unchanged).
        """
        session_start = time.time()
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
            _LOGGER.warning("Failed to get WebRTC signaling ticket", exc_info=True)
            await pc.close()
            return
        _LOGGER.debug("Got WebRTC signaling ticket")

        # 2. Setup peer connection offer
        pc.addTransceiver("video", direction="recvonly")
        if audio_out_track is not None:
            pc.addTrack(audio_out_track)  # outgoing audio (send to intercom)
        else:
            pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        if audio_out_track is not None:
            _LOGGER.debug(
                "[audio-diag] SDP OFFER (audio_out):\n%s", pc.localDescription.sdp
            )

        # 3. WebSocket signaling
        ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
        dialog_id = str(uuid.uuid4())
        session_id = None
        # Ring keeps the device speaker in "stealth" (muted) mode by default.
        # activate_session is liveness only — to actually play server-originated
        # audio the speaker must be un-muted with camera_options{stealth_mode:
        # false} once the session is up. This mirrors ring-client-api's
        # activateCameraSpeaker() and python-ring-doorbell. Only do it when we
        # are actually sending audio (audio_out_track is not None).
        camera_connected = False
        speaker_activated = False
        send_speaker = audio_out_track is not None

        # create_default_context() loads CA certs from disk (blocking I/O); run
        # it in the executor so it doesn't block the event loop.
        ssl_ctx = await self.hass.async_add_executor_job(ssl.create_default_context)

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
                            "audio_enabled": audio_out_track is not None,
                            "video_enabled": True,
                        },
                        "sdp": pc.localDescription.sdp,
                        "type": "offer",
                    },
                }))

                _LOGGER.debug("Signaling websocket connected")
                while time.time() - session_start < max_seconds and not done.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        body = msg.get("body", {})

                        if method == "sdp":
                            sdp = body.get("sdp", "")
                            if sdp:
                                if audio_out_track is not None:
                                    _LOGGER.debug(
                                        "[audio-diag] SDP ANSWER (audio_out):\n%s",
                                        sdp,
                                    )
                                else:
                                    _LOGGER.debug("Received SDP answer")
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(
                                        sdp=sdp, type="answer"
                                    )
                                )
                        elif method == "session_created":
                            session_id = body.get("session_id")
                            _LOGGER.debug("Signaling session created")
                            # camera_connected may arrive before session_created;
                            # if so, un-mute the speaker now that we have a id.
                            if (
                                send_speaker
                                and camera_connected
                                and session_id
                                and not speaker_activated
                            ):
                                await ws.send(json.dumps({
                                    "method": "camera_options",
                                    "dialog_id": dialog_id,
                                    "body": {
                                        "doorbot_id": self._device.device_api_id,
                                        "session_id": session_id,
                                        "stealth_mode": False,
                                    },
                                }))
                                speaker_activated = True
                                _LOGGER.debug(
                                    "[audio-diag] sent camera_options "
                                    "stealth_mode=false"
                                )
                        elif (
                            method == "notification"
                            and body.get("text") == "camera_connected"
                        ):
                            camera_connected = True
                            _LOGGER.debug("Camera connected, activating session")
                            if session_id:
                                await ws.send(json.dumps({
                                    "method": "activate_session",
                                    "dialog_id": dialog_id,
                                    "body": {
                                        "doorbot_id": self._device.device_api_id,
                                        "session_id": session_id,
                                    },
                                }))
                                # Un-mute the panel speaker so server-originated
                                # audio actually plays (the step we were missing).
                                if send_speaker and not speaker_activated:
                                    await ws.send(json.dumps({
                                        "method": "camera_options",
                                        "dialog_id": dialog_id,
                                        "body": {
                                            "doorbot_id": self._device.device_api_id,
                                            "session_id": session_id,
                                            "stealth_mode": False,
                                        },
                                    }))
                                    speaker_activated = True
                                    _LOGGER.debug(
                                        "[audio-diag] sent camera_options "
                                        "stealth_mode=false"
                                    )
                        elif method == "close":
                            break
                    except asyncio.TimeoutError:
                        if done.is_set():
                            break

                _LOGGER.debug(
                    "Signaling session ended after %.1f s (done=%s)",
                    time.time() - session_start, done.is_set(),
                )

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
            _LOGGER.warning("WebRTC signaling error", exc_info=True)
        finally:
            # aiortc's ICE/DTLS shutdown can stall; bound it so async_camera_image()
            # still returns within HA's 10 s CAMERA_IMAGE_TIMEOUT.
            try:
                await asyncio.wait_for(pc.close(), timeout=SNAPSHOT_CLOSE_TIMEOUT)
            except Exception:
                _LOGGER.debug("pc.close() timed out or errored", exc_info=True)

    # ---- Record (server-side WebRTC capture to MP4) ----

    async def async_record_clip(
        self, filename: str, duration: int, enable_audio: bool = False
    ) -> None:
        """Record a video clip (ring_intercom_camera.record service).

        When ``enable_audio`` is True the session is negotiated audio-capable
        (an outgoing audio track is added from the start), so ``say``/
        ``play_media`` can inject audio into this same session while it records.
        It defaults to False: a plain receive-only recording that never sends
        anything to the intercom (no silence on the line).
        """
        if not self.hass.config.is_allowed_path(filename):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
                translation_placeholders={"filename": filename},
            )
        if self._recording:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="already_recording",
                translation_placeholders={"entity_id": self.entity_id},
            )
        if self._audio_injector is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="audio_session_active",
                translation_placeholders={"entity_id": self.entity_id},
            )

        try:
            from aiortc import RTCPeerConnection
            from aiortc.contrib.media import MediaRecorder, MediaRelay
        except ImportError as err:
            raise HomeAssistantError(
                "aiortc not available — recording requires aiortc. "
                "It should be installed automatically via requirements."
            ) from err

        await self.hass.async_add_executor_job(
            partial(os.makedirs, os.path.dirname(filename), exist_ok=True)
        )

        injector = _get_injector_class()(self.hass) if enable_audio else None
        pc = RTCPeerConnection()
        # MediaRecorder opens the output container on creation (blocking I/O)
        recorder = await self.hass.async_add_executor_job(MediaRecorder, filename)
        # Relay duplicates the single video track so the recorder and the
        # snapshot cache can consume frames concurrently without stealing
        # them from each other
        relay = MediaRelay()
        record_done = asyncio.Event()
        recording = {"started": False}
        cache_feed: dict[str, asyncio.Task | None] = {"task": None}

        @pc.on("track")
        async def on_track(track):
            if track.kind != "video" or recording["started"]:
                return
            recording["started"] = True
            recorder.addTrack(relay.subscribe(track))
            await recorder.start()
            cache_feed["task"] = asyncio.create_task(
                self._feed_snapshot_cache(relay.subscribe(track))
            )
            _LOGGER.debug(
                "Recording %s for %d s to %s",
                self.entity_id, duration, filename,
            )
            asyncio.get_running_loop().call_later(duration, record_done.set)

        self._recording = True
        self._audio_injector = injector
        self.async_write_ha_state()
        try:
            await self._run_webrtc_session(
                pc,
                done=record_done,
                max_seconds=duration + RECORD_SETUP_MARGIN,
                audio_out_track=injector,
            )
        finally:
            self._recording = False
            self._audio_injector = None
            if injector is not None:
                injector.stop()
            self.async_write_ha_state()
            if task := cache_feed["task"]:
                task.cancel()
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

    # ---- Outgoing audio (server-side WebRTC TTS / media playback) ----

    async def async_play_media(
        self,
        media: str,
        timeout: int = PLAY_DEFAULT_TIMEOUT,
        gain: float = AUDIO_DEFAULT_GAIN,
    ) -> dict:
        """Play an audio file or URL through the intercom speaker.

        (``ring_intercom_camera.play_media`` service.) ``gain`` boosts the
        volume (hard-clipped). Returns a response with ``delivered`` /
        ``reason`` / ``frames_sent`` so automations can react.
        """
        return await self._send_audio(media, timeout, gain)

    async def async_say(
        self,
        message: str,
        language: str | None = None,
        engine: str | None = None,
        options: dict | None = None,
        timeout: int = PLAY_DEFAULT_TIMEOUT,
        gain: float = AUDIO_DEFAULT_GAIN,
    ) -> dict:
        """Speak a TTS message through the intercom speaker.

        (``ring_intercom_camera.say`` service.) Resolves the message with HA's
        TTS, writes it to a temp file and plays it like ``play_media``. ``gain``
        boosts the volume. Returns the same ``delivered`` / ``reason`` /
        ``frames_sent`` response.
        """
        from homeassistant.components import tts

        media_source_id = tts.generate_media_source_id(
            self.hass,
            message,
            engine=engine,
            language=language,
            options=options,
        )
        extension, data = await tts.async_get_media_source_audio(
            self.hass, media_source_id
        )
        path = await self.hass.async_add_executor_job(
            self._write_temp_audio, data, extension
        )
        try:
            return await self._send_audio(path, timeout, gain)
        finally:
            await self.hass.async_add_executor_job(_remove_quietly, path)

    @staticmethod
    def _write_temp_audio(data: bytes, extension: str) -> str:
        fd, path = tempfile.mkstemp(
            prefix="ring_intercom_tts_", suffix=f".{extension}"
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return path

    async def _send_audio(
        self, media: str, timeout: int, gain: float = 1.0
    ) -> dict:
        """Route audio to the intercom, reusing an active session if there is one.

        The device allows only one live view, so this never opens a second
        session: if a recording (with ``enable_audio``) or another play/say is
        already running, the audio is injected into that session; otherwise a
        dedicated audio-only session is opened. Returns a response dict (see
        :func:`_audio_result`) instead of raising when audio cannot be sent for
        an expected reason, so automations can branch on the result.
        """
        if self._recording and self._audio_injector is None:
            # A recording is running but was started without enable_audio, so no
            # outgoing audio channel exists. Report it instead of raising so the
            # automation can notify.
            _LOGGER.warning(
                "%s: audio not sent — recording is running without enable_audio",
                self.entity_id,
            )
            return _audio_result(False, "recording_without_audio")

        if self._audio_injector is not None:
            return await self._inject(self._audio_injector, media, timeout, gain)

        async with self._audio_lock:
            # A session may have started while we waited for the lock.
            if self._audio_injector is not None:
                return await self._inject(self._audio_injector, media, timeout, gain)
            return await self._play_standalone(media, timeout, gain)

    @staticmethod
    async def _inject(injector, media: str, timeout: int, gain: float = 1.0) -> dict:
        """Feed a clip into an already-running injector and wait for it to finish."""
        mark = injector.frames_sent
        await injector.play(media, gain)
        still_playing = False
        try:
            await injector.wait_idle(timeout)
        except asyncio.TimeoutError:
            # Clip longer than timeout: leave it playing in the host session.
            still_playing = True
            _LOGGER.debug("Audio playback still running after %d s", timeout)

        sent = injector.frames_sent - mark
        if sent <= 0:
            _LOGGER.warning(
                "Audio session produced no outgoing frames — the intercom did "
                "not accept the audio channel"
            )
            return _audio_result(False, "no_audio_channel", 0)
        return _audio_result(True, "still_playing" if still_playing else None, sent)

    async def _play_standalone(
        self, media: str, timeout: int, gain: float = 1.0
    ) -> dict:
        """Open a dedicated audio-only WebRTC session and play one clip."""
        try:
            from aiortc import RTCPeerConnection
        except ImportError as err:
            raise HomeAssistantError(
                "aiortc not available — audio playback requires aiortc."
            ) from err

        injector = _get_injector_class()(self.hass)
        pc = RTCPeerConnection()
        done = asyncio.Event()
        self._audio_injector = injector
        session_task = asyncio.create_task(
            self._run_webrtc_session(
                pc, done=done, max_seconds=timeout, audio_out_track=injector
            )
        )
        try:
            await injector.play(media, gain)
            idle_task = asyncio.create_task(injector.wait_idle())
            # End when the clip finishes, the session closes, or timeout hits.
            await asyncio.wait(
                {idle_task, session_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            idle_task.cancel()
        finally:
            done.set()
            try:
                await asyncio.wait_for(asyncio.shield(session_task), timeout=10)
            except asyncio.CancelledError:
                session_task.cancel()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Audio session teardown error", exc_info=True)
                session_task.cancel()
            self._audio_injector = None
            injector.stop()

        sent = injector.frames_sent
        if sent <= 0:
            _LOGGER.warning(
                "Audio session produced no outgoing frames — the intercom did "
                "not accept the audio channel"
            )
            return _audio_result(False, "no_audio_channel", 0)
        return _audio_result(True, None, sent)

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
