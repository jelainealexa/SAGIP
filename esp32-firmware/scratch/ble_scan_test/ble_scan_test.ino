/*
 * SAGIP bench test: BLE scan, RSSI, and report throttling
 *
 * Runs on any ESP32 with BLE, including the ESP32-C3 bench board. Needs no
 * LoRa module, so it can be tested before the SX1276 shipment arrives.
 *
 * What it proves:
 *   1. The board detects a SAGIP distress advertisement and ignores everything
 *      else on the air.
 *   2. It reads the BLE RSSI of that advertisement. This single measurement
 *      carries all the victim position information in the whole system.
 *   3. It parses the advertisement into the fields the LoRa packet needs.
 *   4. It reports at a rate LoRa can actually carry.
 *
 * On the reporting rate
 * ---------------------
 *
 * A phone advertises several times a second, and the scanner sees every one.
 * Forwarding all of them over LoRa would saturate the band: at SF9 a packet
 * this size occupies the channel for roughly 180 ms, and flooding it to three
 * hops costs around 700 ms of airtime across the network. Three survivors
 * heard by four relays is twelve reports per round; at one round every 30
 * seconds that is already about 30 percent channel occupancy, which is where
 * collisions start eating the delivery rate this thesis measures.
 *
 * So: report the first sighting immediately, because that is the alert that
 * matters, then throttle repeats. A trapped person is not moving, so a fresh
 * position every 30 to 60 seconds is plenty. A status change reports at once,
 * since someone marking themselves CRITICAL should not wait out a timer.
 *
 * Between reports the readings are still collected and averaged. BLE RSSI
 * jumps several dB on its own, so an average over a dozen readings is better
 * input to the path loss model than any single sample.
 *
 * Testing without the Android app:
 *   Android: nRF Connect -> Advertiser -> Complete Local Name -> S|7A3C|0|18
 *   iOS:     LightBlue -> Virtual -> name it S|7A3C|0|18 -> start advertising
 *            (iOS stops advertising the moment the app leaves the foreground)
 *
 * Built against ESP32 Arduino core 3.x.
 *
 * Board settings for the ESP32-C3 bench board:
 *   Board: ESP32C3 Dev Module
 *   USB CDC On Boot: Enabled     <- without this, Serial prints nothing
 *   Port: whichever COM appears
 */

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

#define SERIAL_BAUD    115200
#define BLE_SCAN_SEC   2
#define SOS_PREFIX     "S|"

// How long a device stays throttled after a report. 30000 for bench work,
// 60000 in the field. Lower it to 5000 while doing a range walk so the
// numbers refresh often enough to follow.
#define REPORT_INTERVAL_MS   30000

// How many distinct devices this relay tracks at once. Eight is plenty for
// the concurrency test; a real deployment would need more.
#define MAX_TRACKED          8

// If no sighting for this long, the device is considered gone and its slot
// is freed. The next sighting then reports immediately as a new arrival.
#define DEVICE_TIMEOUT_MS    120000

// This relay's surveyed position, measured with a tape from a fixed
// benchmark at installation, not read from a GPS. Placeholder values for
// bench work; set them per node before deployment.
#define RELAY_ID       "R-01"
#define RELAY_LAT      14.457000
#define RELAY_LON      120.985000
#define RELAY_ALT      3.0

BLEScan *pBLEScan;

// Fields carried by one distress advertisement.
// See PACKET_FORMAT.md section 1.
struct Distress {
  String devID;
  int    status;      // index into STATUS_NAMES in backend/priority.py
  int    battery;     // percent
  int    rssi;        // dBm, measured here, never overwritten downstream
  bool   valid;
};

// One tracked device. rssiSum and rssiCount accumulate between reports so
// the reported value is an average rather than whichever sample happened to
// land when the timer expired.
struct Tracked {
  String        devID;
  int           lastStatus;
  long          rssiSum;
  int           rssiCount;
  int           rssiMin;
  int           rssiMax;
  unsigned long lastReportMs;
  unsigned long lastSeenMs;
  bool          inUse;
};

