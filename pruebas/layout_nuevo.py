# _send_typed REAL contra un ADB falso con los DOS diseños de 'Nuevo chat' de WhatsApp.
#   fake #1 = diseño ANTIGUO (lupa + botón 'Chatear' -> chat)
#   fake #2 = diseño NUEVO, modelado según lo leído en el moto g77 #3 real: cuadro de búsqueda visible; el resultado es una fila
#             con el número; tocarla añade la etiqueta 'Para:' y muestra 'Enviar mensaje' (extended_fab); la lista se REINICIA y
#             en el mismo sitio queda un contacto frecuente: un segundo toque lo seleccionaría -> 'Crear grupo'.
# Uso: python layout_nuevo.py            (flujo normal)
#      python layout_nuevo.py LENTO      (el botón 'Enviar mensaje' tarda en aparecer: la app NO debe volver a tocar la lista)
import sys, os, base64, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness
WA = harness.WA
NUEVO = "10.255.0.2:5555"
SIN_WA = "900002001"            # este número no tiene WhatsApp (en el teléfono del diseño nuevo)
MODE = sys.argv[1] if len(sys.argv) > 1 else "NORMAL"


def fmt(local):                  # '900002000' -> '+51 900 002 000'
    return f"+51 {local[:3]} {local[3:6]} {local[6:]}"


