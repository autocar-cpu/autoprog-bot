"""Servidor del bot de WhatsApp de Autoprog.

GHL (workflow "cliente respondió" → webhook) avisa aquí cada mensaje entrante. El servidor espera unos
segundos por si el cliente manda varios mensajes seguidos, lee la conversación completa desde GHL,
piensa con Claude y responde (o deja borrador, según MODO)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

import cerebro
import config
import ghl
import regiones
from cotizador import cotizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="Autoprog bot")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_estado: dict[str, dict] = {}                # contact_id -> {"tarea", "fase": esperando|atendiendo, "repetir"}
_barrido_visto: dict[str, float] = {}         # conversación -> lastMessageDate ya revisado por el barrido
_procesados: set[str] = set()                 # ids de mensajes entrantes ya atendidos
_enviados_bot: set[str] = set()               # ids de mensajes que mandó el bot
_bitacora: list[dict] = []                    # últimas respuestas (para revisar)
_eventos: list[dict] = []                     # diagnóstico: qué pasó con cada aviso


def _ev(**kw):
    kw["t"] = dt.datetime.now(ZoneInfo(config.ZONA_HORARIA)).strftime("%d-%m %H:%M:%S")
    _eventos.append(kw)
    del _eventos[:-300]
    log.info("EVENTO %s", json.dumps(kw, ensure_ascii=False)[:500])   # queda en los logs de Railway


def _clave_ok(k: str | None) -> bool:
    return bool(config.WEBHOOK_SECRET) and k == config.WEBHOOK_SECRET


@app.get("/")
def salud():
    return {"ok": True, "modo": config.MODO, "modelo": config.MODELO,
            "ghl": bool(config.GHL_TOKEN), "claude": bool(config.ANTHROPIC_API_KEY)}


@app.get("/cotizar")
def probar_cotizacion(modelo: str, anio: int, lugar: str, situacion: str = "duplicado", tipo_llave: str = ""):
    return cotizar(modelo, anio, lugar, situacion, tipo_llave).dict()


# ------------------------------------------------------------------ webhook de GHL
@app.post("/webhook/ghl")
async def webhook(request: Request, k: str | None = None):
    if not _clave_ok(k or request.headers.get("x-autoprog-key")):
        raise HTTPException(401)
    try:
        datos = await request.json()
    except Exception:
        datos = {}
    contact_id = (datos.get("contact_id") or datos.get("contactId")
                  or (datos.get("contact") or {}).get("id"))
    _ev(paso="webhook", contacto=contact_id, claves=list(datos)[:15])
    if not contact_id:
        log.warning("webhook sin contacto: %s", json.dumps(datos)[:300])
        return {"ok": False, "motivo": "sin contacto"}
    if config.MODO == "apagado":
        return {"ok": True, "modo": "apagado"}
    _disparar(contact_id, config.ESPERA_SEGUNDOS, "webhook")
    return {"ok": True}


def _disparar(contact_id: str, espera: float, origen: str):
    """Programa la atención de un contacto.
    - Si está esperando (juntando mensajes), reinicia la espera.
    - Si ya está pensando/respondiendo, NO lo corta: marca que hay que repasar al terminar
      (antes se cancelaba a medio pensar y el mensaje nuevo quedaba como 'ya procesado' sin respuesta)."""
    e = _estado.get(contact_id)
    if e and not e["tarea"].done():
        if e["fase"] == "esperando":
            e["tarea"].cancel()
        else:
            e["repetir"] = True
            _ev(paso="en curso: se repasa al terminar", contacto=contact_id, origen=origen)
            return
    e = {"fase": "esperando", "repetir": False}
    e["tarea"] = asyncio.create_task(_ciclo(contact_id, e, espera))
    _estado[contact_id] = e


async def _ciclo(contact_id: str, e: dict, espera: float):
    try:
        await asyncio.sleep(espera)
        for _ in range(3):
            e["fase"], e["repetir"] = "atendiendo", False
            await atender(contact_id)
            if not e["repetir"]:
                break
            e["fase"] = "esperando"
            await asyncio.sleep(config.ESPERA_SEGUNDOS)
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        import traceback
        import re as _re
        limpio = lambda x: _re.sub(r"(Bearer|pit-|sk-ant-)\S*", "[oculto]", x)
        _ev(paso="error", contacto=contact_id, error=limpio(f"{type(ex).__name__}: {ex}")[:300],
            donde=limpio(traceback.format_exc()[-600:]))
        log.exception("error atendiendo %s", contact_id)
    finally:
        if _estado.get(contact_id) is e:
            _estado.pop(contact_id, None)


async def _esperar_y_atender(contact_id: str):
    """Compatibilidad: atender ya (lo usa /reintentar)."""
    _disparar(contact_id, 0, "manual")


def _historial(msgs: list[dict]) -> list[dict]:
    tz = ZoneInfo(config.ZONA_HORARIA)
    out = []
    for m in sorted(msgs, key=lambda x: x.get("dateAdded", "")):
        if m.get("messageType") != "TYPE_WHATSAPP":
            continue
        adj = m.get("attachments") or []
        imgs = [a for a in adj if any(x in a.lower() for x in (".jpg", ".jpeg", ".png", ".webp", "image"))]
        audio = any(any(x in a.lower() for x in (".ogg", ".opus", ".mp3", ".m4a", "audio")) for a in adj)
        try:
            f = dt.datetime.fromisoformat(m["dateAdded"].replace("Z", "+00:00")).astimezone(tz).strftime("%d-%m %H:%M")
        except Exception:
            f = ""
        out.append({"id": m.get("id"), "rol": "cliente" if m.get("direction") == "inbound" else "jesus",
                    "texto": m.get("body", ""), "fecha": f, "imagenes": imgs, "audio": audio,
                    "dateAdded": m.get("dateAdded", ""),
                    "api": bool((m.get("meta") or {}).get("marketplace"))})
    return out


def _es_bot_viejo(texto: str) -> bool:
    """Mensajes del bot anterior de GHL (firmaba así); no cuentan como atención manual de Jesús."""
    t = texto or ""
    return "AUTOPROG SPA" in t and ("Ing. Jesús Perdomo" in t or "Jesús David" in t)


async def atender(contact_id: str):
    contacto = await ghl.contacto(contact_id)
    etiquetas = [t.lower() for t in contacto.get("tags", [])]
    if config.ETIQUETA_BOT_OFF in etiquetas or config.ETIQUETA_ESCALADO in etiquetas:
        _ev(paso="omitido", contacto=contact_id, motivo="etiqueta bot-off/escalado")
        return
    conv = None
    for intento in range(5):        # GHL tarda en indexar las conversaciones nuevas (leads recién llegados)
        conv = await ghl.buscar_conversacion(contact_id)
        if conv:
            break
        await asyncio.sleep(6)
    if not conv:
        _ev(paso="omitido", contacto=contact_id, motivo="sin conversación (GHL no la encuentra aún; el barrido la reintenta)")
        return
    hist = _historial(await ghl.mensajes(conv))
    if not hist or hist[-1]["rol"] != "cliente":
        _ev(paso="omitido", contacto=contact_id, motivo="último mensaje no es del cliente", n=len(hist))
        return
    if hist[-1]["id"] in _procesados:
        _ev(paso="omitido", contacto=contact_id, motivo="ya procesado")
        return
    # ¿Jesús escribió a mano hace poco? Entonces no se mete.
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=config.SILENCIO_SI_HUMANO_HORAS)
    for m in hist:
        if m["rol"] == "jesus" and m["id"] not in _enviados_bot and m["dateAdded"] \
                and not m.get("api") and not _es_bot_viejo(m.get("texto", "")):
            if dt.datetime.fromisoformat(m["dateAdded"].replace("Z", "+00:00")) > limite:
                _ev(paso="omitido", contacto=contact_id, motivo="Jesús atendiendo", texto=m["texto"][:80])
                return

    nombre = contacto.get("firstName") or contacto.get("contactName") or ""
    for _ in range(3):
        ctx = cerebro.Contexto(contact_id, nombre, hist, simulacion=False)
        res = await cerebro.pensar(ctx)
        # ¿El cliente escribió algo más mientras se pensaba? Entonces se vuelve a pensar con todo.
        nuevo = _historial(await ghl.mensajes(conv))
        ids_antes = {m["id"] for m in hist}
        if any(m["rol"] == "cliente" and m["id"] not in ids_antes for m in nuevo):
            _ev(paso="repensar", contacto=contact_id, motivo="llegó otro mensaje mientras pensaba")
            hist = nuevo
            continue
        break
    _bitacora.append({"t": time.time(), "contacto": contact_id, "nombre": nombre,
                      "ultimo_cliente": hist[-1]["texto"][:200], "respuesta": res.mensajes,
                      "imagen": res.imagen, "escalado": res.escalado, "acciones": res.acciones,
                      "problemas": res.problemas, "modelo": res.modelo_usado})
    del _bitacora[:-200]
    await _aplicar(contact_id, nombre, res, conv)
    for m in hist:                 # recién ahora queda como atendido
        if m["rol"] == "cliente":
            _procesados.add(m["id"])
    _ev(paso="respondido" if res.mensajes else "sin respuesta (decisión del bot)", contacto=contact_id,
        escalado=bool(res.escalado), n_mensajes=len(res.mensajes))


ACUSE = __import__("re").compile(r"^\W*(ok+|oki|okey|okay|vale|dale|listo|ya|gracias|muchas gracias|perfecto|bueno|"
                                 r"de acuerdo|genial|excelente|si|sí)\W*$", __import__("re").I)


async def _barrido():
    """Red de seguridad: cada pocos minutos busca chats cuyo último mensaje es del cliente y nadie respondió
    (webhook que no llegó, GHL que no encontraba la conversación, caída del servidor) y los atiende."""
    await asyncio.sleep(30)
    while True:
        try:
            if config.MODO == "activo":
                ahora = time.time() * 1000
                for c in await ghl.conversaciones_entrantes_recientes():
                    edad_min = (ahora - (c.get("lastMessageDate") or 0)) / 60000
                    etiquetas = [t.lower() for t in c.get("tags") or []]
                    if not (1.5 <= edad_min <= config.BARRIDO_HORAS * 60):
                        continue
                    if c.get("lastMessageType") != "TYPE_WHATSAPP" or c.get("lastMessageDirection") != "inbound":
                        continue
                    if config.ETIQUETA_BOT_OFF in etiquetas or config.ETIQUETA_ESCALADO in etiquetas:
                        continue
                    if ACUSE.match(c.get("lastMessageBody") or "x"):
                        continue        # "ok", "vale", "gracias": no requiere respuesta
                    if _barrido_visto.get(c["id"]) == c.get("lastMessageDate") or c["contactId"] in _estado:
                        continue
                    _barrido_visto[c["id"]] = c.get("lastMessageDate")
                    _ev(paso="barrido rescata", contacto=c["contactId"], minutos=int(edad_min),
                        ultimo=(c.get("lastMessageBody") or "")[:60])
                    _disparar(c["contactId"], 0, "barrido")
                    await asyncio.sleep(2)
        except Exception as ex:
            log.exception("barrido: %s", ex)
        await asyncio.sleep(config.BARRIDO_SEGUNDOS)


@app.on_event("startup")
async def _arrancar_barrido():
    asyncio.create_task(_barrido())


async def _aplicar(contact_id: str, nombre: str, res: cerebro.Resultado, conv: str = ""):
    archivo = config.IMAGENES.get(res.imagen) if res.imagen else None
    if config.MODO == "borrador":
        txt = "BORRADOR DEL BOT:\n" + "\n---\n".join(res.mensajes or ["(sin respuesta)"])
        if archivo:
            txt += f"\n[imagen: {res.imagen}]"
        if res.escalado:
            txt += f"\n[escalaría: {res.escalado['motivo']}]"
        await ghl.nota_interna(contact_id, txt)
        return

    # MODO activo: mensajes como persona, con pausa entre cada uno; la foto va antes del último mensaje.
    url_img = None
    if archivo:
        subidas = await ghl.subir_adjunto(conv, archivo) if conv else []
        url_img = subidas[0] if subidas else await ghl.url_imagen(archivo)
        if not url_img:
            _ev(paso="aviso", contacto=contact_id, motivo=f"no se pudo preparar la imagen {archivo}")
    msgs = list(res.mensajes)
    for i, m in enumerate(msgs):
        if url_img and i == len(msgs) - 1:
            mid = await ghl.enviar_imagen_whatsapp(contact_id, url_img, archivo)
            if mid:
                _enviados_bot.add(mid)
            await asyncio.sleep(2)
        mid = await ghl.enviar_whatsapp(contact_id, m)
        if mid:
            _enviados_bot.add(mid)
        await asyncio.sleep(1.5)
    if url_img and not msgs:
        mid = await ghl.enviar_imagen_whatsapp(contact_id, url_img, archivo)
        if mid:
            _enviados_bot.add(mid)

    marca = next((a["guardar_datos"].get("marca") for a in res.acciones if "guardar_datos" in a), None)
    pipeline = config.pipeline_por_marca(marca)
    precios = [a for a in res.acciones if a.get("precio")]
    agendo = any("agendar" in a for a in res.acciones)
    cupo = any("reservar_cupo" in a for a in res.acciones)
    en_region = bool(res.region and res.region.get("tipo") == "region")
    if en_region and not res.escalado:
        # Cliente de regiones: va a la columna de su ruta en VIAJES REGIONES, con el precio si ya lo tiene.
        valor = precios[-1]["precio"] if precios else None
        etapa = regiones.ETAPA_RUTA.get(res.region.get("bloque", ""), "")
        try:
            movido = bool(etapa) and await ghl.mover_a_viajes(contact_id, nombre, etapa, valor)
            if cupo:
                await ghl.agregar_etiquetas(contact_id, ["cupo-reservado"])
        except Exception:
            log.exception("no se pudo mover a viajes")
            movido = False
        if not movido and precios:
            await ghl.mover_oportunidad(contact_id, nombre, pipeline, "Cotizado", valor)
    elif precios and not agendo and not res.escalado:
        await ghl.mover_oportunidad(contact_id, nombre, pipeline, "Cotizado", precios[-1]["precio"])
    if res.escalado:
        await ghl.agregar_etiquetas(contact_id, [config.ETIQUETA_ESCALADO])
        await ghl.crear_tarea(contact_id, f"Bot escaló: {res.escalado['motivo']}"[:120], res.escalado["resumen"])
        try:
            await ghl.mover_oportunidad(contact_id, nombre, pipeline, "Escalado a Jesús")
        except Exception:
            log.exception("no se pudo mover la oportunidad")


# ------------------------------------------------------------------ pruebas y revisión
@app.post("/simular")
async def simular(request: Request, k: str | None = None):
    """Prueba el cerebro con una conversación inventada, sin tocar GHL."""
    if not _clave_ok(k):
        raise HTTPException(401)
    d = await request.json()
    ctx = cerebro.Contexto("simulacion", d.get("nombre", ""), d["historial"], simulacion=True)
    r = await cerebro.pensar(ctx)
    return r.__dict__


@app.get("/prueba")
async def prueba(k: str, texto: str, nombre: str = "", previo: str = ""):
    """Versión GET para pruebas rápidas. 'previo' = turnos anteriores separados por ' || ',
    alternando cliente / Jesús, empezando por el cliente."""
    if not _clave_ok(k):
        raise HTTPException(401)
    hist = []
    turnos = [t for t in previo.split(" || ") if t.strip()] if previo else []
    for i, t in enumerate(turnos):
        hist.append({"rol": "cliente" if i % 2 == 0 else "jesus", "texto": t})
    hist.append({"rol": "cliente", "texto": texto})
    r = await cerebro.pensar(cerebro.Contexto("simulacion", nombre, hist, simulacion=True))
    return r.__dict__


@app.get("/bitacora")
def bitacora(k: str, n: int = 30):
    if not _clave_ok(k):
        raise HTTPException(401)
    return _bitacora[-n:]


# ------------------------------------------------------------------ batería de pruebas
_lote: dict = {"estado": "sin correr", "resultados": []}


def _evaluar(caso: dict, r: cerebro.Resultado) -> list[str]:
    import re as _re
    e, fallas = caso["esperado"], []
    texto = " ".join(r.mensajes)
    montos = {int(x.replace(".", "")) for x in _re.findall(r"\$\s?(\d{1,3}(?:\.\d{3})+)", texto)}
    if e.get("precio") and e["precio"] not in montos:
        fallas.append(f"esperaba precio {e['precio']}, dijo {sorted(montos) or 'nada'}")
    if e.get("sin_precio") and montos:
        fallas.append(f"no debía dar precio, dijo {sorted(montos)}")
    if e.get("escala") and not r.escalado:
        fallas.append("debía escalar a Jesús y no lo hizo")
    if not e.get("escala") and r.escalado:
        fallas.append(f"escaló sin necesidad: {r.escalado.get('motivo')}")
    if e.get("imagen") and r.imagen != e["imagen"]:
        fallas.append(f"esperaba imagen {e['imagen']}, mandó '{r.imagen}'")
    for p in e.get("no_contiene", []):
        if p.lower() in texto.lower():
            fallas.append(f"contiene '{p}'")
    if r.problemas:
        fallas.append("revisor: " + "; ".join(r.problemas))
    estilo = {"usted": r"\busted\b", "estimado": r"estimad[oa]", "gracias por escribir": r"gracias por escribir",
              "firma": r"AUTOPROG SPA|— ?Ing|Jesús David /", "nombre técnico": r"\b(CAS\d?|FEM|BDC\d?|EWS\d?|BCP|AKL)\b",
              "con gusto": r"con gusto|quedo atento|quedamos atentos"}
    for k, pat in estilo.items():
        if _re.search(pat, texto, _re.I):
            fallas.append(f"estilo: {k}")
    if len(r.mensajes) > 3:
        fallas.append(f"estilo: {len(r.mensajes)} mensajes")
    return fallas


async def _correr_lote(modelo: str | None = None):
    from casos_prueba import CASOS
    _lote.update(estado="corriendo", resultados=[], inicio=time.time())
    for caso in CASOS:
        hist = [{"rol": "cliente" if q == "c" else "jesus", "texto": t} for q, t in caso["hist"]]
        try:
            r = await cerebro.pensar(cerebro.Contexto("simulacion", caso["nombre"], hist, simulacion=True), modelo)
            fallas = _evaluar(caso, r)
            _lote["resultados"].append({"caso": caso["id"], "ok": not fallas, "fallas": fallas,
                                        "respuesta": r.mensajes, "imagen": r.imagen,
                                        "escalado": r.escalado, "modelo": r.modelo_usado})
        except Exception as ex:
            _lote["resultados"].append({"caso": caso["id"], "ok": False, "fallas": [f"error: {ex}"]})
    ok = sum(1 for x in _lote["resultados"] if x["ok"])
    _lote.update(estado="terminado", resumen=f"{ok}/{len(_lote['resultados'])} casos OK",
                 segundos=int(time.time() - _lote["inicio"]))


@app.get("/lote/iniciar")
async def lote_iniciar(k: str, modelo: str | None = None):
    if not _clave_ok(k):
        raise HTTPException(401)
    if _lote.get("estado") == "corriendo":
        return {"estado": "ya está corriendo"}
    asyncio.create_task(_correr_lote(modelo))
    return {"estado": "iniciado"}


@app.get("/lote/resultado")
def lote_resultado(k: str, solo_fallas: bool = False):
    if not _clave_ok(k):
        raise HTTPException(401)
    out = dict(_lote)
    if solo_fallas:
        out["resultados"] = [x for x in out.get("resultados", []) if not x["ok"]]
    return out


@app.get("/diag")
def diag(k: str, n: int = 30):
    if not _clave_ok(k):
        raise HTTPException(401)
    return _eventos[-n:]


@app.get("/reintentar")
async def reintentar(k: str, contacto: str):
    """Fuerza que el bot atienda ahora a un contacto (para recuperar un mensaje que falló)."""
    if not _clave_ok(k):
        raise HTTPException(401)
    _ev(paso="reintento manual", contacto=contacto)
    asyncio.create_task(_esperar_y_atender(contacto))
    return {"ok": True}


@app.get("/preparar_imagenes")
async def preparar_imagenes(k: str):
    """Sube (una vez) las láminas del catálogo a GHL y muestra sus URLs."""
    if not _clave_ok(k):
        raise HTTPException(401)
    out = {n: await ghl.url_imagen(a) for n, a in config.IMAGENES.items()}
    out["error"] = ghl.ultimo_error_media
    return out



@app.get("/probar_foto")
async def probar_foto(k: str, contacto: str, imagen: str = "catalogo_general", texto: str = "Prueba de lámina del catálogo",
                      origen: str = "adjunto"):
    """Envía una lámina de prueba a un contacto (solo para pruebas con el número de Jesús)."""
    if not _clave_ok(k):
        raise HTTPException(401)
    conv = await ghl.buscar_conversacion(contacto)
    if origen == "adjunto":
        subidas = await ghl.subir_adjunto(conv, config.IMAGENES[imagen])
    elif origen == "biblioteca":
        subidas = [config.IMAGENES_GHL[config.IMAGENES[imagen]]]
    else:
        subidas = [f"{config.URL_PUBLICA}/static/{config.IMAGENES[imagen]}"]
    mid = None
    if subidas:
        mid = await ghl.enviar_imagen_whatsapp(contacto, subidas[0], config.IMAGENES[imagen])
        if texto:
            await ghl.enviar_whatsapp(contacto, texto)
    return {"subidas": subidas, "mensaje": mid, "error": ghl.ultimo_error_media}
