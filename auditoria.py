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
    d = json.loads(m.group(0)) if m else {}
    # a veces anida {"campo": {"respuesta": X}}
    return {k: (next(iter(v.values()), None) if isinstance(v, dict) and len(v) == 1 else v) for k, v in d.items()}


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


def _linea(f: dict) -> str:
    """Una línea corta por chat: campo=jev/claude (marca * cuando no coinciden)."""
    m, j, c = f["metricas"], f["jev"], f["claude"] if isinstance(f["claude"], dict) else {}
    partes = [f"#{f.get('i')}", f"dias={m['dias_desde_ultimo']}", f"ultimo={m['ultimo_habla']}",
              f"precios={m['precios_dados']}"]
    for k in ("lugar", "anio", "marca", "modelo", "servicio", "estado", "esperando", "pidio_fotos", "molesto",
              "intencion", "recuperable"):
        if k not in j:
            continue
        jv, cv = j[k]["v"], c.get(k)
        if k in ("esperando", "pidio_fotos", "molesto"):
            igual = (jv >= 0.5) == bool(cv)
        elif k in ("intencion", "recuperable"):
            try:
                igual = abs(float(jv) - float(cv)) <= 1
            except (TypeError, ValueError):
                igual = False
        else:
            igual = str(jv).strip().lower() == str(cv).strip().lower()
        partes.append(f"{k}={jv}/{cv}{'' if igual else ' *'}")
    return " | ".join(partes)


@router.get("/piloto/lote")
async def piloto_lote(k: str, desde: int = 0, n: int = 3, universo: int = 300, semilla: int = 7, texto: int = 1500,
                      compacto: int = 0):
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
    if compacto:
        from fastapi.responses import PlainTextResponse
        tok = sum(f.get("jev_tokens", 0) for f in out)
        return PlainTextResponse("\n".join([f"instancia={INSTANCIA} tokens_jev={tok}"] + [_linea(f) for f in out]
                                           + [f"ERROR {e}" for e in errores]))
    return {"instancia": INSTANCIA, "universo_bajado": len(convs), "fichas": out, "errores": errores}


# ============================================================ revisión del 100 %
# Corre en segundo plano en el servidor, solo con Jev (sin Claude). Guarda cada chat en un JSONL en disco
# apenas lo clasifica, así puede retomar si se corta. Nada se escribe en GHL.
TOTAL = Path(os.getenv("AUDITORIA_DIR", "/tmp")) / "auditoria_total.jsonl"
_total: dict = {"estado": "sin_iniciar"}

DESCARTE = {"Spam o equivocado", "Lo resolvió con otro", "No lo atendemos", "Trabajo realizado"}
UMBRAL_NOUL = 0.85   # en el piloto Jev marcaba "sí" con 0,5-0,8 cuando Claude decía "no"


def _ajustar_estado(est: str | None, metr: dict) -> str | None:
    """'Cotizado, no respondió' vs 'en conversación' lo decide el código (quién habló último), no Jev."""
    if est in ("Cotizado, cliente no respondió", "Cotizado y en conversación"):
        return "Cotizado y en conversación" if metr["ultimo_habla"] == "cliente" else "Cotizado, cliente no respondió"
    if est == "Nunca escribió" and metr["mensajes_cliente"] > 1:
        return "Solo saludó"
    return est


def segmento(f: dict) -> str:
    """Segmento de ataque, en orden de prioridad."""
    m, j = f["metricas"], f["jev"]
    est = f.get("estado_final")
    v = lambda k: (j.get(k) or {}).get("v")  # noqa: E731
    if est in DESCARTE or v("marca") not in (None, "BMW", "MINI", "No lo dice"):
        return "9 Descartar (spam, otra marca, ya resuelto, no lo hacemos)"
    if est == "Agendado":
        return "8 Agendado (revisar si se hizo)"
    if m["cliente_sin_respuesta"]:
        return "1 Nos escribió y no le respondimos"
    if est == "Dio datos y no se le cotizó":
        return "2 Dio datos y no recibió precio"
    if est == "Cotizado y en conversación":
        return "3 Cotizado, conversación quedó abierta"
    if est == "Cotizado, cliente no respondió":
        return "4 Cotizado, no volvió a escribir"
    if est == "Rechazó por precio":
        return "5 Rechazó por precio"
    if est == "Solo saludó":
        return "6 Solo saludó (sin datos)"
    return "7 Nunca escribió (solo mensaje del anuncio)"


