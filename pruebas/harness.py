# Simulador: ADB falso, sin BD, sin subprocess real, con el mainloop de Tk corriendo y el tiempo acelerado x10.
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import os, json, tempfile, threading, subprocess, re, time as _time, csv, glob

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # carpeta del proyecto (donde está control_telefono.py)
K = 10                       # aceleración del tiempo dentro de control_telefono
LAUNCHER = "com.motorola.launcher3/com.android.launcher3.Launcher"
WA = "com.whatsapp.w4b"


class FastTime:
    def __init__(self, k):
        self.k, self.t0 = k, _time.time()

    def time(self):
        return self.t0 + (_time.time() - self.t0) * self.k

    def sleep(self, x):
        _time.sleep(max(0.0, x) / self.k)

    def __getattr__(self, n):
        return getattr(_time, n)


class FakeADB:
    INTRUSIVE = ("input", "monkey", "am", "ime", "uiautomator", "tcpip")

    def __init__(self, n=9):
        self.lock = threading.Lock()
        self.calls = []                       # (t_real, serial, args)
        self.ph = {}
        for i in range(1, n + 1):
            self.ph[f"10.255.0.{i}:5555"] = dict(
                i=i, hw=f"FAKE{i:02d}", listed=True, state="device", focus=WA + "/.Home", locked=False, pin=False,
                has_fab=True, has_search=True, fix_on_force=False, picker=False,
                ime="com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME",
                connect="ok", battery=80, powered=True, dead=False, usb=None, dialog_text="")
        self.usb = {}                         # serial usb -> clave ip:5555 del teléfono

    def dev(self, serial):
        if serial in self.ph:
            return self.ph[serial]
        if serial in self.usb:
            return self.ph[self.usb[serial]]
        return None

    def intrusive_to(self, serial, since=0.0):
        return [c for c in self.calls if c[1] == serial and c[0] >= since and
                any(a in self.INTRUSIVE for a in c[2][:2]) and not (c[2][:2] == ("shell", "input") and False)]

    def xml(self, d):
        if WA not in d["focus"]:
            body = '<node text="Play Store" resource-id="com.motorola.launcher3:id/icon" bounds="[0,0][100,100]" />'
        elif d["picker"]:
            if d["has_search"]:
                body = '<node text="" resource-id="com.whatsapp.w4b:id/menuitem_search" bounds="[900,80][1000,180]" />'
            else:
                body = ('<node text="%s" resource-id="android:id/message" bounds="[100,900][980,1100]" />'
                        '<node text="Ahora no" resource-id="android:id/button2" bounds="[100,1200][500,1300]" />'
                        % (d["dialog_text"] or "Permitir que WhatsApp acceda a tus contactos"))
        elif d["has_fab"]:
            body = '<node text="" resource-id="com.whatsapp.w4b:id/fab" bounds="[860,2000][1020,2160]" />'
        else:
            body = '<node text="Actualiza WhatsApp" resource-id="com.whatsapp.w4b:id/title" bounds="[100,900][980,1000]" />'
        return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">%s</hierarchy>' % body).encode("utf-8")

    def run(self, app, *args, timeout=30, binary=False, serial=None):
        target = serial or app.serial
        with self.lock:
            self.calls.append((_time.time(), target if args and args[0] not in ("devices", "connect", "disconnect") else
                               (args[1] if len(args) > 1 else None), tuple(args)))
        a = args
        if a[0] == "devices":
            rows = [f"{s}\t{d['state']}" for s, d in self.ph.items() if d["listed"]]
            rows += [f"{u}\tdevice" for u, s in self.usb.items() if self.ph[s].get("usb_plugged")]
            return "List of devices attached\n" + "\n".join(rows)
        if a[0] == "connect":
            d = self.ph.get(a[1])
            if d is None:
                return f"cannot connect to {a[1]}: host unreachable"
            if d["connect"] == "timeout":
                raise subprocess.TimeoutExpired(["adb", "connect", a[1]], timeout)
            if d["connect"] == "refused":
                return f"cannot connect to {a[1]}: No se puede establecer una conexión ya que el equipo de destino denegó expresamente dicha conexión. (10061)"
            d["listed"], d["state"] = True, "device"
            return f"connected to {a[1]}"
        if a[0] == "disconnect":
            d = self.ph.get(a[1])
            if d:
                d["listed"] = False
            return f"disconnected {a[1]}"
        d = self.dev(target)
        if d is None:
            return "error: device not found"
        if d["dead"]:
            raise subprocess.TimeoutExpired(["adb"] + list(a), timeout)
        if a[0] == "get-state":
            return "device" if d["state"] == "device" else "offline"
        if a[0] == "tcpip":
            d["connect"] = "ok"
            return "restarting in TCP mode port: 5555"
        if a[0] == "install":
            return "Success"
        if a[0] == "exec-out":
            out = self.xml(d)
            return out if binary else out.decode()
        if a[0] != "shell":
            return ""
        sh = a[1:]
        if sh[:2] == ("getprop", "ro.serialno"):
            return d["hw"]
        if sh[:2] == ("getprop", "ro.product.model"):
            return "fake"
        if sh[0] == "ip" or sh[:1] == ("cat",):
            return f"36: wlan0: <UP>\n    link/ether aa:bb:cc:dd:ee:{d['i']:02x} brd ff\n    inet 10.255.0.{d['i']}/24 brd 10.255.0.255 scope global wlan0"
        if sh[:2] == ("wm", "size"):
            return "Physical size: 1080x2400"
        if sh[:2] == ("dumpsys", "window"):
            return ("mDreamingLockscreen=true\n" if d["locked"] else "") + "mCurrentFocus=Window{abc123 u0 %s}" % (
                "NotificationShade" if d["locked"] else d["focus"])
        if sh[:2] == ("dumpsys", "battery"):
            return f"  AC powered: {'true' if d['powered'] else 'false'}\n  level: {d['battery']}\n"
        if sh[:4] == ("settings", "get", "secure", "default_input_method"):
            return d["ime"]
        if sh[:3] == ("pm", "list", "packages"):
            return "package:com.android.adbkeyboard"
        if sh[:2] == ("ime", "set"):
            d["ime"] = sh[2]
            return ""
        if sh[0] == "monkey":
            d["focus"], d["picker"] = sh[2] + "/.Home", False
            return ""
        if sh[:2] == ("am", "force-stop"):
            d["focus"], d["picker"] = LAUNCHER, False
            if d["fix_on_force"]:
                d["has_fab"] = d["has_search"] = True
            return ""
        if sh[0] == "input":
            if sh[1] == "keyevent" and sh[2] == "KEYCODE_BACK":
                d["picker"] = False
            elif sh[1] == "tap" and WA in d["focus"] and not d["picker"] and d["has_fab"] and int(sh[3]) > 1900:
                d["picker"] = True
            elif sh[1] == "swipe" and d["locked"] and not d["pin"]:
                d["locked"] = False
            return ""
        return ""


