/*
  One Wire polling
  Polls temperature sensors over one-wire, and blinks internal LED on every read
*/
#include <OneWire.h>
#include <DallasTemperature.h>
#include <SoftwareSerial.h>

// Data wire is plugged into PB0 (pin 5) on the Attiny85
#define ONE_WIRE_BUS PB0
// Serial communication on PB3, PB4
#define RX_PIN PB3
#define TX_PIN PB4

SoftwareSerial tinySerial(RX_PIN, TX_PIN);

// Setup a oneWire instance to communicate with any OneWire device
OneWire oneWire(ONE_WIRE_BUS); 
// Pass oneWire reference to DallasTemperature library
DallasTemperature sensors(&oneWire);

// the setup function runs once when you press reset or power the board
void setup() {
  // initialize digital pin LED_BUILTIN as an output.
  pinMode(LED_BUILTIN, OUTPUT);
  sensors.begin(); // Start up the sensors library
  //Serial.begin(9600); // Open serial
  tinySerial.begin(9600);
}

// the loop function runs over and over again forever
void loop() {
  // wait with LED on
  digitalWrite(LED_BUILTIN, HIGH);   // turn the LED on (HIGH is the voltage level)
  delay(250);                        // wait

  // Send the command to get temperatures
  sensors.requestTemperatures(); 

  //print the temperatures in Celsius
  for (uint8_t i = 0; i < 2; i++) {
    tinySerial.print('T');
    tinySerial.print(i);
    tinySerial.print(':');
    tinySerial.print(sensors.getTempCByIndex(i));
    tinySerial.print('|');
  }
 
  // wait with LED off
  digitalWrite(LED_BUILTIN, LOW);    // turn the LED off by making the voltage LOW
  delay(750);                        // wait
}
