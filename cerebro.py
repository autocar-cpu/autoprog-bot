"""El cerebro: Claude con herramientas. La IA entiende y redacta; el código calcula y ejecuta."""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import re

import anthropic

import config
import ghl
from cotizador import cotizar
from prompt import SISTEMA
import regiones
from revisor import revisar

log = logging.getLogger("cerebro")
_cliente = None


def _claude():
    global _cliente
    if _cliente is None:
        _cliente = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _cliente


HERRAMIENTAS = [
    {"name": "cotizar",
     "description": "Calcula el precio exacto de una llave BMW/MINI con la tabla oficial de Autoprog. Única fuente de precios.",
     "input_schema": {"type": "object", "properties": {
         "modelo": {"type": "string", "description": "Modelo tal como lo dijo el cliente, ej. '320i', 'X5 xDrive40i', 'Mini Cooper S', 'Serie 3'"},
         "anio": {"type": "integer"},
         "lugar": {"type": "string", "description": "Comuna o ciudad del cliente"},
         "situacion": {"type": "string", "enum": ["duplicado", "akl", "no_sabe"],
                       "description": "duplicado = tiene una llave que funciona; akl = perdió todas"},
         "tipo_llave": {"type": "string", "enum": ["", "pantalla", "bcp"], "description": "'pantalla' si quiere la llave con pantalla (8); 'bcp' si quiere la llave 7 o si la llave que usa hoy es la 7; vacío para razor 6A/6B u otras"},
         "cliente_es_negocio": {"type": "boolean"}},
         "required": ["modelo", "anio", "lugar", "situacion"]}},
    {"name": "horarios_disponibles",
     "description": "Horarios libres reales de la agenda de Jesús para los próximos días (bloques de 3 horas).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "agendar",
     "description": "Crea la cita en la agenda. Solo cuando el cliente confirmó día, hora y dirección exacta.",
     "input_schema": {"type": "object", "properties": {
         "inicio": {"type": "string", "description": "Fecha y hora de inicio ISO con zona, tal como vino de horarios_disponibles"},
         "direccion": {"type": "string"},
         "resumen": {"type": "string", "description": "Muy corto: marca modelo año, comuna, servicio, precio"}},
         "required": ["inicio", "direccion", "resumen"]}},
    {"name": "guardar_datos",
     "description": "Guarda en el CRM los datos del vehículo y del cliente apenas se conocen.",
     "input_schema": {"type": "object", "properties": {
         "marca": {"type": "string"}, "modelo": {"type": "string"}, "anio": {"type": "integer"},
         "comuna": {"type": "string"}, "llave_actual": {"type": "string"}, "llave_deseada": {"type": "string"}}}},
    {"name": "reservar_cupo",
     "description": "Cliente de regiones que aceptó el precio y quiere que le reserves cupo en la ruta a su zona. "
                    "Si la ruta ya tiene fecha y el cliente aceptó la visita ese día, pon acepto_fecha=true.",
     "input_schema": {"type": "object", "properties": {
         "resumen": {"type": "string", "description": "Muy corto: vehículo, ciudad, servicio, precio aceptado"},
         "acepto_fecha": {"type": "boolean", "description": "true solo si el cliente dijo que sí al día de la ruta"},
         "dia": {"type": "string", "description": "El día que aceptó, tal como vino en ruta.dia (ej. 'jueves 22 de octubre')"},
         "horario": {"type": "string", "description": "Horario que eligió el cliente, si ya lo dijo"}},
         "required": ["resumen"]}},
    {"name": "escalar_a_jesus",
     "description": "Pasa el caso a Jesús (tarea en el CRM) y el bot deja de responder a este contacto. Incluye el mensaje para el cliente.",
     "input_schema": {"type": "object", "properties": {
         "motivo": {"type": "string"},
         "resumen": {"type": "string", "description": "Nota corta para Jesús: marca, modelo, año, comuna, qué pide, 1 línea relevante"},
         "mensajes": {"type": "array", "items": {"type": "string"}, "description": "Lo que se le dice al cliente (corto)"}},
         "required": ["motivo", "resumen", "mensajes"]}},
    {"name": "enviar_respuesta",
     "description": "Envía la respuesta final al cliente. Último paso de cada turno.",
     "input_schema": {"type": "object", "properties": {
         "mensajes": {"type": "array", "items": {"type": "string"}, "description": "1 a 3 mensajes cortos, en orden. Vacío si no hay que responder."},
         "imagen": {"type": "string", "enum": [""] + list(config.IMAGENES), "description": "Imagen del catálogo a enviar antes del último mensaje"}},
         "required": ["mensajes"]}},
]


