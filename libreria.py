# ========================================================
# A. HERRAMIENTAS Y CONFIGURACIÓN DEL PROYECTO
# ========================================================
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sqlite3
import re
import unicodedata
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
DB = BASE / "libreria.db"
sys.path.insert(0, str(BASE / "libs"))

from productos import PRODUCTOS as CATALOGO_ORIGINAL

if isinstance(CATALOGO_ORIGINAL, list):
    PRODUCTOS = {
        p["codigo"].upper(): p for p in CATALOGO_ORIGINAL
    }
elif isinstance(CATALOGO_ORIGINAL, dict):
    PRODUCTOS = {
        codigo.upper(): p
        for codigo, p in CATALOGO_ORIGINAL.items()
    }
else:
    raise ValueError("El catálogo debe ser lista o diccionario.")

MODELO_PRINCIPAL = "gemini-3.5-flash-lite"
MODELO_RESPALDO = "gemini-3.6-flash"

sesiones = {}
memoria_usuarios = {}

# ========================================================
# B. CREAR Y VERIFICAR LA BASE DE DATOS
# ========================================================
def conectar():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def iniciar_db():
    """Crea la tabla si no existe y migra el CHECK antiguo sin borrar pedidos."""
    nueva = not DB.exists()
    con = conectar()
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consecutivo TEXT UNIQUE,
          cliente TEXT NOT NULL,
          chat_id INTEGER NOT NULL,
          codigo_producto TEXT NOT NULL,
          nombre_producto TEXT NOT NULL,
          cantidad INTEGER NOT NULL CHECK(cantidad > 0),
          precio_unitario INTEGER NOT NULL CHECK(precio_unitario > 0),
          total INTEGER NOT NULL CHECK(total = cantidad * precio_unitario),
          fecha TEXT NOT NULL,
          hora TEXT NOT NULL,
          estado TEXT NOT NULL DEFAULT 'PENDIENTE'
            CHECK(estado IN ('PENDIENTE','EN_ESPERA','ACEPTADO','CANCELADO')),
          telegram_update_id INTEGER NOT NULL UNIQUE
        )
        """)
        con.commit()
        definicion = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pedidos'"
        ).fetchone()[0]
        # La versión original solo permitía PENDIENTE. SQLite no permite alterar
        # un CHECK: se reconstruye la tabla dentro de una transacción y se copian
        # todos los registros, incluidos sus IDs y consecutivos.
        if "CHECK(estado = 'PENDIENTE')" in definicion.replace(" ", "") or "CHECK(estado='PENDIENTE')" in definicion.replace(" ", ""):
            con.execute("BEGIN IMMEDIATE")
            con.execute("""
              CREATE TABLE pedidos_nueva (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consecutivo TEXT UNIQUE,
                cliente TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                codigo_producto TEXT NOT NULL,
                nombre_producto TEXT NOT NULL,
                cantidad INTEGER NOT NULL CHECK(cantidad > 0),
                precio_unitario INTEGER NOT NULL CHECK(precio_unitario > 0),
                total INTEGER NOT NULL CHECK(total = cantidad * precio_unitario),
                fecha TEXT NOT NULL,
                hora TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'PENDIENTE'
                  CHECK(estado IN ('PENDIENTE','EN_ESPERA','ACEPTADO','CANCELADO')),
                telegram_update_id INTEGER NOT NULL UNIQUE
              )
            """)
            con.execute("""
              INSERT INTO pedidos_nueva
              (id, consecutivo, cliente, chat_id, codigo_producto, nombre_producto,
               cantidad, precio_unitario, total, fecha, hora, estado, telegram_update_id)
              SELECT id, consecutivo, cliente, chat_id, codigo_producto, nombre_producto,
                     cantidad, precio_unitario, total, fecha, hora, estado, telegram_update_id
              FROM pedidos
            """)
            con.execute("DROP TABLE pedidos")
            con.execute("ALTER TABLE pedidos_nueva RENAME TO pedidos")
            con.commit()
            print("🔧 Base de datos migrada: se conservaron todos los pedidos existentes.")

        columnas = {fila[1] for fila in con.execute("PRAGMA table_info(pedidos)")}
        necesarias = {"id", "consecutivo", "cliente", "chat_id", "codigo_producto",
                      "nombre_producto", "cantidad", "precio_unitario", "total",
                      "fecha", "hora", "estado", "telegram_update_id"}
        if not necesarias.issubset(columnas):
            raise ValueError("La tabla pedidos tiene otro diseño.")
        accion = "creada" if nueva else "abierta"
        print(f"✅ Base de datos {accion}: {DB}")
        print("✅ Tabla pedidos verificada")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

ESTADOS_PEDIDO = {"PENDIENTE", "EN_ESPERA", "ACEPTADO", "CANCELADO"}


def cambiar_estado(consecutivo, nuevo_estado):
    """Actualiza el estado y devuelve el pedido actualizado; no envía notificaciones."""
    nuevo_estado = str(nuevo_estado).upper()
    if nuevo_estado not in ESTADOS_PEDIDO:
        raise ValueError("Estado de pedido no válido.")
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        fila = con.execute("SELECT * FROM pedidos WHERE consecutivo = ?", (consecutivo.upper(),)).fetchone()
        if not fila:
            raise ValueError("No existe ese pedido.")
        anterior = fila["estado"]
        if anterior == nuevo_estado:
            con.commit()
            return dict(fila), False
        con.execute("UPDATE pedidos SET estado = ? WHERE consecutivo = ?",
                    (nuevo_estado, consecutivo.upper()))
        actual = dict(con.execute("SELECT * FROM pedidos WHERE consecutivo = ?",
                                  (consecutivo.upper(),)).fetchone())
        con.commit()
        return actual, True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cancelar_pedido_cliente(consecutivo, chat_id):
    """Solo el propietario puede cancelar un pedido pendiente o en espera."""
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        fila = con.execute(
            "SELECT * FROM pedidos WHERE consecutivo = ? AND chat_id = ?",
            (consecutivo.upper(), chat_id)
        ).fetchone()
        if not fila:
            con.rollback()
            return None, "No encontré un pedido con ese código asociado a tu cuenta."
        pedido = dict(fila)
        if pedido["estado"] not in ("PENDIENTE", "EN_ESPERA"):
            con.rollback()
            return pedido, f"No puedes cancelar el pedido {consecutivo.upper()} porque su estado es {pedido['estado']}. Solo se cancelan pedidos PENDIENTE o EN_ESPERA."
        con.execute("UPDATE pedidos SET estado = 'CANCELADO' WHERE id = ?", (pedido["id"],))
        pedido = dict(con.execute("SELECT * FROM pedidos WHERE id = ?", (pedido["id"],)).fetchone())
        con.commit()
        return pedido, None
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def notificar_estado_telegram(pedido):
    """Envía una notificación por Telegram usando la API estándar del bot."""
    import json
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        try:
            from dotenv import load_dotenv
            load_dotenv(BASE / ".env")
            token = os.getenv("TELEGRAM_TOKEN")
        except ImportError:
            pass
    if not token:
        return False, "No se encontró TELEGRAM_TOKEN en el entorno ni en .env."
    estados = {
        "PENDIENTE": "Pendiente",
        "EN_ESPERA": "En espera",
        "ACEPTADO": "Aceptado",
        "CANCELADO": "Cancelado",
    }
    texto = (f"📚 Actualización de tu pedido {pedido['consecutivo']}\n"
             f"Libro: {pedido['nombre_producto']}\n"
             f"Cantidad: {pedido['cantidad']}\n"
             f"Nuevo estado: {estados.get(pedido['estado'], pedido['estado'])}\n"
             f"Total: {pesos(pedido['total'])}")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    datos = json.dumps({"chat_id": pedido["chat_id"], "text": texto}).encode("utf-8")
    req = Request(url, data=datos, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=8) as respuesta:
            resultado = json.loads(respuesta.read().decode("utf-8"))
        return bool(resultado.get("ok")), None if resultado.get("ok") else "Telegram no aceptó el mensaje."
    except (URLError, HTTPError, TimeoutError, OSError, ValueError) as error:
        return False, type(error).__name__

# ========================================================
# C. VALIDAR Y GUARDAR EL PEDIDO
# ========================================================
def validar_cantidad(texto):
    if not re.fullmatch(r"[0-9]{1,3}", texto.strip()):
        raise ValueError("Escriba un entero de 1 a 999.")
    cantidad = int(texto)
    if not 1 <= cantidad <= 999:
        raise ValueError("La cantidad debe estar entre 1 y 999.")
    return cantidad

def guardar_pedido(cliente, chat_id, codigo,
                   cantidad, update_id):
    if codigo not in PRODUCTOS:
        raise ValueError("Libro desconocido.")
    if type(cantidad) is not int or not 1 <= cantidad <= 999:
        raise ValueError("Cantidad inválida.")
    producto = PRODUCTOS[codigo]
    precio = producto["precio"]
    total = cantidad * precio
    ahora = datetime.now()
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        existente = con.execute(
            "SELECT * FROM pedidos "
            "WHERE telegram_update_id = ?",
            (update_id,)
        ).fetchone()
        if existente:
            con.commit()
            return dict(existente)

        cursor = con.execute("""
          INSERT INTO pedidos (
            cliente, chat_id, codigo_producto,
            nombre_producto, cantidad, precio_unitario,
            total, fecha, hora, telegram_update_id
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cliente, chat_id, codigo, producto["nombre"],
            cantidad, precio, total,
            ahora.strftime("%Y-%m-%d"),
            ahora.strftime("%H:%M:%S"), update_id
        ))
        numero = cursor.lastrowid
        consecutivo = f"PED-{numero:04d}"
        con.execute(
            "UPDATE pedidos SET consecutivo = ? WHERE id = ?",
            (consecutivo, numero)
        )
        pedido = dict(con.execute(
            "SELECT * FROM pedidos WHERE id = ?", (numero,)
        ).fetchone())
        con.commit()
        return pedido
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

