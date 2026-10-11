"""Reproduce los chats que quedaron sin respuesta el 11-10-2026 (Marco, Hector, Jd, Carlos, Yo Aqui)
con un GHL y un cerebro falsos, sin gastar API."""
import asyncio

import app
import cerebro
import config
import ghl

ESPERAR = asyncio.sleep     # sleep real, para las escenas de prueba


class GHLFalso:
    def __init__(self, conv_visible_en=0):
        self.msgs, self.enviados, self.busquedas = [], [], 0
        self.conv_visible_en = conv_visible_en       # nº de búsquedas que fallan antes de encontrar la conversación

    def cliente(self, id_, texto):
        n = len(self.msgs)
        self.msgs.append({"id": id_, "messageType": "TYPE_WHATSAPP", "direction": "inbound", "body": texto,
                          "dateAdded": f"2026-10-11T20:59:{n:02d}.000Z"})

    async def contacto(self, cid):
        return {"tags": [], "firstName": "Marco"}

    async def buscar_conversacion(self, cid):
        self.busquedas += 1
        return "conv1" if self.busquedas > self.conv_visible_en else None

    async def mensajes(self, conv, limite=40):
        return list(self.msgs)

    async def enviar_whatsapp(self, cid, texto):
        mid = f"bot{len(self.enviados)}"
        self.enviados.append(texto)
        self.msgs.append({"id": mid, "messageType": "TYPE_WHATSAPP", "direction": "outbound", "body": texto,
                          "dateAdded": f"2026-10-11T21:00:{len(self.msgs):02d}.000Z", "meta": {"marketplace": {}}})
        return mid


def _preparar(monkeypatch, falso, demora_pensar=0.3):
    for n in ("contacto", "buscar_conversacion", "mensajes", "enviar_whatsapp"):
        monkeypatch.setattr(ghl, n, getattr(falso, n))
    monkeypatch.setattr(config, "MODO", "activo")
    monkeypatch.setattr(config, "ESPERA_SEGUNDOS", 0.2)
    app._procesados.clear(); app._estado.clear()

    async def pensar(ctx, modelo_forzado=None):
        await ESPERAR(demora_pensar)
        r = cerebro.Resultado()
        r.mensajes = ["resp a: " + ctx.historial[-1]["texto"]]
        return r
    monkeypatch.setattr(cerebro, "pensar", pensar)

    async def sleep_rapido(s):        # los reintentos de 6 s se acortan en la prueba
        await ESPERAR(min(s, 0.25))
    monkeypatch.setattr(app.asyncio, "sleep", sleep_rapido)


def test_mensaje_que_llega_mientras_piensa_se_responde(monkeypatch):
    """Caso Marco: el 2º mensaje ya estaba en GHL cuando el bot leyó, pero su webhook llegó
    con el bot pensando. Antes: se cancelaba y el 2º quedaba 'ya procesado' sin respuesta."""
    f = GHLFalso()
    _preparar(monkeypatch, f)

    async def escena():
        f.cliente("m1", "Tengo hoy solo una")
        app._disparar("c1", config.ESPERA_SEGUNDOS, "webhook")
        await ESPERAR(0.25)                  # el bot ya empezó a atender
        f.cliente("m2", "Necesito una back up")
        await ESPERAR(0.05)
        app._disparar("c1", config.ESPERA_SEGUNDOS, "webhook")   # webhook atrasado del 2º mensaje
        await ESPERAR(2)

    asyncio.run(escena())
    assert f.enviados, "el cliente quedó sin respuesta"
    assert f.enviados[-1] == "resp a: Necesito una back up"


def test_lead_nuevo_sin_conversacion_indexada(monkeypatch):
    """Caso Carlos / Yo Aqui: GHL no encontraba la conversación recién creada."""
    f = GHLFalso(conv_visible_en=2)
    _preparar(monkeypatch, f, demora_pensar=0.01)

    async def escena():
        f.cliente("m1", "Qué valor para x6 año 2018?")
        app._disparar("c2", 0, "webhook")
        await ESPERAR(2)

    asyncio.run(escena())
    assert f.enviados == ["resp a: Qué valor para x6 año 2018?"]


def test_no_responde_dos_veces(monkeypatch):
    f = GHLFalso()
    _preparar(monkeypatch, f, demora_pensar=0.05)

    async def escena():
        f.cliente("m1", "Hola")
        app._disparar("c3", 0, "webhook")
        await ESPERAR(0.5)
        app._disparar("c3", 0, "barrido")
        await ESPERAR(0.5)

    asyncio.run(escena())
    assert len(f.enviados) == 1


def test_acuses_no_se_rescatan():
    for t in ("Ok", "okey.", "Vale", "Gracias!", "dale", "Perfecto"):
        assert app.ACUSE.match(t)
    for t in ("Huechuraba", "Se puede con la llave del 120?", "Necesito una back up"):
        assert not app.ACUSE.match(t)
