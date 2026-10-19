import asyncio

import app
import ghl


def test_mensaje_confirmacion():
    t = app.mensaje_confirmacion("Enrique", "2026-10-21T10:00:00-03:00",
                                 "Av. Trinidad Ote. 833, 8300558 La Florida, Región Metropolitana")
    assert "Hola Enrique, tu visita quedó confirmada para el miércoles 21 de octubre a las 10:00." in t
    assert "Dirección: Av. Trinidad Ote. 833" in t and t.endswith("Autoprog SPA")


def test_no_confirma_citas_del_bot(monkeypatch):
    enviados = []

    async def citas(cid):
        return [{"id": "BOT1", "inicio": "2026-10-21T10:00:00-03:00", "direccion": "x", "titulo": "", "creada_por": ""}]

    async def enviar(cid, texto):
        enviados.append(texto)

    monkeypatch.setattr(ghl, "citas_futuras", citas)
    monkeypatch.setattr(ghl, "enviar_whatsapp", enviar)
    ghl.citas_bot.add("BOT1")
    asyncio.run(app._confirmar_cita_manual("c1", espera=0))
    assert enviados == []


def test_confirma_cita_manual_una_vez(monkeypatch):
    enviados, movidas = [], []

    async def citas(cid):
        return [{"id": "MAN1", "inicio": "2026-10-21T10:00:00-03:00",
                 "direccion": "Julieta 1750, San Miguel", "titulo": "", "creada_por": "contactdetails_page"}]

    async def enviar(cid, texto):
        enviados.append(texto)
        return "m1"

    async def contacto(cid):
        return {"firstName": "Ana"}

    async def mover(*a, **k):
        movidas.append(a)

    async def pipe(cid):
        return "BMW MINI"

    monkeypatch.setattr(ghl, "citas_futuras", citas)
    monkeypatch.setattr(ghl, "enviar_whatsapp", enviar)
    monkeypatch.setattr(ghl, "contacto", contacto)
    monkeypatch.setattr(ghl, "mover_oportunidad", mover)
    monkeypatch.setattr(ghl, "pipeline_de_oportunidad", pipe)
    asyncio.run(app._confirmar_cita_manual("c2", espera=0))
    asyncio.run(app._confirmar_cita_manual("c2", espera=0))   # webhook repetido: no reenvía
    assert len(enviados) == 1 and "Julieta 1750, San Miguel" in enviados[0]
    assert movidas and movidas[0][3] == "Agendado"
