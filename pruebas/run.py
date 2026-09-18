# Escenarios de simulación. Uso: python run.py T1   |   python run.py ALL
import sys, os, subprocess as _sp, time as _time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def S(i):
    return f"10.255.0.{i}:5555"


def per_phone(rows):
    out = {}
    for r in rows:
        d = out.setdefault(r[5] or "-", {"ok": 0, "bad": 0, "pend": 0})
        d["ok" if r[2] == "enviado" else "bad" if r[2].startswith("NO enviado") else "pend"] += 1
    return out


# ---------------------------------------------------------------------------------------------- PUROS
def PURE():
    import harness
    ct, fake, tmp, dialogs, answers, hits, probes = harness.boot()
    res = []
    eta = ct.bulk_eta
    res.append(("eta None sin vivos", eta([], {}, 0, {}) is None))
    res.append(("eta None con <3 muestras", eta(["a"], {"a": 5}, 0, {"a": {"durs": [10, 10], "cur": None}}) is None))
    per = {"a": {"durs": [100] * 5, "cur": None}, "b": {"durs": [10] * 5, "cur": None}}
    res.append(("eta = cola propia del lento", abs(eta(["a", "b"], {"a": 10, "b": 0}, 0, per) - 1000) < 1e-6))
    per2 = {"a": {"durs": [10] * 5, "cur": None}, "b": {"durs": [10] * 5, "cur": None}}
    res.append(("eta solo cola común = pool/Σ(1/avg)", abs(eta(["a", "b"], {}, 20, per2) - 100) < 1e-6))
    per3 = {"a": {"durs": [10] * 5, "cur": "x"}}
    res.append(("en vuelo cuenta 0.5", abs(eta(["a"], {"a": 0}, 0, per3) - 5) < 1e-6))
    res.append(("fmt_dur 59s", ct.fmt_dur(59) == "0 min"))
    res.append(("fmt_dur 3600s", ct.fmt_dur(3600) == "1 h 00 min"))
    res.append(("fmt_dur negativo", ct.fmt_dur(-5) == "0 min"))
    import socket
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]
    res.append(("probe_port abierto", ct._orig_probe_port("127.0.0.1", port=port, timeout=1) == "abierto"))
    srv.close()
    res.append(("probe_port rechaza", ct._orig_probe_port("127.0.0.1", port=port) == "rechaza"))
    res.append(("probe_port mudo", ct._orig_probe_port("10.255.255.1", port=5555, timeout=0.4) == "mudo"))
    bad = [r for r in res if not r[1]]
    print(f"\n===== PURE: {'OK' if not bad else 'FALLA'} ({len(res) - len(bad)}/{len(res)}) =====")
    for n, ok in res:
        if not ok:
            print("  ✖", n)
    return not bad


# ---------------------------------------------------------------------------------------------- ESCENARIOS
def T1(h):
    """F3: desmarcar #3 y #7, enviar. Sus números quedan pendientes 'no marcado'; no se les toca."""
    r, app, fake = h["r"], h["r"].app, h["fake"]

    def go():
        r.click_usar(2); r.click_usar(6)
        r.check("send_off guardado", sorted(app.cfg.get("send_off", [])) == ["FAKE03", "FAKE07"], str(app.cfg.get("send_off")))
        r.check("glifo ☐ en la fila", app.tree.item("2", "values")[0] == "☐", str(app.tree.item("2", "values")))
        t_go = _time.time()
        app.wa_start()
        r.check("un diálogo de confirmación", [d[0] for d in h["dialogs"]] == ["askyesno"], str([d[0] for d in h["dialogs"]]))
        txt = h["dialogs"][0][1][1] if h["dialogs"] else ""
        r.check("diálogo lista #3 y #7 con 12", "fake #3 — no marcado ☐ — 12" in txt and "fake #7 — no marcado ☐ — 12" in txt, txt[:300])

        def end():
            rows = r.csv_rows()
            pend = [x for x in rows if x[2].startswith("pendiente")]
            r.check("24 pendientes 'no marcado'", len(pend) == 24 and all("no marcado" in x[2] for x in pend), f"{len(pend)} {pend[:1]}")
            r.check("84 enviados", sum(1 for x in rows if x[2] == "enviado") == 84)
            r.check("nada intrusivo a #3/#7", not fake.intrusive_to(S(3), t_go) and not fake.intrusive_to(S(7), t_go),
                    str(fake.intrusive_to(S(3), t_go)[:2]))
            app._tick()
            r.check("tablero final SIN ENVIAR 24 de 108", "SIN ENVIAR 24 de 108" in app.bn_left.cget("text"), app.bn_left.cget("text"))
            r.check("ningún texto dice 'procesados'", not r.logs_with("procesados"), str(r.logs_with("procesados")[:1]))
            r.check("resumen por teléfono en orden", any("fake #1" in l and "12 enviados" in l for l in r.logs))
            r.finish()
        r.wait(r.send_done, end, timeout=90, label="fin T1")
    r.prime(go)