def _completar(f: dict) -> dict:
    m, j = f["metricas"], f["jev"]
    f["estado_final"] = _ajustar_estado((j.get("estado") or {}).get("v"), m)
    lug = (j.get("lugar") or {}).get("v")
    if not lug and len(m["candidatos_lugar"]) == 1:
        lug = m["candidatos_lugar"][0]
    if lug == "Ninguno de estos":
        lug = None
    f["lugar"] = lug
    r = resolver_lugar(lug) if lug else None
    f["zona"] = ("RM " + r.zona) if r and r.tipo == "rm" else (r.zona if r and r.tipo == "region" else "Sin dato")
    f["region"] = bool(r and r.tipo == "region")
    for k in ("esperando", "pidio_fotos", "molesto"):
        f[k] = ((j.get(k) or {}).get("v") or 0) >= UMBRAL_NOUL
    f["segmento"] = segmento(f)
    return f


def _leer_total() -> list[dict]:
    if not TOTAL.exists():
        return []
    out = []
    for linea in TOTAL.read_text().splitlines():
        try:
            out.append(json.loads(linea))
        except ValueError:
            pass
    return out


async def _correr_total(maximo: int, paralelo: int):
    _total.update(estado="listando", inicio=time.time(), hechos=0, vacios=0, errores=[], tokens=0, instancia=INSTANCIA)
    convs = await listar_conversaciones(maximo)
    ya = {f["conv"] for f in _leer_total()}
    pendientes = [c for c in convs if c["id"] not in ya]
    _total.update(estado="clasificando", total_conversaciones=len(convs), ya_hechos=len(ya), pendientes=len(pendientes))
    sem = asyncio.Semaphore(paralelo)

    async def uno(cli, c):
        async with sem:
            for intento in range(3):
                try:
                    f = await _ficha(cli, c, con_claude=False)
                    break
                except Exception as e:  # noqa: BLE001
                    if intento == 2:
                        _total["errores"].append(f"{c.get('id')}: {str(e)[:200]}")
                        return
                    await asyncio.sleep(3 * (intento + 1))
            if not f:
                _total["vacios"] += 1
                return
            f["telefono"] = c.get("phone")
            f["ultimo_texto"] = (c.get("lastMessageBody") or "")[:200]
            f.pop("claude", None)
            _total["tokens"] += f.get("jev_tokens", 0)
            with TOTAL.open("a") as fh:
                fh.write(json.dumps(_completar(f), ensure_ascii=False, default=str) + "\n")
            _total["hechos"] += 1

    async with httpx.AsyncClient() as cli:
        await asyncio.gather(*(uno(cli, c) for c in pendientes))
    _total.update(estado="listo", segundos=round(time.time() - _total["inicio"]))


@router.get("/total/iniciar")
async def total_iniciar(k: str, maximo: int = 50000, paralelo: int = 3, desde_cero: int = 0):
    _clave(k)
    if not JEV_KEY:
        raise HTTPException(400, "Falta la variable JEV_API_KEY")
    if _total.get("estado") in ("listando", "clasificando"):
        return {"ya_corriendo": True, **{x: _total.get(x) for x in ("estado", "hechos", "pendientes", "instancia")}}
    if desde_cero and TOTAL.exists():
        TOTAL.unlink()
    t = asyncio.create_task(_correr_total(maximo, max(1, min(paralelo, 6))))
    _tareas.add(t)
    t.add_done_callback(_tareas.discard)
    return {"iniciado": True, "instancia": INSTANCIA}


def _conteo(fichas: list[dict], clave) -> list[tuple[str, int]]:
    d: dict = {}
    for f in fichas:
        x = clave(f)
        d[x] = d.get(x, 0) + 1
    return sorted(d.items(), key=lambda t: (-t[1], t[0]))


