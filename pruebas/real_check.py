# Valida en un teléfono REAL, con el código corregido, los pasos 1-5 del envío (hasta ABRIR el chat). NO escribe ni envía nada.
# Uso (desde la carpeta del proyecto): python pruebas/real_check.py "moto g77 #3" 51900000000
import sys, os, json, re, tempfile, threading, shutil, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SP = os.path.dirname(os.path.abspath(__file__))
name, num = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(SP)                      # carpeta del proyecto
cfg = json.load(open(os.path.join(ROOT, "config_telefono.json"), encoding="utf-8"))
ph = next(p for p in cfg["phones"] if p["name"] == name)
serial = f"{ph['ip']}:5555"
tmp = tempfile.mkdtemp(prefix="realchk_")
os.environ["CONTROL_TELEFONO_CONFIG"] = os.path.join(tmp, "config_telefono.json")   # nada de esto toca la config real
os.environ["CONTROL_TELEFONO_SIN_BD"] = "1"
sys.modules["db_numeros"] = None
sys.path.insert(0, ROOT)
import control_telefono as ct

app = object.__new__(ct.App)                  # sin ventana: solo los métodos de ADB/lectura de pantalla
app.adb = shutil.which("adb")
app.serial = None
app.sizes = {}
app._phone_locks, app._locks_guard = {}, threading.Lock()
app._dump_sem = threading.BoundedSemaphore(2)
app.log = lambda m: print("   LOG:", m)
_orig_hdump = ct.App._hdump
def _dbg_hdump(self, serial_):
    t = time.time()
    x = _orig_hdump(self, serial_)
    foco = re.search(r"mCurrentFocus=Window\{[^}]*\s(\S+)\}", self.shell("dumpsys", "window", serial=serial_, timeout=15) or "")
    print(f"      [lectura {time.time() - t:4.1f}s  {len(x):6d} bytes  entry={':id/entry' in x}  fab={':id/fab' in x}  foco={(foco.group(1) if foco else '?').split('/')[-1]}]")
    return x
if os.environ.get("TRAZA"):
    app._hdump = _dbg_hdump.__get__(app)
PKG = "com.whatsapp.w4b"
local = num[2:] if num.startswith("51") else num

real = (app.shell("getprop", "ro.serialno", serial=serial, timeout=10) or "").strip()
assert real == ph["hw"], f"esa IP no es {name}: contesta {real}"
print(f"=== {name} ({serial}, serie {real}) — número de prueba {num}")
t0 = time.time()
app.wake_unlock(serial)
xml = app._wa_list_ready(serial, pkg=PKG)
fab = app.ui_find(xml, "fab") if xml else None
print("1) listado de chats:", "OK" if fab else "NO se llegó")
if not fab:
    sys.exit(1)
found = app._open_picker_search(serial, fab)
print("2) buscador:", (f"OK, diseño {'NUEVO (cuadro de búsqueda)' if found[0] == 'cuadro' else 'antiguo (lupa)'}" if found else "NO se abrió"))
if not found:
    sys.exit(1)
app._tap(serial, found[1])
time.sleep(1.2)
for d in local:
    app.shell("input", "text", d, serial=serial)
    time.sleep(0.07)
time.sleep(2.0)
target = None
for _ in range(5):
    x = app._hdump(serial)
    cb = app.ui_find(x, "chat") if x else None
    if cb:
        target = ("botón Chatear", cb)
        break
    row = app._row_matching(x, local)
    if row:
        target = ("fila con el número", row)
        break
    time.sleep(1.0)
if not target:
    row = app._find_result_row(serial)
    target = ("fila de contacto guardado", row) if row else None
print("3-4) resultado de la búsqueda:", target[0] if target else "NADA (¿número sin WhatsApp?)")
print("     cuadro de búsqueda -> la app lee:", repr(app._search_box_digits(x)), "| tecleado:", repr(local), "| coincide:", app._search_box_digits(x) == local)
ok = False
if target:
    try:
        if found[0] == "cuadro":
            entry, x = app._open_chat_new_picker(serial, target[1], local)     # un toque + 'Enviar mensaje'
        else:
            entry = app._tap_until(serial, target[1], "entry", find_id=("chat" if target[0] == "botón Chatear" else None))
            x = app._hdump(serial) if entry else ""
        ok = entry is not None
    except RuntimeError as e:
        print("   ABORTÓ con seguridad:", e)
        x = ""
    print("5) chat abierto (cuadro de mensaje visible):", "SÍ" if ok else "NO")
    if ok:
        tit = re.search(r'<node[^>]*resource-id="[^"]*:id/conversation_contact_name"[^>]*>', x or "")
        tt = re.search(r'text="([^"]*)"', tit.group(0)).group(1) if tit else "(no leído)"
        print("   título del chat:", tt, "| problema detectado:", app._chat_title_problem(x, local, found[0] == "cuadro"))
        ent = app.ui_find(x, "entry")
        print("   cuadro de mensaje vacío (no se escribió nada):", ent["text"] in ("", "Mensaje", "Message"), "->", repr(ent["text"]))
# volver al listado SIN escribir nada
x = app._wa_list_ready(serial, pkg=PKG)
print("teléfono de vuelta en el listado:", bool(x and app.ui_find(x, "fab")), f"| tardó {time.time() - t0:.0f} s | NO se escribió ni envió nada")
sys.exit(0 if ok else 1)
