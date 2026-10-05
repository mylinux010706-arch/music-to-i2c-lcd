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

const char T0000[] PROGMEM = "Hello";
const char T0001[] PROGMEM = "my";
const char T0002[] PROGMEM = "beautiful";
const char T0003[] PROGMEM = "world";
const char T0004[] PROGMEM = "this";
const char T0005[] PROGMEM = "is";
const char T0006[] PROGMEM = "me";
const char T0007[] PROGMEM = "singing";
const char T0008[] PROGMEM = "in";
const char T0009[] PROGMEM = "the";
const char T0010[] PROGMEM = "morning";
const char T0011[] PROGMEM = "light";
const char T0012[] PROGMEM = "internatio-";
const char T0013[] PROGMEM = "nalization";
const char T0014[] PROGMEM = "of";
const char T0015[] PROGMEM = "heart";
const char T0016[] PROGMEM = "and";
const char T0017[] PROGMEM = "night";
const char T0018[] PROGMEM = "calling";
const char T0019[] PROGMEM = "home";

const LcdEvent EVENTS[] PROGMEM = {
    {3000UL, 0, 0, FLAG_SLEEP, 0},
    {4200UL, 0, 0, FLAG_WAKE, T0000},
    {4700UL, 0, 6, 0, T0001},
    {5000UL, 1, 0, 0, T0002},
    {5900UL, 1, 10, 0, T0003},
    {7100UL, 0, 0, FLAG_CLEAR, T0004},
    {7400UL, 0, 5, 0, T0005},
    {7600UL, 0, 8, 0, T0006},
    {8600UL, 1, 0, 0, T0007},
    {9250UL, 1, 8, 0, T0008},
    {9450UL, 1, 11, 0, T0009},
    {9950UL, 0, 0, FLAG_CLEAR, T0010},
    {10400UL, 0, 8, 0, T0011},
    {14300UL, 0, 0, FLAG_SLEEP, 0},
    {15200UL, 0, 0, FLAG_WAKE, T0012},
    {16050UL, 1, 0, 0, T0013},
    {17300UL, 1, 11, 0, T0014},
    {17550UL, 1, 14, 0, T0001},
    {18050UL, 0, 0, FLAG_CLEAR, T0015},
    {19500UL, 0, 6, 0, T0016},
    {19750UL, 0, 10, 0, T0009},
    {20000UL, 1, 0, 0, T0017},
    {20800UL, 1, 6, 0, T0005},
    {21000UL, 1, 9, 0, T0018},
    {21800UL, 0, 0, FLAG_CLEAR, T0006},
    {22400UL, 0, 3, 0, T0019},
    {26300UL, 0, 0, FLAG_SLEEP, 0},
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
