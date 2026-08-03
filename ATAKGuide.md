# Nucleus VLoRa Voice
## ATAK Push-To-Talk Operator Guide

**Version:** 1.0 (Draft)

---

# Table of Contents

1. Introduction
2. System Overview
3. Equipment Required
4. Connecting to a Nucleus
5. Configuring ATAK PTT
6. Using Push-To-Talk
7. Verifying Operation
8. Troubleshooting
9. Frequently Asked Questions

---

# Introduction

Nucleus VLoRa Voice extends ATAK's built-in Push-To-Talk (PTT) capability over a long-range LoRa network.

To the operator, the system behaves like a standard handheld radio while using ATAK's native voice interface.

No additional radio hardware is required beyond:

- Android Device
- ATAK
- TAK PTT Plugin
- Connection to a Nucleus Access Point

---

# System Overview

Voice is transported through the following path:

```

Android Phone
↓
ATAK PTT
↓
Wi-Fi
↓
Nucleus
↓
VLoRa Bridge
↓
LoRa
↓
Remote Nucleus
↓
Wi-Fi
↓
Remote ATAK

```

The entire process is automatic.

The operator simply presses Push-To-Talk and speaks.

---

# Equipment Required

Each operator requires:

- Android device
- ATAK installed
- TAK PTT Plugin installed
- Connection to a Nucleus Wi-Fi network

Nothing else is required.

---

# Connecting to the Nucleus

Open Android Wi-Fi Settings.

Connect to the assigned Nucleus.

Example:

```

SSID:
0024-Nucleus

```

or

```

SSID:
0025-Nucleus

```

Wait until Android reports:

```

Connected

```

---

# Launch ATAK

Open ATAK.

Verify:

- GPS Position appears
- Team members appear
- Maps load correctly

If ATAK is functioning normally, continue.

---

# Opening Push-To-Talk

Select the TAK PTT icon.



The Push-To-Talk interface opens.

---

# Voice Channel

Select the desired voice channel.

Example:

```

01 – Alpha Test

```

The channel should display:

```

Multicast:
239.5.5.1

Port:
17501

```

---

# Verify Voice Configuration

The Voice Status screen should display:

| Setting | Value |
|----------|-------|
| Codec | OPUS |
| Protocol | RTP |
| Network | Wi-Fi |



If these values are different, contact your administrator.

---

# Configuring a Voice Channel

Open the Voice Channel Configuration screen.



Configure:

| Field | Value |
|---------|-------|
| Alias | Alpha Test |
| Address | 239.5.5.1 |
| Port | 17501 |
| Protocol | RTP |

Press **Save**.

---

# Optional Data Channel

Expand:

```

Data Channel Configuration

```

Configure:

| Field | Value |
|---------|-------|
| Address | 239.5.5.2 |
| Port | 17501 |

Press **Save**.

---

# Using Push-To-Talk

Press and hold **PTT1**.

Speak normally.

Release the button when finished.

The remote operator should hear your transmission.

---

# Radio Check

Operator A:

```

"Alpha, Radio Check."

```

Operator B:

```

"Alpha, Loud and Clear."

```

If both operators hear each other, the system is functioning normally.

---

# Troubleshooting

## No Audio

Verify:

- Connected to Nucleus Wi-Fi
- Correct Voice Channel selected
- Codec is OPUS
- Protocol is RTP
- Volume is not muted

---

## Broken Audio

Move closer to the Nucleus.

Retry the transmission.

If problems continue, contact the administrator.

---

## Cannot Connect

Verify Wi-Fi connection.

Restart ATAK if necessary.

Reconnect to the Voice Channel.

---

# Administrator Reference

## Voice Channel

| Setting | Value |
|---------|-------|
| Address | 239.5.5.1 |
| Port | 17501 |
| Protocol | RTP |
| Codec | OPUS |

## Data Channel

| Setting | Value |
|---------|-------|
| Address | 239.5.5.2 |
| Port | 17501 |

---

# Frequently Asked Questions

## Do I need a radio?

No.

Your Android device communicates through the Nucleus.

---

## What happens if I move out of Wi-Fi range?

Voice communication will stop because your device is no longer connected to the local Nucleus.

---

## Does the system automatically use LoRa?

Yes.

The operator does not need to select LoRa.

The Nucleus automatically transports voice across the VLoRa network.

---

# Behind the Scenes

The operator does not need to know how the transport works, but the data path is shown below.

```

ATAK
↓
RTP
↓
Opus
↓
Nucleus Bridge
↓
Codec2
↓
LoRa
↓
Codec2
↓
Opus
↓
RTP
↓
ATAK

```

---

# Quick Start

1. Connect to Nucleus Wi-Fi
2. Open ATAK
3. Open TAK PTT
4. Select Voice Channel
5. Verify OPUS / RTP
6. Press PTT
7. Talk
8. Release

That's it.
