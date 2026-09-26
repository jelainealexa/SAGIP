# SAGIP packet format

The contract between the Android app, the relay firmware, the base station and
the backend. Every member parses or builds these strings, so nobody changes
this file alone. Change it here first, tell the others, then change code.

`backend/priority.py` is the source of truth for the status vocabulary and
`backend/serial_reader.py` for the serial line. If either changes, this file
changes in the same commit.

---

## Why numbers instead of words

A BLE advertisement carries 31 bytes in total, and the Complete Local Name
field eats into that. LoRa airtime grows with payload length, and at SF9 a long
packet occupies the channel long enough to collide with another relay
rebroadcasting the same report.

So the radio path sends the index of the status, not its text. The backend
expands it through `status_from_code()`.

---

## Status codes

Index into `STATUS_NAMES` in `backend/priority.py`.

| Code | Status |
|---|---|
| 0 | CRITICAL SOS |
| 1 | MEDICAL |
| 2 | UNCONFIRMED |
| 3 | NEED ASSISTANCE |
| 4 | EVACUATING |
| 5 | SAFE |

The dashboard uses a shorter three-value vocabulary and maps onto this one
through `BACKEND_STATUS_TO_REPORTED` in `dashboard/app.py`. That mapping is
marked provisional in the code. Firmware follows the six-value list above.

---

## 1. BLE advertisement, phone to relay

Sent by the Android app in the Complete Local Name field.

```
S|<devID>|<status>|<battery>
```

| Field | Format | Example |
|---|---|---|
| `S` | literal, marks a SAGIP advertisement | `S` |
| devID | 4 to 6 chars, last of the BLE address or a registered ID | `7A3C` |
| status | one digit, status code above | `0` |
| battery | phone battery percent, 0 to 100 | `18` |

Example: `S|7A3C|0|18`

11 to 13 characters, comfortably inside the 31-byte limit.

**Relay behaviour:** ignore any advertisement whose name does not begin with
`S|`. Do not attempt to parse anything else on the air.

**App note:** set the advertiser to `ADVERTISE_TX_POWER_HIGH`. The default is
much lower and roughly halves usable range.

---

## 2. LoRa report, relay to relay to base station

```
SOS|<pktID>|<hop>|<devID>|<status>|<battery>|<relayID>|<lat>|<lon>|<alt>|<rssi>
```

| Field | Format | Example |
|---|---|---|
| `SOS` | literal | `SOS` |
| pktID | 6 chars, relay ID plus rolling counter, unique | `R2A31C` |
| hop | 1 on first transmission, incremented on each rebroadcast | `1` |
| devID | copied from the advertisement | `7A3C` |
| status | copied | `0` |
| battery | copied | `18` |
| relayID | the relay that heard the phone | `R-02` |
| lat | that relay's surveyed latitude | `14.457000` |
| lon | that relay's surveyed longitude | `120.985000` |
| alt | that relay's mounting height in metres | `3.0` |
| rssi | the BLE RSSI that relay measured, dBm, negative | `-72` |

Example: `SOS|R2A31C|1|7A3C|0|18|R-02|14.457000|120.985000|3.0|-72`

### The fields that must never be overwritten

`relayID`, `lat`, `lon`, `alt` and `rssi` are set once, by the relay that heard
the phone, and carry the entire localization signal. A forwarding relay
increments `hop` and changes nothing else.

If a forwarding relay overwrote `rssi` with the LoRa strength it just measured,
the value would describe the previous relay rather than the phone, and every
coordinate the backend produced would be quietly wrong rather than visibly
broken.

### Surveyed position, not GPS

Each relay's coordinates are compiled into its firmware, or stored in flash,
from a tape measurement taken at installation against a fixed benchmark. That
is accurate to well under a metre and does not drift. A GPS module on each
relay would report 2 to 3 m of error forever and cost about ₱1,000 per node to
make the reference worse.

If a relay is moved after installation, its stored coordinate is wrong and the
system has no way to know. That is a real limitation, and the beacon in
section 5 is how it gets detected.

---

## 3. Serial line, base station to backend

