"""Cliente mínimo de la API v2 de GoHighLevel (LeadConnector)."""
from __future__ import annotations

import datetime as dt
import logging

import httpx

import config

BASE = "https://services.leadconnectorhq.com"
log = logging.getLogger("ghl")


def _h(version: str = "2021-07-28") -> dict:
    return {"Authorization": f"Bearer {config.GHL_TOKEN}", "Version": version,
            "Accept": "application/json", "Content-Type": "application/json"}


async def _req(metodo: str, ruta: str, version: str = "2021-07-28", **kw) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(metodo, BASE + ruta, headers=_h(version), **kw)
    if r.status_code >= 400:
        log.error("GHL %s %s -> %s %s", metodo, ruta, r.status_code, r.text[:400])
        r.raise_for_status()
    return r.json() if r.content else {}


# ------------------------------------------------------------ conversaciones
async def mensajes(conversation_id: str, limite: int = 40) -> list[dict]:
    d = await _req("GET", f"/conversations/{conversation_id}/messages", "2021-04-15", params={"limit": limite})
    return d.get("messages", {}).get("messages", [])


async def buscar_conversacion(contact_id: str) -> str | None:
    d = await _req("GET", "/conversations/search", "2021-04-15",
                   params={"locationId": config.GHL_LOCATION_ID, "contactId": contact_id})
    conv = d.get("conversations") or []
    return conv[0]["id"] if conv else None


async def conversaciones_entrantes_recientes(limite: int = 60) -> list[dict]:
    """Conversaciones cuyo último mensaje es del cliente, más recientes primero."""
    d = await _req("GET", "/conversations/search", "2021-04-15",
                   params={"locationId": config.GHL_LOCATION_ID, "lastMessageDirection": "inbound",
                           "sortBy": "last_message_date", "sort": "desc", "limit": limite})
    return d.get("conversations") or []


async def enviar_whatsapp(contact_id: str, texto: str = "", imagen_url: str | None = None) -> str | None:
    cuerpo = {"type": "WhatsApp", "contactId": contact_id}
    if texto:
        cuerpo["message"] = texto
    if imagen_url:
        cuerpo["attachments"] = [imagen_url]
    d = await _req("POST", "/conversations/messages", "2021-04-15", json=cuerpo)
    return d.get("messageId") or d.get("id")


async def nota_interna(contact_id: str, texto: str) -> None:
    await _req("POST", "/conversations/messages", "2021-04-15",
               json={"type": "InternalComment", "contactId": contact_id, "message": texto})


# ------------------------------------------------------------ contactos
async def contacto(contact_id: str) -> dict:
    return (await _req("GET", f"/contacts/{contact_id}")).get("contact", {})


async def actualizar_campos(contact_id: str, valores: dict[str, str]) -> None:
    custom = [{"id": config.CAMPOS[k], "field_value": v} for k, v in valores.items()
              if k in config.CAMPOS and v not in (None, "")]
    if custom:
        await _req("PUT", f"/contacts/{contact_id}", json={"customFields": custom})


async def agregar_etiquetas(contact_id: str, etiquetas: list[str]) -> None:
    await _req("POST", f"/contacts/{contact_id}/tags", json={"tags": etiquetas})


async def crear_tarea(contact_id: str, titulo: str, detalle: str, horas: float = 1) -> None:
    vence = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=horas)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _req("POST", f"/contacts/{contact_id}/tasks",
               json={"title": titulo, "body": detalle, "dueDate": vence, "completed": False,
                     "assignedTo": config.GHL_USUARIO_JESUS})


# ------------------------------------------------------------ oportunidades
async def oportunidad_abierta(contact_id: str) -> dict | None:
    d = await _req("GET", "/opportunities/search",
                   params={"location_id": config.GHL_LOCATION_ID, "contact_id": contact_id, "status": "open"})
    ops = d.get("opportunities") or []
    return ops[0] if ops else None


async def mover_oportunidad(contact_id: str, nombre_contacto: str, pipeline: str, etapa: str,
                            valor: int | None = None) -> None:
    p = config.PIPELINES[pipeline]
    op = await oportunidad_abierta(contact_id)
    cuerpo = {"pipelineId": p["id"], "pipelineStageId": p["etapas"][etapa]}
    if valor:
        cuerpo["monetaryValue"] = valor
    if op:
        await _req("PUT", f"/opportunities/{op['id']}", json=cuerpo)
    else:
        cuerpo.update({"locationId": config.GHL_LOCATION_ID, "contactId": contact_id, "status": "open",
                       "name": f"{nombre_contacto or 'Cliente'} - WhatsApp"})
        await _req("POST", "/opportunities/", json=cuerpo)