def T2(h):
    """F1: invariante enviados+fallidos+faltan=total en cada tick; fallos de #2 cuentan como fallidos."""
    r, app = h["r"], h["r"].app
    bad_nums = {"51900002003", "51900002007"}
    r.script[S(2)] = lambda n: RuntimeError(f"{n}: WhatsApp no muestra 'Chatear' ni un contacto") if n in bad_nums else None
    samples = []

    def sampler():
        s = app._bulk_snapshot()
        if s:
            samples.append((s["sent"], s["failed"], s["faltan"], s["total"]))
        if not r.send_done() or not samples:
            app.after(100, sampler)

    def go():
        app.wa_start()
        sampler()

        def end():
            r.check("hubo muestras", len(samples) > 5, str(len(samples)))
            r.check("invariante en todas las muestras", all(a + b + c == d for a, b, c, d in samples), str(samples[-3:]))
            rows = r.csv_rows()
            pp = per_phone(rows)
            r.check("#2: 10 enviados 2 fallidos", pp.get("FAKE02") == {"ok": 10, "bad": 2, "pend": 0}, str(pp.get("FAKE02")))
            app._tick()
            r.check("tablero = CSV", "ENVIADOS 106" in app.bn_sent.cget("text") and "FALLIDOS 2" in app.bn_fail.cget("text"),
                    app.bn_sent.cget("text") + " / " + app.bn_fail.cget("text"))
            vals = app.tree.item("1", "values")
            r.check("fila #2 muestra 10/2", str(vals[3]) == "10" and str(vals[4]) == "2", str(vals))
            r.check("título TERMINADO", "TERMINADO" in app.bn_title.cget("text"), app.bn_title.cget("text"))
            r.finish()
        r.wait(r.send_done, end, timeout=90, label="fin T2")
    r.prime(go)


