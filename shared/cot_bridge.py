#!/usr/bin/env python3
"""
ATAK CoT Bridge — Stage 7 + VLoRa Voice Extension

Bidirectional bridge between ATAK multicast CoT and Meshtastic LoRa.

TX: ATAK multicast (SA + Chat) → compress TAKPacketV2 → LoRa (portnum 257)
RX: LoRa (portnum 257) → decompress TAKPacketV2 → ATAK multicast

VLoRa Voice Extension (portnum 256):
TX: Codec2Talkie TCP:4243 → KISS unwrap → packetize → LoRa (portnum 256)
RX: LoRa (portnum 256) → strip header → Codec2 frames → UDP:4244 (for FTS)

Standalone daemon — owns the serial port exclusively.
Not used simultaneously with meshtastic_manager.py.

Usage:
    python3 cot_bridge.py [--port /dev/ttyACM0] [--debug]
"""

import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import defaultdict

from pubsub import pub

# ── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("cot_bridge")

# ── Multicast config ────────────────────────────────────────────

SA_MCAST_GROUP = "239.2.3.1"
SA_MCAST_PORT = 6969

CHAT_MCAST_GROUP = "224.10.10.1"
CHAT_MCAST_PORT = 17012

MCAST_IF = "br-lan"

# ── LoRa config ─────────────────────────────────────────────────

ATAK_FORWARDER_PORTNUM = 257
LORA_MTU = 237

# ── Rate limiting ───────────────────────────────────────────────

TX_MIN_INTERVAL = 30  # seconds — min time between TX for same CoT UID

# ── VLoRa Voice config (portnum 256) ────────────────────────────

VLORA_VOICE_PORTNUM  = 256      # LoRa port for voice traffic
VLORA_TCP_HOST       = "0.0.0.0"
VLORA_TCP_PORT       = 4243     # Codec2Talkie connects here
VLORA_UDP_FWD_HOST   = "127.0.0.1"
VLORA_UDP_FWD_PORT   = 4244     # Decoded RX voice forwarded here (for FTS)
VLORA_RAW_UDP_HOST   = "127.0.0.1"
VLORA_RAW_UDP_PORT   = 4245     # Raw Codec2 input from vlora_tx_bridge.py
VLORA_MAX_PAYLOAD    = 72       # 72 = 9 x 8-byte Codec2 3200 frames, aligned
VLORA_HEADER_SIZE    = 3        # 1B payload_size + 2B seq_num
VLORA_CODEC2_ID      = 2        # Codec ID: 2 = Codec2
VLORA_SEQ_INIT       = 0        # Reserved: Stream Initialisation
VLORA_SEQ_TERM       = 65535    # Reserved: Stream Termination

# Codec2 3200bps frame parameters
CODEC2_FRAME_BYTES   = 8        # bytes per frame
CODEC2_FRAME_MS      = 20       # ms per frame
FRAMES_PER_PACKET    = VLORA_MAX_PAYLOAD // CODEC2_FRAME_BYTES  # 9 frames
MS_PER_PACKET        = FRAMES_PER_PACKET * CODEC2_FRAME_MS      # 260ms

# KISS framing constants
KISS_FEND            = 0xC0
KISS_FESC            = 0xDB
KISS_TFEND           = 0xDC
KISS_TFESC           = 0xDD
KISS_DATA_FRAME      = 0x00

# PTT state — shared between TCP listener and RX handler
_ptt_active          = False
_ptt_lock            = threading.Lock()

# ── Globals (initialized in main) ───────────────────────────────

compressor = None
builder = None
cot_parser = None
mcast_send_sock = None
iface = None
my_node_num = None
local_subnet = None

# VLoRa UDP forward socket (for RX voice → FTS)
_vlora_fwd_sock = None

# Track last TX time per CoT UID for rate limiting
_tx_last_sent = defaultdict(float)
_tx_lock = threading.Lock()

# Track recently RX'd CoT UIDs to prevent re-TX loop
_rx_recent_uids = {}
_rx_lock = threading.Lock()
RX_UID_EXPIRY = 60

# Track last-seen time per node for web dashboard
_node_last_seen = {}

# ── Stats ────────────────────────────────────────────────────────

