# Arranque con una COPIA de la configuración real (ADB falso, sin BD): ¿se construye la ventana y pinta sin errores?
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import os, json, shutil, tempfile, threading, time as _time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PROD = os.path.dirname(HERE)      # carpeta del proyecto
tmp = tempfile.mkdtemp(prefix="smoke_ct_")
for f in ("config_telefono.json", "asignaciones.json", "telefonos_conocidos.json"):
    if os.path.exists(os.path.join(PROD, f)):
        shutil.copy(os.path.join(PROD, f), tmp)
os.environ["CONTROL_TELEFONO_CONFIG"] = os.path.join(tmp, "config_telefono.json")
os.environ["CONTROL_TELEFONO_SIN_BD"] = "1"
sys.modules["db_numeros"] = None
import harness
sys.path.insert(0, harness.WORK)
import control_telefono as ct
assert os.path.dirname(ct.CONFIG_FILE) == tmp
hits = []
ct.subprocess.run = lambda *a, **k: (hits.append(a[:1]), (_ for _ in ()).throw(AssertionError("subprocess prohibido")))[1]
ct.subprocess.Popen = ct.subprocess.run
cfg = json.load(open(os.environ["CONTROL_TELEFONO_CONFIG"], encoding="utf-8"))
phones = cfg.get("phones", [])
print("config real:", len(phones), "teléfonos | claves:", sorted(k for k in cfg if k not in ("phones", "known", "wa_saludos", "wa_cierres")))


class Fake(harness.FakeADB):
    def __init__(self):
        super().__init__(0)
        for i, p in enumerate(phones, 1):
            if p.get("ip"):
                self.ph[f"{p['ip']}:5555"] = dict(self_default(i), hw=p.get("hw") or f"X{i}")


def self_default(i):
    return dict(i=i, listed=True, state="device", focus=harness.WA + "/.Home", locked=False, pin=False, has_fab=True,
                has_search=True, fix_on_force=False, picker=False, ime="com.google.android.inputmethod.latin/x",
                connect="ok", battery=77, powered=True, dead=False, usb=None, dialog_text="")


fake = Fake()
ct.App.run_adb = lambda self, *a, **k: fake.run(self, *a, **k)
ct.App.scan_network = lambda self, auto=False: False
ct.probe_port = lambda ip, **k: "mudo"
errors = []
threading.excepthook = lambda a: errors.append(f"hilo: {a.exc_type.__name__}: {a.exc_value}")
app = ct.App()
app.adb = "FAKE"
app.report_callback_exception = lambda *a: errors.append(f"tk: {a[0].__name__}: {a[1]}")
logs = []
orig = app.log
app.log = lambda m: (logs.append(m), orig(m))[1]
app.withdraw()


def check():
    app._tick()
    app._health_poll()
    app._tick()
    rows = [app.tree.item(i, "values") for i in app.tree.get_children()]
    print("filas en la tabla:", len(rows))
    for r in rows:
        print("   ", r[0], r[1], "|", r[2], "| contactos", r[6])
    print("tablero:", app.bn_title.cget("text"), "|", app.bn_rate.cget("text"))
    print("botón enviar:", app.btn_send.cget("text"), "| marcas:", app.marks_var.get())
    print("columnas visibles:", app.tree.cget("displaycolumns"))
    newcfg = json.load(open(os.environ["CONTROL_TELEFONO_CONFIG"], encoding="utf-8"))
    print("migración: auto_scan =", newcfg.get("auto_scan"), "| wa_auto_join =", newcfg.get("wa_auto_join"), "| fix_0918 =", newcfg.get("fix_0918"))
    print("ERRORES:", errors or "ninguno", "| subprocess real:", hits or "ninguno")
    app.destroy()


app.after(2500, check)
app.mainloop()
