#!/usr/bin/env python3
"""
vlora_tx_bridge.py — ATAK → LoRa
Listens on 239.5.5.1:17501 for RTP/Opus from ATAK Vx PTT
Decodes Opus → PCM → Codec2 3200 using pycodec2 + soxr (no subprocesses)
Sends Codec2 to cot_bridge UDP:4245 → LoRa TX
"""
import socket
import time
import logging
import numpy as np
import soxr
import pycodec2
import opuslib

VX_GROUP      = "239.5.5.1"
VX_PORT       = 17501
MCAST_IF_IP   = "10.20.25.1"   # Bravo: 10.20.25.1
LOCAL_NODE_IP = "10.20.25.1"   # Bravo: 10.20.25.1

RAW_CODEC2_HOST = "127.0.0.1"
RAW_CODEC2_PORT = 4245

OPUS_FRAME_SAMPLES   = 960
CODEC2_MODE          = 3200      # bps: 3200 or 2400 (the RX bridge MUST use the same)
CODEC2_FRAME_BYTES   = {3200: 8, 2400: 6}[CODEC2_MODE]
CODEC2_FRAME_SAMPLES = 160       # 20 ms at 8 kHz in both modes
SILENCE_TIMEOUT      = 0.5

PACKET_BYTES  = 144    # MUST equal cot_bridge STREAM_MAX_PAYLOAD (18 x 8 B @3200, 24 x 6 B @2400)
SEND_SPACING  = 0.30   # s between LoRa packets; receiver dead-time needs >= ~0.25 s (144 B / 0.30 s = 480 B/s = 1.2x speech)
PRIME_SILENCE = True   # send one throwaway silence packet first (absorbs the INIT collision)
PAD_TO_PACKET = True   # pad the clip with silence so no short tail packet waits on cot_bridge
assert PACKET_BYTES % CODEC2_FRAME_BYTES == 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vlora_tx] %(message)s",
)
log = logging.getLogger("vlora_tx")


def build_packets(c2_out):
    """Split a clip into PACKET_BYTES chunks, with optional silence prime (first)
    and silence padding (last). Silence comes from a fresh encoder so the live
    encoder state is untouched."""
    sil = bytes(pycodec2.Codec2(CODEC2_MODE).encode(
        np.zeros(CODEC2_FRAME_SAMPLES, dtype=np.int16)))
    assert len(sil) == CODEC2_FRAME_BYTES
    data = bytes(c2_out)
    if PAD_TO_PACKET and len(data) % PACKET_BYTES:
        data += sil * ((-len(data) % PACKET_BYTES) // CODEC2_FRAME_BYTES)
    packets = [data[i:i + PACKET_BYTES] for i in range(0, len(data), PACKET_BYTES)]
    if PRIME_SILENCE:
        packets.insert(0, sil * (PACKET_BYTES // CODEC2_FRAME_BYTES))
    return packets


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", VX_PORT))
    mreq = socket.inet_aton(VX_GROUP) + socket.inet_aton(MCAST_IF_IP)
    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    rx.settimeout(0.1)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    dec      = opuslib.Decoder(48000, 1)
    c2_state = pycodec2.Codec2(CODEC2_MODE)

    # PCM buffer accumulates 8kHz samples until we have a full Codec2 frame
    pcm8_buf  = np.array([], dtype=np.float32)
    c2_out    = bytearray()

    last_rtp  = 0
    receiving = False
    total_c2  = 0

    log.info(f"Listening ATAK Vx RTP/Opus on {VX_GROUP}:{VX_PORT}")
    log.info(f"Forwarding Codec2 → UDP {RAW_CODEC2_HOST}:{RAW_CODEC2_PORT}")
    log.info(f"Codec2 mode {CODEC2_MODE} bps ({CODEC2_FRAME_BYTES} B/frame), "
             f"{PACKET_BYTES} B packets every {SEND_SPACING}s, "
             f"prime={PRIME_SILENCE} pad={PAD_TO_PACKET}")

    while True:
        try:
            pkt, addr = rx.recvfrom(2048)
            src_ip = addr[0]

            # Loop prevention
            if src_ip == LOCAL_NODE_IP:
                continue

            # Must be RTP
            if len(pkt) < 13 or pkt[0] != 0x80:
                continue

            opus_payload = pkt[12:]

            try:
                pcm48_bytes = dec.decode(opus_payload, OPUS_FRAME_SAMPLES,
                                         decode_fec=False)
            except Exception as e:
                log.error(f"Opus decode failed: {e}")
                continue

            # Convert to float32 for resampling
            pcm48 = np.frombuffer(pcm48_bytes, dtype=np.int16).astype(np.float32)

            # Resample 48kHz → 8kHz (960 → 160 samples)
            pcm8 = soxr.resample(pcm48, 48000, 8000, quality='MQ')
            pcm8_buf = np.concatenate([pcm8_buf, pcm8])

            last_rtp  = time.time()
            receiving = True

            # Encode Codec2 frames whenever we have 160 samples
            while len(pcm8_buf) >= CODEC2_FRAME_SAMPLES:
                frame_f32 = pcm8_buf[:CODEC2_FRAME_SAMPLES]
                pcm8_buf  = pcm8_buf[CODEC2_FRAME_SAMPLES:]

                frame_i16 = np.clip(frame_f32, -32768, 32767).astype(np.int16)
                c2_frame  = c2_state.encode(frame_i16)
                c2_out.extend(c2_frame)

        except socket.timeout:
            if receiving and time.time() - last_rtp > SILENCE_TIMEOUT:
                receiving = False

                if c2_out:
                    # Send PACKET_BYTES chunks to cot_bridge, one every SEND_SPACING
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    speech_bytes = len(c2_out)
                    packets = build_packets(c2_out)
                    total_c2 = 0
                    t0 = time.monotonic()
                    for n, chunk in enumerate(packets):
                        wait = t0 + n * SEND_SPACING - time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                        sock.sendto(chunk, (RAW_CODEC2_HOST, RAW_CODEC2_PORT))
                        total_c2 += len(chunk)
                    sock.close()

                    log.info(f"PTT released — sent {total_c2}B Codec2 "
                             f"({speech_bytes}B speech, {len(packets)} packets, "
                             f"mode {CODEC2_MODE}, spacing {SEND_SPACING}s) "
                             f"to cot_bridge UDP:{RAW_CODEC2_PORT}")
                    c2_out   = bytearray()
                    pcm8_buf = np.array([], dtype=np.float32)


if __name__ == "__main__":
    main()