def T3(h):
    """Caída por fallo del TELÉFONO (#4): no quema números, sale a los 2, y ⚡ lo repara y lo vuelve a sumar."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    broken = {"on": True}
    r.script[S(4)] = lambda n: RuntimeError("no se abrió el buscador de contactos") if broken["on"] else None
    r.send_secs = 2.0 * harness_K()

    def go():
        app.wa_start()

        def dropped():
            app._tick()
            vals = app.tree.item("3", "values")
            r.check("fila #4 FUERA en rojo", vals[2].startswith("FUERA") and "out" in app.tree.item("3", "tags"), str(vals))
            r.check("tablero avisa 1 FUERA", "1 FUERA" in app.bn_phones.cget("text"), app.bn_phones.cget("text"))
            with app.lock:
                st = app._bulk["stats"]
                r.check("los fallos del teléfono NO cuentan como fallidos", st["failed"] == 0 and st["per"]["FAKE04"]["failed"] == 0,
                        str((st["failed"], st["per"]["FAKE04"])))
            broken["on"] = False
            t_fix = _time.time()
            app.activate_all_clicked()

            def rejoined():
                r.check("log SE SUMÓ AL ENVÍO", bool(r.logs_with("SE SUMÓ AL ENVÍO")), str(r.logs[-5:]))

                def end():
                    rows = r.csv_rows()
                    pp = per_phone(rows)
                    r.check("#4 terminó sus 12", pp.get("FAKE04", {}).get("ok") == 12, str(pp.get("FAKE04")))
                    r.check("108 enviados, 0 fallidos", sum(v["ok"] for v in pp.values()) == 108 and not any(v["bad"] for v in pp.values()), str(pp))
                    # nada intrusivo cae dentro de un intervalo ocupado de otro teléfono
                    viol = []
                    for serial, ivs in r.busy.items():
                        for c in fake.intrusive_to(serial, t_fix):
                            if any(a <= c[0] <= b for a, b in ivs):
                                viol.append((serial, c[2]))
                    r.check("⚡ no tocó teléfonos que estaban enviando", not viol, str(viol[:3]))
                    r.finish()
                r.wait(r.send_done, end, timeout=120, label="fin T3")
            r.wait(lambda: bool(r.logs_with("SE SUMÓ AL ENVÍO")) or r.send_done(), rejoined, timeout=90, label="reenganche #4")
        r.wait(lambda: bool(r.logs_with("SALIÓ DEL ENVÍO")), dropped, timeout=60, label="caída #4")
    r.prime(go)


def harness_K():
    import harness
    return harness.K


def T4(h):
    """⚡ en reposo con 6 averías distintas."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    import harness

    def go():
        fake.ph[S(6)]["listed"] = False                       # ausente hasta connect
        fake.ph[S(8)]["listed"] = False; fake.ph[S(8)]["connect"] = "timeout"
        fake.ph[S(9)]["state"] = "offline"
        fake.ph[S(5)]["has_search"] = False
        fake.ph[S(1)]["focus"] = harness.LAUNCHER
        fake.ph[S(2)]["locked"] = True; fake.ph[S(2)]["pin"] = True
        serial0 = app.serial
        n0 = len(fake.calls)
        app.activate_all_clicked()

        def end():
            calls = fake.calls[n0:]
            L = "\n".join(r.logs)
            r.check("#6 reconectado y listo", "[fake #6] ✔" in L, "")
            r.check("#8 APAGADO sin traza", "[fake #8] ✖" in L and "no responde" in L, str(r.logs_with("fake #8")[-1:]))
            disc9 = [i for i, c in enumerate(calls) if c[2][:2] == ("disconnect", S(9))]
            conn9 = [i for i, c in enumerate(calls) if c[2][:2] == ("connect", S(9))]
            r.check("#9 offline: disconnect antes de connect", bool(disc9) and bool(conn9) and disc9[0] < conn9[0], f"{disc9[:1]} {conn9[:1]}")
            fs5 = [c for c in calls if c[1] == S(5) and c[2][:3] == ("shell", "am", "force-stop")]
            r.check("#5: un solo force-stop y NO abre el buscador", len(fs5) == 1 and "[fake #5] ✖" in L and "buscador" in L, f"{len(fs5)}")
            r.check("#5: se registra lo que se lee en pantalla", bool(r.logs_with("En su pantalla se lee")), "")
            c1 = [c[2] for c in calls if c[1] == S(1) and c[2][:2] in (("shell", "monkey"), ("shell", "input"))]
            first_back = next((i for i, a in enumerate(c1) if a[:4] == ("shell", "input", "keyevent", "KEYCODE_BACK")), 999)
            first_monkey = next((i for i, a in enumerate(c1) if a[1] == "monkey"), 999)
            r.check("#1: abre WhatsApp antes de cualquier ATRÁS", first_monkey < first_back, f"monkey={first_monkey} back={first_back}")
            r.check("#2 pantalla BLOQUEADA", "pantalla BLOQUEADA" in L, "")
            r.check("sin 'ime set' en reposo", not [c for c in calls if c[2][:3] == ("shell", "ime", "set")], "")
            r.check("teléfono activo sin cambios", app.serial == serial0, f"{serial0} -> {app.serial}")
            warn = [d for d in h["dialogs"] if d[0] == "showwarning"]
            r.check("un aviso con #2, #5 y #8", len(warn) == 1 and all(f"fake #{i}" in warn[0][1][1] for i in (2, 5, 8)), str(warn)[:300])
            r.check("#8 queda esperando cable USB", "FAKE08" in app._need_usb, str(app._need_usb))
            app._tick()
            r.check("fila #5 en rojo con ✖", "out" in app.tree.item("4", "tags"), str(app.tree.item("4", "values")))
            r.finish()
        r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")), lambda: app.after(600, end), timeout=120, label="fin ⚡")
    r.prime(go)


