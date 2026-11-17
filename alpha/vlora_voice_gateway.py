#!/usr/bin/env python3
"""
vlora_voice_gateway.py — nucleus-voice (Valhalla headset) <-> ATAK Vx, local to one node

nucleus-voice owns the hardware PTT headset and already sends it over LoRa
(transport "stream": Codec2 3200 frames -> cot_bridge UDP 4245). It never
touches ATAK's Vx channel, and on this node vlora_rx_bridge owns UDP 4244, so
the headset neither reaches local ATAK users nor hears LoRa voice. This
gateway closes both gaps without modifying nucleus-voice, cot_bridge or the
VLoRa bridges.

HEADSET -> ATAK (local tablets hear the headset):
    stream mode: nucleus-voice's 8 B Codec2 frames on lo:4245 are read with a
                 packet socket (copies only; cot_bridge still gets them), decoded,
                 paced at 20 ms and sent as Opus/RTP to 239.5.5.1:17501 from
                 VX_IF_IP. vlora_tx_bridge ignores packets from VX_IF_IP, so
                 nothing is sent over LoRa twice. vlora_tx_bridge sends 144 B
                 packets, so only 8 B packets are taken.
    ip mode:     nucleus-voice's own NVOX PCM frames on its mesh group
                 (node_id == DAEMON_NODE) are forwarded the same way.

ATAK -> HEADSET (earcups hear tablets and LoRa voice):
    Vx Opus/RTP (Wi-Fi tablets + vlora_rx_bridge's LoRa voice) is decoded to
    16 kHz and sent as NVOX frames to nucleus-voice's group with multicast TTL 0
    on MESH_IF_IP, so the copy stays on this node. nucleus-voice mixes it into
    the earcups like any other source. Our own Vx packets are skipped by SSRC.

KEEPS nucleus-voice ON LoRa: nucleus-voice starts on transport "ip" every time
it launches. Every KEEP_EVERY seconds the gateway asks its control socket for
STATUS and, if the transport is not --transport (default "stream"), sets it.
It also logs when the headset is plugged in or unplugged.
(--transport off leaves the setting alone.)

Needs root (packet socket on lo). Run with nucleus-voice in either transport.
If any part of the gateway fails (e.g. the network is not up yet at boot), it
exits so systemd restarts it, rather than running half-working.
"""
import argparse
import json
import logging
import os
import queue
import socket
import struct
import threading
import time

import numpy as np
import opuslib
import pycodec2
import soxr

# ── Defaults for Alpha (0024). Bravo: --vx-if 10.20.25.1 --mesh-if 10.20.1.25 --daemon-node 25
VX_GROUP      = "239.5.5.1"
VX_PORT       = 17501
VX_IF_IP      = "10.20.24.1"
MESH_GROUP    = "239.10.10.1"     # nucleus-voice channel 1 group
MESH_PORT     = 5555
MESH_IF_IP    = "10.20.1.24"
DAEMON_NODE   = 24
CHANNEL       = 1
STREAM_PORT   = 4245              # cot_bridge raw Codec2 input

OWN_SSRC      = 0x56475759        # "VGWY"
SKIP_SSRCS    = {OWN_SSRC, 0x564C4857}   # + vlora_hw_bridge, if ever run
GW_NODE_BASE  = 0xFE00            # NVOX node ids we use: 0xFE00 | (ssrc & 0xFF)

# NVOX (nucleus-voice) wire format
NVOX_MAGIC    = b"NVOX"
NVOX_VERSION  = 1
NVOX_PCM16    = 0
NVOX_HDR      = struct.Struct("<4sBBHBBI")      # magic ver codec node chan flags seq
RATE16        = 16000
FRAME16       = 320                             # 20 ms
RATE48        = 48000
FRAME48       = 960                             # 20 ms

C2_MODE       = 3200
C2_BYTES      = 8
STREAM_END    = 0.5     # s without frames = headset stream over
PREBUFFER     = 0.10    # s queued before Vx output starts (smooths bursty input)

