# -*- coding: utf-8 -*-


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import concurrent.futures
import re
import os
import shutil
import tempfile
import json
import docx
import PyPDF2
import pygame
import requests
from openai import OpenAI, AuthenticationError, APIConnectionError

# Maximum characters per TTS API request
BATCH_CHAR_LIMIT = 4000


def split_text_into_batches(text, limit=BATCH_CHAR_LIMIT):
    """Split text into batches that respect sentence boundaries where possible."""
    batches = []
    while text:
        if len(text) <= limit:
            batches.append(text)
            break
        # Try to split at the last sentence-ending punctuation within the limit
        chunk = text[:limit]
        split_pos = -1
        for sep in ['. ', '.\n', '! ', '!\n', '? ', '?\n']:
            pos = chunk.rfind(sep)
            if pos > split_pos:
                split_pos = pos + 1  # include the punctuation
        if split_pos <= 0:
            # Fall back to last space
            split_pos = chunk.rfind(' ')
        if split_pos <= 0:
            # No good break point, hard split
            split_pos = limit
        part = text[:split_pos].strip()
        if part:
            batches.append(part)
        text = text[split_pos:].strip()
    return batches


def apply_filters(text, filters):
    """Apply the enabled filters to text and return the cleaned version."""
    if filters.get("urls"):
        text = re.sub(r'https?://\S+', '', text)

    if filters.get("emails"):
        text = re.sub(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', '', text)

    if filters.get("round_brackets"):
        text = re.sub(r'\([^)]*\)', '', text)

    if filters.get("square_brackets"):
        text = re.sub(r'\[[^\]]*\]', '', text)

    if filters.get("curly_brackets"):
        text = re.sub(r'\{[^}]*\}', '', text)

    if filters.get("angle_brackets"):
        text = re.sub(r'<[^>]*>', '', text)

    if filters.get("tables"):
        # Remove lines that look like table rows (contain multiple | or tab separators)
        lines = text.split('\n')
        filtered = []
        for line in lines:
            stripped = line.strip()
            # Table separator lines like |---|---|
            if re.match(r'^[\s|+\-:=]+$', stripped) and '|' in stripped:
                continue
            # Lines with 2+ pipe separators (table cells)
            if stripped.count('|') >= 2:
                continue
            # Lines with 3+ tab separators (tab-delimited tables)
            if stripped.count('\t') >= 2:
                continue
            filtered.append(line)
        text = '\n'.join(filtered)

    if filters.get("page_numbers"):
        # Standalone page numbers (lines that are just a number, optionally with "Page" prefix)
        text = re.sub(r'(?m)^\s*(?:Page\s*)?\d{1,5}\s*$', '', text, flags=re.IGNORECASE)

    if filters.get("headers_footers"):
        # Remove lines that are likely headers/footers:
        # - Very short lines (<=5 chars) that are all caps or just numbers/symbols
        # - Common header/footer patterns
        lines = text.split('\n')
        filtered = []
        for line in lines:
            stripped = line.strip()
            # Skip very short all-caps lines (likely headers)
            if 0 < len(stripped) <= 5 and stripped.isupper():
                continue
            # Common footer patterns
            if re.match(r'^\s*[-—]\s*\d+\s*[-—]\s*$', stripped):
                continue
            # "Page X of Y" patterns
            if re.match(r'^\s*page\s+\d+\s+(of|/)\s+\d+\s*$', stripped, re.IGNORECASE):
                continue
            filtered.append(line)
        text = '\n'.join(filtered)

    if filters.get("citations"):
        # Remove citation markers like [1], [2,3], (Author, 2020), (Author et al., 2020)
        text = re.sub(r'\[\d+(?:[,;\s]+\d+)*\]', '', text)
        text = re.sub(r'\([A-Z][a-z]+(?:\s+(?:et\s+al\.|and|&)\s+[A-Z][a-z]+)*,?\s*\d{4}[a-z]?\)', '', text)

    if filters.get("special_chars"):
        # Remove standalone special characters and symbols, keep basic punctuation
        text = re.sub(r'[#*~^\\|`@$%&]+', '', text)

    if filters.get("extra_whitespace"):
        # Collapse multiple blank lines into one, trim trailing spaces
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'(?m)^ +| +$', '', text)

    if filters.get("footnotes"):
        # Remove footnote markers (superscript-style numbers) and footnote lines
        # Lines starting with a small number followed by text (footnote definitions)
        text = re.sub(r'(?m)^\s{0,4}\d{1,3}[\.\)]\s+.{0,200}$', '', text)

    return text.strip()


def split_text_by_headings(text):
    """Split text into sections based on detected headings.
    Returns a list of (heading, body_text) tuples."""
    heading_pattern = re.compile(
        r'^('
        r'(?:Chapter\s+\d+[.:]?\s*.*)'        # Chapter 1: Title
        r'|(?:Part\s+\w+[.:]?\s*.*)'           # Part One: Title
        r'|(?:Section\s+\d+[.:]?\s*.*)'         # Section 1: Title
        r'|(?:\d+(?:\.\d+)*\.?\s+[A-Z].*)'     # 1. Title or 1.2.3 Title
        r'|(?:[IVXLCDM]+\.\s+.*)'              # IV. Title (Roman numerals)
        r'|(?:[A-Z][A-Z\s]{2,}[A-Z])$'         # ALL CAPS LINE (min 4 chars)
        r')',
        re.MULTILINE
    )

    matches = list(heading_pattern.finditer(text))

    if not matches:
        return [("Full Text", text)]

    sections = []
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()

        # Include any text before the first heading as an intro section
        if i == 0 and match.start() > 0:
            intro = text[:match.start()].strip()
            if intro:
                sections.append(("Introduction", intro))

        if body:
            sections.append((heading, body))

    return sections if sections else [("Full Text", text)]


