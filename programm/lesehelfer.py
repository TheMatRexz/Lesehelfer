"""
Lesehelfer  –  liest Texte aus Spielen, vom Desktop und in VR laut vor
=======================================================================
Schaut auf einen markierten Bereich des Bildschirms (das Textfeld im Spiel),
erkennt den Text (Windows-Texterkennung) und liest neue Texte mit einer
natürlichen Stimme vor. Bedienung über ein kleines Fenster oder Tasten.

Start: Lesehelfer_starten.bat (installiert alles Nötige beim ersten Mal).
"""

import ctypes
import difflib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

# Ordnerstruktur:
#   Lesehelfer\Lesehelfer_starten.bat
#   Lesehelfer\programm\lesehelfer.py   (dieses Programm)
#   Lesehelfer\daten\                   (Einstellungen, Sprachmodelle)
#   Lesehelfer\protokoll\               (Protokoll für die Fehlersuche)
ORDNER = os.path.dirname(os.path.abspath(__file__))
BASIS = os.path.dirname(ORDNER) if os.path.basename(ORDNER).lower() == "programm" else ORDNER
DATEN = os.path.join(BASIS, "daten")
PROTOKOLL = os.path.join(BASIS, "protokoll")
for _d in (DATEN, PROTOKOLL):
    os.makedirs(_d, exist_ok=True)
EINST_DATEI = os.path.join(DATEN, "einstellungen.json")

# ---------------------------------------------------------------- Updates
VERSION = "1.0.0"
UPDATE_QUELLE = "https://raw.githubusercontent.com/TheMatRexz/Lesehelfer/main/"
UPDATE_MERKER = os.path.join(DATEN, "gerade_aktualisiert.txt")
UPDATE_ABSTAND = 6 * 3600     # während das Programm läuft: alle 6 Stunden nachsehen
LOGS_BEHALTEN = 10                 # so viele Sitzungs-Protokolle bleiben erhalten
LANGZEIT_TAGE = 14            # mit "langzeit" in fehlersuche.txt
LOG_MAX_BYTES = 5 * 1024 * 1024    # ab dieser Größe wird eine neue Teil-Datei begonnen
FEHLERSUCHE_DATEI = os.path.join(DATEN, "fehlersuche.txt")
EIGENE_TOENE = os.path.join(BASIS, "eigene_toene")


def fehlersuche(schalter):
    """Zusätzliche Ausgaben nur, wenn in daten\fehlersuche.txt das Stichwort steht
    (z. B. "rohdaten", "bilder", "uebersetzung")."""
    try:
        with open(FEHLERSUCHE_DATEI, encoding="utf-8") as f:
            return schalter in f.read().lower()
    except OSError:
        return False

# Ohne Konsole (pythonw) Ausgaben in eine Log-Datei schreiben
import multiprocessing as _mp
# Hilfsprozess erkennen: Das Hauptprogramm trägt seine Prozessnummer in die Umgebung ein,
# die der Hilfsprozess erbt. (parent_process() ist beim Laden des Hilfsprozesses noch nicht gesetzt.)
_IST_HILFSPROZESS = (os.environ.get("VORLESER_HAUPT_PID", str(os.getpid())) != str(os.getpid())
                     or _mp.parent_process() is not None)
if not _IST_HILFSPROZESS:
    os.environ["VORLESER_HAUPT_PID"] = str(os.getpid())

# Pro Programmstart eine eigene Protokolldatei: protokoll\vorleser_2026-11-15_10-33-00.txt
# Der Hilfsprozess (Texterkennung) schreibt in dieselbe Datei – der Name wird über die Umgebung weitergegeben.
if _IST_HILFSPROZESS and os.environ.get("VORLESER_LOG"):
    LOG_DATEI = os.environ["VORLESER_LOG"]
else:
    LOG_DATEI = os.path.join(PROTOKOLL, time.strftime("vorleser_%Y-%m-%d_%H-%M-%S.txt"))
    os.environ["VORLESER_LOG"] = LOG_DATEI


class _Protokoll:
    """Schreibt ins Protokoll: ohne Farbcodes und mit Größenbegrenzung."""
    _farbe = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, pfad):
        self.basis = pfad
        self.pfad = pfad
        self.teil = 1
        self.zaehler = 0
        self.f = open(pfad, "a", encoding="utf-8", buffering=1)

    def write(self, text):
        # technische Meldungen von RapidOCR weglassen (Fehler bleiben drin)
        if "[RapidOCR]" in text and ("[INFO]" in text or "detection result is empty" in text):
            return len(text)
        text = self._farbe.sub("", text)
        self.f.write(text)
        self.zaehler += 1
        if self.zaehler % 200 == 0:
            self._groesse_pruefen()
        return len(text)

    def _groesse_pruefen(self):
        try:
            if os.path.getsize(self.pfad) > LOG_MAX_BYTES:
                self.f.close()
                self.teil += 1
                self.pfad = self.basis.replace(".txt", f"_teil{self.teil}.txt")
                self.f = open(self.pfad, "a", encoding="utf-8", buffering=1)
        except OSError:
            pass

    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass


def _alte_protokolle_entfernen():
    """Normal: die Protokolle der letzten 10 Starts behalten.
    Steht in daten\\fehlersuche.txt das Wort "langzeit": alles der letzten 14 Tage behalten (für Langzeittests)."""
    try:
        alt = os.path.join(PROTOKOLL, "vorleser_log.txt")  # Datei aus früheren Versionen
        if os.path.exists(alt):
            os.remove(alt)
        langzeit = fehlersuche("langzeit")
        grenze = time.time() - LANGZEIT_TAGE * 86400
        for muster in (r"vorleser_\d{4}-\d\d-\d\d_.*\.txt$", r"leistung_\d{4}-\d\d-\d\d_.*\.txt$"):
            dateien = sorted(f for f in os.listdir(PROTOKOLL) if re.match(muster, f))
            sitzungen = sorted({re.sub(r"_teil\d+", "", f) for f in dateien})
            if langzeit:
                zu_alt = set()
                for x in sitzungen:   # Datum steht im Namen: vorleser_JJJJ-MM-TT_HH-MM-SS.txt
                    m = re.search(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)", x)
                    if m and time.mktime(time.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")) < grenze:
                        zu_alt.add(x)
            else:
                zu_alt = set(sitzungen[:-LOGS_BEHALTEN]) if len(sitzungen) > LOGS_BEHALTEN else set()
            for f in dateien:
                if re.sub(r"_teil\d+", "", f) in zu_alt:
                    os.remove(os.path.join(PROTOKOLL, f))
    except OSError:
        pass


if not _IST_HILFSPROZESS:
    _alte_protokolle_entfernen()
def _ohne_konsole():
    # Achtung: Nach einem automatischen Neustart zeigt die Ausgabe auf "NUL" – und das meldet
    # unter Windows isatty() = True. Deshalb zählt pythonw.exe immer als "ohne Konsole".
    try:
        if os.path.basename(sys.executable).lower() == "pythonw.exe":
            return True
        return sys.stdout is None or not sys.stdout.isatty()
    except Exception:
        return True


_LOG_ZIEL = None   # Ziel für log(), siehe unten

if _ohne_konsole():  # gestartet ohne schwarzes Fenster -> alles ins Protokoll
    _log = _Protokoll(LOG_DATEI)
    sys.stdout = sys.stderr = _log
    _LOG_ZIEL = _log


def log(*teile):
    """Schreibt direkt ins Protokoll – auch wenn ein Zusatzpaket sys.stdout verbiegt."""
    try:
        zeile = time.strftime("%H:%M:%S") + " " + " ".join(str(t) for t in teile) + "\n"
        if _LOG_ZIEL is not None:
            _LOG_ZIEL.write(zeile)
            _LOG_ZIEL.flush()
        else:
            print(zeile, end="", flush=True)
    except Exception:
        pass


# Bildschirm-Skalierung (z. B. 125 %) korrekt behandeln – vor tkinter!
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
from tkinter import messagebox, simpledialog

try:
    import mss
    import keyboard
    from PIL import Image, ImageOps, ImageEnhance, ImageTk, ImageChops
except Exception as e:  # fehlende Pakete verständlich melden
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Lesehelfer",
        f"Es fehlt etwas: {e}\n\nBitte das Programm über 'Lesehelfer_starten.bat' starten.",
    )
    sys.exit(1)


def _ctk_laden():
    """CustomTkinter (runde Knöpfe/Karten) laden – fehlt es, wird es einmalig nachinstalliert."""
    try:
        import customtkinter
        return customtkinter
    except ImportError:
        pass
    if _IST_HILFSPROZESS:
        return None
    import subprocess
    python = sys.executable
    if python.lower().endswith("pythonw.exe"):
        python = python[:-len("pythonw.exe")] + "python.exe"
    try:
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "customtkinter"],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import customtkinter
        return customtkinter
    except Exception as e:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("Lesehelfer", f"Oberfläche konnte nicht geladen werden: {e}\n\n"
                                             "Bitte das Programm über 'Lesehelfer_starten.bat' starten.")
        sys.exit(1)


ctk = _ctk_laden()

# ---------------------------------------------------------------- feste Einstellungen
INTERVALL = 0.25           # Sekunden zwischen zwei Bildschirmlesungen
STABIL_LESUNGEN = 3        # so oft muss ein Text gleich bleiben, bevor er vorgelesen wird
AEHNLICHKEIT = 0.85        # ab hier gilt ein Text als "schon vorgelesen"
MIN_ZEICHEN = 3
MIN_WOERTER = 2
MENUE_WOERTER = ["angriff", "magie", "item", "drive", "sprechen", "summon", "limit"]
VERGROESSERUNG = 2

# Ersatzstimmen, falls eine Stimme vom Dienst gerade keinen Ton liefert (in dieser Reihenfolge probiert)
KI_ERSATZ = {
    ("de", "w"): [("de-DE-SeraphinaMultilingualNeural", "Seraphina"), ("de-DE-KatjaNeural", "Katja"),
                  ("de-DE-AmalaNeural", "Amala")],
    ("de", "m"): [("de-DE-ConradNeural", "Conrad"), ("de-DE-KillianNeural", "Killian"),
                  ("de-DE-FlorianMultilingualNeural", "Florian")],
    ("en", "w"): [("en-US-AvaMultilingualNeural", "Ava"), ("en-US-JennyNeural", "Jenny"),
                  ("en-US-AriaNeural", "Aria")],
    ("en", "m"): [("en-US-AndrewMultilingualNeural", "Andrew"), ("en-US-GuyNeural", "Guy"),
                  ("en-US-ChristopherNeural", "Christopher")],
}

KI_STIMMEN = {
    "de": {"w": ("de-DE-SeraphinaMultilingualNeural", "Seraphina"),
           "m": ("de-DE-ConradNeural", "Conrad")},
    "en": {"w": ("en-US-AvaMultilingualNeural", "Ava"),
           "m": ("en-US-AndrewMultilingualNeural", "Andrew")},
}

FUNKTIONEN = [  # (Schlüssel, Anzeigename, Standardtaste)
    ("pause", "Pause / weiter", "f9"),
    ("wiederholen", "Nochmal vorlesen", "f10"),
    ("stimme", "Frau / Mann", "f7"),
    ("markieren", "Textfeld markieren", "f8"),
    ("beenden", "Beenden", "ctrl+q"),
    ("maus", "Text unter der Maus", ""),   # absichtlich frei: jeder legt sie selbst fest
    ("vr", "VR: Text in der Blickmitte", ""),   # Ersatz/Ergänzung zur Controller-Taste
]
# Tasten, die PCSX2 (je nach Einstellung) selbst benutzt
PCSX2_TASTEN = {"f1", "f2", "f3", "f4", "f6", "f8", "f9", "f11", "f12", "tab", "space", "esc",
                "shift+f2", "alt+enter"}

# Farben (wie in der Skizze)
BG, PANEL, RAND, KNOPF = "#14161C", "#1B1E26", "#2A2E39", "#232733"
TEXT, BLASS, GRUEN, DUNKEL = "#E8EAF0", "#9AA0AE", "#3DDC84", "#0B1A10"
GRAU, GELB, GRUEN_HELL = "#3A3F4D", "#F5C451", "#1E2A22"
DURCHSICHTIG = "#010203"      # Farbe, die bei Overlays unsichtbar geschaltet wird
SCHRIFT = "Segoe UI"