stats = {
    "tx_mcast_received": 0,
    "tx_parsed": 0,
    "tx_compressed": 0,
    "tx_sent": 0,
    "tx_rate_limited": 0,
    "tx_too_large": 0,
    "tx_errors": 0,
    "rx_total": 0,
    "rx_atak": 0,
    "rx_decompress_ok": 0,
    "rx_inject_ok": 0,
    "rx_errors": 0,
    # VLoRa voice stats
    "vlora_tx_packets": 0,
    "vlora_tx_bytes": 0,
    "vlora_tx_errors": 0,
    "vlora_rx_packets": 0,
    "vlora_rx_bytes": 0,
    "vlora_rx_errors": 0,
}


# ═══════════════════════════════════════════════════════════════
#  TX SIDE: Multicast → LoRa (UNCHANGED from Stage 7)
# ═══════════════════════════════════════════════════════════════

def _get_local_subnet():
    import fcntl
    max_attempts = 15
    for attempt in range(1, max_attempts + 1):
        try:
            ifname = MCAST_IF.encode()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ip_bytes = fcntl.ioctl(
                s.fileno(), 0x8915,
                struct.pack("256s", ifname[:15])
            )[20:24]
            mask_bytes = fcntl.ioctl(
                s.fileno(), 0x891b,
                struct.pack("256s", ifname[:15])
            )[20:24]
            s.close()
            ip_str = socket.inet_ntoa(ip_bytes)
            mask_str = socket.inet_ntoa(mask_bytes)
            network = ipaddress.ip_network(f"{ip_str}/{mask_str}", strict=False)
            if attempt > 1:
                logger.info(f"{MCAST_IF} subnet detected on attempt {attempt}/{max_attempts}")
            return network
        except Exception as e:
            if attempt < max_attempts:
                logger.info(f"Waiting for {MCAST_IF} IP (attempt {attempt}/{max_attempts}): {e}")
                time.sleep(2)
            else:
                logger.warning(f"Could not determine {MCAST_IF} subnet after {max_attempts} attempts: {e}")
                return None


def _create_mcast_listener(group, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    try:
        import fcntl
        ifname = MCAST_IF.encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip_bytes = fcntl.ioctl(
            s.fileno(), 0x8915,
            struct.pack("256s", ifname[:15])
        )[20:24]
        s.close()
        mreq = socket.inet_aton(group) + ip_bytes
    except Exception:
        mreq = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)
    return sock


def _is_chat_event(cot_xml):
    return "GeoChat" in cot_xml or "b-t-f" in cot_xml


def _tx_rate_ok(uid):
    now = time.time()
    with _tx_lock:
        last = _tx_last_sent.get(uid, 0)
        if now - last < TX_MIN_INTERVAL:
            return False
        _tx_last_sent[uid] = now
        return True


def _was_recently_rxd(uid):
    now = time.time()
    with _rx_lock:
        ts = _rx_recent_uids.get(uid)
        if ts and (now - ts) < RX_UID_EXPIRY:
            return True
        return False


def _tx_process_packet(data):
    import takproto
    sys.path.insert(0, "/opt/nucleus/meshtastic")
    from takmessage_to_xml import takmessage_to_xml

    stats["tx_mcast_received"] += 1

    try:
        tak_msg = takproto.parse_proto(bytearray(data))
        if tak_msg is None:
            return
        stats["tx_parsed"] += 1

        cot_xml = takmessage_to_xml(tak_msg)
        uid = tak_msg.cotEvent.uid

        if _was_recently_rxd(uid):
            logger.debug(f"TX skip (from LoRa): {uid}")
            return

        if not _tx_rate_ok(uid):
            stats["tx_rate_limited"] += 1
            logger.debug(f"TX rate limited: {uid}")
            return

        tak_packet = cot_parser.parse(cot_xml)
        wire_bytes = compressor.compress(tak_packet)
        stats["tx_compressed"] += 1

        if len(wire_bytes) > LORA_MTU:
            stats["tx_too_large"] += 1
            logger.warning(f"TX too large ({len(wire_bytes)}B > {LORA_MTU}B): {uid}")
            return

        iface.sendData(wire_bytes, portNum=ATAK_FORWARDER_PORTNUM, wantAck=False)
        stats["tx_sent"] += 1

        if uid:
            cot_type = tak_msg.cotEvent.type
            logger.info(f"TX → LoRa | {uid} | {cot_type} | {len(wire_bytes)}B")
        else:
            logger.info(f"TX → LoRa | [discovery] | {len(wire_bytes)}B")

    except Exception as e:
        stats["tx_errors"] += 1
        logger.error(f"TX error: {e}")


