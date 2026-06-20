# 📞 Ring Intercom Video Camera

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/cmos486/ring-intercom-video)](https://github.com/cmos486/ring-intercom-video/releases)

> 🏠 Home Assistant custom integration that adds a **WebRTC live-stream camera** for the **Ring Intercom Handset Video** (2024/2025 model with built-in camera).

The official Ring integration only exposes lock and ding entities for intercoms. This component adds the missing **camera entity with native WebRTC live view** — the same streaming technology Ring uses for its doorbell cameras.

---

## 🧩 The full picture: backend + frontend

This repository is the **backend integration**. There is a companion **Lovelace card** that pairs with it to give you the full intercom experience (video, two‑way audio, open door, hang up) from any HA dashboard.

| Piece | Repo | What it does |
|---|---|---|
| 🛰️ **Integration (this repo)** | [`ring-intercom-video`](https://github.com/cmos486/ring-intercom-video) | Creates the `camera.*` entity and bridges WebRTC signaling between HA and Ring |
| 🎛️ **Lovelace card** | [`ring-intercom-video-card`](https://github.com/cmos486/ring-intercom-video-card) | UI with two‑way audio, pick‑up / hang‑up and open‑door buttons |

You can use this integration on its own (you'll get a working camera entity), but the card is what turns it into a real intercom on your dashboards or wallpanels.

---

## ✨ Why?

The Ring Intercom Video replaces analog intercoms (e.g. Fermax, Tegui, Comelit) and includes a camera that digitizes the analog CVBS video signal. However:

- ❌ The official HA Ring integration doesn't create camera entities for intercoms
- ❌ Standard Ring snapshot/recording APIs don't work for this device (they require Ring Protect)
- ✅ The device **does** support WebRTC live view, using the exact same protocol as Ring doorbells

This component bridges that gap.

---

## 🛠️ How it works

```
Lovelace (browser)  <-->  Camera Entity  <-->  Ring Signaling  <-->  Ring Intercom
   [WebRTC peer]         [SDP/ICE bridge]      [WebSocket]          [720x576 H.264]
```

1. 🖥️ You open the camera card in your dashboard
2. 🌐 Your browser creates a WebRTC connection (SDP offer)
3. 🔀 This component forwards it to Ring's signaling server via `python-ring-doorbell`
4. 📨 Ring returns the SDP answer and ICE candidates
5. 🎥 Your browser connects directly to the Ring device — live video at ~25fps

> ⚡ **No server‑side video processing.** No `aiortc`, no `ffmpeg`, no `Pillow` for the live stream. The browser handles all the WebRTC decoding natively.

---

## 🎛️ Companion Lovelace card

For a full intercom experience, install the companion card:

👉 **[ring-intercom-video-card](https://github.com/cmos486/ring-intercom-video-card)**

What the card adds on top of this integration:

- 📹 **Live video** in any Lovelace dashboard
- 🎤 **Two‑way audio** with a push‑to‑talk button (uses your browser's microphone)
- 📞 **Pick up / Hang up** buttons (clean WebRTC session teardown — releases mic and camera)
- 🔓 **Open door** button (calls `lock.unlock` or any custom service of your choice)
- 🪟 **Pop‑up on a wallpanel** — pairs nicely with [`browser_mod`](https://github.com/thomasloven/hass-browser_mod) so a ding can automatically open the card as a popup on your tablet / wallpanel
- 🌍 **Multi‑language UI** (Spanish, English, Catalan) with auto‑detection
- 🛠️ **Visual editor** — pick entities, no YAML needed

> 💡 **Suggested setup:** integration + card + `browser_mod` automation triggered on `binary_sensor.<intercom>_ding` → instant doorphone on every wallpanel in the house.

---

## 🔌 Device compatibility

| Device | Kind | Supported |
|--------|------|-----------|
| Ring Intercom Handset Video (2024/2025) | `intercom_handset_video` | ✅ Yes |
| Ring Intercom (audio only) | `intercom_handset_audio` | ❌ No (no camera) |

Tested with Fermax 3304/99139 (5‑wire) as the predecessor analog intercom.

---

## ⚠️ Important: camera behavior

The analog intercom camera (Fermax CVBS) only outputs video when activated:

- 🛎️ **During a ding** — someone presses the call button on the street panel
- 🖐️ **Manual activation** — pressing the camera button on the indoor handset

When the camera is not active, the stream shows a **black image**. This is normal — it's how analog intercoms work.

> 💡 **Tip:** If you have a Zigbee/Z‑Wave relay connected to the camera button on your indoor unit, you can trigger the camera via HA automation before opening the stream.

---

## 📥 Installation

### 🟢 HACS (recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations** → click the **⋮ menu** (top right) → **Custom repositories**
3. Add this URL: `https://github.com/cmos486/ring-intercom-video`
4. Category: **Integration**
5. Click **Add**, then search for *"Ring Intercom Video Camera"* and download it
6. **Restart** Home Assistant

### 🔧 Manual

1. Copy the `custom_components/ring_intercom_camera/` folder to your HA `custom_components/` directory
2. **Restart** Home Assistant

---

## ⚙️ Configuration

Everything is done from the UI — no YAML needed:

1. Go to **Settings → Devices & Services**
2. Click **➕ Add Integration** and search for *"Ring Intercom Video Camera"*
3. Confirm the dialog — no credentials are asked, the integration reuses the official Ring integration's auth

The component will **auto‑discover** `intercom_handset_video` devices from your existing Ring integration, and a new camera entity will appear: `camera.<device_name>_camera` 🎉

> 🔄 **Upgrading from YAML?** Existing `ring_intercom_camera:` entries in `configuration.yaml` are **imported automatically** into a UI config entry on the next restart. A repair issue will remind you to remove the now-obsolete YAML line.

---

## 📋 Prerequisites

- ✅ The official **[Ring](https://www.home-assistant.io/integrations/ring/)** integration must be configured and working in HA
- ✅ Your Ring account must have an **Intercom Handset Video** device
- ✅ HACS installed (for HACS installation method)

---

## 🔐 Authentication

> **You don't need to configure any credentials.** This component reuses the authentication from the official Ring integration already set up in your Home Assistant.

Under the hood:

- 🔑 The official Ring integration handles all login / OAuth / 2FA via its config flow (Settings → Integrations → Ring)
- 🔗 This component declares `ring` as a dependency and accesses the already‑authenticated Ring API client from `hass.data["ring"]`
- 🚫 No tokens, passwords, or API keys are stored or managed by this component

If your Ring integration is working (you can see your intercom's lock and ding entities), this component will work too — no extra login needed.

---

## 🎨 Dashboard setup

You have two options:

- 🎛️ **Use the companion [Ring Intercom Video Card](https://github.com/cmos486/ring-intercom-video-card)** *(recommended)* — full intercom UX with video + two‑way audio + open door + hang up.
- 📷 **Use a built‑in card** — any Picture Entity or Camera card. When you click the live view button, WebRTC streaming starts automatically. (Video only — no two‑way audio.)

---

## 🧪 Technical details

This component:

1. 🐒 **Monkey‑patches `RingOther`** (the intercom class in `python-ring-doorbell`) to add WebRTC streaming methods — the same methods that `RingDoorBell` has for doorbell cameras
2. 🎥 **Creates a camera entity** with `CameraEntityFeature.STREAM` that implements the HA WebRTC signaling interface (`async_handle_async_webrtc_offer`, `async_on_webrtc_candidate`, `close_webrtc_session`)
3. 🛰️ Uses `python-ring-doorbell`'s `RingWebRtcStream` for all signaling — no custom WebSocket/HTTP code needed

---

## 🎬 Services

### `ring_intercom_camera.record`

Records a video clip server-side (no browser needed) and saves it as an MP4 file. The standard `camera.record` service **does not work** with this camera — it requires an RTSP/HLS `stream_source`, and this camera is WebRTC-only. This service opens the same server-side WebRTC session used for snapshots and writes the video track to disk with `aiortc`'s `MediaRecorder`.

| Field | Required | Default | Description |
|---|---|---|---|
| `entity_id` (target) | ✅ | — | The intercom camera entity |
| `filename` | ✅ | — | Full path of the output MP4. Must be in an allowed path (e.g. under `/media`) |
| `duration` | ❌ | `20` | Clip length in seconds (1–300) |

```yaml
# Example: record 20 s when someone rings
- action: ring_intercom_camera.record
  data:
    entity_id: camera.portal_camera
    filename: '/media/local/cameras/portal/video-{{ now().strftime("%Y%m%d-%H%M%S") }}.mp4'
    duration: 20
```

> 💡 Like the live view, the analog camera only outputs video while activated (ding or handset camera button) — trigger the recording on `binary_sensor.<intercom>_ding` for best results. If no video arrives, the service raises an error and no file is left behind.

---

## 🔧 Troubleshooting

**❓ No camera entity appears after restart**
- Check that the official Ring integration is working (Settings → Integrations → Ring)
- Verify your device is an `intercom_handset_video` (not `intercom_handset_audio`)
- Check HA logs for `ring_intercom_camera` entries

**❓ Live view shows black / no video**
- This is expected when the Fermax camera is not active
- The analog camera only outputs video during a ding or manual activation
- Try pressing the call button on the street panel, then open the live view

**❓ Live view button doesn't appear**
- Make sure you're using a browser that supports WebRTC (Chrome, Firefox, Safari, Edge — all current versions)
- Check that `CameraEntityFeature.STREAM` is listed in the entity attributes

**❓ Two‑way audio doesn't work / microphone is silent**
- The browser requires a **secure context** (HTTPS) to access the microphone
- Use Nabu Casa, a reverse proxy with Let's Encrypt, or a native HA HTTPS certificate
- Also make sure you're using the [companion card](https://github.com/cmos486/ring-intercom-video-card) — built‑in HA camera cards do not implement two‑way audio

**❓ Ring Protect subscription**
- **Not required.** This component uses WebRTC live view which works without any subscription
- Snapshots and recordings stored in the cloud do require Ring Protect, but this component doesn't use those APIs

---

## 🚀 Improvements in this fork

The following features were designed and developed by **Andoni Sánchez ([@asanchezed](https://github.com/asanchezed))** on top of the original integration:

| Version | Improvement | What it adds |
|---|---|---|
| **v0.5.0** | 🧭 **UI config flow** | Migrated from YAML to a guided **Settings → Devices & Services** setup (single config entry, no credentials). Existing `ring_intercom_camera:` YAML is **auto-imported** into a config entry, with a repair issue reminding you to remove the obsolete YAML. Includes `en`/`es` translations. |
| **v0.6.0** | 🎬 **`ring_intercom_camera.record` service** | Server-side **MP4 recording** with no browser open. Works around the standard `camera.record` service, which needs an RTSP/HLS `stream_source` this WebRTC-only camera doesn't have — uses `aiortc`'s `MediaRecorder` to write the video track to disk. |
| **v0.6.1** | ⏱️ **Snapshot timeout fix** | Bounds the server-side snapshot session so `camera.snapshot` / `async_camera_image()` reliably returns within HA's 10 s `CAMERA_IMAGE_TIMEOUT`. |
| **v0.6.2** | 🔗 **Shared WebRTC session** | The intercom only allows **one live-view session** at a time. The snapshot cache is now fed from the recording's own video track via a `MediaRelay`, so snapshots work *during* a recording instead of conflicting with it. |
| **v0.6.3** | 🧱 **Whole-session time budget** | Bounds the **entire** server-side session (ticket fetch + websocket handshake + signaling + frame decode) from the very start, with a bounded `recv()` tail and `pc.close()` teardown, keeping it safely under HA's 10 s budget. |

> These build on the original WebRTC live-stream camera and server-side snapshot work by **Kilian Ubeda Cano ([@cmos486](https://github.com/cmos486))**.

---

## 🙏 Attribution

This integration builds on top of:

- 📚 **[python-ring-doorbell](https://github.com/python-ring-doorbell/python-ring-doorbell)** (LGPL‑3.0) — Python library for Ring devices. This component monkey‑patches its `RingOther` class at runtime to add WebRTC stream support for intercom devices.
- 🏠 **[Home Assistant Ring Integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/ring)** (Apache 2.0) — The camera entity's WebRTC signaling interface is modeled after the official Ring camera implementation in HA Core.

---

## 📄 License

[Apache License 2.0](LICENSE)

Copyright 2026 Kilian Ubeda Cano
