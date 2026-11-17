# ========================================================
# CONSULTAS.PY — LIBRERÍA MVP
# Panel local de administración: consulta y actualización de estados.
# ========================================================
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from libreria import DB, consultar, pesos, iniciar_db, cambiar_estado, notificar_estado_telegram

ESTADOS = {
    "PENDIENTE": "Pendiente",
    "EN_ESPERA": "En espera",
    "ACEPTADO": "Aceptado",
    "CANCELADO": "Cancelado",
}

ESTILOS = """
:root{color-scheme:dark;--bg:#090e18;--surface:#111a2a;--line:#344258;--ink:#f5f7fb;--muted:#b6c4d7;--blue:#008dcc;--yellow:#f5c400}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Arial,sans-serif;line-height:1.5;min-height:100vh}
header{background:#080c14;border-bottom:1px solid #263244;padding:18px 5%;display:flex;gap:14px;align-items:center}header strong{font-size:21px}header small{display:block;color:var(--muted);font-size:12px}.local{margin-left:auto;color:var(--muted);font-size:12px}
main{max-width:1700px;margin:auto;padding:34px 4% 46px}.tag{color:var(--yellow);text-transform:uppercase;letter-spacing:1.7px;font-size:12px;font-weight:bold}h1{font-size:clamp(24px,3vw,36px);margin:12px 0}p{color:var(--muted);margin:12px 0 24px}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}.box{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 20px;min-width:175px}.box span{display:block;font-size:12px;color:var(--muted)}.box strong{display:block;font-size:23px}.yellow{color:var(--yellow)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:0 16px 40px #0004}.tools{padding:20px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;border-bottom:1px solid var(--line)}nav{display:flex;gap:8px;flex-wrap:wrap}a,button{font-family:inherit;font-size:13px;font-weight:bold;text-decoration:none;border:1px solid #607087;border-radius:8px;padding:9px 12px;color:#fff;background:#18253a;cursor:pointer}a.active,button.primary{background:var(--blue);border-color:var(--blue)}button.hold{background:#665018;border-color:#9a7b16}button.cancel{background:#702d3b;border-color:#a94355}a:hover,button:hover{filter:brightness(1.15)}form.search{display:flex;align-items:end;gap:10px;flex-wrap:wrap}label{font-size:12px;color:var(--muted)}input{display:block;background:#080f1c;border:1px solid #6b7b91;border-radius:8px;color:white;padding:10px 12px;font-size:14px;width:180px;margin-top:5px}
.title-table{display:flex;justify-content:space-between;gap:12px;padding:18px 20px;align-items:center}.title-table h2{font-size:17px;margin:0}.title-table span{font-size:12px;color:var(--muted)}.table-wrap{overflow:auto;max-height:68vh}table{border-collapse:separate;border-spacing:0;width:100%;min-width:1450px;font-size:13px}th{background:#23334b;color:#fff;text-align:left;font-size:12px;white-space:nowrap;position:sticky;top:0;z-index:1;border-bottom:2px solid #52708f;padding:14px 12px}td{padding:13px 12px;border-bottom:1px solid #344258;vertical-align:middle;background:#111a2a}tbody tr:nth-child(even) td{background:#18243a}tbody tr:hover td{background:#23364f}.num{text-align:right;white-space:nowrap}.order{font-weight:bold;color:#ffda39;white-space:nowrap}.state{display:inline-block;border:1px solid #9a7b16;border-radius:6px;padding:5px 8px;background:#392f12;color:#ffe280;font-size:11px;font-weight:bold;white-space:nowrap}.state-ACEPTADO{border-color:#287d53;background:#123b2b;color:#a9f0c8}.state-CANCELADO{border-color:#a94355;background:#4a1c27;color:#ffc0cb}.state-EN_ESPERA{border-color:#8b6a25;background:#392f12;color:#ffe280}.actions{display:flex;gap:6px;flex-wrap:wrap;min-width:300px}.actions form{margin:0}.empty{text-align:center;padding:42px;color:var(--muted)}.foot{padding:14px 20px;background:#0e1726;color:var(--muted);font-size:12px}.notice{padding:12px 16px;margin:15px 0;border:1px solid #287d53;background:#123b2b;color:#c8f7dc;border-radius:10px}.error{border-color:#a94355;background:#4a1c27;color:#ffc0cb}footer{margin-top:20px;font-size:12px;color:var(--muted)}
@media(max-width:650px){main{padding:24px 14px}.local{display:none}.tools{padding:14px}.search{width:100%}input{width:100%}.title-table{align-items:start;flex-direction:column}}
"""