def _mcast_listener_loop(sock, name):
    logger.info(f"TX listener started: {name}")
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break

        if local_subnet is not None:
            src_ip = addr[0]
            if ipaddress.ip_address(src_ip) not in local_subnet:
                logger.debug(f"TX skip (non-local src {src_ip}): {name}")
                continue

        try:
            _tx_process_packet(data)
        except Exception as e:
            logger.error(f"TX listener ({name}) error: {e}")

    logger.info(f"TX listener exiting: {name}")


# ═══════════════════════════════════════════════════════════════
#  RX SIDE: LoRa → Multicast (UNCHANGED from Stage 7)
# ═══════════════════════════════════════════════════════════════

def _setup_mcast_send_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
    try:
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
            MCAST_IF.encode() + b"\0",
        )
    except OSError as e:
        logger.warning(f"Could not bind send socket to {MCAST_IF}: {e}")
    return sock


def _inject_multicast(cot_xml):
    if _is_chat_event(cot_xml):
        group, port = CHAT_MCAST_GROUP, CHAT_MCAST_PORT
    else:
        group, port = SA_MCAST_GROUP, SA_MCAST_PORT

    xml_bytes = cot_xml.encode("utf-8") if isinstance(cot_xml, str) else cot_xml
    try:
        mcast_send_sock.sendto(xml_bytes, (group, port))
        stats["rx_inject_ok"] += 1
        return group, port
    except Exception as e:
        logger.error(f"Multicast inject failed: {e}")
        stats["rx_errors"] += 1
        return None, None


def onReceive(packet, interface):
    """pypubsub callback: handle all received mesh packets."""
    stats["rx_total"] += 1

    sender = packet.get("from", "?")
    from_id = packet.get("fromId", "?")

    if sender == my_node_num:
        return

    if isinstance(sender, int):
        _node_last_seen[sender] = int(time.time())

    decoded = packet.get("decoded", {})
    portnum = decoded.get("portnum", "")

    # ── Port 257: ATAK CoT (unchanged) ──────────────────────────
    if portnum == "ATAK_FORWARDER":
        payload = decoded.get("payload")
        if not payload:
            return

        stats["rx_atak"] += 1
        rx_snr = packet.get("rxSnr", "?")

        sender_name = from_id
        try:
            node_info = iface.nodes.get(f"{from_id}")
            if node_info:
                sender_name = node_info.get("user", {}).get("shortName", from_id)
        except Exception:
            pass

        try:
            tak_packet = compressor.decompress(payload)
            stats["rx_decompress_ok"] += 1

            cot_xml = builder.build(tak_packet)
            _track_rx_uid(cot_xml)

            group, port = _inject_multicast(cot_xml)
            if group:
                uid, _ = _extract_uid_callsign(cot_xml)
                if uid and uid != "unknown" and uid != "?":
                    logger.info(
                        f"RX ← LoRa | {sender_name} | {uid} | "
                        f"{len(payload)}B | SNR={rx_snr}"
                    )
                else:
                    logger.info(
                        f"RX ← LoRa | {sender_name} | [discovery] | "
                        f"{len(payload)}B | SNR={rx_snr}"
                    )

        except Exception as e:
            stats["rx_errors"] += 1
            logger.error(f"RX error from {from_id}: {e}")

    # ── Port 256: VLoRa Voice (new) ─────────────────────────────
    elif portnum == "UNKNOWN_APP" and decoded.get("raw") is not None:
        # Meshtastic returns custom portnums as UNKNOWN_APP
        # Check the actual portnum integer
        raw_portnum = packet.get("decoded", {}).get("portnumInt", 0)
        if raw_portnum == VLORA_VOICE_PORTNUM:
            _handle_vlora_rx(packet, decoded)

    # Check via raw packet portnum for port 256
    elif portnum == "PRIVATE_APP":
        _handle_vlora_rx(packet, decoded)


# ═══════════════════════════════════════════════════════════════
#  VLoRa VOICE — RX HANDLER (new)
# ═══════════════════════════════════════════════════════════════

