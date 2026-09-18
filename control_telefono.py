"""
Control de teléfonos Android por ADB con interfaz gráfica.
Requisitos: Python 3 y ADB (viene incluido con scrcpy: winget install Genymobile.scrcpy).
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import csv
import html
import queue
import random
import threading
import time
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, messagebox, filedialog, scrolledtext, simpledialog
from urllib.parse import quote
from collections import deque

try:
    import db_numeros              # base PostgreSQL (DATABASE_URL en .env); opcional
except Exception:                  # sin driver o sin archivo: la app sigue con JSON
    db_numeros = None

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONTROL_TELEFONO_CONFIG") or os.path.join(BASE, "config_telefono.json")
PENDING_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "pendientes_ultimo.txt")
FAILED_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "fallidos_ultimo.txt")
LOG_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "logs")
ASSIGN_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "asignaciones.json")
KNOWN_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "telefonos_conocidos.json")   # {serie_hw: nombre}
_ASSIGN_LOCK = threading.Lock()


def load_assignments():
    """{numero: {"key": serie_hw_o_nombre, "name": nombre, "at": fecha}} o None si el archivo no existe."""
    try:
        with open(ASSIGN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def save_assignments(assign):
    try:
        with _ASSIGN_LOCK:
            tmp = ASSIGN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(assign, f, indent=1, ensure_ascii=False)
            os.replace(tmp, ASSIGN_FILE)
    except Exception:
        pass


def seed_assignments_from_reports(phones):
    """Construye las asignaciones iniciales a partir de los reportes envios_*.csv (último envío exitoso por número)."""
    by_name = {ph["name"]: (ph.get("hw") or ph["name"]) for ph in phones}
    assign = {}
    folder = os.path.dirname(CONFIG_FILE)
    try:
        files = sorted(f for f in os.listdir(folder) if f.startswith("envios_") and f.endswith(".csv"))
    except Exception:
        return assign
    for fn in files:
        try:
            with open(os.path.join(folder, fn), encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f, delimiter=";"):
                    if len(row) >= 4 and row[2] == "enviado":
                        key = row[5] if len(row) >= 6 and row[5] else by_name.get(row[1])
                        if key:
                            assign[row[0]] = {"key": key, "name": row[1], "at": row[3]}
        except Exception:
            continue
    return assign


class FileLog:
    """Registro en archivo, seguro entre hilos.
    - logs/control_AAAAMMDD.log : todo lo que aparece en la consola de la app, más errores con detalle.
    - logs/envio_AAAAMMDD_HHMMSS.log : traza paso a paso de cada envío masivo (un archivo por envío).
    """
    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()
        self._send = None          # archivo del envío en curso
        self.send_path = None

    def _write(self, path, line):
        try:
            os.makedirs(self.folder, exist_ok=True)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def daily_path(self):
        return os.path.join(self.folder, time.strftime("control_%Y%m%d.log"))

    def write(self, level, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {level:<5} | {msg}"
        self._write(self.daily_path(), line)
        if self.send_path:
            self._write(self.send_path, line)

    def error(self, msg, exc=None):
        self.write("ERROR", msg)
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
            self._write(self.daily_path(), "    " + tb.replace("\n", "\n    "))
            if self.send_path:
                self._write(self.send_path, "    " + tb.replace("\n", "\n    "))

    def start_send(self, header_lines):
        self.send_path = os.path.join(self.folder, time.strftime("envio_%Y%m%d_%H%M%S.log"))
        for l in header_lines:
            self.write("INFO", l)
        return self.send_path

    def step(self, name, num, text):
        """Traza fina de un paso del envío (solo va al archivo del envío, no a la consola)."""
        if self.send_path:
            self._write(self.send_path, f"{time.strftime('%Y-%m-%d %H:%M:%S')} | PASO  | [{name}] {num}: {text}")

    def end_send(self, summary):
        self.write("INFO", summary)
        self.send_path = None


FILELOG = FileLog(LOG_DIR)


def write_numbers(path, nums):
    """Escritura atómica (temporal + reemplazo): se reescribe a menudo y un corte no debe dejarlo vacío."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(nums))
        os.replace(tmp, path)
    except Exception:
        pass


def read_numbers(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

WA_APPS = {"WhatsApp Business": "com.whatsapp.w4b", "WhatsApp": "com.whatsapp"}

# --- Modo "escribir como humano" (buscar el número en la app y teclear letra por letra)
ADBKB_APK = os.path.join(BASE, "ADBKeyboard.apk")   # teclado que acepta tildes/emojis por ADB
ADBKB_PKG = "com.android.adbkeyboard"
ADBKB_IME = "com.android.adbkeyboard/.AdbIME"
GBOARD_IME = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
URL_RE = re.compile(r"(https?://\S+)")  # los enlaces se insertan de golpe, no letra por letra

KEYS = {
    "Inicio": "KEYCODE_HOME", "Atrás": "KEYCODE_BACK", "Recientes": "KEYCODE_APP_SWITCH",
    "Encender": "KEYCODE_POWER", "Vol +": "KEYCODE_VOLUME_UP", "Vol -": "KEYCODE_VOLUME_DOWN",
    "Enter": "KEYCODE_ENTER", "Borrar": "KEYCODE_DEL", "Desbloquear": "KEYCODE_MENU",
}

SALUDOS = [
    "Hola 👋", "Hola, ¿cómo estás?", "Hola, ¿qué tal?", "Buen día ☀️", "{hora} 👋", "{hora}, ¿cómo estás?",
    "{hora}, espero que estés bien", "Hola, un gusto saludarte", "Hola, ¿cómo va todo?", "¡Hola! 😊",
    "Hola, espero que estés muy bien", "{hora}, ¿todo bien?", "Qué tal, ¿cómo estás?", "Hola, saludos 🙌",
]
CIERRES = [
    "😊", "🙌", "👍", "✨", "🙏", "💪", "🤝", "Saludos 👋", "¡Gracias! 🙏", "Quedo atento 👍", "Un abrazo 🤗",
    "Que tengas un buen día ☀️", "Cualquier consulta, escríbeme 📲", "¡Éxitos! ✨", "Saludos cordiales 🤝",
    "Estamos en contacto 📱", "Nos vemos 👋", "Gracias por tu tiempo 🙏", "😊👍", "🙌✨",
]


def saludo_hora():
    h = time.localtime().tm_hour
    return "Buenos días" if h < 12 else ("Buenas tardes" if h < 19 else "Buenas noches")


class AccountRestricted(RuntimeError):
    """WhatsApp muestra 'Tu cuenta está restringida' en el chat."""


class SendUnconfirmed(RuntimeError):
    """Ya se tocó Enviar y no se pudo comprobar el resultado: el mensaje PUEDE haber salido. Nunca se reenvía solo."""


def compose_message(body, saludos=None, cierres=None, last=None):
    """Arma: saludo aleatorio + cuerpo + cierre aleatorio, evitando repetir la combinación anterior."""
    saludos = [x for x in (saludos or SALUDOS) if x.strip()]
    cierres = [x for x in (cierres or CIERRES) if x.strip()]
    for _ in range(5):
        sal = random.choice(saludos).replace("{hora}", saludo_hora()) if saludos else ""
        cie = random.choice(cierres) if cierres else ""
        if (sal, cie) != last:
            break
    parts = [x for x in (sal, body.strip(), cie) if x]
    return "\n".join(parts), (sal, cie)


ADB_GLOBAL = ("devices", "connect", "disconnect", "kill-server", "start-server")
ADB_PORT = 5555


# ----------------------------------------------------------------- red WiFi
def norm_mac(mac):
    """Normaliza una MAC a minúsculas con dos puntos (aa:bb:cc:dd:ee:ff)."""
    m = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)).lower() if len(m) == 12 else None


def local_ipv4s():
    """IPs IPv4 de la PC (todas sus interfaces), sin loopback ni APIPA."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        # Truco: un socket UDP "conectado" revela la IP de salida sin enviar nada
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.connect(("8.8.8.8", 80))
            ips.add(u.getsockname()[0])
    except Exception:
        pass
    return {ip for ip in ips if not ip.startswith(("127.", "169.254."))}


def subnet_prefixes(extra_ips=()):
    """Prefijos /24 ("172.17.1.") de la PC y de las IPs dadas."""
    out = set()
    for ip in list(local_ipv4s()) + [ip for ip in extra_ips if ip]:
        m = re.match(r"(\d+\.\d+\.\d+)\.\d+$", ip)
        if m:
            out.add(m.group(1) + ".")
    return sorted(out)


def port_open(ip, port=ADB_PORT, timeout=0.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe_port(ip, port=ADB_PORT, timeout=3.0):   # Windows tarda ~2 s en informar "conexión rechazada"
    """'abierto' | 'rechaza' (encendido y en la red, pero ADB WiFi apagado: se reinició) | 'mudo' (apagado / sin WiFi)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return "abierto"
    except ConnectionRefusedError:
        return "rechaza"
    except OSError:
        return "mudo"
    finally:
        s.close()


def fmt_dur(sec):
    """Duración legible: '7 min', '1 h 05 min'."""
    m = int(max(0, sec) // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def bulk_eta(alive, own, pool, per):
    """PURA (testeable). Segundos hasta terminar lo ALCANZABLE, o None si aún no hay datos.
    alive: claves enviando; own: {clave: nº en su cola propia}; pool: nº en la cola común;
    per[k] = {"durs": [segundos por mensaje...], "cur": número en curso o None}."""
    fleet = [d for p in per.values() for d in p["durs"]]
    if not alive or len(fleet) < 3:
        return None
    base = sum(fleet) / len(fleet)
    avg = {k: (sum(per[k]["durs"]) / len(per[k]["durs"]) if k in per and len(per[k]["durs"]) >= 3 else base)
           for k in alive}
    cur = {k: (0.5 if per.get(k, {}).get("cur") else 0) for k in alive}
    t_own = max((own.get(k, 0) + cur[k]) * avg[k] for k in alive)
    t_all = (sum(own.get(k, 0) + cur[k] for k in alive) + pool) / sum(1.0 / avg[k] for k in alive)
    return max(t_own, t_all)


def scan_adb_hosts(prefixes, port=ADB_PORT, workers=128, timeout=0.5):
    """Devuelve las IPs de los prefijos /24 dados que tienen el puerto ADB abierto."""
    hosts = [f"{pre}{i}" for pre in prefixes for i in range(1, 255)]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ip, ok in zip(hosts, ex.map(lambda h: port_open(h, port, timeout), hosts)):
            if ok:
                found.append(ip)
    return found


def arp_table():
    """{mac: ip} de la tabla ARP de la PC (equipos vistos en la red hace poco)."""
    table = {}
    try:
        cmd = ["arp", "-a"] if sys.platform == "win32" else ["arp", "-an"]
        r = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace")
        for line in out.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)\D+([0-9a-fA-F]{2}([-:][0-9a-fA-F]{2}){5})", line)
            if m:
                mac = norm_mac(m.group(2))
                if mac and mac != "ff:ff:ff:ff:ff:ff":
                    table[mac] = m.group(1)
    except Exception:
        pass
    return table


# ----------------------------------------------------------------- utilidades
def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_CFG_LOCK = threading.Lock()