# ========================================================
# D. CONSULTAR LOS PEDIDOS CON SELECT
# ========================================================
def consultar(tipo="todos", consecutivo=None, chat_id=None):
    sql = "SELECT * FROM pedidos"
    parametros = ()
    if tipo == "pendientes":
        sql += " WHERE estado IN (?, ?)"
        parametros = ("PENDIENTE", "EN_ESPERA")
    elif tipo == "pedido":
        sql += " WHERE consecutivo = ?"
        parametros = (consecutivo,)
    elif tipo != "todos":
        raise ValueError("Consulta desconocida.")
    if chat_id is not None:
        sql += " AND" if parametros else " WHERE"
        sql += " chat_id = ?"
        parametros += (chat_id,)
    sql += " ORDER BY id"
    con = conectar()
    try:
        return [dict(fila) for fila in
                con.execute(sql, parametros).fetchall()]
    finally:
        con.close()

def pesos(valor):
    return "$" + f"{valor:,}".replace(",", ".")

def mostrar(pedidos):
    if not pedidos:
        print("No hay pedidos para esta consulta.")
    for p in pedidos:
        print(f"\n{p['consecutivo']} | {p['estado']}")
        print(f"Cliente: {p['cliente']}")
        print(f"{p['codigo_producto']} | {p['nombre_producto']}")
        print(f"Cantidad: {p['cantidad']}")
        print("Unitario:", pesos(p['precio_unitario']))
        print("Total:", pesos(p['total']))
        print(f"Fecha: {p['fecha']} | Hora: {p['hora']}")

