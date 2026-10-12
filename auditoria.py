"""Auditoría de WhatsApp: baja conversaciones de GHL y las clasifica con Jev (TypeSafe).

Reparto de trabajo:
- Código (sin IA): fechas, quién habló último, días sin respuesta, precios que dio Autoprog,
  candidatos de ciudad y año encontrados en el texto, y su ruta/zona según nuestras tablas.
- Jev: juicios sobre el texto (dónde está el auto entre los candidatos, marca, servicio, estado del chat,
  si quedó esperando respuesta, intención de compra, recuperabilidad).
- Claude (solo en el piloto): etiqueta los mismos chats para medir cuánto acierta Jev.

Solo lee GHL; no escribe nada en GHL ni manda mensajes."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
import socket
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

import config
import ghl
from cotizador import ALIAS, BLOQUES, ZONAS, _norm, resolver_lugar

log = logging.getLogger("auditoria")
router = APIRouter(prefix="/auditoria")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_KEY = (os.getenv("JEV_API_KEY", "").split() or [""])[0]
JEV_MODELO = os.getenv("JEV_MODELO", "jev-latest")
ARCHIVO = Path(os.getenv("AUDITORIA_DIR", "/tmp")) / "auditoria_piloto.json"

INSTANCIA = f"{socket.gethostname()}-{os.getpid()}-{int(time.time())}"
_tareas: set = set()
_piloto: dict = {"estado": "sin_iniciar", "fichas": [], "uso_jev_tokens": 0, "errores": []}

TIPOS_FUERA = ("ACTIVITY", "COMMENT", "CALL", "INTERNAL")
AUTOFILL = ("necesito una llave para mi bmw. modelo, año y comuna",)


def _clave(k: str | None):
    if not config.WEBHOOK_SECRET or k != config.WEBHOOK_SECRET:
        raise HTTPException(401)


# ------------------------------------------------------------ descarga
async def listar_conversaciones(maximo: int) -> list[dict]:
    out: list[dict] = []
    despues = None
    while len(out) < maximo:
        params = {"locationId": config.GHL_LOCATION_ID, "limit": 100,
                  "sortBy": "last_message_date", "sort": "desc"}
        if despues:
            params["startAfterDate"] = despues
        d = await ghl._req("GET", "/conversations/search", "2021-04-15", params=params)
        lote = d.get("conversations") or []
        if not lote:
            break
        out += lote
        nuevo = lote[-1].get("lastMessageDate")
        if len(lote) < 100 or not nuevo or nuevo == despues:
            break
        despues = nuevo
    return out[:maximo]


async def todos_los_mensajes(conv_id: str, tope: int = 400) -> list[dict]:
    msgs: list[dict] = []
    ultimo = None
    while len(msgs) < tope:
        params = {"limit": 100}
        if ultimo:
            params["lastMessageId"] = ultimo
        d = await ghl._req("GET", f"/conversations/{conv_id}/messages", "2021-04-15", params=params)
        m = d.get("messages") or {}
        lote = m.get("messages") or []
        msgs += lote
        if not lote or not m.get("nextPage"):
            break
        ultimo = m.get("lastMessageId") or lote[-1].get("id")
    return sorted(msgs, key=lambda x: x.get("dateAdded") or "")


def _fecha(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _texto(m: dict) -> str:
    cuerpo = (m.get("body") or "").strip()
    if not cuerpo and m.get("attachments"):
        cuerpo = "[adjunto: foto, audio o archivo]"
    if cuerpo.lower().rstrip(":. ") in AUTOFILL or any(a in cuerpo.lower() for a in AUTOFILL):
        cuerpo = "[mensaje automático del anuncio, sin datos]"
    return cuerpo[:600]


def armar(msgs: list[dict]) -> tuple[str, dict]:
    """Transcripción compacta + métricas calculadas por código."""
    utiles = [m for m in msgs if not any(t in (m.get("messageType") or "").upper() for t in TIPOS_FUERA)
              and m.get("direction") in ("inbound", "outbound")]
    lineas, entrantes, salientes = [], [], []
    for m in utiles:
        t = _texto(m)
        if not t:
            continue
        f = _fecha(m.get("dateAdded"))
        quien = "CLIENTE" if m["direction"] == "inbound" else "AUTOPROG"
        lineas.append(f"[{f.strftime('%d/%m/%y') if f else '?'}] {quien}: {t}")
        (entrantes if quien == "CLIENTE" else salientes).append((f, t))
    trans = "\n".join(lineas)
    if len(trans) > 9000:   # Jev pierde precisión con mucho texto: inicio + final
        trans = trans[:2000] + "\n[...]\n" + trans[-7000:]

    ahora = dt.datetime.now(dt.timezone.utc)
    ult_in = max((f for f, _ in entrantes if f), default=None)
    ult_out = max((f for f, _ in salientes if f), default=None)
    ultimo = max([x for x in (ult_in, ult_out) if x], default=None)
    precios = []
    for _, t in salientes:
        for x in re.findall(r"\$\s?(\d{1,3}(?:[.\s]\d{3})+)", t):
            v = int(re.sub(r"\D", "", x))
            if 20000 <= v <= 3000000:
                precios.append(v)
        for x in re.findall(r"\b(\d{2,3})\s?mil\b", t.lower()):
            precios.append(int(x) * 1000)
    texto_cliente = " ".join(t for _, t in entrantes)
    metr = {
        "mensajes_cliente": len(entrantes), "mensajes_autoprog": len(salientes),
        "primer_mensaje": min((f for f, _ in entrantes + salientes if f), default=None),
        "ultimo_mensaje": ultimo,
        "ultimo_habla": "cliente" if ult_in and (not ult_out or ult_in > ult_out) else ("autoprog" if ult_out else "nadie"),
        "dias_desde_ultimo": (ahora - ultimo).days if ultimo else None,
        "cliente_sin_respuesta": bool(ult_in and (not ult_out or ult_in > ult_out)),
        "precios_dados": sorted(set(precios)),
        "candidatos_lugar": candidatos_lugar(texto_cliente),
        "candidatos_anio": sorted(set(int(a) for a in re.findall(r"\b(19[89]\d|20[0-2]\d)\b", texto_cliente)
                                      if 1985 <= int(a) <= ahora.year + 1)),
    }
    for k in ("primer_mensaje", "ultimo_mensaje"):
        metr[k] = metr[k].strftime("%Y-%m-%d") if metr[k] else None
    return trans, metr


_PALABRAS_LUGAR = set(ZONAS) | set(BLOQUES) | set(ALIAS)


def candidatos_lugar(texto: str) -> list[str]:
    """Lugares de nuestras tablas que el cliente nombró (en orden de aparición, sin repetir)."""
    pal = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü']+", texto)
    vistos, out = set(), []
    for n in (4, 3, 2, 1):
        for i in range(len(pal) - n + 1):
            k = _norm("".join(pal[i:i + n]))
            if len(k) < 4 or k not in _PALABRAS_LUGAR:
                continue
            lug = resolver_lugar(k)
            if lug.tipo in ("rm", "region") and lug.nombre not in vistos:
                vistos.add(lug.nombre)
                out.append((i, lug.nombre))
    return [n for _, n in sorted(out)]


# ------------------------------------------------------------ preguntas
MARCAS = {"BMW": None, "MINI": None, "Mercedes-Benz": None, "Audi": None, "Volkswagen": None,
          "Porsche": None, "Jeep, Dodge, RAM o Chrysler": None, "Fiat o Alfa Romeo": None, "Volvo": None,
          "Land Rover o Jaguar": None, "Otra marca": "Any other brand (Toyota, Chevrolet, Hyundai, Kia, etc.)",
          "No lo dice": "The customer never says the brand of the car"}
SERIES = {**{f"BMW Serie {i}": None for i in range(1, 9)}, **{f"BMW X{i}": None for i in range(1, 8)},
          "BMW Z4": None, "BMW eléctrico (i3, i4, i8, iX)": None, "BMW M (M2 a M8)": None, "MINI": None,
          "No es BMW ni MINI": None, "No lo dice": "The customer never says the model"}
SERVICIOS = {
    "Duplicado": "Wants an extra/spare key while still having at least one working key",
    "Llave perdida": "Lost all keys / has no working key (all keys lost)",
    "Llave con pantalla": "Wants the BMW display key (key with screen)",
    "Carcasa o batería": "Only wants a new key shell/case or battery, no programming",
    "Módulo o electrónica": "Module repair, cloning, coding or other electronic programming that is not a key",
    "Solo pregunta precio": "Asks price or information without saying what exactly they need",
    "Otro": "Something else (mechanics, parts, unrelated)",
    "No lo dice": "No request is visible (only greetings or automatic messages)",
}
ESTADOS = {
    "Nunca escribió": "Only automatic ad messages; the customer never wrote anything of their own",
    "Solo saludó": "The customer only greeted or asked one vague question and gave no car data",
    "Dio datos y no se le cotizó": "The customer gave car data but AUTOPROG never gave a price",
    "Cotizado, cliente no respondió": "AUTOPROG gave a price and the customer did not answer after that",
    "Cotizado y en conversación": "Price was given and the conversation continued (questions, negotiation) without a booking",
    "Agendado": "A date/time for the service was agreed",
    "Trabajo realizado": "The job was done or paid",
    "Rechazó por precio": "The customer said it is too expensive or declined because of price",
    "Lo resolvió con otro": "The customer says they solved it elsewhere or no longer needs it",
    "No lo atendemos": "AUTOPROG said it cannot do this job or brand",
    "Spam o equivocado": "Spam, wrong number, job seeker, supplier or not a customer",
}


def preguntas(metr: dict) -> dict:
    q = {
        "marca": {"type": "choice", "instructions": "Which car brand is the CLIENTE asking about?", "criteria": MARCAS},
        "modelo": {"type": "choice", "instructions": "Which car model is the CLIENTE asking about?", "criteria": SERIES},
        "servicio": {"type": "choice", "instructions": "What service does the CLIENTE want from AUTOPROG?", "criteria": SERVICIOS},
        "estado": {"type": "choice", "instructions": "How did this WhatsApp conversation end up? Judge the final situation.",
                   "criteria": ESTADOS},
        "esperando": {"type": "noul", "instructions": "At the end of the conversation, is the CLIENTE left waiting for an "
                      "answer, price or action from AUTOPROG that never came?"},
        "pidio_fotos": {"type": "noul", "instructions": "Did the CLIENTE ask for photos, a catalog or pictures of keys that "
                        "AUTOPROG never sent?"},
        "molesto": {"type": "noul", "instructions": "Is the CLIENTE annoyed or upset with AUTOPROG (slow answer, bad service)?"},
        "intencion": {"type": "score", "instructions": "How strong is the CLIENTE's intention to buy the service?",
                      "criteria": ["No intention or not a real customer", "Just curious", "Interested but undecided",
                                   "Clearly wants it, asking how/when", "Ready to book or pay"]},
        "recuperable": {"type": "score", "instructions": "If AUTOPROG writes to this CLIENTE again today, how likely is a sale?",
                        "criteria": ["Lost: solved elsewhere, refused, or not a customer", "Unlikely", "Possible",
                                     "Likely: was interested and simply not followed up", "Very likely: was waiting for us"]},
    }
    if metr["candidatos_lugar"]:
        q["lugar"] = {"type": "choice",
                      "instructions": "Where is the CLIENTE's car (where the service would be done)? Pick the place the "
                                      "CLIENTE says the car is or where they are. If none of these is the car's location, "
                                      "pick the last option.",
                      "criteria": {**{c: None for c in metr["candidatos_lugar"]},
                                   "Ninguno de estos": "None of the listed places is where the car is"}}
    if metr["candidatos_anio"]:
        q["anio"] = {"type": "choice", "instructions": "Which is the model year of the CLIENTE's car?",
                     "criteria": {**{str(a): None for a in metr["candidatos_anio"]},
                                  "Ninguno": "None of these numbers is the car's year"}}
    return q


NOTA = ("AUTOPROG is a mobile car-key programming business in Chile (mostly BMW). CLIENTE is the customer. "
        "The chat is in Chilean Spanish. Lines marked [mensaje automático del anuncio] were sent automatically by an ad.")


async def jev(cliente: httpx.AsyncClient, trans: str, q: dict) -> dict:
    cuerpo = {"model": JEV_MODELO, "state": {"context": NOTA, "conversation": trans}, "questions": q}
    for intento in range(5):
        r = await cliente.post(JEV_URL, json=cuerpo, headers={"Authorization": f"Bearer {JEV_KEY}"}, timeout=60)
        if r.status_code in (429, 529, 500, 502, 503):
            await asyncio.sleep(2 ** intento)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Jev {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Jev: sin respuesta tras 5 intentos")


def _resumir_jev(d: dict) -> dict:
    out = {}
    for k, a in (d.get("answers") or {}).items():
        t = a.get("type")
        if t == "choice":
            out[k] = {"v": a.get("choice"), "conf": round(a.get("confidence") or 0, 2)}
        elif t == "noul":
            out[k] = {"v": round(a.get("noul") or 0, 2)}
        elif t == "score":
            leg = [int(x) for x in (a.get("legend") or {}) if str(x).lstrip("-").isdigit()]
            base = min(leg) if leg else 0          # niveles normalizados a 0..4
            out[k] = {"v": round((a.get("score") or 0) - base, 2), "conf": round(a.get("confidence") or 0, 2)}
    return out


# ------------------------------------------------------------ juez Claude (solo piloto)
async def juez_claude(trans: str, q: dict) -> dict:
    import anthropic
    cli = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    especif = {}
    for k, v in q.items():
        if v["type"] == "choice":
            especif[k] = {"pregunta": v["instructions"], "opciones": list(v["criteria"])}
        elif v["type"] == "noul":
            especif[k] = {"pregunta": v["instructions"], "responde": "true o false"}
        else:
            especif[k] = {"pregunta": v["instructions"], "responde": "entero 0 a 4 según: " + " | ".join(v["criteria"])}
    prompt = (f"{NOTA}\n\nConversación:\n{trans}\n\nResponde cada pregunta. Para las de opciones copia EXACTO el texto "
              f"de una opción. Devuelve solo un JSON con las mismas claves.\n{json.dumps(especif, ensure_ascii=False)}")
    r = await cli.messages.create(model=config.MODELO, max_tokens=800, messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}


# ------------------------------------------------------------ piloto
async def _correr_piloto(n: int, universo: int, semilla: int):
    _piloto.update(estado="descargando", fichas=[], errores=[], uso_jev_tokens=0, inicio=time.time())
    convs = await listar_conversaciones(universo)
    random.Random(semilla).shuffle(convs)
    _piloto["universo_bajado"] = len(convs)
    _piloto["estado"] = "clasificando"
    async with httpx.AsyncClient() as cli:
        for c in convs:
            if len(_piloto["fichas"]) >= n:
                break
            try:
                msgs = await todos_los_mensajes(c["id"])
                trans, metr = armar(msgs)
                if metr["mensajes_cliente"] == 0 and metr["mensajes_autoprog"] == 0:
                    continue
                q = preguntas(metr)
                t0 = time.time()
                dj = await jev(cli, trans, q)
                seg = round(time.time() - t0, 2)
                _piloto["uso_jev_tokens"] += (dj.get("usage") or {}).get("input_tokens", 0)
                try:
                    dc = await juez_claude(trans, q)
                except Exception as e:  # noqa: BLE001
                    dc = {"error": str(e)[:200]}
                _piloto["fichas"].append({"conv": c["id"], "contacto": c.get("contactId"),
                                          "nombre": c.get("fullName") or c.get("contactName"),
                                          "metricas": metr, "jev": _resumir_jev(dj), "jev_seg": seg,
                                          "claude": dc, "transcripcion": trans})
            except Exception as e:  # noqa: BLE001
                _piloto["errores"].append(f"{c.get('id')}: {str(e)[:300]}")
                if len(_piloto["errores"]) > 15:
                    break
    _piloto["estado"] = "listo"
    _piloto["segundos"] = round(time.time() - _piloto["inicio"])
    ARCHIVO.write_text(json.dumps(_piloto, ensure_ascii=False, default=str))


def _acuerdo() -> dict:
    campos = ["lugar", "anio", "marca", "modelo", "servicio", "estado", "esperando", "pidio_fotos", "molesto",
              "intencion", "recuperable"]
    res = {}
    for k in campos:
        tot = ok = 0
        for f in _piloto["fichas"]:
            j, c = f["jev"].get(k), f["claude"].get(k) if isinstance(f["claude"], dict) else None
            if j is None or c is None:
                continue
            tot += 1
            jv = j["v"]
            if k in ("esperando", "pidio_fotos", "molesto"):
                ok += (jv >= 0.5) == bool(c)
            elif k in ("intencion", "recuperable"):
                try:
                    ok += abs(float(jv) - float(c)) <= 1
                except (TypeError, ValueError):
                    pass
            else:
                ok += str(jv).strip().lower() == str(c).strip().lower()
        if tot:
            res[k] = {"coinciden": ok, "de": tot, "pct": round(100 * ok / tot)}
    return res


@router.get("/piloto/iniciar")
async def piloto_iniciar(k: str, n: int = 30, universo: int = 300, semilla: int = 7):
    _clave(k)
    if not JEV_KEY:
        raise HTTPException(400, "Falta la variable JEV_API_KEY")
    if _piloto.get("estado") in ("descargando", "clasificando"):
        return {"ya_corriendo": True, "estado": _piloto["estado"], "fichas": len(_piloto["fichas"])}
    t = asyncio.create_task(_correr_piloto(n, universo, semilla))
    _tareas.add(t)
    t.add_done_callback(_tareas.discard)
    return {"iniciado": True, "n": n, "universo": universo, "instancia": INSTANCIA}


@router.get("/piloto/estado")
def piloto_estado(k: str):
    _clave(k)
    jt = _piloto.get("uso_jev_tokens", 0)
    return {"instancia": INSTANCIA, "estado": _piloto.get("estado"), "fichas": len(_piloto.get("fichas", [])),
            "universo_bajado": _piloto.get("universo_bajado"), "errores": _piloto.get("errores", [])[:5],
            "jev_tokens": jt, "jev_costo_usd": round(jt * 0.042 / 1e6, 4), "segundos": _piloto.get("segundos"),
            "acuerdo_jev_vs_claude": _acuerdo() if _piloto.get("fichas") else None}


@router.get("/piloto/ficha")
def piloto_ficha(k: str, i: int, con_texto: bool = True):
    _clave(k)
    f = _piloto["fichas"][i]
    return {**{x: f[x] for x in ("conv", "nombre", "metricas", "jev", "jev_seg", "claude")},
            **({"transcripcion": f["transcripcion"][-3500:]} if con_texto else {})}


@router.get("/piloto/diferencias")
def piloto_diferencias(k: str, campo: str):
    _clave(k)
    out = []
    for i, f in enumerate(_piloto["fichas"]):
        j = f["jev"].get(campo)
        c = f["claude"].get(campo) if isinstance(f["claude"], dict) else None
        if j is None or c is None:
            continue
        if str(j["v"]).strip().lower() != str(c).strip().lower():
            out.append({"i": i, "jev": j, "claude": c, "candidatos": f["metricas"].get("candidatos_lugar")})
    return out


async def _ficha(cli: httpx.AsyncClient, c: dict, con_claude: bool = True) -> dict | None:
    msgs = await todos_los_mensajes(c["id"])
    trans, metr = armar(msgs)
    if metr["mensajes_cliente"] == 0 and metr["mensajes_autoprog"] == 0:
        return None
    q = preguntas(metr)
    t0 = time.time()
    dj = await jev(cli, trans, q)
    seg = round(time.time() - t0, 2)
    dc = {}
    if con_claude:
        try:
            dc = await juez_claude(trans, q)
        except Exception as e:  # noqa: BLE001
            dc = {"error": str(e)[:200]}
    return {"conv": c["id"], "contacto": c.get("contactId"), "nombre": c.get("fullName") or c.get("contactName"),
            "metricas": metr, "jev": _resumir_jev(dj), "jev_seg": seg,
            "jev_tokens": (dj.get("usage") or {}).get("input_tokens", 0), "claude": dc, "transcripcion": trans}


@router.get("/piloto/lote")
async def piloto_lote(k: str, desde: int = 0, n: int = 3, universo: int = 300, semilla: int = 7, texto: int = 1500):
    """Versión sin estado: procesa n chats de una muestra fija y devuelve las fichas en la respuesta."""
    _clave(k)
    convs = await listar_conversaciones(universo)
    random.Random(semilla).shuffle(convs)
    out, errores = [], []
    async with httpx.AsyncClient() as cli:
        for i, c in enumerate(convs[desde:desde + n], start=desde):
            try:
                f = await _ficha(cli, c)
                if f:
                    f["i"] = i
                    f["transcripcion"] = f["transcripcion"][-texto:]
                    out.append(f)
            except Exception as e:  # noqa: BLE001
                errores.append(f"{i} {c.get('id')}: {str(e)[:300]}")
    return {"instancia": INSTANCIA, "universo_bajado": len(convs), "fichas": out, "errores": errores}
