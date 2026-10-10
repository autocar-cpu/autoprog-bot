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
from cotizador import cotizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="Autoprog bot")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_pendientes: dict[str, asyncio.Task] = {}     # contact_id -> tarea con espera
_procesados: set[str] = set()                 # ids de mensajes entrantes ya atendidos
_enviados_bot: set[str] = set()               # ids de mensajes que mandó el bot
_bitacora: list[dict] = []                    # últimas respuestas (para revisar)


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
    if not contact_id:
        log.warning("webhook sin contacto: %s", json.dumps(datos)[:300])
        return {"ok": False, "motivo": "sin contacto"}
    if config.MODO == "apagado":
        return {"ok": True, "modo": "apagado"}
    tarea = _pendientes.pop(contact_id, None)
    if tarea and not tarea.done():
        tarea.cancel()                      # llegó otro mensaje: reinicia la espera
    _pendientes[contact_id] = asyncio.create_task(_esperar_y_atender(contact_id))
    return {"ok": True}


async def _esperar_y_atender(contact_id: str):
    try:
        await asyncio.sleep(config.ESPERA_SEGUNDOS)
        await atender(contact_id)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("error atendiendo %s", contact_id)
    finally:
        if _pendientes.get(contact_id) is asyncio.current_task():
            _pendientes.pop(contact_id, None)


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
                    "dateAdded": m.get("dateAdded", "")})
    return out


def _es_bot_viejo(texto: str) -> bool:
    """Mensajes del bot anterior de GHL (firmaba así); no cuentan como atención manual de Jesús."""
    t = texto or ""
    return "AUTOPROG SPA" in t and ("Ing. Jesús Perdomo" in t or "Jesús David" in t)


async def atender(contact_id: str):
    contacto = await ghl.contacto(contact_id)
    etiquetas = [t.lower() for t in contacto.get("tags", [])]
    if config.ETIQUETA_BOT_OFF in etiquetas or config.ETIQUETA_ESCALADO in etiquetas:
        log.info("contacto %s con bot apagado/escalado", contact_id)
        return
    conv = await ghl.buscar_conversacion(contact_id)
    if not conv:
        return
    hist = _historial(await ghl.mensajes(conv))
    if not hist or hist[-1]["rol"] != "cliente":
        return                               # el último mensaje ya no es del cliente
    if hist[-1]["id"] in _procesados:
        return
    # ¿Jesús escribió a mano hace poco? Entonces no se mete.
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=config.SILENCIO_SI_HUMANO_HORAS)
    for m in hist:
        if m["rol"] == "jesus" and m["id"] not in _enviados_bot and m["dateAdded"] \
                and not _es_bot_viejo(m.get("texto", "")):
            if dt.datetime.fromisoformat(m["dateAdded"].replace("Z", "+00:00")) > limite:
                log.info("Jesús está atendiendo a %s; el bot no responde", contact_id)
                return
    for m in hist:
        if m["rol"] == "cliente":
            _procesados.add(m["id"])

    nombre = contacto.get("firstName") or contacto.get("contactName") or ""
    ctx = cerebro.Contexto(contact_id, nombre, hist, simulacion=False)
    res = await cerebro.pensar(ctx)
    _bitacora.append({"t": time.time(), "contacto": contact_id, "nombre": nombre,
                      "ultimo_cliente": hist[-1]["texto"][:200], "respuesta": res.mensajes,
                      "imagen": res.imagen, "escalado": res.escalado, "acciones": res.acciones,
                      "problemas": res.problemas, "modelo": res.modelo_usado})
    del _bitacora[:-200]
    await _aplicar(contact_id, nombre, res)


async def _aplicar(contact_id: str, nombre: str, res: cerebro.Resultado):
    url_img = f"{config.URL_PUBLICA}/static/{config.IMAGENES[res.imagen]}" if res.imagen else None
    if config.MODO == "borrador":
        txt = "BORRADOR DEL BOT:\n" + "\n---\n".join(res.mensajes or ["(sin respuesta)"])
        if url_img:
            txt += f"\n[imagen: {res.imagen}]"
        if res.escalado:
            txt += f"\n[escalaría: {res.escalado['motivo']}]"
        await ghl.nota_interna(contact_id, txt)
        return

    # MODO activo: mensajes como persona, con pausa entre cada uno
    msgs = list(res.mensajes)
    for i, m in enumerate(msgs):
        if url_img and i == len(msgs) - 1:
            mid = await ghl.enviar_whatsapp(contact_id, imagen_url=url_img)
            if mid:
                _enviados_bot.add(mid)
            await asyncio.sleep(2)
        mid = await ghl.enviar_whatsapp(contact_id, m)
        if mid:
            _enviados_bot.add(mid)
        await asyncio.sleep(1.5)
    if url_img and not msgs:
        mid = await ghl.enviar_whatsapp(contact_id, imagen_url=url_img)
        if mid:
            _enviados_bot.add(mid)

    marca = next((a["guardar_datos"].get("marca") for a in res.acciones if "guardar_datos" in a), None)
    pipeline = config.pipeline_por_marca(marca)
    precios = [a for a in res.acciones if a.get("precio")]
    agendo = any("agendar" in a for a in res.acciones)
    if precios and not agendo and not res.escalado:
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


async def _correr_lote():
    from casos_prueba import CASOS
    _lote.update(estado="corriendo", resultados=[], inicio=time.time())
    for caso in CASOS:
        hist = [{"rol": "cliente" if q == "c" else "jesus", "texto": t} for q, t in caso["hist"]]
        try:
            r = await cerebro.pensar(cerebro.Contexto("simulacion", caso["nombre"], hist, simulacion=True))
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
async def lote_iniciar(k: str):
    if not _clave_ok(k):
        raise HTTPException(401)
    if _lote.get("estado") == "corriendo":
        return {"estado": "ya está corriendo"}
    asyncio.create_task(_correr_lote())
    return {"estado": "iniciado"}


@app.get("/lote/resultado")
def lote_resultado(k: str, solo_fallas: bool = False):
    if not _clave_ok(k):
        raise HTTPException(401)
    out = dict(_lote)
    if solo_fallas:
        out["resultados"] = [x for x in out.get("resultados", []) if not x["ok"]]
    return out