@router.get("/total/estado")
def total_estado(k: str):
    """Texto corto: avance y conteos por segmento, zona y mes."""
    _clave(k)
    from fastapi.responses import PlainTextResponse
    fichas = [_completar(f) for f in _leer_total()]
    t = _total
    seg = t.get("segundos") or (round(time.time() - t["inicio"]) if t.get("inicio") else 0)
    lin = [f"instancia={INSTANCIA} estado={t.get('estado')} conversaciones={t.get('total_conversaciones')} "
           f"clasificados_en_disco={len(fichas)} esta_corrida={t.get('hechos')} vacios={t.get('vacios')} "
           f"errores={len(t.get('errores') or [])} segundos={seg} costo_jev_usd={round((t.get('tokens') or 0) * 0.042 / 1e6, 4)}"]
    lin += [f"ERROR {e}" for e in (t.get("errores") or [])[:3]]
    lin.append("SEGMENTOS")
    lin += [f"  {s}: {n}" for s, n in sorted(_conteo(fichas, lambda f: f["segmento"]))]
    lin.append("SEGMENTOS 1-5 POR ZONA")
    lin += [f"  {s}: {n}" for s, n in _conteo([f for f in fichas if f["segmento"][0] in "12345"], lambda f: f["zona"])]
    lin.append("SEGMENTOS 1-5 POR SERVICIO")
    lin += [f"  {s}: {n}" for s, n in _conteo([f for f in fichas if f["segmento"][0] in "12345"],
                                               lambda f: (f["jev"].get("servicio") or {}).get("v"))]
    lin.append("POR MES DEL ULTIMO MENSAJE (todos / segmentos 1-5)")
    meses = _conteo(fichas, lambda f: (f["metricas"].get("ultimo_mensaje") or "?")[:7])
    m15 = dict(_conteo([f for f in fichas if f["segmento"][0] in "12345"], lambda f: (f["metricas"].get("ultimo_mensaje") or "?")[:7]))
    lin += [f"  {m}: {n} / {m15.get(m, 0)}" for m, n in sorted(meses, reverse=True)]
    lin.append(f"FLAGS esperando={sum(f['esperando'] for f in fichas)} pidio_fotos={sum(f['pidio_fotos'] for f in fichas)} "
               f"molesto={sum(f['molesto'] for f in fichas)}")
    return PlainTextResponse("\n".join(lin))


COLUMNAS = ["segmento", "nombre", "telefono", "zona", "lugar", "anio", "marca", "modelo", "servicio", "estado_final",
            "precios", "ultimo_mensaje", "dias", "ultimo_habla", "intencion", "recuperable", "esperando", "pidio_fotos",
            "molesto", "link_ghl", "contacto", "conv"]


def _fila(f: dict) -> dict:
    j, m = f["jev"], f["metricas"]
    v = lambda x: (j.get(x) or {}).get("v")  # noqa: E731
    return {"segmento": f["segmento"], "nombre": f.get("nombre") or "", "telefono": f.get("telefono") or "",
            "zona": f["zona"], "lugar": f.get("lugar") or "", "anio": v("anio") if v("anio") != "Ninguno" else "",
            "marca": v("marca"), "modelo": v("modelo"), "servicio": v("servicio"), "estado_final": f["estado_final"],
            "precios": " ".join(str(p) for p in m["precios_dados"]), "ultimo_mensaje": m["ultimo_mensaje"],
            "dias": m["dias_desde_ultimo"], "ultimo_habla": m["ultimo_habla"], "intencion": v("intencion"),
            "recuperable": v("recuperable"), "esperando": "sí" if f["esperando"] else "",
            "pidio_fotos": "sí" if f["pidio_fotos"] else "", "molesto": "sí" if f["molesto"] else "",
            "link_ghl": f"https://app.gohighlevel.com/v2/location/{config.GHL_LOCATION_ID}/conversations/conversations/{f['conv']}",
            "contacto": f.get("contacto"), "conv": f["conv"]}


def _ordenadas(fichas: list[dict]) -> list[dict]:
    return sorted((_fila(_completar(f)) for f in fichas),
                  key=lambda r: (r["segmento"], -(r["recuperable"] or 0), r["dias"] if r["dias"] is not None else 9999))


@router.get("/total/csv")
def total_csv(k: str):
    _clave(k)
    import csv
    import io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNAS, delimiter=";")
    w.writeheader()
    w.writerows(_ordenadas(_leer_total()))
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=auditoria_whatsapp.csv"})