@dataclass
class Resultado:
    mensajes: list[str] = field(default_factory=list)
    imagen: str = ""
    escalado: dict | None = None
    acciones: list[dict] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)
    modelo_usado: str = ""
    imagen_sugerida: str = ""
    region: dict | None = None      # clasificación geográfica (regiones.clasificar)


@dataclass
class Contexto:
    contact_id: str
    nombre: str
    historial: list[dict]          # [{"rol": "cliente"|"jesus", "texto", "fecha", "imagenes": [urls]}]
    simulacion: bool = True        # True = no toca GHL (pruebas)
    cotizaciones_previas: list[dict] = field(default_factory=list)  # [{"clave": [...], "precio": int}] de notas internas


# ---------------------------------------------------------------- reglas duras de precio (Jesús, 15/10/2026)
MAX_AUTOS_POR_CHAT = 2          # más autos distintos = probable competidor o revendedor: se escala
NOTA_COTIZACION = "[cotizacion] "


def clave_cotizacion(args: dict) -> list:
    from cotizador import normalizar_modelo
    m = args.get("modelo", "") or ""
    return [(normalizar_modelo(m) or m).strip().upper(), int(args.get("anio") or 0),
            args.get("tipo_llave", "") or "", args.get("situacion", "duplicado") or "duplicado"]


def leer_cotizaciones(msgs: list[dict]) -> list[dict]:
    """Cotizaciones que el bot dejó como nota interna en la conversación."""
    out = []
    for m in msgs:
        b = m.get("body") or ""
        if b.startswith(NOTA_COTIZACION):
            try:
                out.append(json.loads(b[len(NOTA_COTIZACION):]))
            except ValueError:
                pass
    return out


async def _imagen_base64(url: str) -> dict | None:
    """Descarga la foto del cliente y la entrega en base64 (Claude no puede abrir las URLs de GHL)."""
    import base64
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
        tipo = r.headers.get("content-type", "").split(";")[0].strip()
        if r.status_code != 200 or tipo not in ("image/jpeg", "image/png", "image/webp", "image/gif") \
                or len(r.content) > 4_500_000:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": tipo,
                                            "data": base64.b64encode(r.content).decode()}}
    except Exception:
        log.warning("no se pudo descargar la imagen %s", url[:80])
        return None


async def _transcripcion(ctx: Contexto) -> list[dict]:
    ahora = dt.datetime.now(ZoneInfo(config.ZONA_HORARIA))
    lineas = [f"Fecha y hora actual en Chile: {ahora:%A %d-%m-%Y %H:%M}.",
              f"Nombre del contacto en WhatsApp: {ctx.nombre or '(sin nombre)'}", "", "Conversación (más antigua arriba):"]
    imagenes = []
    for m in ctx.historial[-40:]:
        quien = "CLIENTE" if m["rol"] == "cliente" else "TÚ (Autoprog)"
        txt = (m.get("texto") or "").strip()
        if m.get("imagenes"):
            txt += f" [envió {len(m['imagenes'])} imagen(es)]"
            if m["rol"] == "cliente":
                imagenes.extend(m["imagenes"])
        if m.get("audio"):
            txt += " [envió una nota de voz]"
        lineas.append(f"[{m.get('fecha', '')}] {quien}: {txt}")
    lineas += ["", "Responde a lo último que escribió el cliente."]
    contenido = [{"type": "text", "text": "\n".join(lineas)}]
    for url in imagenes[-3:]:
        bloque = await _imagen_base64(url)
        if bloque:
            contenido.append(bloque)
    return contenido