# ================================================================ Einstellungen
class Einstellungen:
    STANDARD = {
        "profil": "Mein Spiel",
        "profile": {"Mein Spiel": {"bereich": None, "sprache": "de"}},
        "stimme": "w",
        "tempo": -8,
        "lautstaerke": 100,
        "ki_stimmen": True,
        "tasten": {k: t for k, _, t in FUNKTIONEN},
        "mini_pos": None,
        "fenster_pos": None,
        "unterbrechen": False,
        "rueckmeldung": "ton",       # ton / ansage / beides / aus
        "reiter": "spiele",          # zuletzt offener Reiter
        "modus": "spiele",           # was gerade vorliest: spiele / desktop / vr (folgt dem Reiter)
        "vr": {"modus": "taste", "wartezeit": 1.5, "bereich": "mittel", "uebersetzen": True,
               "fenster": "", "knopf": None},
        "desktop": {"an": False, "modus": "taste", "wartezeit": 1.0, "rahmen": True, "uebersetzen": True},
        "in_uebertragung_sichtbar": True,
        "rechner": "auto",           # Texterkennung auf: auto / cpu / gpu
    }

    def __init__(self):
        self.d = json.loads(json.dumps(self.STANDARD))
        try:
            with open(EINST_DATEI, encoding="utf-8") as f:
                geladen = json.load(f)
            self.d.update(geladen)
            for k, _, t in FUNKTIONEN:
                self.d["tasten"].setdefault(k, t)
        except FileNotFoundError:
            self._alte_dateien_uebernehmen()
        except Exception as e:
            log("Einstellungen nicht lesbar:", e)
        if self.d["profil"] not in self.d["profile"]:
            self.d["profil"] = next(iter(self.d["profile"]))

    def _alte_dateien_uebernehmen(self):
        """Übernimmt Bereich und Stimme aus der älteren Version."""
        try:
            with open(os.path.join(BASIS, "vorleser_bereich.json"), encoding="utf-8") as f:
                self.d["profile"]["Mein Spiel"]["bereich"] = json.load(f)
        except Exception:
            pass
        try:
            with open(os.path.join(BASIS, "vorleser_stimme.json"), encoding="utf-8") as f:
                if "Conrad" in json.load(f).get("stimme", ""):
                    self.d["stimme"] = "m"
        except Exception:
            pass
        self.speichern()

    def speichern(self):
        try:
            with open(EINST_DATEI, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("Speichern fehlgeschlagen:", e)

    @property
    def profil(self):
        return self.d["profile"][self.d["profil"]]

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v
        self.speichern()


# ================================================================ Texterkennung
class RapidOCRMotor:
    """Lokale Texterkennung mit kleinem Modell (RapidOCR / PP-OCR). Kostenlos, offline, ohne Konto."""
    art = "rapid"
    name = "RapidOCR (lokal)"

    def __init__(self, gpu=False):
        import numpy  # noqa: F401
        import logging
        import rapidocr
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        from rapidocr import RapidOCR
        LangRec = getattr(rapidocr, "LangRec", None)
        OCRVersion = getattr(rapidocr, "OCRVersion", None)
        ModelType = getattr(rapidocr, "ModelType", None)
        if LangRec is None or not hasattr(LangRec, "LATIN"):
            raise RuntimeError("diese RapidOCR-Version kennt kein lateinisches Modell")
        latin = LangRec.LATIN
        mobile = getattr(ModelType, "MOBILE", "mobile") if ModelType else "mobile"
        versionen = [getattr(OCRVersion, n) for n in ("PPOCRV5", "PPOCRV4") if OCRVersion and hasattr(OCRVersion, n)]
        # Zeilensuche feiner: eng stehende Zeilen nicht zusammenwachsen lassen
        det = {"Det.use_dilation": False, "Det.unclip_ratio": 1.4, "Det.box_thresh": 0.4}
        versuche = [dict(det, **{"Rec.lang_type": latin, "Rec.ocr_version": v, "Rec.model_type": mobile})
                    for v in versionen]
        versuche.append(dict(det, **{"Rec.lang_type": latin, "Rec.model_type": mobile}))
        versuche += [{"Rec.lang_type": latin, "Rec.ocr_version": v, "Rec.model_type": mobile} for v in versionen]
        versuche.append({"Rec.lang_type": latin})
        if gpu:   # Grafikkarte über DirectML (Paket onnxruntime-directml)
            versuche = [dict(p, **{"EngineConfig.onnxruntime.use_dml": True}) for p in versuche]

        # Testbild mit Text, damit auch die Erkennung (nicht nur das Laden) geprüft wird
        from PIL import ImageDraw
        test = Image.new("RGB", (360, 60), "white")
        ImageDraw.Draw(test).text((10, 15), "Hallo Welt Test", fill="black", font_size=28)
        import numpy as np
        letzter_fehler = None
        self.engine = None
        for params in versuche:
            try:
                e = RapidOCR(params=params)
                r = e(np.asarray(test), use_det=True, use_cls=False, use_rec=True)
                if getattr(r, "txts", None) is None and getattr(r, "boxes", None) is not None:
                    raise RuntimeError("keine Erkennung")
                self.engine = e
                try:
                    self.geraete = list(e.text_det.session.session.get_providers())
                except Exception:
                    self.geraete = []
                log("RapidOCR mit", params, "Test:", getattr(r, "txts", None), "Geräte:", self.geraete)
                break
            except Exception as ex:
                letzter_fehler = ex
                log("RapidOCR-Variante geht nicht:", params, ex)
        if self.engine is None:
            raise RuntimeError(f"kein lateinisches Modell: {letzter_fehler}")

    def zeilen(self, img, sprache):
        import numpy as np
        bild = np.asarray(img.convert("RGB"))
        # Einstellungen bei JEDEM Aufruf angeben – RapidOCR merkt sich sonst die vom letzten Aufruf
        res = self.engine(bild, use_det=True, use_cls=False, use_rec=True)
        boxen = getattr(res, "boxes", None)
        texte = getattr(res, "txts", None)
        werte = getattr(res, "scores", None)
        if boxen is None or not texte:
            return []
        kaesten = []
        for i, t in enumerate(texte):
            b = np.asarray(boxen[i])
            xs, ys = b[:, 0], b[:, 1]
            sc = werte[i] if werte is not None and i < len(werte) else 1.0
            kaesten.append([t, float(xs.min()), float(ys.min()), float(ys.max() - ys.min()),
                            float(xs.max() - xs.min()), sc])
        if fehlersuche("rohdaten"):
            log("ROH:", [(k[0], round(k[2]), round(k[3]), round(k[4]), round(float(k[5]), 2)) for k in kaesten])
        kaesten = [k for k in kaesten if k[5] >= 0.5]
        kaesten += self._luecken_fuellen(bild, kaesten)
        return [(t, x, y, h, w) for t, x, y, h, w, sc in kaesten]

    def _luecken_fuellen(self, bild, kaesten):
        """Übersehene Zeilen nachlesen: Liegt zwischen zwei Textzeilen Platz für weitere Zeilen,
        wird dieser Streifen gezielt gelesen (bei eng gesetzten Spieltexten nötig)."""
        textzeilen = sorted([k for k in kaesten if len(k[0]) >= 8], key=lambda k: k[2])
        if not textzeilen:
            return []
        hoehen = sorted(k[3] for k in textzeilen)
        h = hoehen[len(hoehen) // 2]
        abstaende = [b[2] - a[2] for a, b in zip(textzeilen, textzeilen[1:]) if b[2] - a[2] > 0.5 * h]
        zeilenabstand = min(abstaende) if abstaende and min(abstaende) < 1.3 * h else 0.8 * h
        neu = []
        H, W = bild.shape[0], bild.shape[1]
        for a, b in zip(textzeilen, textzeilen[1:]):
            # nur Zeilen, die übereinander stehen (gleicher Textblock)
            if a[1] > b[1] + b[4] or b[1] > a[1] + a[4]:
                continue
            fehlend = round((b[2] - a[2]) / zeilenabstand) - 1
            if fehlend < 1 or fehlend > 3:
                continue
            links = max(0, int(min(a[1], b[1]) - 0.05 * h))
            rechts = min(W, int(max(a[1] + a[4], b[1] + b[4]) * 1.0 + 0.6 * max(a[4], b[4])))
            for n in range(1, fehlend + 1):
                mitte = a[2] + a[3] / 2 + n * (b[2] - a[2]) / (fehlend + 1)
                y1, y2 = int(max(0, mitte - h / 2)), int(min(H, mitte + h / 2))
                if y2 - y1 < 10 or rechts - links < 20:
                    continue
                try:
                    r = self.engine(bild[y1:y2, links:rechts], use_det=False, use_cls=False, use_rec=True)
                    tt = getattr(r, "txts", None)
                    ss = getattr(r, "scores", None)
                except Exception as e:
                    log("Lücke lesen fehlgeschlagen:", e)
                    continue
                text = " ".join(tt).strip() if tt else ""
                if text and ss and min(ss) >= 0.6 and len(text) >= 3:
                    log("Übersehene Zeile gefunden:", text)
                    neu.append([text, float(links), float(y1), float(y2 - y1), float(rechts - links), min(ss)])

        # direkt unter der letzten und über der ersten Zeile nachsehen (z. B. wenn ein Pfeil die Zeile stört)
        for nachbar, richtung in ((textzeilen[-1], 1), (textzeilen[0], -1)):
            mitte = nachbar[2] + nachbar[3] / 2 + richtung * zeilenabstand
            y1, y2 = int(max(0, mitte - h / 2)), int(min(H, mitte + h / 2))
            links = max(0, int(nachbar[1] - 0.05 * h))
            rechts = min(W, int(nachbar[1] + nachbar[4] * 1.6))
            if y2 - y1 < 10 or rechts - links < 20:
                continue
            # nicht dort, wo schon eine erkannte Zeile liegt
            if any(abs((k[2] + k[3] / 2) - mitte) < 0.5 * h for k in kaesten):
                continue
            try:
                r = self.engine(bild[y1:y2, links:rechts], use_det=False, use_cls=False, use_rec=True)
                tt = getattr(r, "txts", None)
                ss = getattr(r, "scores", None)
            except Exception as e:
                log("Rand lesen fehlgeschlagen:", e)
                continue
            text = " ".join(tt).strip() if tt else ""
            if not text or not ss or min(ss) < 0.8:
                continue
            n, anteil = zeilen_guete(text)
            if n >= 2 and anteil >= 0.8:
                log("Zeile am Rand gefunden:", text)
                neu.append([text, float(links), float(y1), float(y2 - y1), float(rechts - links), min(ss)])
        return neu

    def lesen(self, img, sprache):
        return " ".join(z[0] for z in self.zeilen(img, sprache))


def _ocr_hilfsprozess(conn, gpu=False):
    """Läuft in einem eigenen Prozess: hängt die Texterkennung, kann man ihn einfach beenden."""
    try:
        m = RapidOCRMotor(gpu)
        conn.send(("bereit", "gpu" if "DmlExecutionProvider" in getattr(m, "geraete", []) else "cpu"))
    except Exception as e:
        conn.send(("fehler", str(e)))
        return
    while True:
        try:
            auftrag = conn.recv()
        except (EOFError, OSError):
            return   # Hauptprogramm ist weg (z. B. Neustart) -> leise beenden
        if auftrag is None:
            return
        groesse, daten, sprache = auftrag
        try:
            antwort = ("ok", m.zeilen(Image.frombytes("RGB", groesse, daten), sprache))
        except Exception as e:
            antwort = ("fehler", str(e))
        try:
            conn.send(antwort)
        except (EOFError, OSError):
            return


class OCRProzess:
    """RapidOCR in einem Hilfsprozess mit Zeitlimit."""
    art = "rapid"
    name = "RapidOCR (lokal)"
    ZEITLIMIT = 10

    def __init__(self, gpu=False):
        self.gpu_gewuenscht = gpu
        self.geraet = "cpu"
        self._starten()

    def _starten(self):
        try:
            self._starten_mit(self.gpu_gewuenscht)
        except Exception as e:
            if not self.gpu_gewuenscht:
                raise
            log("Grafikkarte geht nicht, nehme Prozessor:", e)
            self._starten_mit(False)

    def _starten_mit(self, gpu):
        self.conn, kind = _mp.Pipe()
        self.p = _mp.Process(target=_ocr_hilfsprozess, args=(kind, gpu), daemon=True)
        self.p.start()
        if not self.conn.poll(180):  # beim allerersten Mal wird evtl. das Modell geladen
            self.p.kill()
            raise RuntimeError("Hilfsprozess startet nicht")
        status, wert = self.conn.recv()
        if status != "bereit":
            self.p.kill()
            raise RuntimeError(wert)
        self.geraet = wert or "cpu"
        if gpu and self.geraet != "gpu":
            log("Grafikkarte nicht verfügbar – Texterkennung läuft auf dem Prozessor")

    def beenden(self):
        try:
            self.conn.send(None)
        except Exception:
            pass
        try:
            self.p.join(3)
        except Exception:
            pass
        try:
            if self.p.is_alive():
                self.p.kill()
                self.p.join(3)
        except Exception:
            pass

    def neu_starten(self, gpu):
        """Hilfsprozess mit anderem Gerät neu starten (Aufrufer hält die OCR-Sperre)."""
        self.beenden()
        self.gpu_gewuenscht = gpu
        self._starten()

    aufrufe = 0
    letzte_anzahl = None
    letzter_status = ""

    zeit_summe = 0.0     # für die Leistungsmessung

    def zeilen(self, img, sprache):
        img = img.convert("RGB")
        OCRProzess.aufrufe += 1
        t0 = time.time()
        try:
            self.conn.send((img.size, img.tobytes(), sprache))
            if self.conn.poll(self.ZEITLIMIT):
                status, wert = self.conn.recv()
                OCRProzess.zeit_summe += time.time() - t0
                OCRProzess.letzter_status = status
                if status == "ok":
                    OCRProzess.letzte_anzahl = len(wert)
                    return wert
                log("Texterkennung-Fehler im Hilfsprozess:", wert)
                return []
            log(f"Texterkennung hing länger als {self.ZEITLIMIT} s – Hilfsprozess wird neu gestartet")
        except Exception as e:
            log("Hilfsprozess-Problem:", e)
        try:
            self.p.kill()
        except Exception:
            pass
        try:
            self._starten()
        except Exception as e:
            log("Hilfsprozess-Neustart fehlgeschlagen:", e)
        return []

    def lesen(self, img, sprache):
        return " ".join(z[0] for z in self.zeilen(img, sprache))


def directml_installiert():
    """Ist das Grafikkarten-Paket da? (ohne onnxruntime im Hauptprogramm zu laden)"""
    try:
        from importlib import metadata
        metadata.version("onnxruntime-directml")
        return True
    except Exception:
        return False


def gpu_gewuenscht(einst):
    """Soll die Texterkennung gerade auf der Grafikkarte laufen?
    Automatisch: Grafikkarte, nur in VR den Prozessor (die Brille braucht die Grafikkarte)."""
    wahl = einst.d.get("rechner", "auto")
    if wahl == "cpu" or not directml_installiert():
        return False
    if wahl == "gpu":
        return True
    return einst.d.get("modus") != "vr"


def geraet_name(g):
    return "Grafikkarte" if g == "gpu" else "Prozessor"


def ocr_erstellen(gpu=False):
    try:
        m = OCRProzess(gpu)
        log("Texterkennung: RapidOCR (lokal, eigener Prozess) auf", geraet_name(m.geraet))
        return m
    except Exception as e:
        log("RapidOCR nicht verfügbar, nehme Windows-Texterkennung:", e)
    m = OCR()
    log("Texterkennung: Windows")
    return m


class OCR:
    art = "windows"
    name = "Windows-Texterkennung"

    def __init__(self):
        import winocr
        self._w = winocr

    def lesen(self, img, sprache):
        res = self._w.recognize_pil_sync(img, lang="de" if sprache == "auto" else sprache)
        if isinstance(res, dict):
            return res.get("text", "")
        return getattr(res, "text", "")

    def zeilen(self, img, sprache):
        """Liste von (text, x, y, hoehe) je erkannter Zeile."""
        res = self._w.recognize_pil_sync(img, lang="de" if sprache == "auto" else sprache)
        if not isinstance(res, dict):
            return [(z, 0, i * 10, 10, 0) for i, z in enumerate(getattr(res, "text", "").splitlines())]
        ergebnis = []
        for i, z in enumerate(res.get("lines") or []):
            text = z.get("text", "")
            rechtecke = [w.get("bounding_rect") or {} for w in z.get("words") or []]
            if rechtecke and all("y" in r for r in rechtecke):
                x = min(r.get("x", 0) for r in rechtecke)
                y = min(r["y"] for r in rechtecke)
                h = max(r.get("height", 10) for r in rechtecke)
                b = max(r.get("x", 0) + r.get("width", 0) for r in rechtecke) - x
            else:
                x, y, h, b = 0, i * 10, 10, 0
            ergebnis.append((text, x, y, h, b))
        if not ergebnis and res.get("text"):
            ergebnis = [(z, 0, i * 10, 10, 0) for i, z in enumerate(res["text"].splitlines())]
        return ergebnis


def bereinigen(text):
    text = text.replace("\n", " ")
    text = re.sub(r"[|_~^`<>{}\[\]\\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[▼►▶•·*#]+$", "", text).strip()
    # Pixel-Schriften: auseinandergerissene Buchstaben wieder zusammenfügen ("erhel l t" -> "erhellt")
    text = re.sub(r"\b(\w{2,})((?: [a-zäöüßI](?=[\s.,!?…]|$))+)",
                  lambda m: m.group(1) + m.group(2).replace(" ", ""), text)
    # "ß" am Wortanfang ist ein "B" ("ßursche" -> "Bursche")
    text = re.sub(r"\bß(?=[a-zäöü])", "B", text)
    # "gefährl icher", "Natürl ich" -> zusammen
    text = re.sub(r"(?<=[bcdfghkmnprstvwzäöüß])l (?=ich)", "l", text)
    text = re.sub(r"(?<=[a-zäöüß]{3})li (?=ch[a-zäöü]*\b)", "li", text)
    # großes I mitten im kleingeschriebenen Wort ist fast immer ein kleines l
    text = re.sub(r"(?<=[a-zäöüß])I(?=[a-zäöüß]|\b)", "l", text)
    # Anführungszeichen mitten vor einem kleinen Wortanfang ist meist ein falsch erkanntes "H"
    if text.count('"') % 2 == 1:  # nur wenn das Anführungszeichen kein Gegenstück hat
        text = re.sub(r'(?<=\s)"(?=[a-zäöü]{3,})', "H", text)
    return text


def echte_woerter(text):
    anzahl = 0
    for w in re.findall(r"\w{2,}", text.lower()):
        if any(w in m or (len(w) >= 4 and m in w) for m in MENUE_WOERTER):
            continue
        anzahl += 1
    return anzahl


VOKALE = set("aeiouyäöüAEIOUYÄÖÜ")


def zeilen_guete(text):
    """(gute Wörter, Anteil sinnvoller Zeichen) – Gekritzel/Grafik ergibt schlechte Werte."""
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß]+", text)
    gut = [w for w in woerter if len(w) >= 2 and any(c in VOKALE for c in w)
           and not re.search(r"[a-zäöüß][A-ZÄÖÜ]", w)]          # kein wildes Groß/klein mitten im Wort
    zeichen = len(re.sub(r"\s", "", text)) or 1
    sinnvoll = sum(len(w) for w in gut) + len(re.findall(r"[.,!?'\"…:;-]", text))
    return len(gut), sinnvoll / zeichen


_WB = {}


def woerterbuch(sprache):
    """Deutsches/englisches Wörterbuch (pyspellchecker). None, wenn nicht installiert."""
    if sprache not in _WB:
        try:
            from spellchecker import SpellChecker
            _WB[sprache] = SpellChecker(language=sprache, distance=1)
        except Exception as e:
            log("Wörterbuch nicht verfügbar:", e)
            _WB[sprache] = None
    return _WB[sprache]


_ZIFFERN = str.maketrans({"3": "ß", "0": "o", "1": "l", "5": "s", "8": "B"})
_KORREKTUR_CACHE = {}


_VOR_I = {"but", "can", "and", "so", "if", "when", "as", "that", "what", "where", "because", "cause", "could",
          "should", "would", "will", "do", "did", "have", "think", "know", "guess", "hope", "wish", "then",
          "maybe", "now", "yes", "yeah", "oh", "well", "sure", "how", "why", "before", "after", "until", "ok",
          "okay", "sorry", "or", "than", "while", "since", "unless", "though", "am", "was", "who", "say", "said",
          "hey", "hi", "um", "uh", "ah", "no", "please", "thanks", "said", "where", "whatever", "until"}
_VOR_A = {"for", "just", "is", "was", "as", "if", "in", "such", "what", "quite", "with", "have", "had", "got",
          "need", "not", "of", "at", "be", "into", "like", "about", "take", "make", "give", "get", "half",
          "once", "from", "by", "on", "to", "and", "but", "or", "had", "need", "only", "rather", "after"}
_A_IMMER = {"fora", "justa", "ifa", "isa", "wasa", "asa", "ofa", "ata", "witha", "abouta", "liketa"}


def englisch_ausbessern(text, wb=None):
    """Typische Fehler der Texterkennung in englischem Text:
    "l'm" -> "I'm", "I'Il" -> "I'll", einzelnes "l" -> "I", "Butl" -> "But I", "Justa" -> "Just a"."""
    if not text:
        return text
    text = re.sub(r"(?<![\w'])[lI1|]['’](m|ve|d|ll|Il|lI|II|LL)\b",
                  lambda m: "I'" + ("ll" if m.group(1).lower() in ("ll", "il", "li") else m.group(1)), text)
    text = re.sub(r"(?<![\w'’.])l(?![\w'’])", "I", text)

    def wort(m):
        w = m.group(0)
        k = w.lower()
        if wb is not None and k in wb and k not in _A_IMMER:
            return w
        if len(k) > 2 and k.endswith("l") and k[:-1] in _VOR_I:
            return w[:-1] + " I"
        if len(k) > 2 and k.endswith("a") and k[:-1] in _VOR_A and (wb is None or k not in wb or k in _A_IMMER):
            return w[:-1] + " a"
        return w
    return re.sub(r"[A-Za-z]+", wort, text)


def mit_woerterbuch(text, sprache):
    """Typische Erkennungsfehler mit dem Wörterbuch ausbessern:
    - auseinandergerissene Wörter zusammenfügen ("verni chtet" -> "vernichtet")
    - Ziffern in Wörtern ("grd3" -> "groß")
    - kleingeschriebene Wörter mit 1 falschen Buchstaben ("purde" -> "wurde")
    Großgeschriebene Wörter bleiben (könnten Namen sein)."""
    if sprache == "auto":
        sprache = sprache_genau(text or "") or "de"
    wb = woerterbuch(sprache)
    if sprache == "en":
        text = englisch_ausbessern(text, wb)
    if not wb or not text:
        return text
    teile = re.split(r"(\s+)", text)
    woerter = [t for t in teile if t and not t.isspace()]

    def kern(w):
        m = re.match(r"^([^\wÄÖÜäöüß]*)([\wÄÖÜäöüß'-]*?)([^\wÄÖÜäöüß]*)$", w)
        return m.groups() if m else ("", w, "")

    def bekannt(w):
        return bool(w) and w.lower() in wb

    # 1) zusammenfügen
    neu = []
    i = 0
    while i < len(woerter):
        if i + 1 < len(woerter):
            v1, k1, n1 = kern(woerter[i])
            v2, k2, n2 = kern(woerter[i + 1])
            if k1 and k2 and not n1 and not v2 and k2[:1].islower() and \
                    (not bekannt(k1) or not bekannt(k2)) and bekannt(k1 + k2):
                neu.append(v1 + k1 + k2 + n2)
                i += 2
                continue
        neu.append(woerter[i])
        i += 1

    # 2) einzelne Wörter ausbessern
    ergebnis = []
    for w in neu:
        v, k, n = kern(w)
        if k and re.search(r"\d", k) and re.search(r"[A-Za-zÄÖÜäöüß]", k):
            k2 = k.translate(_ZIFFERN)
            k = k2 if bekannt(k2) else (_korrigieren(wb, k2) or k)
        elif k and len(k) >= 3 and k[:1].islower() and k.isalpha() and not bekannt(k):
            k = _korrigieren(wb, k) or k
        ergebnis.append(v + k + n)
    return " ".join(ergebnis)


_WB2 = {}


def _korrigieren(wb, wort):
    schluessel = wort.lower()
    if schluessel not in _KORREKTUR_CACHE:
        try:
            r = wb.correction(schluessel)
            # lange Wörter: auch 2 falsche Buchstaben erlauben ("persmlich" -> "persönlich")
            if not r and len(schluessel) >= 7:
                from spellchecker import SpellChecker
                sprache = next((k for k, v in _WB.items() if v is wb), "de")
                if sprache not in _WB2:
                    _WB2[sprache] = SpellChecker(language=sprache, distance=2)
                r = _WB2[sprache].correction(schluessel)
            _KORREKTUR_CACHE[schluessel] = r
        except Exception:
            _KORREKTUR_CACHE[schluessel] = None
    r = _KORREKTUR_CACHE[schluessel]
    if not r:
        return None
    return r[:1].upper() + r[1:] if wort[:1].isupper() else r


def textguete(text):
    n, anteil = zeilen_guete(text)
    return n * anteil


def _gute_zeilen(roh):
    alle = []
    for text, x, y, h, b in roh:
        text = bereinigen(text)
        if not text:
            continue
        n, anteil = zeilen_guete(text)
        alle.append({"text": text, "x": x, "y": y, "h": max(h, 1), "b": b, "n": n, "anteil": anteil})
    gute = [z for z in alle if z["n"] >= 2 and z["anteil"] >= 0.7]
    # kurze Zeilen (z. B. "wird...") gelten, wenn sie direkt an einer guten Zeile hängen
    for z in alle:
        if z in gute or z["n"] < 1 or z["anteil"] < 0.7:
            continue
        if any(abs(z["y"] - g["y"]) <= 2.2 * g["h"] and abs(z["x"] - g["x"]) < 20 * g["h"] for g in gute):
            gute.append(z)
    return gute


def _in_lesereihenfolge(zeilen):
    """Zeilen zu Reihen gruppieren (gleiche Höhe) und links nach rechts, oben nach unten ordnen."""
    reihen = []  # [mitte, hoehe, [zeilen]]
    for z in sorted(zeilen, key=lambda z: z["y"] + z["h"] / 2):
        mitte = z["y"] + z["h"] / 2
        for r in reihen:
            if abs(mitte - r[0]) < 0.5 * max(z["h"], r[1]):
                r[2].append(z)
                break
        else:
            reihen.append([mitte, z["h"], [z]])
    reihen.sort(key=lambda r: r[0])
    teile = []
    for _, _, r in reihen:
        teile.extend(z["text"] for z in sorted(r, key=lambda z: z["x"]))
    return " ".join(teile)


_letztes_kontrollbild = 0


def otsu(g):
    """Schwellwert, der helle und dunkle Bildteile am besten trennt."""
    hist = g.histogram()
    gesamt = sum(hist)
    summe = sum(i * h for i, h in enumerate(hist))
    sb, wb, beste, schwelle = 0, 0, -1, 128
    for t in range(256):
        wb += hist[t]
        if wb == 0:
            continue
        wf = gesamt - wb
        if wf == 0:
            break
        sb += t * hist[t]
        mb, mf = sb / wb, (summe - sb) / wf
        zwischen = wb * wf * (mb - mf) ** 2
        if zwischen > beste:
            beste, schwelle = zwischen, t
    return schwelle


def woerter_kombinieren(texte, sprache):
    """Nimmt die beste Fassung als Grundlage und ersetzt darin unbekannte Wörter durch
    ähnliche, bekannte Wörter aus den anderen Fassungen."""
    texte = [t for t in texte if t]
    if not texte:
        return ""
    texte.sort(key=lambda t: punkte_fuer(t, sprache), reverse=True)
    basis = texte[0]
    wb = woerterbuch(sprache)
    if not wb or len(texte) == 1:
        return basis

    def sauber(w):
        return re.sub(r"[^A-Za-zÄÖÜäöüß]", "", w)

    andere = []
    for t in texte[1:]:
        ws = t.split()
        andere.append([(i / max(1, len(ws) - 1), sauber(w)) for i, w in enumerate(ws)])
    basis_w = basis.split()
    ergebnis = []
    for i, w in enumerate(basis_w):
        k = sauber(w)
        if len(k) >= 2 and k.lower() not in wb:
            pos = i / max(1, len(basis_w) - 1)
            bester, beste_aeh = None, 0.55
            for liste in andere:
                for p2, k2 in liste:
                    if abs(p2 - pos) > 0.25 or len(k2) < 2 or k2.lower() not in wb:
                        continue
                    a = difflib.SequenceMatcher(None, k.lower(), k2.lower()).ratio()
                    if a > beste_aeh:
                        bester, beste_aeh = k2, a
            # kein bekanntes Wort gefunden: wenigstens eine "saubere" Fassung ohne Störzeichen nehmen
            kaputt = bool(re.search(r"[^A-Za-zÄÖÜäöüß.,!?…:;'\"]", w.strip(".,!?…:;'\"()"))) or "-" in w.strip("-")
            if not bester and kaputt:
                for t in texte[1:]:
                    for w2 in t.split():
                        k2 = w2.strip(".,!?…:;'\"()")
                        if not k2.isalpha() or len(k2) < 3:
                            continue
                        a = difflib.SequenceMatcher(None, k.lower(), k2.lower()).ratio()
                        if a > beste_aeh:
                            bester, beste_aeh = k2, a
            if bester:
                if k[:1].isupper():
                    bester = bester[:1].upper() + bester[1:]
                m = re.match(r"^([^A-Za-zÄÖÜäöüß]*).*?([^A-Za-zÄÖÜäöüß]*)$", w)
                w = (m.group(1) if m else "") + bester + (m.group(2) if m else "")
        ergebnis.append(w)
    return " ".join(ergebnis)


def punkte_fuer(text, sprache):
    """Wie sehr sieht das nach echtem Text aus? Mit Wörterbuch: bekannte Wörter zählen voll."""
    wb = woerterbuch(sprache)
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", text)
    if wb:
        return sum(1.0 if w.lower() in wb else 0.3 for w in woerter)
    n, anteil = zeilen_guete(text)
    return n * anteil


def text_finden(ocr, img, schrift, sprache):
    """Sucht Text im Bild – auch in großen Bereichen mit Bild-Hintergrund.
    1) Liest das (evtl. verkleinerte) Bild auf mehrere Arten und nimmt die beste.
    2) Schneidet den gefundenen Textblock aus, vergrößert ihn, macht ihn schwarz/weiß
       und liest ihn nochmal genauer. Das bessere Ergebnis gewinnt."""
    if getattr(ocr, "art", "") == "rapid":
        if img.width > 2600:
            img = img.resize((2600, max(1, int(img.height * 2600 / img.width))), Image.LANCZOS)
        if schrift in ("hell", "dunkel"):
            img = bild_vorbereiten(img, schrift)
        zeilen = _gute_zeilen(ocr.zeilen(img, sprache))
        text = _in_lesereihenfolge(zeilen) if zeilen else ""
        text = re.sub(r"(\w)[-–] (?!und\b|oder\b|bis\b|sowie\b)(?=[a-zäöüß])", r"\1", text)
        return mit_woerterbuch(text, sprache)
    original = img
    f = 1.0
    if img.width > 1800:  # großer Bereich: verkleinern, damit es schnell bleibt
        f = 1800 / img.width
        img = img.resize((1800, max(1, int(img.height * f))), Image.LANCZOS)
    arten = ["normal", "hell"] if schrift == "auto" else [schrift]
    beste_zeilen, beste_art, beste_punkte = [], None, 0
    for art in arten:
        zeilen = _gute_zeilen(ocr.zeilen(bild_vorbereiten(img, art), sprache))
        punkte = sum(z["n"] * z["anteil"] for z in zeilen)
        if punkte > beste_punkte:
            beste_zeilen, beste_art, beste_punkte = zeilen, art, punkte
    if not beste_zeilen:
        return ""
    text = _in_lesereihenfolge(beste_zeilen)

    # ---- 2. Durchgang: Textblock vergrößert und kontrastreich nochmal lesen
    try:
        h = max(z["h"] for z in beste_zeilen) / f
        x1 = min(z["x"] for z in beste_zeilen) / f - h
        y1 = min(z["y"] for z in beste_zeilen) / f - 0.7 * h
        x2 = max(z["x"] + (z["b"] or z["h"] * len(z["text"]) * 0.6) for z in beste_zeilen) / f + h
        y2 = max(z["y"] + z["h"] for z in beste_zeilen) / f + 0.7 * h
        box = (max(0, int(x1)), max(0, int(y1)), min(original.width, int(x2)), min(original.height, int(y2)))
        if box[2] - box[0] > 20 and box[3] - box[1] > 10:
            ausschnitt = original.crop(box)
            faktor = max(1.0, min(3.0, 48 / max(h, 1)))
            if faktor > 1.05:
                ausschnitt = ausschnitt.resize((int(ausschnitt.width * faktor), int(ausschnitt.height * faktor)),
                                               Image.LANCZOS)
            if beste_art == "hell":
                sw = bild_vorbereiten(ausschnitt, "hell")
            else:
                g = ImageOps.autocontrast(ImageOps.grayscale(ausschnitt))
                t = otsu(g)
                sw = g.point(lambda v: 255 if v > t else 0)
                hist = sw.histogram()
                if hist[0] > hist[255]:  # mehr Schwarz als Weiß -> Schrift ist hell -> umkehren
                    sw = ImageOps.invert(sw)
                sw = sw.convert("RGB")
            global _letztes_kontrollbild
            if fehlersuche("bilder") and time.time() - _letztes_kontrollbild > 3:
                _letztes_kontrollbild = time.time()
                try:
                    sw.save(os.path.join(PROTOKOLL, "kontrollbild.png"))
                except Exception:
                    pass
            # mehrere Varianten lesen – jede macht andere Fehler – und Wort für Wort das Beste nehmen
            from PIL import ImageFilter
            varianten = [sw, sw.filter(ImageFilter.MaxFilter(3)), sw.filter(ImageFilter.MinFilter(3)),
                         sw.resize((int(sw.width * 1.5), int(sw.height * 1.5)), Image.LANCZOS)]
            texte = []
            for v in varianten:
                z2 = _gute_zeilen(ocr.zeilen(v, sprache))
                if z2:
                    texte.append(_in_lesereihenfolge(z2))
            texte.append(text)
            text = woerter_kombinieren(texte, sprache)
    except Exception as e:
        log("Zweiter Durchgang fehlgeschlagen:", e)

    text = re.sub(r"(\w)[-–] (?!und\b|oder\b|bis\b|sowie\b)(?=[a-zäöüß])", r"\1", text)  # "einge- schlossen"
    return mit_woerterbuch(text, sprache)


FUELLWOERTER = set("""der die das den dem des ein eine einen einem einer eines und oder aber ich du er sie es wir ihr
mich dich sich uns euch mir dir ihm ihn ihnen ist bin bist sind war hat habe hast haben wird werden nicht noch nur
schon auch mit von zu zum zur in im an am auf aus bei für fur nach vor über unter um so wie was wer wo da dann
the and you your this that with have are was not for""".split())


def inhaltswoerter(text):
    return {w for w in re.findall(r"[a-zäöüß]{3,}", text.lower()) if w not in FUELLWOERTER}


def neuer_teil(alt, neu):
    """Die Wörter von 'neu', die hinter dem schon gelesenen 'alt' dazugekommen sind."""
    a, n = alt.split(), neu.split()
    sm = difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in n])
    letzte = 0
    for block in sm.get_matching_blocks():
        if block.size:
            letzte = block.b + block.size
    return " ".join(n[letzte:])