Tracked tracked[MAX_TRACKED];
unsigned long totalHeard = 0;
unsigned long totalReported = 0;

// Split on '|' and validate. Returns valid = false on anything malformed,
// because a half-parsed report is worse than a dropped one.
Distress parseAdvertisement(const String &name, int rssi) {
  Distress d;
  d.valid = false;
  d.rssi  = rssi;

  if (!name.startsWith(SOS_PREFIX)) return d;

  int f[3];
  int found = 0;
  int from = 0;
  while (found < 3) {
    int at = name.indexOf('|', from);
    if (at < 0) break;
    f[found++] = at;
    from = at + 1;
  }
  if (found < 3) return d;

  d.devID   = name.substring(f[0] + 1, f[1]);
  d.status  = name.substring(f[1] + 1, f[2]).toInt();
  d.battery = name.substring(f[2] + 1).toInt();

  if (d.devID.length() == 0)            return d;
  if (d.status < 0 || d.status > 5)     return d;
  if (d.battery < 0 || d.battery > 100) return d;

  d.valid = true;
  return d;
}

// Must stay in the same order as STATUS_NAMES in backend/priority.py.
const char *statusText(int s) {
  switch (s) {
    case 0:  return "CRITICAL SOS";
    case 1:  return "MEDICAL";
    case 2:  return "UNCONFIRMED";
    case 3:  return "NEED ASSISTANCE";
    case 4:  return "EVACUATING";
    case 5:  return "SAFE";
    default: return "INVALID";
  }
}

// Free slots whose device has not been seen for a while, so a device that
// leaves and returns is treated as a new arrival and alerts immediately.
void expireStale(unsigned long now) {
  for (int i = 0; i < MAX_TRACKED; i++) {
    if (tracked[i].inUse && now - tracked[i].lastSeenMs > DEVICE_TIMEOUT_MS) {
      Serial.print("[lost] ");
      Serial.print(tracked[i].devID);
      Serial.println(" not seen, slot freed");
      tracked[i].inUse = false;
    }
  }
}

// Find this device's slot, or claim a free one. Returns -1 when full.
int slotFor(const String &devID) {
  for (int i = 0; i < MAX_TRACKED; i++) {
    if (tracked[i].inUse && tracked[i].devID == devID) return i;
  }
  for (int i = 0; i < MAX_TRACKED; i++) {
    if (!tracked[i].inUse) return i;
  }
  return -1;
}

void emitReport(Tracked &t, int battery, const char *reason) {
  int avg = t.rssiCount > 0 ? (int)(t.rssiSum / t.rssiCount) : 0;

  totalReported++;

  Serial.println("---");
  Serial.print("REPORT   : "); Serial.println(reason);
  Serial.print("device   : "); Serial.println(t.devID);
  Serial.print("status   : "); Serial.print(t.lastStatus);
  Serial.print(" ("); Serial.print(statusText(t.lastStatus)); Serial.println(")");
  Serial.print("battery  : "); Serial.print(battery); Serial.println("%");
  Serial.print("BLE RSSI : "); Serial.print(avg);
  Serial.print(" dBm avg over "); Serial.print(t.rssiCount);
  Serial.print(" readings (min "); Serial.print(t.rssiMin);
  Serial.print(", max "); Serial.print(t.rssiMax); Serial.println(")");

  // What the relay will send over LoRa, and the nine-field line the base
  // station then prints to the backend. Compare the second against
  // backend/tools/simulate_packets.py output.
  Serial.print("lora tx  : SOS|XXXXXX|1|");
  Serial.print(t.devID);      Serial.print("|");
  Serial.print(t.lastStatus); Serial.print("|");
  Serial.print(battery);      Serial.print("|");
  Serial.print(RELAY_ID);     Serial.print("|");
  Serial.print(RELAY_LAT, 6); Serial.print("|");
  Serial.print(RELAY_LON, 6); Serial.print("|");
  Serial.print(RELAY_ALT, 1); Serial.print("|");
  Serial.println(avg);

  Serial.print("serial   : SOS|");
  Serial.print(t.devID);      Serial.print("|");
  Serial.print(t.lastStatus); Serial.print("|");
  Serial.print(battery);      Serial.print("|");
  Serial.print(RELAY_ID);     Serial.print("|");
  Serial.print(RELAY_LAT, 6); Serial.print("|");
  Serial.print(RELAY_LON, 6); Serial.print("|");
  Serial.print(RELAY_ALT, 1); Serial.print("|");
  Serial.println(avg);

  // Start a fresh averaging window.
  t.rssiSum      = 0;
  t.rssiCount    = 0;
  t.rssiMin      = 0;
  t.rssiMax      = -200;
  t.lastReportMs = millis();
}