CONTROL_ADDR  = ("127.0.0.1", 5556)  # nucleus-voice control socket
KEEP_EVERY    = 5.0                  # s between transport checks

ETH_P_ALL     = 0x0003
PACKET_OUTGOING = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [vgw] %(message)s")
log = logging.getLogger("vgw")


def db_to_lin(db):
    return 10 ** (db / 20)


def soft_limit(x, gain):
    """Gain, then a soft limiter so loud peaks round off instead of clipping."""
    y = x.astype(np.float32) * gain / 32768.0
    if gain > 1.0:
        y = np.tanh(y * 1.2) / np.tanh(1.2)
    return np.clip(y * 32767.0, -32768, 32767).astype(np.int16)


def rtp_header(seq, ts, ssrc):
    return struct.pack("!BBHII", 0x80, 0x00, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)


def rtp_payload(pkt):
    """-> (ssrc, payload) or None."""
    if len(pkt) < 13 or pkt[0] >> 6 != 2:
        return None
    ssrc = struct.unpack("!I", pkt[8:12])[0]
    hdr = 12 + 4 * (pkt[0] & 0x0F)
    if pkt[0] & 0x10 and len(pkt) >= hdr + 4:
        hdr += 4 + 4 * struct.unpack("!H", pkt[hdr + 2:hdr + 4])[0]
    payload = pkt[hdr:]
    return (ssrc, payload) if payload else None


def lo_udp(frame, port):
    """Ethernet-framed packet from lo -> UDP payload to `port`, or None."""
    if len(frame) < 14 + 20 + 8 or frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    if ip[0] >> 4 != 4 or ip[9] != 17:
        return None
    ihl = (ip[0] & 0x0F) * 4
    udp = ip[ihl:]
    if len(udp) < 8 or struct.unpack("!H", udp[2:4])[0] != port:
        return None
    ulen = struct.unpack("!H", udp[4:6])[0]
    return bytes(udp[8:ulen])


# ═══════════════════════════════════════════════════════════════════════════
#  Headset -> Vx
# ═══════════════════════════════════════════════════════════════════════════

class VxOut:
    """Paced Opus/RTP sender: PCM48 in (any chunking), one 20 ms packet out per tick."""

    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(args.vx_if))
        self.enc = opuslib.Encoder(RATE48, 1, opuslib.APPLICATION_VOIP)
        self.gain = db_to_lin(args.tx_gain)
        self.buf = np.zeros(0, dtype=np.int16)
        self.lock = threading.Lock()
        self.seq = 0
        self.ts = 0
        self.playing = False
        self.last_in = 0.0
        self.sent = 0
        threading.Thread(target=self._clock, daemon=True).start()

    def push(self, pcm48):
        with self.lock:
            self.buf = np.concatenate([self.buf, pcm48])
            self.last_in = time.monotonic()

    def _clock(self):
        nxt = time.monotonic()
        while True:
            nxt += 0.02
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.monotonic()
            with self.lock:
                have = len(self.buf)
                if not self.playing:
                    if have >= int(PREBUFFER * RATE48):
                        self.playing = True
                        self.sent = 0
                    else:
                        continue
                if have < FRAME48:
                    if time.monotonic() - self.last_in > STREAM_END:
                        self.playing = False
                        self.buf = np.zeros(0, dtype=np.int16)
                        log.info(f"headset -> Vx ended ({self.sent * 0.02:.1f}s sent)")
                    continue
                frame = self.buf[:FRAME48]
                self.buf = self.buf[FRAME48:]
            frame = soft_limit(frame, self.gain)
            try:
                pkt = self.enc.encode(frame.tobytes(), FRAME48)
                self.sock.sendto(rtp_header(self.seq, self.ts, OWN_SSRC) + pkt,
                                 (VX_GROUP, VX_PORT))
            except Exception as e:
                log.error(f"Vx send failed: {e}")
            self.seq += 1
            self.ts += FRAME48
            self.sent += 1


