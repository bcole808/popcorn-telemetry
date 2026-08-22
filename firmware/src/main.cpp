// Popcorn telemetry — SHT45 humidity/temperature over USB serial.
//
// Wiring (ESP-WROOM-32 38-pin):
//   SHT45 VIN -> 3V3    SHT45 GND -> GND
//   SHT45 SDA -> GPIO21 SHT45 SCL -> GPIO22
//
// Line protocol (115200 baud, one message per line):
//   SCAN,0x44                     an I2C device answered at this address (boot only)
//   READY                         sensor initialised, data follows
//   D,<millis>,<temp_c>,<rh>      a reading
//   ERR,<reason>                  something went wrong

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>

static const int PIN_SDA = 21;
static const int PIN_SCL = 22;
static const uint32_t SAMPLE_INTERVAL_MS = 500;  // 2 Hz

static Adafruit_SHT4x sht4;
static bool sensorReady = false;
static uint32_t lastSampleMs = 0;

// Walk the I2C address range and report anything that ACKs. This is the wiring
// diagnostic — a healthy SHT45 shows up as SCAN,0x44.
static void scanBus() {
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("SCAN,0x%02X\n", addr);
    }
  }
}

// Returns true once the sensor is talking. Called from setup and retried from
// loop so an unplugged sensor doesn't wedge the board.
static bool initSensor() {
  if (!sht4.begin()) {
    return false;
  }
  sht4.setPrecision(SHT4X_HIGH_PRECISION);
  sht4.setHeater(SHT4X_NO_HEATER);
  return true;
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) {
    // Give the USB serial a moment, but never block forever.
  }

  Wire.begin(PIN_SDA, PIN_SCL);
  scanBus();

  sensorReady = initSensor();
  if (sensorReady) {
    Serial.println("READY");
  } else {
    Serial.println("ERR,sht45_not_found");
  }
}

void loop() {
  const uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) {
    return;
  }
  lastSampleMs = now;

  if (!sensorReady) {
    sensorReady = initSensor();
    if (!sensorReady) {
      Serial.println("ERR,sht45_not_found");
      return;
    }
    Serial.println("READY");
  }

  sensors_event_t humidity, temp;
  if (!sht4.getEvent(&humidity, &temp)) {
    Serial.println("ERR,read_failed");
    sensorReady = false;  // force a re-init on the next tick
    return;
  }

  Serial.printf("D,%lu,%.2f,%.2f\n", (unsigned long)now,
                temp.temperature, humidity.relative_humidity);
}