def save_config(cfg):
    """Guarda la configuración de forma atómica (archivo temporal + reemplazo) y con candado entre hilos."""
    try:
        with _CFG_LOCK:
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.replace(tmp, CONFIG_FILE)
            if cfg.get("known"):      # copia aparte de los nombres por serie, sobrevive a borrar la lista
                with open(KNOWN_FILE + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(cfg["known"], f, indent=1, ensure_ascii=False)
                os.replace(KNOWN_FILE + ".tmp", KNOWN_FILE)
    except Exception:
        pass


def load_known():
    try:
        with open(KNOWN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def msg_key(text, n=12):
    """Clave corta y sin acentos/espacios de un texto, para reconocerlo en el cuadro de WhatsApp."""
    return re.sub(r"[^0-9A-Za-z]", "", html.unescape(text or ""))[:n]


def find_tool(name, saved=None):
    """Busca adb.exe / scrcpy.exe en la ruta guardada, el PATH y carpetas típicas."""
    if saved and os.path.isfile(saved):
        return saved
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Android", "Sdk", "platform-tools", f"{name}.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "scrcpy", f"{name}.exe"),
    ]
    pkgs = os.path.join(local, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(pkgs):
        for root, _dirs, files in os.walk(pkgs):
            if f"{name}.exe" in files:
                candidates.append(os.path.join(root, f"{name}.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def normalize_number(raw, country="51"):
    """Deja solo dígitos y antepone el código de país si falta. Devuelve None si no parece un celular."""
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("00"):
        d = d[2:]
    if not d:
        return None
    if country and d.startswith(country) and len(d) == len(country) + 9:
        return d
    if len(d) == 10 and d.startswith("09"):
        d = d[1:]
    if len(d) == 9 and d.startswith("9"):
        return country + d
    if len(d) >= 10 and not country:
        return d
    if country and len(d) > len(country) + 9 and d.startswith(country):
        return None
    return None


def read_numbers_file(path, country="51"):
    """Lee números de un .xlsx, .csv o .txt mirando TODAS las celdas de cada fila (sirve para exportaciones
    de WhatsApp donde el número puede venir en otra columna). Devuelve (válidos, inválidos)."""
    rows = []
    low = path.lower()
    if low.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append(cells)
        wb.close()
    else:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            text = f.read()
        delim = max(";,	", key=text.count)
        for parts in csv.reader(text.splitlines(), delimiter=delim):
            cells = [c.strip() for c in parts if c and c.strip()]
            if cells:
                rows.append(cells)
    valid, invalid, seen = [], [], set()
    for cells in rows:
        found = []
        for c in cells:
            if "://" in c or "@" in c:            # enlaces y correos: no son teléfonos
                continue
            for piece in re.split(r"[/|]", c):    # varias formas de escribir un celular en una celda
                n = normalize_number(piece, country)
                if n:
                    found.append(n)
        if found:
            for n in found:
                if n not in seen:
                    valid.append(n)
                    seen.add(n)
        else:
            invalid.append(cells[0])
    return valid, invalid


def escape_for_input(text):
    """Escapa texto para `adb shell input text` (espacio -> %s, caracteres especiales)."""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in "\\\"'`$&|;<>()[]{}*?~#!":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Control de teléfonos (ADB)")
        self.geometry("1500x860")
        self.minsize(1100, 640)
        try:
            self.state("zoomed")                  # abrir maximizada: el tablero y los botones de envío necesitan sitio
        except Exception:
            pass

        self.cfg = load_config()
        self.cfg.setdefault("phones", [])          # [{name, usb, ip, model, wa_x, wa_y}]
        self.cfg.setdefault("known", {})           # {serie_hw: nombre} para que un teléfono conserve su nombre siempre
        for _hw, _nm in load_known().items():
            self.cfg["known"].setdefault(_hw, _nm)
        for _ph in self.cfg["phones"]:
            if _ph.get("hw"):
                self.cfg["known"].setdefault(_ph["hw"], _ph["name"])
        self._migrate_config()
        self._migrated = False
        if not self.cfg.get("fix_0918"):           # una prueba del 13/09 dejó apagadas estas dos opciones en la config real
            self.cfg.update(auto_scan=True, wa_auto_join=True, fix_0918=True)
            save_config(self.cfg)
            self._migrated = True
        self.adb = find_tool("adb", self.cfg.get("adb"))
        self.scrcpy = find_tool("scrcpy", self.cfg.get("scrcpy"))

        self.serial = None            # serie ADB del teléfono activo (usb o ip:5555)
        self.connected = {}           # serial -> estado según `adb devices`
        self.screen_w, self.screen_h = 1080, 2400
        self.scale = 1.0
        self.photo = None
        self.last_png = None
        self.running_bulk = False
        self.lock = threading.Lock()
        # Lecturas de pantalla (uiautomator): un candado por teléfono (nunca dos lecturas a la vez en el mismo)
        # y un tope global de lecturas simultáneas entre teléfonos (configurable en la pestaña WhatsApp).
        self._phone_locks = {}
        self._locks_guard = threading.Lock()
        self._dump_par = int(self.cfg.get("wa_dump_par", 6) or 6)
        self._dump_sem = threading.BoundedSemaphore(max(1, self._dump_par))
        self.sizes = {}               # serial -> (ancho, alto) de pantalla
        self.bulk_active = False      # hay un envío masivo en curso (hasta que termine del todo)
        self.wa_pending = None        # números no intentados en el último envío (None = leer del archivo)
        self.wa_failed = None         # números que fallaron en el último envío
        self.wa_saved_ime = {}        # serial -> teclado original, para restaurarlo tras el modo humano
        self._last_scan = 0.0         # última búsqueda automática en la red WiFi
        self._scanning = threading.Lock()
        self._bulk = None             # estado del envío masivo en curso (cola, teléfonos activos, spawn)
        self._bulk_lock = threading.Lock()
        self._last_step = {}          # nombre de teléfono -> (hora, número, texto del último paso)
        self._send_off = frozenset(self.cfg.get("send_off") or [])   # claves (hw/nombre) DESMARCADAS ☐ para enviar
        self._sel_key = None          # clave de la fila desconectada elegida por el operador (sobrevive a fill_tree)
        self._contact_n = {}          # clave -> nº de contactos asignados (caché de fill_tree)
        self._row_cache = {}          # iid -> (valores, tag) ya pintados
        self._last_snap = None        # última foto de contadores (queda visible al terminar el envío)
        self._alerted = set()         # claves por las que ya sonó la campana en este envío
        self._stop_requested = False  # el operador pulsó Detener
        self._start_opts = {}         # decisiones del diálogo de arranque (hilo principal -> hilo del envío)
        self.health = {}              # clave -> {"ts","conn","bat","carga","act":(ok,texto,ts)}
        self._activating = threading.Lock()   # un ⚡ ACTIVAR TODOS a la vez (acquire no bloqueante)
        self._act_busy = set()        # claves que se están activando/reparando (protegido por _bulk_lock)
        self._need_usb = set()        # claves que esperan cable USB
        self._usb_watching = False
        self._after_activation = None  # '▶ Enviar' pidió comprobar antes: qué hacer al terminar ⚡
        self._act_report = None        # resultado del último ⚡ (listos, marcados, [los que necesitan ayuda])
        self._health_mut = threading.Lock()   # candado corto SOLO para fusionar campos en self.health
        self._health_lock = threading.Lock()
        # Asignación fija número -> teléfono (el contacto vuelve siempre al teléfono que le escribió)
        self.assign = load_assignments()
        self._assign_seeded = 0
        if self.assign is None:
            self.assign = seed_assignments_from_reports(self.cfg["phones"])
            self._assign_seeded = len(self.assign)
            save_assignments(self.assign)
        self.db = None
        self._db_msg = self._db_connect()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, lambda: self.bg(self.refresh_devices))
        self.after(1000, self._tick)
        self.after(15000, self._health_kick)
        if self._migrated:
            self.after(700, lambda: self.log(
                "Aviso: se volvieron a activar 'Sumar los que se conecten durante el envío' y la búsqueda automática en la red "
                "(una prueba antigua las había dejado apagadas). Puedes desmarcar la casilla si no la quieres."))
        if self._assign_seeded:
            self.after(400, lambda: self.log(
                f"Asignaciones iniciales: {self._assign_seeded} número(s) ligados a su teléfono según los reportes anteriores "
                "(archivo asignaciones.json)."))
        self.after(500, lambda: self.log(self._db_msg))

    # ---------------------------------------------------------- base de datos
    def _db_connect(self):
        """Conecta a PostgreSQL (.env), crea las tablas si faltan y funde asignaciones JSON y BD. Devuelve un mensaje."""
        if os.environ.get("CONTROL_TELEFONO_SIN_BD", "").strip().lower() in ("1", "true", "si", "sí", "yes"):
            return "Base de datos DESACTIVADA (CONTROL_TELEFONO_SIN_BD): solo archivos JSON."
        if db_numeros is None:
            return "Base de datos: módulo no disponible (instala: pip install \"psycopg[binary]\"). Se usa asignaciones.json."
        db = db_numeros.DB.from_env()
        if db is None:
            return "Base de datos: no hay DATABASE_URL en .env o falta el driver psycopg. Se usa asignaciones.json."
        try:
            db.init_schema()
            db.upsert_telefonos(self.cfg["phones"])
            for hw, nm in self.cfg.get("known", {}).items():
                db.upsert_telefono(hw, nm)
            en_bd = db.cargar_asignaciones()                    # la BD manda; el JSON es solo copia
            self.assign = en_bd
            save_assignments(self.assign)
            self.db = db
            por_tel = {}
            for a in en_bd.values():
                por_tel[a.get("name") or a.get("key")] = por_tel.get(a.get("name") or a.get("key"), 0) + 1
            detalle = ", ".join(f"{k}={v}" for k, v in sorted(por_tel.items()))
            return f"Base de datos conectada: {len(en_bd)} número(s) asignados" + (f" ({detalle})." if detalle else ".")
        except Exception as e:
            FILELOG.error(f"Base de datos: {e}", e)
            return f"Base de datos: no se pudo conectar ({str(e).strip()[:120]}). Se usa asignaciones.json."

    def _db(self, fn, *args, what="base de datos"):
        """Ejecuta una operación en la BD sin romper nada si falla (se registra en el log)."""
        if not self.db:
            return None
        try:
            return fn(*args)
        except Exception as e:
            FILELOG.error(f"BD ({what}): {e}", e)
            self.log(f"Aviso: no se pudo escribir en la {what} ({str(e).strip()[:80]}). El dato quedó en los archivos JSON/CSV.")
            return None

    def on_close(self):
        if self.bulk_active and not messagebox.askyesno(
                "Envío en curso", "Hay un envío masivo en curso. Si cierras ahora se interrumpe (puede cortar el mensaje que se está "
                "escribiendo).\nLo ya enviado queda en el reporte CSV y lo que falte se retoma con '↺ Reanudar pendientes'."
                "\n\n¿Cerrar de todos modos?",
                parent=self):
            return
        self.running_bulk = False
        self.destroy()

    def _migrate_config(self):
        """Convierte la configuración antigua (un solo teléfono) al registro de teléfonos."""
        old_ip = self.cfg.pop("ip", None)
        old_x, old_y = self.cfg.pop("wa_x", None), self.cfg.pop("wa_y", None)
        if old_ip and not self.cfg["phones"]:
            ph = {"name": "Teléfono 1", "model": "", "usb": None, "ip": old_ip.split(":")[0]}
            if old_x and old_y:
                ph["wa_x"], ph["wa_y"] = int(old_x), int(old_y)
            self.cfg["phones"].append(ph)
        save_config(self.cfg)

    # ---------------------------------------------------------- ADB base
    def run_adb(self, *args, timeout=30, binary=False, serial=None):
        if not self.adb:
            raise RuntimeError("ADB no encontrado. Instala scrcpy (incluye adb) o pulsa 'Configurar rutas'.")
        cmd = [self.adb]
        target = serial or self.serial
        if target and args and args[0] not in ADB_GLOBAL:
            cmd += ["-s", target]
        r = subprocess.run(cmd + list(args), capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        if binary:
            return r.stdout
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        return out.strip()

    def shell(self, *args, serial=None, **kw):
        return self.run_adb("shell", *args, serial=serial, **kw)

    def bg(self, fn, *args):
        """Ejecuta fn en un hilo para no congelar la interfaz."""
        def wrapper():
            try:
                fn(*args)
            except Exception as e:
                FILELOG.error(f"ERROR en {getattr(fn, '__name__', fn)}: {e}", e)
                self.log(f"ERROR: {e}")
        threading.Thread(target=wrapper, daemon=True).start()

    def log(self, msg):
        FILELOG.write("WARN" if ("ERROR" in msg or "NO enviado" in msg or "RESTRINGIDA" in msg) else "INFO", msg)
        def _append():
            self.console.configure(state="normal")
            self.console.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
            self.console.see("end")
            self.console.configure(state="disabled")
        self.after(0, _append)

    # ---------------------------------------------------------- teléfonos
    @staticmethod
    def wifi_serial(ph):
        return f"{ph['ip']}:5555" if ph.get("ip") else None

    @staticmethod
    def phone_key_of(ph):
        return (ph.get("hw") or ph["name"]) if ph else None

    def phone_name_by_key(self, key):
        ph = next((p for p in self.cfg["phones"] if self.phone_key_of(p) == key), None)
        return ph["name"] if ph else key

    def assigned_numbers(self, ph):
        key = self.phone_key_of(ph)
        return [n for n, a in self.assign.items() if a.get("key") == key]

    def assign_number(self, num, ph):
        """Liga un número al teléfono que le acaba de escribir (y lo guarda)."""
        if not ph or not num:
            return
        key = self.phone_key_of(ph)
        cur = self.assign.get(num)
        if cur and cur.get("key") == key:
            return
        self.assign[num] = {"key": key, "name": ph["name"], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_assignments(self.assign)
        if self.db:
            self._db(self.db.asignar, num, key, ph["name"], what="asignación")
        self.after(0, self.fill_tree)

    def _assign_targets(self):
        """Teléfonos entre los que se reparte: los registrados con serie; si hay menos de 2, todos los conocidos."""
        phones = [ph for ph in self.cfg["phones"] if ph.get("hw")]
        if len(phones) < 2:
            phones = [{"name": nm, "hw": hw} for hw, nm in self.cfg.get("known", {}).items()]
        return sorted(phones, key=lambda ph: (len(ph["name"]), ph["name"]))

    def list_assignments_console(self):
        """Escribe en la consola qué números tiene asignados cada teléfono."""
        groups = {}
        for n, a in self.assign.items():
            groups.setdefault(a.get("name") or a.get("key"), []).append(n)
        if not groups:
            self.log("No hay números asignados. Usa 'Repartir archivo entre teléfonos' o envía para que se asignen solos.")
            return
        self.log(f"===== ASIGNACIONES: {len(self.assign)} números en {len(groups)} teléfono(s) =====")
        for name in sorted(groups, key=lambda k: (len(k), k)):
            nums = sorted(groups[name])
            self.log(f"--- {name}: {len(nums)} números")
            for i in range(0, len(nums), 8):
                self.log("    " + "  ".join(nums[i:i + 8]))
        self.log("===== fin de asignaciones =====")

    def import_assignments_file(self):
        """Lee un Excel/CSV/TXT de números y los reparte por igual entre los teléfonos (queda fijo en la BD)."""
        p = filedialog.askopenfilename(title="Archivo de números a repartir",
                                       filetypes=[("Excel / CSV / Texto", "*.xlsx *.xlsm *.csv *.txt"), ("Todos", "*")])
        if not p:
            return
        country = re.sub(r"\D", "", self.wa_country.get()) or "51"
        try:
            valid, invalid = read_numbers_file(p, country)
        except Exception as e:
            self.log(f"No se pudo leer el archivo: {e}")
            return
        phones = self._assign_targets()
        if not phones:
            self.log("No hay teléfonos registrados ni conocidos entre los que repartir.")
            return
        nuevos = [n for n in valid if n not in self.assign]
        ya = len(valid) - len(nuevos)
        if not nuevos:
            self.log(f"Los {len(valid)} números del archivo ya estaban asignados. Nada que repartir.")
            return
        if not messagebox.askyesno(
                "Repartir números",
                f"{len(valid)} números válidos en el archivo ({ya} ya asignados, se respetan).\n"
                f"Se repartirán {len(nuevos)} números nuevos por igual entre {len(phones)} teléfonos:\n"
                + ", ".join(ph["name"] for ph in phones) + "\n\n¿Continuar?", parent=self):
            return
        # Reparto equilibrado: empieza por el teléfono que menos tiene
        counts = {self.phone_key_of(ph): len(self.assigned_numbers(ph)) for ph in phones}
        ahora = time.strftime("%Y-%m-%d %H:%M:%S")
        for n in nuevos:
            ph = min(phones, key=lambda x: (counts[self.phone_key_of(x)], x["name"]))
            key = self.phone_key_of(ph)
            counts[key] += 1
            self.assign[n] = {"key": key, "name": ph["name"], "at": ahora}
        save_assignments(self.assign)
        if self.db:
            def _todo():
                for n in nuevos:
                    a = self.assign[n]
                    self.db.asignar(n, a["key"], a["name"])
            self._db(_todo, what="asignaciones")
        self.fill_tree()
        self.log(f"Repartidos {len(nuevos)} números nuevos entre {len(phones)} teléfonos: "
                 + ", ".join(f"{ph['name']}={counts[self.phone_key_of(ph)]}" for ph in phones)
                 + (f". {len(invalid)} descartados del archivo." if invalid else "."))
        self.list_assignments_console()

    def show_assigned(self):
        """Ventana con los contactos asignados al teléfono seleccionado (copiar, exportar, quitar)."""
        ph = self.selected_phone()
        if not ph:
            self.log("Selecciona un teléfono de la lista.")
            return
        nums = sorted(self.assigned_numbers(ph))
        win = tk.Toplevel(self)
        win.title(f"Contactos de {ph['name']} ({len(nums)})")
        win.geometry("420x520")
        ttk.Label(win, text=f"{len(nums)} número(s) asignados a {ph['name']}. "
                            "Cada uno volverá a enviarse desde este teléfono.",
                  wraplength=400, justify="left").pack(fill="x", padx=8, pady=(8, 4))
        box = scrolledtext.ScrolledText(win, height=20)
        box.pack(fill="both", expand=True, padx=8)
        box.insert("1.0", "\n".join(nums))
        box.configure(state="disabled")
        row = ttk.Frame(win)
        row.pack(fill="x", padx=8, pady=8)

        def copiar():
            self.clipboard_clear()
            self.clipboard_append("\n".join(nums))
            self.log(f"{len(nums)} números de {ph['name']} copiados al portapapeles.")

        def exportar():
            path = filedialog.asksaveasfilename(parent=win, defaultextension=".txt",
                                                initialfile=f"contactos_{ph['name'].replace(' ', '_').replace('#', '')}.txt",
                                                filetypes=[("Texto", "*.txt")])
            if path:
                write_numbers(path, nums)
                self.log(f"Exportados {len(nums)} números de {ph['name']} a {os.path.basename(path)}.")

        def quitar():
            if not nums or not messagebox.askyesno(
                    "Quitar asignación", f"¿Quitar la asignación de los {len(nums)} números de {ph['name']}?\n\n"
                    "Volverán a repartirse libremente en el próximo envío.", parent=win):
                return
            for n in nums:
                self.assign.pop(n, None)
            save_assignments(self.assign)
            if self.db:
                self.bg(self._db, self.db.quitar_asignaciones, self.phone_key_of(ph))
            self.fill_tree()
            self.log(f"Asignación quitada a {len(nums)} números de {ph['name']}.")
            win.destroy()

        ttk.Button(row, text="Copiar", command=copiar).pack(side="left")
        ttk.Button(row, text="Exportar TXT", command=exportar).pack(side="left", padx=6)
        ttk.Button(row, text="Quitar asignación", command=quitar).pack(side="right")

    def phone_by_serial(self, serial):
        if not serial:
            return None
        for ph in self.cfg["phones"]:
            if serial == ph.get("usb") or serial == self.wifi_serial(ph):
                return ph
        return None

    def phone_state(self, ph):
        """Devuelve (serial_activo, texto_estado) para un teléfono registrado."""
        wifi = self.wifi_serial(ph)
        if wifi and self.connected.get(wifi) == "device":
            return wifi, "WiFi"
        if ph.get("usb") and self.connected.get(ph["usb"]) == "device":
            return ph["usb"], "USB"
        for s in (wifi, ph.get("usb")):
            if s and s in self.connected:
                return None, self.connected[s]
        return None, "desconectado"

    @staticmethod
    def is_restricted(ph):
        """True si el teléfono fue marcado como restringido por WhatsApp en las últimas 24 h."""
        return bool(ph) and bool(ph.get("restricted_at")) and (time.time() - ph["restricted_at"]) < 24 * 3600

    def mark_restricted(self, ph, detail=""):
        if not ph:
            return
        ph["restricted_at"] = time.time()
        ph["restricted_detail"] = detail
        save_config(self.cfg)
        self.after(0, self.fill_tree)

    def clear_restricted(self):
        ph = self.selected_phone()
        if not ph:
            self.log("Selecciona un teléfono de la lista.")
            return
        ph.pop("restricted_at", None)
        ph.pop("restricted_detail", None)
        save_config(self.cfg)
        self.fill_tree()
        self.log(f"{ph['name']}: marca de restricción quitada.")

    def active_phone(self):
        return self.phone_by_serial(self.serial)

    def active_name(self):
        ph = self.active_phone()
        return ph["name"] if ph else (self.serial or "?")

    def refresh_devices(self, auto=True):
        if not self.adb:
            self.log("ADB no encontrado. Instala scrcpy (winget install Genymobile.scrcpy) o pulsa 'Configurar rutas'.")
            return
        out = self.run_adb("devices")
        rows = [l.split() for l in out.splitlines()[1:] if l.strip() and not l.startswith("*")]
        self.connected = {r[0]: r[1] for r in rows if len(r) >= 2}

        # Completar número de serie de hardware y MAC WiFi de los registrados que estén conectados
        for ph in self.cfg["phones"]:
            s_on, _ = self.phone_state(ph)
            if s_on and not ph.get("hw"):
                ph["hw"] = self.shell("getprop", "ro.serialno", serial=s_on) or None
            if s_on and not ph.get("mac"):
                ph["mac"] = self.phone_mac(s_on)

        # Teléfonos conectados que aún no están registrados. Se identifican por su
        # número de serie de hardware (ro.serialno) para unir la entrada USB y la WiFi.
        for serial, state in self.connected.items():
            if state != "device" or self.phone_by_serial(serial):
                continue
            model = self.shell("getprop", "ro.product.model", serial=serial) or serial
            hw = self.shell("getprop", "ro.serialno", serial=serial) or None
            ph = next((p for p in self.cfg["phones"] if hw and p.get("hw") == hw), None)
            if ph is None and not hw and ":" in serial:
                ph = next((p for p in self.cfg["phones"] if p.get("ip") == serial.split(":")[0]), None)
            if ph is None:
                known_name = self.cfg["known"].get(hw) if hw else None
                if known_name and not any(p.get("name") == known_name for p in self.cfg["phones"]):
                    name = known_name
                else:
                    used = set()
                    for pp in self.cfg["phones"]:
                        m = re.search(r"#(\d+)$", pp.get("name", ""))
                        if m and pp.get("model") == model:
                            used.add(int(m.group(1)))
                    for kh, kn in self.cfg["known"].items():   # no reutilizar números de otros teléfonos conocidos
                        m = re.search(r"#(\d+)$", kn or "")
                        if m and kh != hw:
                            used.add(int(m.group(1)))
                    idx = 1
                    while idx in used:
                        idx += 1
                    name = f"{model} #{idx}"
                ph = {"name": name, "model": model, "usb": None, "ip": None, "hw": hw}
                self.cfg["phones"].append(ph)
                if hw:
                    self.cfg["known"][hw] = name
                self.log(f"Detectado {model} ({serial}). Se añadió a la lista como '{name}'.")
            ph["model"] = ph.get("model") or model
            ph["hw"] = ph.get("hw") or hw
            if ":" in serial:
                ph["ip"] = serial.split(":")[0]
            else:
                ph["usb"] = serial
        for ph in self.cfg["phones"]:
            s, _ = self.phone_state(ph)
            if s and not ph.get("model"):
                ph["model"] = self.shell("getprop", "ro.product.model", serial=s)
            if ph.get("model") and ph["name"].startswith("Teléfono "):
                ph["name"] = ph["model"]
        save_config(self.cfg)
        if self.db:
            self._db(self.db.upsert_telefonos, self.cfg["phones"], what="tabla de teléfonos")

        # Registro automático: teléfono por USB sin IP -> activar WiFi ADB y conectar
        if auto:
            pending = [ph for ph in self.cfg["phones"]
                       if not ph.get("ip") and ph.get("usb") and self.connected.get(ph["usb"]) == "device"]
            for ph in pending:
                self._register_serial(ph["usb"])
            if pending:
                return self.refresh_devices(auto=False)
            # Registrados con IP que no responden: buscarlos en la red (la IP pudo cambiar)
            offline = [ph for ph in self.cfg["phones"] if ph.get("ip") and not self.phone_state(ph)[0]]
            if offline and self.cfg.get("auto_scan", True) and time.time() - self._last_scan > 90:
                if self.scan_network(auto=True):
                    return self.refresh_devices(auto=False)

        ready = [s for s, st in self.connected.items() if st == "device"]
        # Activo: el teléfono actual (prefiriendo WiFi) o el primero registrado que esté conectado
        current = self.phone_by_serial(self.serial)
        preferred = self.phone_state(current)[0] if current else None
        if not preferred:
            preferred = next((s for _, s in self.connected_serials()), None)
        self.serial = preferred or (ready[0] if ready else None)
        if self.serial:
            size = self.shell("wm", "size")
            m = re.search(r"(\d+)x(\d+)", size)
            if m:
                self.screen_w, self.screen_h = int(m.group(1)), int(m.group(2))
        self.after(0, self.fill_tree)
        n = len({self.phone_by_serial(s)["name"] if self.phone_by_serial(s) else s for s in ready})
        self.log(f"{n} teléfono(s) conectado(s)." + (f" Activo: {self.active_name()}" if n else
                 " Conecta uno por USB o pulsa 'Conectar todos por WiFi'."))

    def fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._row_cache = {}
        counts = {}
        for a in list(self.assign.values()):              # copia: los hilos del envío mutan self.assign
            counts[a.get("key")] = counts.get(a.get("key"), 0) + 1
        self._contact_n = counts
        snap = self._bulk_snapshot() or self._last_snap
        phones = self.cfg["phones"]
        want = active_row = None
        for i in sorted(range(len(phones)), key=lambda i: (len(phones[i]["name"]), phones[i]["name"])):   # #1…#N
            ph = phones[i]
            view = self._row_view(ph, snap)
            self.tree.insert("", "end", iid=str(i), tags=(view[1],), values=view[0])
            self._row_cache[str(i)] = view
            serial = self.phone_state(ph)[0]
            if serial and serial == self.serial:
                active_row = str(i)
            elif not serial and self._sel_key and self.phone_key_of(ph) == self._sel_key:
                want = str(i)                             # fila DESCONECTADA elegida por el operador (para 'Conectar')
        if want or active_row:
            self.tree.selection_set(want or active_row)
        self.status_var.set(f"Activo: {self.active_name()}" if self.serial else "Sin teléfono activo")
        self._update_wa_btn_label()
        self._update_marks_label()

    def _toggle_all_marks(self):
        """Clic en la cabecera ☑: marca todos, o desmarca todos si ya lo estaban. Durante un envío no desmarca en bloque."""
        todos = all(self.phone_enabled(p) for p in self.cfg["phones"])
        if todos and self.bulk_active:
            self.log("Hay un envío en curso: para retirar teléfonos desmárcalos ☐ uno a uno (así no se para todo de golpe).")
            return
        self.mark_phones((lambda p: False) if todos else (lambda p: True))

    def _on_tree_click(self, e):
        """Clic en la columna ☑: marca/desmarca sin cambiar la selección (ni el teléfono activo ni la captura)."""
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#1":
            return None
        iid = self.tree.identify_row(e.y)
        if iid:
            try:
                target = self.cfg["phones"][int(iid)]
            except (ValueError, IndexError):
                return "break"
            self.mark_phones(lambda p: (not self.phone_enabled(p)) if p is target else self.phone_enabled(p))
        return "break"

    def _update_marks_label(self):
        phones = self.cfg["phones"]
        marked = [p for p in phones if self.phone_enabled(p)]
        ready = [p for p in marked if self.phone_state(p)[0]]
        if hasattr(self, "marks_var"):
            self.marks_var.set(f"Marcados {len(marked)} de {len(phones)} · conectados {len(ready)}")
        if hasattr(self, "btn_send") and hasattr(self, "wa_split"):
            self.btn_send.configure(text=(f"▶ Enviar con {len(ready)} teléfono(s)" if self.wa_split.get()
                                          else f"▶ Enviar solo con {self.active_name()}"))

    STEP_CORTO = (("inicio", "encendiendo pantalla"), ("buscando el listado", "abriendo WhatsApp"),
                  ("listado OK", "abriendo el buscador"), ("buscador abierto", "buscando el número"),
                  ("resultado encontrado", "abriendo el chat"), ("espera de", "esperando con el chat abierto"),
                  ("chat abierto", "chat abierto"), ("tecleando", "escribiendo el mensaje"),
                  ("mensaje tecleado", "tocando Enviar"), ("tocar Enviar", "tocando Enviar"),
                  ("enviado confirmado", "enviado ✔"))
    WHY_CORTO = (("buscador", "no abre el buscador"), ("listado de chats", "WhatsApp no abre"),
                 ("no se abrió el chat", "no abre los chats"), ("no responde", "no responde"),
                 ("teclado", "falla el teclado ADB"))

    def _row_view(self, ph, snap):
        """(valores, tag) de una fila de la tabla. SOLO HILO PRINCIPAL. No hace ADB ni BD."""
        key, on = self.phone_key_of(ph), self.phone_enabled(ph)
        serial, st = self.phone_state(ph)
        h = self.health.get(key) or {}
        env = fal = pen = ""
        if self.is_restricted(ph):
            hrs = (time.time() - ph["restricted_at"]) / 3600
            estado, tag = f"RESTRINGIDO por WhatsApp (hace {hrs:.0f} h)", "restricted"
        elif serial:
            if h.get("conn") == "noresp":
                estado, tag = "NO RESPONDE (¿apagado o colgado?)", "out"
            else:
                bat = h.get("bat")
                estado = f"Listo · {st}" + (f" · {bat}%{'⚡' if h.get('carga') else ''}" if bat is not None else "")
                tag = "on"
                if bat is not None and bat < 20 and not h.get("carga"):
                    estado, tag = estado + " ¡BATERÍA BAJA!", "warn"
            act = h.get("act")
            if act and not act[0] and time.time() - act[2] < 1800:
                estado, tag = "✖ " + act[1][:60], "out"          # la última activación falló (PIN, buscador…)
        else:
            conn = h.get("conn")
            estado = {"rechaza": "Falta CABLE USB (se reinició)", "mudo": "APAGADO o sin WiFi",
                      "unauth": "Acepta el permiso en el teléfono"}.get(conn, "Sin conexión" if st == "desconectado" else st)
            tag = "warn" if conn in ("rechaza", "unauth") else "off"
        if snap:
            p, own_n = snap["per"].get(key), snap["own"].get(key, 0)
            if p or own_n:
                env, fal, pen = (p or {}).get("sent", 0), (p or {}).get("failed", 0), own_n
            if snap.get("live"):
                if key in snap["alive"]:
                    ts, _num, txt = snap["steps"].get(ph["name"], (snap["ts"], None, "tomando número"))
                    idle = int(snap["ts"] - ts)
                    corto = next((c for pre, c in self.STEP_CORTO if txt.startswith(pre)), txt[:28])
                    if idle > snap["stall"]:
                        estado, tag = f"ENVIANDO · ¿TRABADO? {idle // 60}:{idle % 60:02d} sin avanzar", "warn"
                    else:
                        estado, tag = f"ENVIANDO · {corto} ({idle // 60}:{idle % 60:02d})", "send"
                elif key in snap["fixing"]:
                    estado, tag = "🔧 Reparando WhatsApp…", "warn"
                elif p and p["state"] == "SALIÓ":
                    why = next((c for pat, c in self.WHY_CORTO if pat in p["why"]), p["why"][:30])
                    estado, tag = f"FUERA {time.strftime('%H:%M', time.localtime(p['since']))} · {why}", "out"
                elif own_n and not on:
                    estado, tag = f"No marcado ☐ · {own_n} números esperando", "skip"
                elif own_n:
                    estado, tag = f"{estado} · {own_n} esperando", "out"
                elif p:
                    estado, tag = "Terminó ✔", "done"
        if not on and tag in ("on", "off"):
            tag = "skip"
        return (("☑" if on else "☐"), ph["name"], estado, env, fal, pen, self._contact_n.get(key, 0),
                ph.get("ip") or "-", ph.get("model", ""), serial or "-"), tag

    def _paint_rows(self, snap=None):
        """Actualiza celdas sin reconstruir filas (no pierde selección ni scroll)."""
        snap = snap or self._last_snap
        for i, ph in enumerate(list(self.cfg["phones"])):
            iid = str(i)
            if not self.tree.exists(iid):
                continue
            view = self._row_view(ph, snap)
            if self._row_cache.get(iid) != view:
                self.tree.item(iid, values=view[0], tags=(view[1],))
                self._row_cache[iid] = view

    def on_tree_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        ph = self.cfg["phones"][int(sel[0])]
        key = self.phone_key_of(ph)
        repeated = key == self._sel_key
        self._sel_key = key
        serial, state = self.phone_state(ph)
        if not serial:
            if not repeated:
                self.log(f"{ph['name']} está {state}. Pulsa ⚡ ACTIVAR TODOS o conéctalo por USB.")
            return
        if serial != self.serial:
            self.serial = serial
            self.status_var.set(f"Activo: {ph['name']} ({state})")
            self._update_wa_btn_label()
            self.log(f"Teléfono activo: {ph['name']} por {state}")
            self.bg(self._after_select)

    def _after_select(self):
        size = self.shell("wm", "size")
        m = re.search(r"(\d+)x(\d+)", size)
        if m:
            self.screen_w, self.screen_h = int(m.group(1)), int(m.group(2))
        self.take_screenshot()

    def selected_phone(self):
        sel = self.tree.selection()
        return self.cfg["phones"][int(sel[0])] if sel else None

    def _register_serial(self, serial, make_active=True):
        """Activa ADB por WiFi en un teléfono USB, lo conecta y guarda su IP. Devuelve True si quedó por WiFi."""
        model = self.shell("getprop", "ro.product.model", serial=serial)
        hw = (self.shell("getprop", "ro.serialno", serial=serial) or "").strip() or None
        # Primero por número de serie (la IP y el serial USB pueden pertenecer ya a otro registro)
        ph = (next((p for p in self.cfg["phones"] if hw and p.get("hw") == hw), None)
              or self.phone_by_serial(serial))
        quien = ph["name"] if ph else (self.cfg["known"].get(hw) or model)
        ipinfo = self.shell("ip", "-f", "inet", "addr", "show", "wlan0", serial=serial)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ipinfo)
        if not m:
            self.log(f"{quien}: no tiene IP WiFi. Conéctalo a la misma red WiFi que la PC y pulsa 'Actualizar'.")
            return False
        ip = m.group(1)
        self.log(f"{quien}: activando ADB por WiFi en {ip}:5555 ...")
        self.run_adb("tcpip", "5555", serial=serial)
        time.sleep(2)
        res = self.run_adb("connect", f"{ip}:5555", timeout=20)
        self.log(f"{quien}: {res}")
        ph = ph or self.phone_by_serial(f"{ip}:5555")
        mac = self.phone_mac(serial)
        if ph:
            for other in self.cfg["phones"]:            # esa IP ya no es de otro teléfono
                if other is not ph and other.get("ip") == ip:
                    other["ip"] = None
            ph.update(usb=serial, ip=ip, model=model, hw=ph.get("hw") or hw, mac=mac or ph.get("mac"))
        else:
            name = self.cfg["known"].get(hw) or model
            self.cfg["phones"].append({"name": name, "model": model, "usb": serial, "ip": ip, "hw": hw, "mac": mac})
        if hw:
            self.cfg["known"][hw] = (ph or self.cfg["phones"][-1])["name"]
        save_config(self.cfg)
        ok = "connected" in res and "unable" not in res
        if ok:
            if make_active:
                self.serial = f"{ip}:5555"
            self.log(f"{quien} registrado por WiFi ({ip}). Ya puedes quitar el cable USB.")
        return ok

    def phone_mac(self, serial):
        """MAC de la interfaz WiFi del teléfono (para reconocerlo en la red aunque cambie de IP)."""
        for args in (("ip", "addr", "show", "wlan0"), ("cat", "/sys/class/net/wlan0/address")):
            out = self.shell(*args, serial=serial, timeout=10)
            m = re.search(r"([0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5})", out or "")
            if m:
                return norm_mac(m.group(1))
        return None

    def scan_network(self, auto=False):
        """Busca en la red WiFi los teléfonos con ADB activo (puerto 5555), los conecta y
        actualiza la IP de los ya registrados. Devuelve True si conectó alguno nuevo."""
        if not self._scanning.acquire(blocking=False):
            if not auto:
                self.log("Ya hay una búsqueda en curso.")
            return False
        try:
            self._last_scan = time.time()
            prefixes = subnet_prefixes([ph.get("ip") for ph in self.cfg["phones"]])
            if not prefixes:
                self.log("No se detectó ninguna red local en la PC. ¿Está conectada al WiFi/LAN?")
                return False
            # Primero la red donde ya hay teléfonos registrados; el resto (VPN, otras tarjetas) solo si no aparece nada
            known = {p for p in prefixes if any((ph.get("ip") or "").startswith(p) for ph in self.cfg["phones"])}
            first = sorted(known) or prefixes
            self.log(f"Buscando teléfonos con ADB WiFi en {', '.join(p + '0/24' for p in first)} ...")
            found = scan_adb_hosts(first)
            rest = [p for p in prefixes if p not in first]
            if not found and rest:
                self.log(f"Nada ahí; probando también {', '.join(p + '0/24' for p in rest)} ...")
                found = scan_adb_hosts(rest)
            # Descubrimiento mDNS (Android 11+ anuncia el servicio _adb._tcp); complementa el escaneo
            try:
                out = self.run_adb("mdns", "services", timeout=8)
                for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+):(\d+)", out):
                    if m.group(2) == str(ADB_PORT) and m.group(1) not in found:
                        found.append(m.group(1))
            except Exception:
                pass
            already = {s for s, st in self.connected.items() if st == "device" and ":" in s}
            targets = [ip for ip in found if f"{ip}:{ADB_PORT}" not in already]
            self.log(f"{len(found)} equipo(s) con ADB WiFi activo; {len(targets)} sin conectar." if found
                     else "Ningún equipo con ADB WiFi activo en la red.")
            if targets:
                with ThreadPoolExecutor(max_workers=16) as ex:
                    list(ex.map(lambda ip: self.run_adb("connect", f"{ip}:{ADB_PORT}", timeout=15), targets))
                time.sleep(1)
            out = self.run_adb("devices")
            rows = [l.split() for l in out.splitlines()[1:] if l.strip() and not l.startswith("*")]
            self.connected = {r[0]: r[1] for r in rows if len(r) >= 2}

            new_ok = False
            for ip in targets:
                serial = f"{ip}:{ADB_PORT}"
                st = self.connected.get(serial)
                if st != "device":
                    if st:
                        self.log(f"{ip}: ADB responde pero está '{st}' (acepta la depuración en el teléfono).")
                    else:
                        self.log(f"{ip}: tiene el puerto abierto pero no acepta ADB (¿otro equipo?).")
                    continue
                hw = self.shell("getprop", "ro.serialno", serial=serial, timeout=10) or None
                ph = next((p for p in self.cfg["phones"] if hw and p.get("hw") == hw), None)
                if ph:
                    for other in self.cfg["phones"]:        # esa IP ya no es de otro teléfono
                        if other is not ph and other.get("ip") == ip:
                            other["ip"] = None
                if ph and ph.get("ip") != ip:
                    self.log(f"{ph['name']}: cambió de IP {ph.get('ip') or '-'} -> {ip}. Actualizado.")
                    ph["ip"] = ip
                elif ph:
                    self.log(f"{ph['name']}: reconectado en {ip}.")
                else:
                    model = self.shell("getprop", "ro.product.model", serial=serial, timeout=10) or serial
                    self.log(f"{model} ({ip}): teléfono nuevo con ADB WiFi, se añadirá a la lista.")
                new_ok = True
            save_config(self.cfg)

            # Los que siguen sin responder: ¿están en la red (por MAC) pero sin ADB WiFi?
            missing = [ph for ph in self.cfg["phones"] if ph.get("ip") and self.connected.get(self.wifi_serial(ph)) != "device"]
            if missing:
                arp = arp_table()
                for ph in missing:
                    ip_seen = arp.get(ph.get("mac") or "")
                    if ip_seen:
                        if ip_seen != ph.get("ip"):
                            ph["ip"] = ip_seen
                        for other in self.cfg["phones"]:
                            if other is not ph and other.get("ip") == ip_seen:
                                other["ip"] = None
                        self.log(f"{ph['name']}: está en la red ({ip_seen}) pero ADB WiFi está apagado "
                                 "(se apaga al reiniciar). Conéctalo un momento por USB.")
                    else:
                        self.log(f"{ph['name']}: no se ve en la red WiFi (apagado, sin WiFi o en otra red).")
                save_config(self.cfg)
            if not auto:
                self.refresh_devices(auto=False)
            return new_ok
        finally:
            self._scanning.release()

    def register_usb(self):
        """Registra manualmente todos los teléfonos que estén por USB (normalmente ocurre solo)."""
        self.refresh_devices(auto=False)
        usb = [s for s, st in self.connected.items() if st == "device" and ":" not in s]
        if not usb:
            self.log("No hay ningún teléfono por USB. Conecta el cable y acepta la depuración USB.")
            return
        for serial in usb:
            self._register_serial(serial)
        self.refresh_devices(auto=False)

    def connect_all(self):
        phones = [ph for ph in self.cfg["phones"] if ph.get("ip")]
        if not phones:
            self.log("No hay teléfonos con IP registrada. Usa 'Registrar por USB' primero.")
            return
        self.log(f"Conectando {len(phones)} teléfono(s) por WiFi...")

        def one(ph):
            try:
                res = self.run_adb("connect", self.wifi_serial(ph), timeout=20)
                self.log(f"{ph['name']}: {res}")
            except subprocess.TimeoutExpired:
                self.log(f"{ph['name']}: no responde en {ph['ip']} (¿apagado o sin WiFi?).")
            except Exception as e:
                self.log(f"{ph['name']}: no se pudo conectar ({str(e)[:80]}).")
        threads = [threading.Thread(target=one, args=(ph,), daemon=True) for ph in phones]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        self.refresh_devices()

    def connect_selected(self):
        ph = self.selected_phone()
        if not ph:
            self.log("Selecciona un teléfono de la lista.")
            return
        if not ph.get("ip"):
            if ph.get("usb") and self.connected.get(ph["usb"]) == "device":
                self._register_serial(ph["usb"])
                self.refresh_devices(auto=False)
            else:
                self.log(f"{ph['name']} no tiene IP registrada. Conéctalo por cable USB y se registrará solo.")
            return
        try:
            res = self.run_adb("connect", self.wifi_serial(ph), timeout=20)
        except subprocess.TimeoutExpired:
            self.log(f"{ph['name']}: no responde en {ph['ip']} (¿apagado o sin WiFi?). "
                     "Enciéndelo; si se reinició, pásale el cable USB.")
            return
        self.log(f"{ph['name']}: {res}")
        if "connected" in res and "unable" not in res:
            self.serial = self.wifi_serial(ph)
        self.refresh_devices()

    def disconnect_selected(self):
        ph = self.selected_phone()
        if not ph or not ph.get("ip"):
            return
        self.log(f"{ph['name']}: " + self.run_adb("disconnect", self.wifi_serial(ph)))
        self.refresh_devices()

    def rename_selected(self):
        ph = self.selected_phone()
        if not ph:
            return
        name = simpledialog.askstring("Renombrar", "Nombre para este teléfono:", initialvalue=ph["name"], parent=self)
        if name and name.strip():
            ph["name"] = name.strip()
            if ph.get("hw"):
                self.cfg["known"][ph["hw"]] = ph["name"]
            save_config(self.cfg)
            self.fill_tree()

    def edit_ip_selected(self):
        ph = self.selected_phone()
        if not ph:
            return
        ip = simpledialog.askstring("IP", "IP WiFi del teléfono:", initialvalue=ph.get("ip") or "", parent=self)
        if ip and ip.strip():
            ph["ip"] = ip.strip().split(":")[0]
            save_config(self.cfg)
            self.fill_tree()

    def remove_selected(self):
        if self.bulk_active:
            self.log("No se puede quitar un teléfono de la lista durante un envío.")
            return
        ph = self.selected_phone()
        if not ph:
            return
        if messagebox.askyesno("Quitar", f"¿Quitar '{ph['name']}' de la lista?", parent=self):
            if ph.get("ip"):
                self.bg(self.run_adb, "disconnect", self.wifi_serial(ph))
            self.cfg["phones"].remove(ph)
            self._sel_key = None
            off = set(self._send_off) - {ph.get("hw"), ph.get("name")}
            self._send_off = frozenset(off)
            self.cfg["send_off"] = sorted(off)
            save_config(self.cfg)
            self.fill_tree()

    def connected_serials(self):
        """Un serial por teléfono registrado y conectado (prefiere WiFi)."""
        out = []
        for ph in self.cfg["phones"]:
            serial, _ = self.phone_state(ph)
            if serial:
                out.append((ph, serial))
        return out

    # ---------------------------------------------------------- selección de teléfonos para enviar (☑)
    def phone_enabled(self, ph):
        """¿Marcado ☑ para enviar? Seguro desde cualquier hilo (lee un frozenset inmutable). Teléfono nuevo = marcado."""
        if not ph:
            return True
        off = self._send_off
        return ph.get("hw") not in off and ph.get("name") not in off

    def send_targets(self):
        """Registrados + conectados + marcados ☑. (connected_serials NO cambia: Monitor, scrcpy, restaurar teclado
        y refresh_devices siguen viendo todos.)"""
        return [(ph, s) for ph, s in self.connected_serials() if self.phone_enabled(ph)]

    def phone_by_key(self, key):
        return next((p for p in self.cfg["phones"] if self.phone_key_of(p) == key), None)

    def mark_phones(self, rule):
        """SOLO HILO PRINCIPAL. Marca ☑ los teléfonos con rule(ph) verdadero y desmarca el resto."""
        phones = list(self.cfg["phones"])
        before = {id(p): self.phone_enabled(p) for p in phones}
        ids = {x for p in phones for x in (p.get("hw"), p.get("name")) if x}
        off = set(self._send_off) - ids               # conserva claves de teléfonos que hoy no están en la lista
        off |= {self.phone_key_of(p) for p in phones if not rule(p)}
        self._send_off = frozenset(off)
        self.cfg["send_off"] = sorted(off)
        save_config(self.cfg)
        self._paint_rows()
        self._update_marks_label()
        names = [p["name"] for p in phones if not self.phone_enabled(p)]
        self.log(f"Marcados para enviar: {len(phones) - len(names)} de {len(phones)}"
                 + (f" (desmarcados ☐: {', '.join(names)})." if names else "."))
        for p in phones:
            if self.phone_enabled(p) != before[id(p)]:
                self._mark_changed(p, self.phone_enabled(p))

    def _mark_changed(self, ph, on):
        """Efecto de marcar/desmarcar sobre un envío EN CURSO."""
        if not (self.bulk_active and self._bulk):
            return
        if on:
            self.bg(self.wa_join_selected, ph, self._wa_pkg(), bool(self.wa_human.get()))
        else:
            self.log(f"[{ph['name']}] desmarcado ☐: termina el mensaje que está escribiendo y se retira del envío. "
                     "Sus números quedan en espera (no se reasignan).")

    def _adb_devices(self):
        """Relee `adb devices` (consulta al servidor adb local; no toca los teléfonos)."""
        for intento in (1, 2):
            out = self.run_adb("devices", timeout=15)
            rows = [l.split() for l in out.splitlines()[1:] if l.strip() and not l.startswith("*")]
            rows = [r for r in rows if len(r) >= 2]
            if rows or not self.connected or intento == 2:     # un vacío transitorio (arranque del servidor adb) se reintenta
                break
            time.sleep(1.0)
        self.connected = {r[0]: r[1] for r in rows}

    def _busy_keys(self):
        """Teléfonos que NO se pueden tocar: están enviando o activándose."""
        with self._bulk_lock:
            bulk = self._bulk
            alive = {k for k, (t, _s) in bulk["active"].items() if t.is_alive()} if bulk else set()
            return alive | set(self._act_busy)

    # ---------------------------------------------------------- interfaz
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="buscando teléfonos...")
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="Configurar rutas", command=self.configure_paths).pack(side="right")
        ttk.Button(top, text="📄 Logs", command=self.open_logs).pack(side="right", padx=4)
        ttk.Button(top, text="scrcpy del activo", command=self.launch_scrcpy).pack(side="right", padx=4)
        ttk.Button(top, text="🖥  Monitor en vivo (todos)", command=self.open_monitor).pack(side="right", padx=4)
        ttk.Button(top, text="Actualizar", command=lambda: self.bg(self.refresh_devices)).pack(side="right", padx=4)

        # Tablero del envío: siempre visible (el operador puede estar en otra pestaña)
        BG = "#f4f6f8"
        bar = tk.Frame(self, bg=BG, highlightbackground="#c9d1d9", highlightthickness=1)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        l1 = tk.Frame(bar, bg=BG)
        l1.pack(fill="x", padx=8, pady=(4, 0))
        self.bn_title = tk.Label(l1, text="SIN ENVÍO EN CURSO", bg=BG, fg="#555555", font=("Segoe UI", 10, "bold"))
        self.bn_sent = tk.Label(l1, bg=BG, fg="#0a7d2c", font=("Segoe UI", 15, "bold"))
        self.bn_fail = tk.Label(l1, bg=BG, fg="#c00000", font=("Segoe UI", 15, "bold"))
        self.bn_left = tk.Label(l1, bg=BG, fg="#1f2937", font=("Segoe UI", 15, "bold"))
        for w in (self.bn_title, self.bn_sent, self.bn_fail, self.bn_left):
            w.pack(side="left", padx=(0, 18))
        self.btn_activar = tk.Button(l1, text="⚡ ACTIVAR TODOS", bg="#0a7d2c", fg="white", activebackground="#086323",
                                     activeforeground="white", font=("Segoe UI", 10, "bold"), padx=14, relief="flat",
                                     command=self.activate_all_clicked)
        self.btn_activar.pack(side="right")
        self.bn_rate = tk.Label(bar, bg=BG, anchor="w", font=("Segoe UI", 10))
        self.bn_rate.pack(fill="x", padx=8)
        self.bn_phones = tk.Label(bar, bg=BG, anchor="w", justify="left", wraplength=1250, font=("Segoe UI", 10, "bold"))
        self.bn_phones.pack(fill="x", padx=8, pady=(0, 4))
        bar.bind("<Configure>", lambda e: self.bn_phones.configure(wraplength=max(300, e.width - 24)))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # Panel de teléfonos
        left = ttk.Labelframe(body, text="Teléfonos", padding=6)
        body.add(left, weight=2)
        cols = ("usar", "nombre", "estado", "env", "fal", "pen", "contactos", "ip", "modelo", "serie")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=10, selectmode="browse",
                                 displaycolumns=cols if self.cfg.get("ver_tecnico") else cols[:8])
        for c, w, txt, anc in (("usar", 30, "☑", "center"), ("nombre", 92, "Teléfono", "w"),
                               ("estado", 210, "Estado", "w"), ("env", 46, "Env.", "e"),
                               ("fal", 46, "Fall.", "e"), ("pen", 50, "Faltan", "e"),
                               ("contactos", 62, "Contact.", "e"), ("ip", 84, "IP", "w"),
                               ("modelo", 80, "Modelo", "w"), ("serie", 120, "Serie ADB", "w")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, minwidth=(170 if c == "estado" else 24), anchor=anc, stretch=(c == "estado"))
        self.tree.heading("usar", command=self._toggle_all_marks)
        self.tree.tag_configure("on", foreground="#0a7d2c")
        self.tree.tag_configure("send", foreground="#0a7d2c", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("done", foreground="#33658a")
        self.tree.tag_configure("off", foreground="#888888")
        self.tree.tag_configure("skip", foreground="#9aa0a6")
        self.tree.tag_configure("warn", foreground="#b06000")
        self.tree.tag_configure("out", foreground="#b00000", background="#fde8e8", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("restricted", foreground="#c00000", font=("Segoe UI", 9, "bold"))
        hbar = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=hbar.set)
        self.tree.pack(fill="both", expand=True)
        hbar.pack(fill="x")
        self.after(600, lambda: body.sashpos(0, max(640, body.sashpos(0))))   # que quepan ☑, Estado y los contadores
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: "break" if self.tree.identify_column(e.x) == "#1" else self.rename_selected())

        adm = ttk.Frame(left)
        adm.pack(fill="x", pady=(4, 0))
        ttk.Label(adm, text="Marcar ☑:").pack(side="left")
        ttk.Button(adm, text="Todos", width=7, command=lambda: self.mark_phones(lambda p: True)).pack(side="left", padx=2)
        ttk.Button(adm, text="Ninguno", width=8, command=lambda: self.mark_phones(lambda p: False)).pack(side="left", padx=2)
        ttk.Button(adm, text="Solo conectados",
                   command=lambda: self.mark_phones(lambda p: bool(self.phone_state(p)[0]))).pack(side="left", padx=2)
        self.marks_var = tk.StringVar(value="")
        ttk.Label(adm, textvariable=self.marks_var, foreground="#555").pack(side="left", padx=8)

        pb = ttk.Frame(left)
        pb.pack(fill="x", pady=(6, 0))
        ttk.Button(pb, text="➕ Registrar por USB", command=lambda: self.bg(self.register_usb)).grid(row=0, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="📶 Conectar todos por WiFi", command=lambda: self.bg(self.connect_all)).grid(row=0, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="🔍 Buscar en la red WiFi", command=lambda: self.bg(self.scan_network)).grid(row=4, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="➕ Sumar al envío en curso", command=self._join_click).grid(row=4, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="Conectar", command=lambda: self.bg(self.connect_selected)).grid(row=1, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="Desconectar", command=lambda: self.bg(self.disconnect_selected)).grid(row=1, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="Renombrar", command=self.rename_selected).grid(row=2, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="IP manual", command=self.edit_ip_selected).grid(row=2, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="Quitar de la lista", command=self.remove_selected).grid(row=3, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="Quitar marca de restricción", command=self.clear_restricted).grid(row=3, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="👥 Contactos del teléfono", command=self.show_assigned).grid(row=5, column=0, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="📋 Ver asignaciones en consola", command=self.list_assignments_console).grid(row=5, column=1, sticky="we", padx=2, pady=2)
        ttk.Button(pb, text="📥 Repartir archivo entre teléfonos", command=self.import_assignments_file).grid(row=6, column=0, columnspan=2, sticky="we", padx=2, pady=2)
        pb.columnconfigure(0, weight=1)
        pb.columnconfigure(1, weight=1)
        ttk.Label(left, foreground="#666", wraplength=330, justify="left",
                  text="Clic en ☑/☐ para elegir qué teléfonos envían. Teléfono nuevo: conéctalo por cable y se registra solo. "
                       "Si algo falla o se apagaron: ⚡ ACTIVAR TODOS (arriba a la derecha).").pack(fill="x", pady=(6, 0))

        # Pestañas
        nb = ttk.Notebook(body)
        body.add(nb, weight=3)
        nb.add(self._tab_whatsapp(nb), text="WhatsApp")
        nb.add(self._tab_control(nb), text="Control")
        nb.add(self._tab_apps(nb), text="Apps y URLs")

        # Pantalla
        right = ttk.Labelframe(body, text="Pantalla del activo (clic = tocar, clic derecho = fijar botón Enviar)", padding=6)
        body.add(right, weight=2)
        btns = ttk.Frame(right)
        btns.pack(fill="x")
        ttk.Button(btns, text="Capturar", command=lambda: self.bg(self.take_screenshot)).pack(side="left")
        ttk.Button(btns, text="Guardar PNG", command=self.save_screenshot).pack(side="left", padx=4)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Auto cada 3 s", variable=self.auto_var, command=self.auto_capture).pack(side="left", padx=4)
        self.coord_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.coord_var).pack(side="right")
        self.canvas = tk.Canvas(right, bg="#222", width=300, height=520, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Button-3>", self.on_canvas_right_click)
        self.canvas.bind("<Motion>", self.on_canvas_move)

        # Consola
        cf = ttk.Labelframe(self, text="Registro", padding=4)
        cf.pack(side="bottom", fill="x", padx=8, pady=(0, 8), before=body)
        self.console = scrolledtext.ScrolledText(cf, height=6, state="disabled", font=("Consolas", 9))
        self.console.pack(fill="x")

    def _tab_whatsapp(self, parent):
        f = ttk.Frame(parent, padding=10)
        g = ttk.Labelframe(f, text="Mensaje individual (teléfono activo)", padding=8)
        g.pack(fill="x")
        sel = ttk.Frame(g)
        sel.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(sel, text="App:").pack(side="left")
        self.wa_app = tk.StringVar(value=self.cfg.get("wa_app", "WhatsApp Business"))
        ttk.Combobox(sel, textvariable=self.wa_app, values=list(WA_APPS), state="readonly", width=20).pack(side="left", padx=6)
        ttk.Button(sel, text="Abrir la app", command=lambda: self.bg(self.wa_launch)).pack(side="left")
        ttk.Label(g, text="Número (con código de país, ej. 51982123456):").grid(row=1, column=0, sticky="w")
        self.wa_num = tk.StringVar()
        ttk.Entry(g, textvariable=self.wa_num, width=28).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(g, text="Mensaje (cuerpo):").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        self.wa_msg = tk.Text(g, height=4, width=50)
        self.wa_msg.grid(row=2, column=1, sticky="we", padx=6, pady=(6, 0))
        g.columnconfigure(1, weight=1)

        var = ttk.Frame(g)
        var.grid(row=5, column=0, columnspan=2, sticky="we", pady=(8, 0))
        self.wa_vary = tk.BooleanVar(value=self.cfg.get("wa_vary", True))
        ttk.Checkbutton(var, text="Variar cada mensaje: saludo aleatorio al inicio + cierre con emoji al final",
                        variable=self.wa_vary).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(var, text="Saludos (uno por línea, {hora} = buenos días/tardes/noches):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(var, text="Cierres / emojis (uno por línea):").grid(row=1, column=1, sticky="w", pady=(4, 0), padx=(6, 0))
        self.wa_saludos = tk.Text(var, height=4, width=34)
        self.wa_saludos.grid(row=2, column=0, sticky="we")
        self.wa_cierres = tk.Text(var, height=4, width=34)
        self.wa_cierres.grid(row=2, column=1, sticky="we", padx=(6, 0))
        self.wa_saludos.insert("1.0", self.cfg.get("wa_saludos") or "\n".join(SALUDOS))
        self.wa_cierres.insert("1.0", self.cfg.get("wa_cierres") or "\n".join(CIERRES))
        var.columnconfigure(0, weight=1)
        var.columnconfigure(1, weight=1)
        pv = ttk.Frame(var)
        pv.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(pv, text="Vista previa", command=self.wa_preview).pack(side="left")
        ttk.Button(pv, text="Restaurar listas", command=self.wa_restore_lists).pack(side="left", padx=6)

        opts = ttk.Frame(g)
        opts.grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(opts, text="Carga del chat:").pack(side="left")
        self.wa_delay = tk.StringVar(value=self.cfg.get("wa_delay", "4"))
        ttk.Entry(opts, textvariable=self.wa_delay, width=4).pack(side="left", padx=3)
        ttk.Label(opts, text="a").pack(side="left")
        self.wa_delay_max = tk.StringVar(value=self.cfg.get("wa_delay_max", "8"))
        ttk.Entry(opts, textvariable=self.wa_delay_max, width=4).pack(side="left", padx=3)
        ttk.Label(opts, text="s").pack(side="left")
        ttk.Label(opts, text="Espera con el chat abierto antes de enviar:").pack(side="left", padx=(14, 0))
        self.wa_cool = tk.StringVar(value=self.cfg.get("wa_cool", "40"))
        ttk.Entry(opts, textvariable=self.wa_cool, width=5).pack(side="left", padx=3)
        ttk.Label(opts, text="a").pack(side="left")
        self.wa_cool_max = tk.StringVar(value=self.cfg.get("wa_cool_max", "120"))
        ttk.Entry(opts, textvariable=self.wa_cool_max, width=5).pack(side="left", padx=3)
        ttk.Label(opts, text="s (al azar, en cada mensaje)").pack(side="left")
        ttk.Label(opts, text="Lecturas de pantalla a la vez:").pack(side="left", padx=(14, 0))
        self.wa_dump_par = tk.StringVar(value=str(self.cfg.get("wa_dump_par", 6)))
        ttk.Entry(opts, textvariable=self.wa_dump_par, width=3).pack(side="left", padx=3)
        ttk.Label(opts, text="Botón Enviar de respaldo:").pack(side="left", padx=(10, 4))
        self.wa_btn_var = tk.StringVar(value="no fijado (usa Enter)")
        ttk.Label(opts, textvariable=self.wa_btn_var, foreground="#555").pack(side="left")

        ttk.Button(g, text="Abrir chat", command=lambda: self.bg(self.wa_open_only)).grid(row=4, column=0, sticky="w")
        ttk.Button(g, text="Abrir y enviar", command=lambda: self.bg(self.wa_send_one)).grid(row=4, column=1, sticky="w", padx=6)

        hm = ttk.Frame(g)
        hm.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.wa_human = tk.BooleanVar(value=self.cfg.get("wa_human", True))
        ttk.Checkbutton(hm, text="Modo humano: buscar el número en la app y teclear letra por letra",
                        variable=self.wa_human, command=self._save_human).pack(side="left")
        vel = ttk.Frame(g)
        vel.grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Label(vel, text="Velocidad de tecleo (s por carácter):").pack(side="left")
        self.wa_type_min = tk.StringVar(value=self.cfg.get("wa_type_min", "0.05"))
        ttk.Entry(vel, textvariable=self.wa_type_min, width=5).pack(side="left", padx=3)
        ttk.Label(vel, text="a").pack(side="left")
        self.wa_type_max = tk.StringVar(value=self.cfg.get("wa_type_max", "0.15"))
        ttk.Entry(vel, textvariable=self.wa_type_max, width=5).pack(side="left", padx=3)
        ttk.Label(vel, text="(los enlaces se insertan de golpe)", foreground="#666").pack(side="left", padx=(6, 0))
        ttk.Button(vel, text="Preparar teclado ADB", command=lambda: self.bg(self.wa_prepare_kb)).pack(side="left", padx=(12, 0))
        ttk.Button(vel, text="Restaurar teclado", command=lambda: self.bg(self.wa_restore_kb)).pack(side="left", padx=4)

        b = ttk.Labelframe(f, text="Envío masivo", padding=8)
        b.pack(side="bottom", fill="both", expand=True, pady=(10, 0), before=g)
        top = ttk.Frame(b)
        top.pack(fill="x", pady=(0, 4))
        ttk.Button(top, text="📂 Cargar Excel / CSV / TXT", command=self.wa_load_file).pack(side="left")
        ttk.Button(top, text="🗄 Números de la base de datos", command=lambda: self.bg(self.wa_load_db)).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Código de país:").pack(side="left", padx=(12, 2))
        self.wa_country = tk.StringVar(value=self.cfg.get("wa_country", "51"))
        ttk.Entry(top, textvariable=self.wa_country, width=5).pack(side="left")
        self.wa_count = tk.StringVar(value="0 números")
        ttk.Label(top, textvariable=self.wa_count, font=("Segoe UI", 9, "bold")).pack(side="left", padx=12)
        ttk.Button(top, text="Limpiar", command=self.wa_clear_list).pack(side="right")
        self.wa_progress = ttk.Progressbar(b, length=200)
        self.wa_progress.pack(side="bottom", fill="x")
        row3 = ttk.Frame(b)
        row3.pack(side="bottom", fill="x", pady=(0, 4))
        row2 = ttk.Frame(b)
        row2.pack(side="bottom", fill="x")
        row = ttk.Frame(b)
        row.pack(side="bottom", fill="x", pady=4)
        ttk.Label(b, foreground="#666", wraplength=620, justify="left",
                  text="Si la lista está vacía, '▶ Enviar' usa los números asignados de la base de datos. "
                       "Excel: los celulares pueden ir en cualquier columna (con o sin +51). "
                       "Cada teléfono envía primero a SUS contactos.").pack(side="bottom", fill="x", pady=(2, 0))
        self.wa_list = scrolledtext.ScrolledText(b, height=4)
        self.wa_list.pack(fill="both", expand=True)
        self.wa_list.bind("<KeyRelease>", lambda _e: self._wa_count_update())
        self.btn_send = ttk.Button(row, text="▶ Enviar", command=self.wa_start)
        self.btn_send.pack(side="left")
        ttk.Button(row, text="■ Detener", command=self.wa_stop).pack(side="left", padx=6)
        ttk.Button(row, text="↺ Reanudar pendientes", command=self.wa_resume).pack(side="left")
        self.wa_resume_failed = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="incluir fallidos", variable=self.wa_resume_failed).pack(side="left", padx=(4, 0))
        self.wa_status = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.wa_status, font=("Segoe UI", 9, "bold")).pack(side="right")
        self.wa_split = tk.BooleanVar(value=self.cfg.get("wa_split", True))
        ttk.Checkbutton(row2, text="Usar los teléfonos marcados ☑ (si no: solo el activo)", variable=self.wa_split,
                        command=self._update_marks_label).pack(side="left")
        self.wa_auto_join = tk.BooleanVar(value=self.cfg.get("wa_auto_join", True))
        ttk.Checkbutton(row2, text="Sumar los que se conecten", variable=self.wa_auto_join).pack(side="left", padx=(10, 0))
        self.wa_reassign = tk.BooleanVar(value=self.cfg.get("wa_reassign", False))
        ttk.Checkbutton(row3, text="Reasignar si su teléfono no está (cambia el dueño para siempre)",
                        variable=self.wa_reassign).pack(side="left")
        self.wa_auto_fix = tk.BooleanVar(value=self.cfg.get("wa_auto_reparar", False))
        ttk.Checkbutton(row3, text="Reparar solo al que se caiga", variable=self.wa_auto_fix).pack(side="left", padx=(10, 0))
        return f

    def _tab_control(self, parent):
        f = ttk.Frame(parent, padding=10)
        k = ttk.Labelframe(f, text="Teclas", padding=8)
        k.pack(fill="x")
        for i, (name, code) in enumerate(KEYS.items()):
            ttk.Button(k, text=name, width=11, command=lambda c=code: self.bg(self.key, c)).grid(row=i // 5, column=i % 5, padx=3, pady=3)

        t = ttk.Labelframe(f, text="Tocar y deslizar", padding=8)
        t.pack(fill="x", pady=(10, 0))
        self.tap_x, self.tap_y = tk.StringVar(value="540"), tk.StringVar(value="1200")
        ttk.Label(t, text="Tocar X:").grid(row=0, column=0, sticky="e")
        ttk.Entry(t, textvariable=self.tap_x, width=7).grid(row=0, column=1)
        ttk.Label(t, text="Y:").grid(row=0, column=2, sticky="e")
        ttk.Entry(t, textvariable=self.tap_y, width=7).grid(row=0, column=3)
        ttk.Button(t, text="Tocar", command=lambda: self.bg(self.tap, self.tap_x.get(), self.tap_y.get())).grid(row=0, column=5, padx=6)
        ttk.Button(t, text="Mantener 1 s", command=lambda: self.bg(self.long_press, self.tap_x.get(), self.tap_y.get())).grid(row=0, column=6)
        self.sw = [tk.StringVar(value=v) for v in ("540", "1600", "540", "600")]
        ttk.Label(t, text="Deslizar X1,Y1 → X2,Y2:").grid(row=1, column=0, sticky="e", pady=6)
        for i in range(4):
            ttk.Entry(t, textvariable=self.sw[i], width=7).grid(row=1, column=1 + i, pady=6)
        ttk.Button(t, text="Deslizar", command=lambda: self.bg(self.swipe)).grid(row=1, column=5, padx=6)
        q = ttk.Frame(t)
        q.grid(row=2, column=0, columnspan=7, sticky="w")
        for txt, d in (("↑ Subir", "up"), ("↓ Bajar", "down"), ("← Izquierda", "left"), ("→ Derecha", "right")):
            ttk.Button(q, text=txt, command=lambda d=d: self.bg(self.scroll, d)).pack(side="left", padx=2)

        w = ttk.Labelframe(f, text="Escribir texto (en el campo activo del teléfono)", padding=8)
        w.pack(fill="x", pady=(10, 0))
        self.type_var = tk.StringVar()
        e = ttk.Entry(w, textvariable=self.type_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _e: self.bg(self.type_text))
        ttk.Button(w, text="Escribir", command=lambda: self.bg(self.type_text)).pack(side="left", padx=6)
        ttk.Button(w, text="Escribir + Enter", command=lambda: self.bg(self.type_text, True)).pack(side="left")

        c = ttk.Labelframe(f, text="Comando ADB shell libre", padding=8)
        c.pack(fill="x", pady=(10, 0))
        self.cmd_var = tk.StringVar(value="getprop ro.product.model")
        ce = ttk.Entry(c, textvariable=self.cmd_var)
        ce.pack(side="left", fill="x", expand=True)
        ce.bind("<Return>", lambda _e: self.bg(self.free_cmd))
        ttk.Button(c, text="Ejecutar", command=lambda: self.bg(self.free_cmd)).pack(side="left", padx=6)
        self.cmd_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="En todos", variable=self.cmd_all).pack(side="left")
        return f

    def _tab_apps(self, parent):
        f = ttk.Frame(parent, padding=10)
        u = ttk.Labelframe(f, text="Abrir URL o enlace", padding=8)
        u.pack(fill="x")
        self.url_var = tk.StringVar(value="https://www.google.com")
        ttk.Entry(u, textvariable=self.url_var).pack(side="left", fill="x", expand=True)
        ttk.Button(u, text="Abrir", command=lambda: self.bg(self.open_url, self.url_var.get())).pack(side="left", padx=6)

        a = ttk.Labelframe(f, text="Apps", padding=8)
        a.pack(fill="both", expand=True, pady=(10, 0))
        row = ttk.Frame(a)
        row.pack(fill="x")
        ttk.Label(row, text="Paquete:").pack(side="left")
        self.pkg_var = tk.StringVar(value="com.whatsapp.w4b")
        ttk.Entry(row, textvariable=self.pkg_var, width=36).pack(side="left", padx=6)
        ttk.Button(row, text="Abrir app", command=lambda: self.bg(self.open_app)).pack(side="left")
        ttk.Button(row, text="Cerrar app", command=lambda: self.bg(self.close_app)).pack(side="left", padx=4)
        ttk.Button(row, text="Listar apps instaladas", command=lambda: self.bg(self.list_apps)).pack(side="left")
        self.apps_box = tk.Listbox(a, height=12)
        self.apps_box.pack(fill="both", expand=True, pady=6)
        self.apps_box.bind("<<ListboxSelect>>", self.on_app_select)

        m = ttk.Labelframe(f, text="Otros", padding=8)
        m.pack(fill="x", pady=(10, 0))
        ttk.Button(m, text="Llamar", command=lambda: self.bg(self.call)).pack(side="left")
        self.call_var = tk.StringVar()
        ttk.Entry(m, textvariable=self.call_var, width=18).pack(side="left", padx=4)
        ttk.Button(m, text="Colgar", command=lambda: self.bg(self.key, "KEYCODE_ENDCALL")).pack(side="left", padx=4)
        ttk.Button(m, text="Info del teléfono", command=lambda: self.bg(self.phone_info)).pack(side="left", padx=10)
        ttk.Button(m, text="Batería", command=lambda: self.bg(self.battery)).pack(side="left")
        return f

    # ---------------------------------------------------------- herramientas
    def scrcpy_cmd(self, serial, title, geom=None):
        args = [self.scrcpy, "-s", serial, "--window-title", title, "--no-audio",
                "--max-size", "1024", "--max-fps", "30"]
        if geom:
            x, y, w, h = geom
            args += ["--window-x", str(x), "--window-y", str(y), "--window-width", str(w), "--window-height", str(h)]
        return args

    def launch_scrcpy(self):
        if not self.scrcpy:
            messagebox.showinfo("scrcpy", "scrcpy no está instalado.\n\nEn PowerShell:\nwinget install Genymobile.scrcpy")
            return
        if not self.serial:
            self.log("No hay teléfono activo.")
            return
        subprocess.Popen(self.scrcpy_cmd(self.serial, self.active_name()), creationflags=NO_WINDOW)
        self.log(f"scrcpy abierto para {self.active_name()}.")

    def launch_scrcpy_all(self):
        """Abre una ventana scrcpy por teléfono conectado, en mosaico sobre la pantalla de la PC."""
        if not self.scrcpy:
            messagebox.showinfo("scrcpy", "scrcpy no está instalado.\n\nEn PowerShell:\nwinget install Genymobile.scrcpy")
            return
        targets = self.connected_serials()
        if not targets:
            self.log("No hay teléfonos conectados.")
            return
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight() - 60
        n = len(targets)
        ratio = self.screen_h / max(self.screen_w, 1)
        best = None
        for cols in range(1, n + 1):
            rows = -(-n // cols)
            w = min(sw // cols, int((sh // rows) / ratio))
            if best is None or w > best[0]:
                best = (w, cols, rows)
        w, cols, rows = best
        h = int(w * ratio)
        for i, (ph, serial) in enumerate(targets):
            x, y = (i % cols) * (sw // cols), (i // cols) * (sh // rows) + 30
            subprocess.Popen(self.scrcpy_cmd(serial, ph["name"] if ph else serial, (x, y, w, h)), creationflags=NO_WINDOW)
        self.log(f"scrcpy abierto para {n} teléfono(s) en mosaico de {cols}x{rows}.")

    def open_monitor(self):
        if getattr(self, "monitor", None) and self.monitor.winfo_exists():
            self.monitor.lift()
            return
        self.monitor = Monitor(self)

    def open_logs(self):
        """Abre la carpeta de registros (control_AAAAMMDD.log y envio_*.log)."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)
        except Exception as e:
            self.log(f"No se pudo abrir la carpeta de logs ({LOG_DIR}): {e}")

    def configure_paths(self):
        p = filedialog.askopenfilename(title="Selecciona adb.exe", filetypes=[("adb", "adb.exe"), ("Todos", "*")])
        if p:
            self.adb = p
            self.cfg["adb"] = p
        p2 = filedialog.askopenfilename(title="Selecciona scrcpy.exe (opcional, cancela si no)",
                                        filetypes=[("scrcpy", "scrcpy.exe"), ("Todos", "*")])
        if p2:
            self.scrcpy = p2
            self.cfg["scrcpy"] = p2
        save_config(self.cfg)
        self.bg(self.refresh_devices)

    # ---------------------------------------------------------- acciones básicas
    def key(self, code):
        self.shell("input", "keyevent", code)
        self.log(f"[{self.active_name()}] tecla {code}")

    def tap(self, x, y):
        self.shell("input", "tap", str(int(float(x))), str(int(float(y))))
        self.log(f"[{self.active_name()}] toque en ({x}, {y})")

    def long_press(self, x, y):
        self.shell("input", "swipe", str(x), str(y), str(x), str(y), "1000")
        self.log(f"[{self.active_name()}] pulsación larga en ({x}, {y})")

    def swipe(self):
        v = [s.get() for s in self.sw]
        self.shell("input", "swipe", *v, "300")
        self.log(f"[{self.active_name()}] deslizar {v[0]},{v[1]} -> {v[2]},{v[3]}")

    def scroll(self, direction):
        cx, cy = self.screen_w // 2, self.screen_h // 2
        d = self.screen_h // 4
        moves = {"up": (cx, cy - d, cx, cy + d), "down": (cx, cy + d, cx, cy - d),
                 "left": (cx + d, cy, cx - d, cy), "right": (cx - d, cy, cx + d, cy)}
        self.shell("input", "swipe", *map(str, moves[direction]), "300")

    def type_text(self, enter=False):
        text = self.type_var.get()
        if not text:
            return
        self.shell("input", "text", escape_for_input(text))
        if enter:
            self.shell("input", "keyevent", "KEYCODE_ENTER")
        self.log(f"[{self.active_name()}] escrito: {text}")

    def free_cmd(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        targets = self.connected_serials() if self.cmd_all.get() else [(self.active_phone(), self.serial)]
        for ph, serial in targets:
            out = self.shell(cmd, serial=serial)
            self.log(f"[{ph['name'] if ph else serial}] $ {cmd}\n{out}")

    def open_url(self, url):
        self.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        self.log(f"[{self.active_name()}] abrir {url}")

    def launch_package(self, pkg, serial=None):
        out = self.shell("cmd", "package", "resolve-activity", "--brief",
                         "-c", "android.intent.category.LAUNCHER", pkg, serial=serial)
        activity = next((l.strip() for l in out.splitlines() if "/" in l), None)
        if activity:
            res = self.shell("am", "start", "-n", activity, serial=serial)
        else:
            res = self.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
        last = res.splitlines()[-1] if res else "sin respuesta"
        self.log(f"[{self.active_name()}] abrir {pkg} -> {last}")
        if "Error" in res or "not found" in res.lower():
            self.log("Respuesta completa: " + res)
        time.sleep(1.5)
        self.take_screenshot()

    def open_app(self):
        self.launch_package(self.pkg_var.get().strip())

    def close_app(self):
        pkg = self.pkg_var.get().strip()
        self.shell("am", "force-stop", pkg)
        self.log(f"[{self.active_name()}] cerrada {pkg}")

    def list_apps(self):
        out = self.shell("pm", "list", "packages", "-3")
        pkgs = sorted(l.replace("package:", "") for l in out.splitlines() if l.startswith("package:"))

        def fill():
            self.apps_box.delete(0, "end")
            for p in pkgs:
                self.apps_box.insert("end", p)
        self.after(0, fill)
        self.log(f"[{self.active_name()}] {len(pkgs)} apps de usuario")

    def on_app_select(self, _e):
        sel = self.apps_box.curselection()
        if sel:
            self.pkg_var.set(self.apps_box.get(sel[0]))

    def call(self):
        num = re.sub(r"\D", "", self.call_var.get())
        if num:
            self.shell("am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{num}")
            self.log(f"[{self.active_name()}] llamando a {num}")

    def phone_info(self):
        props = ["ro.product.manufacturer", "ro.product.model", "ro.build.version.release", "ro.serialno"]
        info = {p: self.shell("getprop", p) for p in props}
        self.log(f"[{self.active_name()}] " + ", ".join(f"{k.split('.')[-1]}={v}" for k, v in info.items())
                 + f", pantalla {self.screen_w}x{self.screen_h}")

    def battery(self):
        out = self.shell("dumpsys", "battery")
        m = re.search(r"level: (\d+)", out)
        self.log(f"[{self.active_name()}] batería: {m.group(1)}%" if m else out)

    # ---------------------------------------------------------- WhatsApp
    def _wa_pkg(self):
        return WA_APPS.get(self.wa_app.get(), "com.whatsapp.w4b")

    def _wa_params(self):
        num = re.sub(r"\D", "", self.wa_num.get())
        msg = self.wa_msg.get("1.0", "end").strip()
        try:
            dmin = max(0.0, float(self.wa_delay.get()))
        except ValueError:
            dmin = 4.0
        try:
            dmax = max(dmin, float(self.wa_delay_max.get()))
        except ValueError:
            dmax = dmin
        self.cfg.update(wa_delay=self.wa_delay.get(), wa_delay_max=self.wa_delay_max.get(),
                        wa_cool=self.wa_cool.get(), wa_cool_max=self.wa_cool_max.get(),
                        wa_app=self.wa_app.get(), wa_split=self.wa_split.get(), wa_vary=self.wa_vary.get(),
                        wa_auto_join=self.wa_auto_join.get(), wa_dump_par=self._dump_par_value(),
                        wa_reassign=self.wa_reassign.get(), wa_auto_reparar=self.wa_auto_fix.get(),
                        wa_saludos=self.wa_saludos.get("1.0", "end").strip(),
                        wa_cierres=self.wa_cierres.get("1.0", "end").strip())
        if hasattr(self, "wa_human"):
            self.cfg.update(wa_human=self.wa_human.get(),
                            wa_type_min=self.wa_type_min.get(),
                            wa_type_max=self.wa_type_max.get())
        save_config(self.cfg)
        return num, msg, (dmin, dmax)

    def _dump_par_value(self):
        try:
            return max(1, min(int(self.wa_dump_par.get()), 64))
        except (ValueError, AttributeError):
            return self._dump_par

    def _wa_lists(self):
        sal = [l.strip() for l in self.wa_saludos.get("1.0", "end").splitlines() if l.strip()]
        cie = [l.strip() for l in self.wa_cierres.get("1.0", "end").splitlines() if l.strip()]
        return sal, cie

    def _wa_compose(self, body, last=None):
        if not self.wa_vary.get():
            return body, last
        sal, cie = self._wa_lists()
        return compose_message(body, sal, cie, last)

    @staticmethod
    def _wa_wait(delay):
        dmin, dmax = delay if isinstance(delay, tuple) else (float(delay), float(delay))
        return random.uniform(dmin, dmax)

    def _wa_cooldown(self):
        try:
            cmin = max(0.0, float(self.wa_cool.get()))
        except ValueError:
            cmin = 40.0
        try:
            cmax = max(cmin, float(self.wa_cool_max.get()))
        except ValueError:
            cmax = cmin
        return random.uniform(cmin, cmax)

    def _sleep_bulk(self, seconds, name=None):
        """Duerme en trozos de 1 s; 'Detener' corta la espera durante un envío masivo."""
        end = time.time() + seconds
        while time.time() < end and self.running_bulk:
            time.sleep(max(0.0, min(1.0, end - time.time())))

    def _wa_cool_range(self):
        try:
            cmin = max(0.0, float(self.wa_cool.get()))
        except ValueError:
            cmin = 40.0
        try:
            cmax = max(cmin, float(self.wa_cool_max.get()))
        except ValueError:
            cmax = cmin
        return (cmin, cmax)

    def wa_preview(self):
        body = self.wa_msg.get("1.0", "end").strip() or "(cuerpo del mensaje)"
        last = None
        samples = []
        for _ in range(3):
            txt, last = self._wa_compose(body, last)
            samples.append(txt)
        messagebox.showinfo("Vista previa (3 ejemplos)", "\n\n— — —\n\n".join(samples), parent=self)

    def wa_restore_lists(self):
        self.wa_saludos.delete("1.0", "end")
        self.wa_saludos.insert("1.0", "\n".join(SALUDOS))
        self.wa_cierres.delete("1.0", "end")
        self.wa_cierres.insert("1.0", "\n".join(CIERRES))

    def wa_launch(self):
        self.cfg["wa_app"] = self.wa_app.get()
        save_config(self.cfg)
        self.launch_package(self._wa_pkg())

    def _wa_open(self, num, msg, serial=None):
        url = f"https://wa.me/{num}?text={quote(msg)}"
        out = self.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", self._wa_pkg(), serial=serial)
        low = out.lower()
        if "error" in low and "starting:" not in low:
            raise RuntimeError("no se pudo abrir WhatsApp: " + (out.splitlines()[-1] if out else "sin respuesta"))

    # --- utilidades de pantalla (uiautomator)
    def ui_dump(self, serial=None, tries=2):
        """Devuelve el XML de la pantalla actual (árbol de vistas) o cadena vacía si no se pudo leer."""
        for i in range(tries):
            try:
                out = self.run_adb("exec-out", "uiautomator", "dump", "/dev/tty", serial=serial, timeout=20, binary=True)
            except Exception:
                out = b""
            txt = out.decode("utf-8", "replace")
            a, b = txt.find("<?xml"), txt.rfind("</hierarchy>")
            if a >= 0 and b >= 0:
                return txt[a:b + 12]
            if i + 1 < tries:
                time.sleep(0.8)
        return ""

    def entry_has_msg(self, xml, msg):
        """True si el cuadro de texto del chat existe y contiene el mensaje (por su clave corta)."""
        entry = self.ui_find(xml, "entry")
        if entry is None:
            return False
        key = msg_key(msg)
        if len(key) < 4:
            return entry["text"].strip() not in ("", "Mensaje", "Message")
        return key in msg_key(entry["text"], 10_000)

    @staticmethod
    def ui_find(xml, res_id):
        """Busca un nodo por resource-id. Devuelve dict(text, cx, cy) o None."""
        m = re.search(r'<node[^>]*resource-id="[^"]*:id/%s"[^>]*>' % re.escape(res_id), xml)
        if not m:
            return None
        node = m.group(0)
        t = re.search(r'text="([^"]*)"', node)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            return None
        x1, y1, x2, y2 = map(int, b.groups())
        return {"text": t.group(1) if t else "", "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2}

    def screen_size(self, serial=None):
        """(ancho, alto) del teléfono indicado, consultado una vez y guardado."""
        key = serial or self.serial
        if key not in self.sizes:
            m = re.search(r"(\d+)x(\d+)", self.shell("wm", "size", serial=serial, timeout=10))
            self.sizes[key] = (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)
        return self.sizes[key]

    def is_online(self, serial=None):
        try:
            return self.run_adb("get-state", serial=serial, timeout=10).strip() == "device"
        except Exception:
            return False

    def wake_unlock(self, serial=None):
        """Enciende la pantalla y quita el bloqueo (sin PIN) si hace falta."""
        self.shell("input", "keyevent", "KEYCODE_WAKEUP", serial=serial)
        self.shell("wm", "dismiss-keyguard", serial=serial)
        time.sleep(0.6)
        win = self.shell("dumpsys", "window", serial=serial, timeout=15)
        m = re.search(r"mCurrentFocus=Window\{[^}]*\s(\S+)\}", win)
        focus = m.group(1) if m else ""
        if "NotificationShade" in focus or "Keyguard" in focus or "mDreamingLockscreen=true" in win:
            w, h = self.screen_size(serial)
            self.shell("input", "swipe", str(w // 2), str(int(h * 0.8)), str(w // 2), str(int(h * 0.3)), "200", serial=serial)
            time.sleep(0.6)

    def _wa_press_send(self, ph, serial=None, xml=None):
        """Toca el botón Enviar: lo localiza en pantalla; si no aparece, usa el punto fijado o Enter."""
        xml = xml if xml is not None else self.ui_dump(serial)
        btn = self.ui_find(xml, "send")
        if btn:
            out = self.shell("input", "tap", str(btn["cx"]), str(btn["cy"]), serial=serial)
            how = "boton"
        elif ph and ph.get("wa_x") and ph.get("wa_y"):
            out = self.shell("input", "tap", str(ph["wa_x"]), str(ph["wa_y"]), serial=serial)
            how = "fijado"
        else:
            out = self.shell("input", "keyevent", "KEYCODE_ENTER", serial=serial)
            how = "enter"
        if out and out.lower().startswith("error"):
            raise RuntimeError("no se pudo tocar Enviar: " + out.splitlines()[0])
        return how

    def _chat_ready(self, serial, num, msg, delay, bulk=False):
        """Abre el chat con el mensaje precargado. Devuelve el XML de pantalla solo si el cuadro de texto
        contiene el mensaje; None si no se pudo comprobar."""
        if bulk and not self.running_bulk:
            raise RuntimeError("detenido")
        self.wake_unlock(serial)
        self._wa_open(num, msg, serial=serial)
        time.sleep(self._wa_wait(delay))
        if bulk and not self.running_bulk:
            raise RuntimeError("detenido")
        xml = self.ui_dump(serial)
        if not xml:
            return None
        info = self.ui_find(xml, "read_only_chat_info")
        banner = (info or {}).get("text", "")
        if re.search(r"restringid|restricted|nuevos chats|new chats", banner, re.I):
            raise AccountRestricted(banner.replace(" Mostrar detalles", "").strip().rstrip("."))
        return xml if self.entry_has_msg(xml, msg) else None

    def _send_via(self, ph, serial, num, msg, delay, cooldown=None, name=None):
        """Abre el chat, espera el enfriamiento con el chat abierto, toca Enviar y comprueba que salió.
        Devuelve 'enviado' o lanza RuntimeError con el motivo. Nunca da por enviado sin evidencia."""
        bulk = cooldown is not None
        name = name or (ph["name"] if ph else serial)
        self._step(name, num, "inicio (modo enlace)")
        xml = self._chat_ready(serial, num, msg, delay, bulk) or self._chat_ready(serial, num, msg, delay, bulk)
        if xml is None:
            raise RuntimeError("no se abrió el chat con el mensaje (¿número sin WhatsApp, aviso en pantalla o bloqueo?)")
        self._step(name, num, "chat abierto")

        if cooldown:
            secs = random.uniform(*cooldown)
            if name:
                self.log(f"[{name}] chat abierto con {num}; esperando {secs:.0f} s antes de enviar")
            self._step(name, num, f"espera de {secs:.0f} s con el chat abierto")
            self._sleep_bulk(secs)
            if not self.running_bulk:
                raise RuntimeError("detenido antes de enviar")
            # Tras la espera la pantalla puede haberse apagado o el chat cerrado: comprobar y reabrir si hace falta
            self.wake_unlock(serial)
            xml = self.ui_dump(serial)
            if not self.entry_has_msg(xml, msg):
                xml = self._chat_ready(serial, num, msg, delay, bulk)
                if xml is None:
                    raise RuntimeError("el chat se cerró durante la espera y no se pudo reabrir")

        tocado = False
        try:
            for _intento in range(2):
                self._step(name, num, f"tocar Enviar (intento {_intento + 1})")
                tocado = True
                self._wa_press_send(ph, serial=serial, xml=xml)
                time.sleep(1.2)
                xml = self.ui_dump(serial)
                if not xml:
                    raise SendUnconfirmed("no se pudo leer la pantalla tras tocar Enviar (¿conexión perdida?)")
                entry = self.ui_find(xml, "entry")
                if entry is None:
                    raise SendUnconfirmed("el chat desapareció al tocar Enviar; envío no confirmado")
                if not self.entry_has_msg(xml, msg):
                    self._step(name, num, "enviado confirmado")
                    return "enviado"
                self.wake_unlock(serial)
        except (SendUnconfirmed, AccountRestricted):
            raise
        except Exception as e:
            if tocado:                         # falló DESPUÉS de tocar Enviar: el mensaje pudo salir
                raise SendUnconfirmed(f"fallo tras tocar Enviar ({str(e)[:80]})") from e
            raise
        raise RuntimeError("el texto sigue en el cuadro, no se envió")

    # ============================================================ MODO HUMANO
    # Abre el chat buscando el número dentro de WhatsApp y teclea el mensaje
    # letra por letra con el teclado ADBKeyboard (soporta tildes y emojis).
    # Los enlaces se insertan de golpe.

    def _step(self, name, num, text):
        """Registra un paso del envío en el archivo del envío y recuerda la última actividad del teléfono."""
        self._last_step[name] = (time.time(), num, text)
        FILELOG.step(name, num, text)

    def _kb_active(self, serial):
        return self.shell("settings", "get", "secure", "default_input_method", serial=serial).strip().startswith(ADBKB_PKG)

    def _kb_ensure(self, serial):
        """Instala (si falta) y activa ADBKeyboard en el teléfono, guardando su teclado original.
        Verifica que quedó activo (tras instalarlo recién, el primer intento suele fallar)."""
        cur = self.shell("settings", "get", "secure", "default_input_method", serial=serial).strip()
        if cur.startswith(ADBKB_PKG):
            return
        pkgs = self.shell("pm", "list", "packages", ADBKB_PKG, serial=serial)
        if ADBKB_PKG not in pkgs:
            if not os.path.exists(ADBKB_APK):
                raise RuntimeError("Falta ADBKeyboard.apk en la carpeta del programa (necesario para el modo humano).")
            self.run_adb("install", "-r", ADBKB_APK, serial=serial, timeout=120)
            time.sleep(1.5)  # dar tiempo a que el sistema registre el teclado recién instalado
        self.wa_saved_ime[serial] = cur if ("/" in cur and "rror" not in cur) else GBOARD_IME
        for _ in range(8):
            self.shell("ime", "enable", ADBKB_IME, serial=serial)
            self.shell("ime", "set", ADBKB_IME, serial=serial)
            time.sleep(1.0)
            if self._kb_active(serial):
                return
        raise RuntimeError("no se pudo activar el teclado ADB (necesario para escribir tildes/emojis)")

    def _kb_restore(self, serial):
        """Devuelve el teclado normal del teléfono."""
        orig = self.wa_saved_ime.pop(serial, None) or GBOARD_IME
        try:
            self.shell("ime", "set", orig, serial=serial)
        except Exception:
            pass

    def _type_unicode(self, serial, text):
        """Inserta 'text' (con tildes/emojis) de una vez usando ADBKeyboard (base64 UTF-8)."""
        if not text:
            return
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        out = self.shell("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64, serial=serial) or ""
        if out.startswith("error:") or "device offline" in out or "not found" in out[:60]:
            raise RuntimeError(f"se perdió la conexión ADB mientras se tecleaba ({out[:60]})")

    def _type_human(self, serial, text, rng):
        """Teclea 'text' letra por letra (pausa aleatoria del rango rng), pero los enlaces (http...) de golpe."""
        for part in URL_RE.split(text):
            if not part:
                continue
            if URL_RE.fullmatch(part):
                self._type_unicode(serial, part)      # enlace: de golpe (como pegar)
                time.sleep(random.uniform(*rng))
            else:
                for ch in part:
                    self._type_unicode(serial, ch)    # letra/emoji: uno a uno
                    time.sleep(random.uniform(*rng))

    def _wa_list_ready(self, serial, tries=4, pkg=None):
        """Deja WhatsApp en el listado de chats (botón 'Nuevo chat' visible). Devuelve el XML de pantalla."""
        for _ in range(tries):
            xml = self._hdump(serial)
            if xml and self.ui_find(xml, "fab"):
                return xml
            self.shell("input", "keyevent", "KEYCODE_BACK", serial=serial)
            time.sleep(1.4)
        self.shell("monkey", "-p", pkg or self._wa_pkg(), "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
        time.sleep(3.5)
        xml = self._hdump(serial)
        if xml and not self.ui_find(xml, "fab"):
            self.shell("input", "keyevent", "KEYCODE_BACK", serial=serial)
            time.sleep(1.4)
            xml = self._hdump(serial)
        return xml

    # ============================================================ ⚡ ACTIVAR TODOS
    def _focus_state(self, serial):
        """(ventana en primer plano, ¿bloqueada?) con la MISMA lectura que usa wake_unlock."""
        win = self.shell("dumpsys", "window", serial=serial, timeout=15)
        m = re.search(r"mCurrentFocus=Window\{[^}]*\s(\S+)\}", win)
        focus = m.group(1) if m else ""
        return focus, ("NotificationShade" in focus or "Keyguard" in focus or "mDreamingLockscreen=true" in win)

    def _wa_launch_pkg(self, serial, pkg, force=False):
        if force:
            self.shell("am", "force-stop", pkg, serial=serial)
            time.sleep(1.0)
        self.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
        time.sleep(4.0 if force else 3.0)

    @staticmethod
    def _screen_texts(xml, n=8):
        """Textos visibles de una lectura de pantalla (para decirle al operador QUÉ tapa WhatsApp)."""
        out = []
        for t in re.findall(r'text="([^"]{2,60})"', xml or ""):
            t = html.unescape(t).strip()
            if t and t not in out:
                out.append(t)
            if len(out) >= n:
                break
        return out

    def _wa_repair(self, serial, pkg, check_search=True, name=None):
        """Pantalla encendida y desbloqueada, WhatsApp en el listado de chats y (opcional) el buscador abre.
        Devuelve (ok, texto). NUNCA llamar con un trabajador vivo en ese teléfono (usar _act_claim antes)."""
        self.wake_unlock(serial)
        focus, locked = self._focus_state(serial)
        if locked:
            self.wake_unlock(serial)
            focus, locked = self._focus_state(serial)
            if locked:
                return False, "pantalla BLOQUEADA (¿PIN o patrón?): desbloquéalo a mano"
        hecho = []
        if focus.split("/")[0] != pkg:         # abrir WhatsApp ANTES de pulsar ATRÁS (evita 'atrás' en otra app)
            self._wa_launch_pkg(serial, pkg)
            hecho.append("se abrió WhatsApp")
        forced = False
        xml = ""
        while True:
            xml = self._wa_list_ready(serial, tries=3, pkg=pkg)
            fab = self.ui_find(xml, "fab") if xml else None
            if fab and check_search:           # el fallo repetido del envío: 'no se abrió el buscador de contactos'
                found = self._open_picker_search(serial, fab, tries=2)
                if not found:
                    xml = self._hdump(serial) or xml          # qué hay en pantalla en vez del buscador
                self.shell("input", "keyevent", "KEYCODE_BACK", serial=serial)   # cerrar el selector de contactos
                time.sleep(1.0)
                if found:
                    break
                problema = "WhatsApp NO abre el buscador de contactos (¿aviso o permiso en pantalla?)"
            elif fab:
                break
            else:
                problema = "WhatsApp NO llega al listado de chats (¿aviso en pantalla?)"
            if forced:
                vistos = self._screen_texts(xml)
                if vistos and name:
                    self.log(f"[{name}] En su pantalla se lee: " + " | ".join(vistos))
                return False, problema + ": mira su pantalla (Monitor / scrcpy)"
            self._wa_launch_pkg(serial, pkg, force=True)     # último recurso, una sola vez
            forced = True
            hecho.append("se reinició WhatsApp")
        return True, "listo" + (f" ({', '.join(hecho)})" if hecho else "")

    def _reconnect_one(self, ph):
        """adb connect por WiFi. Jamás toca la conexión de un teléfono ocupado. Verifica que sea ESE aparato."""
        s = self.wifi_serial(ph)
        with self._bulk_lock:
            b = self._bulk
            en_uso = {ss for tt, ss in b["active"].values() if tt.is_alive()} if b else set()
        if not s or s in en_uso or self.phone_key_of(ph) in self._busy_keys():
            return                             # jamás tocar la conexión de un aparato que está escribiendo
        try:
            if self.connected.get(s) in ("offline", "unauthorized"):
                self.run_adb("disconnect", s, timeout=8)     # si no, adb responde 'already connected' para siempre
            for _ in range(2):
                out = self.run_adb("connect", s, timeout=8)
                if "connected" in out and "unable" not in out:
                    real = (self.shell("getprop", "ro.serialno", serial=s, timeout=8) or "").strip()
                    if ph.get("hw") and re.fullmatch(r"[A-Za-z0-9]{6,}", real) and real != ph["hw"]:
                        self.run_adb("disconnect", s, timeout=8)
                        self.log(f"⚠ En {ph['ip']} ya no está {ph['name']} (ahora es otro aparato). "
                                 "Pulsa '🔍 Buscar en la red WiFi' para actualizar las IP.")
                    return
                time.sleep(1.5)
        except Exception:
            pass                               # _why_offline dirá por qué

    def _usb_serial_of(self, ph):
        for s, st in list(self.connected.items()):
            if st == "device" and ":" not in s and s in (ph.get("usb"), ph.get("hw")):
                return s
        return None

    def _is_other_device(self, ph, serial):
        """True SOLO si en ese ip:5555 contesta un nº de serie válido que NO es el de ph (otro aparato tomó su IP)."""
        if not ph or not ph.get("hw") or ":" not in (serial or ""):
            return False
        try:
            real = (self.shell("getprop", "ro.serialno", serial=serial, timeout=8) or "").strip()
        except Exception:
            return False                       # no contestó: is_online / el trabajador lo tratan como caída
        return bool(re.fullmatch(r"[A-Za-z0-9]{6,}", real)) and real != ph["hw"]

    def _verify_identity(self, busy):
        """Comprueba por ro.serialno que cada ip:5555 conectada es el teléfono que dice la lista; corrige IP cruzadas."""
        wifi = [s for s, st in list(self.connected.items()) if st == "device" and ":" in s]
        if not wifi:
            return

        def hw_of(s):
            try:
                return s, (self.shell("getprop", "ro.serialno", serial=s, timeout=8) or "").strip()
            except Exception:
                return s, ""
        with ThreadPoolExecutor(max_workers=16) as ex:
            real = dict(ex.map(hw_of, wifi))
        by_hw = {p["hw"]: p for p in self.cfg["phones"] if p.get("hw")}
        new_ip = {hw: s.split(":")[0] for s, hw in real.items() if hw in by_hw}   # un texto de error no coincide con ningún hw
        changed = False
        for s_, hw_ in real.items():           # aparato que NO está en la lista contestando en la IP de uno registrado
            ph_ = next((p for p in self.cfg["phones"] if self.wifi_serial(p) == s_), None)
            if (ph_ and ph_.get("hw") and hw_ not in by_hw and re.fullmatch(r"[A-Za-z0-9]{6,}", hw_ or "")
                    and ph_["hw"] not in busy):
                try:
                    self.run_adb("disconnect", s_, timeout=8)
                except Exception:
                    pass
                self.connected.pop(s_, None)
                self.log(f"⚠ En {ph_['ip']} contesta OTRO aparato ({hw_}), no {ph_['name']}: desconectado. "
                         "Pulsa '🔍 Buscar en la red WiFi' o pásale el CABLE USB.")
        for hw, ip in new_ip.items():
            ph = by_hw[hw]
            if ph.get("ip") == ip:
                continue
            if hw in busy:
                self.log(f"⚠ {ph['name']}: su IP guardada no coincide con el aparato ({ip}) y está enviando. "
                         "Al terminar, pulsa ⚡ ACTIVAR TODOS.")
                continue
            self.log(f"{ph['name']}: cambió de IP {ph.get('ip') or '-'} → {ip}. Corregido.")
            ph["ip"], changed = ip, True
        for ph in self.cfg["phones"]:          # su IP guardada es ahora de OTRO teléfono
            if ph.get("hw") and ph["hw"] not in new_ip and ph.get("ip") in new_ip.values() and ph["hw"] not in busy:
                ph["ip"], changed = None, True
        if changed:
            save_config(self.cfg)

    def activate_all_clicked(self):
        """HILO PRINCIPAL: lee las variables Tk y lanza la activación en segundo plano."""
        if self._activating.locked():
            self.log("⚡ Ya se está activando; espera a que termine.")
            return
        self.btn_activar.configure(state="disabled", text="⏳ Activando…")
        self.bg(self._activate_all, self._wa_pkg(), bool(self.wa_human.get()))

    def _act_claim(self, key, serial=None):
        """None si el teléfono queda reservado; si no, (ok, motivo). Atómico frente a spawn() (mismo candado)."""
        with self._bulk_lock:
            if key in self._act_busy:
                return True, "está ocupado (activándose o con un envío individual)"
            bulk = self._bulk
            if bulk:
                t = bulk["active"].get(key)
                if t and t[0].is_alive():
                    return True, "está enviando: no se toca"     # jamás ATRÁS / abrir app sobre un teléfono que teclea
                if serial and any(tt.is_alive() and ss == serial for tt, ss in bulk["active"].values()):
                    return False, "en su IP está enviando OTRO teléfono (IP duplicada): no se toca"
            elif self.bulk_active:
                return False, "el envío está arrancando o cerrando; vuelve a pulsar en unos segundos"
            self._act_busy.add(key)
            return None

    def _activate_phone(self, ph, pkg, human):
        """Un teléfono: comprobar, reparar y (si hay envío y está marcado ☑) sumarlo. Devuelve (ok, texto)."""
        key = self.phone_key_of(ph)
        serial = self.phone_state(ph)[0]
        if not serial and ph.get("ip") and not self._activating.locked():
            # Caminos automáticos (reparación, última oportunidad, ☑, cable): nadie ha probado 'adb connect' todavía.
            # Durante ⚡ ACTIVAR TODOS la fase 1 ya lo hizo. _reconnect_one respeta a los ocupados y verifica el nº de serie.
            self._reconnect_one(ph)
            try:
                self._adb_devices()
            except Exception:
                pass
            serial = self.phone_state(ph)[0]
        if not serial:
            return False, self._why_offline(ph)
        if not self.phone_enabled(ph):
            return True, "conectado (no marcado ☐: no se prepara)"
        if self.is_restricted(ph):
            return False, "RESTRINGIDO por WhatsApp: no se usa (desmárcalo ☐)"
        claim = self._act_claim(key, serial)
        if claim:
            return claim
        ok, txt = False, "sin terminar"
        try:
            if not self.is_online(serial):     # 'device' en adb pero el WiFi murió: reconectar una vez
                if ":" in serial:
                    try:
                        self.run_adb("disconnect", serial, timeout=8)
                        self.run_adb("connect", serial, timeout=10)
                    except Exception:
                        pass
                if not self.is_online(serial):
                    ok, txt = False, "aparece conectado pero no responde (¿se apagó o perdió el WiFi?)"
                    return ok, txt
            if self._is_other_device(ph, serial):          # nunca despertar/abrir WhatsApp ni sumar OTRO aparato
                try:
                    self.run_adb("disconnect", serial, timeout=8)
                except Exception:
                    pass
                self.connected.pop(serial, None)
                ok, txt = False, (f"en {ph.get('ip')} contesta OTRO aparato (la IP cambió de dueño): pulsa "
                                  "'🔍 Buscar en la red WiFi' o pásale el CABLE USB")
                return ok, txt
            ok, txt = self._wa_repair(serial, pkg, check_search=human, name=ph["name"])
            if not ok:
                return ok, txt                 # NO se suma un teléfono roto: quemaría números de clientes
            bulk = self._bulk
            if bulk and self.running_bulk:     # el teclado ADB lo pone el propio trabajador, nunca en reposo
                why = bulk["spawn"](ph, serial, manual=True, from_activation=True)   # manual=True salta los 120 s
                if why is None:
                    txt += " · SE SUMÓ AL ENVÍO"
                else:
                    bulk["mark_ok"](key)       # reparado: deja de figurar como FUERA aunque ya no le queden números
                    if ("no le quedan" not in why and "ya está" not in why and "ya terminó" not in why
                            and "solo el activo" not in why):
                        txt += f" · no se sumó: {why}"
            return ok, txt
        except subprocess.TimeoutExpired:
            ok, txt = False, "no responde (tiempo agotado): ¿apagado o sin WiFi?"
            return ok, txt
        finally:
            kv = {"act": (ok, txt, time.time())}   # lo escribe él mismo: un hilo tardío corrige su fila al terminar
            if ok:
                kv["conn"] = "ok"
            self._health_set(key, **kv)
            with self._bulk_lock:
                self._act_busy.discard(key)

    def _health_set(self, key, **kv):
        """Fusiona campos en health[key]; nunca reescribe el dict entero desde una copia vieja."""
        with self._health_mut:
            h = dict(self.health.get(key) or {})
            h.update(kv)
            self.health[key] = h

    def _why_offline(self, ph):
        key = self.phone_key_of(ph)
        h = {}
        st = self.connected.get(self.wifi_serial(ph) or "") or self.connected.get(ph.get("usb") or "")
        if st == "unauthorized":
            h["conn"], txt = "unauth", "acepta 'Permitir depuración' en la pantalla del teléfono"
        elif not ph.get("ip"):
            h["conn"], txt = "rechaza", "nunca se registró por WiFi: conéctalo con el CABLE USB (se arregla solo)"
        else:
            p = probe_port(ph["ip"])
            if p == "mudo":
                p = probe_port(ph["ip"], timeout=4.0)        # los teléfonos dormidos tardan en contestar
            if p == "rechaza":
                h["conn"] = "rechaza"
                txt = (f"necesita CABLE USB: está encendido y en la red ({ph['ip']}) pero se reinició y perdió ADB WiFi. "
                       "Enchufa el cable: se arregla solo")
            elif p == "mudo":
                h["conn"] = "mudo"
                txt = (f"no responde en {ph['ip']}: enciéndelo y revisa su WiFi; si sigue igual, pásale el CABLE USB "
                       "(se arregla solo)")
            else:
                txt = "responde pero ADB no conectó: vuelve a pulsar ⚡ ACTIVAR TODOS en 10 s"
        if self.phone_enabled(ph):
            self._need_usb.add(key)            # cualquier marcado sin conexión: el cable lo arregla sin pulsar nada
        if h:
            self._health_set(key, **h)
        return txt

    def _activate_all(self, pkg, human):
        """HILO bg. Reconecta, repara y (si hay envío) vuelve a sumar. Nunca toca a un teléfono que está enviando."""
        if not self._activating.acquire(blocking=False):
            return
        t0 = time.time()
        completed = False                      # ⚡ llegó al informe (no abortó por ADB caído ni por excepción)
        self._act_report = None                # (listos, marcados, [ayuda]) solo si ⚡ llegó al informe
        try:
            phones = list(self.cfg["phones"])
            self.log(f"⚡ ACTIVAR TODOS: revisando {len(phones)} teléfono(s)…")
            try:
                self._adb_devices()
            except Exception as e:
                self.log(f"⚡ ADB no responde en la PC ({str(e)[:80]}). No se hace nada.")
                return
            # 1) CONEXIÓN (todos los registrados; no intrusivo)
            miss = [p for p in phones if not self.phone_state(p)[0] and p.get("ip")]
            if miss:
                with ThreadPoolExecutor(max_workers=16) as ex:
                    list(ex.map(self._reconnect_one, miss))
                time.sleep(1.0)
                self._adb_devices()
            for p in phones:                   # con cable puesto: reactivar WiFi sin pulsar 'Registrar por USB'. SECUENCIAL
                if self.connected.get(self.wifi_serial(p) or "") == "device":
                    continue
                usb = self._usb_serial_of(p)
                if not usb or self._act_claim(self.phone_key_of(p), usb) is not None:
                    continue                   # tcpip reinicia adbd: jamás sobre un teléfono que está enviando (reserva atómica)
                try:
                    self._register_serial(usb, make_active=False)
                except Exception as e:
                    self.log(f"[{p['name']}] cable puesto, pero no se pudo reactivar ADB WiFi ({str(e)[:80]}).")
                finally:
                    with self._bulk_lock:
                        self._act_busy.discard(self.phone_key_of(p))
            self._adb_devices()
            if any(not self.phone_state(p)[0] and p.get("ip") for p in phones):
                try:
                    self.scan_network(auto=True)   # IP cambiada: los encuentra por nº de serie; tiene su propio candado
                except Exception as e:
                    FILELOG.error(f"activar: búsqueda en red: {e}", e)
                self._adb_devices()
            self._verify_identity(self._busy_keys())
            # 2) PANTALLA + WHATSAPP + BUSCADOR + (si hay envío) SUMAR — un hilo por teléfono, plazo global
            res = {}

            def one(p):
                try:
                    res[self.phone_key_of(p)] = self._activate_phone(p, pkg, human)
                except subprocess.TimeoutExpired:
                    res[self.phone_key_of(p)] = (False, "no responde (tiempo agotado): ¿apagado o sin WiFi?")
                except Exception as e:
                    FILELOG.error(f"activar {p['name']}: {e}", e)
                    res[self.phone_key_of(p)] = (False, str(e)[:80])
            ths = [threading.Thread(target=one, args=(p,), daemon=True) for p in phones]
            for t in ths:
                t.start()
            limite = time.time() + 300
            for t in ths:
                t.join(max(0.1, limite - time.time()))
            # 3) INFORME
            ok_n, ayuda, marcados, no_marc = 0, [], 0, []
            for p in sorted(phones, key=lambda p: (len(p["name"]), p["name"])):
                key = self.phone_key_of(p)
                ok, txt = res.get(key, (False, "sigue trabajando; el resultado aparecerá en su fila"))
                self.log(f"[{p['name']}] {'✔' if ok else '✖'} {txt}")
                if self.phone_enabled(p):
                    marcados += 1
                    ok_n += bool(ok)
                    if not ok:
                        ayuda.append(f"• {p['name']}: {txt}")
                elif ok:
                    no_marc.append(p["name"])
            self.log(f"⚡ RESULTADO: {ok_n} de {marcados} marcados ☑ listos"
                     + (f" · {len(ayuda)} necesitan tu ayuda" if ayuda else "")
                     + (f" · conectados pero NO marcados ☐: {', '.join(no_marc)}" if no_marc else "")
                     + f". Tardó {time.time() - t0:.0f} s.")
            self._usb_watch_start(pkg, human)
            callback = getattr(self, "_after_activation", None)
            self._act_report = (ok_n, marcados, list(ayuda))   # lo lee el '▶ Enviar' que está esperando
            if ayuda and not callback:
                self.after(0, lambda: messagebox.showwarning(
                    "Teléfonos que necesitan tu ayuda", f"{ok_n} de {marcados} teléfonos marcados ☑ están listos.\n\n"
                    + "\n".join(ayuda) + "\n\nLos que piden CABLE USB se arreglan solos al enchufarlo "
                    "(durante 10 min, sin pulsar nada).", parent=self))
            mon = getattr(self, "monitor", None)
            if mon is not None:
                self.after(0, lambda: mon.winfo_exists() and mon.rebuild())
            completed = True
        finally:
            self._activating.release()
            self.after(0, lambda: (self.btn_activar.configure(state="normal", text="⚡ ACTIVAR TODOS"), self.fill_tree()))
            self.after(200, lambda: self._run_after_activation(completed))   # se decide en el HILO PRINCIPAL (como Detener)

    def _run_after_activation(self, completed):
        """HILO PRINCIPAL. '▶ Enviar' pidió comprobar antes: sigue con el envío, salvo 'Detener' o ⚡ abortado."""
        callback, self._after_activation = getattr(self, "_after_activation", None), None
        if not callback:
            return
        if not completed:
            self.log("⚡ no pudo terminar la comprobación: el envío NO arranca. Revisa el aviso de arriba y pulsa '▶ Enviar' otra vez.")
            return
        callback()

    def _usb_watch_start(self, pkg, human):
        with self._locks_guard:
            if not self._need_usb or self._usb_watching:
                return
            self._usb_watching = True
        self.bg(self._usb_watch, pkg, human)

    def _usb_watch(self, pkg, human):
        """HILO bg: durante 10 min, al enchufar el cable a un teléfono que lo necesita se re-registra y prepara solo."""
        try:
            end, avisado, reintento = time.time() + 600, set(), {}
            while self._need_usb and time.time() < end:
                time.sleep(3)
                try:
                    self._adb_devices()        # consulta al servidor adb local; no molesta a los que envían
                except Exception:
                    continue
                for s_, st in list(self.connected.items()):
                    if st == "unauthorized" and ":" not in s_ and s_ not in avisado:
                        avisado.add(s_)
                        self.log("Cable detectado: acepta 'Permitir depuración USB' en la pantalla de ese teléfono.")
                for key in list(self._need_usb):
                    ph = self.phone_by_key(key)
                    cur = self.phone_state(ph)[0] if ph else None
                    if ph is None or (cur and ":" in cur):
                        self._need_usb.discard(key)          # quitado de la lista o ya volvió por WiFi
                        continue
                    usb = self._usb_serial_of(ph)
                    if not usb or self._activating.locked() or time.time() < reintento.get(key, 0):
                        continue
                    if self._act_claim(key, usb) is not None:
                        continue               # está enviando o activándose: tcpip reiniciaría su adbd
                    reintento[key] = time.time() + 20
                    try:
                        try:
                            reg = self._register_serial(usb, make_active=False)
                        finally:
                            with self._bulk_lock:
                                self._act_busy.discard(key)
                        if reg:
                            self._need_usb.discard(key)
                            self._adb_devices()
                            pk = WA_APPS.get(self.cfg.get("wa_app"), pkg)
                            ok, txt = self._activate_phone(ph, pk, bool(self.cfg.get("wa_human", human)))
                            self.log(f"[{ph['name']}] {'✔' if ok else '✖'} conexión recuperada por cable · {txt}. "
                                     "Ya puedes quitar el cable y pasarlo al siguiente.")
                            self.after(0, self.bell)
                    except Exception as e:
                        self.log(f"[{ph['name']}] cable puesto, pero no se pudo reactivar ADB WiFi ({str(e)[:80]}).")
            if self._need_usb:
                self.log("Vigilancia de cable USB terminada (10 min). Pulsa ⚡ ACTIVAR TODOS para reactivarla.")
        finally:
            with self._locks_guard:
                self._usb_watching = False

    # ---------------------------------------------------------- salud de los teléfonos (batería / conexión)
    def _health_kick(self):
        """HILO PRINCIPAL: programa el sondeo periódico (solo lectura)."""
        secs = int(self.cfg.get("health_secs", 60) or 0)
        if secs > 0 and self.adb and not self._activating.locked():
            self.bg(self._health_poll)
        self.after(max(secs, 30) * 1000, self._health_kick)

    def _health_poll(self):
        """HILO bg. A los teléfonos que están enviando no se les manda nada ni se toca su conexión."""
        if not self._health_lock.acquire(blocking=False):
            return
        try:
            self._adb_devices()
            busy = self._busy_keys()

            def one(ph):
                key = self.phone_key_of(ph)
                h = {"ts": time.time()}                    # solo campos NUEVOS: se fusionan al final
                serial = self.phone_state(ph)[0]
                if key in busy:
                    if serial:
                        h["conn"] = "ok"
                elif serial:
                    try:
                        out = self.shell("dumpsys", "battery", serial=serial, timeout=10)
                        m = re.search(r"level: (\d+)", out)
                        h["bat"] = int(m.group(1)) if m else None
                        h["carga"] = "powered: true" in out
                        h["conn"] = "ok" if m else "noresp"
                    except subprocess.TimeoutExpired:
                        h["conn"] = "noresp"               # 'device' en adb pero muerto (WiFi caído / colgado)
                    except Exception:
                        pass
                elif ph.get("ip"):
                    pr = probe_port(ph["ip"])
                    if pr == "abierto":
                        self._reconnect_one(ph)            # volvió el WiFi con ADB vivo: reconectar (verifica identidad)
                    else:
                        h["conn"] = pr
                self._health_set(key, **h)
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(one, list(self.cfg["phones"])))
        except Exception as e:
            FILELOG.error(f"sondeo de salud: {e}", e)
        finally:
            self._health_lock.release()

    def _tap(self, serial, node):
        self.shell("input", "tap", str(node["cx"]), str(node["cy"]), serial=serial)

    def _phone_lock(self, serial):
        with self._locks_guard:
            lk = self._phone_locks.get(serial)
            if lk is None:
                lk = self._phone_locks[serial] = threading.Lock()
            return lk

    def _set_dump_parallel(self, n):
        """Cambia el tope de lecturas de pantalla simultáneas (solo se aplica al iniciar un envío)."""
        n = max(1, min(int(n), 64))
        if n != self._dump_par:
            self._dump_par = n
            self._dump_sem = threading.BoundedSemaphore(n)
        return n

    def _hdump(self, serial):
        """Lee la pantalla (uiautomator). Serializa por teléfono y limita cuántos teléfonos leen a la vez."""
        with self._phone_lock(serial):
            with self._dump_sem:
                return self.ui_dump(serial)

    # Filas del buscador que NO son un contacto/resultado (son opciones de menú)
    _PICKER_MENU = {"", "Nuevo grupo", "Nuevo contacto", "Compartir enlace de invitación",
                    "Ayuda", "Enviar mensaje a este mismo número."}

    def _clear_entry(self, serial):
        """Vacía el cuadro de mensaje (borra borradores que quedaron de intentos anteriores).
        Usa el teclado ADB y, si algo queda, borra con retrocesos."""
        entry = self._find_retry(serial, "entry", tries=3)
        if entry:
            self._tap(serial, entry)
            time.sleep(0.3)
        self.shell("am", "broadcast", "-a", "ADB_CLEAR_TEXT", serial=serial)
        time.sleep(0.4)
        entry = self.ui_find(self._hdump(serial), "entry")
        if entry and entry.get("text", "").strip() not in ("", "Mensaje", "Message"):
            self.shell("input", "keyevent", "123", serial=serial)          # ir al final
            self.shell("input", "keyevent", *(["67"] * 200), serial=serial)  # retrocesos
            time.sleep(0.3)

    @staticmethod
    def _node_center(node):
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            return None
        x1, y1, x2, y2 = map(int, b.groups())
        return {"cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2, "box": (x1, y1, x2, y2)}

    @classmethod
    def ui_find_search_box(cls, xml):
        """Diseño NUEVO de 'Nuevo chat': el selector de contactos trae un cuadro de búsqueda ya visible
        (un EditText sin id dentro de wds_search_bar) en vez del icono de la lupa. Devuelve dict(cx, cy) o None."""
        if not xml or ":id/contact_picker_layout" not in xml or ":id/wds_search_bar" not in xml:
            return None
        if cls._picker_chips(xml):                 # quedó alguien seleccionado de antes: no es un buscador limpio
            return None
        m = re.search(r'<node[^>]*class="android\.widget\.EditText"[^>]*>', xml)
        c = cls._node_center(m.group(0)) if m else None
        return dict(c, text="") if c else None

    def _open_picker_search(self, serial, fab, tries=4):
        """Toca 'Nuevo chat' hasta que aparezca el buscador, en cualquiera de los dos diseños de WhatsApp.
        Devuelve ('lupa', nodo) | ('cuadro', nodo) | None. Quien llama debe tocar el nodo para escribir."""
        node = fab
        for _ in range(tries):
            if node:
                self._tap(serial, node)
                time.sleep(1.6)
            for _i in range(3):
                xml = self._hdump(serial)
                lupa = self.ui_find(xml, "menuitem_search") if xml else None
                if lupa:
                    return ("lupa", lupa)
                box = self.ui_find_search_box(xml)
                if box:
                    return ("cuadro", box)
                time.sleep(0.9)
            node = self._find_retry(serial, "fab", tries=2)     # reubicar el botón por si cambió de sitio
        return None

    @classmethod
    def _row_matching(cls, xml, digits):
        """Diseño NUEVO: el resultado de un número que no es contacto es una FILA cuyo texto es el propio número
        ('+51 901 288 556'). Solo se acepta si coincide con lo buscado: jamás se abre el chat de otro contacto."""
        want = re.sub(r"\D", "", digits or "")[-9:]
        if len(want) < 7:
            return None
        for m in re.finditer(r'<node[^>]*resource-id="[^"]*:id/contactpicker_row_name"[^>]*>', xml or ""):
            node = m.group(0)
            t = re.search(r'text="([^"]*)"', node)
            txt = html.unescape(t.group(1)) if t else ""
            if "(Tú)" in txt or "(You)" in txt:
                continue
            got = re.sub(r"\D", "", txt)
            if got.endswith(want) and len(got) <= len(want) + 3:
                return cls._node_center(node)
        return None

    def _single_contact_row(self, xml):
        """Diseño NUEVO, contacto GUARDADO (la fila muestra su nombre): solo se acepta si tras buscar el número queda
        EXACTAMENTE una fila que no sea una opción de menú."""
        rows = []
        for m in re.finditer(r'<node[^>]*resource-id="[^"]*:id/contactpicker_row_name"[^>]*>', xml or ""):
            t = re.search(r'text="([^"]*)"', m.group(0))
            nm = html.unescape(t.group(1) if t else "").strip()
            if nm and nm not in self._PICKER_MENU and "(Tú)" not in nm and "(You)" not in nm:
                rows.append(m.group(0))
        return self._node_center(rows[0]) if len(rows) == 1 else None

    @classmethod
    def _picker_chips(cls, xml):
        """Etiquetas 'Para:' ya seleccionadas en el selector nuevo (botones dentro de contact_picker_chip_group_layout).
        Devuelve la lista de sus textos/descripciones."""
        g = re.search(r'<node[^>]*resource-id="[^"]*:id/contact_picker_chip_group_layout"[^>]*>', xml or "")
        area = cls._node_center(g.group(0)) if g else None
        if not area:
            return []
        gx1, gy1, gx2, gy2 = area["box"]
        chips = []
        for m in re.finditer(r'<node[^>]*class="android\.widget\.Button"[^>]*>', xml):
            c = cls._node_center(m.group(0))
            if not c:
                continue
            x1, y1, x2, y2 = c["box"]
            if x1 >= gx1 and y1 >= gy1 and x2 <= gx2 and y2 <= gy2:
                d = re.search(r'content-desc="([^"]*)"', m.group(0))
                t = re.search(r'text="([^"]*)"', m.group(0))
                chips.append(html.unescape((d.group(1) if d and d.group(1) else (t.group(1) if t else ""))))
        return chips

    def _open_chat_new_picker(self, serial, row, want):
        """Diseño NUEVO: UN SOLO toque en la fila -> aparece la etiqueta 'Para:' y el botón 'Enviar mensaje' ->
        se toca ese botón UNA vez -> chat. Devuelve (nodo 'entry', xml) o lanza RuntimeError tras salir sin tocar más."""
        def abort(msg):
            self._wa_list_ready(serial)            # ATRÁS hasta el listado: descarta cualquier selección
            raise RuntimeError(msg)
        want9 = re.sub(r"\D", "", want or "")[-9:]
        self._tap(serial, row)                     # <- el único toque en la lista
        time.sleep(1.5)
        btn = None
        leyo = vio = False                     # ¿hubo lecturas válidas? ¿alguna mostró etiqueta 'Para:' o el botón?
        limite = time.time() + 15              # por TIEMPO, no por nº de lecturas: el botón puede tardar en aparecer
        while time.time() < limite:
            xml = self._hdump(serial)
            entry = self.ui_find(xml, "entry") if xml else None
            if entry:                              # alguna variante abre el chat directamente
                return entry, xml
            chips = self._picker_chips(xml)
            leyo = leyo or bool(xml)
            vio = vio or bool(chips) or bool(self.ui_find(xml, "extended_fab") if xml else None)
            if len(chips) > 1:
                abort("selección de contactos inesperada en 'Nuevo chat' (varios contactos marcados); se salió sin enviar")
            cand = self.ui_find(xml, "extended_fab") if xml else None
            if cand and len(chips) == 1:
                rotulo = (cand["text"] or "").lower()
                if not ("mensaje" in rotulo or "message" in rotulo) or "grup" in rotulo or "group" in rotulo:
                    abort(f"selección de contactos inesperada en 'Nuevo chat' (botón '{cand['text'][:20]}'); se salió sin enviar")
                got = re.sub(r"\D", "", chips[0])
                if not (want9 and len(got) >= 9 and got.endswith(want9)):      # la etiqueta 'Para:' debe SER el número buscado
                    abort(f"selección de contactos inesperada en 'Nuevo chat' (se marcó {chips[0][:30]}); se salió sin enviar")
                btn = cand
                break
            time.sleep(1.0)
        if not btn:
            if leyo and not vio:               # la fila existe pero WhatsApp no la deja seleccionar: es cosa del NÚMERO
                abort(f"{want}: la fila del resultado no se deja seleccionar (¿número sin WhatsApp?)")
            abort("no se abrió el chat tras tocar el resultado")
        self._tap(serial, btn)                     # 'Enviar mensaje': una sola vez
        time.sleep(1.5)
        limite = time.time() + 15
        while time.time() < limite:
            xml = self._hdump(serial)
            entry = self.ui_find(xml, "entry") if xml else None
            if entry:
                return entry, xml
            time.sleep(1.0)
        abort("no se abrió el chat tras tocar el resultado")

    @staticmethod
    def _chat_title_problem(xml, want, strict):
        """Texto del problema si el chat abierto NO es el del número buscado; None si está bien.
        strict (diseño nuevo, solo se abren filas-número): el título DEBE ser ese número.
        no strict (diseño antiguo): un contacto guardado muestra su NOMBRE; solo es problema si el título es OTRO teléfono."""
        m = re.search(r'<node[^>]*resource-id="[^"]*:id/conversation_contact_name"[^>]*>', xml or "")
        t = re.search(r'text="([^"]*)"', m.group(0)) if m else None
        titulo = html.unescape(t.group(1)).strip() if t else ""
        got = re.sub(r"\D", "", titulo)
        want9 = re.sub(r"\D", "", want or "")[-9:]
        if len(got) >= 9 and want9 and got.endswith(want9):
            return None
        if strict:
            return f"el título del chat es '{titulo[:30] or '(no leído)'}' y no el número buscado"
        parece_tel = bool(re.fullmatch(r"[+]?[0-9 ().-]{9,}", titulo))
        return f"se abrió el chat de OTRO número ({titulo[:30]})" if parece_tel else None

    @staticmethod
    def _search_box_digits(xml):
        """Cifras escritas en el buscador de 'Nuevo chat' (search_src_text en el diseño antiguo; el EditText sin id en el
        nuevo). None si no hay cuadro de búsqueda en la pantalla leída."""
        m = (re.search(r'<node[^>]*resource-id="[^"]*:id/search_src_text"[^>]*>', xml or "")
             or re.search(r'<node[^>]*class="android\.widget\.EditText"[^>]*>', xml or ""))
        if not m:
            return None
        t = re.search(r'text="([^"]*)"', m.group(0))
        return re.sub(r"\D", "", html.unescape(t.group(1)) if t else "")

    def _find_result_row(self, serial):
        """Busca en el buscador una fila de resultado que sea un contacto/cuenta (no una opción de menú).
        Sirve cuando el número está guardado como contacto y no aparece el botón 'Chatear'."""
        xml = self._hdump(serial)
        if not xml:
            return None
        for m in re.finditer(r'<node[^>]*resource-id="[^"]*:id/contactpicker_row_name"[^>]*>', xml):
            node = m.group(0)
            t = re.search(r'text="([^"]*)"', node)
            name = (t.group(1) if t else "").strip()
            if name and name not in self._PICKER_MENU:
                b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
                if b:
                    x1, y1, x2, y2 = map(int, b.groups())
                    return {"cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2}
        return None

    def _find_retry(self, serial, res_id, tries=5, delay=0.9):
        """Busca un nodo por id volviendo a leer la pantalla varias veces (tolera lecturas fallidas o transiciones)."""
        node = None
        for _ in range(tries):
            node = self.ui_find(self._hdump(serial), res_id)
            if node:
                return node
            time.sleep(delay)
        return node

    def _tap_until(self, serial, tap_node, expect_id, find_id=None, tries=4):
        """Toca tap_node (reubicándolo por find_id en cada reintento si se indica) hasta que aparezca expect_id.
        Devuelve el nodo esperado o None."""
        node = tap_node
        for _ in range(tries):
            if node:
                self._tap(serial, node)
                time.sleep(1.6)
            got = self._find_retry(serial, expect_id, tries=3)
            if got:
                return got
            if find_id:                      # volver a ubicar el objetivo por si cambiaron las coordenadas
                node = self._find_retry(serial, find_id, tries=2)
        return None

    def _human_ranges(self):
        """(rango pausa por carácter). Lee los campos de velocidad de tecleo."""
        try:
            tmin = max(0.0, float(self.wa_type_min.get()))
        except (ValueError, AttributeError):
            tmin = 0.05
        try:
            tmax = max(tmin, float(self.wa_type_max.get()))
        except (ValueError, AttributeError):
            tmax = max(tmin, 0.15)
        return (tmin, tmax)

    def _save_human(self):
        try:
            self.cfg.update(wa_human=self.wa_human.get(),
                            wa_type_min=self.wa_type_min.get(),
                            wa_type_max=self.wa_type_max.get())
            save_config(self.cfg)
        except Exception:
            pass

    def _send_typed(self, ph, serial, num, msg, delay, cooldown=None, name=None):
        """Flujo humano por número: listado -> Nuevo chat -> Buscar -> digitar número ->
        Chatear -> teclear mensaje letra por letra -> Enviar -> atrás. Devuelve 'enviado' o lanza error."""
        name = name or (ph["name"] if ph else serial)
        bulk = cooldown is not None
        # Valores de interfaz leídos una sola vez (no acceder a Tk desde cada hilo)
        rng = getattr(self, "_human_rng", None) or self._human_ranges()
        if bulk and not self.running_bulk:
            raise RuntimeError("detenido")
        self._step(name, num, "inicio (modo humano): despertar pantalla")
        self.wake_unlock(serial)

        # 1) listado de chats
        self._step(name, num, "buscando el listado de chats")
        xml = self._wa_list_ready(serial)
        fab = self.ui_find(xml, "fab") if xml else None
        if not fab:
            raise RuntimeError("no se pudo llegar al listado de chats de WhatsApp")

        # 2) Nuevo chat -> buscador. WhatsApp tiene dos diseños: icono de lupa (antiguo) o cuadro de búsqueda visible (nuevo)
        self._step(name, num, "listado OK; abriendo Nuevo chat y buscador")
        found = self._open_picker_search(serial, fab)
        if not found:
            raise RuntimeError("no se abrió el buscador de contactos")
        nuevo = found[0] == "cuadro"
        self._tap(serial, found[1])            # lupa: abre el campo; cuadro: le da el foco
        time.sleep(1.2)
        self._step(name, num, "buscador abierto; digitando el número" + (" (diseño nuevo de WhatsApp)" if nuevo else ""))

        # 3) digitar el número dígito por dígito. WhatsApp busca por el número LOCAL
        #    (sin código de país; el teléfono ya asume su región), así que se prueba primero
        #    la forma local y, si no aparece 'Chatear', se reintenta con el número completo.
        country = getattr(self, "_human_country", None)
        if country is None:
            try:
                country = re.sub(r"\D", "", self.wa_country.get())
            except AttributeError:
                country = "51"
        local = num[len(country):] if country and num.startswith(country) else num

        def _type_digits(digits):
            for d in digits:
                out = self.shell("input", "text", d, serial=serial) or ""
                if out.startswith("error:") or "device offline" in out or "not found" in out[:60]:
                    raise RuntimeError(f"se perdió la conexión ADB mientras se tecleaba el número ({out[:60]})")
                time.sleep(random.uniform(*rng))

        def _find_target(typed):
            """('chat', nodo) si hay botón Chatear (diseño antiguo); ('row', nodo) si hay una fila cuyo texto es el
            número buscado (diseño nuevo) o, al final, si es un contacto guardado; None si no aparece nada."""
            xml_t = ""
            for _i in range(5):
                xml_t = self._hdump(serial)
                cb = self.ui_find(xml_t, "chat") if xml_t else None
                if cb and not nuevo:
                    return ("chat", cb)
                row = self._row_matching(xml_t, local) if nuevo else None   # el diseño antiguo queda EXACTAMENTE como estaba
                if row:
                    return ("row", row)
                time.sleep(1.0)
            escrito = self._search_box_digits(xml_t)
            if escrito is not None and escrito != re.sub(r"\D", "", typed):
                # teclas o foco perdidos: la lista NO está filtrada por este número -> jamás tocar una fila. Fallo del teléfono.
                raise RuntimeError("no se abrió el buscador de contactos: el número no quedó escrito completo "
                                   f"(se lee '{escrito}'); no se tocó ninguna fila")
            if nuevo:                               # fallo CERRADO: en el diseño nuevo solo vale una fila que MUESTRE el número
                if self._single_contact_row(xml_t):
                    raise RuntimeError(f"{num}: en este teléfono figura como CONTACTO GUARDADO y el diseño nuevo de WhatsApp "
                                       "no deja verificarlo; no se envió (envíalo desde otro teléfono)")
                return None
            row = self._find_result_row(serial)     # diseño antiguo, contacto guardado: su fila muestra el NOMBRE
            if row:
                return ("row", row)
            return None

        _type_digits(local)
        time.sleep(2.0)
        target = _find_target(local)
        if not target and local != num:
            self.shell("am", "broadcast", "-a", "ADB_CLEAR_TEXT", serial=serial)
            time.sleep(0.6)
            _type_digits(num)
            time.sleep(2.0)
            target = _find_target(num)

        # 4) si no hay ni 'Chatear' ni un contacto, el número no tiene WhatsApp
        if not target:
            raise RuntimeError(f"{num}: WhatsApp no muestra 'Chatear' ni un contacto (¿número sin WhatsApp?)")

        # 5) abrir el chat (botón Chatear para no-contactos, o la fila del contacto guardado)
        kind, node = target
        self._step(name, num, f"resultado encontrado ({'botón Chatear' if kind == 'chat' else 'fila del resultado'}); abriendo chat")
        if nuevo:
            entry, xml_chat = self._open_chat_new_picker(serial, node, local)   # un toque + 'Enviar mensaje' (nunca dos toques)
        else:
            entry = self._tap_until(serial, node, "entry", find_id=("chat" if kind == "chat" else None))
            if entry is None:
                if kind == "row":              # lo causa el NÚMERO: no debe expulsar a un teléfono sano
                    raise RuntimeError(f"{num}: la fila encontrada no abrió ningún chat tras 4 toques (¿número sin WhatsApp?)")
                raise RuntimeError("no se abrió el chat tras tocar el resultado")
            xml_chat = self._hdump(serial)
        problema = self._chat_title_problem(xml_chat, local, strict=nuevo)     # jamás escribir en el chat de otra persona
        if problema:
            self._wa_list_ready(serial)
            raise RuntimeError(f"selección de contactos inesperada: {problema}; se salió sin escribir")
        self._step(name, num, "chat abierto")

        # 5b) borrar de inmediato cualquier borrador viejo del chat (para que no aparezca el mensaje anterior)
        if not self._kb_active(serial):
            self._kb_ensure(serial)
        self._clear_entry(serial)

        # 6) espera con el chat abierto (enfriamiento) igual que el modo normal
        if cooldown:
            secs = random.uniform(*cooldown)
            if name:
                self.log(f"[{name}] chat abierto con {num}; esperando {secs:.0f} s antes de escribir")
            self._step(name, num, f"espera de {secs:.0f} s con el chat abierto")
            self._sleep_bulk(secs)
            if not self.running_bulk:
                raise RuntimeError("detenido antes de enviar")
            self.wake_unlock(serial)
            entry = self._find_retry(serial, "entry", tries=3)
            if entry is None:
                raise RuntimeError("el chat se cerró durante la espera")

        # 7) enfocar el cuadro, asegurar que quedó vacío y teclear el mensaje letra por letra
        if not self._kb_active(serial):
            self._kb_ensure(serial)
        self._clear_entry(serial)
        entry = self._find_retry(serial, "entry", tries=2) or entry
        self._tap(serial, entry)
        time.sleep(0.5)
        self._step(name, num, f"tecleando el mensaje ({len(msg)} caracteres)")
        self._type_human(serial, msg, rng)
        time.sleep(0.8)
        self._step(name, num, "mensaje tecleado; tocando Enviar")

        # 8) tocar Enviar y comprobar que salió (no dar por enviado sin evidencia)
        tocado = False
        try:
            for _intento in range(2):
                send = self._find_retry(serial, "send", tries=3)
                tocado = True
                if send:
                    self._tap(serial, send)
                else:
                    self.shell("input", "keyevent", "KEYCODE_ENTER", serial=serial)
                time.sleep(1.3)
                xml = self._hdump(serial)
                entry = self.ui_find(xml, "entry")
                if entry is None:
                    raise SendUnconfirmed("el chat desapareció al enviar; envío no confirmado")
                if not self.entry_has_msg(xml, msg):
                    # 9) enviado: volver al listado (si el ATRÁS falla, el mensaje YA salió: sigue siendo 'enviado')
                    self._step(name, num, "enviado confirmado; volviendo al listado")
                    try:
                        self.shell("input", "keyevent", "KEYCODE_BACK", serial=serial)
                    except Exception as e:
                        FILELOG.error(f"[{name}] ATRÁS tras enviar a {num}: {e}")
                    time.sleep(1.0)
                    return "enviado"
                self.wake_unlock(serial)
        except (SendUnconfirmed, AccountRestricted):
            raise
        except Exception as e:
            if tocado:                         # falló DESPUÉS de tocar Enviar: el mensaje pudo salir
                raise SendUnconfirmed(f"fallo tras tocar Enviar ({str(e)[:80]})") from e
            raise
        raise RuntimeError("el texto sigue en el cuadro, no se envió")

    def wa_prepare_kb(self):
        """Instala y activa ADBKeyboard en los teléfonos conectados y marcados ☑ (modo humano)."""
        targets = self.send_targets()
        if not targets:
            self.log("No hay teléfonos conectados.")
            return
        for ph, serial in targets:
            try:
                self._kb_ensure(serial)
                self.log(f"[{ph['name'] if ph else serial}] teclado ADB listo.")
            except Exception as e:
                self.log(f"[{ph['name'] if ph else serial}] no se pudo preparar el teclado: {e}")

    def wa_restore_kb(self):
        """Devuelve el teclado normal a todos los teléfonos conectados."""
        seriales = {s for _, s in self.connected_serials()} | set(self.wa_saved_ime)
        for serial in seriales:
            self._kb_restore(serial)
        self.log("Teclado normal restaurado en los teléfonos.")

    def wa_open_only(self):
        num, msg, _ = self._wa_params()
        if not num:
            self.log("Falta el número.")
            return
        ph, serial = self.active_phone(), self.serial
        key = self.phone_key_of(ph) or serial
        claim = self._act_claim(key, serial)   # misma reserva que ⚡: nadie más toca este teléfono mientras tanto
        if claim is not None:
            self.log(f"[{self.active_name()}] no se puede usar ahora: {claim[1]}.")
            return
        try:
            self.wake_unlock(serial)
            self._wa_open(num, msg, serial=serial)
        finally:
            with self._bulk_lock:
                self._act_busy.discard(key)
        self.log(f"[{self.active_name()}] chat abierto con {num}. El botón Enviar se localiza solo; "
                 "el clic derecho en la captura solo hace falta como respaldo.")
        time.sleep(2)
        self.take_screenshot()

    def wa_send_one(self):
        ph, serial = self.active_phone(), self.serial      # fijos: el operador puede cambiar de fila mientras teclea
        name = ph["name"] if ph else (serial or "?")
        if not serial:
            self.log("No hay teléfono activo conectado.")
            return
        num, msg, delay = self._wa_params()
        if not num or not msg:
            self.log("Falta número o mensaje.")
            return
        key = self.phone_key_of(ph) or serial
        claim = self._act_claim(key, serial)   # reserva atómica: ni ⚡, ni reparación, ni el envío masivo lo tocan
        if claim is not None:
            self.log(f"[{name}] no se puede enviar ahora: {claim[1]}. Espera a que termine.")
            return
        human = False
        try:
            text, _ = self._wa_compose(msg)
            human = bool(self.wa_human.get())
            if human:
                self._human_rng = self._human_ranges()
                self._human_country = re.sub(r"\D", "", self.wa_country.get())
                self._kb_ensure(serial)
                self._send_typed(ph, serial, num, text, delay)
            else:
                self._send_via(ph, serial, num, text, delay)
            self.log(f"[{name}] mensaje enviado a {num}: {text.replace(chr(10), ' / ')}")
        except AccountRestricted as e:
            self.log(f"[{name}] CUENTA RESTRINGIDA por WhatsApp: {e}")
            self.mark_restricted(ph, str(e))
            self.after(0, lambda m=str(e), n=name: messagebox.showwarning(
                "Cuenta restringida", f"WhatsApp restringió la cuenta de {n}:\n\n{m}", parent=self))
        except SendUnconfirmed as e:
            self.log(f"[{name}] SIN CONFIRMAR a {num}: {e}. El mensaje pudo salir: mira el chat antes de reintentar.")
        except Exception as e:
            self.log(f"[{name}] NO enviado a {num}: {e}")
        finally:
            try:
                if human:
                    self._kb_restore(serial)   # todavía dentro de la reserva: nadie más teclea en este teléfono
            finally:
                with self._bulk_lock:
                    self._act_busy.discard(key)
        time.sleep(1.5)
        self.take_screenshot()

    def _wa_numbers(self):
        country = re.sub(r"\D", "", self.wa_country.get())
        valid, seen = [], set()
        for line in self.wa_list.get("1.0", "end").splitlines():
            n = normalize_number(line, country)
            if n and n not in seen:
                valid.append(n)
                seen.add(n)
        return valid

    def _wa_count_update(self):
        self.wa_count.set(f"{len(self._wa_numbers())} números")

    def wa_clear_list(self):
        self.wa_list.delete("1.0", "end")
        self._wa_count_update()

    def wa_load_file(self):
        p = filedialog.askopenfilename(filetypes=[("Excel / CSV / Texto", "*.xlsx *.xlsm *.csv *.txt"),
                                                  ("Excel", "*.xlsx *.xlsm"), ("Todos", "*")])
        if not p:
            return
        country = re.sub(r"\D", "", self.wa_country.get())
        self.cfg["wa_country"] = country
        save_config(self.cfg)
        try:
            valid, invalid = read_numbers_file(p, country)
        except Exception as e:
            self.log(f"No se pudo leer el archivo: {e}")
            return
        self.wa_list.delete("1.0", "end")
        self.wa_list.insert("1.0", "\n".join(valid))
        self._wa_count_update()
        self.log(f"Archivo cargado: {len(valid)} números válidos" +
                 (f", {len(invalid)} descartados (ej. {', '.join(invalid[:5])})" if invalid else "") +
                 f". Prefijo +{country}.")

    def wa_resume(self):
        """Carga en la lista los números que quedaron sin intentar en el último envío (y los fallidos, si se marca)."""
        if self.bulk_active:
            self.log("Espera a que termine el envío en curso antes de reanudar.")
            return
        pending = self.wa_pending if self.wa_pending is not None else read_numbers(PENDING_FILE)
        failed = self.wa_failed if self.wa_failed is not None else read_numbers(FAILED_FILE)
        nums = list(pending)
        if self.wa_resume_failed.get():
            nums += [n for n in failed if n not in nums]
        if not nums:
            self.log("No hay pendientes del último envío." + (f" Hay {len(failed)} fallidos; marca 'incluir fallidos' para cargarlos." if failed else ""))
            return
        self.wa_list.delete("1.0", "end")
        self.wa_list.insert("1.0", "\n".join(nums))
        self._wa_count_update()
        extra = f" + {len(nums) - len(pending)} fallidos" if len(nums) > len(pending) else ""
        self.log(f"Cargados {len(pending)} pendientes{extra} del último envío.")
        self.wa_start(resume=True)

    def wa_start(self, resume=False, skip_preflight=False):
        """HILO PRINCIPAL. Única puerta de entrada del envío masivo (▶ Enviar y ↺ Reanudar)."""
        if self.bulk_active:
            self.log("Ya hay un envío en curso.")
            return
        if self._activating.locked():
            self.log("Espera a que termine ⚡ ACTIVAR TODOS antes de iniciar el envío.")
            return
        if not self.wa_msg.get("1.0", "end").strip():
            messagebox.showwarning("Falta el mensaje", "Escribe el mensaje en 'Mensaje (cuerpo)' antes de enviar.", parent=self)
            return
        split = bool(self.wa_split.get())
        if not split:
            act = self.active_phone()
            if act is not None and not self.phone_enabled(act):
                messagebox.showwarning("Teléfono desmarcado", f"{act['name']} es el teléfono activo pero está desmarcado ☐.\n\n"
                                       "Márcalo ☑, o activa 'Usar los teléfonos marcados ☑'.", parent=self)
                return
        # 1) Comprobación previa: ¿hay marcados sin conexión o sin revisar hace poco?
        if not skip_preflight and self.cfg.get("wa_preflight", True):
            base = ([p for p in self.cfg["phones"] if self.phone_enabled(p) and not self.is_restricted(p)] if split
                    else [p for p in [self.active_phone()] if p])
            now = time.time()

            def revisado(p):
                a = (self.health.get(self.phone_key_of(p)) or {}).get("act")
                return bool(self.phone_state(p)[0]) and bool(a) and a[0] and now - a[2] < 900
            sin = [p for p in base if not revisado(p)]
            if sin:
                lista = ", ".join(p["name"] for p in sorted(sin, key=lambda p: (len(p["name"]), p["name"])))
                r = messagebox.askyesnocancel(
                    "Comprobar teléfonos antes de enviar",
                    f"{len(sin)} de {len(base)} teléfono(s) marcados ☑ no están conectados o no se han comprobado "
                    f"en los últimos 15 min:\n{lista}\n\n"
                    "SÍ = comprobarlos y repararlos ahora (⚡, tarda 1-2 min) y después enviar  ← recomendado\n"
                    "NO = enviar ya, sin comprobar\nCancelar = no enviar", parent=self)
                if r is None:
                    return
                if r:
                    self._after_activation = lambda: self._wa_start_checked(resume)
                    self.activate_all_clicked()
                    return
        # 2) Quién participa y qué pasa con los números de los que no
        usar = self.send_targets() if split else ([(self.active_phone(), self.serial)] if self.serial else [])
        restr = [ph["name"] for ph, _s in usar if ph and self.is_restricted(ph)]
        if restr:
            messagebox.showwarning(
                "Teléfonos restringidos", "Marcados como restringidos por WhatsApp: " + ", ".join(restr) + ".\n\n"
                "Desmárcalos (columna ☑) para enviar con los demás, o usa 'Quitar marca de restricción' "
                "cuando WhatsApp la levante.", parent=self)
            return
        if not usar:
            messagebox.showwarning("Sin teléfonos", "No hay ningún teléfono marcado ☑ y conectado.\n\n"
                                   "Marca al menos uno o pulsa ⚡ ACTIVAR TODOS.", parent=self)
            return
        dentro = {self.phone_key_of(ph) for ph, _ in usar if ph}
        nums = self._wa_numbers() or list(self.assign)      # sin tocar la BD en el hilo principal
        por_clave = {}
        for n in nums:
            k = (self.assign.get(n) or {}).get("key")
            if k and k not in dentro:
                por_clave[k] = por_clave.get(k, 0) + 1
        reas = bool(self.wa_reassign.get())
        fuera, hay_desmarcados = [], False
        for k, c in sorted(por_clave.items(), key=lambda kv: (len(self.phone_name_by_key(kv[0])), self.phone_name_by_key(kv[0]))):
            p = self.phone_by_key(k)
            des = (p is not None and not self.phone_enabled(p)) if split else True
            motivo = ("RESTRINGIDO por WhatsApp" if p is not None and self.is_restricted(p) else
                      ("no marcado ☐" if split else "no es el teléfono activo") if des else "APAGADO / sin conexión")
            hay_desmarcados |= des
            fuera.append(f"  • {self.phone_name_by_key(k)} — {motivo} — {c} número(s)"
                         + (" (se reasignan a los demás, como marca 'Reasignar')" if reas and not des else ""))
        if nums and not reas and sum(por_clave.values()) == len(nums):
            messagebox.showwarning(
                "Nadie puede enviar esos números",
                f"Los {len(nums)} número(s) a enviar pertenecen a teléfonos que NO participan:\n" + "\n".join(fuera)
                + "\n\nMárcalos ☑ y pulsa ⚡ ACTIVAR TODOS, o activa 'Reasignar' para que los envíen otros.", parent=self)
            return
        opts = {"reassign_unticked": False}
        cab = f"Se enviarán {len(nums)} número(s) con {len(usar)} de {len(self.cfg['phones'])} teléfono(s).\n\n"
        if fuera and reas and hay_desmarcados:
            r = messagebox.askyesnocancel(
                "Números de teléfonos que NO participan",
                cab + "NO participan:\n" + "\n".join(fuera) + "\n\n'Reasignar' está activo. ¿Qué hago con los números de los "
                "teléfonos NO MARCADOS?\n\nSÍ = que los envíen los demás (esos contactos cambian de teléfono PARA SIEMPRE)\n"
                "NO = dejarlos pendientes\nCancelar = no enviar", parent=self)
            if r is None:
                return
            opts["reassign_unticked"] = bool(r)
        elif fuera or resume:
            if not messagebox.askyesno(
                    "Confirmar envío",
                    cab + (("NO participan (sus números quedarán SIN ENVIAR salvo lo indicado):\n" + "\n".join(fuera) + "\n\n")
                           if fuera else "")
                    + "¿Enviar así?\n\n(No = cancelar. Con ⚡ ACTIVAR TODOS puedes recuperar los que faltan.)", parent=self):
                if resume:
                    self.log("Números cargados en la lista. Pulsa '▶ Enviar' cuando quieras reanudar.")
                return
        self._start_opts = opts
        self._stop_requested = False           # HILO PRINCIPAL: un Detener pulsado mientras arranca ya no se pierde
        self.bg(self.wa_send_bulk)

    def _wa_start_checked(self, resume):
        """HILO PRINCIPAL. ⚡ (pedido por '▶ Enviar') terminó: si algún marcado ☑ NO quedó listo, decide el operador."""
        rep_ = getattr(self, "_act_report", None)
        if rep_ is None:
            messagebox.showwarning("No se pudo comprobar", "⚡ ACTIVAR TODOS no pudo terminar (mira el registro).\n\n"
                                   "El envío NO se inició. Vuelve a pulsar '▶ Enviar'.", parent=self)
            return
        ok_n, marcados, ayuda = rep_
        if ayuda and not messagebox.askyesno(
                "Teléfonos que NO quedaron listos",
                f"{ok_n} de {marcados} teléfonos marcados ☑ están listos.\n\n" + "\n".join(ayuda)
                + "\n\nSÍ = enviar ya con los que están listos. Los otros lo intentan 2 veces y salen FUERA; sus números "
                "quedan pendientes (o pasan a otros teléfonos si 'Reasignar' está activo).\n"
                "NO = no enviar todavía: arréglalos (o desmárcalos ☐) y vuelve a pulsar '▶ Enviar'.", parent=self):
            self.log("Envío NO iniciado: hay teléfonos marcados ☑ que no quedaron listos (ver arriba).")
            return
        self.wa_start(resume=resume, skip_preflight=True)

    def _join_click(self):
        """HILO PRINCIPAL: la selección del árbol y las variables Tk se leen aquí, no en el hilo."""
        self.bg(self.wa_join_selected, self.selected_phone(), self._wa_pkg(), bool(self.wa_human.get()))

    def wa_join_selected(self, sel=None, pkg=None, human=True):
        """Prepara y suma al envío en curso el teléfono dado (o, sin él, todos los marcados ☑ y conectados)."""
        bulk = self._bulk
        if not self.bulk_active or not bulk:
            self.log("No hay ningún envío masivo en curso. Los teléfonos marcados ☑ entrarán al pulsar '▶ Enviar'.")
            return
        if sel is not None and not self.phone_enabled(sel):
            self.log(f"[{sel['name']}] no está marcado ☑. Márcalo (columna ☑) y se sumará solo al envío.")
            return
        try:
            self._adb_devices()
        except Exception:
            pass
        cands = [ph for ph, _s in self.send_targets() if sel is None or ph is sel]
        if sel is not None and not cands:
            self.log(f"[{sel['name']}] no está conectado. Pulsa ⚡ ACTIVAR TODOS (si se reinició pedirá el cable USB).")
            return
        if not cands:
            self.log("No hay teléfonos marcados ☑ y conectados para sumar.")
            return
        pkg = pkg or WA_APPS.get(self.cfg.get("wa_app"), "com.whatsapp.w4b")
        for ph in cands:
            try:
                ok, txt = self._activate_phone(ph, pkg, human)
            except Exception as e:
                ok, txt = False, str(e)[:80]
            self.log(f"[{ph['name']}] {'✔' if ok else '✖'} {txt}")

    def wa_stop(self):
        if getattr(self, "_after_activation", None) is not None:
            self._after_activation = None      # '▶ Enviar' → SÍ → ⚡ en marcha: cancelar el envío encadenado
            self.log("Envío CANCELADO: no arrancará cuando termine ⚡ ACTIVAR TODOS.")
            if not self.bulk_active:
                return
        self._stop_requested = True
        self.running_bulk = False
        self.log("Deteniendo envío masivo... (cada teléfono termina el mensaje que está escribiendo)")

    def wa_send_bulk(self):
        with self._bulk_lock:
            reservado = bool(self._act_busy)
        if reservado:                          # spawn() rechazaría ese teléfono y nadie volvería a sumarlo
            self.log("Hay un teléfono con un envío individual o una activación en curso; espera a que termine "
                     "y vuelve a pulsar '▶ Enviar'.")
            return
        with self.lock:
            if self.bulk_active:
                self.log("Ya hay un envío masivo en curso. Espera el resumen final (o pulsa Detener y espera) antes de iniciar otro.")
                return
            if self._activating.locked():
                self.log("Espera a que termine ⚡ ACTIVAR TODOS antes de iniciar el envío.")
                return
            self.bulk_active = True
        try:
            self._wa_send_bulk_inner()
        finally:
            self._bulk = None                  # también si el motor lanzó una excepción
            self.bulk_active = False
            self.running_bulk = False
            if self.wa_saved_ime:
                for serial in list(self.wa_saved_ime):
                    self._kb_restore(serial)
                self.log("Teclado normal restaurado en los teléfonos.")

    def _db_numbers(self):
        """Números asignados (de la BD si responde, si no del JSON), ordenados por teléfono."""
        if self.db:
            fresh = self._db(self.db.cargar_asignaciones, what="lectura de asignaciones")
            if fresh is not None:
                self.assign = fresh
                save_assignments(self.assign)
        return [n for n, _a in sorted(self.assign.items(), key=lambda kv: (kv[1].get("name") or "", kv[0]))]

    def wa_load_db(self):
        """Carga en la lista de envío todos los números asignados de la base de datos."""
        nums = self._db_numbers()
        if not nums:
            self.log("No hay números asignados en la base de datos. Usa 'Repartir archivo entre teléfonos'.")
            return
        self.wa_list.delete("1.0", "end")
        self.wa_list.insert("1.0", "\n".join(nums))
        self._wa_count_update()
        por = {}
        for a in self.assign.values():
            por[a.get("name")] = por.get(a.get("name"), 0) + 1
        self.log(f"Cargados {len(nums)} números de la base de datos: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(por.items(), key=lambda kv: (len(kv[0] or ''), kv[0] or ''))))

    # Fallos que son culpa del TELÉFONO (pantalla/WhatsApp), no del número: el número vuelve a la cola
    INFRA_ERRORS = ("no se pudo llegar al listado de chats", "no se abrió el buscador de contactos",
                    "se perdió la conexión ADB", "selección de contactos inesperada",
                    "no se abrió el chat tras tocar el resultado")

    def _wa_send_bulk_inner(self):
        _, msg, delay = self._wa_params()
        nums = self._wa_numbers()
        if not nums:
            nums = self._db_numbers()
            if nums:
                self.log(f"Lista vacía: se usan los {len(nums)} números asignados de la base de datos.")
                self.after(0, lambda: (self.wa_list.delete("1.0", "end"), self.wa_list.insert("1.0", "\n".join(nums)),
                                       self._wa_count_update()))
        if not nums or not msg:
            self.log("Falta el mensaje." if nums else "No hay números: la lista está vacía y la base de datos no tiene asignaciones.")
            return
        split = bool(self.wa_split.get())

        def allowed(ph):                       # marca ☑ leída EN VIVO (frozenset inmutable: seguro entre hilos)
            return ph is None or self.phone_enabled(ph)

        # Identidad: cada ip:5555 conectada debe ser el teléfono que dice la lista (las IP cambian de dueño)
        try:
            self._adb_devices()
            self._verify_identity(set())
        except Exception as e:
            FILELOG.error(f"verificación de identidad al iniciar: {e}", e)
        if split:
            targets = self.send_targets()
            skipped = [ph["name"] for ph, _s in self.connected_serials() if not self.phone_enabled(ph)]
            if skipped:
                self.log("Desmarcados (☐), NO participan en este envío: " + ", ".join(skipped) + ".")
        else:
            targets = [(self.active_phone(), self.serial)] if self.serial else []
            targets = [(ph, s_) for ph, s_ in targets if allowed(ph)]
        targets = list({serial: (ph, serial) for ph, serial in targets}.values())
        marked = [ph["name"] for ph, _ in targets if self.is_restricted(ph)]
        if marked:
            names = ", ".join(marked)
            self.log(f"No se inició el envío: {names} está(n) marcado(s) como restringido(s) por WhatsApp. "
                     "Desmárcalos (columna ☑) si decides enviar con los demás, o quita la marca cuando WhatsApp levante la restricción.")
            self.after(0, lambda: messagebox.showwarning(
                "Teléfonos restringidos",
                f"Marcados como restringidos por WhatsApp: {names}.\n\nEl envío no se inició. Desmárcalos (columna ☑) "
                "si decides enviar con los demás, o usa 'Quitar marca de restricción' cuando WhatsApp la levante.", parent=self))
            return
        if not targets:
            self.log("No hay teléfonos conectados y marcados (☑).")
            return

        # Reparto: cada número asignado va a la cola propia de su teléfono; el resto a la cola común
        # (cada teléfono atiende primero su cola propia y luego la común).
        q = queue.Queue()
        own = {}
        for n in nums:
            key = (self.assign.get(n) or {}).get("key")
            if key:
                own.setdefault(key, queue.Queue()).put(n)
            else:
                q.put(n)
        present = {self.phone_key_of(ph) for ph, _s in targets}
        reassign = bool(self.wa_reassign.get())
        opts, self._start_opts = (self._start_opts or {}), {}

        def unticked(key):                     # dueño desmarcado ☐ (con 'solo el activo': todo el que no sea el activo)
            if not split:
                return key not in present
            p = self.phone_by_key(key)
            return p is not None and not self.phone_enabled(p)
        start_unticked = {k for k in own if unticked(k)}

        def may_pool(key):
            """¿Pueden OTROS teléfonos enviar los números de 'key'? (al enviarse cambian de dueño PARA SIEMPRE)"""
            if not reassign:
                return False
            if key in start_unticked:
                return bool(opts.get("reassign_unticked"))   # solo con el SÍ explícito del diálogo de arranque
            return not unticked(key)                         # desmarcado a mitad de envío: congelado
        absent_n = 0
        for key in list(own):
            if key in present:
                continue
            cnt = own[key].qsize()
            motivo = "no marcado ☐" if unticked(key) else "sin conexión"
            if may_pool(key):
                while True:
                    try:
                        q.put(own[key].get_nowait())
                    except queue.Empty:
                        break
                del own[key]
                self.log(f"{cnt} número(s) de {self.phone_name_by_key(key)} ({motivo}) se reasignan a los demás "
                         "(cambian de dueño al enviarse).")
            else:
                absent_n += cnt
                self.log(f"{cnt} número(s) de {self.phone_name_by_key(key)} ({motivo}) quedan en espera de ese teléfono; "
                         "si se suma durante el envío los atenderá, si no, quedarán pendientes.")
        own_n = sum(qq.qsize() for qq in own.values())
        self.log(f"Reparto: {own_n} número(s) asignados a {len(own)} teléfono(s), {q.qsize()} libres para reparto dinámico"
                 + (f", {absent_n} de teléfonos que no participan." if absent_n else "."))
        if q.qsize() + sum(qq.qsize() for k, qq in own.items() if k in present) == 0:
            self.log(f"No se inició el envío: los {len(nums)} número(s) pertenecen a teléfonos que no participan. "
                     "Márcalos ☑ y pulsa ⚡ ACTIVAR TODOS, o activa 'Reasignar'.")
            return

        def remaining():
            return q.qsize() + sum(qq.qsize() for qq in own.values())

        self._last_snap = None
        self._alerted = set()
        self.running_bulk = True
        if self._stop_requested:               # Detener pulsado mientras el envío arrancaba
            self.running_bulk = False
            self.log("Envío cancelado antes de empezar (se pulsó Detener): no se envió ningún mensaje.")
            return
        total = len(nums)
        done = [0]
        report = []
        retries = {}     # número -> veces devuelto a la cola por fallo del TELÉFONO (tope 1)
        hechos, fallidos_vivo = set(), []
        self.wa_pending = self.wa_failed = None          # None = 'Reanudar' lee los archivos, que ahora van al día
        write_numbers(PENDING_FILE, nums)                # desde ya 'Reanudar' refleja ESTE envío, no el anterior
        write_numbers(FAILED_FILE, [])
        stats = {"t0": time.time(), "total": total, "sent": 0, "failed": 0, "requeued": 0,
                 "ok_ts": deque(maxlen=4000), "per": {}}

        def pstat(key, name):                  # llamar SIEMPRE con self.lock tomado
            p = stats["per"].get(key)
            if p is None:
                p = stats["per"][key] = {"name": name, "sent": 0, "failed": 0, "durs": deque(maxlen=20), "last_ok": 0.0,
                                         "cur": None, "state": "", "why": "", "since": 0.0, "joins": 0, "fixes": 0}
            return p
        report_path = os.path.join(os.path.dirname(CONFIG_FILE), time.strftime("envios_%Y%m%d_%H%M%S.csv"))
        csv_live = None
        try:                                   # el reporte se escribe mensaje a mensaje (si la app se cierra, no se pierde)
            csv_live = open(report_path, "w", newline="", encoding="utf-8-sig")
            csv.writer(csv_live, delimiter=";").writerow(["numero", "telefono", "estado", "hora", "mensaje", "serie"])
            csv_live.flush()
        except Exception as e:
            csv_live = None
            self.log(f"Aviso: no se pudo abrir el reporte en vivo ({e}); se guardará al final.")
        self.after(0, lambda: self.wa_progress.configure(maximum=total, value=0))
        self.after(0, lambda: self.wa_status.set(f"0 enviados · faltan {total}"))
        cool_range = self._wa_cool_range()
        avg_cool = sum(cool_range) / 2
        human = bool(self.wa_human.get())
        pkg = self._wa_pkg()
        rng = self._human_ranges()
        por_msg = (45 + len(msg) * (sum(rng) / 2 + 0.12) + avg_cool) if human else (avg_cool + sum(delay) / 2 + 12)
        est = ((total - absent_n) / max(len(targets), 1)) * por_msg
        stall_secs = max(60, int(self.cfg.get("wa_stall_secs", 240)), int(len(msg) * (sum(rng) / 2 + 0.12) * 1.5) + 120)
        par = self._set_dump_parallel(self._dump_par_value())
        self._last_step = {}
        log_path = FILELOG.start_send([
            "=" * 70,
            f"ENVÍO MASIVO iniciado: {total} números, {len(targets)} teléfono(s): "
            + ", ".join(f"{ph['name'] if ph else s} ({s})" for ph, s in targets),
            f"Parámetros: app={self.cfg.get('wa_app')} humano={human} "
            f"carga={delay[0]:.0f}-{delay[1]:.0f}s espera={cool_range[0]:.0f}-{cool_range[1]:.0f}s "
            f"lecturas_simultaneas={par} variar={bool(self.wa_vary.get())} reasignar={reassign} "
            f"sumar_auto={bool(self.wa_auto_join.get())} reporte={os.path.basename(report_path)}",
            f"Mensaje: {msg.replace(chr(10), ' / ')[:300]}",
        ])
        self.log(f"Enviando {total} mensaje(s) con {len(targets)} teléfono(s) "
                 f"(hasta {par} lecturas de pantalla a la vez). Tiempo estimado: ~{fmt_dur(est)} "
                 f"(el tablero de arriba lo corrige con el ritmo real). Registro detallado: logs/{os.path.basename(log_path)}")
        if human:
            self._human_rng = rng
            self._human_country = re.sub(r"\D", "", self.wa_country.get())
            self.log("Modo humano: buscando el número en la app y tecleando letra por letra (enlaces de golpe).")

        restricted = []
        active = {}      # clave de teléfono -> (hilo, serial)
        left = {}        # clave -> hora en que salió por fallos (para no reengancharlo enseguida)
        used = {}        # clave -> nombre, para el resumen
        roster = {}      # clave -> (ph, serial) de todo el que participó (para reenganchar ociosos)

        def phone_key(ph, serial):
            return (ph.get("hw") or ph["name"]) if ph else serial

        def worker(ph, serial, bad, why):
            name = ph["name"] if ph else serial
            wkey = phone_key(ph, serial)
            mine = own.get(wkey)
            last = [None]
            fails = infra = 0
            with self.lock:
                ps = pstat(wkey, name)
                ps.update(name=name, state="enviando", why="", since=time.time(), cur=None)
                ps["joins"] += 1
            while self.running_bulk:
                if not allowed(ph):            # desmarcado ☐ a mitad de envío: sale limpio (sin castigo 'left')
                    why[:] = ["desmarcado", ""]
                    break
                src = q
                try:
                    if mine is not None:
                        try:
                            n = mine.get_nowait()
                            src = mine
                        except queue.Empty:
                            n = q.get_nowait()
                    else:
                        n = q.get_nowait()
                except queue.Empty:
                    break
                t_msg0 = time.time()
                devuelto = sent_ok = salir = False
                try:
                    with self.lock:
                        ps["cur"] = n
                    text, last[0] = self._wa_compose(msg, last[0])
                    try:
                        if human:
                            status = self._send_typed(ph, serial, n, text, delay, cooldown=cool_range, name=name)
                        else:
                            status = self._send_via(ph, serial, n, text, delay, cooldown=cool_range, name=name)
                        sent_ok = status == "enviado"
                        fails = infra = 0
                    except AccountRestricted as e:
                        src.put(n)
                        devuelto = True
                        with self.lock:
                            stats["requeued"] += 1
                            ps["cur"] = None
                        why[:] = ["RESTRINGIDO", str(e)[:120]]
                        restricted.append(name)
                        self.mark_restricted(ph, str(e))
                        self.running_bulk = False
                        self.log(f"[{name}] CUENTA RESTRINGIDA por WhatsApp: {e}. Envío detenido para proteger las demás cuentas.")
                        break
                    except SendUnconfirmed as e:
                        status = f"NO enviado: SIN CONFIRMAR (pudo salir; mira el chat antes de reintentar): {e}"
                        fails += 1
                        FILELOG.error(f"[{name}] envío SIN CONFIRMAR a {n}: {e}", e.__cause__)
                        timed_out = isinstance(e.__cause__, subprocess.TimeoutExpired)
                        if timed_out or fails >= 5 or not self.is_online(serial):
                            salir = True
                            bad[0] = True
                            why[:] = ["SALIÓ", "el teléfono no responde (ADB)" if timed_out else str(e)[:120]]
                            self.log(f"[{name}] ⛔ SALIÓ DEL ENVÍO: dejó de responder justo al enviar a {n}. Ese número NO se "
                                     "reenvía solo (el mensaje pudo salir). Pulsa ⚡ ACTIVAR TODOS para repararlo.")
                    except Exception as e:
                        status = f"NO enviado: {e}"
                        if not self.running_bulk and "detenido" in status:
                            src.put(n)
                            devuelto = True
                            with self.lock:
                                stats["requeued"] += 1
                                ps["cur"] = None
                            break
                        fails += 1
                        es_infra = (any(t in str(e) for t in self.INFRA_ERRORS)
                                    or not isinstance(e, (RuntimeError, subprocess.TimeoutExpired)))   # fallo interno: no es culpa del número
                        infra = infra + 1 if es_infra else 0
                        last_step = self._last_step.get(name, (0, None, "?"))[2]
                        FILELOG.error(f"[{name}] fallo #{fails} con {n} en el paso '{last_step}': {e}",
                                      e if not isinstance(e, RuntimeError) else None)
                        timed_out = isinstance(e, subprocess.TimeoutExpired)
                        if timed_out or fails >= 5 or infra >= 2 or not self.is_online(serial):
                            (q if may_pool(wkey) else src).put(n)
                            devuelto = True
                            with self.lock:
                                stats["requeued"] += 1
                                ps["cur"] = None
                            bad[0] = True
                            why[:] = ["SALIÓ", "el teléfono no responde (ADB)" if timed_out else str(e)[:120]]
                            self.log(f"[{name}] ⛔ SALIÓ DEL ENVÍO tras {fails} fallo(s) seguidos ({e}). Devuelve {n} a la cola. "
                                     "Pulsa ⚡ ACTIVAR TODOS para repararlo y volver a sumarlo.")
                            break
                        if es_infra:           # culpa del teléfono: el número NO se quema; una segunda oportunidad
                            with self.lock:
                                again = retries.get(n, 0) < 1
                                if again:
                                    retries[n] = retries.get(n, 0) + 1
                                    stats["requeued"] += 1
                                    ps["cur"] = None
                            if again:
                                src.put(n)
                                devuelto = True
                                self.log(f"[{name}] ⚠ WhatsApp no respondió bien ({e}); {n} vuelve a la cola (no cuenta como fallido).")
                                time.sleep(1.0)
                                continue
                    ok = status == "enviado"
                    if ok:
                        try:
                            self.assign_number(n, ph)
                        except Exception as e:
                            FILELOG.error(f"[{name}] asignar {n}: {e}", e)
                    hora = time.strftime("%Y-%m-%d %H:%M:%S")
                    now = time.time()
                    rkey = self.phone_key_of(ph) or serial
                    row = (n, name, status, hora, text.replace("\n", " / "), rkey)
                    with self.lock:
                        done[0] += 1
                        report.append(row)
                        devuelto = True        # ya quedó registrado: no se devuelve a la cola pase lo que pase
                        stats["sent" if ok else "failed"] += 1
                        ps["sent" if ok else "failed"] += 1
                        ps["durs"].append(now - t_msg0)
                        ps["cur"] = None
                        if ok:
                            ps["last_ok"] = now
                            stats["ok_ts"].append(now)
                        s_t, f_t, s_m, f_m = stats["sent"], stats["failed"], ps["sent"], ps["failed"]
                        if csv_live:
                            try:
                                csv.writer(csv_live, delimiter=";").writerow(row)
                                csv_live.flush()
                            except Exception:
                                pass
                        hechos.add(n)
                        if not ok and "SIN CONFIRMAR" not in status:
                            fallidos_vivo.append(n)
                            write_numbers(FAILED_FILE, fallidos_vivo)
                        write_numbers(PENDING_FILE, [x for x in nums if x not in hechos])
                    cola = (f"este teléfono: {s_m} enviados, {f_m} fallidos — TOTAL: {s_t} enviados, {f_t} fallidos, "
                            f"faltan {total - s_t - f_t}")
                    self.log(f"[{name}] ✔ enviado a {n} — {cola}" if ok else f"[{name}] ✖ {status} (número {n}) — {cola}")
                    if self.db:                # la BD va DESPUÉS del aviso visible; misma clave que el CSV
                        self._db(self.db.registrar_envio, n, rkey, name, status, hora,
                                 text.replace("\n", " / "), os.path.basename(report_path), what="tabla de envíos")
                except BaseException:
                    if not devuelto and not sent_ok:
                        src.put(n)             # un error interno nunca pierde el número en curso
                    raise
                if salir:
                    break
                time.sleep(1.0)
            if why[0] == "terminó" and not self.running_bulk:
                why[:] = ["detenido", ""]
            if why[0] == "desmarcado":
                self.log(f"[{name}] desmarcado ☐: salió del envío. Sus números quedan en espera.")

        closing = [False]                      # True (bajo _bulk_lock) = el supervisor ya decidió cerrar: nadie más entra
        solo = None if split else {phone_key(p_, s_) for p_, s_ in targets}   # 'solo el activo': nadie más entra

        def spawn(ph, serial, manual=False, from_activation=False):
            """Suma un teléfono al envío. Devuelve un texto de motivo si no se pudo. ÚNICO punto de entrada."""
            key = phone_key(ph, serial)
            name = ph["name"] if ph else serial
            with self._bulk_lock:              # solo comprobaciones en memoria: microsegundos
                if closing[0] or not self.running_bulk or remaining() == 0:
                    return "el envío ya terminó o no quedan números"
                if key in active and active[key][0].is_alive():
                    return "ya está enviando"
                if any(t_.is_alive() and s_ == serial for t_, s_ in active.values()):
                    return "ya está enviando ese aparato con otro nombre (IP duplicada)"
                if solo is not None and key not in solo:
                    return "este envío es 'solo el activo' (los demás teléfonos no participan)"
                if key in self._act_busy and not from_activation:
                    return "lo está preparando ⚡ (se sumará solo al terminar)"
                if self.is_restricted(ph):
                    return "está marcado como restringido"
                if not allowed(ph):
                    return "no está marcado ☑ para enviar"
                mine_q = own.get(key)
                if q.qsize() == 0 and not (mine_q is not None and mine_q.qsize()):
                    return "no le quedan números (los que faltan son de otros teléfonos)"
                if not manual and time.time() - left.get(key, 0) < 120:
                    return "salió por fallos hace poco (usa ⚡ ACTIVAR TODOS para repararlo y sumarlo)"
                bad, why = [False], ["terminó", ""]

                def run():
                    try:
                        if self._is_other_device(ph, serial):     # jamás enviar los números de A desde el WhatsApp de B
                            bad[0] = True
                            why[:] = ["SALIÓ", "en su IP contesta OTRO aparato (IP cruzada)"]
                            self.log(f"[{name}] ⛔ NO se suma: en {serial} contesta OTRO aparato. "
                                     "Pulsa '🔍 Buscar en la red WiFi' o pásale el CABLE USB.")
                            return
                        if human:              # teclado FUERA de _bulk_lock, en el hilo propio (arranques en paralelo)
                            try:
                                self._kb_ensure(serial)
                            except Exception as e:
                                self.log(f"[{name}] no se pudo activar el teclado ADB: {e}")
                        worker(ph, serial, bad, why)
                    except Exception as e:     # un trabajador nunca muere en silencio (pythonw no tiene stderr)
                        bad[0] = True
                        why[:] = ["SALIÓ", f"fallo interno: {e}"[:120]]
                        FILELOG.error(f"[{name}] trabajador: {e}", e)
                        self.log(f"[{name}] ⛔ SALIÓ DEL ENVÍO por un fallo interno ({str(e)[:80]}). Mira logs/.")
                    finally:
                        with self._bulk_lock:
                            active.pop(key, None)
                            if bad[0]:
                                left[key] = time.time()
                        with self.lock:        # después de soltar _bulk_lock: nunca anidados
                            pstat(key, name).update(state=why[0], why=why[1], since=time.time(), cur=None)
                t = threading.Thread(target=run, daemon=True)
                active[key] = (t, serial)
                used[key] = name
                roster[key] = (ph, serial)
                t.start()
            return None

        def mark_ok(key):                      # ⚡ reparó a uno que figuraba FUERA y ya no tiene números: deja de estar FUERA
            with self.lock:
                p_ = stats["per"].get(key)
                if p_ and p_["state"] == "SALIÓ":
                    p_.update(state="terminó", why="")

        self._bulk = {"q": q, "own": own, "spawn": spawn, "active": active, "left": left, "used": used, "mark_ok": mark_ok,
                      "stats": stats, "total": total, "stall": stall_secs, "report": os.path.basename(report_path)}
        for ph, serial in targets:
            spawn(ph, serial, manual=True)

        # Supervisor: vive mientras haya trabajadores o números; suma teléfonos, vigila bloqueos, reengancha ociosos,
        # repara (opcional) y da una última oportunidad a los que salieron antes de cerrar.
        auto_join = bool(self.wa_auto_join.get()) and split
        auto_fix = bool(self.wa_auto_fix.get())
        fix_max = int(self.cfg.get("wa_reparar_max", 2))
        last_chance = bool(self.cfg.get("wa_ultima_oportunidad", True))
        fixing_now, last_done = set(), set()
        stalled = {}
        idle_since = hold_since = None
        last_check = last_idle = 0.0
        last_resumen = time.time()

        def fix_one(k, p, motivo):
            try:
                self.log(f"[{p['name']}] 🔧 {motivo}: despertando y reabriendo WhatsApp…")
                ok, txt = self._activate_phone(p, pkg, human)        # misma rutina y misma exclusión que el botón ⚡
                self.log(f"[{p['name']}] {'✔' if ok else '✖'} {txt}")
            except Exception as e:
                self.log(f"[{p['name']}] ✖ la reparación falló: {str(e)[:80]}")
            finally:
                fixing_now.discard(k)

        def out_candidates():
            """Teléfonos marcados que SALIERON y a los que aún les quedan números que atender."""
            with self.lock:
                cand = [(k, p["since"], p["fixes"]) for k, p in stats["per"].items() if p["state"] == "SALIÓ"]
            res = []
            for k, since, fixes in cand:
                p = self.phone_by_key(k)
                if p is None or not allowed(p) or self.is_restricted(p):
                    continue
                if not (q.qsize() or (own.get(k) is not None and own[k].qsize())):
                    continue
                res.append((k, p, since, fixes))
            return res

        while self.running_bulk:
            with self._bulk_lock:
                alive = [k for k, (t, _s) in active.items() if t.is_alive()]
                alive_names = [used.get(k, k) for k in alive]
                activating = bool(self._act_busy)
            rem = remaining()
            if rem == 0 and not alive:
                break                          # sigue vivo mientras haya trabajadores: la cola final tiene vigilante
            now = time.time()
            if reassign and rem:
                for key, qq in list(own.items()):
                    if (key not in alive and qq.qsize() and may_pool(key) and key not in fixing_now
                            and key not in self._act_busy):                  # ni los de uno que se está reparando
                        moved = 0
                        while True:
                            try:
                                q.put(qq.get_nowait())
                                moved += 1
                            except queue.Empty:
                                break
                        if moved:
                            self.log(f"{moved} número(s) de {self.phone_name_by_key(key)} pasan a los demás teléfonos.")
            for nm in alive_names:
                ts, num, text = self._last_step.get(nm, (now, None, "esperando su primer número"))
                idle = now - ts
                if idle > stall_secs and stalled.get(nm) != ts:
                    stalled[nm] = ts
                    self.log(f"[{nm}] POSIBLE BLOQUEO: lleva {idle:.0f} s sin avanzar. Último paso: '{text}'"
                             + (f" (número {num})" if num else "") + ". Mira la pantalla de ese teléfono.")
            # Reenganche de ociosos: si la cola común tiene números, los teléfonos que ya terminaron vuelven a tomar
            if q.qsize() and (now - last_idle >= 10 or not alive):
                last_idle = now
                with self.lock:
                    outs = {k for k, p in stats["per"].items() if p["state"] == "SALIÓ"}
                for key, (rph, rserial) in list(roster.items()):
                    if key in alive or key in outs:
                        continue
                    if rph is not None:                       # el serial pudo cambiar (nueva IP, USB<->WiFi)
                        rserial = self.phone_state(rph)[0]
                        if not rserial:
                            continue
                    if spawn(rph, rserial) is None:
                        alive.append(key)
                        self.log(f"[{rph['name'] if rph else rserial}] vuelve a tomar números de la cola común.")
            if auto_join and now - last_check >= 10:
                last_check = now
                try:
                    self._adb_devices()
                    for ph, serial in self.send_targets():          # solo marcados ☑
                        key = phone_key(ph, serial)
                        if key in alive:
                            continue
                        if self.is_restricted(ph) or time.time() - left.get(key, 0) < 120:
                            continue
                        with self.lock:
                            salio = (stats["per"].get(key) or {}).get("state") == "SALIÓ"
                        if salio:                                   # un teléfono roto solo vuelve reparado (⚡)
                            continue
                        if not self.is_online(serial):
                            continue
                        if self._is_other_device(ph, serial):
                            if stalled.get(("otro", key)) != serial:       # avisar una sola vez
                                stalled[("otro", key)] = serial
                                self.log(f"[{ph['name']}] NO se suma: en {serial} contesta OTRO aparato. "
                                         "Pulsa ⚡ ACTIVAR TODOS o '🔍 Buscar en la red WiFi'.")
                            continue
                        if spawn(ph, serial) is None:
                            alive.append(key)
                            self.log(f"[{ph['name']}] se conectó durante el envío y se sumó al reparto "
                                     f"({len(alive)} teléfono(s) enviando).")
                except Exception as e:
                    self.log(f"Supervisor del envío: {e}")
            # Reparación automática a mitad de envío (opcional, casilla 'Reparar solo al que se caiga')
            if auto_fix and rem and not self._activating.locked():
                for k, p, since, fixes in out_candidates():
                    if k in alive or k in fixing_now or fixes >= fix_max or now - since < 60:
                        continue
                    with self.lock:
                        stats["per"][k]["fixes"] += 1
                        stats["per"][k]["since"] = now            # el siguiente intento espera otros 60 s
                    fixing_now.add(k)
                    threading.Thread(target=fix_one, args=(k, p, "reparación automática"), daemon=True).start()
            if now - last_resumen >= 300:
                last_resumen = now
                snap_now = self._bulk_snapshot()
                if snap_now:
                    self.log("RESUMEN — " + self._snap_line(snap_now))
            if alive or rem == 0:
                idle_since = hold_since = None
            elif activating or fixing_now:     # nunca se cierra con una activación/reparación en marcha… con tope
                idle_since = None
                hold_since = hold_since or now
                if now - hold_since > 420:
                    self.log("La reparación tarda demasiado; se cierra el envío con los números pendientes.")
                    break
            else:
                hold_since = None
                # Última oportunidad: nadie envía y quedan números de teléfonos que salieron -> un intento de reparación
                if last_chance and not self._activating.locked():
                    nuevos = [(k, p) for k, p, _since, _f in out_candidates() if k not in last_done]
                    if nuevos:
                        for k, p in nuevos:
                            last_done.add(k)
                            fixing_now.add(k)
                            threading.Thread(target=fix_one, args=(k, p, "última oportunidad antes de cerrar"),
                                             daemon=True).start()
                        time.sleep(2)
                        continue
                if idle_since is None:
                    idle_since = time.time()
                    if auto_join:
                        self.log("Ningún teléfono está enviando; esperando 90 s por si alguno se reconecta...")
                if time.time() - idle_since > (90 if auto_join else 20):
                    with self._bulk_lock:      # 'alive'/'activating' son de inicio de vuelta: ⚡ pudo sumar a alguien
                        fresh = (any(t.is_alive() for t, _s in active.values()) or bool(self._act_busy)
                                 or bool(fixing_now))
                        if not fresh:
                            closing[0] = True  # atómico frente a spawn(): desde aquí nadie más entra
                    if fresh:
                        idle_since = None
                        time.sleep(2)
                        continue
                    det = [f"{qq.qsize()} de {self.phone_name_by_key(k)}" for k, qq in own.items() if qq.qsize()]
                    if q.qsize():
                        det.append(f"{q.qsize()} sin teléfono asignado")
                    self.log(f"Se cierra el envío: quedan {rem} número(s) sin enviar ({', '.join(det)}) y ningún teléfono "
                             "puede atenderlos. Pulsa ⚡ ACTIVAR TODOS y luego ↺ Reanudar pendientes.")
                    break
            time.sleep(2)
        with self._bulk_lock:
            closing[0] = True                  # spawn() ya no acepta a nadie: la foto de hilos es completa
            threads = [t for t, _s in active.values()]
        for t in threads:
            t.join()
        snap = self._bulk_snapshot()
        if snap:
            snap["live"] = False
            self._last_snap = snap
        self._bulk = None
        for nm in sorted(used.values()):
            times = [r[3] for r in report if r[1] == nm and r[3]]
            FILELOG.write("INFO", f"[{nm}] mensajes procesados: {len(times)}"
                          + (f", primero {times[0][11:]}, último {times[-1][11:]}" if times else ""))
        stopped = self._stop_requested
        self.running_bulk = False
        pending = []
        while not q.empty():
            try:
                pending.append(q.get_nowait())
            except queue.Empty:
                break
        for n in pending:
            report.append((n, "-", "pendiente (no enviado)", "", "", ""))
        for key, qq in own.items():
            nm = self.phone_name_by_key(key)
            p_own = self.phone_by_key(key)
            lbl = "no marcado" if (p_own is not None and not self.phone_enabled(p_own)) else "no disponible"
            while True:
                try:
                    n = qq.get_nowait()
                except queue.Empty:
                    break
                pending.append(n)
                report.append((n, "-", f"pendiente (asignado a {nm}, {lbl})", "", "", key))
        failed = [r[0] for r in report if r[2].startswith("NO enviado") and "SIN CONFIRMAR" not in r[2]]
        sin_conf = [r[0] for r in report if "SIN CONFIRMAR" in r[2]]
        self.wa_pending, self.wa_failed = pending, failed
        write_numbers(PENDING_FILE, pending)
        write_numbers(FAILED_FILE, failed)
        motivo_fin = ("DETENIDO" if stopped else "DETENIDO (cuenta restringida)" if restricted
                      else "CERRADO SIN TERMINAR" if pending else "TERMINADO")
        final_snap = dict(self._last_snap or {}, live=False, fin=time.strftime("%H:%M"), titulo=motivo_fin,
                          n_pend=len(pending), dur=time.time() - stats["t0"])
        self._last_snap = final_snap
        self.after(1500, lambda: setattr(self, "_last_snap", final_snap))   # por si un tic rezagado lo pisó
        if pending or failed:
            self.log(f"Guardados {len(pending)} pendientes y {len(failed)} fallidos. "
                     "Pulsa 'Reanudar pendientes' para volver a cargarlos en la lista.")
        if restricted:
            names = ", ".join(sorted(set(restricted)))
            self.after(0, lambda: messagebox.showwarning(
                "Cuenta restringida por WhatsApp",
                f"WhatsApp restringió la cuenta de: {names}.\n\n"
                "El envío se detuvo para no exponer las demás cuentas. Los números pendientes quedaron en el reporte "
                "como 'pendiente (no enviado)'.\n\nUna restricción suele durar de horas a días y, si se repite, "
                "puede volverse permanente.", parent=self))
        try:
            if csv_live:
                csv_live.close()
            with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["numero", "telefono", "estado", "hora", "mensaje", "serie"])
                w.writerows(report)
            self.log(f"Reporte guardado: {os.path.basename(report_path)}")
        except Exception as e:
            self.log(f"No se pudo guardar el reporte: {e}")
        if self.db:
            pend_rows = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in report if r[2].startswith("pendiente")]
            if pend_rows:
                self._db(self.db.registrar_envios, pend_rows, os.path.basename(report_path), what="tabla de envíos")
        per = {}
        for _n, _nm, stt, _t, _m, k in report:
            d = per.setdefault(k or "-", {"name": self.phone_name_by_key(k) if k else "(sin teléfono asignado)",
                                          "ok": 0, "bad": 0, "pend": 0})
            d["ok" if stt == "enviado" else "bad" if stt.startswith("NO enviado") else "pend"] += 1
        with self.lock:
            outs = {k: (p["since"], p["why"]) for k, p in stats["per"].items() if p["state"] == "SALIÓ"}
        n_ok, n_bad = sum(d["ok"] for d in per.values()), sum(d["bad"] for d in per.values())
        self.log(f"══════ ENVÍO {motivo_fin} ══════  duró {fmt_dur(time.time() - stats['t0'])}")
        self.log(f"ENVIADOS {n_ok} · FALLIDOS {n_bad} · SIN ENVIAR {len(pending)} · TOTAL {total}")
        for k, d in sorted(per.items(), key=lambda kv: (len(kv[1]["name"]), kv[1]["name"])):
            extra = (f"   ← FUERA desde {time.strftime('%H:%M', time.localtime(outs[k][0]))} ({outs[k][1]})"
                     if k in outs else "")
            self.log(f"  {d['name']:<16}{d['ok']:>4} enviados {d['bad']:>4} fallidos {d['pend']:>4} sin enviar{extra}")
        if sin_conf:
            self.log(f"⚠ {len(sin_conf)} SIN CONFIRMAR (el teléfono falló justo al tocar Enviar; el mensaje pudo salir): "
                     + ", ".join(sin_conf[:12]) + ("…" if len(sin_conf) > 12 else "")
                     + ". No se reenvían solos: mira esos chats.")
        if pending or n_bad:
            self.log((f"Para enviar los {len(pending)} que faltan: ⚡ ACTIVAR TODOS y luego ↺ Reanudar pendientes. " if pending else "")
                     + (f"Marca 'incluir fallidos' para reintentar los {n_bad} fallidos (muchos 'sin WhatsApp' sí tienen)." if n_bad else ""))
        FILELOG.end_send(f"FIN DEL ENVÍO: {n_ok} enviados, {n_bad} fallidos, {len(pending)} sin enviar, de {total} "
                         f"({stats['requeued']} devueltos a la cola).")

    # ---------------------------------------------------------- tablero en vivo
    def _bulk_snapshot(self):
        """Foto coherente de los contadores. Segura desde cualquier hilo; candados de uno en uno, NUNCA anidados."""
        bulk = self._bulk                      # copiar la referencia: pasa a None en el hilo del envío
        if not bulk:
            return None
        now = time.time()
        with self._bulk_lock:
            alive = {k for k, (t, _s) in bulk["active"].items() if t.is_alive()}
            fixing = set(self._act_busy)
        st = bulk["stats"]
        with self.lock:
            sent, failed = st["sent"], st["failed"]
            per = {k: dict(v, durs=list(v["durs"])) for k, v in st["per"].items()}
            recent = sum(1 for t in st["ok_ts"] if now - t <= 600)
        own = {k: qq.qsize() for k, qq in bulk["own"].items()}
        pool = bulk["q"].qsize()
        orphan = sum(c for k, c in own.items() if k not in alive) + (pool if not alive else 0)
        span = min(600.0, now - st["t0"])
        return {"ts": now, "t0": st["t0"], "total": bulk["total"], "sent": sent, "failed": failed,
                "faltan": bulk["total"] - sent - failed, "orphan": orphan, "pool": pool, "own": own, "per": per,
                "alive": alive, "fixing": fixing, "rate_h": (recent / span * 3600 if span >= 120 and recent >= 3 else None),
                "eta": bulk_eta(alive, own, pool, per), "steps": dict(self._last_step), "stall": bulk["stall"],
                "live": True, "stopping": not self.running_bulk, "report": bulk.get("report")}

    def _snap_line(self, s):
        t = f"enviados {s['sent']} · fallidos {s['failed']} · faltan {s['faltan']}"
        if s["orphan"]:
            t += f" ({s['orphan']} SIN TELÉFONO que los atienda)"
        if s["rate_h"]:
            t += f" · ≈ {s['rate_h']:.0f} por hora"
        if s["eta"] is not None:
            t += f" · termina ≈ {time.strftime('%H:%M', time.localtime(s['ts'] + s['eta']))} (en {fmt_dur(s['eta'])})"
        return t

    def _tick(self):
        """SOLO hilo principal, cada segundo; ni ADB ni BD. Pinta el tablero y las filas de la tabla."""
        try:
            snap = self._bulk_snapshot()
            if snap and self._bulk is not None:    # no pisar la foto FINAL con una viva tomada justo antes de cerrar
                self._last_snap = snap
            view = (snap if self._bulk is not None else None) or self._last_snap
            self._paint_banner(view)
            self._paint_rows(view)
            self._update_marks_label()
        except Exception as e:                 # un fallo de pintura nunca mata el temporizador
            FILELOG.error(f"tablero: {e}", e)
        finally:
            self.after(1000, self._tick)

    def _paint_banner(self, s):
        phones = self.cfg["phones"]
        marked = [p for p in phones if self.phone_enabled(p)]
        ready = [p for p in marked if self.phone_state(p)[0] and not self.is_restricted(p)]
        if not s or not (s.get("fin") or self.bulk_active):
            self.bn_title.configure(text="SIN ENVÍO EN CURSO", fg="#555555")
            for w in (self.bn_sent, self.bn_fail, self.bn_left):
                w.configure(text="")
            self.bn_rate.configure(text=f"Teléfonos marcados ☑: {len(marked)} de {len(phones)} · conectados: {len(ready)}")
            bad = [f"{p['name']} ({self._row_view(p, None)[0][2]})" for p in marked if p not in ready]
            self.bn_phones.configure(text=("⚠ No listos: " + "; ".join(bad) + " → pulsa ⚡ ACTIVAR TODOS") if bad else "",
                                     fg="#c00000")
            return
        fin = s.get("fin")
        self.bn_sent.configure(text=f"ENVIADOS {s['sent']}")
        self.bn_fail.configure(text=f"FALLIDOS {s['failed']}")
        self.wa_progress.configure(maximum=max(s["total"], 1), value=s["sent"] + s["failed"])
        if fin:
            self.bn_title.configure(text=f"ENVÍO {s.get('titulo', 'TERMINADO')} {fin}",
                                    fg="#33658a" if s.get("titulo") == "TERMINADO" else "#b06000")
            self.bn_left.configure(text=f"SIN ENVIAR {s.get('n_pend', s['faltan'])} de {s['total']}")
            self.bn_rate.configure(text=f"Duró {fmt_dur(s.get('dur', 0))}" + (f" · reporte {s['report']}" if s.get("report") else ""))
            self.wa_status.set(f"{s['sent']} enviados · {s['failed']} fallidos · sin enviar {s.get('n_pend', s['faltan'])}")
        else:
            saving = self._bulk is None
            self.bn_title.configure(
                text=("GUARDANDO REPORTE…" if saving else "DETENIENDO… (cada teléfono termina su mensaje)" if s["stopping"]
                      else "ENVIANDO"), fg="#b06000" if (saving or s["stopping"]) else "#0a7d2c")
            self.bn_left.configure(text=f"FALTAN {s['faltan']} de {s['total']}")
            rate = (f"≈ {s['rate_h']:.0f} por hora" if s["rate_h"] else "ritmo: calculando…")
            eta = (f" · termina ≈ {time.strftime('%H:%M', time.localtime(s['ts'] + s['eta']))} (en {fmt_dur(s['eta'])})"
                   if s["eta"] is not None else "")
            self.bn_rate.configure(text=rate + eta + (f" · {s['orphan']} SIN TELÉFONO que los atienda" if s["orphan"] else "")
                                   + f" · lleva {fmt_dur(s['ts'] - s['t0'])}")
            self.wa_status.set(f"{s['sent']} enviados · {s['failed']} fallidos · faltan {s['faltan']}")
        outs = [(k, p) for k, p in s["per"].items() if p["state"] == "SALIÓ" and k not in s["alive"]]
        n_done = sum(1 for k, p in s["per"].items() if p["state"] == "terminó" and k not in s["alive"])
        if outs:
            txt = "; ".join(f"{p['name']} desde {time.strftime('%H:%M', time.localtime(p['since']))} "
                            f"({s['own'].get(k, 0)} números esperando)" for k, p in outs)
            self.bn_phones.configure(fg="#c00000", text=(f"Quedaron FUERA: {txt}. ⚡ ACTIVAR TODOS y luego ↺ Reanudar." if fin
                                     else f"⚠ {len(outs)} FUERA: {txt} → pulsa ⚡ ACTIVAR TODOS"))
            if not fin:
                for k, _p in outs:
                    if k not in self._alerted:
                        self._alerted.add(k)
                        self.bell()            # una vez por teléfono y envío
        elif fin and not self.bulk_active:
            bad = [f"{p['name']} ({self._row_view(p, None)[0][2]})" for p in marked if p not in ready]
            self.bn_phones.configure(fg="#c00000", text=("⚠ No listos para el próximo envío: " + "; ".join(bad)
                                                         + " → pulsa ⚡ ACTIVAR TODOS") if bad else "")
        else:
            self.bn_phones.configure(fg="#1f2937", text=("" if fin else
                                     f"Teléfonos: {len(s['alive'])} enviando · {n_done} terminaron"))

    def _update_wa_btn_label(self):
        ph = self.active_phone()
        if ph and ph.get("wa_x") and ph.get("wa_y"):
            self.wa_btn_var.set(f"({ph['wa_x']}, {ph['wa_y']})")
        else:
            self.wa_btn_var.set("automático")

    # ---------------------------------------------------------- pantalla
    def take_screenshot(self):
        if not self.serial:
            return
        png = self.run_adb("exec-out", "screencap", "-p", binary=True)
        if not png.startswith(b"\x89PNG"):
            self.log("No se pudo capturar la pantalla.")
            return
        self.last_png = png
        self.after(0, self._show_png, png)

    def _show_png(self, png):
        try:
            img = tk.PhotoImage(data=png)
        except tk.TclError as e:
            self.log(f"No se pudo mostrar la imagen: {e}")
            return
        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 300)
        factor = max(1, -(-img.width() // cw), -(-img.height() // ch))
        self.scale = factor
        self.screen_w, self.screen_h = img.width(), img.height()
        self.photo = img.subsample(factor, factor)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

    def save_screenshot(self):
        if not self.last_png:
            self.log("Primero captura la pantalla.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if p:
            with open(p, "wb") as f:
                f.write(self.last_png)
            self.log(f"Guardado {p}")

    def auto_capture(self):
        if self.auto_var.get():
            self.bg(self.take_screenshot)
            self.after(3000, self.auto_capture)

    def on_canvas_move(self, e):
        if self.photo:
            self.coord_var.set(f"({int(e.x * self.scale)}, {int(e.y * self.scale)})")

    def on_canvas_click(self, e):
        if not self.photo:
            return
        x, y = int(e.x * self.scale), int(e.y * self.scale)
        self.tap_x.set(str(x))
        self.tap_y.set(str(y))
        self.bg(self._click_and_refresh, x, y)

    def on_canvas_right_click(self, e):
        """Clic derecho: guarda el punto como botón 'Enviar' de WhatsApp para el teléfono activo."""
        ph = self.active_phone()
        if not self.photo or not ph:
            return
        x, y = int(e.x * self.scale), int(e.y * self.scale)
        ph["wa_x"], ph["wa_y"] = x, y
        save_config(self.cfg)
        self._update_wa_btn_label()
        self.log(f"[{ph['name']}] botón 'Enviar' fijado en ({x}, {y})")

    def _click_and_refresh(self, x, y):
        self.tap(x, y)
        time.sleep(0.8)
        self.take_screenshot()


# ---------------------------------------------------------------- monitor en vivo
class Monitor(tk.Toplevel):
    """Ventana con la pantalla de todos los teléfonos conectados, en mosaico y actualizándose sola."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Monitor en vivo")
        self.configure(bg="#111")
        try:
            self.state("zoomed")
        except Exception:
            self.geometry("1400x850")
        self.tiles = {}          # serial -> dict(frame, canvas, photo, ...)
        self.focus_serial = None
        self.alive = True
        self.gen = 0             # generación del mosaico: los hilos viejos terminan al reconstruir
        self.paused = tk.BooleanVar(value=False)
        self.interval = tk.StringVar(value="1")
        self.protocol("WM_DELETE_WINDOW", self.close)

        bar = tk.Frame(self, bg="#1e1e1e", padx=10, pady=6)
        bar.pack(fill="x")
        tk.Label(bar, text="MONITOR EN VIVO", fg="#eee", bg="#1e1e1e", font=("Segoe UI", 12, "bold")).pack(side="left")
        self.info = tk.Label(bar, text="", fg="#9ad", bg="#1e1e1e", font=("Segoe UI", 10))
        self.info.pack(side="left", padx=16)
        ttk.Button(bar, text="Abrir todos en scrcpy (mosaico)", command=app.launch_scrcpy_all).pack(side="right")
        ttk.Checkbutton(bar, text="Pausar", variable=self.paused).pack(side="right", padx=8)
        ttk.Entry(bar, textvariable=self.interval, width=4).pack(side="right")
        tk.Label(bar, text="Actualizar cada (s):", fg="#ccc", bg="#1e1e1e").pack(side="right", padx=(8, 4))
        ttk.Button(bar, text="Actualizar lista", command=self.rebuild).pack(side="right", padx=8)
        tk.Label(self, text="Clic = tocar  |  arrastrar = deslizar  |  doble clic = ampliar / volver al mosaico",
                 fg="#777", bg="#111", font=("Segoe UI", 9)).pack(fill="x")

        self.grid_frame = tk.Frame(self, bg="#111")
        self.grid_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.bind("<Configure>", lambda e: self._relayout() if e.widget is self else None)
        self.after(300, self.rebuild)

    # --- construcción del mosaico
    def rebuild(self):
        for t in self.tiles.values():
            t["frame"].destroy()
        self.tiles = {}
        self.focus_serial = None
        for ph, serial in self.app.connected_serials():
            self._make_tile(ph, serial)
        self._relayout()
        self.info.configure(text=f"{len(self.tiles)} teléfono(s) conectado(s)")
        self.gen += 1
        for serial in self.tiles:
            threading.Thread(target=self._loop, args=(serial, self.gen), daemon=True).start()

    def _make_tile(self, ph, serial):
        f = tk.Frame(self.grid_frame, bg="#1e1e1e", bd=0, highlightthickness=2, highlightbackground="#333")
        head = tk.Frame(f, bg="#1e1e1e")
        head.pack(fill="x")
        name = ph["name"] if ph else serial
        tk.Label(head, text=name, fg="#fff", bg="#1e1e1e", font=("Segoe UI", 11, "bold")).pack(side="left", padx=8, pady=4)
        via = "WiFi" if ":" in serial else "USB"
        status = tk.Label(head, text=f"● {via}", fg="#4c4", bg="#1e1e1e", font=("Segoe UI", 9))
        status.pack(side="left")
        if self.app.is_restricted(ph):
            status.configure(text=f"● {via} · RESTRINGIDO por WhatsApp", fg="#e55", font=("Segoe UI", 9, "bold"))
            f.configure(highlightbackground="#c00000")
        bat = tk.Label(head, text="", fg="#aaa", bg="#1e1e1e", font=("Segoe UI", 9))
        bat.pack(side="right", padx=8)
        canvas = tk.Canvas(f, bg="#000", highlightthickness=0, cursor="hand2")
        canvas.pack(fill="both", expand=True)
        foot = tk.Frame(f, bg="#1e1e1e")
        foot.pack(fill="x")
        for txt, code in (("Inicio", "KEYCODE_HOME"), ("Atrás", "KEYCODE_BACK"),
                          ("Recientes", "KEYCODE_APP_SWITCH"), ("Encender", "KEYCODE_POWER")):
            tk.Button(foot, text=txt, bg="#333", fg="#eee", relief="flat", activebackground="#555",
                      command=lambda c=code, s=serial: self.app.bg(self._key, s, c)).pack(side="left", padx=2, pady=3)
        tk.Button(foot, text="scrcpy", bg="#2a6", fg="#fff", relief="flat", activebackground="#3b7",
                  command=lambda s=serial, n=name: self._scrcpy(s, n)).pack(side="right", padx=4, pady=3)
        tk.Button(foot, text="Activar", bg="#333", fg="#eee", relief="flat", activebackground="#555",
                  command=lambda s=serial: self._activate(s)).pack(side="right", padx=4, pady=3)
        tile = {"frame": f, "canvas": canvas, "photo": None, "status": status, "bat": bat, "ph": ph,
                "press": None, "img_w": 1, "img_h": 1, "scale": 1, "ticks": 0, "name": name, "via": via}
        canvas.bind("<ButtonPress-1>", lambda e, s=serial: self._press(s, e))
        canvas.bind("<ButtonRelease-1>", lambda e, s=serial: self._release(s, e))
        canvas.bind("<Double-1>", lambda e, s=serial: self._toggle_focus(s))
        self.tiles[serial] = tile

    def _relayout(self):
        if not self.tiles:
            return
        for t in self.tiles.values():
            t["frame"].grid_forget()
        for i in range(12):
            self.grid_frame.columnconfigure(i, weight=0)
            self.grid_frame.rowconfigure(i, weight=0)
        if self.focus_serial in self.tiles:
            self.tiles[self.focus_serial]["frame"].grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
            self.grid_frame.columnconfigure(0, weight=1)
            self.grid_frame.rowconfigure(0, weight=1)
            return
        n = len(self.tiles)
        gw = max(self.grid_frame.winfo_width(), 400)
        gh = max(self.grid_frame.winfo_height(), 300)
        ratio = 2.1  # alto/ancho aproximado de un teléfono con cabecera y pie
        best = None
        for cols in range(1, n + 1):
            rows = -(-n // cols)
            w = min(gw / cols, (gh / rows) / ratio)
            if best is None or w > best[0]:
                best = (w, cols, rows)
        _, cols, rows = best
        for i, t in enumerate(self.tiles.values()):
            t["frame"].grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)
        for c in range(cols):
            self.grid_frame.columnconfigure(c, weight=1)
        for r in range(rows):
            self.grid_frame.rowconfigure(r, weight=1)

    def _toggle_focus(self, serial):
        self.focus_serial = None if self.focus_serial == serial else serial
        self._relayout()

    # --- captura en bucle (un hilo por teléfono)
    def _loop(self, serial, gen=0):
        while self.alive and self.gen == gen and serial in self.tiles:
            if not self.paused.get():
                t = self.tiles.get(serial)
                try:
                    png = self.app.run_adb("exec-out", "screencap", "-p", binary=True, serial=serial, timeout=15)
                    if png.startswith(b"\x89PNG"):
                        self.after(0, self._show, serial, png)
                    if t:
                        t["ticks"] += 1
                        if t["ticks"] % 30 == 1:
                            out = self.app.shell("dumpsys", "battery", serial=serial, timeout=10)
                            m = re.search(r"level: (\d+)", out)
                            if m:
                                self.after(0, lambda t=t, v=m.group(1): t["bat"].configure(text=f"Bateria {v}%"))
                except Exception:
                    if t:
                        self.after(0, lambda t=t: t["status"].configure(text="● sin respuesta", fg="#e55"))
            try:
                delay = max(0.3, float(self.interval.get()))
            except ValueError:
                delay = 1.0
            time.sleep(delay)

    def _show(self, serial, png):
        t = self.tiles.get(serial)
        if not t or not self.alive:
            return
        try:
            img = tk.PhotoImage(data=png)
        except tk.TclError:
            return
        cw = max(t["canvas"].winfo_width(), 100)
        ch = max(t["canvas"].winfo_height(), 100)
        factor = max(1, -(-img.width() // cw), -(-img.height() // ch))
        small = img.subsample(factor, factor)
        t["photo"], t["scale"], t["img_w"], t["img_h"] = small, factor, img.width(), img.height()
        c = t["canvas"]
        c.delete("all")
        c.create_image(cw // 2, ch // 2, anchor="center", image=small)
        t["origin"] = ((cw - small.width()) // 2, (ch - small.height()) // 2)
        if self.app.is_restricted(t["ph"]):
            t["status"].configure(text=f"● {t['via']} · RESTRINGIDO por WhatsApp", fg="#e55")
        else:
            t["status"].configure(text=f"● {t['via']}", fg="#4c4")

    # --- interacción
    def _to_phone(self, t, e):
        ox, oy = t.get("origin", (0, 0))
        x = int((e.x - ox) * t["scale"])
        y = int((e.y - oy) * t["scale"])
        return max(0, min(x, t["img_w"] - 1)), max(0, min(y, t["img_h"] - 1))

    def _press(self, serial, e):
        t = self.tiles[serial]
        if t["photo"]:
            t["press"] = self._to_phone(t, e)

    def _release(self, serial, e):
        t = self.tiles[serial]
        if not t["photo"] or not t["press"]:
            return
        x1, y1 = t["press"]
        x2, y2 = self._to_phone(t, e)
        t["press"] = None
        if abs(x2 - x1) < 15 and abs(y2 - y1) < 15:
            self.app.bg(lambda: self.app.shell("input", "tap", str(x1), str(y1), serial=serial))
        else:
            self.app.bg(lambda: self.app.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), "250", serial=serial))

    def _key(self, serial, code):
        self.app.shell("input", "keyevent", code, serial=serial)

    def _scrcpy(self, serial, name):
        if not self.app.scrcpy:
            messagebox.showinfo("scrcpy", "scrcpy no está instalado.", parent=self)
            return
        subprocess.Popen(self.app.scrcpy_cmd(serial, name), creationflags=NO_WINDOW)

    def _activate(self, serial):
        self.app.serial = serial
        self.app.fill_tree()
        self.app.log(f"Teléfono activo: {self.app.active_name()}")

    def close(self):
        self.alive = False
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