def _handle_vlora_rx(packet, decoded):
    """
    Handle an incoming VLoRa voice packet from LoRa port 256.

    Strips the 3-byte VLoRa header, extracts Codec2 frames,
    and forwards raw Codec2 bytes to UDP 4244 for FTS/codec2_rtp_bridge.
    """
    payload = decoded.get("payload")
    if not payload or len(payload) < VLORA_HEADER_SIZE:
        return

    sender = packet.get("fromId", "?")
    rx_snr = packet.get("rxSnr", "?")

    try:
        # Parse 3-byte header
        payload_size, seq = struct.unpack(">BH", payload[:VLORA_HEADER_SIZE])
        audio_data = payload[VLORA_HEADER_SIZE:]

        if seq == VLORA_SEQ_INIT:
            # Stream Init — log and set PTT state
            codec_id = audio_data[2] if len(audio_data) >= 3 else 0
            codec_name = {1: "G.711", 2: "Codec2", 9: "G.729"}.get(codec_id, f"Unknown({codec_id})")
            logger.info(
                f"VLoRa RX ← LoRa | {sender} | STREAM INIT | "
                f"codec={codec_name} | SNR={rx_snr}"
            )
            with _ptt_lock:
                global _ptt_active
                _ptt_active = True

        elif seq == VLORA_SEQ_TERM:
            # Stream Termination
            logger.info(
                f"VLoRa RX ← LoRa | {sender} | STREAM TERM | SNR={rx_snr}"
            )
            with _ptt_lock:
                _ptt_active = False

        else:
            # Stream Data — forward Codec2 bytes to UDP 4244
            if len(audio_data) < payload_size:
                logger.warning(
                    f"VLoRa RX size mismatch: header={payload_size}B "
                    f"actual={len(audio_data)}B seq={seq}"
                )
                stats["vlora_rx_errors"] += 1
                return

            frame_count = len(audio_data) // CODEC2_FRAME_BYTES
            audio_ms = frame_count * CODEC2_FRAME_MS

            # Forward to UDP 4244
            try:
                _vlora_fwd_sock.sendto(
                    audio_data,
                    (VLORA_UDP_FWD_HOST, VLORA_UDP_FWD_PORT)
                )
                stats["vlora_rx_packets"] += 1
                stats["vlora_rx_bytes"] += len(audio_data)
                logger.debug(
                    f"VLoRa RX ← LoRa | {sender} | seq={seq} | "
                    f"{len(audio_data)}B | {frame_count} frames | "
                    f"{audio_ms}ms | SNR={rx_snr}"
                )
            except Exception as e:
                stats["vlora_rx_errors"] += 1
                logger.error(f"VLoRa RX forward error: {e}")

    except Exception as e:
        stats["vlora_rx_errors"] += 1
        logger.error(f"VLoRa RX handler error: {e}")


# ═══════════════════════════════════════════════════════════════
#  VLoRa VOICE — PACKET BUILDERS (new)
# ═══════════════════════════════════════════════════════════════

def _vlora_build_init() -> bytes:
    """Stream Initialisation packet. Seq=0, codec=Codec2."""
    payload = struct.pack(">HB", 0, VLORA_CODEC2_ID)
    return struct.pack(">BH", len(payload), VLORA_SEQ_INIT) + payload


def _vlora_build_data(seq: int, audio: bytes) -> bytes:
    """Stream Data packet."""
    return struct.pack(">BH", len(audio), seq) + audio


def _vlora_build_term() -> bytes:
    """Stream Termination packet."""
    return struct.pack(">BH", 0, VLORA_SEQ_TERM)


def _vlora_send(pkt: bytes, label: str):
    """Send a VLoRa packet over LoRa port 256 via existing iface."""
    try:
        iface.sendData(pkt, portNum=VLORA_VOICE_PORTNUM, wantAck=False)
        stats["vlora_tx_packets"] += 1
        stats["vlora_tx_bytes"] += len(pkt)
        logger.debug(f"VLoRa TX → LoRa | {label} | {len(pkt)}B")
    except Exception as e:
        stats["vlora_tx_errors"] += 1
        logger.error(f"VLoRa TX error ({label}): {e}")


# ═══════════════════════════════════════════════════════════════
#  VLoRa VOICE — KISS UNWRAPPER (new)
# ═══════════════════════════════════════════════════════════════

class KISSUnwrapper:
    """Stateful KISS frame unwrapper for TCP stream from Codec2Talkie."""

    def __init__(self, on_frame):
        self._on_frame       = on_frame
        self._in_frame       = False
        self._escape_next    = False
        self._frame_type     = None
        self._frame_data     = bytearray()

    def feed(self, data: bytes):
        for byte in data:
            self._process_byte(byte)

    def _process_byte(self, byte: int):
        if byte == KISS_FEND:
            if self._in_frame:
                self._emit_frame()
                self._in_frame    = False
                self._escape_next = False
                self._frame_type  = None
                self._frame_data  = bytearray()
            else:
                self._in_frame    = True
                self._frame_type  = None
                self._frame_data  = bytearray()
                self._escape_next = False
            return

        if not self._in_frame:
            return

        if byte == KISS_FESC:
            self._escape_next = True
            return

        if self._escape_next:
            self._escape_next = False
            if byte == KISS_TFEND:
                byte = KISS_FEND
            elif byte == KISS_TFESC:
                byte = KISS_FESC

        if self._frame_type is None:
            self._frame_type = byte
            return

        self._frame_data.append(byte)

    def _emit_frame(self):
        if self._frame_type != KISS_DATA_FRAME:
            return
        if not self._frame_data:
            return
        try:
            self._on_frame(bytes(self._frame_data))
        except Exception as e:
            logger.error(f"KISS frame callback error: {e}")