The base station strips `pktID` and `hop` and prints nine fields, which is
exactly what `backend/serial_reader.py` parses.

```
SOS|<devID>|<status>|<battery>|<relayID>|<lat>|<lon>|<alt>|<rssi>\n
```

Example: `SOS|7A3C|0|18|R-02|14.457000|120.985000|3.0|-72`

```
Baud: 115200
Line ending: \n
Encoding: ASCII
```

The reader ignores any line not starting with `SOS|`, so boot messages and
debug output on the same port are harmless. Keep debug prints free of that
prefix.

---

## 4. Release command, backend to base station

The only traffic going the other way. `POST /release` writes it through the
same serial connection, because Windows allows only one program to hold a COM
port.

```
RELEASE|<devID>\n
RELEASE\n
```

The base station forwards it to the drone payload over LoRa, and the payload
actuates the servo. The ID is only for the log.

**Not yet settled:** whether the payload releases in flight at all, pending
CAAP's answer on dropping objects from an RPA. If it is not permitted, the
servo becomes a ground demonstration and this command stays as a bench test.

---

## 5. Heartbeat and beacon, relay to base station

Not yet consumed by the backend. Specified here so the firmware can emit them
and the dashboard can add node health later.

### Heartbeat

Every 30 to 60 seconds, staggered so the relays do not all transmit on the
same second.

```
HB|<relayID>|<battV>|<uptimeS>|<fwdCount>|<tilt>
```

Example: `HB|R-02|3.87|14322|47|OK`

`tilt` is `OK`, `TILT` or `JOLT` from the MPU6050. Static tilt only: gravity
gives a drift-free reference while the node is still, so "this relay fell over"
is reliable. Position from acceleration is not, since bias integrates into tens
of metres within a minute.

Three missed heartbeats means the node is offline.

### Beacon

```
BC|<relayID>|<seq>
```

Beacons are never rebroadcast. Every other relay records the RSSI it arrives
at and reports the set periodically:

```
NB|<relayID>|<peer>:<rssi>,<peer>:<rssi>,...
```

Example: `NB|R-02|R-01:-71,R-03:-68,R-04:-89`

Compared against a baseline recorded at installation, this is how a moved or
damaged relay is detected. All links from one node shifting together means
that node moved, or its antenna broke. Either way it needs a physical check,
and the dashboard should flag its position as unverified rather than rewriting
the coordinate from an estimate. A surveyed point should never be replaced by
a guess.

---

## Flooding rules

Not optional. Without them two relays in range of each other rebroadcast the
same packet back and forth until the band is saturated.

1. Every `SOS` packet carries a unique `pktID`.
2. Each relay keeps the last 20 pktIDs it has seen and drops repeats without
   forwarding.
3. `hop` is incremented on every rebroadcast. A packet arriving at or above
   hop 3 is dropped.
4. Wait a random 50 to 500 ms before rebroadcasting, so relays that heard the
   same packet do not transmit together.

---

## Radio settings

Every node must match exactly or nothing is received.

| Setting | Value |
|---|---|
| Frequency | 916.0 MHz |
| Bandwidth | 125 kHz |
| Spreading factor | 9 |
| Coding rate | 4/5 |
| Sync word | 0x12 |

The Philippine allocation under AS923-3 is 915 to 918 MHz. SX1276 modules sold
as "915 MHz" ship configured for the US plan at 902 to 928 MHz, which is wider
than permitted here, so the frequency is pinned in firmware.

---

## Testing without hardware

`backend/tools/simulate_packets.py` generates traffic from four simulated
relays and prints the resulting position error per survivor. Run it to see
what well-formed input looks like, then compare your relay's first real output
against it line by line.

---

## Open items

- Drone to base station link: LoRa or ESP-NOW. LoRa is the current
  recommendation, since it removes a second protocol and avoids competing with
  the telemetry radio.
- Payload release in flight, pending CAAP.
- Backend does not yet consume `HB` or `NB` lines.
- Path loss exponent is uncalibrated. Measure it in the real environment,
  open air and through concrete separately, before quoting any accuracy
  figure.