def boot(cfg_extra=None, n=9, assigned_per_phone=12, assign=None):
    """Prepara el entorno aislado e importa control_telefono. Devuelve (ct, fake, tmp, dialogs, answers)."""
    tmp = tempfile.mkdtemp(prefix="sim_ct_")
    os.environ["CONTROL_TELEFONO_CONFIG"] = os.path.join(tmp, "config_telefono.json")
    os.environ["CONTROL_TELEFONO_SIN_BD"] = "1"
    sys.modules["db_numeros"] = None
    phones = [{"name": f"fake #{i}", "model": "fake", "usb": None, "ip": f"10.255.0.{i}", "hw": f"FAKE{i:02d}",
               "mac": f"aa:bb:cc:dd:ee:{i:02x}"} for i in range(1, n + 1)]
    cfg = {"phones": phones, "wa_human": True, "wa_split": True, "wa_auto_join": False, "wa_reassign": False,
           "auto_scan": False, "wa_cool": "0", "wa_cool_max": "0", "wa_stall_secs": 60, "health_secs": 0,
           "fix_0918": True, "wa_preflight": False, "wa_ultima_oportunidad": False, "wa_vary": False,
           "wa_delay": "0", "wa_delay_max": "0", "wa_type_min": "0.05", "wa_type_max": "0.15"}
    cfg.update(cfg_extra or {})
    json.dump(cfg, open(os.environ["CONTROL_TELEFONO_CONFIG"], "w", encoding="utf-8"))
    if assign is None:
        assign = {f"519000{p:02d}{j:03d}": {"key": f"FAKE{p:02d}", "name": f"fake #{p}", "at": "x"}
                  for p in range(1, n + 1) for j in range(assigned_per_phone)}
    json.dump(assign, open(os.path.join(tmp, "asignaciones.json"), "w", encoding="utf-8"))
    sys.path.insert(0, WORK)
    import control_telefono as ct
    assert ct.db_numeros is None and os.path.dirname(ct.CONFIG_FILE) == tmp, "aislamiento roto"
    hits = []

    def _forbidden(*a, **k):
        hits.append(a[:1])
        raise AssertionError("subprocess real prohibido en simulación")
    ct.subprocess.run = _forbidden
    ct.subprocess.Popen = _forbidden
    ct.time = FastTime(K)
    fake = FakeADB(n)
    ct.App.run_adb = lambda self, *a, **k: fake.run(self, *a, **k)
    ct.App.scan_network = lambda self, auto=False: False
    ct._orig_probe_port = ct.probe_port
    probes = {}
    ct.probe_port = lambda ip, **k: probes.get(ip, "mudo")
    dialogs, answers = [], {"askyesno": True, "askyesnocancel": False}
    for fn in ("showwarning", "showinfo", "askyesno", "askyesnocancel"):
        setattr(ct.messagebox, fn, lambda *a, _f=fn, **k: (dialogs.append((_f, a)), answers.get(_f))[1])
    return ct, fake, tmp, dialogs, answers, hits, probes