def T6(h):
    """Regresión del bucle infinito: auto-join activo, #3 desmarcado, sin reasignar -> el envío termina solo."""
    r, app = h["r"], h["r"].app

    def go():
        r.click_usar(2)
        app.wa_auto_join.set(True)
        t0 = _time.time()
        app.wa_start()

        def end():
            r.check("terminó solo en tiempo razonable", _time.time() - t0 < 60, f"{_time.time() - t0:.0f}s")
            r.check("sin reenganches falsos", not r.logs_with("se sumó al reparto"), str(r.logs_with("se sumó al reparto")[:2]))
            pend = [x for x in r.csv_rows() if x[2].startswith("pendiente")]
            r.check("12 pendientes de #3", len(pend) == 12, str(len(pend)))
            r.finish()
        r.wait(r.send_done, end, timeout=90, label="fin T6")
    r.prime(go)


def T7(h):
    """Desmarcar #5 a mitad de envío: sale limpio; sus números se congelan aunque 'reasignar' esté activo; re-marcar lo suma."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    r.send_secs = 1.5 * harness_K()

    def go():
        app.wa_reassign.set(True)
        app.wa_start()

        def untick():
            r.click_usar(4)

            def left():
                r.check("log 'desmarcado ☐: salió'", bool(r.logs_with("desmarcado ☐: salió")), "")
                b = app._bulk
                r.check("sin castigo 'left'", b is None or "FAKE05" not in b["left"], "")
                n_own = b["own"]["FAKE05"].qsize() if b else -1
                r.check("sus números siguen en SU cola (congelados)", n_own > 0, str(n_own))
                _time.sleep(0.6)
                r.check("no pasaron a la cola común", b is None or b["own"]["FAKE05"].qsize() == n_own, "")
                r.click_usar(4)                                   # volver a marcar -> se prepara y se suma

                def end():
                    pp = per_phone(r.csv_rows())
                    r.check("#5 acabó enviando sus 12", pp.get("FAKE05", {}).get("ok") == 12, str(pp.get("FAKE05")))
                    r.check("dueño sin cambios", all(a["key"] == "FAKE05" for n, a in app.assign.items() if n.startswith("51900005")), "")
                    r.finish()
                r.wait(r.send_done, end, timeout=120, label="fin T7")
            r.wait(lambda: bool(r.logs_with("desmarcado ☐: salió")), left, timeout=60, label="salida #5")
        r.wait(lambda: len(r.sent) >= 9, untick, timeout=60, label="arranque T7")
    r.prime(go)


def T8(h):
    """Clics: la columna ☑ no cambia la selección ni el teléfono activo; cabecera marca/desmarca todos."""
    r, app = h["r"], h["r"].app

    def go():
        sel0, serial0 = app.tree.selection(), app.serial
        ret = r.click_usar(3)
        r.check("clic en ☑ devuelve 'break'", ret == "break", str(ret))
        r.check("selección intacta", app.tree.selection() == sel0, f"{sel0} -> {app.tree.selection()}")
        r.check("activo intacto", app.serial == serial0, "")
        r.check("fila 3 quedó ☐", app.tree.item("3", "values")[0] == "☐", "")
        r.check("clic fuera de ☑ no intercepta", r.click_usar(3, column="#2") is None, "")
        app.mark_phones(lambda p: False)
        r.check("Ninguno: todos ☐", all(app.tree.item(str(i), "values")[0] == "☐" for i in range(9)), "")
        app.mark_phones(lambda p: True)
        r.check("Todos: send_off vacío", app.cfg.get("send_off") == [], str(app.cfg.get("send_off")))
        app.mark_phones(lambda p: False)
        app.wa_start()
        r.check("sin marcados: aviso y no arranca", any(d[0] == "showwarning" for d in h["dialogs"]) and not app.bulk_active, str(h["dialogs"])[:200])
        r.finish()
    r.prime(go)


def T9(h):
    """Reasignar activo + #3 desmarcado: NO = pendientes y dueño intacto."""
    r, app = h["r"], h["r"].app

    def go():
        r.click_usar(2)
        app.wa_reassign.set(True)
        h["answers"]["askyesnocancel"] = False
        app.wa_start()
        r.check("diálogo a tres bandas", any(d[0] == "askyesnocancel" for d in h["dialogs"]), str([d[0] for d in h["dialogs"]]))

        def end():
            pend = [x for x in r.csv_rows() if x[2].startswith("pendiente")]
            r.check("12 pendientes de #3", len(pend) == 12, str(len(pend)))
            r.check("dueño de #3 intacto", all(a["key"] == "FAKE03" for n, a in app.assign.items() if n.startswith("51900003")), "")
            r.finish()
        r.wait(r.send_done, end, timeout=90, label="fin T9")
    r.prime(go)


