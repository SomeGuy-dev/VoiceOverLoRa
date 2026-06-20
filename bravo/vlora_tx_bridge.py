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
CODEC2_FRAME_BYTES   = 8
CODEC2_FRAME_SAMPLES = 160
SILENCE_TIMEOUT      = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vlora_tx] %(message)s",
)
log = logging.getLogger("vlora_tx")


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", VX_PORT))
    mreq = socket.inet_aton(VX_GROUP) + socket.inet_aton(MCAST_IF_IP)
    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    rx.settimeout(0.1)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    dec      = opuslib.Decoder(48000, 1)
    c2_state = pycodec2.Codec2(3200)

    # PCM buffer accumulates 8kHz samples until we have a full Codec2 frame
    pcm8_buf  = np.array([], dtype=np.float32)
    c2_out    = bytearray()

    last_rtp  = 0
    receiving = False
    total_c2  = 0

    log.info(f"Listening ATAK Vx RTP/Opus on {VX_GROUP}:{VX_PORT}")
    log.info(f"Forwarding Codec2 → UDP {RAW_CODEC2_HOST}:{RAW_CODEC2_PORT}")

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
                    # Send in 72-byte chunks to cot_bridge
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    total_c2 = 0
                    for i in range(0, len(c2_out), 72):
                        chunk = bytes(c2_out[i:i + 72])
                        sock.sendto(chunk, (RAW_CODEC2_HOST, RAW_CODEC2_PORT))
                        total_c2 += len(chunk)
                        time.sleep(0.005)

                    log.info(f"PTT released — sent {total_c2}B Codec2 "
                             f"to cot_bridge UDP:{RAW_CODEC2_PORT}")
                    c2_out   = bytearray()
                    pcm8_buf = np.array([], dtype=np.float32)


if __name__ == "__main__":
    main()
