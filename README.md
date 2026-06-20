VoiceOverLoRa (VLoRa)
Real-time push-to-talk voice over a LoRa radio link, delivered natively into ATAK's built-in Voice (Vx) plugin. No internet, no cellular, no additional apps on the end-user device — operators press PTT in ATAK exactly as they normally would, and voice crosses the long-range LoRa link to reach teammates beyond Wi-Fi range.
Relationship to Natak Mesh / Nucleus
This project is an extension of, and depends entirely on, the Nucleus platform built by Natak Mesh. It is not a standalone product and will not function without a working Nucleus deployment.
Nucleus already provides:
A high-speed Wi-Fi mesh carrying full ATAK functionality (SA, chat, voice, video) at close-to-medium range
A parallel long-range LoRa mesh, bridged transparently into ATAK for situational awareness (position, chat, markers) via a built-in CoT bridge daemon
ATAK Voice plugin support natively over the Wi-Fi mesh
What Nucleus does not do out of the box is carry voice over the long-range LoRa link — the LoRa side is built for low-bandwidth telemetry (CoT/SA/chat), not continuous audio streaming. VLoRa adds that missing capability by bridging ATAK's Voice (Vx) plugin traffic into the existing LoRa CoT bridge, encoding it efficiently enough to survive LoRa's bandwidth constraints, and decoding it back into native ATAK Voice on the receiving end.
In short: Nucleus gets you long-range SA and chat over LoRa. VLoRa adds long-range voice on top of that same link.
All credit for the underlying mesh platform, the CoT bridge architecture, and the Nucleus hardware goes to Nathan at Natak Mesh. This project modifies and extends his `cot_bridge.py` daemon and requires a working Nucleus node to run.
What This Solves
ATAK's Voice plugin (Vx) communicates over IP multicast (`239.5.5.1:17501`, RTP/Opus) — this works natively across the Nucleus Wi-Fi mesh, but Opus is far too high-bandwidth for LoRa, which realistically carries somewhere in the range of hundreds of bits to a few kilobits per second depending on radio configuration. VLoRa solves this by:
Intercepting outgoing Vx voice traffic on the transmitting node
Transcoding it from Opus down to Codec2 (a speech codec designed for extremely low bitrates, originally built for HF radio use)
Carrying the Codec2 stream across the existing LoRa mesh link
Transcoding it back to Opus on the receiving node and injecting it into the same ATAK Vx multicast group
The result: an operator presses PTT in ATAK on Node A, speaks, and a teammate on Node B — potentially miles away, with no Wi-Fi connectivity between them — hears it through their normal ATAK Voice plugin. No additional app, no additional radio, no additional PTT button.
Architecture
```
ATAK Vx (Node A)                                    ATAK Vx (Node B)
239.5.5.1:17501 RTP/Opus                             239.5.5.1:17501 RTP/Opus
        │                                                     ▲
        ▼                                                     │
vlora_tx_bridge.py                                   vlora_rx_bridge.py
  - Captures Opus PTT audio                            - Receives Codec2 from cot_bridge
  - Decodes to PCM, resamples to 8kHz                   - Decodes Codec2 → PCM
  - Encodes to Codec2 (3200bps)                          - Resamples to 48kHz
  - Sends to cot_bridge via UDP:4245                     - Encodes to Opus, wraps in RTP
        │                                                     ▲
        ▼                                                     │
cot_bridge.py (modified)                             cot_bridge.py (modified)
  - Existing Nucleus LoRa CoT bridge daemon             - Forwards decoded LoRa voice
  - Extended with a raw Codec2 UDP input                  packets out to UDP:4244
  - Frames and transmits over LoRa port 256
        │                                                     ▲
        └──────────────────── LoRa radio link ───────────────┘
                          (SX1262, 915MHz)
```
Both nodes run an identical software stack. There is no hub/spoke relationship — if one node goes down, the other continues operating independently.
Hardware Requirements
Two or more Nucleus nodes (Raspberry Pi + RAK4631 LoRa module, per Natak Mesh's build)
LoRa radio: SX1262, 915MHz (or regional equivalent), configured per the notes below
ATAK end-user devices (EUDs) connected to each Nucleus node's Wi-Fi access point
Software Components
File	Runs On	Purpose
`shared/cot_bridge.py`	Both nodes	Modified Nucleus LoRa CoT bridge. Adds a raw Codec2 UDP input (port 4245) and forwards decoded LoRa voice to UDP port 4244. All existing CoT/SA/chat functionality is untouched — these are additive changes only.
`alpha/vlora_tx_bridge.py` `bravo/vlora_tx_bridge.py`	Per-node	Captures ATAK Vx RTP/Opus PTT traffic, transcodes to Codec2, forwards to `cot_bridge.py`. Identical logic on both nodes; only the bound local IP differs.
`alpha/vlora_rx_bridge.py` `bravo/vlora_rx_bridge.py`	Per-node	Receives Codec2 voice forwarded by `cot_bridge.py`, transcodes back to Opus/RTP, injects into the local ATAK Vx multicast group. Streaming decode — audio begins playing as packets arrive rather than waiting for the full transmission.
`*/vlora-tx-bridge.service` `*/vlora-rx-bridge.service`	Per-node	systemd units so both bridges start automatically on boot and restart on failure.
The `alpha` and `bravo` folders contain functionally identical scripts — the only difference is the node's local IP address, baked in as a constant (`VX_BIND_IP`, `MCAST_IF_IP`, `LOCAL_NODE_IP`). Deploying to additional nodes means copying either folder and updating those IP constants.
LoRa Radio Configuration — Important
Voice over LoRa is bandwidth-constrained in a way that CoT/SA/chat traffic is not. The spreading factor (SF) chosen for the radio link directly determines whether real-time voice is usable.
Nucleus's stock LoRa configuration is tuned for range and penetration (e.g. SF11), which is correct for infrequent small CoT/SA/chat packets but produces airtime per LoRa packet that is far too slow to carry continuous voice in real time — a few seconds of speech can take 15–20+ seconds to physically clear the air at high SF, with significant packet loss as transmissions queue faster than the radio can send them.
Running voice reliably in real time requires a faster spreading factor (SF7–SF9 range), which trades maximum range/penetration for throughput.
Both nodes on a given LoRa link must use matching SF/bandwidth settings — this is a radio-level parameter affecting every packet on the link, not something configurable per-application.
This is a deliberate range-vs-speed tradeoff that should be made consciously based on your operational distance requirements, not left at the long-range default if real-time voice is a requirement. See the `docs/` notes (or project issues) for LoRa airtime calculations used to inform this tradeoff.
ATAK Device Setup
VLoRa itself requires no plugin installation — it rides entirely on ATAK's Voice (Vx) plugin. However, Vx is a separate plugin from core ATAK and is not bundled by default on every device/build. Setup happens once per device, per mission/channel:
Install the TAK Voice (Vx) plugin on every ATAK device that will use voice. This must be downloaded and installed separately from core ATAK — confirm it is present on each device before attempting to configure a channel; if the Vx plugin is missing, no voice channel options will be available in ATAK.
Connect the ATAK device to the Nucleus node's Wi-Fi access point (each node broadcasts its own AP; connect to whichever node the operator is physically near).
Confirm the device's active network interface is set to `wlan0` (the Nucleus Wi-Fi mesh interface), not a cellular, secondary Wi-Fi, or other network adapter. ATAK and the Vx plugin will bind to whichever interface is selected in ATAK's network preferences — if the device is set to the wrong interface, voice traffic will not reach the Nucleus node at all, even though the device may show as connected to the AP. This is a common silent failure point and should be checked first if voice isn't working on a given device.
Open the Vx (Voice) plugin inside ATAK.
Configure a multicast voice channel with the following settings:
Address: `239.5.5.1`
Port: `17501`
Protocol: `RTP`
These values must match exactly on every device and every node for voice to interoperate — this is the standard Vx multicast address Nucleus and ATAK already use for voice over the Wi-Fi mesh; VLoRa simply extends the same channel over LoRa as well.
Enter the channel — from the channel list, tap the channel name to actively join it. Critical detail: configuring or viewing a channel is not the same as being inside it. The PTT button only appears on the ATAK map, and the device only joins the `239.5.5.1` multicast group (verifiable via IGMP), once the channel has actually been entered. If a device shows no PTT button on the map, it has not joined the voice channel and will neither transmit nor receive VLoRa audio.
Confirm the PTT button is visible on the map. This is the reliable visual indicator that the device is correctly joined to the voice channel and ready to transmit/receive.
If voice fails on a specific device after setup, check in this order: Vx plugin installed → correct network interface (`wlan0`) selected → device actually inside the channel (PTT button visible), not just connected to Wi-Fi.
Once joined, voice usage is identical to standard ATAK Voice plugin operation — press and hold PTT, speak, release. No additional steps, no separate app, no separate channel selection for "LoRa voice" versus "Wi-Fi voice" — it is the same Vx channel, simply extended over a second transport (LoRa) in parallel to Wi-Fi by the Nucleus bridge infrastructure.
Dependencies
System packages (both nodes):
```
ffmpeg
codec2
```
Python (virtual environment recommended):
```
opuslib
pycodec2
soxr
numpy
```
Known Limitations
Voice quality and latency are governed by the underlying LoRa link's spreading factor — see the radio configuration section above. This is a physical constraint of LoRa, not a software limitation.
At long range / high spreading factor, voice will be choppy or significantly delayed; this project does not (and cannot) circumvent LoRa's fundamental throughput ceiling at a given SF.
Both TX and RX bridge implementations must match on both ends of a link — mismatched encoder/decoder implementations (e.g. one node using a subprocess-based Codec2 path while the other uses a different library) can produce corrupted, unintelligible audio even when the radio link itself is healthy. Always deploy matching bridge versions to every node.
Status
Functional, tested bidirectionally between two Nucleus nodes with real-time-capable voice quality at SF7. Field range testing at reduced spreading factor is ongoing.
Acknowledgments
Built on top of, and entirely dependent on, Nucleus by Natak Mesh. This project would not exist without Nathan's underlying mesh and CoT bridge platform — VLoRa is a voice extension layered on his work, not a replacement for or independent alternative to it.
