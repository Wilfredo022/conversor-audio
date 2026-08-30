# Conversor Audio

Aplicación de escritorio para **convertir y recortar audio**. Acepta vídeo o audio, extrae el sonido y lo guarda en el formato que elijas (MP3, WAV, FLAC, etc.).

No hace falta instalar FFmpeg a mano: la app usa el que trae `imageio-ffmpeg`.

---

## Qué puedes hacer

- Arrastrar archivos a la zona violeta o elegirlos con el botón
- Convertir uno o varios archivos a la vez
- Elegir formato de salida: **MP3, M4A, AAC, WAV, OGG, FLAC**
- Recortar un fragmento (Desde → Hasta) y exportar solo ese tramo
- Escuchar una vista previa para marcar el corte con precisión
- Elegir la carpeta de destino (por defecto: Descargas)

---

## Requisitos

- **Windows 10 u 11** (también puede funcionar en macOS/Linux)
- **Python 3.10 o superior**
- Un dispositivo de audio si quieres usar la vista previa (reproducir / marcar cortes)

En Windows, si `python` no funciona en la terminal, usa el lanzador `py`.

---

## Instalación

1. Clona o copia esta carpeta.

2. Abre una terminal **dentro del proyecto** y crea un entorno virtual (recomendado):

```bash
py -3 -m venv .venv
.venv\Scripts\activate
```

En macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Instala las dependencias:

```bash
pip install -r requirements.txt
```

---

## Cómo usarlo

Arranca la app:

```bash
py app.py
```

o, si `python` está en el PATH:

```bash
python app.py
```

### Convertir archivos completos

1. Arrastra los archivos a **Arrastra tus archivos aquí**, o pulsa **Seleccionar archivos**.
2. Revisa la lista **Archivos agregados**.
3. Elige el **formato de salida** (pills de la barra inferior).
4. Comprueba **Guardar en** (o pulsa **Cambiar**).
5. Pulsa **Convertir a MP3** (el texto cambia según el formato).

El archivo nuevo se guarda con el **mismo nombre** y la extensión del formato elegido. Ejemplo: `clase.mp4` → `clase.mp3`.

### Recortar un fragmento

1. Agrega el archivo (o varios).
2. Activa **Recortar audio**.
3. Elige el archivo en la lista de la guía.
4. Pulsa **Preparar vista previa** y espera a que cargue.
5. Reproduce, mueve el slider y usa **Marcar «Desde»** / **Marcar «Hasta»**.
6. También puedes escribir el tiempo a mano:
   - segundos: `90`
   - `MM:SS`: `1:30`
   - `HH:MM:SS`: `0:01:30`
7. Pulsa **Exportar fragmento a …** en la barra inferior.

Si el recorte está activo, **todos** los archivos de la lista se exportan con el mismo Desde/Hasta.

---

## Archivos que acepta

| Tipo | Extensiones |
| --- | --- |
| Vídeo | `.mp4` `.m4v` `.mov` `.mkv` `.avi` `.webm` |
| Audio | `.m4a` `.aac` `.mp3` `.wav` `.flac` `.ogg` `.opus` `.wma` `.aiff` `.aif` |

La conversión **solo exporta audio** (quita la imagen del vídeo).

### Calidad de salida (aprox.)

| Formato | Notas |
| --- | --- |
| MP3 | Calidad alta (`-q:a 2`), 44.1 kHz |
| M4A / AAC | 192 kbps |
| OGG | Vorbis calidad 5 |
| WAV | PCM 16-bit (sin pérdida de códec) |
| FLAC | Sin pérdida |

---

## Cómo funciona (por dentro)

1. La interfaz está hecha con **CustomTkinter**.
2. El arrastrar y soltar lo gestiona **tkinterdnd2**.
3. Al convertir, un hilo en segundo plano lanza **FFmpeg** por cada archivo.
4. El progreso se lee de la salida de FFmpeg (`time=…`) y se muestra en la lista.
5. Si hay recorte, FFmpeg usa `-ss` (inicio) y `-t` (duración del fragmento).
6. La vista previa decodifica el audio a un WAV temporal, lo carga con **numpy** y lo reproduce con **sounddevice**.

Archivos principales:

```
conversor-audio/
├── app.py              # toda la aplicación
├── requirements.txt    # dependencias
└── README.md           # esta guía
```

---

## Problemas frecuentes

**La ventana se ve borrosa o pixelada**  
Cierra la app y ábrela de nuevo. Está pensada para pantallas HiDPI de Windows. Si sigue mal, revisa la escala de pantalla (100 / 125 / 150%).

**No se oye la vista previa**  
Comprueba que el volumen del sistema no esté muteado y que haya un dispositivo de salida activo. La conversión no depende del audio del PC.

**«Formato no soportado»**  
Esa extensión no está en la lista. Puedes convertir el archivo a uno compatible con otra herramienta y luego usarlo aquí.

**Error al convertir**  
El archivo puede estar dañado, protegido o en un códec raro. Prueba con otro archivo o exporta primero a MP4/WAV.

**`python` no se reconoce**  
Usa `py -3 app.py` en Windows.

---

## Para quien quiera contribuir o reutilizar el código

- Toda la lógica está en `app.py`.
- Formatos de salida: diccionario `OUTPUT_FORMATS`.
- Extensiones de entrada: tupla `SUPPORTED_EXTENSIONS`.
- Nombre de la app: `APP_NAME` / `APP_TAGLINE`.

Si añades un formato nuevo, incluye la extensión y los argumentos de FFmpeg en `OUTPUT_FORMATS`.