# ═══════════════════════════════════════════════════════════════
#  VLoRa VOICE — TCP CLIENT HANDLER (new)
# ═══════════════════════════════════════════════════════════════

def _vlora_handle_client(conn, addr):
    """
    Handle one Codec2Talkie TCP connection.

    Unwraps KISS framing, buffers Codec2 bytes, emits LoRa packets
    using the same iface object as the ATAK bridge.
    Silence timeout (300ms) detects PTT key-up.
    """
    logger.info(f"VLoRa | Phone connected: {addr[0]}:{addr[1]}")
    conn.settimeout(0.3)  # 300ms silence = PTT key-up

    # Packetizer state
    buf        = bytearray()
    seq        = 1
    ptt_active = False
    pkt_count  = 0
    byte_count = 0
    stream_start = None

    def on_kiss_frame(codec2_bytes: bytes):
        nonlocal buf, seq, ptt_active, pkt_count, byte_count, stream_start

        # PTT key-down on first frame
        if not ptt_active:
            ptt_active   = True
            stream_start = time.time()
            seq          = 1
            buf          = bytearray()
            pkt_count    = 0
            byte_count   = 0
            init_pkt     = _vlora_build_init()
            _vlora_send(init_pkt, "INIT")
            logger.info(
                f"VLoRa | PTT KEY-DOWN | {addr[0]} | "
                f"Codec2 3200bps | LoRa port {VLORA_VOICE_PORTNUM}"
            )

        codec2_bytes = codec2_bytes[23:]
        with open("/tmp/alpha_codec2_3200.raw", "ab") as f:
            f.write(codec2_bytes)
        logger.info(f"VLoRa DEBUG | stripped_len={len(codec2_bytes)} | mod8={len(codec2_bytes) % 8} | first16_after={codec2_bytes[:16].hex()}")
        # Buffer Codec2 bytes
        buf.extend(codec2_bytes)
        logger.info(f"VLoRa DEBUG | KISS payload={len(codec2_bytes)}B | total_buf={len(buf)}B | mod8={len(buf) % 8} | first16={codec2_bytes[:16].hex()}")

        # Emit full packets
        while len(buf) >= VLORA_MAX_PAYLOAD:
            chunk = bytes(buf[:VLORA_MAX_PAYLOAD])
            buf   = buf[VLORA_MAX_PAYLOAD:]
            pkt   = _vlora_build_data(seq, chunk)
            _vlora_send(pkt, f"DATA seq={seq}")
            seq        = (seq % (VLORA_SEQ_TERM - 1)) + 1
            pkt_count += 1
            byte_count += len(chunk)

    def ptt_stop():
        nonlocal buf, ptt_active, pkt_count, byte_count

        # Flush remaining bytes
        if buf:
            chunk = bytes(buf)
            buf   = bytearray()
            pkt   = _vlora_build_data(seq, chunk)
            _vlora_send(pkt, f"DATA seq={seq} [flush]")
            pkt_count  += 1
            byte_count += len(chunk)

        # Send termination
        _vlora_send(_vlora_build_term(), "TERM")
        ptt_active = False

        duration = time.time() - stream_start if stream_start else 0
        logger.info(
            f"VLoRa | PTT KEY-UP | {addr[0]} | "
            f"duration={duration:.1f}s | packets={pkt_count} | "
            f"bytes={byte_count}B"
        )

    unwrapper = KISSUnwrapper(on_frame=on_kiss_frame)

    try:
        while True:
            try:
                data = conn.recv(4096)
                if not data:
                    break
                unwrapper.feed(data)

            except socket.timeout:
                if ptt_active:
                    ptt_stop()
                    logger.info("VLoRa | Waiting for next PTT press...")

    except Exception as e:
        logger.error(f"VLoRa client handler error: {e}")

    finally:
        if ptt_active:
            ptt_stop()
        conn.close()
        logger.info(f"VLoRa | Phone disconnected: {addr[0]}:{addr[1]}")


