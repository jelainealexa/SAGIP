#ifndef CONFIG_H
#define CONFIG_H

// SAGIP shared firmware configuration.
// Included by relay_node, drone_payload and base_station so that all three
// agree on radio settings and timings. Change it here, not in a sketch.

// ---------------------------------------------------------------------------
// LoRa radio
//
// The Philippine allocation under AS923-3 is 915 to 918 MHz. SX1276 modules
// sold as "915 MHz" ship with the US plan spanning 902 to 928 MHz, which is
// wider than permitted here, so the frequency is pinned in firmware.
//
// Every node must match on all five of these or nothing is received.
// ---------------------------------------------------------------------------
#define LORA_FREQ        916E6
#define LORA_BW          125E3
#define LORA_SF          9
#define LORA_CR          5
#define LORA_SYNC        0x12      // private network; 0x34 marks LoRaWAN

// Start low on the bench so two boards on one desk do not saturate each
// other's front end. Raise for range tests and record the value used.
#define LORA_TX_POWER    14

// ---------------------------------------------------------------------------
// SX1276 wiring, ESP32-WROOM-32E
//
// SPI uses the default VSPI bus: SCK 18, MOSI 23, MISO 19.
// The module runs at 3.3 V. It is not 5 V tolerant.
//
// The ESP32-C3 bench board uses different pins. Override these before
// including this file when building for the C3.
// ---------------------------------------------------------------------------
#ifndef LORA_NSS
#define LORA_NSS         5
#endif
#ifndef LORA_RST
#define LORA_RST         14
#endif
#ifndef LORA_DIO0
#define LORA_DIO0        2
#endif

// ---------------------------------------------------------------------------
// Node identity
//
// Change this line before flashing each board. Everything else is identical
// across the four relays.
//   R-01 .. R-04  relays
//   BASE          base station
//   PAYLOAD       drone payload
// ---------------------------------------------------------------------------
#define NODE_ID          "R-01"

// ---------------------------------------------------------------------------
// BLE scanning
//
// Scan and sleep set the trade-off between detection latency and battery
// life. They are exposed here because that curve is a measured result, not a
// fixed choice: a longer sleep stretches endurance but risks missing a phone
// that only advertises for a few seconds.
// ---------------------------------------------------------------------------
#define BLE_SCAN_MS      2000
#define BLE_SLEEP_MS     3000
#define SOS_PREFIX       "S|"      // see PACKET_FORMAT.md section 1

// ---------------------------------------------------------------------------
// Reporting intervals
//
// Offsets are staggered per node so all four relays do not transmit on the
// same second. Add (node index * HEARTBEAT_STAGGER_MS) to the heartbeat timer
// at boot.
// ---------------------------------------------------------------------------
#define HEARTBEAT_MS           45000
#define HEARTBEAT_STAGGER_MS   3000
#define BEACON_MS              60000
#define NEIGHBOUR_REPORT_MS    120000

// ---------------------------------------------------------------------------
// Flooding
//
// Mandatory. Without duplicate suppression two relays in range of each other
// rebroadcast the same packet indefinitely and saturate the band.
// ---------------------------------------------------------------------------
#define MAX_HOPS         3
#define SEEN_LIST_SIZE   20
#define BACKOFF_MIN_MS   50
#define BACKOFF_MAX_MS   500

// ---------------------------------------------------------------------------
// Battery monitoring
//
// Divider ratio depends on the resistors fitted on the board. Measure the
// actual cell voltage with a multimeter and correct this before trusting the
// heartbeat reading.
// ---------------------------------------------------------------------------
#define VBAT_PIN         35
#define VBAT_DIVIDER     2.0
#define VBAT_LOW_V       3.30

// ---------------------------------------------------------------------------
// MPU6050 tilt
//
// Static tilt only. Gravity gives a drift-free reference while the node is
// stationary, so "this relay fell over" is reliable. Do not attempt position
// from acceleration: bias error integrates into tens of metres within a
// minute.
// ---------------------------------------------------------------------------
#define MPU_I2C_ADDR     0x68
#define TILT_ALARM_DEG   25.0

// ---------------------------------------------------------------------------
// Serial
// ---------------------------------------------------------------------------
#define SERIAL_BAUD      115200

#endif