class Runner:
    """Crea la App, recoge excepciones y logs, y ofrece utilidades para encadenar pasos sobre el mainloop."""
    def __init__(self, ct, fake, tmp, hits):
        self.ct, self.fake, self.tmp, self.hits = ct, fake, tmp, hits
        self.errors, self.logs, self.results = [], [], []
        threading.excepthook = lambda a: self.errors.append(f"hilo: {a.exc_type.__name__}: {a.exc_value}")
        self.app = ct.App()
        self.app.adb = "FAKE"
        self.app.report_callback_exception = lambda *a: self.errors.append(f"tk: {a[0].__name__}: {a[1]}")
        orig_log = self.app.log
        self.app.log = lambda msg: (self.logs.append(msg), orig_log(msg))[1]
        self.busy = {}                        # serial -> [(t0,t1)]
        self.script = {}                      # serial -> callable(num) -> None | Exception
        self.send_secs = 0.5 * K
        self.sent = []                        # (serial, num)
        self.app._send_typed = self.fake_send
        self.app._send_via = self.fake_send
        self.app.withdraw()
        self.t_start = _time.time()

    def fake_send(self, ph, serial, num, msg, delay, cooldown=None, name=None):
        t0 = _time.time()
        self.app._step(name or serial, num, "sim")
        self.ct.time.sleep(self.send_secs)
        self.busy.setdefault(serial, []).append((t0, _time.time()))
        fn = self.script.get(serial)
        err = fn(num) if fn else None
        if err:
            raise err
        self.sent.append((serial, num))
        return "enviado"

    def check(self, name, cond, detail=""):
        self.results.append((name, bool(cond), detail))

    def logs_with(self, text):
        return [l for l in self.logs if text in l]

    def wait(self, cond, then, timeout=60.0, every=150, label=""):
        t0 = _time.time()

        def poll():
            try:
                ok = cond()
            except Exception as e:
                ok = False
                self.errors.append(f"cond {label}: {e}")
            if ok:
                then()
            elif _time.time() - t0 > timeout:
                self.check(f"TIMEOUT esperando: {label}", False)
                self.finish()
            else:
                self.app.after(every, poll)
        self.app.after(every, poll)

    def send_done(self):
        return not self.app.bulk_active and self.app._bulk is None

    def finish(self):
        self.app.after(300, self.app.destroy)

    def report(self, title):
        self.check("sin subprocess real", self.hits == [], str(self.hits))
        self.check("sin excepciones en hilos/Tk", not self.errors, " | ".join(self.errors[:4]))
        bad = [r for r in self.results if not r[1]]
        print(f"\n===== {title}: {'OK' if not bad else 'FALLA'} ({len(self.results) - len(bad)}/{len(self.results)}) =====")
        for name, ok, detail in self.results:
            if not ok or os.environ.get("SIM_VERBOSE"):
                print(("  ✔ " if ok else "  ✖ ") + name + (f"  -> {detail}" if detail else ""))
        if bad and os.environ.get("SIM_LOGS", "1") == "1":
            print("  --- últimas líneas de log:")
            for l in self.logs[-25:]:
                print("     ", l[:200])
        return not bad

    def csv_rows(self):
        files = sorted(glob.glob(os.path.join(self.tmp, "envios_*.csv")))
        if not files:
            return []
        return list(csv.reader(open(files[-1], encoding="utf-8-sig"), delimiter=";"))[1:]

    def click_usar(self, idx, column="#1"):
        """Clic sintético en la columna ☑ de la fila de cfg['phones'][idx] (sin necesitar la ventana visible)."""
        tree = self.app.tree
        saved = (tree.identify_region, tree.identify_column, tree.identify_row)
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: column
        tree.identify_row = lambda y: str(idx)
        try:
            ev = type("E", (), {})()
            ev.x, ev.y = 5, 5
            return self.app._on_tree_click(ev)
        finally:
            tree.identify_region, tree.identify_column, tree.identify_row = saved

    def prime(self, then, msg="hola sim"):
        """Espera el primer refresh, pone mensaje y lista vacía (usa asignaciones), y sigue."""
        self.app.wa_msg.delete("1.0", "end")
        self.app.wa_msg.insert("1.0", msg)
        self.app.wa_list.delete("1.0", "end")
        self.wait(lambda: len(self.app.tree.get_children()) >= 1 and bool(self.app.connected), then, label="primer refresh")
