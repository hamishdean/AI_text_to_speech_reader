# -*- coding: utf-8 -*-


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import tempfile
import docx
import PyPDF2
import pygame
from openai import OpenAI, AuthenticationError, APIConnectionError

class TTSApp:
    def __init__(self, root):
        self.root = root
        self.root.title("OpenAI Text-to-Speech Reader")
        self.root.geometry("700x600")
        self.root.minsize(500, 400)

        # Initialize pygame mixer for audio playback
        pygame.mixer.init()

        # Variables
        self.api_key_var = tk.StringVar()
        self.voice_var = tk.StringVar(value="alloy")
        self.voices = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
        self.current_temp_file = None

        self.create_widgets()

    def create_widgets(self):
        # Main Frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Top Section: Settings ---
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding="10")
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(settings_frame, text="OpenAI API Key:").grid(row=0, column=0, sticky=tk.W, pady=5)
        api_entry = ttk.Entry(settings_frame, textvariable=self.api_key_var, show="*", width=50)
        api_entry.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)

        ttk.Label(settings_frame, text="Voice:").grid(row=1, column=0, sticky=tk.W, pady=5)
        voice_dropdown = ttk.Combobox(settings_frame, textvariable=self.voice_var, values=self.voices, state="readonly", width=15)
        voice_dropdown.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)

        settings_frame.columnconfigure(1, weight=1)

        # --- Middle Section: Text Area ---
        text_frame = ttk.LabelFrame(main_frame, text="Text Content", padding="10")
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Toolbar for text area
        toolbar = ttk.Frame(text_frame)
        toolbar.pack(fill=tk.X, pady=(0, 5))

        ttk.Button(toolbar, text="Load PDF", command=self.load_pdf).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="Load DOCX", command=self.load_docx).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="Clear Text", command=self.clear_text).pack(side=tk.LEFT)

        # Text Widget with Scrollbar
        self.text_area = tk.Text(text_frame, wrap=tk.WORD, font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(text_frame, command=self.text_area.yview)
        self.text_area.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- Bottom Section: Controls ---
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(5, 0))

        self.play_btn = ttk.Button(control_frame, text="▶ Read Aloud", command=self.start_reading, style="Accent.TButton")
        self.play_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(control_frame, text="⏹ Stop Audio", command=self.stop_audio, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready.")
        status_label = ttk.Label(control_frame, textvariable=self.status_var, foreground="gray")
        status_label.pack(side=tk.RIGHT)

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
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Audio stopped.")

    def start_reading(self):
        api_key = self.api_key_var.get().strip()
        text = self.text_area.get(1.0, tk.END).strip()

        if not api_key:
            messagebox.showwarning("Missing API Key", "Please enter your OpenAI API key.")
            return
        if not text:
            messagebox.showwarning("Empty Text", "There is no text to read.")
            return

        # The OpenAI API has a max limit of 4096 characters per request for tts-1
        if len(text) > 4000:
            messagebox.showinfo("Text Too Long", "The text is longer than the API limit (4096 characters). Only the first 4000 characters will be read.")
            text = text[:4000]

        # Disable UI elements while generating
        self.play_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Generating audio via OpenAI...")

        # Run in a separate thread so GUI doesn't freeze
        threading.Thread(target=self.generate_and_play_audio, args=(api_key, text, self.voice_var.get()), daemon=True).start()

    def generate_and_play_audio(self, api_key, text, voice):
        try:
            client = OpenAI(api_key=api_key)

            # Generate temporary file path
            temp_dir = tempfile.gettempdir()
            self.current_temp_file = os.path.join(temp_dir, f"tts_output_{os.getpid()}.mp3")

            # Call OpenAI API
            response = client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text
            )

            # Save the audio stream to the temp file
            response.stream_to_file(self.current_temp_file)

            # Play the audio file on the main thread safely
            self.root.after(0, self.play_generated_audio)

        except AuthenticationError:
            self.root.after(0, lambda: messagebox.showerror("Error", "Invalid OpenAI API Key."))
            self.root.after(0, self.reset_ui)
        except APIConnectionError:
            self.root.after(0, lambda: messagebox.showerror("Error", "Network error. Please check your connection."))
            self.root.after(0, self.reset_ui)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"An error occurred:\n{str(e)}"))
            self.root.after(0, self.reset_ui)

    def play_generated_audio(self):
        try:
            # Load and play the MP3 file using pygame
            pygame.mixer.music.load(self.current_temp_file)
            pygame.mixer.music.play()

            self.status_var.set("Playing audio...")
            self.stop_btn.config(state=tk.NORMAL)

            # Start a thread to monitor when the audio finishes to reset buttons
            threading.Thread(target=self.monitor_audio_playback, daemon=True).start()

        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play audio:\n{str(e)}")
            self.reset_ui()

    def monitor_audio_playback(self):
        # Wait until pygame stops playing
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)

        # Once finished, reset the UI safely on the main thread
        self.root.after(0, self.reset_ui)
        self.root.after(0, lambda: self.status_var.set("Ready."))

    def reset_ui(self):
        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

if __name__ == "__main__":
    root = tk.Tk()

    # Optional: Apply a slightly better default style if available
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")

    app = TTSApp(root)
    root.mainloop()