# ═══════════════════════════════════════════════════════════════
#  VLoRa VOICE — TCP SERVER THREAD (new)
# ═══════════════════════════════════════════════════════════════

def _vlora_tcp_server_loop():
    """
    TCP server thread — accepts Codec2Talkie connections on port 4243.
    Runs as a daemon thread alongside the existing ATAK bridge threads.
    Handles one phone connection at a time.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((VLORA_TCP_HOST, VLORA_TCP_PORT))
        server.listen(1)
        server.settimeout(1.0)
        logger.info(
            f"VLoRa | TCP server listening on "
            f"{VLORA_TCP_HOST}:{VLORA_TCP_PORT}"
        )
    except Exception as e:
        logger.error(f"VLoRa | TCP server bind failed: {e}")
        return

    while True:
        try:
            conn, addr = server.accept()
            # Handle in thread so server stays responsive
            t = threading.Thread(
                target=_vlora_handle_client,
                args=(conn, addr),
                daemon=True,
            )
            t.start()
        except socket.timeout:
            continue
        except Exception as e:
            logger.error(f"VLoRa | TCP server error: {e}")
            break

    server.close()
    logger.info("VLoRa | TCP server exiting")

def _vlora_raw_udp_server_loop():
    """
    Receives raw Codec2 bytes, packetizes into VLoRa 72-byte payloads,
    and sends over LoRa port 256.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((VLORA_RAW_UDP_HOST, VLORA_RAW_UDP_PORT))
    sock.settimeout(1.0)

    logger.info(
        f"VLoRa | Raw Codec2 UDP input listening on "
        f"{VLORA_RAW_UDP_HOST}:{VLORA_RAW_UDP_PORT}"
    )

    buf = bytearray()
    seq = 1
    ptt_active = False
    last_rx = 0
    pkt_count = 0
    byte_count = 0

    def start_stream():
        nonlocal ptt_active, seq, pkt_count, byte_count
        ptt_active = True
        seq = 1
        pkt_count = 0
        byte_count = 0
        _vlora_send(_vlora_build_init(), "RAW INIT")
        logger.info("VLoRa RAW | PTT KEY-DOWN")

    def stop_stream():
        nonlocal ptt_active, buf, pkt_count, byte_count
        if buf:
            chunk = bytes(buf)
            buf.clear()
            pkt = _vlora_build_data(seq, chunk)
            _vlora_send(pkt, f"RAW DATA seq={seq} [flush]")
            pkt_count += 1
            byte_count += len(chunk)
        _vlora_send(_vlora_build_term(), "RAW TERM")
        ptt_active = False
        logger.info(
            f"VLoRa RAW | PTT KEY-UP | packets={pkt_count} bytes={byte_count}B"
        )

    while True:
        try:
            data, addr = sock.recvfrom(2048)
            now = time.time()

            if not data:
                continue

            if not ptt_active:
                start_stream()

            last_rx = now
            buf.extend(data)

            while len(buf) >= VLORA_MAX_PAYLOAD:
                chunk = bytes(buf[:VLORA_MAX_PAYLOAD])
                del buf[:VLORA_MAX_PAYLOAD]

                pkt = _vlora_build_data(seq, chunk)
                _vlora_send(pkt, f"RAW DATA seq={seq}")

                seq = (seq % (VLORA_SEQ_TERM - 1)) + 1
                pkt_count += 1
                byte_count += len(chunk)

        except socket.timeout:
            if ptt_active and time.time() - last_rx > 0.5:
                stop_stream()

        except Exception as e:
            stats["vlora_tx_errors"] += 1
            logger.error(f"VLoRa RAW UDP error: {e}")



# ═══════════════════════════════════════════════════════════════
#  NODE DUMP (UNCHANGED from Stage 7)
# ═══════════════════════════════════════════════════════════════

NODE_DUMP_PATH = "/tmp/meshtastic_nodes.json"
NODE_DUMP_INTERVAL = 15
NODE_MAX_AGE = 3600