def sniff_stream(args, vx):
    """nucleus-voice stream mode: copy its 8 B Codec2 frames off lo:4245."""
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    s.bind(("lo", 0))
    log.info(f"headset (stream mode): reading 8 B Codec2 frames on lo:{args.stream_port}")
    dec = None
    rs = None
    last = 0.0
    frames = 0
    while True:
        s.settimeout(0.25)
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            if dec and time.monotonic() - last > STREAM_END:
                log.info(f"headset stream ended ({frames} Codec2 frames)")
                dec = rs = None
            continue
        if addr[2] == PACKET_OUTGOING:          # lo shows each packet twice
            continue
        payload = lo_udp(data, args.stream_port)
        if payload is None or len(payload) != C2_BYTES:
            continue                             # 144 B = vlora_tx_bridge; not ours
        if dec is None:
            dec = pycodec2.Codec2(C2_MODE)
            rs = soxr.ResampleStream(8000, RATE48, 1, dtype="int16", quality="MQ")
            frames = 0
            log.info("headset stream started (Codec2 -> Vx)")
        last = time.monotonic()
        frames += 1
        pcm8 = np.asarray(dec.decode(payload), dtype=np.int16)
        vx.push(rs.resample_chunk(pcm8))


def mesh_from_daemon(args, vx):
    """nucleus-voice ip mode: forward its own NVOX PCM frames to Vx."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", MESH_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(args.mesh_group) + socket.inet_aton(args.mesh_if))
    except OSError as e:
        log.warning(f"ip-mode forwarding off (cannot listen on {MESH_PORT}: {e})")
        return
    log.info(f"headset (ip mode): NVOX node {args.daemon_node} on "
             f"{args.mesh_group}:{MESH_PORT}")
    rs = None
    last = 0.0
    while True:
        s.settimeout(0.25)
        try:
            data, _ = s.recvfrom(2048)
        except socket.timeout:
            if rs and time.monotonic() - last > STREAM_END:
                rs = None
            continue
        if len(data) != NVOX_HDR.size + FRAME16 * 2:
            continue
        magic, ver, codec, node, chan, _f, _seq = NVOX_HDR.unpack_from(data)
        if (magic, ver, codec) != (NVOX_MAGIC, NVOX_VERSION, NVOX_PCM16):
            continue
        if node != args.daemon_node or chan != args.channel:
            continue
        if rs is None:
            rs = soxr.ResampleStream(RATE16, RATE48, 1, dtype="int16", quality="MQ")
        last = time.monotonic()
        vx.push(rs.resample_chunk(np.frombuffer(data[NVOX_HDR.size:], dtype=np.int16)))


# ═══════════════════════════════════════════════════════════════════════════
#  Vx -> headset
# ═══════════════════════════════════════════════════════════════════════════

class AGC:
    """Automatic gain for one received stream: aims speech at `target_db`,
    boosts quiet senders up to `max_db`, turns loud ones down (to -12 dB),
    then soft-limits peaks. Gain drops fast and rises slowly, so a loud
    message is never driven into the limiter."""

    FLOOR = 10 ** (-60 / 20)        # below this a packet is treated as silence

    def __init__(self, target_db, max_db):
        self.target = 10 ** (target_db / 20)
        self.gmax = 10 ** (max_db / 20)
        self.gmin = 10 ** (-12 / 20)
        self.env = None
        self.g = 1.0

    def process(self, pcm):
        x = pcm.astype(np.float32) / 32768.0
        r = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        if r > self.FLOOR:
            first = self.env is None
            if first:
                self.env = r
            else:
                a = 0.4 if r > self.env else 0.05
                self.env += (r - self.env) * a
            want = min(max(self.target / self.env, self.gmin), self.gmax)
            if first or want < self.g:        # first speech, or louder: set at once
                self.g = want
            else:                             # quieter: ease up slowly
                self.g += (want - self.g) * 0.1
        y = np.tanh(x * self.g * 1.2) / np.tanh(1.2)
        return np.clip(y * 32767.0, -32768, 32767).astype(np.int16)

    def gain_db(self):
        return 20 * np.log10(self.g)


RX_DIR = "/tmp/vgw"


def rx_report(st):
    """Level + brightness of a received stream (before gain), saved as a WAV.
    'hf' = share of energy above 1.5 kHz: muffled/blurry speech drops it."""
    if not st["pcm"]:
        return ""
    x = np.concatenate(st["pcm"]).astype(np.float64) / 32768
    db = lambda v: 20 * np.log10(v) if v > 0 else -120.0
    peak, rms = np.abs(x).max(), np.sqrt(np.mean(x ** 2))
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / RATE16)
    hf = spec[freqs > 1500].sum() / max(spec[freqs > 100].sum(), 1e-12)
    try:
        import os
        import wave
        os.makedirs(RX_DIR, exist_ok=True)
        path = f"{RX_DIR}/rx_{time.strftime('%H%M%S')}.wav"
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE16)
            w.writeframes((x * 32767).astype(np.int16).tobytes())
    except OSError:
        path = "(not saved)"
    return (f" | peak {db(peak):.1f} dBFS, rms {db(rms):.1f} dBFS, "
            f"hf {hf * 100:.0f}% | {path}")


def vx_to_headset(args):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", VX_PORT))
    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                  socket.inet_aton(VX_GROUP) + socket.inet_aton(args.vx_if))

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)   # this node only
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                  socket.inet_aton(args.mesh_if))
    dest = (args.mesh_group, MESH_PORT)
    gain = db_to_lin(args.rx_gain)
    log.info(f"Vx {VX_GROUP}:{VX_PORT} on {args.vx_if} -> earcups via "
             f"{args.mesh_group}:{MESH_PORT} (TTL 0, auto gain to {args.rx_target:.0f} dBFS, "
             f"max {args.rx_gain:+.0f} dB)")

    streams = {}          # ssrc -> dict(dec, buf, seq, node, last, pkts)
    while True:
        rx.settimeout(0.25)
        try:
            pkt, addr = rx.recvfrom(4096)
        except socket.timeout:
            pkt = None
        now = time.monotonic()
        for ssrc in [k for k, v in streams.items() if now - v["last"] > STREAM_END]:
            v = streams.pop(ssrc)
            log.info(f"Vx -> earcups ended ssrc={ssrc:#010x}: {v['pkts']} pkts, "
                     f"{v['frames'] * 0.02:.1f}s" + rx_report(v)
                     + f" | agc {v['agc'].gain_db():+.0f} dB")
        if pkt is None:
            continue
        r = rtp_payload(pkt)
        if r is None:
            continue
        ssrc, payload = r
        if ssrc in SKIP_SSRCS:
            continue
        st = streams.get(ssrc)
        if st is None:
            st = streams[ssrc] = {
                "dec": opuslib.Decoder(RATE16, 1),
                "buf": np.zeros(0, dtype=np.int16),
                "seq": 0, "node": GW_NODE_BASE | (ssrc & 0xFF),
                "pkts": 0, "frames": 0, "pcm": [],
                "agc": AGC(args.rx_target, args.rx_gain),
            }
            log.info(f"Vx -> earcups started from {addr[0]} ssrc={ssrc:#010x}")
        st["last"] = now
        st["pkts"] += 1
        try:
            pcm = np.frombuffer(st["dec"].decode(payload, 1920), dtype=np.int16)
        except Exception as e:
            if st["pkts"] == 1:
                log.warning(f"opus decode failed: {e}")
            continue
        if len(st["pcm"]) < 3000:                      # keep up to 60 s for the report
            st["pcm"].append(pcm.copy())
        st["buf"] = np.concatenate([st["buf"], st["agc"].process(pcm)])
        while len(st["buf"]) >= FRAME16:
            frame = st["buf"][:FRAME16]
            st["buf"] = st["buf"][FRAME16:]
            hdr = NVOX_HDR.pack(NVOX_MAGIC, NVOX_VERSION, NVOX_PCM16,
                                st["node"], args.channel, 0, st["seq"])
            st["seq"] += 1
            st["frames"] += 1
            try:
                tx.sendto(hdr + frame.tobytes(), dest)
            except OSError as e:
                log.error(f"NVOX send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  Keep nucleus-voice on the right transport
# ═══════════════════════════════════════════════════════════════════════════

def control(cmd, timeout=2.0):
    """One request to nucleus-voice's control socket -> dict, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(cmd.encode(), CONTROL_ADDR)
        return json.loads(s.recvfrom(65535)[0].decode())
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def keep_transport(args):
    want = args.transport
    reachable = None
    hardware = None
    while True:
        st = control("STATUS")
        if st is None:
            if reachable is not False:
                log.warning("nucleus-voice not answering on 127.0.0.1:5556; will keep trying")
            reachable = False
        else:
            if reachable is not True:
                log.info(f"nucleus-voice answering (transport {st.get('transport')}, "
                         f"headset {'attached' if st.get('hardware') else 'not attached'})")
            reachable = True
            hw = bool(st.get("hardware"))
            if hardware is not None and hw != hardware:
                log.info(f"headset {'attached: card ' + str(st.get('card')) if hw else 'unplugged'}")
            hardware = hw
            if st.get("wrong_ptt"):
                log.warning(f"nucleus-voice rejected PTT device: {st.get('wrong_ptt')}")
            if want != "off" and st.get("transport") != want:
                r = control(f"TRANSPORT {want}")
                if r and r.get("ok"):
                    log.info(f"transport {st.get('transport')} -> {r.get('transport')} "
                             f"(nucleus-voice had reset it)")
                else:
                    log.warning(f"could not set transport {want}: {r}")
        time.sleep(KEEP_EVERY)