# ========================================================
# E. GEMINI INTERACTIONS Y MEMORIA
# ========================================================
def responder_gemini(cliente_ia, chat_id, codigo, texto):
    producto = PRODUCTOS[codigo]
    prompt = (
        "Eres el asistente de la Librería MVP. Responde en español, "
        "con amabilidad y brevedad. Usa solo este catálogo de libros: "
        f"{PRODUCTOS}. Libro activo: {producto}. "
        "No inventes libros, autores ni precios. "
        "No proceses pagos. Para comprar indica /comprar. "
        "Python solicita cantidad y calcula el total. "
        "Solo Python confirma que guardó un pedido. "
        "Para consultar pedidos indica /mis_pedidos. "
        f"Mensaje del cliente: {texto}"
    )
    anterior = memoria_usuarios.get(chat_id)
    for modelo in (MODELO_PRINCIPAL, MODELO_RESPALDO):
        try:
            argumentos = {"model": modelo, "input": prompt}
            if anterior:
                argumentos["previous_interaction_id"] = anterior
            respuesta = cliente_ia.interactions.create(
                **argumentos
            )
            texto_ia = respuesta.output_text
            if not texto_ia:
                raise ValueError("Respuesta sin texto.")
            if respuesta.id:
                memoria_usuarios[chat_id] = respuesta.id
            return texto_ia
        except Exception as error:
            print(f"⚠️ {modelo}: {type(error).__name__}")
    return (
        "⚠️ Gemini no respondió en este momento. "
        "Puede continuar con /comprar o /mis_pedidos."
    )