def T9b(h):
    """Reasignar activo + #3 desmarcado: SÍ = los envían otros y cambian de dueño."""
    r, app = h["r"], h["r"].app

    def go():
        r.click_usar(2)
        app.wa_reassign.set(True)
        h["answers"]["askyesnocancel"] = True
        app.wa_start()

        def end():
            rows = r.csv_rows()
            r.check("108 enviados", sum(1 for x in rows if x[2] == "enviado") == 108, "")
            r.check("los de #3 cambiaron de dueño", all(a["key"] != "FAKE03" for n, a in app.assign.items() if n.startswith("51900003")), "")
            r.finish()
        r.wait(r.send_done, end, timeout=90, label="fin T9b")
    r.prime(go)


def T10(h):
    """Restringido y marcado: no arranca. Desmarcado: arranca sin él."""
    r, app = h["r"], h["r"].app

    def go():
        app.cfg["phones"][1]["restricted_at"] = _time.time()
        app.wa_start()
        r.check("no arranca y pide desmarcar", not app.bulk_active and any("Desmárcalos" in d[1][1] for d in h["dialogs"] if d[0] == "showwarning"),
                str(h["dialogs"])[:200])
        r.click_usar(1)
        app.wa_start()

        def end():
            pp = per_phone(r.csv_rows())
            r.check("#2 no envió nada", pp.get("FAKE02", {}).get("ok", 0) == 0 and pp.get("FAKE02", {}).get("pend") == 12, str(pp.get("FAKE02")))
            r.check("los demás 96", sum(v["ok"] for v in pp.values()) == 96, "")
            r.finish()
        r.wait(lambda: r.send_done() and bool(r.csv_rows()), end, timeout=90, label="fin T10")
    r.prime(go)


def T13(h):
    """Guardas: doble ⚡, enviar durante ⚡, quitar teléfono durante un envío, solo-activo desmarcado."""
    r, app = h["r"], h["r"].app
    r.send_secs = 1.0 * harness_K()

    def go():
        app.activate_all_clicked()
        app.activate_all_clicked()
        r.check("segundo ⚡ rechazado", bool(r.logs_with("Ya se está activando")), "")
        app.wa_start()
        r.check("enviar durante ⚡ rechazado", bool(r.logs_with("Espera a que termine ⚡")) and not app.bulk_active, "")

        def after_act():
            app.wa_start()

            def running():
                n = len(app.cfg["phones"])
                app.remove_selected()
                r.check("quitar durante envío rechazado", len(app.cfg["phones"]) == n and bool(r.logs_with("No se puede quitar")), "")
                app.wa_stop()

                def end():
                    app._tick()
                    r.check("título DETENIDO", "DETENIDO" in app.bn_title.cget("text"), app.bn_title.cget("text"))
                    r.check("pendientes guardados", len(app.wa_pending or []) > 0, str(len(app.wa_pending or [])))
                    rows = r.csv_rows()
                    r.check("CSV completo al detener", len(rows) == 108, str(len(rows)))
                    app.wa_split.set(False)
                    act = app.active_phone()
                    app.mark_phones(lambda p: p is not act)
                    app.wa_start()
                    r.check("solo-activo desmarcado: se niega", any("desmarcado" in d[1][1] for d in h["dialogs"] if d[0] == "showwarning"), "")
                    r.finish()
                r.wait(r.send_done, end, timeout=90, label="fin tras detener")
            r.wait(lambda: len(r.sent) >= 9, running, timeout=60, label="envío en marcha")
        r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")) and not app._activating.locked(), after_act, timeout=120, label="fin ⚡")
    r.prime(go)