def aehnlich(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


SCHRIFT_ARTEN = {"auto": "Schrift: auto", "hell": "Schrift: hell", "dunkel": "Schrift: dunkel"}


def bild_vorbereiten(img, schrift="auto"):
    """schrift: "auto"   = Kontrast verbessern, bei dunklem Grund umkehren
               "hell"   = nur weiße/hellgraue Schrift behalten (Rest weg) – gut bei Text über bunten Bildern
               "dunkel" = nur dunkle Schrift behalten"""
    if schrift in ("hell", "dunkel"):
        r, g_, b = img.split()
        mn = ImageChops.darker(ImageChops.darker(r, g_), b)
        mx = ImageChops.lighter(ImageChops.lighter(r, g_), b)
        if schrift == "hell":
            hell_genug = mn.point(lambda v: 255 if v >= 140 else 0)
            farblos = ImageChops.subtract(mx, mn).point(lambda v: 255 if v <= 35 else 0)
            maske = ImageChops.multiply(hell_genug, farblos)
        else:
            maske = mx.point(lambda v: 255 if v <= 90 else 0)
        g = ImageOps.invert(maske)  # schwarze Schrift auf weiß
        if g.height < 400:
            g = g.resize((g.width * VERGROESSERUNG, g.height * VERGROESSERUNG), Image.LANCZOS)
        return g.convert("RGB")
    g = ImageOps.grayscale(img)
    if g.height < 400:
        g = g.resize((g.width * VERGROESSERUNG, g.height * VERGROESSERUNG), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    if schrift == "normal":
        return g.convert("RGB")
    hist = g.histogram()
    mittel = sum(i * n for i, n in enumerate(hist)) / max(1, sum(hist))
    if mittel < 128:  # helle Schrift auf dunklem Grund -> umkehren
        g = ImageOps.invert(g)
    return g.convert("RGB")


# ================================================================ Sprachausgabe
# ================================================================ Töne
TOENE = {  # Name -> Folge von (Frequenz in Hz, Dauer in s)
    "pause": [(880, 0.09), (587, 0.14)],          # nach unten = aus
    "weiter": [(587, 0.09), (880, 0.14)],         # nach oben = an
    "gespeichert": [(660, 0.07), (880, 0.07), (1175, 0.12)],
}
TON_TEXTE = {
    "pause": ("Vorlesen pausiert.", "⏸  Pausiert"),
    "weiter": ("Vorlesen geht weiter.", "▶  Weiter"),
    "gespeichert": ("Textfeld gespeichert.", "✓  Textfeld gespeichert"),
}


def _ton_datei(name, ordner):
    """WAV-Datei für einen Ton. Eine eigene Datei in eigene_toene/<name>.wav hat Vorrang."""
    eigene = os.path.join(EIGENE_TOENE, f"{name}.wav")
    if os.path.isfile(eigene):
        return eigene
    return _ton_datei_eingebaut(name, ordner)


def _ton_erzeugen(name, pfad):
    import math
    import struct
    import wave
    rate = 44100
    proben = []
    for freq, dauer in TOENE[name]:
        n = int(rate * dauer)
        for i in range(n):
            huelle = min(1.0, i / (rate * 0.005), (n - i) / (rate * 0.03))  # weich ein-/ausblenden
            wert = math.sin(2 * math.pi * freq * i / rate) * 0.35 * huelle
            wert += math.sin(4 * math.pi * freq * i / rate) * 0.08 * huelle  # etwas wärmer
            proben.append(int(max(-1, min(1, wert)) * 32767))
        proben.extend([0] * int(rate * 0.015))
    with wave.open(pfad, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(proben)}h", *proben))


LIESMICH_TOENE = 'EIGENE TÖNE FÜR DEN LESEHELFER\n==============================\n\nHier kannst du die eingebauten Töne durch eigene ersetzen.\n\nWELCHE TÖNE GIBT ES?\n  pause.wav        spielt, wenn das Vorlesen pausiert wird\n  weiter.wav       spielt, wenn das Vorlesen weitergeht\n  gespeichert.wav  spielt, wenn ein Textfeld neu markiert und gespeichert wurde\n\nSO GEHT\'S\n1. Such dir einen Ton aus, z. B. ein kurzes "Blip" oder "Pling".\n2. Er muss eine WAV-Datei sein (Endung .wav).\n   MP3 oder andere Formate funktionieren nicht - siehe "Umwandeln" unten.\n3. Benenne die Datei genau so wie oben, z. B. "pause.wav".\n   Achte darauf, dass Windows nicht heimlich "pause.wav.wav" daraus macht\n   (im Explorer unter "Anzeigen" -> "Dateinamenerweiterungen" einschalten).\n4. Lege die Datei direkt in DIESEN Ordner (eigene_toene).\n5. Fertig. Ein Neustart ist nicht nötig - beim nächsten Pause/Weiter\n   wird schon dein Ton gespielt.\n\nZURÜCK ZUM EINGEBAUTEN TON\nEinfach deine Datei aus diesem Ordner löschen oder umbenennen.\n\nVORLAGEN\nIm Unterordner "vorlagen" liegen die eingebauten Töne zum Anhören\noder als Ausgangspunkt. Die werden NICHT benutzt - nur Dateien direkt\nin diesem Ordner zählen.\n\nTIPPS\n- Kurz halten: am besten unter 1 Sekunde, sonst wirkt es träge.\n- Nicht zu laut: der Ton kommt über dieselben Lautsprecher wie das Spiel.\n- Damit man Pause und Weiter unterscheiden kann, eignen sich zwei\n  verschiedene Töne (z. B. einer tief, einer hoch).\n\nUMWANDELN IN WAV\n- Mit dem kostenlosen Programm "Audacity": Datei öffnen, dann\n  "Datei -> Exportieren -> Als WAV exportieren".\n- Oder mit einem Online-Umwandler (Suche nach "MP3 in WAV umwandeln").\n\nOb dein Ton erkannt wurde, sieht man im Ordner "protokoll"\nin der neuesten Datei - dort steht bei Problemen eine Meldung.\n'


def toene_ordner_anlegen():
    """Ordner für eigene Töne mit Beschreibung und Vorlagen anlegen (falls noch nicht da)."""
    try:
        vorlagen = os.path.join(EIGENE_TOENE, "vorlagen")
        os.makedirs(vorlagen, exist_ok=True)
        liesmich = os.path.join(EIGENE_TOENE, "LIESMICH.txt")
        alt_liesmich = False
        if os.path.isfile(liesmich):
            with open(liesmich, encoding="utf-8", errors="replace") as f:
                alt_liesmich = "PS2-VORLESER" in f.read()
        if alt_liesmich or not os.path.isfile(liesmich):
            with open(liesmich, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(LIESMICH_TOENE)
        for name in TOENE:
            ziel = os.path.join(vorlagen, f"{name}.wav")
            if not os.path.isfile(ziel):
                _ton_erzeugen(name, ziel)
    except Exception as e:
        log("Ordner für eigene Töne nicht angelegt:", e)


def _ton_datei_eingebaut(name, ordner):
    pfad = os.path.join(ordner, f"ton_{name}.wav")
    if not os.path.isfile(pfad):
        _ton_erzeugen(name, pfad)
    return pfad


def ton_spielen(name, ordner):
    import winsound
    datei = _ton_datei(name, ordner)
    try:
        winsound.PlaySound(datei, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        log(f"Ton {os.path.basename(datei)} lässt sich nicht abspielen ({e}) – ist es eine echte WAV-Datei? "
            "Nehme den eingebauten Ton.")
        try:
            winsound.PlaySound(_ton_datei_eingebaut(name, ordner),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass


def sprechsprache(profil):
    """Grundsprache für Ansagen und Stimmen-Anzeige. Die Sprache der Spieltexte
    wird je Text automatisch erkannt (siehe Leser._ausgabe)."""
    return "de"


# ================================================================ Übersetzung (offline)
ARGOS_INDEX = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
MODELL_ORDNER = os.path.join(DATEN, "modelle")


class Uebersetzer:
    """Englisch -> Deutsch, komplett auf dem PC (CTranslate2 + Argos-Sprachmodell).
    Das Modell (~100 MB) wird beim ersten Einschalten einmalig heruntergeladen."""

    def __init__(self, melden):
        self.melden = melden
        self.bereit = False
        self.fehler = None
        self.lock = threading.Lock()
        self._gestartet = False

    def vorbereiten(self):
        if self._gestartet:
            return
        self._gestartet = True
        threading.Thread(target=self._laden, daemon=True).start()

    def _modell_pfad(self):
        if os.path.isdir(MODELL_ORDNER):
            for name in os.listdir(MODELL_ORDNER):
                p = os.path.join(MODELL_ORDNER, name)
                if name.startswith("translate-en_de") and os.path.isdir(p):
                    for unter in [p] + [os.path.join(p, u) for u in os.listdir(p)]:
                        if os.path.isdir(os.path.join(unter, "model")):
                            return unter
        return None

    def _herunterladen(self):
        import urllib.request
        import zipfile
        self.melden("hinweis", "Übersetzung wird vorbereitet – das Sprachmodell (ca. 100 MB) wird einmalig geladen …")
        kopf = {"User-Agent": "ArgosTranslate/1.11 (Lesehelfer)"}

        def holen(url, timeout=60):
            return urllib.request.urlopen(urllib.request.Request(url, headers=kopf), timeout=timeout)

        with holen(ARGOS_INDEX, 30) as r:
            index = json.loads(r.read().decode("utf-8"))
        eintrag = next(e for e in index if e.get("from_code") == "en" and e.get("to_code") == "de")
        os.makedirs(MODELL_ORDNER, exist_ok=True)
        ziel = os.path.join(MODELL_ORDNER, "en_de.argosmodel")
        letzter = None
        for link in eintrag.get("links", []):
            try:
                log("Lade Übersetzungsmodell:", link)
                with holen(link, 120) as r, open(ziel, "wb") as f:
                    while True:
                        stueck = r.read(1 << 20)
                        if not stueck:
                            break
                        f.write(stueck)
                break
            except Exception as e:
                letzter = e
                log("Download-Versuch fehlgeschlagen:", link, e)
        else:
            raise RuntimeError(f"Download fehlgeschlagen: {letzter}")
        ordner = os.path.join(MODELL_ORDNER, "translate-en_de")
        with zipfile.ZipFile(ziel) as z:
            z.extractall(ordner)
        os.remove(ziel)

    def _pakete_sicherstellen(self):
        try:
            import ctranslate2  # noqa: F401
            import sentencepiece  # noqa: F401
            return
        except ImportError:
            pass
        import subprocess
        self.melden("hinweis", "Übersetzung wird installiert – einen Moment …")
        python = sys.executable
        if python.lower().endswith("pythonw.exe"):
            python = python[:-len("pythonw.exe")] + "python.exe"
        log("Installiere ctranslate2 + sentencepiece …")
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        "ctranslate2", "sentencepiece"], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _laden(self):
        try:
            self._pakete_sicherstellen()
            import ctranslate2
            import sentencepiece
            pfad = self._modell_pfad()
            if not pfad:
                self._herunterladen()
                pfad = self._modell_pfad()
            if not pfad:
                raise RuntimeError("Modell nicht gefunden")
            sp_datei = os.path.join(pfad, "sentencepiece.model")
            self.sp = sentencepiece.SentencePieceProcessor(model_file=sp_datei)
            self.tr = ctranslate2.Translator(os.path.join(pfad, "model"), device="cpu",
                                             inter_threads=1, intra_threads=4)
            self.bereit = True
            log("Übersetzung bereit:", pfad)
            self.melden("hinweis", "")
            # Probelauf
            log("Übersetzung Test:", self.uebersetzen("Hello, how are you?"))
        except Exception as e:
            self.fehler = str(e)
            self._gestartet = False  # beim nächsten Einschalten erneut versuchen
            log("Übersetzung nicht verfügbar:", traceback.format_exc())
            self.melden("hinweis", "Übersetzung nicht verfügbar – es wird auf Englisch vorgelesen.")

    def uebersetzen(self, text):
        if not self.bereit or not text.strip():
            return None
        saetze = [x for x in re.split(r"(?<=[.!?…])\s+", text.strip()) if x]
        with self.lock:
            stuecke = [self.sp.encode(x, out_type=str) for x in saetze]
            ergebnisse = self.tr.translate_batch(stuecke, beam_size=2, max_decoding_length=256)
        aus = []
        for e in ergebnisse:
            aus.append(self.sp.decode(e.hypotheses[0]).replace("▁", " ").strip())
        return " ".join(aus)


class Sprecher:
    """Zwei Threads: einer erzeugt schon die nächste Sprachdatei, während der andere abspielt.
    Erst KI-Stimme (Internet), sonst Windows-Stimme."""

    MAX_WARTESCHLANGE = 3

    def __init__(self, einst, melden):
        self.einst = einst
        self.melden = melden            # melden(art, wert) -> an die Oberfläche
        self.q = queue.Queue()          # (generation, text)
        self.pq = queue.Queue()         # (generation, art, wert) zum Abspielen
        self.stopp = threading.Event()
        self.gen = 0
        self.nr = 0
        self.ki_ok = None
        self.ki_naechster_versuch = 0
        self.funktionierend = {}     # (sprache, w/m) -> Index der Stimme, die zuletzt ging
        import tempfile
        self.ordner = tempfile.mkdtemp(prefix="vorleser_")
        self.sapi = None
        self.spielt = False
        threading.Thread(target=self._erzeuger, daemon=True).start()
        threading.Thread(target=self._spieler, daemon=True).start()

    def stoppen(self):
        """Laufenden und wartenden Text sofort abbrechen."""
        self.gen += 1
        for qq in (self.q, self.pq):
            while not qq.empty():
                try:
                    qq.get_nowait()
                except queue.Empty:
                    break
        self.stopp.set()

    def beschaeftigt(self):
        return self.spielt or not self.q.empty() or not self.pq.empty()

    def sagen(self, text, unterbrechen=True, sprache=None):
        # Emojis und Platzhalter-Zeichen weg: die Online-Stimmen liefern sonst manchmal gar keinen Ton
        text = re.sub(r"\s+", " ", OHNE_SYMBOLE.sub(" ", text or "")).strip()
        if not text:
            return
        if unterbrechen:
            self.gen += 1
            for qq in (self.q, self.pq):
                while not qq.empty():
                    try:
                        qq.get_nowait()
                    except queue.Empty:
                        break
            self.stopp.set()
        elif self.q.qsize() + self.pq.qsize() >= self.MAX_WARTESCHLANGE:
            try:
                self.q.get_nowait()  # zu viel aufgestaut: ältesten wartenden Satz weglassen
            except queue.Empty:
                pass
        self.q.put((self.gen, text, sprache))

    def _kandidaten(self, sprache=None):
        schluessel = (sprache or sprechsprache(self.einst.profil), self.einst["stimme"])
        liste = KI_ERSATZ[schluessel]
        i = self.funktionierend.get(schluessel, 0)
        return schluessel, liste[i:] + liste[:i]

    def stimmname(self):
        schluessel, liste = self._kandidaten()
        return liste[0][1]

    # -------------------------------------------------------- Sprachdateien erzeugen
    def _erzeuger(self):
        while True:
            gen, text, sprache = self.q.get()
            if gen != self.gen:
                continue
            if self.einst["ki_stimmen"] and time.time() >= self.ki_naechster_versuch:
                try:
                    pfad = self._ki_datei(text, sprache)
                    self._ki_status(True)
                    self.pq.put((gen, "mp3", pfad))
                    continue
                except Exception as e:
                    log("KI-Stimme fehlgeschlagen:", e)
                    self._ki_status(False)
                    # kurz warten und beim nächsten Satz wieder versuchen (nicht eine ganze Minute)
                    self.ki_naechster_versuch = time.time() + 10
            self.pq.put((gen, "sapi", (text, sprache)))

    def _ki_status(self, ok):
        if ok != self.ki_ok:
            self.ki_ok = ok
            self.melden("ki", ok)

    def _ki_datei(self, text, sprache=None):
        import asyncio
        import edge_tts
        tempo = int(self.einst["tempo"])
        self.nr += 1
        pfad = os.path.join(self.ordner, f"satz{self.nr % 8}.mp3")
        schluessel, liste = self._kandidaten(sprache)
        letzter = None
        for stimme, name in liste:
            try:
                asyncio.run(edge_tts.Communicate(text, stimme, rate=f"{tempo:+d}%").save(pfad))
                if os.path.getsize(pfad) == 0:
                    raise RuntimeError("leere Sprachdatei")
                neu = KI_ERSATZ[schluessel].index((stimme, name))
                if self.funktionierend.get(schluessel, 0) != neu:
                    log(f"Benutze Ersatzstimme {name}")
                    self.funktionierend[schluessel] = neu
                    self.melden("stimme_geaendert", None)
                return pfad
            except Exception as e:
                letzter = e
                meldung = str(e).lower()
                # nur wenn die Stimme selbst nicht will -> nächste probieren; Internetfehler sofort melden
                if "no audio" not in meldung and "leere sprachdatei" not in meldung:
                    raise
                log(f"Stimme {name} liefert keinen Ton – probiere die nächste")
        raise RuntimeError(f"keine Stimme lieferte Ton: {letzter}")

    # -------------------------------------------------------- Abspielen
    def _spieler(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        while True:
            gen, art, wert = self.pq.get()
            if gen != self.gen:
                continue
            self.stopp.clear()
            self.spielt = True
            try:
                if art == "mp3":
                    self._mp3(wert)
                else:
                    self._windows(*wert)
            except Exception as e:
                log("Abspielen fehlgeschlagen:", e)
            finally:
                self.spielt = False

    def _mp3(self, pfad):
        mci = ctypes.windll.winmm.mciSendStringW
        mci("close vorleser", None, 0, 0)
        if mci(f'open "{pfad}" type mpegvideo alias vorleser', None, 0, 0) != 0:
            raise RuntimeError("MP3 konnte nicht geöffnet werden")
        mci(f"setaudio vorleser volume to {int(self.einst['lautstaerke']) * 10}", None, 0, 0)
        mci("play vorleser", None, 0, 0)
        puffer = ctypes.create_unicode_buffer(64)
        while not self.stopp.is_set():
            mci("status vorleser mode", puffer, 64, 0)
            if puffer.value != "playing":
                break
            time.sleep(0.05)
        mci("close vorleser", None, 0, 0)

    def _windows(self, text, sprache=None):
        if self.sapi is None:
            import win32com.client
            self.sapi = win32com.client.Dispatch("SAPI.SpVoice")
        sprache = sprache or sprechsprache(self.einst.profil)
        suche = ("german", "deutsch") if sprache == "de" else ("english",)
        stimmen = self.sapi.GetVoices()
        for i in range(stimmen.Count):
            if any(s in stimmen.Item(i).GetDescription().lower() for s in suche):
                self.sapi.Voice = stimmen.Item(i)
                break
        self.sapi.Rate = max(-10, min(10, round(int(self.einst["tempo"]) / 10)))
        self.sapi.Volume = int(self.einst["lautstaerke"])
        self.sapi.Speak(text, 1 | 2)  # asynchron + Altes abbrechen
        while not self.stopp.is_set():
            if self.sapi.WaitUntilDone(50):
                return
        self.sapi.Speak("", 1 | 2)


# ================================================================ Bildschirm lesen
class Leser(threading.Thread):
    def __init__(self, einst, sprecher, melden):
        super().__init__(daemon=True)
        self.eigene_fenster = set()   # hwnds der Lesehelfer-Fenster
        self.uebersetzer = Uebersetzer(melden)
        if einst.profil.get("uebersetzen") or fehlersuche("uebersetzung"):
            self.uebersetzer.vorbereiten()
        self.einst = einst
        self.sprecher = sprecher
        self.melden = melden
        self.pause = False
        self.puls = time.time() + 60   # Zeit für den Start (Modell laden) einplanen
        self.zuruecksetzen = threading.Event()
        self.letzter = ""
        self.gemerkt = []
        self.ocr = None
        self.modus_aus = einst["modus"] != "spiele"   # Desktop-Modus aktiv -> Spiel-Lesen ruht
        self.ocr_sperre = threading.Lock()   # die Texterkennung teilen sich Spiel und Desktop
        self.start()

    def run(self):
        try:
            self.melden("motor", "Texterkennung wird vorbereitet …")
            ocr = ocr_erstellen(gpu_gewuenscht(self.einst))
            self.ocr = ocr
            self.melden("motor", ocr.name + (" · " + geraet_name(ocr.geraet) if hasattr(ocr, "geraet") else ""))
        except Exception as e:
            log("Texterkennung fehlt:", e)
            self.melden("fehler", "Texterkennung nicht verfügbar. Bitte über 'Lesehelfer_starten.bat' starten.")
            return
        schwarz, letzte_vorschau = 0, 0
        self.gruppe = None
        if fehlersuche("diagnose"):
            try:
                diagnose(ocr, "auto")
            except Exception:
                log(traceback.format_exc())
        with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
            while True:
                if self.zuruecksetzen.is_set():
                    self.zuruecksetzen.clear()
                    self.gruppe, schwarz = None, 0
                start = time.time()
                self.puls = start
                bereich = self.einst.profil.get("bereich")
                if self.pause or not bereich or self.modus_aus:
                    time.sleep(0.2)
                    continue
                # Liegt ein (sichtbares) Lesehelfer-Fenster über dem Textfeld? Dann nicht lesen.
                if self.einst["in_uebertragung_sichtbar"]:
                    bx1, by1 = bereich["left"], bereich["top"]
                    bx2, by2 = bx1 + bereich["width"], by1 + bereich["height"]
                    ueber = False
                    for h in list(self.eigene_fenster):
                        r = fenster_rechteck(h)
                        if r and r[0] < bx2 and r[2] > bx1 and r[1] < by2 and r[3] > by1:
                            ueber = True
                            break
                    if ueber:
                        if not getattr(self, "_ueber_gemeldet", False):
                            self.melden("hinweis", "Das Lesehelfer-Fenster liegt über dem Textfeld. "
                                                   "Bitte zur Seite schieben, sonst wird nichts gelesen.")
                            self._ueber_gemeldet = True
                        time.sleep(0.3)
                        continue
                    if getattr(self, "_ueber_gemeldet", False):
                        self.melden("hinweis", "")
                        self._ueber_gemeldet = False
                try:
                    roh = sct.grab(bereich)
                    img = Image.frombytes("RGB", roh.size, roh.rgb)
                except Exception as e:
                    log("Bildschirm nicht lesbar:", e)
                    time.sleep(1)
                    continue

                if time.time() - letzte_vorschau > 1:
                    klein = img.copy()
                    klein.thumbnail((380, 76))
                    self.melden("vorschau", klein)
                    letzte_vorschau = time.time()

                # Komplett schwarzes Bild? -> wahrscheinlich exklusives Vollbild
                if max(hi for _, hi in img.getextrema()) < 12:
                    schwarz += 1
                    if schwarz == 8:
                        self.melden("hinweis", "Das Bild ist schwarz. Im Spiel 'exklusives Vollbild' ausschalten "
                                               "oder Fenster / randloses Vollbild benutzen.")
                    time.sleep(INTERVALL)
                    continue
                if schwarz >= 8:
                    self.melden("hinweis", "")
                schwarz = 0

                try:
                    with self.ocr_sperre:
                        text = text_finden(ocr, img, "auto", "auto")
                except Exception as e:
                    log("Texterkennung-Fehler:", e)
                    self.melden("hinweis", "Texterkennung für diese Sprache fehlt evtl. in Windows.")
                    time.sleep(2)
                    continue

                dauer = time.time() - start
                if dauer > 1.5:
                    log(f"Langsam: {dauer:.1f} s für eine Lesung")
                if time.time() - getattr(self, "_letzter_puls_log", 0) > 60:
                    self._letzter_puls_log = time.time()
                    g = self.gruppe
                    log(f"Puls: OCR-Aufrufe={getattr(ocr, 'aufrufe', '?')} Zeilen={getattr(ocr, 'letzte_anzahl', '?')} "
                        f"Status={getattr(ocr, 'letzter_status', '?')} Bild={img.size} "
                        f"max={max(hi for _, hi in img.getextrema())}")
                    log(f"Puls: erkannt={text[:80]!r} | Gruppe={'-' if not g else repr(g['bester'][:60])} "
                        f"gesehen={g and g['gesehen']} stabil={g and g['stabil']} gesprochen={bool(g and g['gesprochen'])}")
                gueltig = len(text) >= MIN_ZEICHEN and echte_woerter(text) >= MIN_WOERTER
                self._verarbeiten(text if gueltig else "")
                time.sleep(max(0.05, INTERVALL - (time.time() - start)))

    # Eine "Gruppe" = ein Textfeld, das gerade zu sehen ist (auch wenn es noch Buchstabe
    # für Buchstabe erscheint oder die Erkennung bei jedem Bild leicht anders ausfällt).
    def _gleiche_gruppe(self, text):
        g = self.gruppe
        if not g:
            return False
        if aehnlich(text, g["letzter"]) >= 0.75 or aehnlich(text, g["bester"]) >= 0.75:
            return True
        # wächst der Text noch? (Anfang gleich, aber länger)
        kurz, lang = sorted((g["letzter"], text), key=len)
        if len(kurz) >= 6 and aehnlich(kurz, lang[:len(kurz)]) >= 0.75:
            return True
        # die meisten (bedeutungstragenden) Wörter stimmen überein – Erkennung hat nur einzelne
        # Wörter anders gelesen. Allerweltswörter zählen nicht mit.
        w_text, w_best = inhaltswoerter(text), inhaltswoerter(g["bester"])
        if len(w_text) >= 2 and len(w_best) >= 2:
            gemeinsam = len(w_text & w_best)
            return gemeinsam >= 0.7 * min(len(w_text), len(w_best))
        return False

    def _verarbeiten(self, text):
        g = self.gruppe
        if not text:
            if g:
                g["weg"] += 1
                if g["weg"] >= 2:  # Textfeld ist wirklich verschwunden
                    if not g["gesprochen"] and (g["gesehen"] >= 2 or textguete(g["bester"]) >= 3):
                        self._vorlesen(g["bester"])
                    self.gruppe = None
            return
        if g and self._gleiche_gruppe(text):
            g["weg"] = 0
            g["gesehen"] += 1
            # bevorzugt die längste / sauberste Fassung
            if len(text) > len(g["bester"]) * 1.15:
                g["bester"] = text
                g["stabil"] = 1          # Text wächst noch (erscheint Buchstabe für Buchstabe)
            else:
                g["stabil"] += 1         # nicht mehr gewachsen
                if len(text) >= len(g["bester"]) * 0.9 and textguete(text) > textguete(g["bester"]):
                    g["bester"] = text
            g["letzter"] = text
            if g["stabil"] >= STABIL_LESUNGEN:
                if not g["gesprochen"]:
                    g["gesprochen"] = g["bester"]
                    self._vorlesen(g["bester"])
                elif len(g["bester"]) > len(g["gesprochen"]) * 1.25:
                    # Text ist nach dem Vorlesen noch weitergelaufen -> nur den neuen Teil lesen
                    rest = neuer_teil(g["gesprochen"], g["bester"])
                    g["gesprochen"] = g["bester"]
                    if rest:
                        self._vorlesen(rest, merken=False)
            return
        # ein ANDERER Text ist da -> alten (falls noch nicht gelesen) schnell noch vorlesen
        if g and not g["gesprochen"] and g["gesehen"] >= 1 and len(g["bester"]) >= 12:
            self._vorlesen(g["bester"])
        self.gruppe = {"bester": text, "letzter": text, "gesehen": 1, "stabil": 1, "weg": 0,
                       "gesprochen": ""}

    def _ausgabe(self, text):
        """Was wirklich gesprochen wird und in welcher Sprache: Die Sprache wird je Text
        erkannt. Englisch + Haken "übersetzen" -> deutsche Fassung."""
        # unklar (z. B. "Oh, okay.") -> Sprache des letzten Textes nehmen
        sprache = sprache_genau(text) or getattr(self, "_letzte_sprache", "de")
        self._letzte_sprache = sprache
        if sprache != "en" or not self.einst.profil.get("uebersetzen"):
            return text, sprache
        self.uebersetzer.vorbereiten()
        try:
            de = self.uebersetzer.uebersetzen(text)
        except Exception as e:
            log("Übersetzen fehlgeschlagen:", e)
            de = None
        if de:
            log("Übersetzt:", text, "->", de)
            return de, "de"
        return text, "en"

    def _vorlesen(self, text, merken=True):
        if not merken:
            log("Vorlesen (Rest):", text)
            ausgabe, sprache = self._ausgabe(text)
            self.sprecher.sagen(ausgabe, unterbrechen=False, sprache=sprache)
            self.melden("gesprochen", self.gruppe["bester"] if self.gruppe else text)
            return
        if aehnlich(text, self.letzter) >= AEHNLICHKEIT:
            return
        jetzt = time.time()
        self.gemerkt = [(t, z) for t, z in self.gemerkt if jetzt - z < 120]
        if any(aehnlich(text, t) >= 0.75 for t, _ in self.gemerkt):
            return
        self.gemerkt.append((text, jetzt))
        log("Vorlesen:", text)
        ausgabe, sprache = self._ausgabe(text)
        self.letzter = ausgabe
        self.sprecher.sagen(ausgabe, unterbrechen=bool(self.einst["unterbrechen"]), sprache=sprache)
        self.melden("gesprochen", ausgabe)


def diagnose(ocr, sprache):
    """Probiert verschiedene Bildvarianten auf dem gespeicherten Kontrollbild aus (nur zur Fehlersuche)."""
    from PIL import ImageFilter
    pfad = os.path.join(PROTOKOLL, "kontrollbild.png")
    if not os.path.exists(pfad):
        log("DIAGNOSE: kein Kontrollbild")
        return
    sw = Image.open(pfad).convert("L")
    varianten = {
        "A so": sw,
        "B halb": sw.resize((sw.width // 2, sw.height // 2), Image.LANCZOS),
        "C 1.5x": sw.resize((int(sw.width * 1.5), int(sw.height * 1.5)), Image.LANCZOS),
        "D weich": sw.filter(ImageFilter.GaussianBlur(1.5)),
        "E dicker": sw.filter(ImageFilter.MinFilter(3)),
        "F duenner": sw.filter(ImageFilter.MaxFilter(3)),
        "G halb weich": sw.resize((sw.width // 2, sw.height // 2), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8)),
        "H rand": ImageOps.expand(sw, border=60, fill=255),
    }
    for name, v in varianten.items():
        try:
            roh = ocr.lesen(v.convert("RGB"), sprache)
            log(f"DIAGNOSE {name} {v.size}: {roh!r}")
        except Exception as e:
            log(f"DIAGNOSE {name}: Fehler {e}")
    log("DIAGNOSE fertig")


# ================================================================ Hilfsfunktionen Oberfläche
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def fenster_rechteck(hwnd):
    """(links, oben, rechts, unten) eines sichtbaren Fensters, sonst None. Thread-sicher."""
    try:
        u = ctypes.windll.user32
        if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            return None
        r = _RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def taste_anzeigen(t):
    if not t:
        return "–"
    teile = []
    for x in t.split("+"):
        x = x.strip()
        teile.append({"ctrl": "Strg", "strg": "Strg", "alt": "Alt", "shift": "Umschalt", "umschalt": "Umschalt", "space": "Leertaste",
                      "esc": "Esc", "tab": "Tab", "enter": "Enter"}.get(x, x.upper()))
    return " + ".join(teile)


def tempo_text(t):
    t = int(t)
    if t <= -25:
        return "langsam"
    if t < 0:
        return "etwas langsamer"
    if t == 0:
        return "normal"
    if t < 25:
        return "etwas schneller"
    return "schnell"


RADIUS = 12


def schrift(groesse=12, fett=False):
    return ctk.CTkFont(family=SCHRIFT, size=groesse, weight="bold" if fett else "normal")


def knopf(eltern, text, befehl, haupt=False, **kw):
    """Runder Knopf. haupt=True: grün hervorgehoben."""
    optionen = dict(text=text, command=befehl, corner_radius=10, height=40,
                    fg_color=GRUEN if haupt else KNOPF, hover_color="#5BE79B" if haupt else GRAU,
                    text_color=DUNKEL if haupt else TEXT, border_width=0 if haupt else 1, border_color=GRAU,
                    font=schrift(13, fett=haupt), cursor="hand2")
    optionen.update(kw)
    return ctk.CTkButton(eltern, **optionen)


def karte(eltern, **kw):
    """Abgerundete Fläche mit dünnem Rand (wie in der Skizze)."""
    optionen = dict(fg_color=PANEL, corner_radius=RADIUS, border_width=1, border_color=RAND)
    optionen.update(kw)
    return ctk.CTkFrame(eltern, **optionen)


def ueberschrift(eltern, text):
    return ctk.CTkLabel(eltern, text=text.upper(), text_color=BLASS, font=schrift(11, True), anchor="w")


REITER = [("spiele", "🎮\nSpiele"), ("desktop", "🖥\nDesktop"), ("vr", "🥽\nVR"), ("einst", "⚙\nEinstellungen")]


# ================================================================ Desktop: Text unter der Maus
DE_WOERTER = {"der", "die", "das", "und", "ist", "nicht", "ich", "sie", "es", "ein", "eine", "mit", "für",
              "auf", "auch", "sich", "wird", "werden", "oder", "zu", "den", "dem", "im", "von", "bitte", "datei"}
OHNE_SYMBOLE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0E\uFE0F\u200D"
                          "\uFFFC\uFFFD\u20E3]")

# nur Wörter, die es im Deutschen so nicht gibt (also kein "in", "an", "was", "so" …)
EN_WOERTER = {"the", "and", "is", "are", "you", "to", "of", "with", "for", "not", "this", "that", "it",
              "your", "on", "be", "will", "or", "from", "please", "file", "my", "i", "how", "what", "why",
              "we", "have", "has", "can", "do", "does", "all", "new", "best", "at", "by", "about", "when",
              "who", "they", "he", "she", "his", "her", "our", "first", "day", "time", "make", "get", "just",
              "more", "one", "out", "up", "if", "no", "yes", "like", "love", "world", "official", "video",
              "trailer", "full", "game", "games", "every", "ever", "never", "people", "into", "after",
              "before", "these", "those", "there", "their", "them", "would", "could", "should", "been",
              "were", "here", "because", "really", "only", "years", "year", "life", "live", "part",
              "playthrough", "gameplay", "review", "tips", "guide", "ending", "secret", "things"}


def erkenne_sprache(text):
    """Grobe Einschätzung: Deutsch oder Englisch (für die passende Stimme)."""
    woerter = re.findall(r"[a-zäöüß]+", text.lower())
    de = sum(w in DE_WOERTER for w in woerter) + 2 * len(re.findall(r"[äöüß]", text.lower()))
    en = sum(w in EN_WOERTER for w in woerter)
    return "en" if en > de else "de"


def sprache_genau(text):
    """Deutsch oder Englisch mit den Wörterbüchern: zählt Wörter, die NUR in einer
    der beiden Sprachen vorkommen. Gibt None zurück, wenn es unklar ist."""
    en_wb, de_wb = woerterbuch("en"), woerterbuch("de")
    if not en_wb or not de_wb:
        return erkenne_sprache(text)
    klein = (text or "").lower()
    woerter = [w for w in re.findall(r"[a-zäöüß']+", klein) if len(w) > 1 or w == "i"]
    en = sum(1 for w in woerter if w in en_wb and w not in de_wb)
    de = sum(1 for w in woerter if w in de_wb and w not in en_wb) + 2 * bool(re.search(r"[äöüß]", klein))
    if en == de:
        return None
    return "en" if en > de else "de"


def maus_position():
    from ctypes import wintypes
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def fenster_an_punkt(x, y):
    """Hauptfenster (hwnd) unter dem Punkt."""
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        h = u.WindowFromPoint(wintypes.POINT(x, y))
        return u.GetAncestor(h, 2) if h else None
    except Exception:
        return None


def programm_name(hwnd):
    try:
        from ctypes import wintypes
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        hp = k.OpenProcess(0x1000, False, pid.value)
        if not hp:
            return ""
        puffer = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(520)
        ok = k.QueryFullProcessImageNameW(ctypes.c_void_p(hp), 0, puffer, ctypes.byref(n))
        k.CloseHandle(ctypes.c_void_p(hp))
        return os.path.basename(puffer.value).lower() if ok else ""
    except Exception:
        return ""


class DesktopLeser:
    """Liest den Text unter dem Mauszeiger: erst direkt aus dem Programm (Windows UI Automation),
    sonst per Texterkennung auf einem kleinen Ausschnitt um die Maus."""

    MAX_ZEICHEN = 1500

    def __init__(self, leser, melden):
        self.leser = leser
        self.einst = leser.einst
        self.melden = melden
        self.uia = None            # None = noch nicht versucht, False = nicht verfügbar
        self.installiert_gerade = False
        self.beschaeftigt_seit = 0
        self._neuer_arbeiter()

    def _neuer_arbeiter(self):
        self.q = queue.Queue()
        threading.Thread(target=self._arbeiten, args=(self.q,), daemon=True).start()

    def anfragen(self, x, y, automatisch):
        if self.beschaeftigt_seit:
            if time.time() - self.beschaeftigt_seit > 8:
                log("Desktop-Lesen hing – neuer Versuch")
                self.beschaeftigt_seit = 0
                self._neuer_arbeiter()
            else:
                return
        self.q.put((x, y, automatisch))

    # ---------------- UI Automation bereitstellen
    def _uia_laden(self):
        if self.uia is not None:
            return self.uia
        try:
            import uiautomation as auto
        except ImportError:
            auto = None
            if not self.installiert_gerade:
                self.installiert_gerade = True
                try:
                    import subprocess
                    self.melden("hinweis", "Maus-Vorlesen wird eingerichtet – einen Moment …")
                    python = sys.executable
                    if python.lower().endswith("pythonw.exe"):
                        python = python[:-len("pythonw.exe")] + "python.exe"
                    log("Installiere uiautomation …")
                    subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                    "uiautomation"], check=True, timeout=300,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    import uiautomation as auto
                except Exception as e:
                    log("uiautomation nicht verfügbar, nur Texterkennung:", e)
                    auto = None
                self.melden("hinweis", "")
        # manche Pakete leiten beim Import die Ausgabe um -> Protokoll wiederherstellen
        if _LOG_ZIEL is not None and sys.stdout is not _LOG_ZIEL:
            sys.stdout = sys.stderr = _LOG_ZIEL
            log("Hinweis: Ausgabe war umgeleitet und wurde zurückgesetzt")
        if auto is None:
            self.uia = False
            return False
        log("Maus-Vorlesen: UI Automation bereit")
        try:
            auto.SetGlobalSearchTimeout(2)
        except Exception:
            pass
        self.uia = auto
        return auto

    def _arbeiten(self, q):
        init = None
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        while True:
            x, y, automatisch = q.get()
            self.beschaeftigt_seit = time.time()
            text, rechteck, quelle = "", None, ""
            try:
                auto = self._uia_laden()
                if auto and init is None:
                    try:
                        init = auto.UIAutomationInitializerInThread()
                    except Exception:
                        init = False
                if auto:
                    text, rechteck = self._aus_programm(auto, x, y)
                    quelle = "Programm"
                if not text and not automatisch_ohne_ocr(x, y, automatisch):
                    text, rechteck = self._aus_bild(x, y)
                    quelle = "Texterkennung"
            except Exception:
                log("Desktop-Lesen fehlgeschlagen:", traceback.format_exc())
            if q is not self.q:
                return   # wurde inzwischen ersetzt
            self.beschaeftigt_seit = 0
            text = re.sub(r"\s+", " ", OHNE_SYMBOLE.sub(" ", text or "")).strip()
            if len(text) > self.MAX_ZEICHEN:
                text = text[:self.MAX_ZEICHEN].rsplit(". ", 1)[0] + "."
            sprache = (sprache_genau(text) or erkenne_sprache(text)) if text else "de"
            if text and sprache == "en" and quelle == "Texterkennung":
                text = englisch_ausbessern(text, woerterbuch("en"))
            if text:
                log(f"Desktop ({quelle}, {'auto' if automatisch else 'Taste'}, {sprache}):", text[:120])
            if text and sprache == "en" and self.einst["desktop"].get("uebersetzen", True):
                u = self.leser.uebersetzer
                u.vorbereiten()
                if not u.bereit:   # beim allerersten Mal wird das Modell noch geladen
                    for _ in range(30):
                        if u.bereit or u.fehler:
                            break
                        time.sleep(0.2)
                try:
                    de = u.uebersetzen(text)
                except Exception as e:
                    log("Übersetzen fehlgeschlagen:", e)
                    de = None
                if de:
                    log("Übersetzt:", de[:120])
                    text, sprache = de, "de"
            self.melden("desktop_text", (text, rechteck, automatisch, sprache))

    # ---------------- Weg 1: Text direkt vom Programm
    @staticmethod
    def _rechteck(r):
        try:
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            return None

    def _aus_programm(self, auto, x, y):
        el = auto.ControlFromPoint(x, y)
        if not el:
            return "", None
        # a) Textbereich (Webseiten, Dokumente): den Absatz an der Mausposition nehmen
        c = el
        for _ in range(4):
            if not c:
                break
            try:
                tp = c.GetPattern(auto.PatternId.TextPattern)
            except Exception:
                tp = None
            if tp:
                for einheit, laenge in ((auto.TextUnit.Paragraph, 4000), (auto.TextUnit.Line, 800)):
                    try:
                        rng = tp.RangeFromPoint(x, y)
                        if not rng:
                            break
                        rng.ExpandToEnclosingUnit(einheit)
                        t = (rng.GetText(laenge) or "").strip()
                        if not t or len(t) > self.MAX_ZEICHEN * 2:
                            continue
                        rechtecke = [self._rechteck(r) for r in (rng.GetBoundingRectangles() or [])]
                        rechtecke = [r for r in rechtecke if r]
                        # nur wenn die Maus wirklich auf diesem Text liegt
                        if rechtecke and not any(r[0] - 8 <= x <= r[2] + 8 and r[1] - 8 <= y <= r[3] + 8
                                                 for r in rechtecke):
                            break
                        gesamt = None
                        if rechtecke:
                            gesamt = (min(r[0] for r in rechtecke), min(r[1] for r in rechtecke),
                                      max(r[2] for r in rechtecke), max(r[3] for r in rechtecke))
                        return t, gesamt
                    except Exception:
                        break
            try:
                c = c.GetParentControl()
            except Exception:
                break
        # b) Bedienelement: Knopf, Menüpunkt, Datei, Beschriftung …
        try:
            typ = el.ControlTypeName or ""
        except Exception:
            typ = ""
        if typ in ("PaneControl", "WindowControl", "DocumentControl", "GroupControl", "ImageControl"):
            return "", None
        name = ""
        try:
            name = (el.Name or "").strip()
        except Exception:
            pass
        if typ == "EditControl" or not name:
            try:
                wert = (el.GetValuePattern().Value or "").strip()
                if wert:
                    name = wert
            except Exception:
                pass
        r = None
        try:
            r = self._rechteck(el.BoundingRectangle)
        except Exception:
            pass
        if r:
            flaeche = (r[2] - r[0]) * (r[3] - r[1])
            if flaeche > 0.3 * ctypes.windll.user32.GetSystemMetrics(0) * ctypes.windll.user32.GetSystemMetrics(1):
                return "", None   # riesiges Element: Name passt nicht zur Mausposition
        return name, r

    # ---------------- Weg 2: Texterkennung um die Maus herum
    def _aus_bild(self, x, y):
        ocr = self.leser.ocr
        if ocr is None:
            return "", None
        b, h = 900, 240
        links, oben = x - b // 2, y - h // 2
        try:
            with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
                mon = sct.monitors[0]
                links = max(mon["left"], min(links, mon["left"] + mon["width"] - b))
                oben = max(mon["top"], min(oben, mon["top"] + mon["height"] - h))
                roh = sct.grab({"left": links, "top": oben, "width": b, "height": h})
                img = Image.frombytes("RGB", roh.size, roh.rgb)
        except Exception as e:
            log("Desktop-Bild nicht lesbar:", e)
            return "", None
        with self.leser.ocr_sperre:
            zeilen = ocr.zeilen(img, "de")
        zeilen = [z for z in zeilen if len(z) >= 5 and str(z[0]).strip()]
        if not zeilen:
            return "", None
        mx, my = x - links, y - oben

        def abstand(z):
            zx, zy, zh, zb = float(z[1]), float(z[2]), float(z[3]) or 10, float(z[4]) or 10
            dx = 0 if zx <= mx <= zx + zb else min(abs(mx - zx), abs(mx - zx - zb))
            dy = 0 if zy <= my <= zy + zh else min(abs(my - zy), abs(my - zy - zh))
            return dx + dy * 2, zh
        beste = min(zeilen, key=lambda z: abstand(z)[0])
        d, zh = abstand(beste)
        if d > zh * 1.5:
            return "", None
        # Nachbarzeilen desselben Absatzes dazunehmen
        zeilen.sort(key=lambda z: float(z[2]))
        i = zeilen.index(beste)
        block = [beste]

        def passt(a, z):
            ah = float(a[3]) or 10
            luecke = float(z[2]) - (float(a[2]) + ah) if float(z[2]) > float(a[2]) else float(a[2]) - (float(z[2]) + float(z[3]))
            ueberlapp = min(float(a[1]) + float(a[4]), float(z[1]) + float(z[4])) - max(float(a[1]), float(z[1]))
            return luecke < ah * 0.9 and ueberlapp > 0 and abs(float(z[3]) - ah) < ah * 0.5
        j = i
        while j > 0 and passt(zeilen[j], zeilen[j - 1]):
            j -= 1
            block.insert(0, zeilen[j])
        j = i
        while j < len(zeilen) - 1 and passt(zeilen[j], zeilen[j + 1]):
            j += 1
            block.append(zeilen[j])
        text = bereinigen(" ".join(str(z[0]) for z in block))
        text = re.sub(r"(\w)- (\w)", r"\1\2", text)
        rechteck = (int(links + min(float(z[1]) for z in block)), int(oben + min(float(z[2]) for z in block)),
                    int(links + max(float(z[1]) + float(z[4]) for z in block)),
                    int(oben + max(float(z[2]) + float(z[3]) for z in block)))
        return text, rechteck


# ================================================================ VR: Text in der Blickmitte
VR_FENSTER_WOERTER = ("vr view", "vr-ansicht", "vr ansicht", "headset window", "oculus mirror", "mirror")
VR_BEREICHE = {"klein": (0.35, 0.28), "mittel": (0.5, 0.4), "gross": (0.7, 0.55)}   # Anteil Breite/Höhe
VR_KNOPF_NAMEN = {1: "Menü-Taste (B/Y)", 2: "Griff-Taste", 7: "A/X-Taste", 32: "Stick drücken",
                  33: "Abzug (Trigger)", 0: "System-Taste"}


def sichtbare_fenster():
    """[(hwnd, titel)] aller sichtbaren, nicht winzigen Fenster."""
    erg = []
    try:
        import win32gui

        def je(h, _):
            if win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t:
                    try:
                        l, o, r, u = win32gui.GetWindowRect(h)
                        if r - l > 200 and u - o > 150:
                            erg.append((h, t))
                    except Exception:
                        pass
            return True
        win32gui.EnumWindows(je, None)
    except Exception as e:
        log("Fensterliste:", e)
    return erg


def vr_fenster_finden(gewaehlt=""):
    fenster = sichtbare_fenster()
    if gewaehlt:
        for h, t in fenster:
            if t == gewaehlt:
                return h, t
    for wort in VR_FENSTER_WOERTER:
        for h, t in fenster:
            if wort in t.lower() and not ist_eigener_titel(t):
                return h, t
    return None, ""


def fenster_bild(hwnd):
    """Bild vom Inhalt eines Fensters – auch wenn es teilweise verdeckt ist (PrintWindow)."""
    import win32gui
    import win32ui
    l, o, r, u = win32gui.GetClientRect(hwnd)
    b, h = r - l, u - o
    if b < 50 or h < 50:
        return None
    img = None
    hdc = win32gui.GetDC(hwnd)
    try:
        mfc = win32ui.CreateDCFromHandle(hdc)
        ziel = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, b, h)
        ziel.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, ziel.GetSafeHdc(), 3)   # nur Inhalt + DirectX
        if ok:
            info = bmp.GetInfo()
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True),
                                   "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bmp.GetHandle())
        ziel.DeleteDC()
        mfc.DeleteDC()
    finally:
        win32gui.ReleaseDC(hwnd, hdc)
    if img is None or max(hi for _, hi in img.getextrema()) < 12:
        # Notlösung: sichtbaren Bildschirmbereich abfotografieren
        x, y = win32gui.ClientToScreen(hwnd, (0, 0))
        with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
            roh = sct.grab({"left": x, "top": y, "width": b, "height": h})
            img = Image.frombytes("RGB", roh.size, roh.rgb)
    return img


def zusammengeklebte_trennen(text):
    """Texterkennung verschluckt manchmal Leerzeichen ("toMidnight", "togglethe") -> wieder trennen."""
    wbs = [w for w in (woerterbuch("en"), woerterbuch("de")) if w]
    if not wbs:
        return text

    def bekannt(w):
        return any(w.lower() in wb for wb in wbs)

    def trennen(m):
        wort = m.group(0)
        if bekannt(wort):
            return wort
        # kleiner Buchstabe direkt vor großem: dort trennen ("toMidnight")
        k = re.search(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])", wort)
        if k and bekannt(wort[:k.start()]) and bekannt(wort[k.start():]):
            return wort[:k.start()] + " " + wort[k.start():]
        beste = None
        for i in range(1, len(wort)):
            a, b = wort[:i], wort[i:]
            # Teile ab 2 Buchstaben – oder die Ein-Buchstaben-Wörter "a" / "I" ("isa" -> "is a")
            if (len(a) >= 2 or a.lower() in ("a", "i")) and (len(b) >= 2 or b.lower() in ("a", "i")) \
                    and bekannt(a) and bekannt(b) \
                    and (beste is None or min(len(a), len(b)) > min(map(len, beste))):
                beste = (a, b)
        return " ".join(beste) if beste else wort
    text = re.sub(r"[A-Za-zÄÖÜäöüß]{3,}", trennen, text)

    # umgekehrt: ein Wort, das mittendrin auseinandergerissen wurde ("col our" -> "colour")
    teile = text.split(" ")
    aus = []
    for t in teile:
        if aus and re.fullmatch(r"[A-Za-zÄÖÜäöüß]{2,}", aus[-1]) and re.fullmatch(r"[a-zäöüß]{2,}", t) \
                and bekannt(aus[-1] + t) and not (bekannt(aus[-1]) and bekannt(t)):
            aus[-1] = aus[-1] + t
        else:
            aus.append(t)
    return " ".join(aus)


PRONOMEN = {"he": "er", "him": "ihm", "his": "sein", "she": "sie", "her": "ihr", "hers": "ihr",
            "they": "sie", "them": "ihnen", "their": "ihr", "it": "es", "its": "sein", "any": "alle",
            "all": "alle", "xe": "xe", "xem": "xem", "ask": "fragen"}


def pronomen_deutsch(text):
    """Pronomen-Angaben wie "he/him" oder "she/her" direkt übertragen (die Übersetzung macht daraus Unsinn)."""
    def ersetzen(m):
        teile = re.split(r"\s*/\s*", m.group(0))
        return "/".join(PRONOMEN.get(t.lower(), t) for t in teile)
    text = re.sub(r"\b[A-Za-z]{2,5}(?:\s*/\s*[A-Za-z]{2,6}){1,3}\b",
                  lambda m: ersetzen(m) if all(t.lower() in PRONOMEN or len(t) > 3
                                               for t in re.split(r"\s*/\s*", m.group(0)))
                  and any(t.lower() in PRONOMEN for t in re.split(r"\s*/\s*", m.group(0)))
                  else m.group(0), text)
    return re.sub(r"\bPronouns?\s*:", "Pronomen:", text, flags=re.I)


def textblock_bei(zeilen, mx, my, max_abstand=1.5, nur_gute=False, namensschild=False):
    """Den Absatz finden, der am Punkt (mx, my) liegt. zeilen: (text, x, y, höhe, breite, …)."""
    zeilen = [z for z in zeilen if len(z) >= 5 and str(z[0]).strip()]
    if nur_gute:
        # Punktlinien, Rahmen und anderes Gekritzel weglassen
        def gut(t):
            # Namen wie "DeathGun18" sind erlaubt (Groß mitten im Wort, Ziffern) – nur Gekritzel fliegt raus
            zeichen = re.sub(r"\s", "", t)
            buchstaben = re.findall(r"[A-Za-zÄÖÜäöüß]", zeichen)
            sinnvoll = len(re.findall(r"[A-Za-zÄÖÜäöüß0-9/'!?,:]", zeichen))
            punkte = len(re.findall(r"[.…·_–—-]", t))
            return (len(buchstaben) >= 2 and sinnvoll >= len(zeichen) * 0.7
                    and punkte <= max(3, len(t) * 0.25))
        zeilen = [z for z in zeilen if gut(str(z[0]))]
    if not zeilen:
        return "", None

    def abstand(z):
        zx, zy, zh, zb = float(z[1]), float(z[2]), float(z[3]) or 10, float(z[4]) or 10
        dx = 0 if zx <= mx <= zx + zb else min(abs(mx - zx), abs(mx - zx - zb))
        dy = 0 if zy <= my <= zy + zh else min(abs(my - zy), abs(my - zy - zh))
        return dx + dy * 2, zh
    beste = min(zeilen, key=lambda z: abstand(z)[0])
    d, zh = abstand(beste)
    if max_abstand is not None and d > zh * max_abstand:
        return "", None
    zeilen.sort(key=lambda z: float(z[2]))
    i = zeilen.index(beste)
    block = [beste]

    def passt(a, z):
        ah = float(a[3]) or 10
        if float(z[2]) > float(a[2]):
            luecke = float(z[2]) - (float(a[2]) + ah)
        else:
            luecke = float(a[2]) - (float(z[2]) + float(z[3]))
        ueberlapp = min(float(a[1]) + float(a[4]), float(z[1]) + float(z[4])) - max(float(a[1]), float(z[1]))
        if luecke < ah * 0.9 and ueberlapp > 0 and abs(float(z[3]) - ah) < ah * 0.5:
            return True
        if namensschild:
            # Namensschilder (z. B. VRChat): großer Name, darunter kleinere Pronomen/Status –
            # zusammennehmen, wenn beide Zeilen übereinander mittig ausgerichtet sind
            # (linksbündig wie bei VRChat oder mittig – Hauptsache, sie überlappen deutlich)
            schmaler = min(float(a[4]), float(z[4])) or 1
            groesser = max(ah, float(z[3]) or 10)
            return luecke < groesser * 1.2 and ueberlapp > schmaler * 0.5
        return False
    j = i
    while j > 0 and passt(zeilen[j], zeilen[j - 1]):
        j -= 1
        block.insert(0, zeilen[j])
    j = i
    while j < len(zeilen) - 1 and passt(zeilen[j], zeilen[j + 1]):
        j += 1
        block.append(zeilen[j])
    text = bereinigen(" ".join(str(z[0]) for z in block))
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)
    rechteck = (min(float(z[1]) for z in block), min(float(z[2]) for z in block),
                max(float(z[1]) + float(z[4]) for z in block), max(float(z[2]) + float(z[3]) for z in block))
    return text, rechteck


def openvr_laden(melden):
    """Paket 'openvr' holen (bei Bedarf einmalig installieren)."""
    try:
        import openvr
        return openvr
    except ImportError:
        pass
    try:
        import subprocess
        melden("hinweis", "VR-Unterstützung wird eingerichtet – einen Moment …")
        python = sys.executable
        if python.lower().endswith("pythonw.exe"):
            python = python[:-len("pythonw.exe")] + "python.exe"
        log("Installiere openvr …")
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "openvr"],
                       check=True, timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import openvr
        return openvr
    except Exception as e:
        log("openvr nicht verfügbar:", e)
        return None
    finally:
        melden("hinweis", "")


def ist_eigener_titel(t):
    t = (t or "").lower()
    return "lesehelfer" in t or "ps2-vorleser" in t


# interne SteamVR-Kennung bleibt gleich, damit eingestellte Controller-Tasten erhalten bleiben
VR_APP_KEY = "ps2vorleser.vorleser"
VR_ORDNER = os.path.join(DATEN, "vr")
VR_AKTION = "/actions/vorleser/in/vorlesen"


def _vr_dateien_schreiben():
    """Aktions-Datei, Standard-Belegungen und App-Beschreibung für SteamVR anlegen."""
    os.makedirs(VR_ORDNER, exist_ok=True)
    aktionen = {
        "actions": [{"name": VR_AKTION, "type": "boolean", "requirement": "optional"}],
        "action_sets": [{"name": "/actions/vorleser", "usage": "leftright"}],
        "default_bindings": [
            {"controller_type": "oculus_touch", "binding_url": "belegung_oculus_touch.json"},
            {"controller_type": "knuckles", "binding_url": "belegung_knuckles.json"},
        ],
        "localization": [
            {"language_tag": "de_DE", "/actions/vorleser": "Lesehelfer",
             VR_AKTION: "Text in der Blickmitte vorlesen"},
            {"language_tag": "en_US", "/actions/vorleser": "Lesehelfer",
             VR_AKTION: "Read text in the middle of view"},
        ],
    }

    def belegung(typ, stick):
        return {"action_manifest_version": 0, "alias_info": {}, "category": "steamvr_input",
                "controller_type": typ, "description": "Rechten Stick drücken = vorlesen",
                "name": "Lesehelfer Standard", "options": {}, "simulated_actions": [],
                "bindings": {"/actions/vorleser": {
                    "chords": [], "haptics": [], "poses": [], "skeleton": [],
                    "sources": [{"path": f"/user/hand/right/input/{stick}", "mode": "joystick",
                                 "inputs": {"click": {"output": VR_AKTION}}}]}}}
    python = sys.executable
    if python.lower().endswith("python.exe"):
        python = python[:-len("python.exe")] + "pythonw.exe"
    app = {"source": "builtin", "applications": [{
        "app_key": VR_APP_KEY, "launch_type": "binary",
        "binary_path_windows": python,
        "arguments": '"' + os.path.abspath(__file__) + '"',
        "is_dashboard_overlay": True,
        "action_manifest_path": os.path.join(VR_ORDNER, "aktionen.json"),
        "strings": {"en_us": {"name": "Lesehelfer", "description": "Liest Texte in VR vor"},
                    "de_de": {"name": "Lesehelfer", "description": "Liest Texte in VR vor"}}}]}
    for name, inhalt in (("aktionen.json", aktionen),
                         ("belegung_oculus_touch.json", belegung("oculus_touch", "joystick")),
                         ("belegung_knuckles.json", belegung("knuckles", "thumbstick")),
                         ("ps2vorleser.vrmanifest", app)):
        with open(os.path.join(VR_ORDNER, name), "w", encoding="utf-8") as f:
            json.dump(inhalt, f, ensure_ascii=False, indent=2)


class VRController(threading.Thread):
    """Meldet sich bei SteamVR als kleines Overlay-Programm an und bekommt über SteamVR Input
    eine eigene Aktion ("vorlesen"). Die Taste dafür legt man in der SteamVR-Belegung fest
    (Standard: rechten Stick drücken). So kommt der Tastendruck auch an, während ein Spiel läuft."""

    def __init__(self, einst, melden):
        super().__init__(daemon=True)
        self.einst = einst
        self.melden = melden
        self.aktiv = False           # nur im VR-Modus mit SteamVR verbinden
        self.belegung_oeffnen = False
        self.status = None
        self.start()

    def _status(self, s):
        if s != self.status:
            self.status = s
            self.melden("vr_status", s)

    def _verbinden(self, ovr):
        # Erst nur nachsehen, ob SteamVR schon läuft: Als "Hintergrund-Programm" startet openvr
        # SteamVR NICHT von selbst, als Overlay-Programm schon. Der Vorleser soll SteamVR nie starten.
        ovr.init(ovr.VRApplication_Background)   # Fehler, wenn SteamVR nicht läuft
        ovr.shutdown()
        system = ovr.init(ovr.VRApplication_Overlay)
        _vr_dateien_schreiben()
        try:
            apps = ovr.VRApplications()
            apps.addApplicationManifest(os.path.join(VR_ORDNER, "ps2vorleser.vrmanifest"), True)
            apps.identifyApplication(os.getpid(), VR_APP_KEY)
        except Exception as e:
            log("SteamVR-App anmelden:", e)
        eingabe = ovr.VRInput()
        eingabe.setActionManifestPath(os.path.join(VR_ORDNER, "aktionen.json"))
        satz = eingabe.getActionSetHandle("/actions/vorleser")
        aktion = eingabe.getActionHandle(VR_AKTION)
        saetze = (ovr.VRActiveActionSet_t * 1)()
        saetze[0].ulActionSet = satz
        log("SteamVR verbunden (Overlay mit eigener Taste)")
        return system, eingabe, satz, aktion, saetze

    def run(self):
        ovr = None
        verbindung = None
        vorher = False
        while True:
            if not self.aktiv:
                if verbindung is not None:
                    try:
                        ovr.shutdown()
                    except Exception:
                        pass
                    verbindung = None
                    self._status("aus")
                time.sleep(0.5)
                continue
            if ovr is None:
                ovr = openvr_laden(self.melden) or False
            if ovr is False:
                self._status("fehlt")
                time.sleep(5)
                continue
            if verbindung is None:
                try:
                    verbindung = self._verbinden(ovr)
                    vorher = False
                    self._status("verbunden")
                except Exception as e:
                    if self.status != "kein_steamvr":
                        log("SteamVR nicht erreichbar:", e)
                    try:
                        ovr.shutdown()
                    except Exception:
                        pass
                    self._status("kein_steamvr")
                    time.sleep(5)
                    continue
            system, eingabe, satz, aktion, saetze = verbindung
            try:
                ereignis = ovr.VREvent_t()
                while system.pollNextEvent(ereignis):
                    if ereignis.eventType == ovr.VREvent_Quit:
                        log("SteamVR wird beendet")
                        try:
                            system.acknowledgeQuit_Exiting()
                        except Exception:
                            pass
                        raise RuntimeError("SteamVR beendet")
                if self.belegung_oeffnen:
                    self.belegung_oeffnen = False
                    try:
                        eingabe.openBindingUI(VR_APP_KEY, satz, ovr.k_ulInvalidInputValueHandle, True)
                        log("SteamVR-Tastenbelegung geöffnet")
                    except Exception as e:
                        log("Tastenbelegung öffnen:", e)
                        self.melden("vr_belegung_fehler", None)
                eingabe.updateActionState(saetze)
                daten = eingabe.getDigitalActionData(aktion, ovr.k_ulInvalidInputValueHandle)
                gedrueckt = bool(daten.bActive and daten.bState)
                if gedrueckt and not vorher and time.time() - getattr(self, "_zuletzt", 0) > 0.4:
                    self._zuletzt = time.time()   # Prellen des Sticks abfangen
                    log("VR-Controller-Taste gedrückt")
                    self.melden("befehl", "vr")
                vorher = gedrueckt
                time.sleep(0.02)
            except Exception as e:
                log("SteamVR-Verbindung getrennt:", e)
                try:
                    ovr.shutdown()
                except Exception:
                    pass
                verbindung = None
                self._status("kein_steamvr")
                time.sleep(3)


class VRLeser:
    """Liest Text aus der Mitte des SteamVR-Spiegelfensters ('VR-Ansicht')."""

    def __init__(self, leser, melden):
        self.leser = leser
        self.einst = leser.einst
        self.melden = melden
        self.q = queue.Queue()
        self.automatik = False         # wird von der Oberfläche gesetzt
        self.letzter_auto = ("", 0)
        threading.Thread(target=self._arbeiten, daemon=True).start()
        threading.Thread(target=self._automatik, daemon=True).start()

    def anfragen(self):
        if self.q.qsize() < 2:
            self.q.put(True)

    def _mitte_lesen(self):
        """-> (text, fehler). fehler: 'fenster' wenn keine VR-Ansicht gefunden."""
        hwnd, titel = vr_fenster_finden(self.einst["vr"].get("fenster", ""))
        if not hwnd:
            return "", "fenster"
        if ctypes.windll.user32.IsIconic(hwnd):
            return "", "minimiert"
        img = fenster_bild(hwnd)
        if img is None:
            return "", "fenster"
        bw, bh = VR_BEREICHE.get(self.einst["vr"].get("bereich", "mittel"), VR_BEREICHE["mittel"])
        W, H = img.size
        b, h = int(W * bw), int(H * bh)
        x0, y0 = (W - b) // 2, (H - h) // 2
        ausschnitt = img.crop((x0, y0, x0 + b, y0 + h))
        ocr = self.leser.ocr
        if ocr is None:
            return "", None
        # kleine Spiegelfenster vergrößern, damit die Texterkennung die Schrift lesen kann
        # Größe angleichen: zu klein -> Schrift unleserlich, zu groß -> dauert unnötig lange
        if ausschnitt.width < 900 or ausschnitt.width > 1400:
            f = (900 if ausschnitt.width < 900 else 1400) / ausschnitt.width
            ausschnitt = ausschnitt.resize((int(ausschnitt.width * f), int(ausschnitt.height * f)))
        with self.leser.ocr_sperre:
            zeilen = ocr.zeilen(ausschnitt, "de")
        if fehlersuche("vr"):
            mx, my = ausschnitt.width / 2, ausschnitt.height / 2
            log(f"VR-Rohdaten (Mitte {mx:.0f},{my:.0f}):",
                " | ".join(f"{z[0]!r}@{float(z[1]):.0f},{float(z[2]):.0f} h{float(z[3]):.0f} b{float(z[4]):.0f}"
                           for z in zeilen))
        text, _ = textblock_bei(list(zeilen), ausschnitt.width / 2, ausschnitt.height / 2, max_abstand=None,
                                nur_gute=True, namensschild=True)
        if text:
            text = zusammengeklebte_trennen(text)
        return text, None

    def _fertig_machen(self, text):
        text = re.sub(r"\s+", " ", OHNE_SYMBOLE.sub(" ", text or "")).strip()
        text = pronomen_deutsch(text)
        sprache = (sprache_genau(text) or erkenne_sprache(text)) if text else "de"
        if text and sprache == "en":
            text = englisch_ausbessern(text, woerterbuch("en"))
        if text and sprache == "en" and self.einst["vr"].get("uebersetzen", True):
            u = self.leser.uebersetzer
            u.vorbereiten()
            for _ in range(30):
                if u.bereit or u.fehler:
                    break
                time.sleep(0.2)
            try:
                de = u.uebersetzen(text)
            except Exception as e:
                log("Übersetzen fehlgeschlagen:", e)
                de = None
            if de:
                log("Übersetzt:", de[:120])
                text, sprache = de, "de"
        return text, sprache

    def _arbeiten(self):
        while True:
            self.q.get()
            try:
                text, fehler = self._mitte_lesen()
                if text:
                    log("VR (Taste):", text[:120])
                text, sprache = self._fertig_machen(text)
                self.melden("vr_text", (text, False, sprache, fehler))
            except Exception:
                log("VR-Lesen fehlgeschlagen:", traceback.format_exc())
                self.melden("vr_text", ("", False, "de", None))

    def _automatik(self):
        """Liegt ein Text eine Weile ruhig in der Blickmitte, wird er einmal vorgelesen."""
        kandidat, seit = "", 0
        while True:
            if not self.automatik:
                kandidat, seit = "", 0
                time.sleep(0.3)
                continue
            start = time.time()
            try:
                text, _ = self._mitte_lesen()
            except Exception as e:
                log("VR-Automatik:", e)
                text = ""
            if len(text) < MIN_ZEICHEN or echte_woerter(text) < MIN_WOERTER:
                kandidat, seit = "", 0
            elif aehnlich(text, kandidat) >= 0.8:
                warte = float(self.einst["vr"].get("wartezeit", 1.5))
                if seit and time.time() - seit >= warte:
                    alt, wann = self.letzter_auto
                    if aehnlich(text, alt) < AEHNLICHKEIT or time.time() - wann > 120:
                        self.letzter_auto = (text, time.time())
                        log("VR (automatisch):", text[:120])
                        aus, sprache = self._fertig_machen(text)
                        self.melden("vr_text", (aus, True, sprache, None))
                    seit = 0   # nicht dauernd wiederholen
            else:
                kandidat, seit = text, time.time()
            time.sleep(max(0.2, 0.6 - (time.time() - start)))


def automatisch_ohne_ocr(x, y, automatisch):
    """Beim automatischen Vorlesen keine Texterkennung über Spielen/Emulatoren (würde ständig plappern)."""
    if not automatisch:
        return False
    h = fenster_an_punkt(x, y)
    name = programm_name(h) if h else ""
    return any(s in name for s in ("pcsx2", "retroarch", "dolphin", "rpcs3", "ppsspp", "duckstation"))


# ================================================================ Leistungsmessung (nur zum Testen)
class Leistungsmessung(threading.Thread):
    """Schreibt alle 30 s Arbeitsspeicher und Prozessorlast in protokoll\\leistung_….txt.
    Läuft nur, solange in daten\\fehlersuche.txt das Wort "leistung" steht."""

    TAKT = 30

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.datei = None
        self.vorher = {}
        self.kerne = os.cpu_count() or 1
        self.start()

    @staticmethod
    def _werte(pid):
        """(Arbeitsspeicher in MB, CPU-Zeit in s) eines Prozesses."""
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000 | 0x0010, False, pid)
        if not h:
            return None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(pmc), pmc.cb)
            a, b, kern, nutzer = (wintypes.FILETIME() for _ in range(4))
            k.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(a), ctypes.byref(b), ctypes.byref(kern),
                              ctypes.byref(nutzer))
            zeit = lambda ft: (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 1e7
            return pmc.WorkingSetSize / 1048576, zeit(kern) + zeit(nutzer)
        finally:
            k.CloseHandle(ctypes.c_void_p(h))

    def _prozent(self, name, pid, cpu_zeit, jetzt):
        alt = self.vorher.get(name)
        self.vorher[name] = (pid, cpu_zeit, jetzt)
        if not alt or alt[0] != pid or jetzt <= alt[2]:
            return None
        return max(0.0, (cpu_zeit - alt[1]) / (jetzt - alt[2]) / self.kerne * 100)

    def run(self):
        aufrufe_vorher, zeit_vorher = OCRProzess.aufrufe, OCRProzess.zeit_summe
        while True:
            time.sleep(self.TAKT)
            if not fehlersuche("leistung"):
                continue
            try:
                if self.datei is None:
                    name = os.path.basename(LOG_DATEI).replace("vorleser_", "leistung_")
                    self.datei = os.path.join(PROTOKOLL, name)
                    with open(self.datei, "a", encoding="utf-8") as f:
                        f.write(f"Leistungsmessung Lesehelfer – alle {self.TAKT} s. Prozessor: {self.kerne} "
                                f"Kerne. CPU-Prozent wie im Task-Manager (Anteil am ganzen Prozessor).\n\n")
                jetzt = time.time()
                teile = []
                gesamt_ram, gesamt_cpu = 0.0, 0.0
                ocr = getattr(self.app.leser, "ocr", None)
                prozesse = [("Hauptprogramm", os.getpid())]
                if getattr(ocr, "p", None) is not None and ocr.p.pid:
                    prozesse.append(("Texterkennung", ocr.p.pid))
                for name, pid in prozesse:
                    w = self._werte(pid)
                    if not w:
                        continue
                    ram, cpu_zeit = w
                    pr = self._prozent(name, pid, cpu_zeit, jetzt)
                    gesamt_ram += ram
                    gesamt_cpu += pr or 0
                    teile.append(f"{name}: RAM {ram:.0f} MB, CPU " + ("–" if pr is None else f"{pr:.1f} %"))
                n = OCRProzess.aufrufe - aufrufe_vorher
                dauer = OCRProzess.zeit_summe - zeit_vorher
                aufrufe_vorher, zeit_vorher = OCRProzess.aufrufe, OCRProzess.zeit_summe
                ocr_text = f"Texterkennung: {n} Bilder" + (f", Ø {dauer / n * 1000:.0f} ms" if n else "")
                modus = self.app.einst["modus"] + (" (Pause)" if self.app.leser.pause else "")
                zeile = (time.strftime("%H:%M:%S") + f" | Modus: {modus} | " + " | ".join(teile) +
                         f" | Gesamt: RAM {gesamt_ram:.0f} MB, CPU {gesamt_cpu:.1f} % | {ocr_text}")
                with open(self.datei, "a", encoding="utf-8") as f:
                    f.write(zeile.replace(".", ",") + "\n")
            except Exception as e:
                log("Leistungsmessung:", e)


# ================================================================ Bildschirm-Hilfen
def bildschirm_von(hwnd):
    """(Monitor-Kennung, Arbeitsbereich links/oben/rechts/unten) des Bildschirms, auf dem das Fenster liegt."""
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        mon = u.MonitorFromWindow(hwnd, 2)          # 2 = nächster Bildschirm
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None, None
        w = mi.rcWork
        return mon, (w.left, w.top, w.right, w.bottom)
    except Exception:
        return None, None


def fenster_rahmen(hwnd):
    """Außenmaße des Fensters inkl. Titelleiste: (links, oben, rechts, unten)."""
    try:
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def natuerliche_groesse(fenster):
    """Fenster wieder genau so groß machen, wie sein Inhalt es braucht.
    (CustomTkinter setzt beim Bildschirmwechsel sonst eine feste, oft zu kleine Größe.)"""
    try:
        tk.Tk.wm_minsize(fenster, 1, 1)
        tk.Tk.wm_maxsize(fenster, 30000, 30000)
        tk.Tk.wm_geometry(fenster, "")
    except Exception:
        pass


# ================================================================ Oberfläche
class App:
    def __init__(self):
        self.einst = Einstellungen()
        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title("Lesehelfer")
        self.root.resizable(False, False)
        if self.einst["fenster_pos"]:
            self.root.geometry(f"+{self.einst['fenster_pos'][0]}+{self.einst['fenster_pos'][1]}")
        self.root.bind("<Configure>", self._fenster_bewegt)
        self.root.protocol("WM_DELETE_WINDOW", self.beenden)
        self.root.report_callback_exception = self._fehler

        self.nachrichten = queue.Queue()     # von Threads -> Oberfläche
        self.verlauf = []
        self.vorschau_bild = None
        self.mini = None
        self.tasten_fenster = None
        self.taste_wird_gelesen = False

        self.sprecher = Sprecher(self.einst, self.melden)
        self.leser = Leser(self.einst, self.sprecher, self.melden)
        self.desktop = DesktopLeser(self.leser, self.melden)
        self.vr_controller = VRController(self.einst, self.melden)
        self.vr = VRLeser(self.leser, self.melden)
        self._vr_aktiv_spricht = False
        self._desktop_aktiv = False
        self._desktop_letzter = ("", 0)
        self._maus_ruhe = (None, 0, False)     # (Position, seit wann still, schon gelesen?)
        self._rahmen_fenster = None

        self._hauptfenster()
        self._vr_zustand()
        self.leistung = Leistungsmessung(self)
        self.fenster_anmelden(self.root)
        self.tasten_registrieren()
        self.root.after(50, self._abholen)
        self.root.after(500, self._maus_beobachten)
        self._eigene_zeit = os.path.getmtime(os.path.abspath(__file__))
        self.root.after(3000, self._auf_update_pruefen)

        if not any(p.get("bereich") for p in self.einst["profile"].values()):
            self.sprecher.sagen("Willkommen. Starte dein Spiel, bis ein Text zu sehen ist. "
                                "Klicke dann auf Textfeld markieren, oder drücke F 8.")
        else:
            self.sprecher.sagen("Lesehelfer gestartet.")
        if os.path.isfile(UPDATE_MERKER):
            try:
                os.remove(UPDATE_MERKER)
            except Exception:
                pass
            log("Läuft jetzt mit Version", VERSION)
            self.sprecher.sagen("Der Lesehelfer wurde aktualisiert.", unterbrechen=False)
        threading.Thread(target=online_update_schleife, daemon=True).start()

    def _fenster_bewegt(self, e):
        if e.widget is not self.root or self.root.state() != "normal":
            return
        self._bildschirm_pruefen()
        if getattr(self, "_pos_timer", None):
            self.root.after_cancel(self._pos_timer)
        self._pos_timer = self.root.after(800, self._position_speichern)

    # -------------------------------------------------------- Größe passend zum Bildschirm
    def _bildschirm_pruefen(self):
        try:
            hwnd = int(self.root.wm_frame(), 16)
        except Exception:
            return
        mon, _ = bildschirm_von(hwnd)
        if mon is None or mon == getattr(self, "_letzter_bildschirm", None):
            return
        erster = getattr(self, "_letzter_bildschirm", None) is None
        self._letzter_bildschirm = mon
        if not erster:
            log("Fenster auf anderen Bildschirm verschoben – Größe wird angepasst.")
        # CustomTkinter rechnet beim Wechsel ca. 1 s lang um; danach (und zur Sicherheit nochmal) anpassen
        for ms in (300, 1400, 2600):
            self.root.after(ms, lambda: self._groesse_anpassen(erzwingen=True))

    def _groesse_planen(self, _e=None):
        if getattr(self, "_groesse_timer", None):
            self.root.after_cancel(self._groesse_timer)
        self._groesse_timer = self.root.after(120, self._groesse_anpassen)

    def _groesse_anpassen(self, erzwingen=False):
        self._groesse_timer = None
        try:
            r = self.root
            r.update_idletasks()
            bw, bh = self.mitte.winfo_reqwidth(), self.mitte.winfo_reqheight()
            if bw < 10 or bh < 10:
                return
            hwnd = int(r.wm_frame(), 16)
            _, arbeit = bildschirm_von(hwnd)
            rest = r.winfo_reqheight() - self.scroll_rahmen.winfo_reqheight()   # Kopf + Fuß
            rahmen = fenster_rahmen(hwnd)
            titel = 40
            if rahmen:
                titel = max(0, min(120, (rahmen[3] - rahmen[1]) - r.winfo_height()))
            if arbeit:
                platz = (arbeit[3] - arbeit[1]) - titel - rest - 10
            else:
                platz = r.winfo_screenheight() - titel - rest - 60
            h = max(160, min(bh, platz))
            blaettern = bh > h + 1
            neu = (bw, h, blaettern, bh)
            if neu != getattr(self, "_letzte_groesse", None) or erzwingen:
                self._letzte_groesse = neu
                self.leinwand.configure(width=bw, height=h, scrollregion=(0, 0, bw, bh))
                self.leinwand.itemconfigure(self._mitte_id, width=bw)
                if blaettern and not self._blaettern:
                    self.scroll_leiste.pack(side="right", fill="y", padx=(0, 4))
                elif not blaettern and self._blaettern:
                    self.scroll_leiste.pack_forget()
                    self.leinwand.yview_moveto(0)
                self._blaettern = blaettern
                natuerliche_groesse(r)
            # Fenster vollständig auf dem Bildschirm halten
            if arbeit:
                r.update_idletasks()
                rahmen = fenster_rahmen(hwnd)
                if rahmen:
                    x, y = rahmen[0], rahmen[1]
                    nx = min(max(x, arbeit[0]), max(arbeit[0], arbeit[2] - (rahmen[2] - rahmen[0])))
                    ny = min(max(y, arbeit[1]), max(arbeit[1], arbeit[3] - (rahmen[3] - rahmen[1])))
                    if (nx, ny) != (x, y):
                        tk.Tk.wm_geometry(r, f"+{nx}+{ny}")
        except Exception as e:
            log("Fenstergröße anpassen fehlgeschlagen:", e)

    def _mausrad(self, e):
        if not getattr(self, "_blaettern", False):
            return
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        self.leinwand.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def _position_speichern(self):
        self._pos_timer = None
        try:
            pos = [self.root.winfo_x(), self.root.winfo_y()]
            if pos != self.einst["fenster_pos"]:
                self.einst["fenster_pos"] = pos
        except Exception:
            pass

    # -------------------------------------------------------- Sichtbarkeit in Aufnahmen
    def fenster_anmelden(self, fenster):
        try:
            fenster.update_idletasks()
            hwnd = int(fenster.wm_frame(), 16)
        except Exception:
            return
        self.leser.eigene_fenster.add(hwnd)
        self._sichtbarkeit_setzen(hwnd)

    def _sichtbarkeit_setzen(self, hwnd):
        # 0 = normal sichtbar (auch in Discord/OBS), 0x11 = in Aufnahmen unsichtbar
        wert = 0 if self.einst["in_uebertragung_sichtbar"] else 0x11
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, wert)
        except Exception:
            pass

    def sichtbarkeit_umschalten(self):
        self.einst["in_uebertragung_sichtbar"] = bool(self.sichtbar_var.get())
        for h in list(self.leser.eigene_fenster):
            self._sichtbarkeit_setzen(h)
        if not self.einst["in_uebertragung_sichtbar"]:
            self._n_hinweis("")

    # -------------------------------------------------------- Kommunikation
    def melden(self, art, wert=None):
        # Sicherung: Vorschaubilder nicht stapeln, falls die Oberfläche hängt
        if art == "vorschau" and self.nachrichten.qsize() > 200:
            return
        self.nachrichten.put((art, wert))

    def _abholen(self):
        # Ein Fehler in einer Meldung darf das Abholen nie beenden
        # (sonst füllt sich die Warteschlange und der Speicher wächst).
        try:
            for _ in range(500):
                try:
                    art, wert = self.nachrichten.get_nowait()
                except queue.Empty:
                    break
                try:
                    getattr(self, "_n_" + art)(wert)
                except Exception as e:
                    n = getattr(self, "_abhol_fehler", 0) + 1
                    self._abhol_fehler = n
                    if n <= 5:
                        log("Fehler bei Meldung", art + ":", repr(e))
        finally:
            self.root.after(50, self._abholen)

    def _n_gesprochen(self, text):
        self.verlauf.append(text)
        self.verlauf = self.verlauf[-50:]
        kurz = text if len(text) <= 160 else text[:157].rsplit(" ", 1)[0] + " …"
        self.letzter_label.configure(text=f"„{kurz}“")
        self.verlauf_knopf.configure(text=f"Verlauf ({len(self.verlauf)})")
        if self.mini:
            self.mini_text.configure(text=text if len(text) <= 90 else text[:87].rsplit(" ", 1)[0] + " …")

    def _n_vorschau(self, img):
        b = img.copy()
        b.thumbnail((380, 76))
        neu = ctk.CTkImage(light_image=b, dark_image=b, size=b.size)
        # Altes Bild erst NACH dem Umstellen freigeben – sonst zeigt das Feld
        # kurz auf ein gelöschtes Bild ("pyimage… does not exist").
        alt = self.vorschau_bild
        self.vorschau.configure(image=neu, text="")
        self.vorschau_bild = neu
        self._vorschau_alt = alt

    def _n_ki(self, ok):
        self.punkt.configure(text_color=GRUEN if ok else GELB)
        self.status_text.configure(text="KI-Stimme online" if ok else "Einfache Stimme (offline)")

    def _n_stimme_geaendert(self, _):
        self._anzeige_aktualisieren()

    def _n_motor(self, text):
        self.motor_label.configure(text="Texterkennung: " + text)
        if hasattr(self, "rechner_info") and not getattr(self, "_rechner_arbeitet", False):
            self._rechner_info_setzen()

    def _n_hinweis(self, text):
        self.hinweis_text.configure(text=text)
        if text:
            self.hinweis.pack(fill="x", padx=20, pady=(0, 10), before=self.reiter_leiste)
        else:
            self.hinweis.pack_forget()

    def _n_fehler(self, text):
        messagebox.showerror("Lesehelfer", text, parent=self.root)

    def _n_befehl(self, name):
        if self.taste_wird_gelesen:
            return
        {"pause": self.pause_umschalten, "wiederholen": self.wiederholen,
         "stimme": self.stimme_umschalten, "markieren": self.markieren,
         "beenden": self.beenden, "maus": self.maus_taste, "vr": self.vr_taste}[name]()

    def _n_taste(self, wert):
        self._taste_gelesen(*wert)

    def _fehler(self, typ, wert, tb):
        log("".join(traceback.format_exception(typ, wert, tb)))
        messagebox.showerror("Lesehelfer", f"Fehler: {wert}", parent=self.root)

    # -------------------------------------------------------- Hauptfenster
    def _hauptfenster(self):
        r = self.root

        # Kopfzeile
        kopf = ctk.CTkFrame(r, fg_color=PANEL, corner_radius=0)
        kopf.pack(fill="x")
        ctk.CTkLabel(kopf, text="🔊  Lesehelfer", text_color=TEXT, font=schrift(17, True)).pack(
            side="left", padx=18, pady=12)
        self.status_text = ctk.CTkLabel(kopf, text="Stimme wird geladen …", text_color=BLASS, font=schrift(12))
        self.status_text.pack(side="right", padx=(4, 18))
        self.punkt = ctk.CTkLabel(kopf, text="●", text_color=BLASS, font=schrift(14))
        self.punkt.pack(side="right")

        # Mittelteil: liegt in einem Bereich, der bei zu kleinem Bildschirm blättern kann
        self.scroll_rahmen = ctk.CTkFrame(r, fg_color=BG, corner_radius=0)
        self.leinwand = tk.Canvas(self.scroll_rahmen, bg=BG, highlightthickness=0, bd=0,
                                  yscrollincrement=20)
        self.scroll_leiste = ctk.CTkScrollbar(self.scroll_rahmen, command=self.leinwand.yview,
                                              button_color=GRAU, button_hover_color=BLASS)
        self.leinwand.configure(yscrollcommand=self.scroll_leiste.set)
        self.leinwand.pack(side="left", fill="both", expand=True)
        self.mitte = ctk.CTkFrame(self.leinwand, fg_color=BG, corner_radius=0)
        self._mitte_id = self.leinwand.create_window(0, 0, window=self.mitte, anchor="nw")
        self._blaettern = False
        tk.Misc.bind(self.mitte, "<Configure>", self._groesse_planen, "+")
        r.bind_all("<MouseWheel>", self._mausrad, add="+")
        m = self.mitte

        # großer Pause-Knopf
        self.pause_knopf = ctk.CTkButton(m, command=self.pause_umschalten, corner_radius=16, height=66,
                                         anchor="w", font=schrift(18, True), cursor="hand2")
        self.pause_knopf.pack(fill="x", padx=20, pady=(16, 12))

        # Hinweis (nur bei Bedarf sichtbar)
        self.hinweis = ctk.CTkFrame(m, fg_color="#2A2213", corner_radius=RADIUS, border_width=1,
                                    border_color="#6B5320")
        self.hinweis_text = ctk.CTkLabel(self.hinweis, text="", text_color="#F3E3BD", font=schrift(12),
                                         wraplength=380, justify="left", anchor="w")
        self.hinweis_text.pack(fill="x", padx=12, pady=8)

        # Reiterleiste
        self.reiter_leiste = ctk.CTkFrame(m, fg_color=PANEL, corner_radius=RADIUS + 2, border_width=1,
                                          border_color=RAND)
        self.reiter_leiste.pack(fill="x", padx=20, pady=(0, 14))
        self.reiter_knoepfe = {}
        for i, (k, t) in enumerate(REITER):
            self.reiter_leiste.grid_columnconfigure(i, weight=1, uniform="reiter")
            b = ctk.CTkButton(self.reiter_leiste, text=t, command=lambda k=k: self.reiter_zeigen(k),
                              corner_radius=10, height=56, border_width=2, font=schrift(12, True),
                              cursor="hand2")
            b.grid(row=0, column=i, sticky="ew", pady=6,
                   padx=(6 if i == 0 else 3, 6 if i == len(REITER) - 1 else 3))
            self.reiter_knoepfe[k] = b

        # Fläche für die Reiter: alle liegen übereinander, der gewählte kommt nach vorne.
        # So bleibt das Fenster beim Umschalten gleich groß.
        flaeche = ctk.CTkFrame(m, fg_color=BG, corner_radius=0)
        flaeche.pack(fill="both", padx=20, pady=(0, 16))
        flaeche.grid_columnconfigure(0, weight=1)
        flaeche.grid_rowconfigure(0, weight=1)
        self.reiter = {}
        for k, _ in REITER:
            f = ctk.CTkFrame(flaeche, fg_color=BG, corner_radius=0)
            f.grid(row=0, column=0, sticky="nsew")
            self.reiter[k] = f
        self.inhalt = self.reiter["spiele"]

        # ---------------- Reiter: Spiele
        sp = self.reiter["spiele"]
        box = karte(sp)
        box.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(box, text="ZULETZT VORGELESEN", text_color=BLASS, font=schrift(11, True),
                     anchor="w").pack(fill="x", padx=16, pady=(12, 2))
        self.letzter_label = ctk.CTkLabel(box, text="Noch nichts vorgelesen.", text_color=TEXT,
                                          font=schrift(16), wraplength=370, justify="left", anchor="w")
        self.letzter_label.pack(fill="x", padx=16)
        reihe_l = ctk.CTkFrame(box, fg_color="transparent")
        reihe_l.pack(fill="x", padx=16, pady=(10, 14))
        knopf(reihe_l, "↻  Nochmal vorlesen", self.wiederholen, width=170).pack(side="left")
        self.verlauf_knopf = knopf(reihe_l, "Verlauf (0)", self.verlauf_zeigen, width=120,
                                   fg_color="transparent", hover_color=RAND)
        self.verlauf_knopf.pack(side="left", padx=(8, 0))

        ueberschrift(sp, "Textfeld im Spiel").pack(fill="x")
        rahmen_v = ctk.CTkFrame(sp, fg_color=GRUEN, corner_radius=RADIUS + 2)
        rahmen_v.pack(fill="x", pady=(4, 8))
        self.vorschau = ctk.CTkLabel(rahmen_v, text="Noch kein Textfeld markiert", text_color=BLASS,
                                     font=schrift(12), height=84, fg_color=PANEL, corner_radius=RADIUS)
        self.vorschau.pack(fill="x", padx=2, pady=2)
        self.profil_menue = ctk.CTkOptionMenu(sp, values=[""], command=self._profil_menue_gewaehlt,
                                              corner_radius=10, height=40, fg_color=KNOPF, button_color=GRAU,
                                              button_hover_color="#4A5060", text_color=TEXT,
                                              dropdown_fg_color=PANEL, dropdown_hover_color=GRAU,
                                              dropdown_text_color=TEXT, font=schrift(13),
                                              dropdown_font=schrift(13), dynamic_resizing=False, width=150)
        self.profil_menue.pack(fill="x")
        self.spiel_uebers_var = tk.IntVar(value=1 if self.einst.profil.get("uebersetzen") else 0)
        ctk.CTkCheckBox(sp, text="Englische Texte ins Deutsche übersetzen", variable=self.spiel_uebers_var,
                        onvalue=1, offvalue=0, command=self._spiel_uebersetzen_setzen,
                        font=schrift(12), text_color="#B7BCC9", fg_color=GRUEN, hover_color="#5BE79B",
                        border_color=GRAU, checkmark_color=DUNKEL, corner_radius=6).pack(fill="x", pady=(10, 2))
        self.markier_knopf = knopf(sp, "", self.markieren, border_width=2, border_color=GRUEN)
        self.markier_knopf.pack(fill="x", pady=(8, 0))
        self.motor_label = ctk.CTkLabel(sp, text="Texterkennung: …", text_color=BLASS,
                                        font=schrift(11), anchor="w")
        self.motor_label.pack(fill="x", pady=(4, 0))

        # ---------------- Reiter: Desktop und VR (noch in Planung)
        self._desktop_reiter(self.reiter["desktop"])
        self._vr_reiter(self.reiter["vr"])

        # ---------------- Reiter: Einstellungen
        es = self.reiter["einst"]
        ueberschrift(es, "Stimme").pack(fill="x")
        reihe = ctk.CTkFrame(es, fg_color="transparent")
        reihe.pack(fill="x", pady=(4, 8))
        reihe.grid_columnconfigure((0, 1), weight=1)
        self.knopf_w = ctk.CTkButton(reihe, command=lambda: self.stimme_setzen("w"))
        self.knopf_m = ctk.CTkButton(reihe, command=lambda: self.stimme_setzen("m"))
        for i, b in enumerate((self.knopf_w, self.knopf_m)):
            b.configure(corner_radius=RADIUS, height=50, border_width=2, font=schrift(14, True), cursor="hand2")
            b.grid(row=0, column=i, sticky="ew", padx=(0, 6) if i == 0 else (6, 0))

        self._regler(es, "Tempo", -50, 50, self.einst["tempo"], self._tempo_geaendert)
        self._regler(es, "Lautstärke", 0, 100, self.einst["lautstaerke"], self._laut_geaendert)
        reihe_p = ctk.CTkFrame(es, fg_color="transparent")
        reihe_p.pack(fill="x", pady=(8, 16))
        knopf(reihe_p, "▶  Probe hören", self.probe, width=140).pack(side="left")
        self.unterbrechen_var = tk.IntVar(value=1 if self.einst["unterbrechen"] else 0)
        ctk.CTkCheckBox(reihe_p, text="Neuer Text bricht alten ab", variable=self.unterbrechen_var,
                        onvalue=1, offvalue=0, font=schrift(12), text_color="#B7BCC9",
                        fg_color=GRUEN, hover_color="#5BE79B", border_color=GRAU, checkmark_color=DUNKEL,
                        corner_radius=6,
                        command=lambda: self.einst.__setitem__("unterbrechen", bool(self.unterbrechen_var.get()))
                        ).pack(side="left", padx=(14, 0))

        ueberschrift(es, "Bei Pause / Weiter").pack(fill="x")
        anzeige = {"ton": "Ton", "ansage": "Ansage", "beides": "Beides", "aus": "Aus"}
        self.rueckmeldung_wahl = ctk.CTkSegmentedButton(
            es, values=["Ton", "Ansage", "Beides", "Aus"], command=self._rueckmeldung_gewaehlt,
            font=schrift(13, True), height=40, corner_radius=10, fg_color=KNOPF, selected_color=GRUEN,
            selected_hover_color="#5BE79B", unselected_color=KNOPF, unselected_hover_color=GRAU,
            text_color=TEXT, text_color_disabled=BLASS)
        self.rueckmeldung_wahl.set(anzeige.get(self.einst["rueckmeldung"], "Ton"))
        self.rueckmeldung_wahl.pack(fill="x", pady=(4, 16))

        ueberschrift(es, "Texterkennung läuft auf").pack(fill="x")
        self.rechner_wahl = ctk.CTkSegmentedButton(
            es, values=["Automatisch", "Prozessor", "Grafikkarte"], command=self._rechner_gewaehlt,
            font=schrift(13, True), height=40, corner_radius=10, fg_color=KNOPF, selected_color=GRUEN,
            selected_hover_color="#5BE79B", unselected_color=KNOPF, unselected_hover_color=GRAU,
            text_color=TEXT, text_color_disabled=BLASS)
        self.rechner_wahl.set({"auto": "Automatisch", "cpu": "Prozessor", "gpu": "Grafikkarte"}
                              .get(self.einst["rechner"], "Automatisch"))
        self.rechner_wahl.pack(fill="x", pady=(4, 2))
        self.rechner_info = ctk.CTkLabel(es, text="", font=schrift(12), text_color=BLASS, wraplength=380,
                                         justify="left", anchor="w")
        self.rechner_info.pack(fill="x", pady=(0, 16))
        self._rechner_info_setzen()
        self._rechner_arbeitet = False
        self.root.after(2000, self._rechner_pruefen)

        ueberschrift(es, "Fenster und Tasten").pack(fill="x")
        reihe_f = ctk.CTkFrame(es, fg_color="transparent")
        reihe_f.pack(fill="x", pady=(4, 8))
        reihe_f.grid_columnconfigure((0, 1), weight=1)
        knopf(reihe_f, "⌨  Tasten einstellen", self.tasten_zeigen).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        knopf(reihe_f, "Mini-Leiste ↘", self.mini_zeigen).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.sichtbar_var = tk.IntVar(value=1 if self.einst["in_uebertragung_sichtbar"] else 0)
        ctk.CTkCheckBox(es, text="Fenster in Bildschirmübertragung zeigen (z. B. Discord)",
                        variable=self.sichtbar_var, onvalue=1, offvalue=0, command=self.sichtbarkeit_umschalten,
                        font=schrift(12), text_color="#B7BCC9", fg_color=GRUEN, hover_color="#5BE79B",
                        border_color=GRAU, checkmark_color=DUNKEL, corner_radius=6).pack(fill="x", pady=(4, 0))

        gemerkt = self.einst["reiter"] if self.einst["reiter"] in self.reiter else "spiele"
        self.reiter_zeigen(gemerkt)

        self.scroll_rahmen.pack(fill="both", expand=True)

        self._profile_aktualisieren()
        self._anzeige_aktualisieren()
        self.root.after(250, lambda: self._groesse_anpassen(erzwingen=True))
        self.root.after(400, self._bildschirm_pruefen)

    def reiter_zeigen(self, name):
        if name not in self.reiter:
            return
        self.reiter[name].tkraise()
        for k, b in self.reiter_knoepfe.items():
            if k == name:
                b.configure(fg_color="#1E2A22", hover_color="#24352A", border_color=GRUEN, text_color=TEXT)
            else:
                b.configure(fg_color=PANEL, hover_color=RAND, border_color=PANEL, text_color=BLASS)
        if self.einst["reiter"] != name:
            self.einst["reiter"] = name
        # Spiele / Desktop / VR bestimmen, was vorliest; Einstellungen lässt es, wie es war
        if name in ("spiele", "desktop", "vr") and self.einst["modus"] != name:
            self.einst["modus"] = name
            self._modus_gewechselt()

    def _modus_gewechselt(self):
        modus = self.einst["modus"]
        self.leser.modus_aus = modus != "spiele"
        self.sprecher.stoppen()
        self._rahmen_weg()
        self._maus_ruhe = (None, 0, False)
        self._desktop_aktiv = False
        self.leser.zuruecksetzen.set()
        if getattr(self.leser, "_ueber_gemeldet", False):
            self.leser._ueber_gemeldet = False
            self._n_hinweis("")
        self._vr_aktiv_spricht = False
        if modus == "desktop":
            threading.Thread(target=self.desktop._uia_laden, daemon=True).start()
            self.einblendung("Aktiv: Vorlesen mit der Maus", GRUEN)
        elif modus == "vr":
            self.einblendung("Aktiv: Vorlesen in VR", GRUEN)
        else:
            self.einblendung("Aktiv: Spiele", GRUEN)
        log("Modus:", modus)
        self._vr_zustand()
        self._anzeige_aktualisieren()

    # -------------------------------------------------------- Reiter Desktop (Maus-Vorlesen)
    def _desktop_reiter(self, ds):
        d = self.einst["desktop"]
        box = karte(ds)
        box.pack(fill="x", pady=(0, 16))
        oben = ctk.CTkFrame(box, fg_color="transparent")
        oben.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(oben, text="🖱  Vorlesen mit der Maus", text_color=TEXT, font=schrift(16, True)).pack(side="left")
        ctk.CTkLabel(box, text="Zeig mit der Maus auf einen Text – in Programmen, auf Webseiten, in Ordnern "
                               "und Menüs. Aktiv, solange dieser Reiter offen ist. Das Vorlesen im Spiel "
                               "ruht dann – zurück im Reiter Spiele ist es umgekehrt.",
                     text_color="#B7BCC9", font=schrift(12), wraplength=360, justify="left",
                     anchor="w").pack(fill="x", padx=16, pady=(0, 14))

        ueberschrift(ds, "So wird vorgelesen").pack(fill="x")
        self.desktop_modus = ctk.CTkSegmentedButton(
            ds, values=["Mit Taste", "Automatisch"], command=self._desktop_modus_gewaehlt,
            font=schrift(13, True), height=40, corner_radius=10, fg_color=KNOPF, selected_color=GRUEN,
            selected_hover_color="#5BE79B", unselected_color=KNOPF, unselected_hover_color=GRAU,
            text_color=TEXT, text_color_disabled=BLASS)
        self.desktop_modus.pack(fill="x", pady=(4, 10))

        # je nach Art: Taste oder Wartezeit
        self.desktop_taste_rahmen = ctk.CTkFrame(ds, fg_color="transparent")
        self.desktop_taste_label = ctk.CTkLabel(self.desktop_taste_rahmen, text="", font=schrift(13),
                                                text_color=TEXT, wraplength=380, justify="left", anchor="w")
        self.desktop_taste_label.pack(fill="x")
        knopf(self.desktop_taste_rahmen, "⌨  Taste festlegen", self.tasten_zeigen, width=170).pack(
            anchor="w", pady=(8, 0))
        ctk.CTkLabel(self.desktop_taste_rahmen, text="Nochmal drücken stoppt das Vorlesen.", font=schrift(12),
                     text_color=BLASS, anchor="w").pack(fill="x", pady=(6, 0))
        self.desktop_auto_rahmen = ctk.CTkFrame(ds, fg_color="transparent")
        ctk.CTkLabel(self.desktop_auto_rahmen, text="Lass die Maus kurz still auf einem Text liegen – "
                                                    "dann wird er vorgelesen.",
                     font=schrift(13), text_color=TEXT, wraplength=380, justify="left", anchor="w").pack(fill="x")
        self._regler(self.desktop_auto_rahmen, "Wartezeit", 5, 30, round(float(d.get("wartezeit", 1.0)) * 10),
                     self._wartezeit_geaendert)
        ctk.CTkLabel(self.desktop_auto_rahmen, text="Über laufenden Spielen liest es dabei nichts vor. "
                                                    "Pausieren stoppt auch das automatische Vorlesen.",
                     font=schrift(12), text_color=BLASS, wraplength=380, justify="left",
                     anchor="w").pack(fill="x", pady=(2, 0))
        self.desktop_platz = ctk.CTkFrame(ds, fg_color="transparent", height=1)
        self.desktop_platz.pack(fill="x")

        self.desktop_uebers_var = tk.IntVar(value=1 if d.get("uebersetzen", True) else 0)
        ctk.CTkCheckBox(ds, text="Englische Texte ins Deutsche übersetzen", variable=self.desktop_uebers_var,
                        onvalue=1, offvalue=0,
                        command=lambda: self._desktop_setzen("uebersetzen", bool(self.desktop_uebers_var.get())),
                        font=schrift(12), text_color="#B7BCC9", fg_color=GRUEN, hover_color="#5BE79B",
                        border_color=GRAU, checkmark_color=DUNKEL, corner_radius=6).pack(fill="x", pady=(14, 0))
        self.rahmen_var = tk.IntVar(value=1 if d.get("rahmen", True) else 0)
        ctk.CTkCheckBox(ds, text="Rahmen um den gelesenen Text zeigen", variable=self.rahmen_var,
                        onvalue=1, offvalue=0, command=self._rahmen_umschalten,
                        font=schrift(12), text_color="#B7BCC9", fg_color=GRUEN, hover_color="#5BE79B",
                        border_color=GRAU, checkmark_color=DUNKEL, corner_radius=6).pack(fill="x", pady=(8, 14))

        ueberschrift(ds, "Zuletzt gelesen").pack(fill="x")
        self.desktop_letzter_label = ctk.CTkLabel(ds, text="Noch nichts.", font=schrift(13), text_color="#B7BCC9",
                                                  wraplength=380, justify="left", anchor="w")
        self.desktop_letzter_label.pack(fill="x", pady=(4, 0))
        self._desktop_anzeige()

    def _desktop_setzen(self, schluessel, wert):
        d = dict(self.einst["desktop"])
        d[schluessel] = wert
        self.einst["desktop"] = d

    def _desktop_anzeige(self):
        if not hasattr(self, "desktop_modus"):
            return
        d = self.einst["desktop"]
        auto = d.get("modus") == "auto"
        self.desktop_modus.set("Automatisch" if auto else "Mit Taste")
        self.desktop_taste_rahmen.pack_forget()
        self.desktop_auto_rahmen.pack_forget()
        (self.desktop_auto_rahmen if auto else self.desktop_taste_rahmen).pack(fill="x", before=self.desktop_platz)
        taste = self.einst["tasten"].get("maus")
        if taste:
            self.desktop_taste_label.configure(
                text=f"Zeig auf einen Text und drück  {taste_anzeigen(taste)}.", text_color=TEXT)
        else:
            self.desktop_taste_label.configure(
                text="Noch keine Taste festgelegt. Klick auf „Taste festlegen“ und wähle eine für "
                     "„Text unter der Maus“.", text_color=GELB)

    def _desktop_modus_gewaehlt(self, wert):
        self._desktop_setzen("modus", "auto" if wert == "Automatisch" else "taste")
        self._maus_ruhe = (None, 0, False)
        self._desktop_anzeige()

    def _wartezeit_geaendert(self, v, label):
        v = int(round(float(v)))
        label.configure(text=f"{v / 10:.1f} Sekunden".replace(".", ","))
        if hasattr(self, "desktop_modus") and abs(self.einst["desktop"].get("wartezeit", 1.0) - v / 10) > 0.01:
            self._desktop_setzen("wartezeit", v / 10)

    def _rahmen_umschalten(self):
        self._desktop_setzen("rahmen", bool(self.rahmen_var.get()))

    def _ist_eigenes_fenster(self, x, y):
        h = fenster_an_punkt(x, y)
        return bool(h) and int(h) in self.leser.eigene_fenster

    def maus_taste(self):
        if self.einst["modus"] != "desktop":
            self.einblendung("Maus-Vorlesen: erst Reiter Desktop öffnen", GELB)
            return
        if self._desktop_aktiv and self.sprecher.beschaeftigt():
            self.sprecher.stoppen()
            self._desktop_aktiv = False
            self._rahmen_weg()
            return
        x, y = maus_position()
        if self._ist_eigenes_fenster(x, y):
            log("Maus-Taste: Maus liegt auf dem Lesehelfer-Fenster – nichts zu lesen")
            return
        log(f"Maus-Taste bei {x},{y}")
        self.desktop.anfragen(x, y, False)

    def _maus_beobachten(self):
        try:
            d = self.einst["desktop"]
            if self.einst["modus"] == "desktop" and d.get("modus") == "auto" and not self.leser.pause and not self.taste_wird_gelesen:
                pos = maus_position()
                alt, seit, gelesen = self._maus_ruhe
                if alt is None or abs(pos[0] - alt[0]) > 4 or abs(pos[1] - alt[1]) > 4:
                    self._maus_ruhe = (pos, time.time(), False)
                elif not gelesen and time.time() - seit >= float(d.get("wartezeit", 1.0)):
                    self._maus_ruhe = (alt, seit, True)
                    if not self._ist_eigenes_fenster(*pos):
                        self.desktop.anfragen(pos[0], pos[1], True)
        except Exception as e:
            log("Maus beobachten:", e)
        self.root.after(150, self._maus_beobachten)

    def _n_desktop_text(self, wert):
        text, rechteck, automatisch, sprache = wert
        if not text:
            if not automatisch:
                log("Desktop: kein Text gefunden")
                self.einblendung("Hier wurde kein Text gefunden", GELB)
            return
        if automatisch:
            alt, wann = self._desktop_letzter
            if text == alt and time.time() - wann < 60:
                return
            # Maus inzwischen woanders? Dann nicht mehr vorlesen.
            pos = self._maus_ruhe[0]
            jetzt = maus_position()
            if pos and (abs(jetzt[0] - pos[0]) > 4 or abs(jetzt[1] - pos[1]) > 4):
                return
        self._desktop_letzter = (text, time.time())
        self.sprecher.sagen(text, unterbrechen=True, sprache=sprache)
        self._desktop_aktiv = True
        if rechteck and self.einst["desktop"].get("rahmen", True):
            self._rahmen_zeigen(rechteck)
        kurz = text if len(text) <= 200 else text[:197].rsplit(" ", 1)[0] + " …"
        self.desktop_letzter_label.configure(text=f"„{kurz}“", text_color=TEXT)

    def _rahmen_zeigen(self, r):
        try:
            self._rahmen_weg()
            x1, y1, x2, y2 = r
            rand = 5
            w, h = max(8, x2 - x1 + 2 * rand), max(8, y2 - y1 + 2 * rand)
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.configure(bg=DURCHSICHTIG)
            t.attributes("-transparentcolor", DURCHSICHTIG)
            tk.Tk.wm_geometry(t, f"{w}x{h}+{x1 - rand}+{y1 - rand}")
            c = tk.Canvas(t, width=w, height=h, bg=DURCHSICHTIG, highlightthickness=0, bd=0)
            c.pack()
            c.create_rectangle(2, 2, w - 2, h - 2, outline=GRUEN, width=3)
            t.update_idletasks()
            try:
                hwnd = int(t.wm_frame(), 16)
                u = ctypes.windll.user32
                ex = u.GetWindowLongW(hwnd, -20)
                # durchklickbar, nimmt keinen Fokus, nicht in der Taskleiste
                u.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x08000000 | 0x80)
                u.SetWindowDisplayAffinity(hwnd, 0x11)   # nicht im eigenen Bildschirmfoto
            except Exception:
                pass
            self._rahmen_fenster = t
            self._rahmen_timer = self.root.after(1800, self._rahmen_weg)
        except Exception as e:
            log("Rahmen anzeigen:", e)

    def _rahmen_weg(self):
        if getattr(self, "_rahmen_timer", None):
            try:
                self.root.after_cancel(self._rahmen_timer)
            except Exception:
                pass
            self._rahmen_timer = None
        if self._rahmen_fenster:
            try:
                self._rahmen_fenster.destroy()
            except Exception:
                pass
            self._rahmen_fenster = None

    # -------------------------------------------------------- Reiter VR
    def _vr_reiter(self, vs):
        v = self.einst["vr"]
        box = karte(vs)
        box.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(box, text="🥽  Vorlesen in VR", text_color=TEXT, font=schrift(16, True),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(box, text="Schau auf einen Text und drück deine Controller-Taste – vorgelesen wird, "
                               "was in der Mitte deines Blicks steht. Aktiv, solange dieser Reiter offen ist.",
                     text_color="#B7BCC9", font=schrift(12), wraplength=360, justify="left",
                     anchor="w").pack(fill="x", padx=16)
        self.vr_status_steam = ctk.CTkLabel(box, text="", font=schrift(12, True), anchor="w", justify="left")
        self.vr_status_steam.pack(fill="x", padx=16, pady=(10, 0))
        self.vr_status_fenster = ctk.CTkLabel(box, text="", font=schrift(12, True), anchor="w", justify="left",
                                              wraplength=360)
        self.vr_status_fenster.pack(fill="x", padx=16, pady=(2, 0))
        ctk.CTkLabel(box, text="VR-Ansicht öffnen: im SteamVR-Fenster am PC ☰ → „VR-Ansicht anzeigen“. "
                               "Sie darf auf dem zweiten Bildschirm liegen, aber nicht minimiert sein.",
                     text_color=BLASS, font=schrift(11), wraplength=360, justify="left",
                     anchor="w").pack(fill="x", padx=16, pady=(6, 0))
        reihe = ctk.CTkFrame(box, fg_color="transparent")
        reihe.pack(fill="x", padx=16, pady=(8, 14))
        self.vr_fenster_menue = ctk.CTkOptionMenu(reihe, values=["Automatisch suchen"],
                                                  command=self._vr_fenster_gewaehlt, corner_radius=10, height=36,
                                                  fg_color=KNOPF, button_color=GRAU, button_hover_color="#4A5060",
                                                  text_color=TEXT, dropdown_fg_color=PANEL,
                                                  dropdown_hover_color=GRAU, dropdown_text_color=TEXT,
                                                  font=schrift(12), dropdown_font=schrift(12),
                                                  dynamic_resizing=False, width=200)
        self.vr_fenster_menue.pack(side="left", fill="x", expand=True)
        knopf(reihe, "↻ Suchen", self._vr_fenster_liste, width=90, height=36).pack(side="left", padx=(6, 0))

        ueberschrift(vs, "So wird vorgelesen").pack(fill="x")
        self.vr_modus = ctk.CTkSegmentedButton(
            vs, values=["Mit Taste", "Automatisch"], command=self._vr_modus_gewaehlt,
            font=schrift(13, True), height=40, corner_radius=10, fg_color=KNOPF, selected_color=GRUEN,
            selected_hover_color="#5BE79B", unselected_color=KNOPF, unselected_hover_color=GRAU,
            text_color=TEXT, text_color_disabled=BLASS)
        self.vr_modus.pack(fill="x", pady=(4, 10))
        self.vr_taste_rahmen = ctk.CTkFrame(vs, fg_color="transparent")
        self.vr_knopf_label = ctk.CTkLabel(self.vr_taste_rahmen, text="", font=schrift(13), text_color=TEXT,
                                           wraplength=380, justify="left", anchor="w")
        self.vr_knopf_label.pack(fill="x")
        reihe_k = ctk.CTkFrame(self.vr_taste_rahmen, fg_color="transparent")
        reihe_k.pack(fill="x", pady=(8, 0))
        self.vr_anlern_knopf = knopf(reihe_k, "🎮  Taste in SteamVR ändern", self._vr_belegung, width=220)
        self.vr_anlern_knopf.pack(side="left")
        knopf(reihe_k, "⌨  Tastatur", self.tasten_zeigen, width=110).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(self.vr_taste_rahmen, text="Ändern geht auch so: SteamVR → Einstellungen → Controller → "
                                               "Controllerbelegungen verwalten → Lesehelfer. "
                                               "Nochmal drücken stoppt das Vorlesen.",
                     font=schrift(11), text_color=BLASS, wraplength=380, justify="left",
                     anchor="w").pack(fill="x", pady=(6, 0))
        self.vr_auto_rahmen = ctk.CTkFrame(vs, fg_color="transparent")
        ctk.CTkLabel(self.vr_auto_rahmen, text="Liegt ein Text eine Weile ruhig in deiner Blickmitte, wird er "
                                              "einmal vorgelesen.", font=schrift(13), text_color=TEXT,
                     wraplength=380, justify="left", anchor="w").pack(fill="x")
        self._regler(self.vr_auto_rahmen, "Wartezeit", 5, 40, round(float(v.get("wartezeit", 1.5)) * 10),
                     self._vr_wartezeit_geaendert)
        self.vr_platz = ctk.CTkFrame(vs, fg_color="transparent", height=1)
        self.vr_platz.pack(fill="x")

        ueberschrift(vs, "Größe der Blickmitte").pack(fill="x", pady=(12, 0))
        self.vr_bereich = ctk.CTkSegmentedButton(
            vs, values=["Klein", "Mittel", "Groß"], command=self._vr_bereich_gewaehlt,
            font=schrift(13, True), height=36, corner_radius=10, fg_color=KNOPF, selected_color=GRUEN,
            selected_hover_color="#5BE79B", unselected_color=KNOPF, unselected_hover_color=GRAU,
            text_color=TEXT, text_color_disabled=BLASS)
        self.vr_bereich.pack(fill="x", pady=(4, 0))
        self.vr_uebers_var = tk.IntVar(value=1 if v.get("uebersetzen", True) else 0)
        ctk.CTkCheckBox(vs, text="Englische Texte ins Deutsche übersetzen", variable=self.vr_uebers_var,
                        onvalue=1, offvalue=0,
                        command=lambda: self._vr_setzen("uebersetzen", bool(self.vr_uebers_var.get())),
                        font=schrift(12), text_color="#B7BCC9", fg_color=GRUEN, hover_color="#5BE79B",
                        border_color=GRAU, checkmark_color=DUNKEL, corner_radius=6).pack(fill="x", pady=(12, 12))
        ueberschrift(vs, "Zuletzt gelesen").pack(fill="x")
        self.vr_letzter_label = ctk.CTkLabel(vs, text="Noch nichts.", font=schrift(13), text_color="#B7BCC9",
                                             wraplength=380, justify="left", anchor="w")
        self.vr_letzter_label.pack(fill="x", pady=(4, 0))
        self._vr_fenster_liste()
        self._vr_anzeige()
        self.root.after(2000, self._vr_fenster_pruefen)
        self._n_vr_status(self.vr_controller.status or "aus")

    def _vr_setzen(self, schluessel, wert):
        v = dict(self.einst["vr"])
        v[schluessel] = wert
        self.einst["vr"] = v

    def _vr_anzeige(self):
        if not hasattr(self, "vr_modus"):
            return
        v = self.einst["vr"]
        auto = v.get("modus") == "auto"
        self.vr_modus.set("Automatisch" if auto else "Mit Taste")
        self.vr_taste_rahmen.pack_forget()
        self.vr_auto_rahmen.pack_forget()
        (self.vr_auto_rahmen if auto else self.vr_taste_rahmen).pack(fill="x", before=self.vr_platz)
        self.vr_bereich.set({"klein": "Klein", "gross": "Groß"}.get(v.get("bereich"), "Mittel"))
        taste = self.einst["tasten"].get("vr")
        teile = ["Controller: rechten Stick drücken (Standard – in SteamVR änderbar)"]
        if taste:
            teile.append(f"Tastatur: {taste_anzeigen(taste)}")
        self.vr_knopf_label.configure(text="\n".join(teile), text_color=TEXT)

    def _vr_zustand(self):
        """VR-Teile ein-/ausschalten, je nach Modus und Pause."""
        vr = self.einst["modus"] == "vr"
        self.vr_controller.aktiv = vr
        self.vr.automatik = vr and self.einst["vr"].get("modus") == "auto" and not self.leser.pause

    def _vr_modus_gewaehlt(self, wert):
        self._vr_setzen("modus", "auto" if wert == "Automatisch" else "taste")
        self._vr_anzeige()
        self._vr_zustand()

    def _vr_bereich_gewaehlt(self, wert):
        self._vr_setzen("bereich", {"Klein": "klein", "Groß": "gross"}.get(wert, "mittel"))

    def _vr_wartezeit_geaendert(self, v, label):
        v = int(round(float(v)))
        label.configure(text=f"{v / 10:.1f} Sekunden".replace(".", ","))
        if hasattr(self, "vr_modus") and abs(self.einst["vr"].get("wartezeit", 1.5) - v / 10) > 0.01:
            self._vr_setzen("wartezeit", v / 10)

    def _vr_fenster_liste(self):
        titel = sorted({t for _, t in sichtbare_fenster() if not ist_eigener_titel(t)})
        werte = ["Automatisch suchen"] + titel[:40]
        self.vr_fenster_menue.configure(values=werte)
        gewaehlt = self.einst["vr"].get("fenster", "")
        self.vr_fenster_menue.set(gewaehlt if gewaehlt in werte else "Automatisch suchen")
        self._vr_fenster_pruefen(einmal=True)

    def _vr_fenster_gewaehlt(self, wert):
        self._vr_setzen("fenster", "" if wert == "Automatisch suchen" else wert)
        self._vr_fenster_pruefen(einmal=True)

    def _vr_fenster_pruefen(self, einmal=False):
        try:
            h, t = vr_fenster_finden(self.einst["vr"].get("fenster", ""))
            if h:
                self.vr_status_fenster.configure(text=f"✓  VR-Ansicht gefunden: {t}", text_color=GRUEN)
            else:
                self.vr_status_fenster.configure(text="✗  Keine VR-Ansicht gefunden", text_color=GELB)
        except Exception as e:
            log("VR-Fenster prüfen:", e)
        if not einmal:
            self.root.after(3000, self._vr_fenster_pruefen)

    def _n_vr_status(self, s):
        texte = {"verbunden": ("✓  SteamVR verbunden", GRUEN),
                 "kein_steamvr": ("✗  SteamVR läuft nicht", GELB),
                 "fehlt": ("✗  VR-Unterstützung konnte nicht installiert werden", GELB),
                 "aus": ("SteamVR: wird verbunden, sobald der Reiter VR offen ist", BLASS)}
        text, farbe = texte.get(s, ("", BLASS))
        if hasattr(self, "vr_status_steam"):
            self.vr_status_steam.configure(text=text, text_color=farbe)

    def _vr_belegung(self):
        if self.einst["modus"] != "vr" or self.vr_controller.status != "verbunden":
            self.einblendung("Erst SteamVR starten und Reiter VR öffnen", GELB)
            return
        self.vr_controller.belegung_oeffnen = True
        self.einblendung("SteamVR-Belegung wird geöffnet …", GRUEN)

    def _n_vr_belegung_fehler(self, _):
        messagebox.showinfo("Lesehelfer", "Die Belegung ließ sich nicht direkt öffnen.\n\n"
                            "So geht es von Hand: SteamVR → Einstellungen → Controller → "
                            "Controllerbelegungen verwalten → Lesehelfer.", parent=self.root)

    def vr_taste(self):
        if self.einst["modus"] != "vr":
            self.einblendung("VR-Vorlesen: erst Reiter VR öffnen", GELB)
            return
        if self._vr_aktiv_spricht and self.sprecher.beschaeftigt():
            self.sprecher.stoppen()
            self._vr_aktiv_spricht = False
            return
        log("VR-Taste gedrückt")
        self.vr.anfragen()

    def _n_vr_text(self, wert):
        text, automatisch, sprache, fehler = wert
        if self.einst["modus"] != "vr":
            return
        if not text:
            if not automatisch:
                # Wer die Brille aufhat, sieht keine Einblendung -> kurz ansagen
                meldung = {"fenster": "Keine VR-Ansicht gefunden.",
                           "minimiert": "Die VR-Ansicht ist minimiert."}.get(fehler, "Kein Text gefunden.")
                log("VR:", meldung)
                self.sprecher.sagen(meldung)
            return
        self.sprecher.sagen(text, unterbrechen=True, sprache=sprache)
        self._vr_aktiv_spricht = True
        kurz = text if len(text) <= 200 else text[:197].rsplit(" ", 1)[0] + " …"
        self.vr_letzter_label.configure(text=f"„{kurz}“", text_color=TEXT)

    def _kommt_bald(self, eltern, symbol, titel, text, punkte):
        box = karte(eltern)
        box.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(box, text=symbol, font=schrift(34), text_color=BLASS).pack(pady=(22, 4))
        ctk.CTkLabel(box, text="Kommt bald", font=schrift(12, True), text_color="#F3E3BD",
                     fg_color="#2A2213", corner_radius=12, height=26, width=110).pack(pady=(0, 8))
        ctk.CTkLabel(box, text=titel, font=schrift(18, True), text_color=TEXT).pack()
        ctk.CTkLabel(box, text=text, font=schrift(13), text_color="#B7BCC9", wraplength=340,
                     justify="center").pack(padx=16, pady=(6, 20))
        ueberschrift(eltern, "Geplant").pack(fill="x")
        for punkt in punkte:
            ctk.CTkLabel(eltern, text="•  " + punkt, font=schrift(13), text_color="#B7BCC9", anchor="w",
                         justify="left", wraplength=380).pack(fill="x", pady=(4, 0))

    def _regler(self, eltern, name, von, bis, wert, befehl):
        rahmen = ctk.CTkFrame(eltern, fg_color="transparent")
        rahmen.pack(fill="x", pady=(4, 6))
        oben = ctk.CTkFrame(rahmen, fg_color="transparent")
        oben.pack(fill="x")
        ctk.CTkLabel(oben, text=name, text_color=TEXT, font=schrift(13, True)).pack(side="left")
        wert_label = ctk.CTkLabel(oben, text="", text_color=GRUEN, font=schrift(13, True))
        wert_label.pack(side="right")
        s = ctk.CTkSlider(rahmen, from_=von, to=bis, number_of_steps=bis - von, height=20,
                          fg_color="#3A3F4D", progress_color=GRUEN, button_color=GRUEN,
                          button_hover_color="#7CF0AE", button_corner_radius=10, button_length=10)
        s.set(wert)
        befehl(wert, wert_label)
        s.configure(command=lambda v: befehl(v, wert_label))
        s.pack(fill="x", pady=(2, 0))

    def _anzeige_aktualisieren(self):
        p = self.leser.pause
        taste = taste_anzeigen(self.einst["tasten"]["pause"])
        if p:
            self.pause_knopf.configure(text=f"  ❚❚   Pausiert\n          Klicken zum Weitermachen ({taste})",
                                       fg_color=GRAU, hover_color="#4A5060", text_color=TEXT)
        else:
            was = {"desktop": "Maus", "vr": "VR"}.get(self.einst["modus"], "Spiel")
            self.pause_knopf.configure(text=f"  ▶   Liest gerade vor · {was}\n          Klicken zum Pausieren ({taste})",
                                       fg_color=GRUEN, hover_color="#5BE79B", text_color=DUNKEL)
        sprache = sprechsprache(self.einst.profil)
        for k, b in (("w", self.knopf_w), ("m", self.knopf_m)):
            liste = KI_ERSATZ[(sprache, k)]
            name = liste[self.sprecher.funktionierend.get((sprache, k), 0)][1]
            text = ("Frau · " if k == "w" else "Mann · ") + name
            if self.einst["stimme"] == k:
                b.configure(text=text, fg_color=GRUEN_HELL, hover_color=GRUEN_HELL, text_color=TEXT,
                            border_color=GRUEN)
            else:
                b.configure(text=text, fg_color=PANEL, hover_color=KNOPF, text_color="#B7BCC9",
                            border_color=RAND)
        prof = self.einst.profil
        if hasattr(self, "spiel_uebers_var"):
            soll = 1 if prof.get("uebersetzen") else 0
            if self.spiel_uebers_var.get() != soll:
                self.spiel_uebers_var.set(soll)
        self.markier_knopf.configure(text="Textfeld markieren (" +
                                     taste_anzeigen(self.einst["tasten"]["markieren"]) + ")")
        if not prof.get("bereich"):
            self.vorschau.configure(image=None, text="Noch kein Textfeld markiert")
            self.vorschau_bild = None
        if self.mini:
            self.mini_pause.configure(text="❚❚" if not p else "▶", fg_color=GRUEN if not p else GRAU,
                                      hover_color="#5BE79B" if not p else "#4A5060",
                                      text_color=DUNKEL if not p else TEXT)
            self.mini_status.configure(text=("Pausiert" if p else "Liest vor") + " · " + self.sprecher.stimmname(),
                                       text_color=BLASS if p else GRUEN)

    # -------------------------------------------------------- Aktionen
    def pause_umschalten(self):
        self.leser.pause = not self.leser.pause
        self.leser.zuruecksetzen.set()
        if self.leser.pause:
            self.sprecher.stoppen()  # angefangenen Satz sofort abbrechen
        self.rueckmeldung("pause" if self.leser.pause else "weiter")
        self._vr_zustand()
        self._anzeige_aktualisieren()

    def rueckmeldung(self, art):
        """Je nach Einstellung: Ton, Ansage, beides oder nichts – dazu eine kurze Einblendung."""
        modus = self.einst["rueckmeldung"]
        ansage, schild = TON_TEXTE[art]
        if modus in ("ton", "beides"):
            ton_spielen(art, self.sprecher.ordner)
        if modus in ("ansage", "beides"):
            self.sprecher.sagen(ansage)
        if modus != "aus":
            self.einblendung(schild, GRUEN if art != "pause" else GRAU)

    def einblendung(self, text, farbe):
        """Kleines rundes Schild unten rechts, verschwindet nach gut einer Sekunde."""
        try:
            if getattr(self, "_schild", None):
                self._schild.destroy()
            DURCHSICHTIG = "#010203"
            t = ctk.CTkToplevel(self.root, fg_color=DURCHSICHTIG)
            self._schild = t
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            try:
                t.attributes("-transparentcolor", DURCHSICHTIG)
            except Exception:
                t.configure(fg_color=BG)
            k = ctk.CTkFrame(t, fg_color=farbe, corner_radius=18)
            k.pack(padx=2, pady=2)
            ctk.CTkLabel(k, text=text, text_color=DUNKEL if farbe == GRUEN else TEXT,
                         font=schrift(18, True)).pack(padx=22, pady=12)
            t.update_idletasks()
            sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
            bw, bh = t.winfo_reqwidth(), t.winfo_reqheight()
            unten = sh - bh - 90 - (self.mini.winfo_height() + 20 if self.mini else 0)
            t.geometry(f"+{sw - bw - 30}+{max(0, unten)}")
            self.fenster_anmelden(t)

            def weg():
                if getattr(self, "_schild", None) is t:
                    self._schild = None
                try:
                    t.destroy()
                except Exception:
                    pass
            t.after(1300, weg)
        except Exception as e:
            log("Einblendung geht nicht:", e)

    # -------------------------------------------------------- Prozessor / Grafikkarte
    def _rechner_info_setzen(self, extra=None):
        ocr = getattr(self.leser, "ocr", None)
        jetzt = geraet_name(ocr.geraet) if hasattr(ocr, "geraet") else "…"
        text = extra or ("Gerade: " + jetzt + ".  Automatisch = Grafikkarte, in VR der Prozessor.")
        self.rechner_info.configure(text=text)

    def _rechner_gewaehlt(self, wahl):
        self.einst["rechner"] = {"Automatisch": "auto", "Prozessor": "cpu", "Grafikkarte": "gpu"}[wahl]
        self.einst.speichern()
        self._rechner_pruefen(sofort=True)

    def _rechner_pruefen(self, sofort=False):
        """Läuft jede Sekunde: Passt das Gerät der Texterkennung noch zur Wahl und zum Modus?"""
        try:
            ocr = getattr(self.leser, "ocr", None)
            if not self._rechner_arbeitet and hasattr(ocr, "neu_starten"):
                wahl = self.einst["rechner"]
                if wahl != "cpu" and not directml_installiert():
                    if sofort:   # nur nach eigener Wahl installieren, nicht von selbst
                        self._rechner_arbeitet = True
                        self._rechner_info_setzen("Grafikkarten-Unterstützung wird installiert (ca. 25 MB) …")
                        threading.Thread(target=self._directml_installieren, daemon=True).start()
                else:
                    soll = gpu_gewuenscht(self.einst)
                    if soll != ocr.gpu_gewuenscht:
                        self._rechner_arbeitet = True
                        self._rechner_info_setzen("Texterkennung wechselt zu: " +
                                                  ("Grafikkarte" if soll else "Prozessor") + " …")
                        threading.Thread(target=self._rechner_wechseln, args=(soll,), daemon=True).start()
                    elif sofort:
                        self._rechner_info_setzen()
        finally:
            if not sofort:
                self.root.after(1000, self._rechner_pruefen)

    def _rechner_wechseln(self, gpu):
        try:
            with self.leser.ocr_sperre:
                self.leser.ocr.neu_starten(gpu)
            g = self.leser.ocr.geraet
            log("Texterkennung jetzt auf:", geraet_name(g))
            self.melden("motor", self.leser.ocr.name + " · " + geraet_name(g))
        except Exception as e:
            log("Wechsel Prozessor/Grafikkarte fehlgeschlagen:", e)
        self.melden("rechner_fertig", None)

    def _directml_installieren(self):
        """onnxruntime (nur Prozessor) gegen onnxruntime-directml (Prozessor + Grafikkarte) tauschen.
        Der Hilfsprozess muss dafür aus sein, sonst sind die Dateien gesperrt."""
        import subprocess
        python = sys.executable
        if python.lower().endswith("pythonw.exe"):
            python = python[:-len("pythonw.exe")] + "python.exe"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        pip = [python, "-m", "pip", "--disable-pip-version-check"]
        ok = False
        with self.leser.ocr_sperre:
            self.leser.ocr.beenden()
            try:
                log("Installiere onnxruntime-directml …")
                subprocess.run(pip + ["uninstall", "-y", "onnxruntime"], creationflags=flags,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(pip + ["install", "--quiet", "onnxruntime-directml"], check=True,
                               creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                ok = True
                log("onnxruntime-directml installiert")
            except Exception as e:
                log("Installation onnxruntime-directml fehlgeschlagen:", e)
                subprocess.run(pip + ["install", "--quiet", "onnxruntime"], creationflags=flags,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.leser.ocr.gpu_gewuenscht = ok and gpu_gewuenscht(self.einst)
                self.leser.ocr._starten()
            except Exception as e:
                log("Texterkennung startet nach Installation nicht:", e)
        g = getattr(self.leser.ocr, "geraet", "cpu")
        self.melden("motor", self.leser.ocr.name + " · " + geraet_name(g))
        if not ok:
            self.melden("rechner_fertig", "Installation hat nicht geklappt – Texterkennung bleibt auf dem Prozessor.")
        else:
            self.melden("rechner_fertig", None)

    def _n_rechner_fertig(self, text):
        self.leser.puls = time.time() + 5
        self._rechner_arbeitet = False
        self._rechner_info_setzen(text)

    def _rueckmeldung_gewaehlt(self, wahl):
        modus = {"Ton": "ton", "Ansage": "ansage", "Beides": "beides", "Aus": "aus"}[wahl]
        self.einst["rueckmeldung"] = modus
        if modus != "aus":
            self.rueckmeldung("weiter")

    def wiederholen(self):
        if self.leser.letzter:
            self.sprecher.sagen(self.leser.letzter)

    def stimme_setzen(self, k):
        self.einst["stimme"] = k
        self._anzeige_aktualisieren()
        self.sprecher.sagen(f"Hallo, ich bin {self.sprecher.stimmname()}.")

    def stimme_umschalten(self):
        self.stimme_setzen("m" if self.einst["stimme"] == "w" else "w")

    def _tempo_geaendert(self, wert, label):
        label.configure(text=tempo_text(wert))
        if int(float(wert)) != self.einst["tempo"]:
            self.einst["tempo"] = int(float(wert))

    def _laut_geaendert(self, wert, label):
        label.configure(text=f"{int(float(wert))} %")
        if int(float(wert)) != self.einst["lautstaerke"]:
            self.einst["lautstaerke"] = int(float(wert))

    def probe(self):
        self.sprecher.sagen("So klinge ich mit diesen Einstellungen." if sprechsprache(self.einst.profil) == "de"
                            else "This is how I sound with these settings.")

    def schrift_umschalten(self):
        p = self.einst.profil
        folge = {"auto": "hell", "hell": "dunkel", "dunkel": "auto"}
        p["schrift"] = folge[p.get("schrift", "auto")]
        self.einst.speichern()
        self.leser.zuruecksetzen.set()
        self._anzeige_aktualisieren()
        self.sprecher.sagen({"auto": "Schrift automatisch.", "hell": "Helle Schrift.",
                             "dunkel": "Dunkle Schrift."}[p["schrift"]])

    def _spiel_uebersetzen_setzen(self):
        an = bool(self.spiel_uebers_var.get())
        self.einst.profil["uebersetzen"] = an
        if an:
            self.leser.uebersetzer.vorbereiten()
        self.einst.speichern()
        self.leser.zuruecksetzen.set()
        self.sprecher.sagen("Englische Texte werden übersetzt." if an else "Übersetzung aus.")

    def sprache_umschalten(self):
        """Deutsch -> Englisch -> Englisch übersetzt ins Deutsche -> Deutsch"""
        p = self.einst.profil
        if p.get("uebersetzen"):
            p["sprache"], p["uebersetzen"] = "de", False
        elif p.get("sprache", "de") == "de":
            p["sprache"], p["uebersetzen"] = "en", False
        else:
            p["sprache"], p["uebersetzen"] = "en", True
            self.leser.uebersetzer.vorbereiten()
        self.einst.speichern()
        self.leser.zuruecksetzen.set()
        self._anzeige_aktualisieren()
        self.probe()

    # -------------------------------------------------------- Spiel-Profile
    MENUE_NEU = "+ Neues Spiel …"
    MENUE_UMBENENNEN = "✎ Umbenennen …"
    MENUE_LOESCHEN = "✕ Dieses Spiel löschen"

    def _profile_aktualisieren(self):
        werte = list(self.einst["profile"]) + [self.MENUE_NEU, self.MENUE_UMBENENNEN]
        if len(self.einst["profile"]) > 1:
            werte.append(self.MENUE_LOESCHEN)
        self.profil_menue.configure(values=werte)
        self.profil_menue.set(self.einst["profil"])

    def _profil_menue_gewaehlt(self, wahl):
        if wahl == self.MENUE_NEU:
            self.profil_menue.set(self.einst["profil"])
            self.profil_neu()
        elif wahl == self.MENUE_UMBENENNEN:
            self.profil_menue.set(self.einst["profil"])
            self.profil_umbenennen()
        elif wahl == self.MENUE_LOESCHEN:
            self.profil_menue.set(self.einst["profil"])
            self.profil_loeschen()
        else:
            self.profil_waehlen(wahl)

    def profil_waehlen(self, name):
        self.einst["profil"] = name
        self.leser.zuruecksetzen.set()
        self._profile_aktualisieren()
        self._anzeige_aktualisieren()

    def profil_neu(self):
        name = simpledialog.askstring("Neues Spiel", "Wie heißt das Spiel?", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        self.einst["profile"].setdefault(name, {"bereich": None, "uebersetzen": bool(self.einst.profil.get("uebersetzen"))})
        self.profil_waehlen(name)
        self.sprecher.sagen("Starte jetzt das Spiel, bis ein Text zu sehen ist, und markiere dann das Textfeld.")

    def profil_umbenennen(self):
        alt = self.einst["profil"]
        name = simpledialog.askstring("Umbenennen", "Neuer Name:", initialvalue=alt, parent=self.root)
        if not name or not name.strip() or name.strip() == alt:
            return
        self.einst["profile"][name.strip()] = self.einst["profile"].pop(alt)
        self.profil_waehlen(name.strip())

    def profil_loeschen(self):
        name = self.einst["profil"]
        if messagebox.askyesno("Löschen", f"„{name}“ wirklich löschen?", parent=self.root):
            del self.einst["profile"][name]
            self.profil_waehlen(next(iter(self.einst["profile"])))

    # -------------------------------------------------------- Bereich markieren
    def markieren(self):
        if getattr(self, "_markiert_gerade", False):
            return
        self._markiert_gerade = True
        mini_war_offen = bool(self.mini)
        if self.mini:
            self.mini.withdraw()
        self.root.withdraw()
        self.root.after(300, lambda: self._markieren_start(mini_war_offen))

    def _markieren_start(self, mini_war_offen):
        """Dunkelt ALLE Bildschirme ab; auf jedem kann das Rechteck gezogen werden."""
        overlays, ergebnis = [], {}

        def fertig():
            if ergebnis.get("_fertig"):
                return
            ergebnis["_fertig"] = True
            for t in overlays:
                try:
                    t.destroy()
                except Exception:
                    pass
            self._markiert_gerade = False
            if mini_war_offen and self.mini:
                self.mini.deiconify()
            else:
                self.root.deiconify()
                self.root.lift()
            ergebnis.pop("_fertig", None)
            if ergebnis:
                self.einst.profil["bereich"] = dict(ergebnis)
                self.einst.speichern()
                self.leser.zuruecksetzen.set()
                self.rueckmeldung("gespeichert")
            ergebnis["_fertig"] = True
            self._anzeige_aktualisieren()

        try:
            with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
                monitore = [(m, sct.grab(m)) for m in sct.monitors[1:]]
            for mon, roh in monitore:
                overlays.append(self._overlay(mon, roh, ergebnis, fertig))
            for t in overlays:
                t.bind("<Escape>", lambda e: fertig())
            self.root.after(150, lambda: overlays and overlays[0].focus_force())
        except Exception as e:
            log("Markieren fehlgeschlagen:", traceback.format_exc())
            fertig()
            messagebox.showerror("Lesehelfer", f"Markieren ging nicht: {e}", parent=self.root)

    def _overlay(self, mon, roh, ergebnis, fertig):
        hell = Image.frombytes("RGB", roh.size, roh.rgb)
        dunkel = ImageEnhance.Brightness(hell).enhance(0.35)
        ox, oy, W, H = mon["left"], mon["top"], mon["width"], mon["height"]
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"{W}x{H}+{ox}+{oy}")
        top.attributes("-topmost", True)
        top.config(cursor="cross")
        c = tk.Canvas(top, width=W, height=H, highlightthickness=0, bg="black")
        c.pack(fill="both", expand=True)
        top._bilder = {"dunkel": ImageTk.PhotoImage(dunkel, master=top)}
        c.create_image(0, 0, anchor="nw", image=top._bilder["dunkel"])
        c.create_text(W // 2, 50, fill=GRUEN, font=(SCHRIFT, 22, "bold"),
                      text="Rechteck um das Textfeld ziehen   (Esc = abbrechen)")
        start, teile = {}, {"hell": None, "rahmen": None}

        def down(e):
            start["x"], start["y"] = e.x, e.y

        def move(e):
            if "x" not in start:
                return
            x1, y1 = max(0, min(start["x"], e.x)), max(0, min(start["y"], e.y))
            x2, y2 = min(W, max(start["x"], e.x)), min(H, max(start["y"], e.y))
            for k in teile:
                if teile[k]:
                    c.delete(teile[k])
            if x2 - x1 > 2 and y2 - y1 > 2:
                top._bilder["hell"] = ImageTk.PhotoImage(hell.crop((x1, y1, x2, y2)), master=top)
                teile["hell"] = c.create_image(x1, y1, anchor="nw", image=top._bilder["hell"])
            teile["rahmen"] = c.create_rectangle(x1, y1, x2, y2, outline=GRUEN, width=4)

        def up(e):
            if "x" not in start:
                return
            x1, y1 = max(0, min(start["x"], e.x)), max(0, min(start["y"], e.y))
            x2, y2 = min(W, max(start["x"], e.x)), min(H, max(start["y"], e.y))
            if x2 - x1 > 10 and y2 - y1 > 10:
                ergebnis.update(left=ox + x1, top=oy + y1, width=x2 - x1, height=y2 - y1)
                top.after(250, fertig)
            else:
                start.clear()  # nur geklickt: weiter warten

        c.bind("<ButtonPress-1>", down)
        c.bind("<B1-Motion>", move)
        c.bind("<ButtonRelease-1>", up)
        return top

    # -------------------------------------------------------- Mini-Leiste
    def mini_zeigen(self):
        if self.mini:
            return
        DURCHSICHTIG = "#010203"
        m = ctk.CTkToplevel(self.root, fg_color=DURCHSICHTIG)
        self.mini = m
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.attributes("-alpha", 0.92)
        try:
            m.attributes("-transparentcolor", DURCHSICHTIG)  # nur die runde Karte bleibt sichtbar
        except Exception:
            m.configure(fg_color=BG)
        k = karte(m, fg_color=BG, corner_radius=18)
        k.pack(padx=2, pady=2)
        self.mini_pause = ctk.CTkButton(k, command=self.pause_umschalten, font=schrift(20, True),
                                        width=56, height=56, corner_radius=28, cursor="hand2")
        self.mini_pause.pack(side="left", padx=12, pady=12)
        mitte = ctk.CTkFrame(k, fg_color="transparent")
        mitte.pack(side="left", fill="both", expand=True, pady=10)
        self.mini_status = ctk.CTkLabel(mitte, text="", font=schrift(11, True), anchor="w", height=18)
        self.mini_status.pack(fill="x")
        self.mini_text = ctk.CTkLabel(mitte, text=self.leser.letzter or "Noch nichts vorgelesen.", text_color=TEXT,
                                      font=schrift(12), wraplength=280, justify="left", anchor="w", width=290)
        self.mini_text.pack(fill="x")
        rechts = ctk.CTkFrame(k, fg_color="transparent")
        rechts.pack(side="left", padx=(6, 12), pady=10)
        for zeichen, befehl in (("↻", self.wiederholen), ("↗", self.mini_schliessen)):
            ctk.CTkButton(rechts, text=zeichen, command=befehl, fg_color=KNOPF, hover_color=GRAU, text_color=TEXT,
                          border_width=1, border_color=GRAU, corner_radius=10, width=44, height=34,
                          font=schrift(15), cursor="hand2").pack(pady=3)

        # verschieben mit der Maus
        def greifen(e):
            m._dx, m._dy = e.x_root - m.winfo_x(), e.y_root - m.winfo_y()

        def ziehen(e):
            m.geometry(f"+{e.x_root - m._dx}+{e.y_root - m._dy}")

        def loslassen(e):
            self.einst["mini_pos"] = [m.winfo_x(), m.winfo_y()]

        for w in (k, mitte, self.mini_status, self.mini_text):
            w.bind("<ButtonPress-1>", greifen)
            w.bind("<B1-Motion>", ziehen)
            w.bind("<ButtonRelease-1>", loslassen)

        # Position: gespeicherte, sonst unten rechts – immer ganz auf dem Bildschirm
        m.update_idletasks()
        bw, bh = m.winfo_reqwidth(), m.winfo_reqheight()
        sw, sh = m.winfo_screenwidth(), m.winfo_screenheight()
        pos = self.einst["mini_pos"]
        x, y = (pos if pos else (sw - bw - 30, sh - bh - 90))
        x = max(0, min(x, sw - bw)) if not pos or 0 <= pos[0] < sw else x
        y = max(0, min(y, sh - bh)) if not pos or 0 <= pos[1] < sh else y
        m.geometry(f"+{int(x)}+{int(y)}")

        self.root.withdraw()
        self.fenster_anmelden(m)
        self._anzeige_aktualisieren()

    def mini_schliessen(self):
        if self.mini:
            self.mini.destroy()
            self.mini = None
        self.root.deiconify()
        self.root.lift()

    # -------------------------------------------------------- Verlauf
    def verlauf_zeigen(self):
        f = ctk.CTkToplevel(self.root, fg_color=BG)
        f.title("Verlauf")
        f.geometry("560x520")
        f.after(200, f.lift)
        self.fenster_anmelden(f)
        ctk.CTkLabel(f, text="Klick auf einen Satz liest ihn nochmal vor.", text_color=BLASS,
                     font=schrift(12)).pack(anchor="w", padx=16, pady=(14, 6))
        liste = ctk.CTkScrollableFrame(f, fg_color=PANEL, corner_radius=RADIUS, border_width=1, border_color=RAND)
        liste.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        if not self.verlauf:
            ctk.CTkLabel(liste, text="Noch nichts vorgelesen.", text_color=BLASS, font=schrift(12)).pack(pady=20)
        for t in reversed(self.verlauf):
            ctk.CTkButton(liste, text=t, anchor="w", fg_color="transparent", hover_color=KNOPF, text_color=TEXT,
                          font=schrift(13), corner_radius=8, height=36, cursor="hand2",
                          command=lambda t=t: self.sprecher.sagen(t)).pack(fill="x", padx=4, pady=2)

    # -------------------------------------------------------- Tasten
    def tasten_registrieren(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        for k, _, _ in FUNKTIONEN:
            t = self.einst["tasten"].get(k)
            if not t:
                continue
            try:
                keyboard.add_hotkey(t, lambda k=k: self.melden("befehl", k), suppress=True)
            except Exception as e:
                log("Taste nicht möglich:", t, e)

    def tasten_zeigen(self):
        if self.tasten_fenster:
            self.tasten_fenster.lift()
            return
        f = ctk.CTkToplevel(self.root, fg_color=BG)
        self.tasten_fenster = f
        f.title("Tasten einstellen")
        f.resizable(False, False)
        f.after(200, f.lift)
        f._mon = None

        def bewegt(e, f=f):
            if e.widget is not f:
                return
            try:
                mon, _ = bildschirm_von(int(f.wm_frame(), 16))
            except Exception:
                return
            if mon is not None and mon != f._mon:
                if f._mon is not None:
                    for ms in (300, 1400, 2600):
                        f.after(ms, lambda: natuerliche_groesse(f))
                f._mon = mon
        f.bind("<Configure>", bewegt, add="+")
        self.fenster_anmelden(f)

        def zu():
            self.tasten_fenster = None
            f.destroy()
        f.protocol("WM_DELETE_WINDOW", zu)

        ctk.CTkLabel(f, text="Auf eine Taste klicken und dann die neue Taste drücken.\n"
                             "(Esc = abbrechen, Entf = Taste entfernen)",
                     text_color="#B7BCC9", font=schrift(12), justify="left").pack(anchor="w", padx=18, pady=(16, 10))
        self.tasten_knoepfe = {}
        for k, name, _ in FUNKTIONEN:
            zeile = karte(f)
            zeile.pack(fill="x", padx=18, pady=4)
            ctk.CTkLabel(zeile, text=name, text_color=TEXT, font=schrift(14, True), width=180,
                         anchor="w").pack(side="left", padx=14, pady=10)
            b = ctk.CTkButton(zeile, text=taste_anzeigen(self.einst["tasten"].get(k)), width=170, height=40,
                              command=lambda k=k: self._taste_lesen(k), fg_color=KNOPF, hover_color=GRAU,
                              text_color=TEXT, border_width=1, border_color=GRAU, corner_radius=10,
                              font=ctk.CTkFont(family="Consolas", size=13, weight="bold"), cursor="hand2")
            b.pack(side="right", padx=10, pady=8)
            self.tasten_knoepfe[k] = b
        unten = ctk.CTkFrame(f, fg_color="transparent")
        unten.pack(fill="x", padx=18, pady=16)
        unten.grid_columnconfigure((0, 1), weight=1)
        knopf(unten, "Zurücksetzen", self._tasten_zuruecksetzen).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        knopf(unten, "Fertig", zu, haupt=True).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _taste_lesen(self, funktion):
        if self.taste_wird_gelesen:
            return
        self.taste_wird_gelesen = True
        self.tasten_knoepfe[funktion].configure(text="Taste drücken …", text_color=GRUEN, fg_color=GRUEN_HELL,
                                                border_color=GRUEN)
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass

        def lesen():
            try:
                t = keyboard.read_hotkey(suppress=False)
            except Exception:
                t = "esc"
            self.melden("taste", (funktion, t))
        threading.Thread(target=lesen, daemon=True).start()

    def _taste_gelesen(self, funktion, t):
        self.taste_wird_gelesen = False
        if t in ("delete", "entf", "backspace"):
            self.einst["tasten"][funktion] = ""
            self.einst.speichern()
        elif t and t != "esc":
            doppelt = [n for k, n, _ in FUNKTIONEN if k != funktion and self.einst["tasten"].get(k) == t]
            ok = True
            if doppelt:
                messagebox.showwarning("Tasten", f"{taste_anzeigen(t)} ist schon für „{doppelt[0]}“ belegt.",
                                       parent=self.tasten_fenster)
                ok = False
            elif t in PCSX2_TASTEN:
                ok = messagebox.askyesno(
                    "Tasten", f"{taste_anzeigen(t)} benutzt PCSX2 möglicherweise schon selbst.\n"
                              "Der Lesehelfer fängt sie ab, dann geht sie im Emulator nicht mehr.\n\nTrotzdem nehmen?",
                    parent=self.tasten_fenster)
            if ok:
                self.einst["tasten"][funktion] = t
                self.einst.speichern()
        if self.tasten_fenster:
            self.tasten_knoepfe[funktion].configure(text=taste_anzeigen(self.einst["tasten"][funktion]),
                                                    text_color=TEXT, fg_color=KNOPF, border_color=GRAU)
        self.tasten_registrieren()
        self._anzeige_aktualisieren()
        self._desktop_anzeige()
        self._vr_anzeige()

    def _tasten_zuruecksetzen(self):
        self.einst["tasten"] = {k: t for k, _, t in FUNKTIONEN}
        for k, b in self.tasten_knoepfe.items():
            b.configure(text=taste_anzeigen(self.einst["tasten"][k]))
        self.tasten_registrieren()
        self._anzeige_aktualisieren()
        self._desktop_anzeige()

    def _auf_update_pruefen(self):
        """Wurde das Programm aktualisiert? Dann automatisch neu starten."""
        try:
            pfad = os.path.abspath(__file__)
            # Beim Wechsel Prozessor/Grafikkarte wartet das Lesen absichtlich – kein Neustart
            haengt = (time.time() - self.leser.puls > 20) and not getattr(self, "_rechner_arbeitet", False)
            if haengt:
                log("Das Lesen hängt seit über 20 Sekunden – starte neu")
            if haengt or os.path.getmtime(pfad) != self._eigene_zeit:
                time.sleep(1)  # Datei fertig schreiben lassen
                import subprocess
                with open(pfad, encoding="utf-8") as f:  # nur neu starten, wenn die neue Fassung fehlerfrei ist
                    compile(f.read(), pfad, "exec")
                log("Neue Version gefunden – starte neu")
                umgebung = {k: v for k, v in os.environ.items() if k not in ("VORLESER_HAUPT_PID", "VORLESER_LOG")}
                subprocess.Popen([sys.executable, pfad], cwd=BASIS, env=umgebung,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception as e:
            log("Update-Prüfung:", e)
            self._eigene_zeit = os.path.getmtime(os.path.abspath(__file__))
        self.root.after(3000, self._auf_update_pruefen)

    # -------------------------------------------------------- Ende
    def beenden(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()

    def los(self):
        self.root.mainloop()


def aufraeumen():
    """Alte Dateien aus früheren Versionen in die neue Ordnerstruktur bringen bzw. entfernen."""
    import shutil
    # Einstellungen und Sprachmodelle in daten\ verschieben
    for alt, neu in ((os.path.join(BASIS, "einstellungen.json"), EINST_DATEI),
                     (os.path.join(BASIS, "modelle"), os.path.join(DATEN, "modelle"))):
        if os.path.exists(alt) and not os.path.exists(neu):
            try:
                shutil.move(alt, neu)
                log("Verschoben:", alt, "->", neu)
            except Exception as e:
                log("Verschieben fehlgeschlagen:", alt, e)
    weg = ["diagnose.txt", "diagnose_an.txt", "diagnose_roh.txt", "diagnose_rohdaten.txt",
           "diagnose_bilder.txt", "rohdaten_protokollieren.txt", "rohdaten_mitschreiben.txt",
           "uebersetzung_testen.txt", "uebersetzung_vorladen.txt", "kontrollbild.png",
           "kontrollbild_ganz.png", "vorleser_log.txt", "laeuft.pid"]
    if os.path.exists(EINST_DATEI):  # alte Einstellungsdateien sind dann schon übernommen
        weg += ["vorleser_bereich.json", "vorleser_stimme.json"]
    if os.path.normcase(ORDNER) != os.path.normcase(BASIS):
        weg.append("ps2_vorleser.py")  # alte Programmdatei im Hauptordner
    # Umbenennung PS2-Vorleser -> Lesehelfer: alte Startdatei und Programmdatei weg
    if os.path.basename(__file__).lower() == "lesehelfer.py":
        weg.append("Vorleser_starten.bat")
        alt_prog = os.path.join(ORDNER, "ps2_vorleser.py")
        if os.path.isfile(alt_prog):
            try:
                os.remove(alt_prog)
                log("Aufgeräumt: programm\\ps2_vorleser.py")
            except Exception as e:
                log("Konnte nicht löschen: ps2_vorleser.py", e)
    for name in weg:
        pfad = os.path.join(BASIS, name)
        if os.path.isfile(pfad):
            try:
                os.remove(pfad)
                log("Aufgeräumt:", name)
            except Exception as e:
                log("Konnte nicht löschen:", name, e)
    for cache in (os.path.join(BASIS, "__pycache__"), os.path.join(ORDNER, "__pycache__")):
        if os.path.isdir(cache):
            shutil.rmtree(cache, ignore_errors=True)


def alte_instanz_beenden():
    """Läuft der Vorleser schon (evtl. unsichtbar oder hängend)? Dann die alte Instanz beenden.
    Erkannt wird sie am Fenstertitel "Lesehelfer" (früher "PS2-Vorleser") eines python(w)-Prozesses."""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        gefunden = set()
        PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        u.EnumWindows.argtypes = [PROC, ctypes.c_void_p]

        def pruefen(hwnd, _):
            puffer = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(hwnd, puffer, 64)
            if puffer.value in ("Lesehelfer", "PS2-Vorleser"):
                pid = ctypes.c_ulong()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value and pid.value != os.getpid():
                    gefunden.add(pid.value)
            return True

        u.EnumWindows(PROC(pruefen), 0)
        for pid in gefunden:
            h = k.OpenProcess(0x0001 | 0x1000, False, pid)  # TERMINATE | QUERY_LIMITED_INFORMATION
            if not h:
                continue
            puffer = ctypes.create_unicode_buffer(520)
            groesse = ctypes.c_ulong(520)
            if k.QueryFullProcessImageNameW(h, 0, puffer, ctypes.byref(groesse)):
                if os.path.basename(puffer.value).lower() in ("python.exe", "pythonw.exe"):
                    k.TerminateProcess(h, 0)
                    log("Alte Instanz beendet:", pid)
            k.CloseHandle(h)
        if gefunden:
            time.sleep(0.5)
    except Exception as e:
        log("Alte Instanz nicht prüfbar:", e)


def _versions_tupel(v):
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0,)


def online_update_pruefen():
    """Schaut auf der GitHub-Seite nach einer neueren Version. Gibt es eine, wird sie geladen,
    geprüft (Prüfsumme, fehlerfrei übersetzbar, richtige Versionsnummer) und über die eigene
    Programmdatei geschrieben. Den Neustart erledigt dann die normale Update-Prüfung."""
    import hashlib
    import urllib.request
    kopf = {"User-Agent": "Lesehelfer/" + VERSION, "Cache-Control": "no-cache"}

    def holen(name):
        anfrage = urllib.request.Request(UPDATE_QUELLE + name + "?t=" + str(int(time.time())), headers=kopf)
        with urllib.request.urlopen(anfrage, timeout=30) as antwort:
            return antwort.read()

    info = {}
    for zeile in holen("version.txt").decode("utf-8", "replace").splitlines():
        if "=" in zeile:
            k, v = zeile.split("=", 1)
            info[k.strip()] = v.strip()
    neu = info.get("version", "")
    if _versions_tupel(neu) <= _versions_tupel(VERSION):
        return False
    log("Neue Version im Internet:", neu, "(jetzt:", VERSION + ")")
    daten = holen("programm/lesehelfer.py")
    if hashlib.sha256(daten).hexdigest().lower() != info.get("sha256", "").lower():
        log("Update verworfen: Prüfsumme passt nicht")
        return False
    text = daten.decode("utf-8")
    compile(text, "lesehelfer.py", "exec")
    if f'VERSION = "{neu}"' not in text:
        log("Update verworfen: Versionsnummer passt nicht")
        return False
    pfad = os.path.abspath(__file__)
    tmp = pfad + ".neu"
    with open(tmp, "wb") as f:
        f.write(daten)
    with open(UPDATE_MERKER, "w", encoding="utf-8") as f:
        f.write(neu)
    os.replace(tmp, pfad)
    log("Update", neu, "installiert – Neustart folgt")
    return True


def online_update_schleife():
    time.sleep(20)   # erst in Ruhe starten lassen
    while True:
        try:
            if online_update_pruefen():
                return
        except Exception as e:
            log("Update-Prüfung (Internet):", e)
        time.sleep(UPDATE_ABSTAND)


def paketinfo_schreiben():
    """Nur mit Stichwort "pakete" in fehlersuche.txt: Paket-Versionen und die benutzten
    Texterkennungs-Modelle nach daten\\paketinfo schreiben (für das Komplettpaket)."""
    if not fehlersuche("pakete"):
        return
    try:
        import shutil
        from importlib import metadata
        ziel = os.path.join(DATEN, "paketinfo")
        os.makedirs(ziel, exist_ok=True)
        zeilen = [f"python {sys.version}"]
        for d in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
            zeilen.append(f"{d.metadata['Name']}=={d.version}")
        import importlib.util
        spec = importlib.util.find_spec("rapidocr")
        if spec and spec.origin:
            mordner = os.path.join(os.path.dirname(spec.origin), "models")
            zeilen.append("# rapidocr models: " + mordner)
            for n in sorted(os.listdir(mordner)):
                p = os.path.join(mordner, n)
                if os.path.isfile(p):
                    zeilen.append(f"#   {n} {os.path.getsize(p)}")
                    if n.lower().endswith((".onnx", ".txt", ".yaml")):
                        shutil.copy2(p, os.path.join(ziel, n))
        with open(os.path.join(ziel, "pakete.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(zeilen) + "\n")
        log("Paketinfo geschrieben:", ziel)
    except Exception as e:
        log("Paketinfo fehlgeschlagen:", e)


if __name__ == "__main__":
    alte_instanz_beenden()
    paketinfo_schreiben()
    toene_ordner_anlegen()
    try:
        aufraeumen()
    except Exception:
        log(traceback.format_exc())
    try:
        App().los()
    except Exception:
        log(traceback.format_exc())
        try:
            messagebox.showerror("Lesehelfer", "Unerwarteter Fehler:\n" + traceback.format_exc()[-800:])
        except Exception:
            pass
    os._exit(0)
