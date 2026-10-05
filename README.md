# Music to LCD I2C Generator

A Python program that turns a song file into an Arduino sketch (`.ino`). The sketch shows the lyrics on a 16x2 I2C LCD, following the timing of the song. When the song has no words for a set gap (3 seconds by default), the sketch clears the screen and turns the backlight off, then turns it back on just before the next word appears.

## Workflow

```text
Song file (mp3, wav, m4a, flac, ogg, opus, aac, wma)
    |
FFmpeg: convert to mono 16 kHz FLAC, split into chunks of at most 10 minutes for the API upload limit
    |
Transcription: OpenAI whisper-1, or local faster-whisper without an API key
    |
Word timestamps (fallback: segment timestamps, with word times interpolated)
    |
Timing analysis: start, end, duration and gap between words
    |
16x2 LCD layout with word wrapping
    |
lcd.clear() only when the next word does not fit on two rows
    |
Backlight sleep and wake from the real timestamp gaps
    |
generated_lcd_song.ino
```

## Folder structure

```text
music_to_lcd_i2c/
├── music_to_lcd.py
├── requirements.txt
├── requirements-local.txt
├── .env.example
├── README.md
├── examples/
│   ├── sample_transcript.json
│   ├── generated_lcd_song.ino
│   ├── sleep_demo_transcript.json
│   └── sleep_demo.ino
└── tests/
    ├── helpers.py
    ├── test_timeline.py
    ├── test_arduino_simulation.py
    ├── test_cli_and_io.py
    └── arduino_mock/
        ├── Arduino.h
        ├── Wire.h
        ├── LiquidCrystal_I2C.h
        └── simulator.cpp
```

## Requirements

- Python 3.9 or newer.
- FFmpeg and FFprobe, callable from PATH.
- An OpenAI API key for the default engine, or the `faster-whisper` package for the local engine.
- Arduino IDE with the "LiquidCrystal I2C" library (Frank de Brabander). The `Wire` library ships with the IDE.

## Installation

Create a virtual environment and install the dependencies.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\activate`.

Install FFmpeg, then open a new terminal so that PATH is picked up.

| System | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux (Debian, Ubuntu) | `sudo apt install ffmpeg` |

Copy `.env.example` to `.env`, then fill in your API key. The key is read from an environment variable or from the `.env` file and is never written in the source code. On Windows, use `copy .env.example .env`.

```bash
cp .env.example .env
```

```text
OPENAI_API_KEY=sk-your-key-here
```

To transcribe without an API key, install the local engine. The model is downloaded once, on first use.

```bash
pip install -r requirements-local.txt
```

## Usage

Interactive mode. The program shows a prompt and waits for the path of the song file.

```bash
python music_to_lcd.py
```

```text
Music to LCD I2C Generator

Enter audio file:
>
```

Argument mode:

```bash
python music_to_lcd.py song.mp3
python music_to_lcd.py song.mp3 -o my_song.ino --language en
python music_to_lcd.py song.mp3 --engine local
python music_to_lcd.py song.mp3 --save-transcript song.json
python music_to_lcd.py --load-transcript song.json --sleep-gap 5
```

| Option | Purpose |
|---|---|
| `audio` | Path of the song file. If omitted, the program asks for it. |
| `-o`, `--output` | Name of the output `.ino` file. Default `generated_lcd_song.ino`. |
| `--engine` | `openai` (default) or `local`. |
| `--language` | ISO-639-1 language code of the lyrics, for example `en` or `id`. Auto-detected if omitted. |
| `--offset` | Shift every timestamp by this many seconds. Positive values delay the text. |
| `--sleep-gap` | Seconds without words before the backlight turns off. `0` disables the feature. |
| `--save-transcript` | Save the raw transcription as JSON. |
| `--load-transcript` | Use a transcription JSON instead of processing audio. No API call is made. |
| `--debug` | Show the full traceback when an error happens. |

Before writing the `.ino` file, the program prints the transcription and the layout so you can check them. This is the real output for `examples/sleep_demo_transcript.json`:

```text
=== TRANSCRIPTION RESULT ===

Timestamp source: word-level