def sanitize_filename(name, max_len=50):
    """Create a safe filename from a heading string."""
    name = re.sub(r'[^\w\s\-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:max_len] if name else "untitled"


class TTSApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI Text-to-Speech Reader")
        self.root.geometry("700x750")
        self.root.minsize(500, 550)

        # Initialize pygame mixer for audio playback
        pygame.mixer.init()

        # Variables
        self.provider_var = tk.StringVar(value="OpenAI")
        self.api_key_var = tk.StringVar()
        self.elevenlabs_api_key_var = tk.StringVar()
        self.voice_var = tk.StringVar(value="alloy")
        self.model_var = tk.StringVar(value="tts-1")
        self.speed_var = tk.StringVar(value="1.0x")
        self.concurrency_var = tk.StringVar(value="3")

        # OpenAI options
        self.openai_voices = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
        self.openai_models = ["tts-1", "tts-1-hd"]

        # ElevenLabs options
        self.elevenlabs_voices = {}  # {display_name: voice_id}
        self.elevenlabs_voice_names = ["(Fetch voices first)"]
        self.elevenlabs_models = [
            "eleven_v3",
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
            "eleven_monolingual_v1",
            "eleven_flash_v2_5",
        ]

        self.voices = self.openai_voices
        self.models = self.openai_models
        self.speeds = ["1.0x", "1.25x", "1.5x", "1.75x", "2.0x", "2.5x", "3.0x", "4.0x"]
        self.concurrency_options = ["1", "2", "3", "4", "5"]
        self.stop_requested = False
        self.is_processing = False
        self.batch_temp_files = []

        # Export variables
        self.export_format_var = tk.StringVar(value="mp3")
        self.export_split_var = tk.BooleanVar(value=False)
        self.is_exporting = False
        self.export_stop_requested = False

        # Filter toggle variables
        self.filter_vars = {}
        self.filter_definitions = [
            ("urls",            "URLs",                     "Remove http:// and https:// links"),
            ("emails",          "Email Addresses",          "Remove email addresses"),
            ("round_brackets",  "Round Brackets (...)",     "Remove text inside parentheses"),
            ("square_brackets", "Square Brackets [...]",    "Remove text inside square brackets"),
            ("curly_brackets",  "Curly Brackets {...}",     "Remove text inside curly braces"),
            ("angle_brackets",  "Angle Brackets <...>",     "Remove HTML/XML tags and angle bracket content"),
            ("tables",          "Tables",                   "Remove table rows (pipe-separated or tab-delimited)"),
            ("page_numbers",    "Page Numbers",             "Remove standalone page numbers"),
            ("headers_footers", "Headers & Footers",        "Remove repeated short lines, 'Page X of Y' patterns"),
            ("citations",       "Citations & References",   "Remove citation markers like [1] or (Author, 2020)"),
            ("special_chars",   "Special Characters",       "Remove symbols like # * ~ ^ \\ | ` @ $ % &"),
            ("extra_whitespace","Extra Whitespace",         "Collapse multiple blank lines and trim spaces"),
            ("footnotes",       "Footnotes",                "Remove footnote markers and footnote definition lines"),
        ]
        for key, _, _ in self.filter_definitions:
            self.filter_vars[key] = tk.BooleanVar(value=False)

        self.create_widgets()

    def create_widgets(self):
        # Main Frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Top Section: Settings ---
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding="10")
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        # Provider selection
        ttk.Label(settings_frame, text="Provider:").grid(row=0, column=0, sticky=tk.W, pady=5)
        provider_dropdown = ttk.Combobox(
            settings_frame, textvariable=self.provider_var,
            values=["OpenAI", "ElevenLabs"], state="readonly", width=15
        )
        provider_dropdown.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        provider_dropdown.bind("<<ComboboxSelected>>", self.on_provider_change)

        # OpenAI API Key
        self.openai_key_label = ttk.Label(settings_frame, text="OpenAI API Key:")
        self.openai_key_label.grid(row=1, column=0, sticky=tk.W, pady=5)
        self.openai_key_entry = ttk.Entry(settings_frame, textvariable=self.api_key_var, show="*", width=50)
        self.openai_key_entry.grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5)

        # ElevenLabs API Key (hidden by default)
        self.elevenlabs_key_label = ttk.Label(settings_frame, text="ElevenLabs API Key:")
        self.elevenlabs_key_entry = ttk.Entry(settings_frame, textvariable=self.elevenlabs_api_key_var, show="*", width=40)
        self.fetch_voices_btn = ttk.Button(settings_frame, text="Fetch Voices", command=self.fetch_elevenlabs_voices)

        # Voice / Model row
        ttk.Label(settings_frame, text="Voice:").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.voice_dropdown = ttk.Combobox(settings_frame, textvariable=self.voice_var, values=self.voices, state="readonly", width=25)
        self.voice_dropdown.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(settings_frame, text="Model:").grid(row=3, column=2, sticky=tk.W, padx=(15, 0), pady=5)
        self.model_dropdown = ttk.Combobox(settings_frame, textvariable=self.model_var, values=self.models, state="readonly", width=22)
        self.model_dropdown.grid(row=3, column=3, sticky=tk.W, padx=5, pady=5)

        # Speed / Parallel row
        self.speed_label = ttk.Label(settings_frame, text="Speed:")
        self.speed_label.grid(row=4, column=0, sticky=tk.W, pady=5)
        self.speed_dropdown = ttk.Combobox(settings_frame, textvariable=self.speed_var, values=self.speeds, state="readonly", width=10)
        self.speed_dropdown.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(settings_frame, text="Parallel:").grid(row=4, column=2, sticky=tk.W, padx=(15, 0), pady=5)
        concurrency_dropdown = ttk.Combobox(settings_frame, textvariable=self.concurrency_var, values=self.concurrency_options, state="readonly", width=5)
        concurrency_dropdown.grid(row=4, column=3, sticky=tk.W, padx=5, pady=5)

        settings_frame.columnconfigure(1, weight=1)

        # --- Tabbed Notebook ---
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # === Tab 1: Reader ===
        reader_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(reader_tab, text="Reader")

        # Text Content
        text_frame = ttk.LabelFrame(reader_tab, text="Text Content", padding="10")
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        toolbar = ttk.Frame(text_frame)
        toolbar.pack(fill=tk.X, pady=(0, 5))

        ttk.Button(toolbar, text="Load PDF", command=self.load_pdf).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="Load DOCX", command=self.load_docx).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="Clear Text", command=self.clear_text).pack(side=tk.LEFT)

        self.text_area = tk.Text(text_frame, wrap=tk.WORD, font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(text_frame, command=self.text_area.yview)
        self.text_area.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Batch Progress
        batch_frame = ttk.LabelFrame(reader_tab, text="Batch Progress", padding="10")
        batch_frame.pack(fill=tk.X, pady=(0, 5))

        self.batch_progress_var = tk.StringVar(value="No batches to process.")
        self.batch_progress_label = ttk.Label(batch_frame, textvariable=self.batch_progress_var, wraplength=620)
        self.batch_progress_label.pack(fill=tk.X)

        self.progress_bar = ttk.Progressbar(batch_frame, mode='determinate')
        self.progress_bar.pack(fill=tk.X, pady=(5, 0))

        self.batch_log = tk.Text(batch_frame, height=4, wrap=tk.WORD, font=("Segoe UI", 9), state=tk.DISABLED)
        batch_scroll = ttk.Scrollbar(batch_frame, command=self.batch_log.yview)
        self.batch_log.configure(yscrollcommand=batch_scroll.set)
        batch_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.batch_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))

        # === Tab 2: Filters ===
        filters_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(filters_tab, text="Filters")
        self.create_filters_tab(filters_tab)

        # === Tab 3: Export ===
        export_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(export_tab, text="Export")
        self.create_export_tab(export_tab)

        # --- Bottom Section: Controls (always visible) ---
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(5, 0))

        self.play_btn = ttk.Button(control_frame, text="▶ Read Aloud", command=self.start_reading, style="Accent.TButton")
        self.play_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(control_frame, text="⏹ Stop", command=self.stop_audio, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready.")
        status_label = ttk.Label(control_frame, textvariable=self.status_var, foreground="gray")
        status_label.pack(side=tk.RIGHT)

    def on_provider_change(self, event=None):
        """Update UI when the TTS provider is changed."""
        provider = self.provider_var.get()

        if provider == "OpenAI":
            # Show OpenAI key, hide ElevenLabs key
            self.openai_key_label.grid(row=1, column=0, sticky=tk.W, pady=5)
            self.openai_key_entry.grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5)
            self.elevenlabs_key_label.grid_forget()
            self.elevenlabs_key_entry.grid_forget()
            self.fetch_voices_btn.grid_forget()

            # Update voice and model lists
            self.voices = self.openai_voices
            self.models = self.openai_models
            self.voice_dropdown.config(values=self.voices)
            self.model_dropdown.config(values=self.models)
            if self.voice_var.get() not in self.voices:
                self.voice_var.set(self.voices[0])
            self.model_var.set(self.models[0])

            # Show speed (OpenAI supports it)
            self.speed_label.grid(row=4, column=0, sticky=tk.W, pady=5)
            self.speed_dropdown.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)

        elif provider == "ElevenLabs":
            # Hide OpenAI key, show ElevenLabs key
            self.openai_key_label.grid_forget()
            self.openai_key_entry.grid_forget()
            self.elevenlabs_key_label.grid(row=1, column=0, sticky=tk.W, pady=5)
            self.elevenlabs_key_entry.grid(row=1, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=5)
            self.fetch_voices_btn.grid(row=1, column=3, sticky=tk.W, padx=5, pady=5)

            # Update voice and model lists
            self.voices = self.elevenlabs_voice_names
            self.models = self.elevenlabs_models
            self.voice_dropdown.config(values=self.voices)
            self.model_dropdown.config(values=self.models)
            if self.voice_var.get() not in self.voices:
                self.voice_var.set(self.voices[0])
            self.model_var.set(self.models[0])

            # Show speed (ElevenLabs supports speed parameter)
            self.speed_label.grid(row=4, column=0, sticky=tk.W, pady=5)
            self.speed_dropdown.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)

    def fetch_elevenlabs_voices(self):
        """Fetch available voices from the ElevenLabs API."""
        api_key = self.elevenlabs_api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("Missing API Key", "Please enter your ElevenLabs API key.")
            return

        self.status_var.set("Fetching ElevenLabs voices...")
        self.root.update()

        def do_fetch():
            try:
                resp = requests.get(
                    "https://api.elevenlabs.io/v1/voices",
                    headers={"xi-api-key": api_key},
                    timeout=15,
                )
                if resp.status_code == 401:
                    self.root.after(0, lambda: messagebox.showerror("Error", "Invalid ElevenLabs API key."))
                    self.root.after(0, lambda: self.status_var.set("Ready."))
                    return
                resp.raise_for_status()
                data = resp.json()
                voices = data.get("voices", [])

                self.elevenlabs_voices = {}
                for v in voices:
                    name = v.get("name", "Unknown")
                    vid = v.get("voice_id", "")
                    category = v.get("category", "")
                    display = f"{name} ({category})" if category else name
                    self.elevenlabs_voices[display] = vid

                self.elevenlabs_voice_names = sorted(self.elevenlabs_voices.keys())

                def update_ui():
                    self.voices = self.elevenlabs_voice_names
                    self.voice_dropdown.config(values=self.voices)
                    if self.voices:
                        self.voice_var.set(self.voices[0])
                    self.status_var.set(f"Loaded {len(self.voices)} ElevenLabs voice(s).")

                self.root.after(0, update_ui)

            except requests.exceptions.ConnectionError:
                self.root.after(0, lambda: messagebox.showerror("Error", "Network error. Check your connection."))
                self.root.after(0, lambda: self.status_var.set("Ready."))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to fetch voices:\n{err}"))
                self.root.after(0, lambda: self.status_var.set("Ready."))

        threading.Thread(target=do_fetch, daemon=True).start()

    def generate_tts_audio(self, text, voice, model, speed, response_format, output_path,
                           provider=None, api_key=None):
        """Generate TTS audio using the specified provider.
        provider and api_key should be passed explicitly to avoid
        reading tkinter StringVars from background threads."""
        if provider is None:
            provider = self.provider_var.get()
        if api_key is None:
            api_key = self.get_current_api_key()

        if provider == "OpenAI":
            client = OpenAI(api_key=api_key)
            response = client.audio.speech.create(
                model=model,
                voice=voice,
                input=text,
                speed=speed,
                response_format=response_format,
            )
            response.stream_to_file(output_path)

        elif provider == "ElevenLabs":
            voice_id = self.elevenlabs_voices.get(voice, voice)

            body = {
                "text": text,
                "model_id": model,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                },
            }

            # ElevenLabs speed parameter (supported range ~0.7-1.2 depending on model)
            if speed != 1.0:
                body["speed"] = speed

            # Map export format to ElevenLabs output_format parameter
            el_format_map = {
                "mp3": "mp3_44100_128",
                "wav": "pcm_44100",
                "flac": "mp3_44100_128",  # ElevenLabs doesn't support FLAC, fallback to mp3
                "aac": "mp3_44100_128",   # ElevenLabs doesn't support AAC, fallback to mp3
            }
            output_format = el_format_map.get(response_format, "mp3_44100_128")

            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json=body,
                params={"output_format": output_format},
                timeout=120,
            )

            if resp.status_code == 401:
                raise Exception("Invalid ElevenLabs API key.")
            if resp.status_code != 200:
                raise Exception(f"ElevenLabs API error ({resp.status_code}): {resp.text[:300]}")

            with open(output_path, 'wb') as f:
                f.write(resp.content)

        return output_path

    def get_current_api_key(self):
        """Return the API key for the active provider. Call from main thread only."""
        if self.provider_var.get() == "ElevenLabs":
            return self.elevenlabs_api_key_var.get().strip()
        return self.api_key_var.get().strip()

    def create_filters_tab(self, parent):
        """Build the Filters tab with checkbuttons and action buttons."""
        # Description
        desc_label = ttk.Label(
            parent,
            text="Select filters to clean up text before sending to the TTS API. "
                 "Use 'Apply Filters' to modify the text in place, or 'Preview' to see the result first.",
            wraplength=620
        )
        desc_label.pack(fill=tk.X, pady=(0, 10))

        # Filter checkbuttons in a scrollable frame
        filter_container = ttk.LabelFrame(parent, text="Available Filters", padding="10")
        filter_container.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Canvas + scrollbar for the filter list
        canvas = tk.Canvas(filter_container, highlightthickness=0)
        filter_scrollbar = ttk.Scrollbar(filter_container, orient=tk.VERTICAL, command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=filter_scrollbar.set)

        # Populate filter checkbuttons
        for i, (key, label, description) in enumerate(self.filter_definitions):
            row_frame = ttk.Frame(scrollable_frame)
            row_frame.pack(fill=tk.X, pady=2)

            cb = ttk.Checkbutton(row_frame, text=label, variable=self.filter_vars[key])
            cb.pack(side=tk.LEFT)

            desc = ttk.Label(row_frame, text=f"  -  {description}", foreground="gray")
            desc.pack(side=tk.LEFT, padx=(5, 0))

        filter_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Enable mouse wheel scrolling on the canvas
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def on_mousewheel_linux(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")

        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel_linux)
        canvas.bind("<Button-5>", on_mousewheel_linux)

        # Action buttons
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Button(btn_frame, text="Select All", command=self.select_all_filters).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Deselect All", command=self.deselect_all_filters).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Apply Filters to Text", command=self.apply_filters_to_text).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Preview Filtered Text", command=self.preview_filtered_text).pack(side=tk.LEFT)

    def select_all_filters(self):
        for var in self.filter_vars.values():
            var.set(True)

    def deselect_all_filters(self):
        for var in self.filter_vars.values():
            var.set(False)

    def get_active_filters(self):
        """Return a dict of filter_key -> True for all enabled filters."""
        return {key: var.get() for key, var in self.filter_vars.items() if var.get()}

    def apply_filters_to_text(self):
        """Apply enabled filters to the text area content in place."""
        text = self.text_area.get(1.0, tk.END).strip()
        if not text:
            messagebox.showinfo("No Text", "There is no text to filter.")
            return

        active = self.get_active_filters()
        if not active:
            messagebox.showinfo("No Filters", "No filters are selected.")
            return

        filtered = apply_filters(text, active)
        self.text_area.delete(1.0, tk.END)
        self.text_area.insert(tk.END, filtered)

        enabled_names = [label for key, label, _ in self.filter_definitions if self.filter_vars[key].get()]
        self.status_var.set(f"Applied {len(enabled_names)} filter(s).")

    def preview_filtered_text(self):
        """Show filtered text in a preview window without modifying the original."""
        text = self.text_area.get(1.0, tk.END).strip()
        if not text:
            messagebox.showinfo("No Text", "There is no text to preview.")
            return

        active = self.get_active_filters()
        if not active:
            messagebox.showinfo("No Filters", "No filters are selected.")
            return

        filtered = apply_filters(text, active)
        original_len = len(text)
        filtered_len = len(filtered)
        removed = original_len - filtered_len

        # Open preview window
        preview_win = tk.Toplevel(self.root)
        preview_win.title("Filtered Text Preview")
        preview_win.geometry("600x500")
        preview_win.minsize(400, 300)

        info_frame = ttk.Frame(preview_win, padding="10")
        info_frame.pack(fill=tk.X)

        enabled_names = [label for key, label, _ in self.filter_definitions if self.filter_vars[key].get()]
        ttk.Label(info_frame, text=f"Filters: {', '.join(enabled_names)}").pack(anchor=tk.W)
        ttk.Label(
            info_frame,
            text=f"Original: {original_len} chars | Filtered: {filtered_len} chars | Removed: {removed} chars",
            foreground="gray"
        ).pack(anchor=tk.W, pady=(2, 0))

        text_frame = ttk.Frame(preview_win, padding="10")
        text_frame.pack(fill=tk.BOTH, expand=True)

        preview_text = tk.Text(text_frame, wrap=tk.WORD, font=("Segoe UI", 10))
        preview_scroll = ttk.Scrollbar(text_frame, command=preview_text.yview)
        preview_text.configure(yscrollcommand=preview_scroll.set)
        preview_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        preview_text.insert(tk.END, filtered)
        preview_text.config(state=tk.DISABLED)

        btn_frame = ttk.Frame(preview_win, padding="10")
        btn_frame.pack(fill=tk.X)

        def use_filtered():
            self.text_area.delete(1.0, tk.END)
            self.text_area.insert(tk.END, filtered)
            self.status_var.set(f"Applied {len(enabled_names)} filter(s).")
            preview_win.destroy()

        ttk.Button(btn_frame, text="Use This Text", command=use_filtered).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Close", command=preview_win.destroy).pack(side=tk.LEFT)

    def create_export_tab(self, parent):
        """Build the Export tab with format selection, heading split, and export controls."""
        # Description
        ttk.Label(
            parent,
            text="Export the text as audio files. Choose a format, optionally split by headings, "
                 "and select an output folder.",
            wraplength=620
        ).pack(fill=tk.X, pady=(0, 10))

        # --- Export Settings ---
        settings_frame = ttk.LabelFrame(parent, text="Export Settings", padding="10")
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        # Format selection
        format_frame = ttk.Frame(settings_frame)
        format_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(format_frame, text="Format:").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(format_frame, text="MP3", variable=self.export_format_var, value="mp3").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(format_frame, text="WAV", variable=self.export_format_var, value="wav").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(format_frame, text="FLAC", variable=self.export_format_var, value="flac").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(format_frame, text="AAC", variable=self.export_format_var, value="aac").pack(side=tk.LEFT)

        # Split by headings option
        split_frame = ttk.Frame(settings_frame)
        split_frame.pack(fill=tk.X, pady=(5, 0))

        ttk.Checkbutton(
            split_frame,
            text="Split into separate files by headings",
            variable=self.export_split_var,
            command=self.on_split_toggle
        ).pack(side=tk.LEFT)

        ttk.Button(split_frame, text="Detect Headings", command=self.detect_headings).pack(side=tk.RIGHT)

        # --- Heading Preview ---
        self.heading_frame = ttk.LabelFrame(parent, text="Detected Headings", padding="10")
        self.heading_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.heading_list = tk.Text(self.heading_frame, height=6, wrap=tk.WORD, font=("Segoe UI", 9), state=tk.DISABLED)
        heading_scroll = ttk.Scrollbar(self.heading_frame, command=self.heading_list.yview)
        self.heading_list.configure(yscrollcommand=heading_scroll.set)
        heading_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.heading_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- Export Progress ---
        progress_frame = ttk.LabelFrame(parent, text="Export Progress", padding="10")
        progress_frame.pack(fill=tk.X, pady=(0, 10))

        self.export_progress_var = tk.StringVar(value="No export in progress.")
        ttk.Label(progress_frame, textvariable=self.export_progress_var, wraplength=620).pack(fill=tk.X)

        self.export_progress_bar = ttk.Progressbar(progress_frame, mode='determinate')
        self.export_progress_bar.pack(fill=tk.X, pady=(5, 0))

        self.export_log = tk.Text(progress_frame, height=4, wrap=tk.WORD, font=("Segoe UI", 9), state=tk.DISABLED)
        export_scroll = ttk.Scrollbar(progress_frame, command=self.export_log.yview)
        self.export_log.configure(yscrollcommand=export_scroll.set)
        export_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.export_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))

        # --- Export Buttons ---
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X)

        self.export_btn = ttk.Button(btn_frame, text="Export Audio", command=self.start_export)
        self.export_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.export_stop_btn = ttk.Button(btn_frame, text="Cancel Export", command=self.cancel_export, state=tk.DISABLED)
        self.export_stop_btn.pack(side=tk.LEFT)

    def on_split_toggle(self):
        """When split by headings is toggled, auto-detect headings."""
        if self.export_split_var.get():
            self.detect_headings()

    def detect_headings(self):
        """Detect headings in the text and show them in the preview list."""
        text = self.text_area.get(1.0, tk.END).strip()
        if not text:
            messagebox.showinfo("No Text", "Load or enter text first.")
            return

        # Apply filters if any are active
        active_filters = self.get_active_filters()
        if active_filters:
            text = apply_filters(text, active_filters)

        sections = split_text_by_headings(text)

        self.heading_list.config(state=tk.NORMAL)
        self.heading_list.delete(1.0, tk.END)
        for i, (heading, body) in enumerate(sections, 1):
            chars = len(body)
            batches = len(split_text_into_batches(body))
            self.heading_list.insert(tk.END, f"{i}. {heading}  ({chars} chars, {batches} batch(es))\n")
        self.heading_list.config(state=tk.DISABLED)

        self.status_var.set(f"Detected {len(sections)} section(s).")

    def log_export(self, message):
        """Append a message to the export log."""
        self.export_log.config(state=tk.NORMAL)
        self.export_log.insert(tk.END, message + "\n")
        self.export_log.see(tk.END)
        self.export_log.config(state=tk.DISABLED)

    def clear_export_log(self):
        """Clear the export log."""
        self.export_log.config(state=tk.NORMAL)
        self.export_log.delete(1.0, tk.END)
        self.export_log.config(state=tk.DISABLED)

    def cancel_export(self):
        """Cancel the current export."""
        self.export_stop_requested = True
        self.export_stop_btn.config(state=tk.DISABLED)
        self.log_export("Cancelling export...")

    def start_export(self):
        """Start exporting audio files."""
        if self.is_exporting:
            return

        api_key = self.get_current_api_key()
        text = self.text_area.get(1.0, tk.END).strip()

        provider = self.provider_var.get()
        if not api_key:
            messagebox.showwarning("Missing API Key", f"Please enter your {provider} API key.")
            return
        if not text:
            messagebox.showwarning("Empty Text", "There is no text to export.")
            return

        # Apply filters
        active_filters = self.get_active_filters()
        if active_filters:
            text = apply_filters(text, active_filters)
            if not text:
                messagebox.showwarning("Empty After Filtering", "All text was removed by the active filters.")
                return

        fmt = self.export_format_var.get()
        split_by_headings = self.export_split_var.get()

        if split_by_headings:
            # Export multiple files — ask for a folder
            output_dir = filedialog.askdirectory(title="Select Export Folder")
            if not output_dir:
                return
            output_path = output_dir
        else:
            # Export single file — ask for save path
            extensions = {
                "mp3": ("MP3 Files", "*.mp3"),
                "wav": ("WAV Files", "*.wav"),
                "flac": ("FLAC Files", "*.flac"),
                "aac": ("AAC Files", "*.aac"),
            }
            ext_label, ext_pattern = extensions[fmt]
            output_path = filedialog.asksaveasfilename(
                defaultextension=f".{fmt}",
                filetypes=[(ext_label, ext_pattern), ("All Files", "*.*")],
                title="Save Audio File"
            )
            if not output_path:
                return

        # Parse speed
        speed_str = self.speed_var.get().replace("x", "")
        try:
            speed = float(speed_str)
        except ValueError:
            speed = 1.0

        # Parse concurrency
        try:
            max_workers = int(self.concurrency_var.get())
        except ValueError:
            max_workers = 3

        model = self.model_var.get()
        voice = self.voice_var.get()

        # Prepare sections
        if split_by_headings:
            sections = split_text_by_headings(text)
        else:
            sections = [("output", text)]

        # Update UI
        self.clear_export_log()
        self.is_exporting = True
        self.export_stop_requested = False
        self.export_btn.config(state=tk.DISABLED)
        self.export_stop_btn.config(state=tk.NORMAL)

        total_sections = len(sections)
        self.export_progress_var.set(f"Exporting {total_sections} section(s) as {fmt.upper()}...")
        self.export_progress_bar['value'] = 0

        if active_filters:
            self.log_export(f"Applied {len(active_filters)} filter(s) before export.")

        # Run export in background
        threading.Thread(
            target=self.run_export,
            args=(api_key, sections, voice, model, speed, fmt, output_path,
                  split_by_headings, max_workers, provider),
            daemon=True
        ).start()

    def run_export(self, api_key, sections, voice, model, speed, fmt, output_path,
                   split_by_headings, max_workers, provider):
        """Background thread: generate and save audio for each section."""
        total_sections = len(sections)

        # Count total batches across all sections for progress
        all_batches = []
        for heading, body in sections:
            batches = split_text_into_batches(body)
            all_batches.append((heading, batches))

        total_batches = sum(len(b) for _, b in all_batches)
        self.root.after(0, lambda: self.export_progress_bar.configure(maximum=total_batches))

        completed_batches = 0

        for sec_idx, (heading, batches) in enumerate(all_batches):
            if self.export_stop_requested:
                self.root.after(0, lambda: self.log_export("Export cancelled."))
                self.root.after(0, self.reset_export_ui)
                return

            sec_num = sec_idx + 1
            num_batches = len(batches)

            if split_by_headings:
                safe_name = sanitize_filename(heading)
                file_name = f"{sec_num:02d}_{safe_name}.{fmt}"
                file_path = os.path.join(output_path, file_name)
            else:
                file_path = output_path

            self.root.after(0, lambda n=sec_num, h=heading: self.log_export(
                f"Section {n}/{total_sections}: \"{h}\" ({num_batches} batch(es))"
            ))

            if num_batches == 1:
                # Single batch — generate directly to output
                if self.export_stop_requested:
                    self.root.after(0, self.reset_export_ui)
                    return

                try:
                    self.generate_tts_audio(batches[0], voice, model, speed, fmt, file_path,
                                            provider=provider, api_key=api_key)
                    completed_batches += 1
                    self.root.after(0, lambda n=completed_batches: self.export_progress_bar.configure(value=n))
                except AuthenticationError:
                    self.root.after(0, lambda p=self.provider_var.get(): messagebox.showerror("Error", f"Invalid {p} API Key."))
                    self.root.after(0, self.reset_export_ui)
                    return
                except APIConnectionError:
                    self.root.after(0, lambda: messagebox.showerror("Error", "Network error."))
                    self.root.after(0, self.reset_export_ui)
                    return
                except Exception as e:
                    err_msg = str(e)
                    self.root.after(0, lambda msg=err_msg: messagebox.showerror("Error", f"Export failed:\n{msg}"))
                    self.root.after(0, self.reset_export_ui)
                    return
            else:
                # Multiple batches — generate concurrently, then concatenate
                temp_files = []
                error_occurred = False

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {}
                    for i, batch_text in enumerate(batches):
                        if self.export_stop_requested:
                            break
                        temp_file = os.path.join(tempfile.gettempdir(), f"export_{os.getpid()}_{sec_idx}_{i}.{fmt}")
                        futures[i] = executor.submit(
                            self._generate_export_batch,
                            batch_text, voice, model, speed, fmt, temp_file,
                            provider, api_key
                        )

                    for i in range(len(batches)):
                        if self.export_stop_requested:
                            for f in futures.values():
                                f.cancel()
                            self.root.after(0, lambda: self.log_export("Export cancelled."))
                            self.root.after(0, self.reset_export_ui)
                            return

                        if i not in futures:
                            break

                        try:
                            temp_file = futures[i].result()
                            temp_files.append(temp_file)
                            completed_batches += 1
                            self.root.after(0, lambda n=completed_batches: self.export_progress_bar.configure(value=n))
                        except AuthenticationError:
                            self.root.after(0, lambda p=self.provider_var.get(): messagebox.showerror("Error", f"Invalid {p} API Key."))
                            error_occurred = True
                            break
                        except APIConnectionError:
                            self.root.after(0, lambda: messagebox.showerror("Error", "Network error."))
                            error_occurred = True
                            break
                        except Exception as e:
                            err_msg = str(e)
                            self.root.after(0, lambda msg=err_msg: messagebox.showerror("Error", f"Export error:\n{msg}"))
                            error_occurred = True
                            break

                if error_occurred:
                    # Clean up temp files
                    for f in temp_files:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    self.root.after(0, self.reset_export_ui)
                    return

                # Concatenate batch files into the final output
                try:
                    with open(file_path, 'wb') as outfile:
                        for tf in temp_files:
                            with open(tf, 'rb') as infile:
                                shutil.copyfileobj(infile, outfile)
                except Exception as e:
                    err_msg = str(e)
                    self.root.after(0, lambda msg=err_msg: messagebox.showerror("Error", f"Failed to save file:\n{msg}"))
                    self.root.after(0, self.reset_export_ui)
                    return
                finally:
                    for f in temp_files:
                        try:
                            os.remove(f)
                        except OSError:
                            pass

            self.root.after(0, lambda n=sec_num, p=file_path: self.log_export(
                f"  Saved: {os.path.basename(p)}"
            ))
            self.root.after(0, lambda n=sec_num: self.export_progress_var.set(
                f"Exported {n}/{total_sections} section(s)..."
            ))

        # Done
        if split_by_headings:
            done_msg = f"Exported {total_sections} file(s) to {output_path}"
        else:
            done_msg = f"Exported to {os.path.basename(output_path)}"

        self.root.after(0, lambda: self.export_progress_var.set("Export complete."))
        self.root.after(0, lambda msg=done_msg: self.log_export(msg))
        self.root.after(0, lambda msg=done_msg: self.status_var.set(msg))
        self.root.after(0, self.reset_export_ui)

    def _generate_export_batch(self, batch_text, voice, model, speed, fmt, temp_file,
                               provider, api_key):
        """Generate a single batch for export. Returns the temp file path."""
        self.generate_tts_audio(batch_text, voice, model, speed, fmt, temp_file,
                                provider=provider, api_key=api_key)
        return temp_file

    def reset_export_ui(self):
        """Re-enable export buttons after export completes or fails."""
        self.export_btn.config(state=tk.NORMAL)
        self.export_stop_btn.config(state=tk.DISABLED)
        self.is_exporting = False

    def log_batch(self, message):
        """Append a message to the batch log box."""
        self.batch_log.config(state=tk.NORMAL)
        self.batch_log.insert(tk.END, message + "\n")
        self.batch_log.see(tk.END)
        self.batch_log.config(state=tk.DISABLED)

    def clear_batch_log(self):
        """Clear the batch log box."""
        self.batch_log.config(state=tk.NORMAL)
        self.batch_log.delete(1.0, tk.END)
        self.batch_log.config(state=tk.DISABLED)

    def load_pdf(self):
        file_path = filedialog.askopenfilename(filetypes=[("PDF Files", "*.pdf")])
        if not file_path:
            return

        self.status_var.set("Extracting text from PDF...")
        self.root.update()

        try:
            text = ""
            with open(file_path, "rb") as file:
                reader = PyPDF2.PdfReader(file)
                for page in reader.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"

            self.text_area.delete(1.0, tk.END)
            self.text_area.insert(tk.END, text)
            self.status_var.set(f"Loaded {os.path.basename(file_path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read PDF:\n{str(e)}")
            self.status_var.set("Error loading file.")

    def load_docx(self):
        file_path = filedialog.askopenfilename(filetypes=[("Word Documents", "*.docx")])
        if not file_path:
            return

        self.status_var.set("Extracting text from DOCX...")
        self.root.update()

        try:
            doc = docx.Document(file_path)
            text = "\n".join([para.text for para in doc.paragraphs])

            self.text_area.delete(1.0, tk.END)
            self.text_area.insert(tk.END, text)
            self.status_var.set(f"Loaded {os.path.basename(file_path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read DOCX:\n{str(e)}")
            self.status_var.set("Error loading file.")

    def clear_text(self):
        self.text_area.delete(1.0, tk.END)
        self.status_var.set("Ready.")

    def stop_audio(self):
        self.stop_requested = True
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Stopped.")
        self.log_batch("Stopped by user.")

    def start_reading(self):
        # Guard against starting a second batch while one is running
        if self.is_processing:
            return

        api_key = self.get_current_api_key()
        text = self.text_area.get(1.0, tk.END).strip()

        provider = self.provider_var.get()
        if not api_key:
            messagebox.showwarning("Missing API Key", f"Please enter your {provider} API key.")
            return
        if not text:
            messagebox.showwarning("Empty Text", "There is no text to read.")
            return

        # Apply active filters before sending to TTS
        active_filters = self.get_active_filters()
        if active_filters:
            text = apply_filters(text, active_filters)
            if not text:
                messagebox.showwarning("Empty After Filtering", "All text was removed by the active filters.")
                return

        # Parse speed value
        speed_str = self.speed_var.get().replace("x", "")
        try:
            speed = float(speed_str)
        except ValueError:
            speed = 1.0

        # Split text into batches
        batches = split_text_into_batches(text)
        total = len(batches)

        if total == 0:
            messagebox.showwarning("Empty Text", "There is no text to read.")
            return

        # Switch to Reader tab to show progress
        self.notebook.select(0)

        # Update UI
        self.clear_batch_log()
        self.play_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.stop_requested = False
        self.is_processing = True
        self.progress_bar['value'] = 0
        self.progress_bar['maximum'] = total

        if active_filters:
            filter_count = len(active_filters)
            self.log_batch(f"Applied {filter_count} filter(s) before processing.")

        if total == 1:
            self.batch_progress_var.set("1 batch to process (text fits in a single request).")
        else:
            self.batch_progress_var.set(f"{total} batches to process.")
        self.log_batch(f"Text split into {total} batch(es) ({len(text)} characters total).")

        model = self.model_var.get()
        voice = self.voice_var.get()

        # Parse concurrency
        try:
            max_workers = int(self.concurrency_var.get())
        except ValueError:
            max_workers = 3

        # Queue for producer (generator) -> consumer (player) communication
        # Items are (batch_num, temp_file) tuples, or None as a sentinel for "done"
        self.audio_queue = queue.Queue()
        self.generator_error = None

        # Launch producer coordinator and consumer threads
        threading.Thread(
            target=self.generate_batches_concurrent,
            args=(api_key, batches, voice, model, speed, total, max_workers, provider),
            daemon=True
        ).start()
        threading.Thread(
            target=self.play_batches,
            args=(total,),
            daemon=True
        ).start()

    def generate_single_batch(self, batch_index, batch_text, voice, model, speed, total,
                              provider, api_key):
        """Worker: generate audio for a single batch. Returns (batch_num, temp_file)."""
        batch_num = batch_index + 1
        self.root.after(0, lambda n=batch_num, chars=len(batch_text): self.log_batch(
            f"Batch {n}/{total}: Generating audio ({chars} chars)..."
        ))

        temp_dir = tempfile.gettempdir()
        temp_file = os.path.join(temp_dir, f"tts_batch_{os.getpid()}_{batch_index}.mp3")

        self.generate_tts_audio(batch_text, voice, model, speed, "mp3", temp_file,
                                provider=provider, api_key=api_key)

        self.root.after(0, lambda n=batch_num: self.log_batch(
            f"Batch {n}/{total}: Audio generated, ready to play."
        ))

        return batch_num, temp_file

    def generate_batches_concurrent(self, api_key, batches, voice, model, speed, total,
                                    max_workers, provider):
        """Coordinator: submit all batches to a thread pool, then feed results to
        the playback queue in order."""
        generated_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all batches to the pool
            futures = {}
            for i, batch_text in enumerate(batches):
                if self.stop_requested:
                    break
                future = executor.submit(
                    self.generate_single_batch,
                    i, batch_text, voice, model, speed, total, provider, api_key
                )
                futures[i] = future

            # Collect results in order so playback stays sequential
            for i in range(len(batches)):
                if self.stop_requested:
                    # Cancel any pending futures
                    for f in futures.values():
                        f.cancel()
                    self.audio_queue.put(None)
                    return

                if i not in futures:
                    break

                try:
                    batch_num, temp_file = futures[i].result()
                    self.batch_temp_files.append(temp_file)
                    generated_count += 1
                    self.root.after(0, lambda n=generated_count: self.progress_bar.configure(value=n))
                    self.audio_queue.put((batch_num, temp_file))
                except AuthenticationError:
                    self.generator_error = f"Invalid {self.provider_var.get()} API Key."
                    for f in futures.values():
                        f.cancel()
                    self.audio_queue.put(None)
                    return
                except APIConnectionError:
                    self.generator_error = "Network error. Please check your connection."
                    for f in futures.values():
                        f.cancel()
                    self.audio_queue.put(None)
                    return
                except Exception as e:
                    self.generator_error = f"Error on batch {i + 1}:\n{str(e)}"
                    for f in futures.values():
                        f.cancel()
                    self.audio_queue.put(None)
                    return

        # Signal that all batches have been generated
        self.audio_queue.put(None)

    def play_batches(self, total):
        """Consumer: play audio files from the queue as they become available."""
        while True:
            if self.stop_requested:
                self.root.after(0, lambda: self.log_batch("Playback stopped."))
                self.root.after(0, self.reset_ui)
                return

            # Wait for next item (blocks until producer puts something)
            try:
                item = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # None sentinel means producer is done (either finished or errored)
            if item is None:
                if self.generator_error:
                    err_msg = self.generator_error
                    self.root.after(0, lambda msg=err_msg: messagebox.showerror("Error", msg))
                    self.root.after(0, self.reset_ui)
                else:
                    # All batches generated and played
                    self.root.after(0, lambda: self.status_var.set("Ready."))
                    self.root.after(0, lambda: self.batch_progress_var.set(f"All {total} batch(es) completed."))
                    self.root.after(0, lambda: self.log_batch("All batches finished."))
                    self.root.after(0, self.reset_ui)
                return

            batch_num, temp_file = item

            if self.stop_requested:
                self.root.after(0, self.reset_ui)
                return

            try:
                pygame.mixer.music.load(temp_file)
                pygame.mixer.music.play()
                self.root.after(0, lambda n=batch_num: self.status_var.set(f"Playing batch {n}/{total}..."))
                self.root.after(0, lambda n=batch_num: self.batch_progress_var.set(
                    f"Playing batch {n}/{total}..."
                ))

                # Wait for playback to complete
                while pygame.mixer.music.get_busy():
                    if self.stop_requested:
                        pygame.mixer.music.stop()
                        self.root.after(0, self.reset_ui)
                        return
                    pygame.time.Clock().tick(10)

            except Exception as e:
                err_msg = f"Failed to play batch {batch_num}:\n{str(e)}"
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("Playback Error", msg))
                self.root.after(0, self.reset_ui)
                return

            self.root.after(0, lambda n=batch_num: self.log_batch(f"Batch {n}/{total}: Playback complete."))

    def cleanup_temp_files(self):
        """Unload pygame music and remove temporary audio files."""
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
        for f in self.batch_temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self.batch_temp_files = []

    def reset_ui(self):
        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.is_processing = False
        self.cleanup_temp_files()

if __name__ == "__main__":
    root = tk.Tk()

    # Optional: Apply a slightly better default style if available
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")

    app = TTSApp(root)
    root.mainloop()
