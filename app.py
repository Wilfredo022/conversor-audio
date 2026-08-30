import os
import re
import sys
import ctypes
import tempfile
import threading
import time
import wave
import subprocess
import queue
from pathlib import Path
from tkinterdnd2 import DND_FILES, TkinterDnD
import customtkinter as ctk
from tkinter import filedialog, messagebox
import imageio_ffmpeg
import numpy as np
import sounddevice as sd

def _enable_windows_dpi() -> None:
    """Evita que Windows estire la ventana como un bitmap (texto y bordes borrosos)."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_windows_dpi()

# ── Identidad ────────────────────────────────────────────────────────────────
APP_NAME = "Conversor Audio"
APP_TAGLINE = "Convierte y recorta audio en un clic"

# ── Configuración visual ──────────────────────────────────────────────────────
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# FFmpeg suele escribir stderr en UTF-8; en Windows text=True usa cp1252 y revienta con metadatos/accentos.
_FFMPEG_PIPE_TEXT = {"encoding": "utf-8", "errors": "replace"}

ACCENT       = "#7C3AED"   # violeta
ACCENT_HOVER = "#6D28D9"
NAVY         = "#1E1B4B"   # CTA principal
NAVY_HOVER   = "#312E81"
SUCCESS      = "#16A34A"
WARNING      = "#D97706"
ERROR        = "#DC2626"
BG_APP       = "#F4F1FB"   # lavanda suave
BG_CARD      = "#FFFFFF"
BG_ITEM      = "#F8F6FC"
BG_MUTED     = "#EEE9F8"
BG_HERO      = "#6D28D9"
TEXT         = "#1E1B4B"
TEXT_MUTED   = "#6B7280"
TEXT_SOFT    = "#9CA3AF"
BORDER       = "#E5E0F0"
TIMELINE_BG  = "#EDE9FE"
TIMELINE_BAR = "#DDD6FE"

_font_cache: dict[tuple, ctk.CTkFont] = {}


def ui_font(size: int, weight: str = "normal", family: str = "Segoe UI") -> ctk.CTkFont:
    """Fuente que escala con el DPI de CustomTkinter (nítida en pantallas HiDPI)."""
    key = (family, size, weight)
    font = _font_cache.get(key)
    if font is None:
        font = ctk.CTkFont(family=family, size=size, weight=weight)
        _font_cache[key] = font
    return font


def make_icon(master, kind: str, color: str, bg: str, size: int) -> ctk.CTkCanvas:
    """Icono dibujado en canvas (se ve nítido; los emoji se pixelan al escalar)."""
    canvas = ctk.CTkCanvas(
        master,
        width=size,
        height=size,
        bg=bg,
        highlightthickness=0,
        bd=0,
    )

    def redraw(_event=None) -> None:
        canvas.delete("all")
        w = max(int(canvas.winfo_width()), size)
        h = max(int(canvas.winfo_height()), size)
        cx, cy = w / 2, h / 2
        if kind == "wave":
            bars = ((0.30, 0.36), (0.50, 0.62), (0.70, 0.44))
            stroke = max(w * 0.13, 3)
            for rel_x, rel_h in bars:
                x = w * rel_x
                half = h * rel_h / 2
                canvas.create_line(
                    x, cy - half, x, cy + half,
                    fill=color, width=stroke, capstyle="round",
                )
        elif kind == "arrow":
            stroke = max(w * 0.08, 2.5)
            canvas.create_line(
                cx, h * 0.22, cx, h * 0.68,
                fill=color, width=stroke, capstyle="round",
            )
            canvas.create_line(
                w * 0.28, h * 0.50, cx, h * 0.72, w * 0.72, h * 0.50,
                fill=color, width=stroke, capstyle="round", joinstyle="round",
            )

    canvas.bind("<Configure>", redraw)
    canvas.after_idle(redraw)
    return canvas

# Formatos de salida soportados: nombre → (extensión, args de codec FFmpeg)
OUTPUT_FORMATS: dict[str, tuple[str, list[str]]] = {
    "MP3":  (".mp3",  ["-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100"]),
    "M4A":  (".m4a",  ["-acodec", "aac", "-b:a", "192k"]),
    "AAC":  (".aac",  ["-acodec", "aac", "-b:a", "192k"]),
    "WAV":  (".wav",  ["-acodec", "pcm_s16le"]),
    "OGG":  (".ogg",  ["-acodec", "libvorbis", "-q:a", "5"]),
    "FLAC": (".flac", ["-acodec", "flac"]),
}

# Extensiones que FFmpeg puede leer (minúsculas, con punto)
SUPPORTED_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4a",
    ".aac",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
)

# Patrón único para el diálogo «Todos los soportados» (tkinter / Windows)
_SUPPORTED_FILE_PATTERNS = " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)


def _fmt_ffmpeg_seconds(x: float) -> str:
    """Número breve aceptado por FFmpeg (-ss / -t)."""
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_timecode(seconds: float, decimals: int = 2) -> str:
    """HH:MM:SS + centésimas/milésimas para marcar cortes con precisión."""
    if seconds < 0:
        seconds = 0.0
    frac = seconds % 1
    s_int = int(seconds // 1)
    h = s_int // 3600
    m = (s_int % 3600) // 60
    sec = s_int % 60
    dec = max(0, min(decimals, 6))
    frac_str = f"{frac:.{dec}f}".split(".")[1] if dec > 0 else ""
    base = f"{h:d}:{m:02d}:{sec:02d}"
    return f"{base}.{frac_str}" if frac_str else base


def _load_wav_numpy(path: str) -> tuple[int, np.ndarray]:
    """PCM WAV → float32 (frames × canales), rango ~[-1, 1]."""
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if ch < 1 or sw not in (2, 4):
        raise ValueError("Formato WAV no compatible (usa PCM 16 o 32 bit).")
    if sw == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        pcm = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    pcm = pcm.reshape(-1, ch) if ch > 1 else pcm.reshape(-1, 1)
    return rate, pcm


# ── Fila de mensajes de progreso ──────────────────────────────────────────────
progress_queue: queue.Queue = queue.Queue()


class FileRow(ctk.CTkFrame):
    """Una fila por archivo: nombre, barra de progreso, estado."""

    def __init__(self, master, filepath: str, **kwargs):
        super().__init__(master, fg_color=BG_ITEM, corner_radius=18, **kwargs)
        self.filepath = filepath
        self.filename = Path(filepath).name
        self._build()

    def _build(self):
        self.columnconfigure(1, weight=1)

        icon_wrap = ctk.CTkFrame(self, fg_color="#EDE9FE", width=40, height=40, corner_radius=12)
        icon_wrap.grid(row=0, column=0, rowspan=2, padx=(12, 8), pady=10)
        icon_wrap.grid_propagate(False)
        make_icon(icon_wrap, "wave", ACCENT, "#EDE9FE", 28).place(relx=0.5, rely=0.5, anchor="center")

        name_lbl = ctk.CTkLabel(
            self,
            text=self.filename,
            font=ui_font(13, "bold"),
            text_color=TEXT,
            anchor="w",
            wraplength=420,
        )
        name_lbl.grid(row=0, column=1, sticky="w", padx=4, pady=(10, 2))

        self.progress = ctk.CTkProgressBar(
            self, height=7, corner_radius=4, progress_color=ACCENT, fg_color="#E5E0F0"
        )
        self.progress.set(0)
        self.progress.grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 12))

        self.status_var = ctk.StringVar(value="En espera…")
        self.status_lbl = ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            font=ui_font(11),
            text_color=TEXT_SOFT,
            anchor="e",
            width=120,
        )
        self.status_lbl.grid(row=0, column=2, rowspan=2, padx=(4, 8), pady=8, sticky="e")

        del_btn = ctk.CTkButton(
            self,
            text="✕",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color="#EDE9FE",
            text_color=TEXT_MUTED,
            font=ui_font(13),
            command=self._remove,
        )
        del_btn.grid(row=0, column=3, rowspan=2, padx=(0, 10))

    def set_status(self, text: str, color: str = TEXT_SOFT):
        self.status_var.set(text)
        self.status_lbl.configure(text_color=color)

    def set_progress(self, value: float):
        self.progress.set(value)

    def _remove(self):
        app = self.winfo_toplevel()
        if hasattr(app, "remove_file_row"):
            app.remove_file_row(self)


class App(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self):
        super().__init__(fg_color=BG_APP)
        self.TkdndVersion = TkinterDnD._require(self)
        self.title(APP_NAME)
        self.geometry("760x920")
        self.minsize(640, 560)
        self.output_format = "MP3"

        self.file_rows: list[FileRow] = []
        self.converting = False
        self.output_dir: str = str(Path.home() / "Downloads")

        self._preview_loaded_ok = False
        self._preview_original_path: str | None = None
        self._preview_temp_wav: str | None = None
        self._wav_data: np.ndarray | None = None
        self._wav_rate = 44100
        self._preview_duration_sec: float | None = None
        self._preview_playing = False
        self._preview_paused = False
        self._playback_base_sec = 0.0
        self._playback_started_mono = 0.0
        self._slider_after_id: str | None = None
        self._preview_tick_after_id: str | None = None
        self._preview_slider_programmatic = False
        self._slider_pending = 0.0

        self._build_ui()
        self._poll_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Construcción de la UI ─────────────────────────────────────────────────

    def _build_ui(self):
        bottom = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0)
        bottom.pack(side="bottom", fill="x")

        inner_bottom = ctk.CTkFrame(bottom, fg_color="transparent")
        inner_bottom.pack(fill="x", padx=24, pady=16)

        self.global_progress = ctk.CTkProgressBar(
            inner_bottom,
            height=8,
            corner_radius=4,
            progress_color=ACCENT,
            fg_color=BG_MUTED,
        )
        self.global_progress.set(0)
        self.global_progress.pack(fill="x", pady=(0, 6))

        self.global_status = ctk.CTkLabel(
            inner_bottom,
            text="Listo para convertir",
            font=ui_font(11),
            text_color=TEXT_MUTED,
        )
        self.global_status.pack(anchor="w", pady=(0, 10))

        fmt_row = ctk.CTkFrame(inner_bottom, fg_color="transparent")
        fmt_row.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            fmt_row,
            text="Formato de salida",
            font=ui_font(12, "bold"),
            text_color=TEXT,
        ).pack(side="left", padx=(0, 12))

        self._format_btns: dict[str, ctk.CTkButton] = {}
        pills = ctk.CTkFrame(fmt_row, fg_color="transparent")
        pills.pack(side="left")
        for name in OUTPUT_FORMATS:
            btn = ctk.CTkButton(
                pills,
                text=name,
                width=58,
                height=32,
                corner_radius=16,
                font=ui_font(12, "bold"),
                command=lambda n=name: self._set_output_format(n),
            )
            btn.pack(side="left", padx=3)
            self._format_btns[name] = btn
        self._apply_format_pill_styles()

        self.convert_btn = ctk.CTkButton(
            inner_bottom,
            text="Convertir a MP3",
            font=ui_font(15, "bold"),
            height=50,
            corner_radius=18,
            fg_color=NAVY,
            hover_color=NAVY_HOVER,
            text_color="#FFFFFF",
            command=self._start_conversion,
        )
        self.convert_btn.pack(fill="x")

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 12))

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left")

        logo = ctk.CTkFrame(brand, fg_color=ACCENT, width=42, height=42, corner_radius=14)
        logo.pack(side="left")
        logo.pack_propagate(False)
        make_icon(logo, "wave", "#FFFFFF", ACCENT, 28).place(relx=0.5, rely=0.5, anchor="center")

        titles = ctk.CTkFrame(brand, fg_color="transparent")
        titles.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(
            titles,
            text=APP_NAME,
            font=ui_font(22, "bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text=APP_TAGLINE,
            font=ui_font(12),
            text_color=TEXT_MUTED,
        ).pack(anchor="w")

        self.drop_frame = ctk.CTkFrame(self, fg_color=BG_HERO, corner_radius=24)
        self.drop_frame.pack(fill="x", padx=24, pady=(0, 14))
        self.drop_frame.drop_target_register(DND_FILES)
        self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        drop_inner = ctk.CTkFrame(self.drop_frame, fg_color="transparent")
        drop_inner.pack(pady=28)

        make_icon(drop_inner, "arrow", "#FFFFFF", BG_HERO, 40).pack()
        ctk.CTkLabel(
            drop_inner,
            text="Arrastra tus archivos aquí",
            font=ui_font(16, "bold"),
            text_color="#FFFFFF",
        ).pack(pady=(4, 0))
        ctk.CTkLabel(
            drop_inner,
            text="MP4, M4A, WAV, FLAC, OGG y más",
            font=ui_font(12),
            text_color="#DDD6FE",
        ).pack(pady=(2, 14))

        ctk.CTkButton(
            drop_inner,
            text="Seleccionar archivos",
            font=ui_font(13, "bold"),
            height=40,
            width=200,
            corner_radius=20,
            fg_color="#FFFFFF",
            hover_color="#F3E8FF",
            text_color=ACCENT,
            command=self._browse_files,
        ).pack()

        self.main_scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_APP, corner_radius=0
        )
        self.main_scroll.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        list_header = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        list_header.pack(fill="x", pady=(0, 8))
        self.files_label = ctk.CTkLabel(
            list_header,
            text="Archivos agregados (0)",
            font=ui_font(13, "bold"),
            text_color=TEXT,
        )
        self.files_label.pack(side="left")
        ctk.CTkButton(
            list_header,
            text="Limpiar todo",
            font=ui_font(11),
            height=28,
            width=100,
            corner_radius=14,
            fg_color="transparent",
            border_width=1,
            border_color=BORDER,
            hover_color=BG_MUTED,
            text_color=TEXT_MUTED,
            command=self._clear_all,
        ).pack(side="right")

        self.files_container = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        self.files_container.pack(fill="x", pady=(0, 8))

        self.empty_label = ctk.CTkLabel(
            self.files_container,
            text="Todavía no hay archivos. Arrastra o selecciona audio / vídeo.",
            font=ui_font(12),
            text_color=TEXT_SOFT,
        )
        self.empty_label.pack(pady=28)

        out_card = ctk.CTkFrame(self.main_scroll, fg_color=BG_CARD, corner_radius=20)
        out_card.pack(fill="x", pady=(8, 12))

        out_frame = ctk.CTkFrame(out_card, fg_color="transparent")
        out_frame.pack(fill="x", padx=16, pady=14)

        ctk.CTkLabel(
            out_frame, text="Guardar en", font=ui_font(12, "bold"), text_color=TEXT
        ).pack(side="left")
        self.out_var = ctk.StringVar(value=self.output_dir)
        ctk.CTkEntry(
            out_frame,
            textvariable=self.out_var,
            font=ui_font(11),
            height=34,
            corner_radius=12,
            border_color=BORDER,
            state="readonly",
        ).pack(side="left", fill="x", expand=True, padx=10)
        ctk.CTkButton(
            out_frame,
            text="Cambiar",
            width=88,
            height=34,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._choose_output,
        ).pack(side="left")

        trim_card = ctk.CTkFrame(self.main_scroll, fg_color=BG_CARD, corner_radius=20)
        trim_card.pack(fill="x", pady=(0, 16))

        self.trim_enabled_var = ctk.BooleanVar(value=False)
        self.trim_enabled_var.trace_add("write", lambda *_: self._refresh_convert_button_label())
        ctk.CTkCheckBox(
            trim_card,
            text="Recortar audio (exportar solo un fragmento)",
            variable=self.trim_enabled_var,
            font=ui_font(13, "bold"),
            text_color=TEXT,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
        ).pack(anchor="w", padx=16, pady=(14, 8))

        guide = ctk.CTkFrame(trim_card, fg_color=BG_ITEM, corner_radius=16)
        guide.pack(fill="x", padx=12, pady=(0, 10))

        guide_head = ctk.CTkFrame(guide, fg_color="transparent")
        guide_head.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            guide_head,
            text="Guía: escucha y marca el corte",
            font=ui_font(12, "bold"),
            text_color=TEXT,
        ).pack(side="left")

        self.preview_combo = ctk.CTkComboBox(
            guide_head,
            width=300,
            values=["(sin archivos)"],
            font=ui_font(11),
            dropdown_fg_color=BG_CARD,
            state="readonly",
        )
        self.preview_combo.pack(side="right")
        self.preview_combo.set("(sin archivos)")

        prep_row = ctk.CTkFrame(guide, fg_color="transparent")
        prep_row.pack(fill="x", padx=12, pady=(0, 6))
        self.preview_prepare_btn = ctk.CTkButton(
            prep_row,
            text="Preparar vista previa",
            width=168,
            height=32,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._preview_prepare_click,
        )
        self.preview_prepare_btn.pack(side="left")
        self.preview_status_lbl = ctk.CTkLabel(
            prep_row,
            text="Sin cargar · elige archivo y prepara",
            font=ui_font(10),
            text_color=TEXT_MUTED,
        )
        self.preview_status_lbl.pack(side="left", padx=(12, 0))

        time_row = ctk.CTkFrame(guide, fg_color="transparent")
        time_row.pack(fill="x", padx=12, pady=(0, 4))
        self.preview_time_var = ctk.StringVar(value="0:00:00.00 / —")
        ctk.CTkLabel(
            time_row,
            textvariable=self.preview_time_var,
            font=ui_font(15, "bold", "Consolas"),
            text_color=ACCENT,
        ).pack(side="left")

        self.preview_slider = ctk.CTkSlider(
            guide,
            from_=0,
            to=1,
            number_of_steps=2000,
            state="disabled",
            progress_color=ACCENT,
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            fg_color=BG_MUTED,
            command=self._preview_slider_moved,
        )
        self.preview_slider.set(0)
        self.preview_slider.pack(fill="x", padx=12, pady=(2, 4))

        self.preview_canvas = ctk.CTkCanvas(
            guide,
            height=42,
            bg=TIMELINE_BG,
            highlightthickness=0,
            bd=0,
        )
        self.preview_canvas.pack(fill="x", padx=12, pady=(0, 4))
        self.preview_canvas.bind("<Configure>", lambda _e: self._preview_draw_timeline())

        btn_row = ctk.CTkFrame(guide, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 12))
        self.preview_play_btn = ctk.CTkButton(
            btn_row,
            text="▶ Reproducir",
            width=118,
            height=34,
            corner_radius=14,
            fg_color=NAVY,
            hover_color=NAVY_HOVER,
            state="disabled",
            command=self._preview_toggle_play,
        )
        self.preview_play_btn.pack(side="left", padx=(0, 8))
        self.preview_mark_desde_btn = ctk.CTkButton(
            btn_row,
            text="Marcar «Desde»",
            width=140,
            height=34,
            corner_radius=14,
            fg_color=SUCCESS,
            hover_color="#15803D",
            state="disabled",
            command=self._preview_mark_desde,
        )
        self.preview_mark_desde_btn.pack(side="left", padx=(0, 8))
        self.preview_mark_hasta_btn = ctk.CTkButton(
            btn_row,
            text="Marcar «Hasta»",
            width=140,
            height=34,
            corner_radius=14,
            fg_color="#EA580C",
            hover_color="#C2410C",
            state="disabled",
            command=self._preview_mark_hasta,
        )
        self.preview_mark_hasta_btn.pack(side="left")

        trim_row = ctk.CTkFrame(trim_card, fg_color="transparent")
        trim_row.pack(fill="x", padx=16, pady=(0, 6))
        trim_row.columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(trim_row, text="Desde", font=ui_font(11, "bold"), text_color=TEXT, width=48).grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.trim_start_entry = ctk.CTkEntry(
            trim_row,
            placeholder_text="vacío = inicio",
            font=ui_font(12),
            height=34,
            corner_radius=12,
            border_color=BORDER,
        )
        self.trim_start_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        ctk.CTkLabel(trim_row, text="Hasta", font=ui_font(11, "bold"), text_color=TEXT, width=48).grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        self.trim_end_entry = ctk.CTkEntry(
            trim_row,
            placeholder_text="vacío = hasta el final",
            font=ui_font(12),
            height=34,
            corner_radius=12,
            border_color=BORDER,
        )
        self.trim_end_entry.grid(row=0, column=3, sticky="ew")
        self.trim_start_entry.bind("<KeyRelease>", lambda _e: self._preview_draw_timeline())
        self.trim_end_entry.bind("<KeyRelease>", lambda _e: self._preview_draw_timeline())

        ctk.CTkLabel(
            trim_card,
            text="Con el recorte activo, el botón inferior exporta solo el tramo Desde→Hasta "
            "en el formato elegido, dentro de la carpeta «Guardar en». "
            "Sin recorte, exporta el archivo completo.",
            font=ui_font(10),
            text_color=TEXT_MUTED,
            wraplength=640,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 16))

        self._refresh_convert_button_label()

    def _set_output_format(self, name: str) -> None:
        self.output_format = name
        self._apply_format_pill_styles()
        self._refresh_convert_button_label()

    def _apply_format_pill_styles(self) -> None:
        for name, btn in self._format_btns.items():
            if name == self.output_format:
                btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF")
            else:
                btn.configure(fg_color=BG_MUTED, hover_color="#DDD6FE", text_color=TEXT)

    def _refresh_convert_button_label(self) -> None:
        if getattr(self, "converting", False):
            return
        fmt_name = getattr(self, "output_format", "MP3")
        if self.trim_enabled_var.get():
            self.convert_btn.configure(text=f"Exportar fragmento a {fmt_name}")
        else:
            self.convert_btn.configure(text=f"Convertir a {fmt_name}")

    # ── Lógica de archivos ────────────────────────────────────────────────────

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title=f"Seleccionar archivos — {APP_NAME}",
            filetypes=[
                ("Audio y vídeo compatibles", _SUPPORTED_FILE_PATTERNS),
                ("MP4", "*.mp4"),
                ("M4A / AAC", "*.m4a *.aac"),
                ("WAV / FLAC", "*.wav *.flac"),
                ("OGG / Opus", "*.ogg *.opus"),
                ("Otros vídeos", "*.m4v *.mov *.mkv *.avi *.webm"),
                ("Todos los archivos", "*.*"),
            ],
        )
        for p in paths:
            self._add_file(p)

    def _on_drop(self, event):
        raw = event.data
        # tkinterdnd2 puede entregar rutas separadas por espacios o con llaves
        files = self.tk.splitlist(raw)
        for f in files:
            f = f.strip()
            ext = Path(f).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                self._add_file(f)
            else:
                messagebox.showwarning(
                    "Formato no soportado",
                    f"Extensión no reconocida ({ext or 'sin extensión'}).\n"
                    f"Aceptados: {', '.join(sorted(set(e.lstrip('.') for e in SUPPORTED_EXTENSIONS)))}",
                )

    def _add_file(self, path: str):
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            messagebox.showwarning(
                "Formato no soportado",
                f"No se puede agregar «{Path(path).name}».\n"
                f"Extensiones aceptadas: {', '.join(sorted(set(e.lstrip('.') for e in SUPPORTED_EXTENSIONS)))}",
            )
            return
        if any(r.filepath == path for r in self.file_rows):
            return  # ya está en la lista
        if self.empty_label.winfo_ismapped():
            self.empty_label.pack_forget()
        row = FileRow(self.files_container, path)
        row.pack(fill="x", pady=4)
        self.file_rows.append(row)
        self._update_count()

    def remove_file_row(self, row: FileRow):
        if row in self.file_rows:
            self.file_rows.remove(row)
        row.destroy()
        self._update_count()
        if not self.file_rows:
            self.empty_label.pack(pady=30)

    def _clear_all(self):
        for row in list(self.file_rows):
            row.destroy()
        self.file_rows.clear()
        self._update_count()
        self.empty_label.pack(pady=30)
        self.global_progress.set(0)
        self.global_status.configure(text="Listo para convertir")
        self._preview_reset(True)

    def _update_count(self):
        n = len(self.file_rows)
        self.files_label.configure(text=f"Archivos agregados ({n})")
        self._refresh_preview_combo()

    def _choose_output(self):
        d = filedialog.askdirectory(title="Carpeta de destino", initialdir=self.output_dir)
        if d:
            self.output_dir = d
            self.out_var.set(d)

    @staticmethod
    def _parse_flexible_time(s: str) -> float | None:
        """Acepta segundos decimales, MM:SS o HH:MM:SS."""
        s = (s or "").strip()
        if not s:
            return None
        if ":" not in s:
            try:
                return float(s)
            except ValueError:
                return None
        parts = s.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
        return None

    def _validate_trim_before_convert(self) -> bool:
        if not self.trim_enabled_var.get():
            return True
        start_raw = self.trim_start_entry.get().strip()
        end_raw = self.trim_end_entry.get().strip()
        start_sec = self._parse_flexible_time(start_raw) if start_raw else 0.0
        if start_raw and start_sec is None:
            messagebox.showerror(
                "Recorte",
                "«Desde» no es válido.\nUsa segundos (90), MM:SS (1:30) o HH:MM:SS.",
            )
            return False
        if start_sec < 0:
            messagebox.showerror("Recorte", "«Desde» no puede ser negativo.")
            return False
        end_sec = self._parse_flexible_time(end_raw) if end_raw else None
        if end_raw and end_sec is None:
            messagebox.showerror(
                "Recorte",
                "«Hasta» no es válido.\nUsa segundos, MM:SS o HH:MM:SS.",
            )
            return False
        if end_sec is not None and end_sec <= start_sec:
            messagebox.showerror("Recorte", "«Hasta» debe ser mayor que «Desde».")
            return False
        return True

    # ── Vista previa / guía de recorte ───────────────────────────────────────

    def _on_close(self):
        try:
            self._preview_reset(True)
        finally:
            self.destroy()

    def _preview_stop_sound(self) -> None:
        try:
            sd.stop()
        except Exception:
            pass

    def _preview_combo_labels(self) -> list[str]:
        return [f"{i + 1}. {Path(r.filepath).name}" for i, r in enumerate(self.file_rows)]

    def _preview_selected_path(self) -> str | None:
        labels = self._preview_combo_labels()
        sel = self.preview_combo.get()
        if not self.file_rows or sel == "(sin archivos)" or sel not in labels:
            return None
        idx = labels.index(sel)
        return self.file_rows[idx].filepath

    def _refresh_preview_combo(self) -> None:
        labels = self._preview_combo_labels()
        self.preview_combo.configure(values=labels or ["(sin archivos)"])
        cur = self.preview_combo.get()
        if not labels:
            self.preview_combo.set("(sin archivos)")
            self._preview_reset(True)
        elif cur not in labels:
            self.preview_combo.set(labels[0])
        if self._preview_loaded_ok and self._preview_original_path:
            if not any(r.filepath == self._preview_original_path for r in self.file_rows):
                self._preview_reset(True)

    def _preview_cleanup_temp(self) -> None:
        if self._preview_temp_wav and os.path.isfile(self._preview_temp_wav):
            try:
                os.remove(self._preview_temp_wav)
            except OSError:
                pass
        self._preview_temp_wav = None

    def _cancel_preview_tick(self) -> None:
        if self._preview_tick_after_id:
            try:
                self.after_cancel(self._preview_tick_after_id)
            except Exception:
                pass
            self._preview_tick_after_id = None

    def _preview_reset(self, full: bool = True) -> None:
        self._cancel_preview_tick()
        if self._slider_after_id:
            try:
                self.after_cancel(self._slider_after_id)
            except Exception:
                pass
            self._slider_after_id = None
        try:
            self._preview_stop_sound()
        except Exception:
            pass
        self._preview_playing = False
        self._preview_paused = False
        self._playback_base_sec = 0.0
        if full:
            self._preview_cleanup_temp()
            self._preview_loaded_ok = False
            self._preview_original_path = None
            self._wav_data = None
            self._wav_rate = 44100
            self._preview_duration_sec = None
            try:
                self.preview_slider.configure(state="disabled")
            except Exception:
                pass
            self.preview_slider.set(0)
            self.preview_play_btn.configure(state="disabled", text="▶ Reproducir")
            self.preview_mark_desde_btn.configure(state="disabled")
            self.preview_mark_hasta_btn.configure(state="disabled")
            self.preview_time_var.set("0:00:00.00 / —")
            self.preview_status_lbl.configure(text="Sin cargar · elige archivo y prepara")

    def _preview_prepare_click(self) -> None:
        if self.converting:
            messagebox.showinfo("Vista previa", "Espera a que termine la conversión.")
            return
        path = self._preview_selected_path()
        if not path:
            messagebox.showinfo("Vista previa", "Agrega archivos y elige uno en la lista.")
            return
        self.preview_prepare_btn.configure(state="disabled")
        self.preview_status_lbl.configure(text="Preparando…")
        self.update_idletasks()
        threading.Thread(target=self._preview_prepare_worker, args=(path,), daemon=True).start()

    def _preview_prepare_worker(self, src: str) -> None:
        tmp_path: str | None = None
        err_msg: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="conversor_pv_")
            os.close(fd)
            r = subprocess.run(
                [
                    FFMPEG_PATH,
                    "-y",
                    "-i",
                    src,
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    tmp_path,
                ],
                capture_output=True,
                text=True,
                **_FFMPEG_PIPE_TEXT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if r.returncode != 0:
                tail = (r.stderr or "")[-900:]
                raise RuntimeError(tail.strip() or "FFmpeg no pudo decodificar el audio.")
        except Exception as e:
            err_msg = str(e)
            if tmp_path and os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            tmp_path = None

        self.after(
            0,
            lambda s=src, tmp=tmp_path, err=err_msg: self._preview_finish_prepare(s, tmp, err),
        )

    def _preview_finish_prepare(self, src: str, tmp: str | None, err: str | None) -> None:
        if err:
            self.preview_prepare_btn.configure(state="normal")
            self.preview_status_lbl.configure(text="Error al preparar")
            messagebox.showerror("Vista previa", err[:950])
            return
        if not tmp:
            self.preview_prepare_btn.configure(state="normal")
            self.preview_status_lbl.configure(text="Error al preparar")
            return
        self._preview_apply_ready(src, tmp)

    def _preview_apply_ready(self, original: str, wav_path: str) -> None:
        self._preview_cleanup_temp()
        self._preview_original_path = original

        try:
            rate, pcm = _load_wav_numpy(wav_path)
        except Exception as e:
            self.preview_status_lbl.configure(text="No se pudo leer el WAV.")
            messagebox.showerror("Vista previa", str(e))
            self._preview_reset(True)
            self.preview_prepare_btn.configure(state="normal")
            try:
                if os.path.isfile(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass
            return

        try:
            if os.path.isfile(wav_path):
                os.remove(wav_path)
        except OSError:
            pass

        self._wav_rate = rate
        self._wav_data = pcm
        self._preview_duration_sec = len(pcm) / float(rate)
        self._preview_loaded_ok = True
        self._playback_base_sec = 0.0
        self._preview_playing = False
        self._preview_paused = False
        self._preview_stop_sound()

        self.preview_slider.configure(state="normal")
        self.preview_play_btn.configure(state="normal")
        self.preview_mark_desde_btn.configure(state="normal")
        self.preview_mark_hasta_btn.configure(state="normal")
        self.preview_status_lbl.configure(text=f"Listo · {Path(original).name}")
        self._preview_update_time_ui(0.0)
        self.preview_prepare_btn.configure(state="normal")

    def _play_audio_from(self, sec: float) -> None:
        if self._wav_data is None or self._wav_rate <= 0:
            return
        self._preview_stop_sound()
        dur = self._preview_duration_sec or (len(self._wav_data) / float(self._wav_rate))
        sec = max(0.0, min(sec, dur))
        fi = int(sec * self._wav_rate)
        if fi >= len(self._wav_data):
            self._playback_base_sec = dur
            self._preview_playing = False
            self._preview_paused = False
            self.preview_play_btn.configure(text="▶ Reproducir")
            self._preview_update_time_ui(dur)
            return
        chunk = self._wav_data[fi:]
        try:
            sd.play(chunk, self._wav_rate, blocking=False)
        except Exception as e:
            messagebox.showerror("Vista previa", str(e))
            return
        self._playback_base_sec = sec
        self._playback_started_mono = time.monotonic()
        self._preview_playing = True
        self._preview_paused = False
        self.preview_play_btn.configure(text="⏸ Pausar")

    def _preview_get_playhead_sec(self) -> float:
        if self._preview_playing and not self._preview_paused:
            elapsed = time.monotonic() - self._playback_started_mono
            pos = self._playback_base_sec + elapsed
        else:
            pos = self._playback_base_sec
        if self._preview_duration_sec is not None:
            pos = min(max(pos, 0.0), self._preview_duration_sec)
        return pos

    def _preview_update_time_ui(self, pos: float) -> None:
        d = self._preview_duration_sec or 0.0
        cur = _fmt_timecode(pos, 2)
        tot = _fmt_timecode(d, 2) if d else "—"
        self.preview_time_var.set(f"{cur} / {tot}")
        if d > 0:
            self._preview_slider_programmatic = True
            self.preview_slider.set(min(max(pos / d, 0), 1))
            self._preview_slider_programmatic = False
        self._preview_draw_timeline()

    def _preview_schedule_tick(self) -> None:
        self._cancel_preview_tick()
        self._preview_tick_after_id = self.after(110, self._preview_tick)

    def _preview_tick(self) -> None:
        self._preview_tick_after_id = None
        if not self._preview_loaded_ok:
            return
        pos = self._preview_get_playhead_sec()
        if self._preview_duration_sec and pos >= self._preview_duration_sec - 0.06:
            self._preview_stop_sound()
            self._preview_playing = False
            self._preview_paused = False
            self._playback_base_sec = self._preview_duration_sec
            self.preview_play_btn.configure(text="▶ Reproducir")
            self._preview_update_time_ui(self._playback_base_sec)
            return

        self._preview_update_time_ui(pos)
        if self._preview_playing and not self._preview_paused:
            self._preview_schedule_tick()

    def _preview_slider_moved(self, val: float | str) -> None:
        if self._preview_slider_programmatic:
            return
        if not self._preview_loaded_ok:
            return
        self._slider_pending = float(val)
        if self._slider_after_id:
            try:
                self.after_cancel(self._slider_after_id)
            except Exception:
                pass
        self._slider_after_id = self.after(90, self._apply_preview_seek_from_slider)

    def _apply_preview_seek_from_slider(self) -> None:
        self._slider_after_id = None
        if not self._preview_loaded_ok or not self._preview_duration_sec:
            return
        pos = self._slider_pending * self._preview_duration_sec
        self._preview_seek_to(pos, update_slider=False)

    def _preview_seek_to(self, pos: float, *, update_slider: bool = True) -> None:
        if not self._preview_loaded_ok or not self._preview_duration_sec:
            return
        pos = max(0.0, min(pos, self._preview_duration_sec))
        self._playback_base_sec = pos
        self._preview_stop_sound()
        self._preview_paused = False
        self._preview_playing = False
        self._cancel_preview_tick()
        self.preview_play_btn.configure(text="▶ Reproducir")
        if update_slider:
            self._preview_slider_programmatic = True
            self.preview_slider.set(pos / self._preview_duration_sec)
            self._preview_slider_programmatic = False
        self._preview_update_time_ui(pos)

    def _preview_toggle_play(self) -> None:
        if not self._preview_loaded_ok:
            return
        if self._preview_playing and not self._preview_paused:
            pos = self._preview_get_playhead_sec()
            self._preview_stop_sound()
            self._preview_paused = True
            self._preview_playing = True
            self._playback_base_sec = pos
            self._cancel_preview_tick()
            self.preview_play_btn.configure(text="▶ Seguir")
            self._preview_update_time_ui(pos)
            return

        if self._preview_paused:
            self._preview_paused = False
            self._play_audio_from(self._playback_base_sec)
            self._preview_schedule_tick()
            return

        self._play_audio_from(self._playback_base_sec)
        self._preview_schedule_tick()

    def _preview_mark_desde(self) -> None:
        if not self._preview_loaded_ok:
            return
        pos = self._preview_get_playhead_sec()
        self.trim_enabled_var.set(True)
        self.trim_start_entry.delete(0, "end")
        self.trim_start_entry.insert(0, _fmt_timecode(pos, 3))
        self._preview_draw_timeline()

    def _preview_mark_hasta(self) -> None:
        if not self._preview_loaded_ok:
            return
        pos = self._preview_get_playhead_sec()
        self.trim_enabled_var.set(True)
        self.trim_end_entry.delete(0, "end")
        self.trim_end_entry.insert(0, _fmt_timecode(pos, 3))
        self._preview_draw_timeline()

    def _preview_draw_timeline(self, _event=None) -> None:
        c = self.preview_canvas
        c.delete("all")
        w = max(c.winfo_width(), 2)
        h = max(c.winfo_height(), 2)
        pad = 6
        bar_y1 = 10
        bar_y2 = h - 12
        c.create_rectangle(pad, bar_y1, w - pad, bar_y2, outline="#C4B5FD", fill=TIMELINE_BAR)

        dur = self._preview_duration_sec
        if not dur or dur <= 0:
            return

        ds_raw = self.trim_start_entry.get().strip()
        de_raw = self.trim_end_entry.get().strip()
        ds = self._parse_flexible_time(ds_raw) if ds_raw else None
        de = self._parse_flexible_time(de_raw) if de_raw else None

        inner_w = w - 2 * pad

        def x_at(sec: float | None) -> float | None:
            if sec is None:
                return None
            sec = max(0.0, min(sec, dur))
            return pad + (sec / dur) * inner_w

        if ds is not None or de is not None:
            x1 = x_at(ds) if ds is not None else pad
            x2 = x_at(de) if de is not None else w - pad
            x1, x2 = min(x1, x2), max(x1, x2)
            if x2 > x1:
                c.create_rectangle(x1, bar_y1 + 2, x2, bar_y2 - 2, outline="", fill="#C4B5FD")

        ph = self._preview_get_playhead_sec() if self._preview_loaded_ok else 0.0
        xh = pad + (min(max(ph, 0), dur) / dur) * inner_w
        c.create_line(xh, bar_y1, xh, bar_y2, fill=ACCENT, width=2)

        if ds is not None:
            xd = x_at(ds)
            if xd is not None:
                c.create_line(xd, bar_y1 - 2, xd, bar_y2 + 2, fill=SUCCESS, width=2)
        if de is not None:
            xe = x_at(de)
            if xe is not None:
                c.create_line(xe, bar_y1 - 2, xe, bar_y2 + 2, fill="#EA580C", width=2)

    # ── Conversión ────────────────────────────────────────────────────────────

    def _start_conversion(self):
        if self.converting:
            return
        if not self.file_rows:
            messagebox.showinfo("Sin archivos", "Agrega al menos un archivo compatible.")
            return
        if not self._validate_trim_before_convert():
            return
        if self._preview_loaded_ok:
            self._preview_stop_sound()
            self._preview_playing = False
            self._preview_paused = False
            self._cancel_preview_tick()
            self.preview_play_btn.configure(text="▶ Reproducir")
        self.converting = True
        fmt_name = self.output_format
        self.convert_btn.configure(
            state="disabled",
            text=(
                f"Exportando fragmento a {fmt_name}…"
                if self.trim_enabled_var.get()
                else f"Convirtiendo a {fmt_name}…"
            ),
        )
        self.global_progress.set(0)
        threading.Thread(target=self._convert_all, daemon=True).start()

    def _convert_all(self):
        total = len(self.file_rows)
        fmt_name = self.output_format
        ext, audio_args = OUTPUT_FORMATS.get(fmt_name, OUTPUT_FORMATS["MP3"])
        for idx, row in enumerate(self.file_rows):
            progress_queue.put(("status", row, "Convirtiendo…", WARNING))
            progress_queue.put(("progress", row, 0.05))

            out_path = Path(self.output_dir) / (Path(row.filepath).stem + ext)
            success, msg = self._run_ffmpeg(row.filepath, str(out_path), row, audio_args)

            if success:
                progress_queue.put(("progress", row, 1.0))
                progress_queue.put(("status", row, "✓ Listo", SUCCESS))
            else:
                progress_queue.put(("status", row, "✗ Error", ERROR))
                progress_queue.put(("error", msg))

            global_val = (idx + 1) / total
            progress_queue.put(("global", global_val, f"{idx + 1}/{total} completados"))

        progress_queue.put(("done",))

    def _run_ffmpeg(self, input_path: str, output_path: str, row: FileRow, audio_args: list[str] | None = None):
        """Ejecuta FFmpeg y actualiza la barra de progreso."""
        try:
            full_duration = self._get_duration(input_path)
            trim_on = self.trim_enabled_var.get()
            start_raw = self.trim_start_entry.get().strip()
            end_raw = self.trim_end_entry.get().strip()
            start_sec = self._parse_flexible_time(start_raw) if start_raw else 0.0
            end_sec = self._parse_flexible_time(end_raw) if end_raw else None

            cmd = [FFMPEG_PATH, "-y", "-i", input_path]

            duration_for_progress = full_duration

            if trim_on:
                if start_raw and start_sec is None:
                    return False, "«Desde» no es válido."
                if start_sec < 0:
                    return False, "«Desde» no puede ser negativo."
                if end_raw and end_sec is None:
                    return False, "«Hasta» no es válido."
                if end_sec is not None and end_sec <= start_sec:
                    return False, "«Hasta» debe ser mayor que «Desde»."
                if full_duration is not None and start_sec >= full_duration:
                    return False, "«Desde» supera la duración del archivo."

                if start_sec > 0:
                    cmd.extend(["-ss", _fmt_ffmpeg_seconds(start_sec)])

                if end_raw:
                    segment_len = end_sec - start_sec
                    if segment_len <= 0:
                        return False, "El fragmento recortado no tiene duración válida."
                    cmd.extend(["-t", _fmt_ffmpeg_seconds(segment_len)])
                    duration_for_progress = segment_len
                elif full_duration is not None:
                    duration_for_progress = max(0.0, full_duration - start_sec)

            if audio_args is None:
                audio_args = OUTPUT_FORMATS["MP3"][1]
            cmd.extend(["-vn"] + audio_args + [output_path])

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **_FFMPEG_PIPE_TEXT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            # Leer stderr para extraer tiempo actual y calcular progreso
            for line in proc.stderr:
                if (
                    "time=" in line
                    and duration_for_progress
                    and duration_for_progress > 0
                ):
                    t = self._parse_time(line)
                    if t is not None:
                        pct = min(t / duration_for_progress, 0.99)
                        progress_queue.put(("progress", row, pct))

            proc.wait()
            if proc.returncode == 0:
                return True, ""
            else:
                err = proc.stderr.read() if proc.stderr else "Error desconocido"
                return False, err

        except Exception as e:
            return False, str(e)

    def _get_duration(self, path: str) -> float | None:
        """Duración en segundos; ffprobe si existe, si no ffmpeg -i. Puede ser None."""
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        ffprobe = re.sub(r"ffmpeg", "ffprobe", FFMPEG_PATH, count=1, flags=re.I)
        try:
            if os.path.isfile(ffprobe):
                r = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    **_FFMPEG_PIPE_TEXT,
                    creationflags=creationflags,
                )
                if r.returncode == 0 and r.stdout.strip():
                    d = float(r.stdout.strip())
                    if d > 0:
                        return d
        except (ValueError, OSError, subprocess.SubprocessError):
            pass

        try:
            result = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-i", path],
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                **_FFMPEG_PIPE_TEXT,
                creationflags=creationflags,
            )
            for line in result.stderr.splitlines():
                if "Duration:" not in line:
                    continue
                dur_str = line.split("Duration:")[1].split(",")[0].strip()
                if dur_str == "N/A":
                    continue
                parsed = self._parse_time_str(dur_str)
                if parsed is not None and parsed > 0:
                    return parsed
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_time(line: str) -> float | None:
        """Extrae segundos de una línea 'time=HH:MM:SS.xx'."""
        try:
            part = line.split("time=")[1].split(" ")[0].strip()
            return App._parse_time_str(part)
        except Exception:
            return None

    @staticmethod
    def _parse_time_str(s: str) -> float | None:
        try:
            parts = s.split(":")
            h, m, sec = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + sec
        except Exception:
            return None

    # ── Cola de progreso (hilo principal) ────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = progress_queue.get_nowait()
                kind = msg[0]

                if kind == "status":
                    _, row, text, color = msg
                    row.set_status(text, color)

                elif kind == "progress":
                    _, row, val = msg
                    row.set_progress(val)

                elif kind == "global":
                    _, val, text = msg
                    self.global_progress.set(val)
                    self.global_status.configure(text=text)

                elif kind == "error":
                    _, err_msg = msg
                    messagebox.showerror("Error de conversión", err_msg[:400])

                elif kind == "done":
                    self.converting = False
                    self._refresh_convert_button_label()
                    self.convert_btn.configure(state="normal")
                    self.global_status.configure(
                        text=f"✓ Conversión completada — guardado en {self.output_dir}"
                    )

        except queue.Empty:
            pass

        self.after(80, self._poll_queue)


# ── Punto de entrada ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
