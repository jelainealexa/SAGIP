# SAGIP packet format

The contract between the Android app, the relay firmware, and the dashboard
backend. Every member parses or builds these strings, so nobody changes this
file alone. Change it here first, tell the others, then change code.

Enum values and their numeric indices come from `dashboard/app.py`. That file
is the source of truth for the vocabularies. If an enum changes there, it
changes here in the same commit.

---

## Why the numbers instead of the words

A BLE advertisement carries 31 bytes in total, and the Complete Local Name
field eats into that. LoRa airtime grows with payload length, and at SF9 a
long packet occupies the channel long enough to collide with another relay
rebroadcasting the same report.

So the radio path sends the index of the enum, not its text. The dashboard
expands the index back into readable text. `app.py` already documents this.

---

## Vocabularies

### reported_status

| Index | Value |
|---|---|
| 0 | CRITICAL |
| 1 | ASSISTANCE |
| 2 | SAFE |

### requested_assistance

| Index | Value |
|---|---|
| 0 | TRAPPED |
| 1 | MEDICAL |
| 2 | WATER |
| 3 | FOOD |
| 4 | EXTRACTION |
| 5 | SHELTER |

Several codes are sent as concatenated digits with no separator. `23` means
WATER and FOOD. A single `-` means nothing was requested.

### communication_path

Not transmitted. The backend derives it from `hop`:

| hop | communication_path |
|---|---|
| (cellular ingest) | CELLULAR |
| 1 | BLE_LORA_1HOP |
| 2 | BLE_LORA_2HOP |
| 3 | BLE_LORA_3HOP |

### position_source

Not transmitted on the fallback path. The backend sets `RELAY_ESTIMATE` when
it produces a coordinate from relay measurements, and `NONE` when it cannot.
`PHONE_GNSS` only ever arrives over the cellular path.

---

## 1. BLE advertisement, phone to relay

Sent by the Android app in the Complete Local Name field.

```
S|<devID>|<status>|<request>|<battery>
```

| Field | Format | Example |
|---|---|---|
| `S` | literal, marks a SAGIP distress advertisement | `S` |
| devID | 4 hex chars, last 4 of the BLE address or a registered ID | `7A3C` |
| status | one digit, reported_status index | `0` |
| request | assistance digits concatenated, or `-` | `23` |
| battery | phone battery percent, 0 to 100 | `18` |

Example: `S|7A3C|0|0|18`

Length: 13 to 18 characters. Inside the 31-byte advertisement limit with room
to spare.

**Relay behaviour:** ignore any advertisement whose name does not begin with
`S|`. Do not attempt to parse anything else on the air.

**App note:** set the advertiser to `ADVERTISE_TX_POWER_HIGH`. The default is
much lower and roughly halves usable range.

---

## 2. LoRa report, relay to relay to base station

```
R|<pktID>|<hop>|<devID>|<status>|<request>|<battery>|<origin>|<bleRSSI>
```

| Field | Format | Example |
|---|---|---|
| `R` | literal, marks a report | `R` |
| pktID | 6 chars, relay ID plus a rolling counter, unique | `R2A31C` |
| hop | 1 on first transmission, incremented on each rebroadcast | `1` |
| devID | copied from the advertisement | `7A3C` |
| status | copied | `0` |
| request | copied | `0` |
| battery | copied | `18` |
| origin | the relay that heard the phone, never the one forwarding | `R-02` |
| bleRSSI | the BLE RSSI that origin measured, dBm, negative | `-67` |

Example: `R|R2A31C|1|7A3C|0|0|18|R-02|-67`

Length: about 32 characters.

### The two fields that must never be overwritten

`origin` and `bleRSSI` carry the entire localization signal. They are set once,
by the relay that heard the phone, and every later hop leaves them untouched.

A forwarding relay increments `hop` and changes nothing else.

If a relay overwrites `bleRSSI` with the LoRa RSSI it just measured, the
coordinate the backend produces is the position of the previous relay, not the
phone. That is silent and it invalidates the results.

---

## 3. Heartbeat, relay to base station

Sent every 30 to 60 seconds. Stagger the offsets so the relays do not all
transmit on the same second.

```
H|<relayID>|<battV>|<uptimeS>|<fwdCount>|<tilt>
```

| Field | Format | Example |
|---|---|---|
| relayID | node identity | `R-02` |
| battV | cell voltage, two decimals | `3.87` |
| uptimeS | seconds since boot | `14322` |
| fwdCount | packets forwarded since boot | `47` |
| tilt | `OK`, `TILT`, or `JOLT` | `OK` |

Example: `H|R-02|3.87|14322|47|OK`

The backend marks a relay offline after three missed heartbeats.

---

## 4. Beacon, relay to relay

Sent on its own interval so every other relay can measure the RSSI it arrives
at. This is the position trust check.

```
B|<relayID>|<seq>
```

Example: `B|R-02|118`

Beacons are never rebroadcast. A relay that hears one records the RSSI and
stops.

---

## 5. Neighbour RSSI report, relay to base station

What a relay heard from each of its neighbours since the last report.

```
N|<relayID>|<peer>:<rssi>,<peer>:<rssi>,...
```

Example: `N|R-02|R-01:-71,R-03:-68,R-04:-89`

The backend compares these against the baseline recorded at installation. All
links from one node shifting together means that node moved, or its antenna
broke. Either way it needs a physical check, and the dashboard flags its
position as unverified rather than rewriting the coordinate.

---

## 6. Base station to dashboard

The base station prints each received packet verbatim, one per line, to USB
serial. No framing, no prefix.

```
Port: the base station's COM port
Baud: 115200
Line ending: \n
```

The backend reads with pyserial and dispatches on the first character: `R`,
`H`, `B`, or `N`.

Lines that do not start with one of those, or that fail to parse, are logged
and discarded. The base station also prints its own boot messages, so the
parser must tolerate anything.

---

## Flooding rules

These are not optional. Without them two relays in range of each other will
rebroadcast the same packet back and forth until the band is saturated.

1. Every `R` packet carries a unique `pktID`.
2. Each relay keeps the last 20 pktIDs it has seen. A packet already in that
   list is dropped without forwarding.
3. `hop` is incremented on every rebroadcast. A packet arriving with `hop` at
   or above 3 is dropped.
4. Before rebroadcasting, wait a random 50 to 500 ms. Relays that heard the
   same packet at the same moment must not transmit together.

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

The Philippine allocation under AS923-3 is 915 to 918 MHz. The SX1276 modules
ship configured for the US plan at 902 to 928 MHz, which is wider than this
country permits, so the frequency is fixed in firmware and must stay inside
that window.

---

## Still open

- Drone to base station link: LoRa or ESP-NOW. LoRa is the current
  recommendation, since it removes a second protocol and avoids competing with
  the telemetry radio.
- Whether the payload release happens in flight, pending CAAP's answer on
  dropping objects from an RPA.
- Cellular ingest endpoint. The dashboard has no route for it yet, so the app
  has nothing to POST to. That is a Member 1 and Member 3 item.