def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto
                    if unicodedata.category(c) != "Mn")
    return texto.strip().rstrip(".!?")

def mensaje_pedido(p):
    return (
        f"📋 Control de pedidos del cliente — {p['cliente']}\n"
        f"✅ Pedido {p['consecutivo']} registrado.\n"
        f"📚 Libro: {p['nombre_producto']}\n"
        f"Cantidad: {p['cantidad']}\n"
        f"Unitario: {pesos(p['precio_unitario'])}\n"
        f"Total: {pesos(p['total'])}\n"
        f"📌 Estado: {p['estado']}\n"
        "Para consultar sus pedidos: /mis_pedidos\n"
        "Si está PENDIENTE o EN_ESPERA, puede cancelar con: "
        f"/cancelar {p['consecutivo']}"
    )

# ========================================================
# F. ATENDER SEGUIMIENTO, COMPRA Y CANTIDAD
# ========================================================
def procesar(mensaje, update_id, cliente_ia=None):
    chat_id = mensaje["chat"]["id"]
    if mensaje["chat"].get("type", "private") != "private":
        return "Abra el chat privado con el bot de la librería."
    texto = mensaje.get("text", "").strip()
    partes = texto.split()
    comando = partes[0].split("@")[0].lower() if partes else ""

    seguimiento = comando == "/mis_pedidos" or (
        comando == "/start" and [p.lower() for p in partes[1:]] == ["mis_pedidos"]
    )
    if seguimiento:
        propios = consultar(chat_id=chat_id)
        if not propios:
            return (
                f"📋 Control de pedidos del cliente — "
                f"{mensaje.get('from', {}).get('first_name') or 'Cliente'}\n"
                "Aún no ha realizado pedidos. "
                "Abra un libro desde la Landing y escriba "
                "/comprar para realizar su primer pedido."
            )
        nombre_cliente = propios[-1]["cliente"]
        lineas = [f"📋 Control de pedidos del cliente — {nombre_cliente}",
                  "Sus últimos pedidos:"]
        for p in propios[-10:]:
            lineas.append(
                f"{p['consecutivo']} | {p['estado']} | "
                f"{pesos(p['total'])}"
            )
        lineas.append("Detalle: /pedido " + propios[-1]["consecutivo"])
        lineas.append("Cancelar solo si está PENDIENTE o EN_ESPERA: /cancelar PED-0001")
        return "\n".join(lineas)
    if comando == "/pedido":
        if len(partes) != 2:
            return "Escriba /pedido seguido del consecutivo."
        propios = consultar("pedido", partes[1].upper(), chat_id)
        if not propios:
            return "No hay un pedido suyo con ese consecutivo."
        return mensaje_pedido(propios[0])

    con = conectar()
    try:
        previo = con.execute(
            "SELECT * FROM pedidos WHERE telegram_update_id = ?",
            (update_id,)
        ).fetchone()
    finally:
        con.close()
    if previo:
        return mensaje_pedido(dict(previo))

    if comando == "/start":
        print("🔎 Deep Link recibido:", texto)
        memoria_usuarios.pop(chat_id, None)
        nombre = mensaje.get("from", {}).get("first_name") or "cliente"
        if len(partes) == 1:
            sesiones.pop(chat_id, None)
            return (
                f"👋 ¡Hola, {nombre}! 📚\n"
                "Bienvenido a la Librería MVP, donde encontrarás "
                "los mejores libros y publicaciones. 📖\n\n"
                "💬 ¿Sobre qué libro deseas información? "
                "Selecciona uno desde la Landing para comenzar.\n"
                "📋 Para consultar tus pedidos escribe /mis_pedidos."
            )
        codigo = partes[1].upper() if len(partes) == 2 else ""
        if codigo not in PRODUCTOS:
            sesiones.pop(chat_id, None)
            return "⚠️ Libro no disponible. Elija otro libro en la Landing."
        sesiones[chat_id] = {"codigo": codigo, "etapa": "consulta"}
        p = PRODUCTOS[codigo]
        return (
            f"👋 ¡Hola, {nombre}! 📚\n"
            f"Has solicitado información sobre el libro "
            f"{codigo} — {p['nombre']}.\n\n"
            f"📖 {p['descripcion']}\n"
            f"💰 Precio: {pesos(p['precio'])}\n\n"
            "💬 Puedes hacerme preguntas o escribir /comprar "
            "para realizar tu pedido.\n"
            "📋 Consulta tus pedidos con /mis_pedidos."
        )

    if comando == "/cancelar":
        if len(partes) == 2:
            consecutivo = partes[1].upper()
            try:
                pedido, error = cancelar_pedido_cliente(consecutivo, chat_id)
            except sqlite3.Error:
                return "⚠️ No pude actualizar el pedido. Inténtalo de nuevo más tarde."
            if error:
                return "❌ " + error
            sesiones.pop(chat_id, None)
            ok, detalle = notificar_estado_telegram(pedido)
            if not ok:
                print("⚠️ No se pudo notificar la cancelación:", detalle)
            return f"✅ El pedido {consecutivo} fue cancelado.\nEstado: CANCELADO."
        if len(partes) > 2:
            return "Escribe /cancelar PED-0001 para cancelar un pedido guardado."
        sesiones.pop(chat_id, None)
        return "Solicitud de compra cancelada. Para cancelar un pedido guardado, escribe /cancelar PED-0001."

    sesion = sesiones.get(chat_id)
    if not sesion:
        return "Seleccione primero un libro desde la Landing Page."

    intenciones = {"quiero comprar", "quiero pedir", "comprar"}
    if comando == "/comprar" or normalizar(texto) in intenciones:
        sesion["etapa"] = "cantidad"
        return "¿Cuántas unidades desea? Escriba un número de 1 a 999."

    if sesion["etapa"] == "cantidad":
        try:
            cantidad = validar_cantidad(texto)
        except ValueError as error:
            return str(error) + " Para salir, escriba /cancelar."
        usuario = mensaje.get("from", {})
        cliente = usuario.get("first_name") or str(chat_id)
        pedido = guardar_pedido(
            cliente, chat_id, sesion["codigo"], cantidad, update_id
        )
        sesion["etapa"] = "consulta"
        mostrar([pedido])
        return mensaje_pedido(pedido)

    return responder_gemini(
        cliente_ia, chat_id, sesion["codigo"], texto
    )