async def _ejecutar(nombre: str, args: dict, ctx: Contexto, res: Resultado, montos: set[int]) -> str:
    if nombre == "cotizar":
        clave = clave_cotizacion(args)
        previas = ctx.cotizaciones_previas + [{"clave": a["clave"], "precio": a.get("precio")}
                                              for a in res.acciones if a.get("clave")]
        autos = {tuple(p["clave"][:2]) for p in previas} | {tuple(clave[:2])}
        if len(autos) > MAX_AUTOS_POR_CHAT:
            res.acciones.append({"cotizar": args, "resultado": "escalar", "motivo": "muchos autos"})
            return json.dumps({"estado": "escalar", "motivo": (
                "El cliente está pidiendo precios de muchos autos distintos. No cotices más: usa escalar_a_jesus "
                "y dile algo corto como 'déjame revisarlo y te confirmo'.")}, ensure_ascii=False)
        c = cotizar(args.get("modelo", ""), args.get("anio"), args.get("lugar", ""),
                    args.get("situacion", "duplicado"), args.get("tipo_llave", ""),
                    bool(args.get("cliente_es_negocio")))
        # Regla: un precio ya dado nunca baja (ej. cambia de comuna a una zona más barata)
        antes = max((p.get("precio") or 0 for p in previas if p.get("clave") == clave), default=0)
        if c.precio and antes > c.precio:
            c.precio = antes
            c.precio_max = None
            c.aviso = (c.aviso + " " if c.aviso else "") + (
                "Ya le habías dado este precio para la misma llave: se mantiene. No le expliques cálculos de zona.")
        for v in (c.precio, c.precio_max):
            if v:
                montos.add(v)
        res.acciones.append({"cotizar": args, "resultado": c.estado, "precio": c.precio, "precio_max": c.precio_max,
                             "clave": clave})
        if c.precio and not ctx.simulacion and c.precio > antes:
            await ghl.nota_interna(ctx.contact_id, NOTA_COTIZACION + json.dumps(
                {"clave": clave, "precio": c.precio, "lugar": args.get("lugar", "")}, ensure_ascii=False))
        if c.imagen_sugerida:
            res.imagen_sugerida = c.imagen_sugerida
        geo = regiones.clasificar(args.get("lugar", ""))
        if geo:
            res.region = geo
        d = c.dict()
        ruta = regiones.fecha_ruta(geo["ciudad"], geo["bloque"]) if geo and geo["tipo"] == "region" else None
        if ruta:
            d["ruta"] = ruta
            d["nota_cliente"] = ("Precio a domicilio en su ciudad, incluye el traslado. Ofrece el " + ruta["dia"] +
                                 (" o, si no puede, el " + ruta["alternativa"] if ruta["alternativa"] else "") +
                                 " y pregunta si le acomoda la visita ese día; el cliente elige el horario.")
        d.pop("interno", None)   # el sistema IMMO no se le pasa a la IA para que no lo repita al cliente
        return json.dumps(d, ensure_ascii=False)

    if nombre == "horarios_disponibles":
        if ctx.simulacion:
            base = dt.datetime.now(ZoneInfo(config.ZONA_HORARIA)).replace(minute=0, second=0, microsecond=0)
            slots = {}
            for d in range(1, 4):
                dia = base + dt.timedelta(days=d)
                slots[dia.date().isoformat()] = [dia.replace(hour=h).isoformat() for h in (10, 13, 16)]
            return json.dumps(slots)
        return json.dumps(await ghl.horarios_libres(), ensure_ascii=False)

    if nombre == "agendar":
        res.acciones.append({"agendar": args})
        if not ctx.simulacion:
            await ghl.agendar(ctx.contact_id, args["inicio"], args["direccion"],
                              f"Llave - {ctx.nombre}", args["resumen"])
            await ghl.mover_oportunidad(ctx.contact_id, ctx.nombre, "BMW MINI", "Agendado")
        return "Cita creada."

    if nombre == "reservar_cupo":
        res.acciones.append({"reservar_cupo": args})
        if not ctx.simulacion:
            extra = f" | Fecha: {args.get('dia', '')} {args.get('horario', '')}".rstrip() if args.get("acepto_fecha") else ""
            await ghl.nota_interna(ctx.contact_id, "Cupo reservado en ruta: " + args.get("resumen", "") + extra)
        if args.get("acepto_fecha"):
            if args.get("horario"):
                return "Fecha confirmada. Confírmale el día y el horario en un mensaje corto."
            return "Fecha confirmada. Pregúntale qué horario le acomoda ese día (que él elija; no propongas hora)."
        return "Cupo reservado. Dile que apenas confirmes la fecha de la ruta le avisas."

    if nombre == "guardar_datos":
        res.acciones.append({"guardar_datos": args})
        geo = regiones.clasificar(args.get("comuna") or "")
        if geo:
            res.region = geo
        if not ctx.simulacion and geo:
            await ghl.actualizar_campos(ctx.contact_id, geo["campos"])
            if geo.get("etiqueta"):
                await ghl.agregar_etiquetas(ctx.contact_id, [geo["etiqueta"]])
        if not ctx.simulacion:
            await ghl.actualizar_campos(ctx.contact_id, {
                "marca": args.get("marca"), "modelo": args.get("modelo"),
                "anio": str(args["anio"]) if args.get("anio") else None,
                "comuna_detectada": args.get("comuna"), "llave_actual": args.get("llave_actual"),
                "llave_deseada": args.get("llave_deseada")})
        return "Guardado."
    return "Herramienta desconocida."