[1.00s - 1.50s] A  (dur 0.50s, gap 1.00s)
[2.00s - 2.50s] B  (dur 0.50s, gap 0.50s)
[6.00s - 6.50s] C  (dur 0.50s, gap 3.50s, LCD off during gap)
[6.80s - 7.20s] D  (dur 0.40s, gap 0.30s)

=== LCD LAYOUT ===

Screen 1  [1.00s - 5.50s]
+----------------+
|A B             |
|                |
+----------------+
LCD OFF at 5.50s (no words for 3.00s)

Screen 2  [6.00s - 10.20s]  LCD ON
+----------------+
|C D             |
|                |
+----------------+
LCD OFF at 10.20s (no words for 3.00s)

Generating Arduino file...

Analysis complete.

Audio:
sleep_demo.wav

Duration:
00:12

Detected text:
A B C D

LCD layout:
2 screens, 6 events on a 16x2 LCD
Backlight sleeps 2 time(s), after 3.00s without words

Arduino file generated successfully:

examples/sleep_demo.ino
```

## Configuration

All main settings are at the top of `music_to_lcd.py`.

| Constant | Default | Meaning |
|---|---|---|
| `LCD_ADDRESS` | `0x27` | I2C address of the LCD. |
| `LCD_COLUMNS` | `16` | Number of columns. |
| `LCD_ROWS` | `2` | Number of rows. |
| `MIN_DISPLAY_TIME` | `0.5` | Minimum seconds the last printed text stays visible before the screen is cleared or the LCD sleeps. |
| `MIN_GAP` | `0.1` | Minimum seconds between two text prints. |
| `LCD_SLEEP_GAP` | `3.0` | Seconds without words before the backlight turns off. `0` or `None` disables it. |
| `LCD_DARK_AT_START` | `False` | `True`: the backlight is off from second 0 when the first word only appears after `LCD_SLEEP_GAP` seconds. |
| `OUTPUT_FILE` | `"generated_lcd_song.ino"` | Output file name. |
| `LONG_WORD_MODE` | `"split"` | For words longer than the column count: `"split"` breaks them with a hyphen, `"scroll"` scrolls them across a full row. |
| `SCROLL_STEP_TIME` | `0.3` | Minimum seconds per scroll step in scroll mode. |
| `TIME_OFFSET` | `0.0` | Shift every timestamp, in seconds. |
| `START_COUNTDOWN` | `3` | Countdown shown on the LCD before the timeline starts. `0` for no countdown. |
| `START_BUTTON_PIN` | `None` | Pin number of a start button, wired between the pin and GND. `None` for no button. |
| `LOOP_PLAYBACK` | `False` | Restart from the beginning when the song ends. |
| `END_HOLD_TIME` | `3.0` | Pause before restarting when `LOOP_PLAYBACK` is on. |
| `CREATE_SKETCH_FOLDER` | `False` | Save into a folder with the same name as the sketch, as Arduino IDE expects. |
| `TRANSCRIPTION_ENGINE` | `"openai"` | `"openai"` or `"local"`. |
| `LANGUAGE` | `None` | ISO-639-1 language code, or `None` for auto-detect. |
| `OPENAI_MODEL` | `"whisper-1"` | OpenAI model. It must support word-level timestamps. |
| `LOCAL_MODEL_SIZE` | `"small"` | faster-whisper model size. |

For example, to make the LCD turn off after 5 seconds:

```python
LCD_SLEEP_GAP = 5.0
```

## LCD sleep and wake

This feature works from the audio timestamps, not from a `delay(3000)` after every word. The generator computes the real gap as the start of the next word minus the end of the previous word, then adds sleep and wake events to the same timeline as the text.

The rules:

1. Gap smaller than `LCD_SLEEP_GAP`: the LCD stays on.
2. Gap equal to or larger than `LCD_SLEEP_GAP`: at `end of previous word + LCD_SLEEP_GAP`, the sketch calls `lcd.clear()` and then `lcd.noBacklight()`.
3. Before the next word is printed, the sketch calls `lcd.backlight()`.
4. After a sleep the screen is empty. The next word starts at row 0, column 0 with no extra `lcd.clear()`.
5. A silent intro is treated like a gap between words, with second 0 as the starting point. If the first word appears at second 5, the LCD turns off at second 3 and back on at second 5. To have the LCD dark from second 0 instead, set `LCD_DARK_AT_START = True`.
6. After the last word, the LCD turns off once, `LCD_SLEEP_GAP` seconds later. After that the Arduino does not touch the LCD again (unless `LOOP_PLAYBACK` is on).
7. The time at which each word appears does not change. Sleep and wake are extra events, and a test compares the times of all words with the feature on and off.

Gap comparison uses whole microseconds. A gap of 2.9 seconds is never treated as 3.0, and a gap of 3.0 that is hit by floating-point error (for example `3.3 - (0.1 + 0.2)`) is still counted as 3.0.

Result for the Word A, B, C, D scenario (A 1.0 to 1.5, B 2.0 to 2.5, C 6.0 to 6.5, D 6.8 to 7.2):

```text
1.00s   A      shown
2.00s   B      shown
5.50s          lcd.clear(); lcd.noBacklight();      B ends 2.50 + 3.00
6.00s   C      lcd.backlight(); shown at row 0, column 0
6.80s   D      shown
10.20s         lcd.clear(); lcd.noBacklight();      D ends 7.20 + 3.00
```

Effect of the gap size, when the previous word ends at 10.20:

| Next word starts | Gap | Result |
|---|---|---|
| 12.90 | 2.70 | LCD stays on |
| 13.10 | 2.90 | LCD stays on |
| 13.20 | 3.00 | `noBacklight()` and `backlight()` fall on the same millisecond (the screen is cleared and the backlight only flickers very briefly) |
| 13.70 | 3.50 | LCD turns off at 13.20 and on at 13.70 |
| 15.20 | 5.00 | LCD turns off at 13.20 and on at 15.20 |

Two things protect the text timing:

- A sleep is never scheduled earlier than `MIN_DISPLAY_TIME` after the last text was printed, so the last row stays readable.
- If that rule would push a sleep past the time of the next word, the sleep is moved earlier. A word is never delayed by a sleep.

To turn the feature off, set `LCD_SLEEP_GAP = 0` (or `None`), or run with `--sleep-gap 0`.

## 16x2 LCD layout

- Words are placed one at a time, at their start time.
- If a word fits on the current row (separated by one space), it goes there. If not, it moves to the next row.
- If the two rows cannot hold the next word, the sketch waits for that word's time, calls `lcd.clear()` and starts again at row 0, column 0. The screen is not cleared on every word.
- Words are never cut at random. The exception is a word longer than the column count, which `LONG_WORD_MODE` handles.
- With `"split"`, a 17-letter word becomes `abcdefghi-` and `jklmnopq`. Every part always fits on one row.
- With `"scroll"`, the word fills one full row and scrolls for the duration of the word.
- Non-ASCII characters are converted to ASCII (for example `é` becomes `e`) because the character ROM of typical HD44780 displays does not cover them. Tags such as `[Music]` are removed.

`MIN_GAP` and `MIN_DISPLAY_TIME` only delay an event when timestamps are too close together. If any event is delayed, the program prints a `Timing note` with the number of delayed events and the largest delay.

## How the sketch works

The sketch stores all events in one table in PROGMEM, each with an absolute time in milliseconds since the timeline started. In `loop()`, the sketch compares `millis() - playbackStart` with the time of the next event. Because the times are absolute, there is no drift from adding up `delay()` calls, and every event that is already due runs in order.

Each event carries the flag `FLAG_CLEAR`, `FLAG_WAKE` or `FLAG_SLEEP`. Excerpt of the result (`examples/sleep_demo.ino`):

```cpp
const LcdEvent EVENTS[] PROGMEM = {
    {1000UL, 0, 0, 0, T0000},
    {2000UL, 0, 2, 0, T0001},
    {5500UL, 0, 0, FLAG_SLEEP, 0},
    {6000UL, 0, 0, FLAG_WAKE, T0002},
    {6800UL, 0, 2, 0, T0003},
    {10200UL, 0, 0, FLAG_SLEEP, 0},
};
```

```cpp
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
```

The sketch starts with:

```cpp
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