def _dump_nodes():
    if iface is None or not hasattr(iface, 'nodes') or iface.nodes is None:
        return

    try:
        now = int(time.time())
        nodes_snapshot = dict(iface.nodes)
        nodes_list = []
        for node_id, node in nodes_snapshot.items():
            num = node.get("num")
            if num == my_node_num:
                continue

            user = node.get("user", {})
            last_heard = node.get("lastHeard") or 0
            our_seen = _node_last_seen.get(num, 0)
            last_heard = max(last_heard, our_seen)
            if not last_heard or (now - last_heard) > NODE_MAX_AGE:
                continue

            nodes_list.append({
                "id": user.get("id", node_id),
                "short_name": user.get("shortName", "?"),
                "long_name": user.get("longName", ""),
                "last_heard": last_heard,
                "snr": node.get("snr"),
                "hops_away": node.get("hopsAway"),
            })

        nodes_list.sort(key=lambda n: n["last_heard"], reverse=True)

        dump = {
            "timestamp": int(time.time()),
            "nodes": nodes_list,
        }

        tmp_path = NODE_DUMP_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(dump, f)
        os.replace(tmp_path, NODE_DUMP_PATH)

    except Exception as e:
        logger.warning(f"Node dump error: {e}")


def onConnection(interface, topic=pub.AUTO_TOPIC):
    global my_node_num
    my_node_num = interface.myInfo.my_node_num
    logger.info(f"Radio connected: {interface.getLongName()} (node {my_node_num})")


def onDisconnect(interface, topic=pub.AUTO_TOPIC):
    logger.warning("Radio connection lost!")


# ═══════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS (UNCHANGED from Stage 7)
# ═══════════════════════════════════════════════════════════════

def _track_rx_uid(cot_xml):
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(cot_xml)
        uid = root.get("uid", "")
        if uid:
            with _rx_lock:
                _rx_recent_uids[uid] = time.time()
    except Exception:
        pass