_pipe_viajes: dict | None = None


async def pipeline_viajes() -> dict | None:
    """{'id', 'etapas': {nombre: id}} del pipeline de viajes a regiones, o None si aún no existe."""
    global _pipe_viajes
    if _pipe_viajes:
        return _pipe_viajes
    d = await _req("GET", "/opportunities/pipelines", params={"locationId": config.GHL_LOCATION_ID})
    for p in d.get("pipelines", []):
        if p.get("name", "").strip().lower() == config.PIPELINE_VIAJES.lower():
            _pipe_viajes = {"id": p["id"], "etapas": {st["name"].strip().lower(): st["id"] for st in p.get("stages", [])}}
            return _pipe_viajes
    return None


async def mover_a_viajes(contact_id: str, nombre_contacto: str, etapa: str, valor: int | None = None,
                         forzar: bool = False) -> bool:
    """Deja la oportunidad del cliente en la columna de su ruta. Si Jesús ya la avanzó dentro del pipeline
    de viajes (Fecha confirmada, Ganado, Perdido), no la devuelve a la columna de ruta: solo actualiza el valor."""
    p = await pipeline_viajes()
    if not p or etapa.lower() not in p["etapas"]:
        return False
    op = await oportunidad_abierta(contact_id)
    cuerpo = {"pipelineId": p["id"], "pipelineStageId": p["etapas"][etapa.lower()]}
    if op and op.get("pipelineId") == p["id"]:
        etapas_ruta = {v for k, v in p["etapas"].items() if k.startswith("ruta ")}
        if op.get("pipelineStageId") not in etapas_ruta and not forzar:
            cuerpo = {}
    if valor:
        cuerpo["monetaryValue"] = valor
    if op:
        if cuerpo:
            await _req("PUT", f"/opportunities/{op['id']}", json=cuerpo)
    else:
        cuerpo.update({"locationId": config.GHL_LOCATION_ID, "contactId": contact_id, "status": "open",
                       "name": f"{nombre_contacto or 'Cliente'} - Región"})
        await _req("POST", "/opportunities/", json=cuerpo)
    return True


# ------------------------------------------------------------ agenda
async def horarios_libres(dias: int = 4) -> dict[str, list[str]]:
    ahora = dt.datetime.now(dt.timezone.utc)
    d = await _req("GET", f"/calendars/{config.GHL_CALENDARIO_ID}/free-slots", params={
        "startDate": int(ahora.timestamp() * 1000),
        "endDate": int((ahora + dt.timedelta(days=dias)).timestamp() * 1000),
        "timezone": config.ZONA_HORARIA})
    return {k: v.get("slots", []) for k, v in d.items() if isinstance(v, dict) and "slots" in v}


async def agendar(contact_id: str, inicio_iso: str, direccion: str, titulo: str, notas: str) -> dict:
    inicio = dt.datetime.fromisoformat(inicio_iso)
    fin = inicio + dt.timedelta(hours=3)
    return await _req("POST", "/calendars/events/appointments", json={
        "calendarId": config.GHL_CALENDARIO_ID, "locationId": config.GHL_LOCATION_ID,
        "contactId": contact_id, "startTime": inicio.isoformat(), "endTime": fin.isoformat(),
        "title": titulo, "address": direccion, "appointmentStatus": "confirmed",
        "assignedUserId": config.GHL_USUARIO_JESUS, "notes": notas, "toNotify": False})


def _dt(x: str) -> dt.datetime:
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))


async def revisar_hora(inicio_iso: str) -> tuple[bool, list[str]]:
    """¿Está libre esa hora en el calendario? Si no, devuelve las 2 horas libres más cercanas."""
    pedido = _dt(inicio_iso)
    libres = [x for v in (await horarios_libres(dias=8)).values() for x in v]
    if any(_dt(x) == pedido for x in libres):
        return True, []
    cercanos = sorted(libres, key=lambda x: abs((_dt(x) - pedido).total_seconds()))[:2]
    return False, sorted(cercanos)


