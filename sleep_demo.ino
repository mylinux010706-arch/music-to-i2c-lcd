#include <Wire.h>
#include <LiquidCrystal_I2C.h>

LiquidCrystal_I2C lcd(0x27, 16, 2);

const uint8_t FLAG_CLEAR = 1;
const uint8_t FLAG_WAKE = 2;
const uint8_t FLAG_SLEEP = 4;

struct LcdEvent {
    uint32_t atMs;
    uint8_t row;
    uint8_t col;
    uint8_t flags;
    const char *text;
};

const char T0000[] PROGMEM = "A";
const char T0001[] PROGMEM = "B";
const char T0002[] PROGMEM = "C";
const char T0003[] PROGMEM = "D";

const LcdEvent EVENTS[] PROGMEM = {
    {1000UL, 0, 0, 0, T0000},
    {2000UL, 0, 2, 0, T0001},
    {5500UL, 0, 0, FLAG_SLEEP, 0},
    {6000UL, 0, 0, FLAG_WAKE, T0002},
    {6800UL, 0, 2, 0, T0003},
    {10200UL, 0, 0, FLAG_SLEEP, 0},
};

const uint16_t EVENT_COUNT = sizeof(EVENTS) / sizeof(EVENTS[0]);

uint16_t nextEvent = 0;
uint32_t playbackStart = 0;
bool playing = false;

bool playEventIfDue(uint16_t index, uint32_t elapsed) {
    LcdEvent event;
    memcpy_P(&event, &EVENTS[index], sizeof(LcdEvent));
    if (elapsed < event.atMs) {
        return false;
    }
    if (event.flags & FLAG_SLEEP) {
        lcd.clear();
        lcd.noBacklight();
        return true;
    }
    if (event.flags & FLAG_WAKE) {
        lcd.backlight();
    }
    if (event.flags & FLAG_CLEAR) {
        lcd.clear();
    }
    lcd.setCursor(event.col, event.row);
    lcd.print(reinterpret_cast<const __FlashStringHelper *>(event.text));
    return true;
}

void waitForStart() {
    lcd.backlight();
    for (uint8_t seconds = 3; seconds > 0; seconds--) {
        lcd.clear();
        lcd.setCursor(0, 0);
        lcd.print(F("Starting in"));
        lcd.setCursor(0, 1);
        lcd.print(seconds);
        delay(1000);
    }
}

void beginPlayback() {
    lcd.backlight();
    lcd.clear();
    nextEvent = 0;
    playbackStart = millis();
    playing = true;
}

void setup() {
    lcd.init();
    lcd.backlight();
    waitForStart();
    beginPlayback();
}

void loop() {
    if (!playing) {
        return;
    }
    uint32_t elapsed = millis() - playbackStart;
    while (nextEvent < EVENT_COUNT && playEventIfDue(nextEvent, elapsed)) {
        nextEvent++;
    }
    if (nextEvent >= EVENT_COUNT) {
        playing = false;
    }
}