def pagina(tipo="todos", consecutivo="", aviso="", error=False):
    pedidos = consultar(tipo, consecutivo if tipo == "pedido" else None)
    filas = []
    for p in pedidos:
        estado = escape(str(p["estado"]))
        acciones = []
        opciones = [
            ("ACEPTADO", "Aceptar", "primary"),
            ("EN_ESPERA", "Poner en espera", "hold"),
            ("CANCELADO", "Cancelar", "cancel"),
        ]
        if p["estado"] not in ("CANCELADO",):
            for nuevo, etiqueta, clase in opciones:
                if nuevo == p["estado"]:
                    continue
                acciones.append(
                    '<form method="post" action="/estado" onsubmit="return confirm(\'¿Cambiar el estado de '
                    + escape(p["consecutivo"], quote=True)
                    + ' a ' + etiqueta + '?\')">'
                    '<input type="hidden" name="consecutivo" value="'
                    + escape(p["consecutivo"], quote=True) + '">'
                    '<input type="hidden" name="estado" value="' + nuevo + '">'
                    '<button class="' + clase + '" type="submit">' + etiqueta + '</button></form>'
                )
        filas.append(
            '<tr><td class="order">' + escape(str(p["consecutivo"])) + '</td>'
            '<td>' + escape(str(p["cliente"])) + '</td>'
            '<td>' + escape(str(p["codigo_producto"])) + '</td>'
            '<td>' + escape(str(p["nombre_producto"])) + '</td>'
            '<td class="num">' + str(p["cantidad"]) + '</td>'
            '<td class="num">' + escape(pesos(p["precio_unitario"])) + '</td>'
            '<td class="num">' + escape(pesos(p["total"])) + '</td>'
            '<td>' + escape(str(p["fecha"])) + '</td><td>' + escape(str(p["hora"])) + '</td>'
            '<td><span class="state state-' + estado + '">' + estado.replace("_", " ") + '</span></td>'
            '<td><div class="actions">' + (''.join(acciones) or '<span>Sin acciones</span>') + '</div></td></tr>'
        )
    titulos = ''.join('<th>' + t + '</th>' for t in ["Pedido", "Cliente", "Código", "Producto", "Cantidad", "Precio unitario", "Total", "Fecha", "Hora", "Estado", "Acciones"])
    cuerpo = ''.join(filas) or '<tr><td class="empty" colspan="11">No hay pedidos para esta consulta.</td></tr>'
    vista = {"todos": "Todos los pedidos", "pendientes": "Pedidos pendientes o en espera", "pedido": "Resultado de búsqueda"}.get(tipo, "Pedidos")
    mensaje = ('<div class="notice' + (' error' if error else '') + '">' + escape(aviso) + '</div>') if aviso else ''
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>LibreríaMVP | Administración de pedidos</title><style>{ESTILOS}</style></head><body>
<header><span style="font-size:30px">📚</span><div><strong>LibreríaMVP</strong><small>Panel local de administración</small></div><span class="local">Gestión de pedidos</span></header>
<main><div class="tag">Administración</div><h1>Control de pedidos</h1><p>Consulta las compras registradas y cambia su estado. Cuando se guarde un cambio, se intentará notificar al cliente por Telegram.</p>{mensaje}
<section class="summary"><div class="box"><span>Pedidos en esta vista</span><strong>{len(pedidos)}</strong></div><div class="box"><span>Valor total de esta vista</span><strong class="yellow">{escape(pesos(sum(p["total"] for p in pedidos)))}</strong></div></section>
<section class="panel"><div class="tools"><nav><a class="{'active' if tipo=='todos' else ''}" href="/?tipo=todos">Todos</a><a class="{'active' if tipo=='pendientes' else ''}" href="/?tipo=pendientes">Pendientes / en espera</a></nav>
<form class="search" method="get"><input type="hidden" name="tipo" value="pedido"><label>Buscar consecutivo<input name="consecutivo" placeholder="PED-0001" value="{escape(consecutivo, quote=True)}" required maxlength="40"></label><button class="primary" type="submit">Buscar</button></form></div>
<div class="title-table"><h2>{vista}</h2><span>{len(pedidos)} registros · Desliza horizontalmente para ver todas las columnas</span></div><div class="table-wrap" role="region" aria-label="Tabla de pedidos" tabindex="0"><table><thead><tr>{titulos}</tr></thead><tbody>{cuerpo}</tbody></table></div>
<div class="foot">Estados: PENDIENTE, EN ESPERA, ACEPTADO y CANCELADO. El panel solo escucha en este computador (127.0.0.1).</div></section><footer>Los cambios se guardan en la misma base de datos libreria.db.</footer></main></body></html>'''


class ConsultaLocal(BaseHTTPRequestHandler):
    def responder_html(self, contenido, codigo=200):
        datos = contenido.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        ruta = urlparse(self.path)
        if ruta.path in ("/img/favico.ico", "/favicon.ico"):
            icono = DB.parent / "img" / "favico.ico"
            try:
                contenido = icono.read_bytes()
            except OSError:
                self.send_error(404, "No se encontró el favicon")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Content-Length", str(len(contenido)))
            self.end_headers()
            self.wfile.write(contenido)
            return
        if ruta.path != "/":
            self.send_error(404)
            return
        params = parse_qs(ruta.query)
        tipo = params.get("tipo", ["todos"])[0]
        codigo = params.get("consecutivo", [""])[0].strip().upper()
        if tipo not in ("todos", "pendientes", "pedido"):
            self.send_error(400, "Consulta desconocida")
            return
        if tipo == "pedido" and not codigo:
            self.send_error(400, "Escriba un consecutivo")
            return
        try:
            self.responder_html(pagina(tipo, codigo))
        except Exception as error:
            print("Error consultando pedidos:", type(error).__name__)
            self.send_error(503, "Revise la base de datos local")

    def do_POST(self):
        if urlparse(self.path).path != "/estado":
            self.send_error(404)
            return
        try:
            longitud = int(self.headers.get("Content-Length", "0"))
            if longitud < 1 or longitud > 4096:
                self.send_error(400, "Solicitud inválida")
                return
            datos = parse_qs(self.rfile.read(longitud).decode("utf-8"))
            consecutivo = datos.get("consecutivo", [""])[0].strip().upper()
            estado = datos.get("estado", [""])[0].strip().upper()
            if not consecutivo or estado not in ESTADOS or estado == "PENDIENTE":
                self.send_error(400, "Pedido o estado inválido")
                return
            pedido, cambio = cambiar_estado(consecutivo, estado)
            if not cambio:
                aviso = f"El pedido {consecutivo} ya estaba en estado {estado}. No se envió otra notificación."
                self.responder_html(pagina("todos", aviso=aviso))
                return
            ok, detalle = notificar_estado_telegram(pedido)
            if ok:
                aviso = f"Pedido {consecutivo} actualizado a {estado}. Se notificó al cliente por Telegram."
                error = False
            else:
                aviso = f"Pedido {consecutivo} actualizado a {estado}, pero no se pudo enviar la notificación por Telegram ({detalle})."
                error = True
                print("Aviso Telegram:", detalle)
            self.responder_html(pagina("todos", aviso=aviso, error=error))
        except ValueError as error:
            self.responder_html(pagina("todos", aviso=str(error), error=True), 400)
        except Exception as error:
            print("Error actualizando pedido:", type(error).__name__)
            self.send_error(503, "No fue posible actualizar el pedido")


def main():
    try:
        iniciar_db()
    except Exception as error:
        print("❌ No se pudo preparar SQLite:", type(error).__name__)
        return
    try:
        servidor = HTTPServer(("127.0.0.1", 8000), ConsultaLocal)
    except OSError:
        print("❌ No fue posible abrir el puerto 8000. Compruebe si consultas.py ya está ejecutándose.")
        return
    print("✅ Panel de administración iniciado: http://127.0.0.1:8000")
    print("📌 Mantenga esta terminal abierta. Para detener: Ctrl+C.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel detenido. Los pedidos siguen guardados.")
    finally:
        servidor.server_close()

if __name__ == "__main__":
    main()