LiquidCrystal_I2C lcd(0x27, 16, 2);
```

and `setup()` calls `lcd.init()` and `lcd.backlight()`. The generated `.ino` code contains no comments, and neither does the Python source of this project.

## Saving and correcting the transcription

Song transcriptions are rarely perfect. Save the result, fix wrong words or timestamps in the JSON file, then rebuild the sketch without calling the API again.

```bash
python music_to_lcd.py song.mp3 --save-transcript song.json
python music_to_lcd.py --load-transcript song.json -o generated_lcd_song.ino
```

The JSON format matches OpenAI's `verbose_json`, so entries keyed by either `word` or `text` are read:

```json
{
  "audio": "song.mp3",
  "language": "en",
  "duration": 222.5,
  "text": "Hello world",
  "words": [
    {"text": "Hello", "start": 1.2, "end": 1.65},
    {"text": "world", "start": 1.7, "end": 2.2}
  ],
  "segments": [
    {"text": "Hello world", "start": 1.2, "end": 2.2}
  ]
}
```

If `words` is empty but `segments` is filled, the program uses the segment timestamps and spreads each segment's time over its words in proportion to word length. A full example is in `examples/sample_transcript.json`.

## Uploading to Arduino

1. Install the "LiquidCrystal I2C" library from the Arduino IDE Library Manager.
2. Connect the I2C module: GND to GND, VCC to 5V, SDA and SCL to the I2C pins of your board (Uno and Nano: A4 and A5, Mega: 20 and 21, ESP32: GPIO 21 and 22).
3. Open the `.ino` file. Arduino IDE expects the file to sit in a folder with the same name. Accept the automatic folder creation, or set `CREATE_SKETCH_FOLDER = True` before generating the sketch.
4. Select the board and port, then upload.

If the display is lit but blank, turn the contrast potentiometer on the back of the module. If it stays blank, the module address may be `0x3F` instead of `0x27`. Find it with an I2C scanner sketch, then change `LCD_ADDRESS` and generate the sketch again.

Synchronizing with the song is manual. There are two ways:

- A 3-second countdown shows on the LCD after reset. Start the song exactly when the countdown ends.
- Set `START_BUTTON_PIN = 2`, wire a button between pin 2 and GND, and press the button at the same moment you start the song.

If the text on the LCD feels too early or too late compared with the song, generate the sketch again with `--offset`, for example `--offset 0.3` to delay the text by 0.3 seconds.

If the lyric data exceeds about 24 KB, the generator prints a warning because the flash of an Arduino Uno or Nano is only 32 KB. For long songs, use a board with more flash, such as an ESP32.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The suite has 127 tests:

- `test_timeline.py`: the sleep and wake timeline, word wrapping, LCD clear, timing, text sanitizing, the content of the Arduino code, and a check that the sources contain no comments.
- `test_arduino_simulation.py`: the generated sketches are compiled with `g++` against fake Arduino headers in `tests/arduino_mock`, run on a virtual clock, and the log of LCD calls is compared exactly with the Python timeline. The simulator also flags violations: text printed while the backlight is off, or text going past the edge of the display.
- `test_cli_and_io.py`: CLI options, error messages, fake `openai` and `faster_whisper` modules, real FFmpeg conversion for eight audio formats, and the pipeline from audio file to `.ino`.

Tests that need `g++` or FFmpeg are skipped automatically when the program is missing. The simulation uses fake headers, not the `avr-gcc` toolchain and the real LiquidCrystal I2C library, so the first compile in Arduino IDE remains the last verification step.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `ffmpeg was not found in PATH` | Install FFmpeg, then open a new terminal. |
| `OPENAI_API_KEY is not set` | Fill in `.env` or export the variable. Or use `--engine local`. |
| `Could not connect to the OpenAI API` | Check your internet connection. |
| `OpenAI rate limit or quota exceeded` | Check the balance and usage limits of your OpenAI account. |
| `No clear speech was detected` | The vocals are too quiet compared with the music. Use a version of the song with clearer vocals, or set `--language`. |
| Wrong or missing words | Whisper has to pick lyrics out of the music, so some errors are normal. Save with `--save-transcript`, fix the JSON, then use `--load-transcript`. |
| The LCD rarely turns off although the song has long pauses | Whisper sometimes stretches the `end` of a word over the pause. Shorten the `end` value in the transcription JSON. |
| `Unexpected error` appears | Run again with `--debug` to see the full traceback. |
