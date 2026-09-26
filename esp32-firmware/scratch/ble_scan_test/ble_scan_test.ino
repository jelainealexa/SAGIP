/*
 * SAGIP bench test: BLE scan and RSSI
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
 *
 * Testing without the Android app:
 *   nRF Connect for Mobile -> Advertiser -> add record ->
 *   Complete Local Name -> S|7A3C|0|18
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

// This relay's surveyed position, measured with a tape from a fixed
// benchmark at installation, not read from a GPS. Placeholder values for
// bench work; set them per node before deployment.
#define RELAY_ID       "R-01"
#define RELAY_LAT      14.457000
#define RELAY_LON      120.985000
#define RELAY_ALT      3.0

BLEScan *pBLEScan;
unsigned long heard = 0;

// Fields carried by one distress advertisement.
// See PACKET_FORMAT.md section 1.
struct Distress {
  String devID;
  int    status;      // index into STATUS_NAMES in backend/priority.py
  int    battery;     // percent
  int    rssi;        // dBm, measured here, never overwritten downstream
  bool   valid;
};

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

    heard++;

    Serial.println("---");
    Serial.print("raw      : "); Serial.println(name);
    Serial.print("device   : "); Serial.println(d.devID);
    Serial.print("status   : "); Serial.print(d.status);
    Serial.print(" ("); Serial.print(statusText(d.status)); Serial.println(")");
    Serial.print("battery  : "); Serial.print(d.battery); Serial.println("%");
    Serial.print("BLE RSSI : "); Serial.print(d.rssi); Serial.println(" dBm");

    // Preview of the LoRa packet this relay would transmit, and of the
    // nine-field line the base station would then print to the backend.
    // Compare these against backend/tools/simulate_packets.py output.
    Serial.print("lora tx  : SOS|XXXXXX|1|");
    Serial.print(d.devID);   Serial.print("|");
    Serial.print(d.status);  Serial.print("|");
    Serial.print(d.battery); Serial.print("|");
    Serial.print(RELAY_ID);  Serial.print("|");
    Serial.print(RELAY_LAT, 6); Serial.print("|");
    Serial.print(RELAY_LON, 6); Serial.print("|");
    Serial.print(RELAY_ALT, 1); Serial.print("|");
    Serial.println(d.rssi);

    Serial.print("serial   : SOS|");
    Serial.print(d.devID);   Serial.print("|");
    Serial.print(d.status);  Serial.print("|");
    Serial.print(d.battery); Serial.print("|");
    Serial.print(RELAY_ID);  Serial.print("|");
    Serial.print(RELAY_LAT, 6); Serial.print("|");
    Serial.print(RELAY_LON, 6); Serial.print("|");
    Serial.print(RELAY_ALT, 1); Serial.print("|");
    Serial.println(d.rssi);
  }
};

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);   // native USB needs a moment or the first prints vanish

  Serial.println();
  Serial.println("SAGIP BLE scanner, bench test");
  Serial.println("listening for advertisements beginning S|");

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

  Serial.print("scan complete, devices in range: ");
  Serial.print(found->getCount());
  Serial.print(", SAGIP reports heard so far: ");
  Serial.println(heard);

  // Not optional. Scan results accumulate in RAM and the board reboots after
  // a few minutes without this, which looks like a random crash.
  pBLEScan->clearResults();

  delay(500);
}