def supervised(fn, *a):
    """Run a worker thread; if it ever stops or fails, exit the whole process
    so systemd (Restart=always) starts a fresh gateway."""
    def run():
        try:
            fn(*a)
            log.error(f"{fn.__name__} stopped; exiting for a restart")
        except Exception as e:
            log.error(f"{fn.__name__} failed: {e!r}; exiting for a restart")
        os._exit(1)
    threading.Thread(target=run, daemon=True, name=fn.__name__).start()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vx-if", default=VX_IF_IP, help="node LAN IP (ATAK side)")
    p.add_argument("--mesh-if", default=MESH_IF_IP, help="node mesh IP (nucleus-voice side)")
    p.add_argument("--mesh-group", default=MESH_GROUP)
    p.add_argument("--daemon-node", type=int, default=DAEMON_NODE)
    p.add_argument("--channel", type=int, default=CHANNEL)
    p.add_argument("--stream-port", type=int, default=STREAM_PORT)
    p.add_argument("--rx-gain", type=float, default=24.0,
                   help="max dB of automatic gain on ATAK voice before the earcups")
    p.add_argument("--rx-target", type=float, default=-20.0,
                   help="dBFS rms the automatic gain aims received speech at")
    p.add_argument("--tx-gain", type=float, default=12.0,
                   help="dB on headset voice sent to local ATAK users")
    p.add_argument("--no-headset-to-vx", action="store_true")
    p.add_argument("--no-vx-to-headset", action="store_true")
    p.add_argument("--transport", default="stream", choices=["stream", "ip", "lora", "off"],
                   help="transport to keep nucleus-voice on (off = leave it alone)")
    args = p.parse_args()

    if not args.no_headset_to_vx:
        vx = VxOut(args)
        supervised(sniff_stream, args, vx)
        # ip-mode forwarding is optional: if it can't listen it just logs and stops
        threading.Thread(target=mesh_from_daemon, args=(args, vx), daemon=True).start()
    if not args.no_vx_to_headset:
        supervised(vx_to_headset, args)
    if args.transport != "off":
        supervised(keep_transport, args)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