class ScanCallbacks : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    if (!dev.haveName()) return;

    String name = String(dev.getName().c_str());
    Distress d  = parseAdvertisement(name, dev.getRSSI());

    if (!d.valid) {
      if (name.startsWith(SOS_PREFIX)) {
        Serial.print("malformed SAGIP advertisement: ");
        Serial.println(name);
      }
      return;
    }

    totalHeard++;
    unsigned long now = millis();

    int i = slotFor(d.devID);
    if (i < 0) {
      // Every slot is taken by a live device. Dropping the newcomer is the
      // wrong behaviour for a rescue system and is flagged loudly rather
      // than hidden; raise MAX_TRACKED if this ever appears.
      Serial.print("[FULL] no slot for ");
      Serial.println(d.devID);
      return;
    }

    Tracked &t = tracked[i];
    bool isNew = !t.inUse;

    if (isNew) {
      t.devID        = d.devID;
      t.inUse        = true;
      t.rssiSum      = 0;
      t.rssiCount    = 0;
      t.rssiMin      = 0;
      t.rssiMax      = -200;
      t.lastReportMs = 0;
    }

    bool statusChanged = !isNew && d.status != t.lastStatus;

    t.lastStatus = d.status;
    t.lastSeenMs = now;

    t.rssiSum += d.rssi;
    t.rssiCount++;
    if (d.rssi < t.rssiMin) t.rssiMin = d.rssi;
    if (d.rssi > t.rssiMax) t.rssiMax = d.rssi;

    // First sighting alerts at once. A status change alerts at once, because
    // someone marking themselves CRITICAL should not wait out a timer.
    // Everything else waits for the interval.
    if (isNew) {
      emitReport(t, d.battery, "new device");
    } else if (statusChanged) {
      emitReport(t, d.battery, "status changed");
    } else if (now - t.lastReportMs >= REPORT_INTERVAL_MS) {
      emitReport(t, d.battery, "interval");
    }
  }
};

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);   // native USB needs a moment or the first prints vanish

  for (int i = 0; i < MAX_TRACKED; i++) tracked[i].inUse = false;

  Serial.println();
  Serial.println("SAGIP BLE scanner, bench test");
  Serial.println("listening for advertisements beginning S|");
  Serial.print("reporting: first sighting and status changes at once, ");
  Serial.print("repeats every ");
  Serial.print(REPORT_INTERVAL_MS / 1000);
  Serial.println("s");

  BLEDevice::init("");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new ScanCallbacks());
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);
}

void loop() {
  // ESP32 Arduino core 3.x returns a pointer here. Core 2.x returned the
  // object by value, so older examples use BLEScanResults and found.getCount().
  BLEScanResults *found = pBLEScan->start(BLE_SCAN_SEC, false);

  expireStale(millis());

  Serial.print("[scan] devices in range: ");
  Serial.print(found->getCount());
  Serial.print(", heard: ");
  Serial.print(totalHeard);
  Serial.print(", reported: ");
  Serial.println(totalReported);

  // Not optional. Scan results accumulate in RAM and the board reboots after
  // a few minutes without this, which looks like a random crash.
  pBLEScan->clearResults();

  delay(500);
}