async def citas_futuras(contact_id: str) -> list[dict]:
    """Citas que el cliente tiene hoy en adelante en el calendario del bot (no canceladas)."""
    from zoneinfo import ZoneInfo
    d = await _req("GET", f"/contacts/{contact_id}/appointments")
    ahora = dt.datetime.now(dt.timezone.utc)
    out = []
    for e in d.get("events") or []:
        estado = (e.get("appointmentStatus") or e.get("appoinmentStatus") or "").lower()
        if e.get("deleted") or estado in ("cancelled", "invalid", "noshow") \
                or e.get("calendarId") != config.GHL_CALENDARIO_ID:
            continue
        try:
            ini = _dt(str(e["startTime"]))
        except (KeyError, ValueError):
            continue
        if ini.tzinfo is None:
            ini = ini.replace(tzinfo=ZoneInfo(config.ZONA_HORARIA))
        if ini > ahora:
            out.append({"id": e.get("id"), "inicio": ini.isoformat(), "direccion": e.get("address") or "",
                        "titulo": e.get("title") or ""})
    return sorted(out, key=lambda c: c["inicio"])


async def borrar_cita(event_id: str) -> None:
    await _req("DELETE", f"/calendars/events/{event_id}")


# ------------------------------------------------------------ imágenes del catálogo
_urls_media: dict[str, str] = {}
ultimo_error_media = ""


async def url_imagen(nombre_archivo: str) -> str | None:
    """URL alojada en GHL de una imagen del catálogo (WhatsApp entrega bien las de GHL).
    Busca si ya está subida; si no, la sube una vez desde la carpeta static."""
    global ultimo_error_media
    from pathlib import Path
    if nombre_archivo in config.IMAGENES_GHL:
        return config.IMAGENES_GHL[nombre_archivo]
    if nombre_archivo in _urls_media:
        return _urls_media[nombre_archivo]
    nombre = f"bot-autoprog-{nombre_archivo}"
    try:
        d = await _req("GET", "/medias/files", params={"altId": config.GHL_LOCATION_ID, "altType": "location",
                                                        "type": "file", "query": nombre, "limit": 5})
        for f in d.get("files", []):
            if f.get("name") == nombre and f.get("url"):
                _urls_media[nombre_archivo] = f["url"]
                return f["url"]
    except Exception as e:
        ultimo_error_media = f"listar: {e}"[:300]
        log.warning("no se pudo listar medios")
    ruta = Path(__file__).parent / "static" / nombre_archivo
    h = {"Authorization": f"Bearer {config.GHL_TOKEN}", "Version": "2021-07-28", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(BASE + "/medias/upload-file", headers=h,
                         files={"file": (nombre, ruta.read_bytes(), "image/jpeg")}, data={"name": nombre})
    if r.status_code >= 400:
        ultimo_error_media = f"subir {r.status_code}: {r.text[:300]}"
        log.error("subida de imagen %s -> %s %s", nombre, r.status_code, r.text[:300])
        return None
    url = r.json().get("url")
    if url:
        _urls_media[nombre_archivo] = url
    return url



async def subir_adjunto(conversation_id: str, nombre_archivo: str) -> list[str]:
    """Sube la imagen como adjunto de la conversación (así GHL la entrega por WhatsApp)."""
    from pathlib import Path
    ruta = Path(__file__).parent / "static" / nombre_archivo
    h = {"Authorization": f"Bearer {config.GHL_TOKEN}", "Version": "2021-04-15", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(BASE + "/conversations/messages/upload", headers=h,
                         data={"conversationId": conversation_id, "locationId": config.GHL_LOCATION_ID},
                         files={"fileAttachment": (nombre_archivo, ruta.read_bytes(), "image/jpeg")})
    if r.status_code >= 400:
        global ultimo_error_media
        ultimo_error_media = f"adjunto {r.status_code}: {r.text[:300]}"
        log.error("subida de adjunto -> %s %s", r.status_code, r.text[:300])
        return []
    return r.json().get("uploadedFiles", {}) and list(r.json()["uploadedFiles"].values()) or r.json().get("urls", [])



async def enviar_imagen_whatsapp(contact_id: str, url: str, nombre: str = "llaves.jpg") -> str | None:
    """Foto por WhatsApp con el formato que exige GHL (objeto whatsapp.media, no 'attachments')."""
    cuerpo = {"type": "WhatsApp", "contactId": contact_id, "status": "delivered",
              "whatsapp": {"type": "media", "media": {"type": "image", "url": url, "name": nombre}}}
    try:
        d = await _req("POST", "/conversations/messages", "2021-04-15", json=cuerpo)
    except httpx.HTTPStatusError:
        d = await _req("POST", "/conversations/messages", "v3", json=cuerpo)
    return d.get("messageId") or d.get("id")