def T14(h):
    """Cola final: el último trabajador falla con un número de la cola común; un teléfono ya terminado lo recoge."""
    r, app = h["r"], h["r"].app
    import harness
    state = {"armed": True}

    def failing(n):
        if n.startswith("5191") and state["armed"]:
            return harness.subprocess.TimeoutExpired(["adb"], 30)
        return None
    for i in range(1, 10):
        r.script[S(i)] = failing          # el primero que tome el número libre falla (los demás ya habrán terminado)

    def go():
        app.wa_list.insert("1.0", "\n".join(list(app.assign) + ["51910000001"]))   # 108 asignados + 1 libre (cola común)
        app.wa_start()

        def end():
            rows = r.csv_rows()
            free = [x for x in rows if x[0] == "51910000001"]
            r.check("el número libre terminó enviado", len(free) == 1 and free[0][2] == "enviado", str(free))
            r.check("0 pendientes", not [x for x in rows if x[2].startswith("pendiente")], "")
            r.finish()

        def disarm():
            state["armed"] = False
        r.wait(lambda: bool(r.logs_with("SALIÓ DEL ENVÍO")) or r.send_done(), disarm, timeout=90, label="fallo en cola final")
        r.wait(r.send_done, end, timeout=150, label="fin T14")
    r.prime(go)


