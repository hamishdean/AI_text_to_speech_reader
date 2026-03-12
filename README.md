# AI Text-to-Speech Reader

A desktop application that converts text to speech using **OpenAI** or **ElevenLabs** TTS APIs. Load PDFs or Word documents, apply text filters, and listen to or export audio in multiple formats.

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![License](https://img.shields.io/badge/License-MIT-green)

## Features

### Multi-Provider Support
- **OpenAI TTS** — 6 built-in voices (alloy, echo, fable, onyx, nova, shimmer), `tts-1` and `tts-1-hd` models, adjustable speed (1.0x-4.0x)
- **ElevenLabs TTS** — Access your full ElevenLabs voice library (premade + cloned voices), multiple models including `eleven_multilingual_v2` and `eleven_turbo_v2_5`
- Switch between providers with a single dropdown — voice and model lists update automatically

### Reader
- **Load PDF and DOCX** files or paste text directly
- **Batch processing** — text of any length is automatically split into chunks at sentence boundaries and processed through the API
- **Concurrent generation** — configurable parallelism (1-5 threads) generates upcoming batches while current audio plays
- **Stop button** halts playback and cancels remaining batches
- **Progress tracking** with a progress bar and scrollable log

### Filters
Toggle filters to clean up text before sending to the TTS API:

| Filter | Description |
|---|---|
| URLs | Remove `http://` and `https://` links |
| Email Addresses | Remove email addresses |
| Round Brackets | Remove text inside `(...)` |
| Square Brackets | Remove text inside `[...]` |
| Curly Brackets | Remove text inside `{...}` |
| Angle Brackets | Remove HTML/XML tags `<...>` |
| Tables | Remove pipe-separated and tab-delimited table rows |
| Page Numbers | Remove standalone page numbers |
| Headers & Footers | Remove repeated short lines, "Page X of Y" patterns |
| Citations | Remove `[1]`, `(Author, 2020)` style references |
| Special Characters | Remove `# * ~ ^ \ | @ $ % &` |
| Extra Whitespace | Collapse blank lines and trim spaces |
| Footnotes | Remove footnote markers and definitions |

- **Apply Filters** modifies text in place
- **Preview Filtered Text** shows the result in a separate window with a character count diff before committing
- Filters are also applied automatically when pressing Read Aloud or Export

### Export
- **Formats**: MP3, WAV, FLAC, AAC
- **Split by headings** — exports each section as a separate numbered file (e.g., `01_Chapter_1_Introduction.mp3`)
- **Heading detection** finds Chapter/Section/Part headings, numbered headings, Roman numerals, and ALL CAPS lines
- **Detect Headings** button previews sections with character and batch counts
- Export progress bar, log, and cancel button
- Concurrent batch generation for faster exports

## Requirements

- Python 3.8+
- An [OpenAI API key](https://platform.openai.com/api-keys) and/or an [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys)

### Python Dependencies

```
openai
pygame
PyPDF2
python-docx
requests
```

Install with:

```bash
pip install openai pygame PyPDF2 python-docx requests
```

`tkinter` is included with most Python installations. If missing on Linux:

```bash
# Debian/Ubuntu
sudo apt install python3-tk

# Fedora
sudo dnf install python3-tkinter
```

## Usage

```bash
python openai_text_to_speech_reader.py
```

1. Select a **Provider** (OpenAI or ElevenLabs) in the Settings panel
2. Enter the corresponding API key
   - For ElevenLabs, click **Fetch Voices** to load your available voices
3. Load a PDF/DOCX or type/paste text into the text area
4. Select voice, model, speed (OpenAI only), and parallel threads
5. (Optional) Go to the **Filters** tab to enable text cleanup
6. Click **Read Aloud** to listen, or go to the **Export** tab to save audio files

## How Batch Processing Works

TTS APIs have character limits per request (~4096 for OpenAI, ~5000 for ElevenLabs). This app handles text of any length by:

1. Splitting text into ~4000 character chunks at sentence boundaries
2. Submitting multiple chunks concurrently via a thread pool
3. Playing audio in order as chunks finish generating — no gap between batches

```
                    +-- Worker 1 --> Batch 1 --+
ThreadPoolExecutor -+-- Worker 2 --> Batch 2 --+-> Queue -> Player
                    +-- Worker 3 --> Batch 3 --+  (ordered)
```

## Project Structure

```
AI_text_to_speech_reader/
+-- openai_text_to_speech_reader.py   # Main application
+-- README.md
```

## License

MIT