PIDE_FOTOS = re.compile(r"\b(foto|fotos|imagen|imagenes|imágenes|lámina|lamina|catálogo|catalogo|modelos de llave|ver (las )?llaves)\b", re.I)
PROMETE_DESPUES = re.compile(r"(te (las?|lo) (mando|envío|envio|paso)|desde mi celular|en unos minutos|más rato|mas rato|te mando (las )?fotos)", re.I)


def _primer_contacto(ctx: "Contexto") -> bool:
    """True si Autoprog todavía no le ha escrito nada a este cliente."""
    return not any(m["rol"] != "cliente" for m in ctx.historial)


def _pide_fotos(ctx: "Contexto") -> bool:
    for m in reversed(ctx.historial):
        if m["rol"] == "cliente":
            return bool(PIDE_FOTOS.search(m.get("texto") or ""))
        break
    return False


async def pensar(ctx: Contexto, modelo_forzado: str | None = None) -> Resultado:
    res = Resultado()
    montos: set[int] = set()
    mensajes = [{"role": "user", "content": await _transcripcion(ctx)}]
    sistema = [{"type": "text", "text": SISTEMA, "cache_control": {"type": "ephemeral"}}]
    modelo = modelo_forzado or config.MODELO
    reintentos_revisor = 0

    for _ in range(10):
        r = await _claude().messages.create(model=modelo, max_tokens=1500, system=sistema,
                                            tools=HERRAMIENTAS, messages=mensajes)
        res.modelo_usado = modelo
        mensajes.append({"role": "assistant", "content": r.content})
        usos = [b for b in r.content if b.type == "tool_use"]
        if not usos:
            mensajes.append({"role": "user", "content": "Termina llamando enviar_respuesta o escalar_a_jesus."})
            continue

        resultados = []
        final = None
        for u in usos:
            if u.name in ("enviar_respuesta", "escalar_a_jesus"):
                final = u
                continue
            try:
                salida = await _ejecutar(u.name, u.input, ctx, res, montos)
            except Exception as e:  # un error de GHL no debe botar la conversación
                log.exception("herramienta %s", u.name)
                salida = f"Error: {e}. Sigue sin esta acción."
            resultados.append({"type": "tool_result", "tool_use_id": u.id, "content": salida})

        if final:
            msgs = [m.strip() for m in final.input.get("mensajes", []) if m and m.strip()]
            problemas = revisar(msgs, montos)
            if final.name == "enviar_respuesta" and any(PROMETE_DESPUES.search(x) for x in msgs) \
                    and not final.input.get("imagen"):
                problemas.append("No prometas mandar fotos después ni desde otro teléfono: tú mandas la imagen AHORA "
                                 "con el campo 'imagen' de enviar_respuesta, y el último mensaje la presenta.")
            if problemas and reintentos_revisor < 2:
                reintentos_revisor += 1
                modelo = config.MODELO_DIFICIL     # segundo intento con el modelo más fuerte
                resultados.append({"type": "tool_result", "tool_use_id": final.id, "is_error": True,
                                   "content": "Rechazado por el revisor: " + " ".join(problemas)})
                mensajes.append({"role": "user", "content": resultados})
                continue
            if problemas:   # no se pudo corregir: no se envía nada inventado
                res.problemas = problemas
                res.escalado = {"motivo": "El revisor bloqueó la respuesta", "resumen": "; ".join(problemas)}
                res.mensajes = []
                return res
            res.mensajes = msgs
            if final.name == "escalar_a_jesus":
                res.escalado = {"motivo": final.input.get("motivo", ""), "resumen": final.input.get("resumen", "")}
            else:
                res.imagen = final.input.get("imagen") or ""
                if not res.imagen and _pide_fotos(ctx):   # el cliente pidió fotos: se mandan sí o sí
                    res.imagen = res.imagen_sugerida or "catalogo_general"
                if not res.imagen and res.mensajes and _primer_contacto(ctx):
                    # Protocolo de primer contacto: la primera respuesta siempre lleva la lámina de llaves
                    res.imagen = res.imagen_sugerida or "catalogo_general"
            return res

        mensajes.append({"role": "user", "content": resultados})

    res.escalado = {"motivo": "El bot no logró cerrar la respuesta", "resumen": "Revisar conversación"}
    return res