@router.get("/total/lista")
def total_lista(k: str, seg: str = "1", zona: str = "", n: int = 15, desde: int = 0, texto: int = 0):
    """Texto corto de un segmento (para que Claude lea muestras y redacte mensajes)."""
    _clave(k)
    from fastapi.responses import PlainTextResponse
    todas = {f["conv"]: f for f in _leer_total()}
    filas = [r for r in _ordenadas(list(todas.values())) if r["segmento"].startswith(seg)
             and (not zona or zona.lower() in r["zona"].lower())]
    lin = [f"total={len(filas)}"]
    for r in filas[desde:desde + n]:
        lin.append(f"{r['nombre']} | {r['zona']} {r['lugar']} | {r['modelo']} {r['anio']} | {r['servicio']} | "
                   f"{r['estado_final']} | precios={r['precios']} | {r['ultimo_mensaje']} ({r['dias']}d, {r['ultimo_habla']}) "
                   f"| rec={r['recuperable']} | conv={r['conv']}")
        if texto:
            lin.append("   " + todas[r["conv"]].get("transcripcion", "")[-texto:].replace("\n", " / "))
    return PlainTextResponse("\n".join(lin))


@router.get("/total/panel")
def total_panel(k: str):
    """Página para Jesús: tabla filtrable por segmento y zona, con link al chat en GHL y descarga CSV."""
    _clave(k)
    from fastapi.responses import HTMLResponse
    filas = _ordenadas(_leer_total())
    datos = json.dumps(filas, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auditoría WhatsApp</title><style>
body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
h1{font-size:20px;margin:0 0 8px}.f{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
select,input,a.b{padding:8px;font-size:14px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#222;text-decoration:none}
.c{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:10px;margin:8px 0}
.s{font-size:12px;color:#666}.t{font-weight:600}.chip{display:inline-block;background:#eef;border-radius:10px;padding:1px 8px;font-size:12px;margin-right:4px}
</style></head><body><h1>Auditoría WhatsApp</h1><div id="r" class="s"></div>
<div class="f"><select id="seg"></select><select id="zona"></select><input id="q" placeholder="Buscar nombre, comuna, modelo">
<a class="b" href="csv?k=__K__">Descargar CSV</a></div><div id="l"></div>
<script>
const D=__DATOS__;const $=id=>document.getElementById(id);
function opts(el,vals,todo){el.innerHTML='<option value="">'+todo+'</option>'+vals.map(v=>'<option>'+v+'</option>').join('')}
const cuenta=(k)=>{const m={};D.forEach(r=>m[r[k]]=(m[r[k]]||0)+1);return m};
const cs=cuenta('segmento');opts($('seg'),Object.keys(cs).sort().map(s=>s),'Todos los segmentos');
[...$('seg').options].forEach(o=>{if(o.value)o.text=o.value+' ('+cs[o.value]+')'});
const cz=cuenta('zona');opts($('zona'),Object.keys(cz).sort(),'Todas las zonas');
[...$('zona').options].forEach(o=>{if(o.value)o.text=o.value+' ('+cz[o.value]+')'});
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function pinta(){const s=$('seg').value,z=$('zona').value,q=$('q').value.toLowerCase();
const F=D.filter(r=>(!s||r.segmento==s)&&(!z||r.zona==z)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
$('r').textContent=F.length+' de '+D.length+' chats';
$('l').innerHTML=F.slice(0,400).map(r=>'<div class="c"><div class="t">'+esc(r.nombre||'(sin nombre)')+' · '+esc(r.telefono)+'</div>'+
'<div><span class="chip">'+esc(r.segmento)+'</span><span class="chip">'+esc(r.zona)+(r.lugar?' · '+esc(r.lugar):'')+'</span></div>'+
'<div>'+esc(r.modelo)+' '+esc(r.anio)+' · '+esc(r.servicio)+' · '+esc(r.estado_final)+(r.precios?' · $'+esc(r.precios):'')+'</div>'+
'<div class="s">Último: '+esc(r.ultimo_mensaje)+' ('+esc(r.dias)+' días, habló '+esc(r.ultimo_habla)+') · recuperable '+esc(r.recuperable)+
(r.esperando?' · quedó esperando':'')+(r.pidio_fotos?' · pidió fotos':'')+(r.molesto?' · molesto':'')+'</div>'+
'<div><a href="'+r.link_ghl+'" target="_blank">Abrir chat en GHL</a></div></div>').join('')+(F.length>400?'<p class="s">Mostrando 400. Filtra para ver más.</p>':'')}
['seg','zona'].forEach(i=>$(i).onchange=pinta);$('q').oninput=pinta;pinta();
</script></body></html>"""
    return HTMLResponse(html.replace("__DATOS__", datos).replace("__K__", k))


# ============================================================ envío manual por WhatsApp Web
# Página con un botón por cliente: abre WhatsApp Web con el mensaje ya escrito y Jesús solo aprieta Enviar.
# El servidor no envía nada.
from urllib.parse import quote  # noqa: E402

PRESENTA = "Hola, te escribe Jesús Perdomo, ingeniero especialista en programación de vehículos BMW."
RUTAS = {"B5": "El jueves 22 y viernes 23 de octubre voy a estar atendiendo a domicilio desde Curicó hasta Concepción"}


def _auto(r: dict) -> str:
    mod = r.get("modelo") or ""
    if not mod or mod.startswith("No ") or mod.startswith("BMW eléctrico") or mod.startswith("BMW M "):
        return "tu BMW" if r.get("marca") != "MINI" else "tu MINI"
    return "tu " + mod


def mensaje(r: dict) -> str:
    bloque = r["zona"] if not r["zona"].startswith("RM") else "RM"
    ruta = RUTAS.get(bloque)
    auto = _auto(r)
    s = r["segmento"][0]
    if s == "1":
        base = f"{PRESENTA} Disculpa que no te respondí cuando me escribiste por {auto}."
        cierre = "Si todavía necesitas la llave dime en qué comuna estás y te paso el valor."
    elif s == "2":
        base = f"{PRESENTA} Me pasaste los datos de {auto} y nunca te mandé el valor, disculpa."
        cierre = "Si sigues necesitando la llave mándame una foto de la que tienes y te cotizo hoy mismo."
    else:
        base = f"{PRESENTA} Hace un tiempo te coticé una llave para {auto}."
        cierre = "Me quedan pocos cupos, ¿te reservo uno?" if ruta else "¿Sigues necesitándola?"
    if ruta:
        medio = ruta + (", así no tienes que viajar a Santiago." if s not in "12" else ", con pocos cupos." if s == "1" else ".")
    elif bloque == "RM":
        medio = "Atiendo a domicilio en todo Santiago, incluso el mismo día."
    else:
        medio = ""
    return " ".join(x for x in (base, medio, cierre) if x)


def _apto(r: dict) -> bool:
    return (r["segmento"][0] in "12345" and (r["dias"] or 0) >= 3 and r.get("telefono")
            and r.get("marca") in ("BMW", "MINI", "No lo dice") and r.get("modelo") != "No es BMW ni MINI"
            and r.get("servicio") != "Otro")


@router.get("/total/enviar")
def total_enviar(k: str, zona: str = "B5", seg: str = "12345", n: int = 300):
    _clave(k)
    from fastapi.responses import HTMLResponse
    filas = [r for r in _ordenadas(_leer_total()) if _apto(r) and r["segmento"][0] in seg
             and (zona == "RM" and r["zona"].startswith("RM") or zona != "RM" and r["zona"] == zona)][:n]
    items = []
    for r in filas:
        tel = re.sub(r"\D", "", r["telefono"])
        txt = mensaje(r)
        items.append({"conv": r["conv"], "nombre": r["nombre"], "tel": r["telefono"], "auto": f"{r['modelo']} {r['anio'] or ''}",
                      "lugar": r["lugar"], "seg": r["segmento"], "dias": r["dias"], "txt": txt,
                      "web": f"https://web.whatsapp.com/send?phone={tel}&text={quote(txt)}",
                      "ghl": r["link_ghl"]})
    datos = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    html = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Envío WhatsApp</title><style>
body{font-family:system-ui,sans-serif;margin:16px;background:#f6f6f6;color:#222;max-width:760px}
h1{font-size:20px;margin:0 0 4px}.s{font-size:13px;color:#666}
.c{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}.c.ok{opacity:.45}
.t{font-weight:600}.m{background:#eef7ee;border-radius:6px;padding:8px;margin:8px 0;white-space:pre-wrap;font-size:14px}
a.b,button{display:inline-block;padding:9px 14px;border-radius:6px;border:0;font-size:14px;cursor:pointer;text-decoration:none;margin-right:6px}
a.b{background:#1f8f4e;color:#fff}button{background:#e4e4e4;color:#222}a.g{font-size:13px}
</style></head><body><h1>Envío WhatsApp · __ZONA__</h1><div class="s" id="r"></div><div id="l"></div>
<script>
const D=__DATOS__;const KEY='env_'+'__ZONA__';let H={};try{H=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function guarda(){try{localStorage.setItem(KEY,JSON.stringify(H))}catch(e){}}
function marca(id){H[id]=!H[id];guarda();pinta()}
function abre(i){H[D[i].conv]=true;guarda();setTimeout(pinta,500)}
function pinta(){const n=D.filter(x=>H[x.conv]).length;document.getElementById('r').textContent=n+' de '+D.length+' enviados. Envía de a 15 o 20 por día, con un par de minutos entre cada uno.';
document.getElementById('l').innerHTML=D.map((x,i)=>'<div class="c'+(H[x.conv]?' ok':'')+'"><div class="t">'+(i+1)+'. '+esc(x.nombre||'(sin nombre)')+' · '+esc(x.tel)+'</div>'+
'<div class="s">'+esc(x.auto)+' · '+esc(x.lugar)+' · '+esc(x.seg)+' · hace '+esc(x.dias)+' días</div><div class="m">'+esc(x.txt)+'</div>'+
'<a class="b" href="'+x.web+'" target="wa" onclick="abre('+i+')">Abrir en WhatsApp Web</a><button onclick="marca(\\''+x.conv+'\\')">'+(H[x.conv]?'Desmarcar':'Marcar enviado')+'</button>'+
'<a class="g" href="'+x.ghl+'" target="_blank">Ver chat</a></div>').join('')}
pinta();
</script></body></html>"""
    return HTMLResponse(html.replace("__DATOS__", datos).replace("__ZONA__", zona))


# ============================================================ carga a GHL (autorizada por Jesús 13-10-2026)
# Solo para clientes de REGIONES de los segmentos 1-5: comuna, región, bloque, zona operativa y etiqueta de ruta.
# No toca contactos que ya tienen la comuna cargada, no mueve oportunidades ni envía mensajes.
_carga: dict = {"estado": "sin_iniciar"}


async def _correr_carga(seg: str, dry: bool):
    import regiones
    _carga.update(estado="corriendo", hechos=0, ya_tenian=0, sin_lugar=0, errores=[], muestra=[], inicio=time.time())
    filas = {f["conv"]: _completar(f) for f in _leer_total()}.values()
    objetivo = [f for f in filas if f["region"] and f["segmento"][0] in seg and f.get("contacto")]
    _carga["total"] = len(objetivo)
    for f in objetivo:
        try:
            c = regiones.clasificar(f["lugar"] or "")
            if not c or c["tipo"] != "region":
                _carga["sin_lugar"] += 1
                continue
            actual = await ghl.contacto(f["contacto"])
            tiene = any(x.get("id") == config.CAMPOS["comuna"] and x.get("value") for x in actual.get("customFields") or [])
            etiquetas = [c["etiqueta"], "auditoria-jev"]
            if len(_carga["muestra"]) < 5:
                _carga["muestra"].append(f"{f.get('nombre')}: {c['campos']} {etiquetas} ya_tenia_comuna={tiene}")
            if not dry:
                if not tiene:
                    await ghl.actualizar_campos(f["contacto"], {**c["campos"], "comuna_detectada": c["ciudad"]})
                await ghl.agregar_etiquetas(f["contacto"], etiquetas)
            _carga["ya_tenian" if tiene else "hechos"] += 1
            await asyncio.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            _carga["errores"].append(f"{f.get('contacto')}: {str(e)[:150]}")
    _carga.update(estado="listo", segundos=round(time.time() - _carga["inicio"]))


@router.get("/total/cargar_ghl")
async def total_cargar_ghl(k: str, seg: str = "12345", dry: int = 1):
    _clave(k)
    if _carga.get("estado") == "corriendo":
        return {k2: v for k2, v in _carga.items() if k2 != "inicio"}
    t = asyncio.create_task(_correr_carga(seg, bool(dry)))
    _tareas.add(t)
    t.add_done_callback(_tareas.discard)
    return {"iniciado": True, "dry": bool(dry)}


@router.get("/total/cargar_ghl/estado")
def total_cargar_estado(k: str):
    _clave(k)
    return {**{k2: v for k2, v in _carga.items() if k2 not in ("inicio", "errores")},
            "errores": len(_carga.get("errores") or []), "primeros_errores": (_carga.get("errores") or [])[:3]}
