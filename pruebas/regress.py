# Regresiones de la revisión adversarial. Uso: python regress.py R1 | python regress.py ALL
import sys, os, subprocess as _sp, time as _time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run
from run import S, harness_K


def R1(h):
    """Duplicados: el teléfono falla JUSTO tras tocar Enviar (el mensaje salió). Jamás se reenvía solo."""
    r, app, ct = h["r"], h["r"].app, h["ct"]
    import harness
    state = {"done": False}

    def after_send_timeout(n):
        if n == "51900004003" and not state["done"]:
            state["done"] = True
            r.sent.append((S(4), n))                       # el mensaje SÍ salió del teléfono
            e = ct.SendUnconfirmed("fallo tras tocar Enviar (timeout)")
            e.__cause__ = harness.subprocess.TimeoutExpired(["adb"], 30)
            return e
        return None
    r.script[S(4)] = after_send_timeout

    def go():
        app.cfg["wa_ultima_oportunidad"] = True
        app.wa_start()

        def end():
            from collections import Counter
            dup = {n: c for n, c in Counter(n for _s, n in r.sent).items() if c > 1}
            r.check("ningún número enviado dos veces", not dup, str(dup))
            rows = [x for x in r.csv_rows() if x[0] == "51900004003"]
            r.check("una fila SIN CONFIRMAR en el CSV", len(rows) == 1 and "SIN CONFIRMAR" in rows[0][2], str(rows))
            r.check("no entra en 'fallidos' (no se reintenta a ciegas)", "51900004003" not in (app.wa_failed or []), "")
            r.check("no queda como pendiente", "51900004003" not in (app.wa_pending or []), "")
            r.check("aviso final de SIN CONFIRMAR", bool(r.logs_with("SIN CONFIRMAR (el teléfono falló")), "")
            n4 = sum(1 for s_, _n in r.sent if s_ == S(4))
            r.check("#4 volvió (última oportunidad) y terminó el resto", n4 == 12, str(n4))
            r.finish()
        r.wait(r.send_done, end, timeout=150, label="fin R1")
    r.prime(go)


def R2(h):
    """'Solo el activo' + ⚡ a mitad de envío: los demás teléfonos NO entran."""
    r, app = h["r"], h["r"].app
    r.send_secs = 1.5 * harness_K()

    def go():
        app.wa_split.set(False)
        act = app.serial
        app.wa_start()

        def press():
            app.activate_all_clicked()

            def end():
                serials = {s_ for s_, _n in r.sent}
                r.check("solo envió el teléfono activo", serials == {act}, str(serials))
                pend = len([x for x in r.csv_rows() if x[2].startswith("pendiente")])
                r.check("12 enviados, 96 pendientes", len(r.sent) == 12 and pend == 96, f"{len(r.sent)} / {pend}")
                r.check("nadie 'SE SUMÓ'", not r.logs_with("SE SUMÓ AL ENVÍO"), str(r.logs_with("SE SUMÓ")[:2]))
                r.finish()
            r.wait(r.send_done, end, timeout=150, label="fin R2")
        r.wait(lambda: len(r.sent) >= 2, press, timeout=60, label="arranque R2")
    r.prime(go)


def R3(h):
    """Detener durante la comprobación previa (▶ Enviar -> SÍ -> ⚡): el envío NO arranca al terminar ⚡."""
    r, app = h["r"], h["r"].app

    def go():
        app.cfg["wa_preflight"] = True
        h["answers"]["askyesnocancel"] = True
        app.wa_start()
        r.check("⚡ en marcha", bool(r.logs_with("ACTIVAR TODOS: revisando")) or app._activating.locked(), "")
        app.wa_stop()
        r.check("log de cancelación", bool(r.logs_with("Envío CANCELADO")), str(r.logs[-3:]))

        def end():
            r.check("no se envió nada", not r.sent and not app.bulk_active, f"{len(r.sent)}")
            r.finish()
        r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")) and not app._activating.locked(),
               lambda: app.after(1500, end), timeout=120, label="fin ⚡")
    r.prime(go)