def T16(h):
    """Última oportunidad: #4 sale por WhatsApp roto; al quedar solo sus números, se repara (force-stop) y termina."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    d4 = fake.ph[S(4)]
    r.script[S(4)] = lambda n: RuntimeError("no se pudo llegar al listado de chats de WhatsApp") if not d4["has_fab"] else None

    def go():
        d4["has_fab"] = False
        d4["fix_on_force"] = True
        app.cfg["wa_ultima_oportunidad"] = True
        app.wa_start()

        def end():
            pp = per_phone(r.csv_rows())
            r.check("hubo 'última oportunidad'", bool(r.logs_with("última oportunidad")), "")
            r.check("#4 acabó sus 12 sin fallidos", pp.get("FAKE04") == {"ok": 12, "bad": 0, "pend": 0}, str(pp.get("FAKE04")))
            r.check("TERMINADO", any("ENVÍO TERMINADO" in l for l in r.logs), "")
            r.finish()
        r.wait(r.send_done, end, timeout=150, label="fin T16")
    r.prime(go)


def T17(h):
    """Salud: batería baja, no responde, y jamás tocar la conexión de un teléfono que está enviando."""
    r, app, fake = h["r"], h["r"].app, h["fake"]
    r.send_secs = 2.0 * harness_K()

    def go():
        fake.ph[S(6)].update(battery=12, powered=False)
        fake.ph[S(8)]["dead"] = True
        app._health_poll()
        app._tick()
        r.check("#6 BATERÍA BAJA", "BATERÍA BAJA" in app.tree.item("5", "values")[2], str(app.tree.item("5", "values")))
        r.check("#8 NO RESPONDE", "NO RESPONDE" in app.tree.item("7", "values")[2], str(app.tree.item("7", "values")))
        fake.ph[S(8)]["dead"] = False
        app.mark_phones(lambda p: p["name"] in ("fake #1", "fake #2"))
        app.wa_start()

        def running():
            h["probes"]["10.255.0.1"] = "abierto"
            fake.ph[S(1)]["state"] = "offline"                  # microcorte mientras #1 escribe
            n0 = len(fake.calls)
            app._health_poll()
            touched = [c for c in fake.calls[n0:] if c[2][0] in ("connect", "disconnect") and c[2][1] == S(1)]
            r.check("microcorte: 0 connect/disconnect al que envía", not touched, str(touched))
            bat = [c for c in fake.calls[n0:] if c[1] in (S(1), S(2)) and c[2][:3] == ("shell", "dumpsys", "battery")]
            r.check("sin dumpsys a los que envían", not bat, str(bat[:2]))
            fake.ph[S(1)]["state"] = "device"
            app.wa_stop()
            r.wait(r.send_done, r.finish, timeout=90, label="fin T17")
        r.wait(lambda: len(r.sent) >= 2, running, timeout=60, label="envío en marcha")
    r.prime(go)


def T18(h):
    """Comprobación previa: ▶ Enviar -> SÍ -> ⚡ -> el envío arranca solo al terminar."""
    r, app = h["r"], h["r"].app

    def go():
        app.cfg["wa_preflight"] = True
        h["answers"]["askyesnocancel"] = True
        app.wa_start()
        r.check("pregunta de comprobación previa", any("Comprobar teléfonos" in d[1][0] for d in h["dialogs"]), str([d[1][0] for d in h["dialogs"]]))

        def end():
            rows = r.csv_rows()
            r.check("tras ⚡ el envío arrancó y terminó", sum(1 for x in rows if x[2] == "enviado") == 108, str(len(rows)))
            r.check("no repite la pregunta", sum(1 for d in h["dialogs"] if "Comprobar teléfonos" in d[1][0]) == 1, "")
            r.finish()
        r.wait(lambda: r.send_done() and bool(r.csv_rows()), end, timeout=180, label="fin T18")
    r.prime(go)


def T19(h):
    """Cable USB sin clics: #8 rechaza el puerto; ⚡ lo deja esperando cable; al enchufarlo se re-registra solo."""
    r, app, fake = h["r"], h["r"].app, h["fake"]

    def go():
        d8 = fake.ph[S(8)]
        d8["listed"] = False; d8["connect"] = "refused"
        h["probes"]["10.255.0.8"] = "rechaza"
        serial0 = app.serial
        app.activate_all_clicked()

        def plug():
            r.check("pide CABLE USB", any("CABLE USB" in l and "fake #8" in l for l in r.logs), "")
            fake.usb["FAKE08"] = S(8); d8["usb_plugged"] = True

            def end():
                tc = [c for c in fake.calls if c[2][0] == "tcpip"]
                r.check("un solo tcpip y fue al #8", len(tc) == 1 and tc[0][1] == "FAKE08", str(tc))
                r.check("teléfono activo sin cambios", app.serial == serial0, f"{serial0} -> {app.serial}")
                r.check("salió de la lista de espera de cable", "FAKE08" not in app._need_usb, str(app._need_usb))
                r.finish()
            r.wait(lambda: bool(r.logs_with("conexión recuperada por cable")), end, timeout=60, label="recuperación por cable")
        r.wait(lambda: bool(r.logs_with("⚡ RESULTADO")), plug, timeout=120, label="fin ⚡")
    r.prime(go)


SCEN = {k: v for k, v in list(globals().items()) if k.startswith("T") and callable(v)}


def run_one(name):
    if name == "PURE":
        return PURE()
    import harness
    extra = {}
    ct, fake, tmp, dialogs, answers, hits, probes = harness.boot(extra)
    r = harness.Runner(ct, fake, tmp, hits)
    h = {"r": r, "fake": fake, "dialogs": dialogs, "answers": answers, "probes": probes, "ct": ct}
    SCEN[name](h)
    r.app.after(240000, lambda: (r.check("TIMEOUT GLOBAL", False), r.app.destroy()))
    r.app.mainloop()
    return r.report(name + " — " + (SCEN[name].__doc__ or "").strip())


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    if which != "ALL":
        sys.exit(0 if run_one(which) else 1)
    names = ["PURE"] + sorted(SCEN, key=lambda k: (len(k), k))
    fails = []
    for n in names:
        p = _sp.run([sys.executable, os.path.abspath(__file__), n], capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        out = (p.stdout or "") + (p.stderr[-1500:] if p.returncode and p.stderr else "")
        print(out.strip())
        if p.returncode:
            fails.append(n)
    print("\n================ RESUMEN:", "TODO OK" if not fails else "FALLAN " + ", ".join(fails), "================")
