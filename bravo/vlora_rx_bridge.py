#!/usr/bin/env python3
"""
vlora_rx_bridge.py — LoRa → ATAK
Low-latency streaming decode: frames are decoded and injected as soon as
each UDP chunk arrives, instead of buffering the entire transmission.
Partial frame bytes are carried over to the next chunk instead of discarded.
"""
import socket
import struct
import time
import logging
import numpy as np
import soxr
import pycodec2
import opuslib

UDP_LISTEN_HOST = "127.0.0.1"
UDP_LISTEN_PORT = 4244

VX_GROUP   = "239.5.5.1"
VX_PORT    = 17501
VX_BIND_IP = "10.20.25.1"

RTP_SSRC   = 0x06165C5B
RTP_PT     = 0x00

CODEC2_FRAME_BYTES = 8
OPUS_FRAME_SAMPLES = 960
SILENCE_TIMEOUT    = 0.25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vlora_rx] %(message)s",
)
log = logging.getLogger("vlora_rx")


def build_rtp(seq, timestamp):
    return struct.pack("!BBHII", 0x80, RTP_PT,
                       seq & 0xffff, timestamp & 0xffffffff, RTP_SSRC)


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind((UDP_LISTEN_HOST, UDP_LISTEN_PORT))
    rx.settimeout(0.05)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                  socket.inet_aton(VX_BIND_IP))

    enc = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)

    seq        = 1000
    timestamp  = 0
    pcm48_buf  = np.array([], dtype=np.float32)
    carry      = bytearray()
    last_rx    = 0
    receiving  = False
    frames_in  = 0
    frames_out = 0
    bad_frames = 0
    c2_state   = pycodec2.Codec2(3200)

    log.info(f"Listening Codec2 on UDP {UDP_LISTEN_HOST}:{UDP_LISTEN_PORT}")
    log.info(f"Injecting RTP/Opus → {VX_GROUP}:{VX_PORT} via {VX_BIND_IP} (streaming mode)")

    while True:
        try:
            chunk, addr = rx.recvfrom(2048)
        except socket.timeout:
            if receiving and time.time() - last_rx > SILENCE_TIMEOUT:
                receiving = False
                log.info(f"Stream ended — {frames_in} frames decoded, "
                         f"{frames_out} RTP frames injected, "
                         f"{bad_frames} bad frames substituted")
                frames_in  = 0
                frames_out = 0
                bad_frames = 0
                carry      = bytearray()
                pcm48_buf  = np.array([], dtype=np.float32)
                c2_state   = pycodec2.Codec2(3200)
            continue

        if not chunk:
            continue

        if not receiving:
            log.info("Stream start")
            receiving = True

        last_rx = time.time()

        data = bytes(carry) + chunk
        usable = len(data) - (len(data) % CODEC2_FRAME_BYTES)
        carry = bytearray(data[usable:])

        for i in range(0, usable, CODEC2_FRAME_BYTES):
            frame = data[i:i + CODEC2_FRAME_BYTES]
            try:
                pcm8 = c2_state.decode(frame)
            except Exception:
                pcm8 = np.zeros(160, dtype=np.int16)
                bad_frames += 1

            frames_in += 1
            pcm48 = soxr.resample(pcm8.astype(np.float32), 8000, 48000,
                                   quality='MQ')
            pcm48_buf = np.concatenate([pcm48_buf, pcm48])

            while len(pcm48_buf) >= OPUS_FRAME_SAMPLES:
                frame_f32 = pcm48_buf[:OPUS_FRAME_SAMPLES]
                pcm48_buf = pcm48_buf[OPUS_FRAME_SAMPLES:]

                frame_i16 = np.clip(frame_f32, -32768, 32767).astype(np.int16)
                try:
                    opus = enc.encode(frame_i16.tobytes(), OPUS_FRAME_SAMPLES)
                except Exception as e:
                    log.error(f"Opus encode failed: {e}")
                    continue

                tx.sendto(build_rtp(seq, timestamp) + opus, (VX_GROUP, VX_PORT))
                seq        += 1
                timestamp  += OPUS_FRAME_SAMPLES
                frames_out += 1
                time.sleep(0.020)


if __name__ == "__main__":
    main()