def _extract_uid_callsign(cot_xml):
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(cot_xml)
        uid = root.get("uid", "?")
        detail = root.find("detail")
        callsign = "?"
        if detail is not None:
            contact = detail.find("contact")
            if contact is not None:
                callsign = contact.get("callsign", "?")
        return uid, callsign
    except Exception:
        return "?", "?"


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    global compressor, builder, cot_parser, mcast_send_sock, iface, local_subnet
    global _vlora_fwd_sock

    parser = argparse.ArgumentParser(description="ATAK CoT Bridge (Stage 7 + VLoRa)")
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("meshtastic").setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    else:
        logging.getLogger("meshtastic").setLevel(logging.WARNING)

    # ── Initialize ATAK pipeline ─────────────────────────────────
    from meshtastic_tak.cot_xml_parser import CotXmlParser
    from meshtastic_tak.tak_compressor import TakCompressor
    from meshtastic_tak.cot_xml_builder import CotXmlBuilder

    compressor = TakCompressor()
    builder = CotXmlBuilder()
    cot_parser = CotXmlParser()
    logger.info("Pipeline initialized (TakCompressor + CotXmlBuilder + CotXmlParser)")

    # ── Initialize VLoRa UDP forward socket ──────────────────────
    _vlora_fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    logger.info(f"VLoRa | UDP forward socket ready → {VLORA_UDP_FWD_HOST}:{VLORA_UDP_FWD_PORT}")

    # ── Detect local subnet ───────────────────────────────────────
    local_subnet = _get_local_subnet()
    if local_subnet:
        logger.info(f"TX source filter: only bridging multicast from {local_subnet}")
    else:
        logger.warning("TX source filter DISABLED — could not detect br-lan subnet")

    # ── Setup multicast sockets ───────────────────────────────────
    mcast_send_sock = _setup_mcast_send_socket()
    logger.info(f"Multicast send socket ready on {MCAST_IF}")

    # ── Subscribe to pypubsub BEFORE opening serial ───────────────
    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    pub.subscribe(onDisconnect, "meshtastic.connection.lost")

    # ── Open serial interface ─────────────────────────────────────
    import meshtastic.serial_interface

    logger.info(f"Opening SerialInterface(devPath={args.port})...")
    try:
        iface = meshtastic.serial_interface.SerialInterface(devPath=args.port)
    except Exception as e:
        logger.error(f"Failed to open serial interface: {e}")
        sys.exit(1)
    logger.info(f"Radio open on {iface.devPath}")

    # ── Start ATAK TX multicast listeners ────────────────────────
    try:
        sa_sock = _create_mcast_listener(SA_MCAST_GROUP, SA_MCAST_PORT)
        sa_thread = threading.Thread(
            target=_mcast_listener_loop, args=(sa_sock, "SA"),
            daemon=True,
        )
        sa_thread.start()
        logger.info(f"TX: Listening on {SA_MCAST_GROUP}:{SA_MCAST_PORT} (SA)")
    except Exception as e:
        logger.error(f"Could not start SA listener: {e}")
        sa_sock = None

    try:
        chat_sock = _create_mcast_listener(CHAT_MCAST_GROUP, CHAT_MCAST_PORT)
        chat_thread = threading.Thread(
            target=_mcast_listener_loop, args=(chat_sock, "Chat"),
            daemon=True,
        )
        chat_thread.start()
        logger.info(f"TX: Listening on {CHAT_MCAST_GROUP}:{CHAT_MCAST_PORT} (Chat)")
    except Exception as e:
        logger.error(f"Could not start Chat listener: {e}")
        chat_sock = None

    # ── Start VLoRa TCP server ────────────────────────────────────
    vlora_thread = threading.Thread(
        target=_vlora_tcp_server_loop,
        daemon=True,
    )
    vlora_thread.start()

    vlora_raw_thread = threading.Thread(
        target=_vlora_raw_udp_server_loop,
        daemon=True,
    )
    vlora_raw_thread.start()

    print()
    print("=" * 60)
    print("  ATAK CoT Bridge — Bidirectional LoRa ↔ Multicast")
    print(f"  TX: {SA_MCAST_GROUP}:{SA_MCAST_PORT} + {CHAT_MCAST_GROUP}:{CHAT_MCAST_PORT} → LoRa")
    print(f"  RX: LoRa → multicast on {MCAST_IF}")
    print(f"  Rate limit: {TX_MIN_INTERVAL}s per UID")
    print("  ── VLoRa Voice ──────────────────────────────────────")
    print(f"  TX: TCP:{VLORA_TCP_PORT} (Codec2Talkie) → LoRa port {VLORA_VOICE_PORTNUM}")
    print(f"  TX: UDP:{VLORA_RAW_UDP_PORT} (vlora_tx_bridge) -> LoRa port {VLORA_VOICE_PORTNUM}")
    print(f"  RX: LoRa port {VLORA_VOICE_PORTNUM} → UDP:{VLORA_UDP_FWD_PORT} (FTS)")
    print("=" * 60)
    print()

    # ── Shutdown handler ──────────────────────────────────────────
    def _shutdown(signum=None, frame=None):
        sig_name = signal.Signals(signum).name if signum else "KeyboardInterrupt"
        logger.info(f"Shutting down ({sig_name})...")
        logger.info(
            f"TX stats: mcast={stats['tx_mcast_received']} parsed={stats['tx_parsed']} "
            f"sent={stats['tx_sent']} rate_limited={stats['tx_rate_limited']} "
            f"too_large={stats['tx_too_large']} errors={stats['tx_errors']}"
        )
        logger.info(
            f"RX stats: total={stats['rx_total']} atak={stats['rx_atak']} "
            f"decompress={stats['rx_decompress_ok']} inject={stats['rx_inject_ok']} "
            f"errors={stats['rx_errors']}"
        )
        logger.info(
            f"VLoRa TX: packets={stats['vlora_tx_packets']} "
            f"bytes={stats['vlora_tx_bytes']} errors={stats['vlora_tx_errors']}"
        )
        logger.info(
            f"VLoRa RX: packets={stats['vlora_rx_packets']} "
            f"bytes={stats['vlora_rx_bytes']} errors={stats['vlora_rx_errors']}"
        )
        try:
            iface.close()
        except Exception:
            pass
        try:
            mcast_send_sock.close()
        except Exception:
            pass
        try:
            _vlora_fwd_sock.close()
        except Exception:
            pass
        if sa_sock:
            try:
                sa_sock.close()
            except Exception:
                pass
        if chat_sock:
            try:
                chat_sock.close()
            except Exception:
                pass
        logger.info("Serial interface closed — radio released")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    # ── Keep alive ────────────────────────────────────────────────
    last_node_dump = 0
    try:
        while True:
            time.sleep(10)
            try:
                now = time.time()

                if now - last_node_dump >= NODE_DUMP_INTERVAL:
                    _dump_nodes()
                    last_node_dump = now

                with _rx_lock:
                    expired = [u for u, t in _rx_recent_uids.items() if now - t > RX_UID_EXPIRY]
                    for u in expired:
                        del _rx_recent_uids[u]
                with _tx_lock:
                    expired = [u for u, t in _tx_last_sent.items() if now - t > TX_MIN_INTERVAL * 2]
                    for u in expired:
                        del _tx_last_sent[u]
            except Exception as e:
                logger.warning(f"Main loop error (continuing): {e}")
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
