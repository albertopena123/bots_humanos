"""
Base de datos PostgreSQL de números y teléfonos (control_telefono.py).

Lee DATABASE_URL del archivo .env (o de la variable de entorno). Si no hay driver, .env o servidor,
la app sigue funcionando con los archivos JSON; nada aquí lanza errores hacia la interfaz.

Tablas:
  telefonos     serie (PK), nombre, modelo, actualizado
  asignaciones  numero (PK) -> serie del teléfono que lo atiende, nombre_telefono, asignado_en,
                ultimo_envio, total_envios
  envios        historial completo: numero, serie, nombre_telefono, estado, hora, mensaje, reporte
"""
import os
import re
import threading

try:
    import psycopg
except ImportError:          # pip install "psycopg[binary]"
    psycopg = None

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, ".env")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telefonos (
    serie       TEXT PRIMARY KEY,
    nombre      TEXT NOT NULL,
    modelo      TEXT,
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS asignaciones (
    numero          TEXT PRIMARY KEY,
    serie           TEXT NOT NULL REFERENCES telefonos(serie),
    nombre_telefono TEXT,
    asignado_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_envio    TIMESTAMPTZ,
    total_envios    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS asignaciones_serie_idx ON asignaciones(serie);
CREATE TABLE IF NOT EXISTS envios (
    id              BIGSERIAL PRIMARY KEY,
    numero          TEXT NOT NULL,
    serie           TEXT,
    nombre_telefono TEXT,
    estado          TEXT NOT NULL,
    hora            TIMESTAMPTZ,
    mensaje         TEXT,
    reporte         TEXT,
    creado          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS envios_numero_idx ON envios(numero);
CREATE INDEX IF NOT EXISTS envios_hora_idx ON envios(hora);
"""


def database_url():
    """DATABASE_URL de .env o del entorno, sin el parámetro ?schema= (estilo Prisma) que psycopg no acepta."""
    url = os.environ.get("DATABASE_URL")
    if not url and os.path.isfile(ENV_FILE):
        try:
            with open(ENV_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            url = None
    if not url:
        return None
    url = re.sub(r"[?&]schema=[^&]*", "", url).replace("?&", "?").rstrip("?&")
    return url


class DB:
    def __init__(self, url):
        self.url = url
        self._lock = threading.Lock()
        self._con = None
        self.last_error = None

    @classmethod
    def from_env(cls):
        url = database_url()
        return cls(url) if url and psycopg else None

    # ------------------------------------------------------------ base
    def _connect(self):
        if self._con is None or self._con.closed:
            self._con = psycopg.connect(self.url, autocommit=True, connect_timeout=5)
        return self._con

    def run(self, fn):
        """Ejecuta fn(con) con la conexión bajo candado. Reconecta una vez si la conexión se cayó."""
        with self._lock:
            for intento in (1, 2):
                try:
                    return fn(self._connect())
                except Exception as e:
                    self.last_error = e
                    try:
                        if self._con:
                            self._con.close()
                    except Exception:
                        pass
                    self._con = None
                    if intento == 2 or isinstance(e, (psycopg.ProgrammingError, psycopg.DataError)):
                        raise

    def ping(self):
        try:
            self.run(lambda con: con.execute("select 1").fetchone())
            return True
        except Exception:
            return False

    def init_schema(self):
        self.run(lambda con: con.execute(SCHEMA))

    # ------------------------------------------------------------ teléfonos
    def upsert_telefono(self, serie, nombre, modelo=None):
        if not serie:
            return
        self.run(lambda con: con.execute(
            "INSERT INTO telefonos(serie, nombre, modelo) VALUES (%s, %s, %s) "
            "ON CONFLICT (serie) DO UPDATE SET nombre = EXCLUDED.nombre, "
            "modelo = COALESCE(EXCLUDED.modelo, telefonos.modelo), actualizado = now()",
            (serie, nombre, modelo)))

    def upsert_telefonos(self, phones):
        """phones: iterable de dicts con hw, name, model."""
        def _f(con):
            for ph in phones:
                if ph.get("hw"):
                    con.execute(
                        "INSERT INTO telefonos(serie, nombre, modelo) VALUES (%s, %s, %s) "
                        "ON CONFLICT (serie) DO UPDATE SET nombre = EXCLUDED.nombre, "
                        "modelo = COALESCE(EXCLUDED.modelo, telefonos.modelo), actualizado = now()",
                        (ph["hw"], ph["name"], ph.get("model")))
        self.run(_f)

    # ------------------------------------------------------------ asignaciones
    def cargar_asignaciones(self):
        """{numero: {"key": serie, "name": nombre_telefono, "at": "AAAA-MM-DD HH:MM:SS"}}"""
        rows = self.run(lambda con: con.execute(
            "SELECT a.numero, a.serie, COALESCE(t.nombre, a.nombre_telefono), a.asignado_en "
            "FROM asignaciones a LEFT JOIN telefonos t ON t.serie = a.serie").fetchall())
        return {n: {"key": s, "name": nm, "at": at.strftime("%Y-%m-%d %H:%M:%S") if at else ""}
                for n, s, nm, at in rows}

    def asignar(self, numero, serie, nombre):
        """Liga (o re-liga) un número a un teléfono. Crea el teléfono si no existe."""
        def _f(con):
            con.execute("INSERT INTO telefonos(serie, nombre) VALUES (%s, %s) ON CONFLICT (serie) DO NOTHING",
                        (serie, nombre))
            con.execute(
                "INSERT INTO asignaciones(numero, serie, nombre_telefono) VALUES (%s, %s, %s) "
                "ON CONFLICT (numero) DO UPDATE SET serie = EXCLUDED.serie, nombre_telefono = EXCLUDED.nombre_telefono, "
                "asignado_en = CASE WHEN asignaciones.serie <> EXCLUDED.serie THEN now() ELSE asignaciones.asignado_en END",
                (numero, serie, nombre))
        self.run(_f)

    def importar_asignaciones(self, assign):
        """Inserta las asignaciones de un dict (formato de asignaciones.json) que aún no existan. Devuelve cuántas."""
        def _f(con):
            n = 0
            for numero, a in assign.items():
                serie, nombre = a.get("key"), a.get("name") or ""
                if not serie:
                    continue
                con.execute("INSERT INTO telefonos(serie, nombre) VALUES (%s, %s) ON CONFLICT (serie) DO NOTHING",
                            (serie, nombre or serie))
                cur = con.execute(
                    "INSERT INTO asignaciones(numero, serie, nombre_telefono, asignado_en) "
                    "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now())) ON CONFLICT (numero) DO NOTHING",
                    (numero, serie, nombre, a.get("at") or None))
                n += cur.rowcount
            return n
        return self.run(_f)

    def quitar_asignaciones(self, serie):
        return self.run(lambda con: con.execute("DELETE FROM asignaciones WHERE serie = %s", (serie,)).rowcount)

    def quitar_asignacion(self, numero):
        return self.run(lambda con: con.execute("DELETE FROM asignaciones WHERE numero = %s", (numero,)).rowcount)

    # ------------------------------------------------------------ envíos
    def registrar_envio(self, numero, serie, nombre, estado, hora, mensaje, reporte):
        """Guarda un intento de envío. Si fue 'enviado', actualiza contador y último envío de la asignación."""
        def _f(con):
            con.execute(
                "INSERT INTO envios(numero, serie, nombre_telefono, estado, hora, mensaje, reporte) "
                "VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s)",
                (numero, serie or None, nombre, estado, hora or None, mensaje, reporte))
            if estado == "enviado" and serie:
                con.execute(
                    "UPDATE asignaciones SET total_envios = total_envios + 1, ultimo_envio = COALESCE(%s::timestamptz, now()) "
                    "WHERE numero = %s", (hora or None, numero))
        self.run(_f)

    def registrar_envios(self, rows, reporte):
        """rows: iterable de (numero, nombre, estado, hora, mensaje, serie). Inserción en bloque."""
        def _f(con):
            with con.transaction():
                for numero, nombre, estado, hora, mensaje, serie in rows:
                    con.execute(
                        "INSERT INTO envios(numero, serie, nombre_telefono, estado, hora, mensaje, reporte) "
                        "VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s)",
                        (numero, serie or None, nombre if nombre != "-" else None, estado, hora or None, mensaje, reporte))
        self.run(_f)

    def resumen(self):
        """[(nombre, serie, asignados, enviados_total)] por teléfono."""
        return self.run(lambda con: con.execute(
            "SELECT t.nombre, t.serie, COUNT(a.numero), COALESCE(SUM(a.total_envios), 0) "
            "FROM telefonos t LEFT JOIN asignaciones a ON a.serie = t.serie "
            "GROUP BY t.nombre, t.serie ORDER BY t.nombre").fetchall())

    def historial(self, numero):
        return self.run(lambda con: con.execute(
            "SELECT hora, nombre_telefono, estado, reporte FROM envios WHERE numero = %s ORDER BY hora DESC NULLS LAST",
            (numero,)).fetchall())