def main():
    import time as _t
    ct, fake, tmp, dialogs, answers, hits, probes = harness.boot({}, n=2, assigned_per_phone=3)
    r = harness.Runner(ct, fake, tmp, hits)
    app = r.app
    del app._send_typed                       # se ejecuta el _send_typed REAL del archivo parcheado
    assert app._send_typed.__func__ is ct.App._send_typed
    base_run = fake.run
    st, delivered, wrong_taps, groups = {}, [], [], []

    PICK = '<node text="" resource-id="%s:id/contact_picker_layout" class="android.view.ViewGroup" bounds="[0,120][1080,2235]" />' % WA
    BAR = '<node text="" resource-id="%s:id/wds_search_bar" class="android.widget.FrameLayout" bounds="[0,120][1080,257]" />' % WA
    GROUP = '<node text="" resource-id="%s:id/contact_picker_chip_group_layout" class="android.widget.ScrollView" bounds="[39,257][1041,375]" />' % WA
    FREQ = '<node text="Alberto Frecuente" resource-id="%s:id/contactpicker_row_name" class="android.widget.Button" bounds="[176,531][600,584]" />' % WA
    MENU = ('<node text="Nuevo contacto" resource-id="%s:id/contactpicker_row_name" class="android.widget.TextView" bounds="[176,692][484,745]" />'
            '<node text="Ayuda" resource-id="%s:id/contactpicker_row_name" class="android.widget.TextView" bounds="[176,1024][302,1077]" />' % (WA, WA))

    def xml_of(target, d, s):
        nuevo = target == NUEVO
        if s["scr"] is None:
            if not d["picker"]:
                return None                   # listado de chats: lo pinta el arnés (fab)
            if not nuevo:
                body = '<node text="" resource-id="%s:id/menuitem_search" class="android.widget.ImageView" bounds="[900,80][1000,180]" />' % WA
            else:
                body = (PICK + BAR + GROUP + '<node text="Buscar nombre, número o nombre de usuario" resource-id="" '
                        'class="android.widget.EditText" bounds="[156,267][1002,365]" />' + FREQ)
        elif s["scr"] == "search":
            if not nuevo:
                body = '<node text="%s" resource-id="%s:id/search_src_text" class="android.widget.EditText" bounds="[100,80][800,180]" />' % (s["typed"], WA)
                if len(s["typed"]) >= 9:
                    body += '<node text="Chatear" resource-id="%s:id/chat" class="android.widget.Button" bounds="[800,400][1000,500]" />' % WA
            else:
                body = PICK + BAR + GROUP + '<node text="%s" resource-id="" class="android.widget.EditText" bounds="[156,267][354,365]" />' % s["typed"]
                loc = s["typed"][-9:]
                if len(s["typed"]) >= 9 and loc != SIN_WA:
                    body += ('<node text="Usuarios que no están en tus contactos" resource-id="%s:id/header_textview" class="android.widget.TextView" bounds="[39,425][1041,470]" />'
                             '<node text="%s" resource-id="%s:id/contactpicker_row_name" class="android.widget.Button" bounds="[176,531][469,584]" />'
                             % (WA, fmt(loc), WA))
                elif len(s["typed"]) < 9:
                    body += FREQ
                body += MENU
        elif s["scr"] == "chip":              # etiqueta(s) 'Para:' + la lista REINICIADA con el contacto frecuente en el mismo sitio
            chips = "".join('<node text="" resource-id="" class="android.widget.Button" content-desc="%s, No seleccionado" '
                            'bounds="[%d,267][%d,365]" />' % (c, 194 + i * 320, 502 + i * 320) for i, c in enumerate(s["chips"]))
            body = PICK + BAR + GROUP + '<node text="Para:" resource-id="" class="android.widget.TextView" bounds="[78,267][175,365]" />' + chips
            body += '<node text="Para: " resource-id="" class="android.widget.EditText" bounds="[921,267][945,365]" />' + FREQ
            if _t.time() >= s.get("btn_at", 0):
                body += ('<node text="%s" resource-id="%s:id/extended_fab" class="android.widget.Button" content-desc="x" bounds="[643,1255][1041,1392]" />'
                         % ("Enviar mensaje" if len(s["chips"]) == 1 else "Crear grupo", WA))
        else:
            body = ('<node text="%s" resource-id="%s:id/conversation_contact_name" class="android.widget.TextView" bounds="[244,158][547,213]" />'
                    '<node text="%s" resource-id="%s:id/entry" class="android.widget.EditText" bounds="[100,1500][900,1600]" />'
                    '<node text="" resource-id="%s:id/send" class="android.widget.ImageButton" bounds="[920,1500][1060,1600]" />'
                    % (s["title"], WA, s["entry"] or "Mensaje", WA, WA))
        return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">%s</hierarchy>' % body).encode("utf-8")

    def run(app_, *args, timeout=30, binary=False, serial=None):
        target = serial or app_.serial
        d = fake.dev(target) if target else None
        if d is None or not args:
            return base_run(app_, *args, timeout=timeout, binary=binary, serial=serial)
        s = st.setdefault(target, {"scr": None, "typed": "", "entry": "", "chips": [], "title": ""})
        nuevo = target == NUEVO
        a = args
        if a[0] == "exec-out":
            out = xml_of(target, d, s)
            if out is not None:
                return out if binary else out.decode()
        if a[0] == "shell":
            sh = a[1:]
            if sh[:2] == ("input", "tap"):
                x, y = int(sh[2]), int(sh[3])
                if s["scr"] is None and d["picker"]:
                    if (not nuevo and y < 300) or (nuevo and 267 <= y <= 365):
                        s.update(scr="search", typed="")
                    elif nuevo and 520 <= y <= 600:
                        wrong_taps.append((target, "contacto frecuente (sin buscar)"))
                        s.update(scr="chip", chips=["Alberto Frecuente"])
                    return ""
                if s["scr"] == "search":
                    ok = len(s["typed"]) >= 9 and s["typed"][-9:] != SIN_WA
                    if not nuevo and 400 <= y <= 500 and ok:
                        s.update(scr="chat", entry="", title=fmt(s["typed"][-9:]))
                    elif nuevo and 520 <= y <= 600:
                        if ok:
                            s.update(scr="chip", chips=[fmt(s["typed"][-9:])], btn_at=_t.time() + (0.8 if MODE == "LENTO" else 0.0))
                        else:
                            wrong_taps.append((target, "fila que no es el número"))
                            s.update(scr="chip", chips=["Alberto Frecuente"])
                    return ""
                if s["scr"] == "chip":
                    if 520 <= y <= 600:                       # SEGUNDO toque en la lista: selecciona al contacto frecuente
                        wrong_taps.append((target, "SEGUNDO toque en la lista -> Alberto Frecuente"))
                        s["chips"].append("Alberto Frecuente")
                    elif 1255 <= y <= 1392 and x >= 643 and _t.time() >= s.get("btn_at", 0):
                        if len(s["chips"]) == 1:
                            s.update(scr="chat", entry="", title=s["chips"][0])
                        else:
                            groups.append((target, list(s["chips"])))      # ¡se habría creado un grupo!
                    return ""
                if s["scr"] == "chat":
                    if x > 900 and s["entry"]:
                        delivered.append((target, s["title"], s["entry"]))
                        s["entry"] = ""
                    return ""
            if sh[:2] == ("input", "text") and s["scr"] == "search":
                s["typed"] += sh[2]
                return ""
            if sh[:2] == ("am", "broadcast"):
                if "ADB_CLEAR_TEXT" in sh:
                    if s["scr"] == "search":
                        s["typed"] = ""
                    elif s["scr"] == "chat":
                        s["entry"] = ""
                    return ""
                if "ADB_INPUT_B64" in sh and s["scr"] == "chat":
                    s["entry"] += base64.b64decode(sh[-1]).decode("utf-8")
                    return ""
            if sh[:3] == ("input", "keyevent", "KEYCODE_BACK") and s["scr"]:
                s.update(scr=None, typed="", entry="", chips=[], title="")
                d["picker"] = False
                return ""
            if sh[:2] == ("input", "keyevent") and s["scr"]:
                return ""
        return base_run(app_, *args, timeout=timeout, binary=binary, serial=serial)

    fake.run = run

    def go():
        app.activate_all_clicked()            # ⚡ debe dar por BUENOS los dos diseños, sin tocar ninguna fila

        def after_act():
            L = "\n".join(r.logs)
            r.check("⚡: diseño antiguo listo", "[fake #1] ✔ listo" in L, str(r.logs_with("fake #1")[-1:]))
            r.check("⚡: diseño NUEVO listo", "[fake #2] ✔ listo" in L, str(r.logs_with("fake #2")[-1:]))
            r.check("⚡ no tocó ninguna fila de contactos", not wrong_taps, str(wrong_taps))
            app.wa_start()

            def end():
                c = collections.Counter(n for _s, n, _t2 in delivered)
                rows = r.csv_rows()
                print("ENTREGADOS:", [(s_[-6:], n) for s_, n, _t2 in delivered])
                print("CSV:", [(x[0], x[1], x[2][:50]) for x in rows])
                r.check("nadie recibe dos veces", not {n: v for n, v in c.items() if v > 1}, str(c))
                r.check("diseño antiguo envió sus 3", sum(1 for s_, _n, _t2 in delivered if s_ != NUEVO) == 3, "")
                r.check("diseño NUEVO envió los 2 que tienen WhatsApp", sum(1 for s_, _n, _t2 in delivered if s_ == NUEVO) == 2,
                        str([n for s_, n, _t2 in delivered if s_ == NUEVO]))
                r.check("cada mensaje llegó al chat del número correcto",
                        all(n.replace("+51", "").replace(" ", "") in [x[0][2:] for x in rows] for _s, n, _t2 in delivered), "")
                sin = [x for x in rows if x[0].endswith(SIN_WA)]
                r.check("el número sin WhatsApp queda NO enviado", len(sin) == 1 and sin[0][2].startswith("NO enviado"), str(sin))
                r.check("NUNCA un segundo toque en la lista ni un contacto ajeno", not wrong_taps, str(wrong_taps))
                r.check("NUNCA se creó un grupo", not groups, str(groups))
                r.check("el mensaje llegó completo", all(t.strip() == "hola sim" for _s, _n, t in delivered), "")
                r.finish()
            r.wait(r.send_done, end, timeout=500, label="fin envío")
        r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")) and not app._activating.locked(), after_act, timeout=200, label="fin ⚡")
    r.prime(go)
    app.after(700000, lambda: (r.check("TIMEOUT GLOBAL", False), app.destroy()))
    app.mainloop()
    return r.report(f"diseños de 'Nuevo chat' con el tecleo REAL [{MODE}]")


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
