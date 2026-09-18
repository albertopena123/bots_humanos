# bots_humanos

Aplicación de escritorio (Python + Tkinter) para controlar varios teléfonos Android por ADB y enviar mensajes de
WhatsApp Business de forma repartida, escribiendo "como una persona" (busca el número en la app y teclea letra por letra).

## Qué hace

- **Registro y conexión de teléfonos** por USB y por WiFi (ADB puerto 5555). Busca los teléfonos en la red y los
  reconoce por número de serie aunque cambien de IP.
- **Asignación fija de contactos**: cada número queda ligado al teléfono que le escribió y siempre se le envía desde ese.
- **Selección de teléfonos** con casilla ☑ para decidir cuáles participan en un envío.
- **Tablero en vivo**: enviados, fallidos, faltan, ritmo por hora y hora estimada de fin, en total y por teléfono.
- **⚡ ACTIVAR TODOS**: reconecta, despierta, abre WhatsApp y comprueba que cada teléfono está listo. Si uno se reinició
  y necesita cable USB, lo dice y se re-registra solo al enchufarlo.
- **Seguridad de los envíos**: nunca reenvía solo un mensaje que pudo haber salido, comprueba el número de serie del
  aparato antes de escribir, y verifica el título del chat antes de teclear.
- Soporta los **dos diseños de "Nuevo chat"** de WhatsApp (lupa + "Chatear", y el nuevo con cuadro de búsqueda +
  "Enviar mensaje").
- **Base de datos PostgreSQL** (opcional) con teléfonos, asignaciones e historial de envíos; si no está, usa archivos JSON/CSV.
- **Registros** en `logs/`: uno general por día y uno detallado por envío, paso a paso.

## Requisitos

- Windows, Python 3.10 o superior.
- ADB y scrcpy: ejecutar `Instalar ADB y scrcpy.bat`.
- Dependencias de Python: `pip install -r requirements.txt` (solo hace falta para la base de datos).
- Depuración USB activada en cada teléfono. `ADBKeyboard.apk` se instala solo (teclado que admite tildes y emojis).
  `ADBKeyboard.apk` es el proyecto de código abierto [senzhk/ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard) (GPL-2.0).

## Uso

1. (Opcional) Copiar `.env.example` como `.env` y poner la conexión a PostgreSQL.
2. Abrir con `Abrir control.bat` (o `python control_telefono.py`).
3. Conectar cada teléfono por cable una vez: se registra solo y queda por WiFi.
4. Cargar los números (Excel/CSV/TXT) o usar los asignados en la base de datos, escribir el mensaje y pulsar **▶ Enviar**.

## Pruebas sin teléfonos

La carpeta `pruebas/` trae un simulador: ADB falso, sin base de datos, con el tiempo acelerado.
No toca teléfonos reales ni envía nada.

```
cd pruebas
python run.py ALL          # escenarios de envío, selección, activación y tablero
python regress.py ALL      # regresiones (duplicados, 'solo el activo', Detener, identidad…)
python layout_nuevo.py     # los dos diseños de 'Nuevo chat' con el tecleo real de la app
```

`pruebas/real_check.py` sí usa un teléfono real: recorre los pasos hasta **abrir** el chat y se detiene, sin escribir ni enviar.

## Qué NO está en el repositorio (a propósito)

`.env`, los reportes `envios_*.csv`, `logs/`, `asignaciones.json`, `pendientes_ultimo.txt`, `fallidos_ultimo.txt`,
`config_telefono.json` y `telefonos_conocidos.json`: contienen contraseñas, teléfonos de clientes o datos propios de cada PC.
La aplicación los crea sola al usarse.