def R4(h):
    """'Abrir y enviar' reserva el teléfono: ⚡ no lo toca mientras teclea."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    r.send_secs = 3.0 * harness_K()

    def go():
        app.wa_num.set("51911112222")
        serial = app.serial
        t0 = _time.time()
        app.bg(app.wa_send_one)

        def during():
            app.activate_all_clicked()

            def end():
                iv = r.busy.get(serial, [])
                viol = [c[2] for c in fake.intrusive_to(serial, t0) if any(a <= c[0] <= b for a, b in iv)
                        and c[2][:2] != ("shell", "ime")]
                r.check("hubo un envío individual", len(iv) == 1, str(iv))
                r.check("⚡ no mandó nada intrusivo al que tecleaba", not viol, str(viol[:3]))
                r.check("⚡ lo reportó como ocupado", any("ocupado" in l for l in r.logs), "")
                r.check("reserva liberada al final", not app._act_busy, str(app._act_busy))
                r.finish()
            r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")) and bool(r.logs_with("mensaje enviado a")), end,
                   timeout=120, label="fin R4")
        r.wait(lambda: bool(app._act_busy), during, timeout=30, label="envío individual en marcha")
    r.prime(go)


def R5(h):
    """Detener pulsado mientras el envío ARRANCA: se cancela antes de enviar nada."""
    r, app = h["r"], h["r"].app

    def go():
        app.wa_start()
        app.wa_stop()

        def end():
            r.check("cancelado antes de empezar", bool(r.logs_with("cancelado antes de empezar")), str(r.logs[-4:]))
            r.check("0 enviados", not r.sent, str(len(r.sent)))
            r.finish()
        r.wait(lambda: not app.bulk_active, lambda: app.after(800, end), timeout=60, label="fin R5")
    r.prime(go)


def R6(h):
    """El archivo de pendientes va al día: si la app se cierra a mitad, 'Reanudar' no reenvía lo ya enviado."""
    r, app, ct = h["r"], h["r"].app, h["ct"]
    r.send_secs = 1.0 * harness_K()

    def go():
        app.wa_start()

        def mid():
            pend = set(ct.read_numbers(ct.PENDING_FILE))
            enviados = {n for _s, n in r.sent}
            r.check("pendientes en disco excluyen lo ya enviado (tolerancia: 1 en vuelo por teléfono)",
                    len(pend & enviados) <= 9, f"solapados={len(pend & enviados)}")
            r.check("pendientes en disco < total", 0 < len(pend) < 108, str(len(pend)))
            app.wa_stop()
            r.wait(r.send_done, r.finish, timeout=90, label="fin R6")
        r.wait(lambda: len(r.sent) >= 30, mid, timeout=90, label="30 enviados")
    r.prime(go)


def R7(h):
    """Identidad: a mitad de envío aparece OTRO aparato en la IP de #4. No se suma ni envía con otra cuenta."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    r.send_secs = 1.5 * harness_K()

    def go():
        fake.ph[S(4)]["listed"] = False
        app.wa_auto_join.set(True)
        app.wa_start()

        def intruder():
            fake.ph[S(4)].update(listed=True, hw="FAKE77XX")

            def end():
                r.check("nadie envió desde la IP de #4", not [1 for s_, _n in r.sent if s_ == S(4)], "")
                r.check("aviso de OTRO aparato", bool(r.logs_with("contesta OTRO aparato")), str(r.logs[-4:]))
                pend = [x for x in r.csv_rows() if x[2].startswith("pendiente")]
                r.check("los 12 de #4 quedan pendientes", len(pend) == 12, str(len(pend)))
                r.finish()
            r.wait(r.send_done, end, timeout=200, label="fin R7")
        r.wait(lambda: len(r.sent) >= 8, intruder, timeout=60, label="arranque R7")
    r.prime(go)


REG = {k: v for k, v in list(globals().items()) if k[0] == "R" and k[1:].isdigit() and callable(v)}
run.SCEN.update(REG)

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    if which != "ALL":
        sys.exit(0 if run.run_one(which) else 1)
    fails = []
    for n in sorted(REG, key=lambda k: int(k[1:])):
        p = _sp.run([sys.executable, os.path.abspath(__file__), n], capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        print(((p.stdout or "") + (p.stderr[-1500:] if p.returncode and p.stderr else "")).strip())
        if p.returncode:
            fails.append(n)
    print("\n================ REGRESIONES:", "TODO OK" if not fails else "FALLAN " + ", ".join(fails), "================")