# ========================================================
# G. CONEXIÓN CON TELEGRAM
# ========================================================
def ejecutar_bot():
    try:
        from dotenv import load_dotenv
        from google import genai
        from telegram import ReplyKeyboardMarkup
        from telegram.ext import Application, MessageHandler
        from telegram.ext import filters
    except ImportError as error:
        print("📦 Falta una librería de comunicación:", error.name)
        marca = "LIBRERIA_DEPENDENCIAS_REINTENTO"
        if os.environ.get(marca) == "1":
            print("❌ La librería sigue sin cargar después de instalar.")
            return

        import subprocess
        carpeta_libs = BASE / "libs"
        carpeta_libs.mkdir(parents=True, exist_ok=True)

        comando = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            "--target", str(carpeta_libs),
            "python-dotenv", "google-genai", "python-telegram-bot"
        ]
        print("📥 Instalando librerías en:", carpeta_libs)
        try:
            subprocess.run(comando, check=True)
        except (subprocess.CalledProcessError, OSError):
            print("❌ No fue posible completar la instalación.")
            return

        entorno = os.environ.copy()
        entorno[marca] = "1"
        print("✅ Instalación terminada. Reiniciando el bot...")
        try:
            subprocess.run(
                [sys.executable, str(BASE / __file__)],
                env=entorno, check=False
            )
        except KeyboardInterrupt:
            pass
        return

    load_dotenv(BASE / ".env")
    token = os.getenv("TELEGRAM_TOKEN")
    clave = os.getenv("GEMINI_API_KEY")
    if not token or not clave:
        print("❌ No se encontraron las dos variables de .env.")
        return
    cliente_ia = genai.Client(api_key=clave)

    async def atender(update, context):
        mensaje = update.effective_message
        if mensaje is None or mensaje.text is None:
            return
        try:
            respuesta = procesar(
                mensaje.to_dict(), update.update_id, cliente_ia
            )
        except (sqlite3.Error, ValueError):
            print("❌ No fue posible procesar el pedido en SQLite.")
            respuesta = (
                "⚠️ No pude confirmar esta operación. "
                "Consulte /mis_pedidos antes de intentar otra compra."
            )
        teclado = ReplyKeyboardMarkup(
            [["/mis_pedidos"]], resize_keyboard=True
        )
        await mensaje.reply_text(
            respuesta[:4000], reply_markup=teclado
        )

    async def listo(app):
        print("✅ Telegram conectado: @" + app.bot.username)
        print("📡 Esperando mensajes. Para detener: Ctrl+C")

    async def informar_error(update, context):
        nombre = type(context.error).__name__
        print("❌ Telegram:", nombre)

    print("\n📚 ASISTENTE LIBRERÍA MVP")
    print("✅ Credenciales cargadas")
    print("🧠 Gemini Interactions API")
    print("🤖 Modelo principal:", MODELO_PRINCIPAL)
    print("🛟 Modelo de respaldo:", MODELO_RESPALDO)
    
    app = Application.builder().token(token).post_init(listo).build()
    app.add_handler(MessageHandler(filters.TEXT, atender))
    app.add_error_handler(informar_error)
    app.run_polling(drop_pending_updates=False)

# ========================================================
# H. INICIAR EL BOT
# ========================================================
def main():
    try:
        if not PRODUCTOS:
            raise ValueError("El catálogo está vacío.")
        for codigo, producto in PRODUCTOS.items():
            if (not producto.get("nombre") or
                    type(producto.get("precio")) is not int or
                    producto["precio"] <= 0):
                raise ValueError("Revise nombre y precio: " + codigo)
        print("✅ Carpeta del proyecto:", BASE)
        print("✅ SQLite disponible:", sqlite3.sqlite_version)
        print("📚 Libros cargados:", len(PRODUCTOS))
        iniciar_db()
    except (sqlite3.Error, ValueError) as error:
        print("❌ No se pudo preparar el proyecto:", error)
        return

    ejecutar_bot()

if __name__ == "__main__":
    main()