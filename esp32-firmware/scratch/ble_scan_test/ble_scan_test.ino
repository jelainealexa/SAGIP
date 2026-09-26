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
 *   Complete Local Name -> S|7A3C|0|0|18
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

BLEScan *pBLEScan;
unsigned long heard = 0;

// Fields carried by one distress advertisement.
// See PACKET_FORMAT.md section 1.
struct Distress {
  String devID;
  int    status;      // 0 CRITICAL, 1 ASSISTANCE, 2 SAFE
  String request;     // concatenated assistance digits, or "-"
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

  int f[4];
  int found = 0;
  int from = 0;
  while (found < 4) {
    int at = name.indexOf('|', from);
    if (at < 0) break;
    f[found++] = at;
    from = at + 1;
  }
  if (found < 4) return d;

  d.devID   = name.substring(f[0] + 1, f[1]);
  d.status  = name.substring(f[1] + 1, f[2]).toInt();
  d.request = name.substring(f[2] + 1, f[3]);
  d.battery = name.substring(f[3] + 1).toInt();

  if (d.devID.length() == 0)            return d;
  if (d.status < 0 || d.status > 2)     return d;
  if (d.battery < 0 || d.battery > 100) return d;

  d.valid = true;
  return d;
}

const char *statusText(int s) {
  switch (s) {
    case 0:  return "CRITICAL";
    case 1:  return "ASSISTANCE";
    case 2:  return "SAFE";
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
    Serial.print("request  : "); Serial.println(d.request);
    Serial.print("battery  : "); Serial.print(d.battery); Serial.println("%");
    Serial.print("BLE RSSI : "); Serial.print(d.rssi); Serial.println(" dBm");

    // Preview of the LoRa packet this relay would transmit. Once the SX1276
    // modules arrive this string goes over the air unchanged.
    Serial.print("would tx : R|XXXXXX|1|");
    Serial.print(d.devID);  Serial.print("|");
    Serial.print(d.status); Serial.print("|");
    Serial.print(d.request); Serial.print("|");
    Serial.print(d.battery); Serial.print("|");
    Serial.print("R-01");   Serial.print("|");
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
  BLEScanResults found = pBLEScan->start(BLE_SCAN_SEC, false);

  Serial.print("scan complete, devices in range: ");
  Serial.print(found.getCount());
  Serial.print(", SAGIP reports heard so far: ");
  Serial.println(heard);

  // Not optional. Scan results accumulate in RAM and the board reboots after
  // a few minutes without this, which looks like a random crash.
  pBLEScan->clearResults();

  delay(500);
}
