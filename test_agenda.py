import asyncio, json
import cerebro
from revisor import revisar


def test_prohibe_confirmo_despues():
    assert revisar(["dejame dejar confirmada la visita en la agenda y te aviso por aca en un momento"], set())
    assert revisar(["te confirmo despues la hora"], set())
    assert not revisar(["tengo mañana 10:00 o 15:00, cual te sirve?"], set())


def test_no_confirma_sin_cita():
    assert cerebro.CONFIRMA_CITA.search("si, hoy a las 17:30 se puede")
    assert cerebro.CONFIRMA_CITA.search("listo, quedas agendado para mañana")
    assert not cerebro.CONFIRMA_CITA.search("tengo mañana 10:00 o 15:00, cual te sirve?")


def test_agendar_simulacion_marca_ok():
    ctx = cerebro.Contexto("x", "Ana", [], simulacion=True)
    res = cerebro.Resultado()
    out = asyncio.run(cerebro._ejecutar("agendar", {"inicio": "2026-10-20T10:00:00-03:00", "direccion": "x 1",
                                                    "resumen": "r"}, ctx, res, set()))
    assert "Cita creada" in out and res.acciones[0]["agendar_ok"]


def test_fecha_es():
    assert cerebro.fecha_es("2026-10-20T15:00:00-03:00") == "martes 20 de octubre a las 15:00"


def _ctx_con_cita():
    return cerebro.Contexto("c1", "Ana", [], simulacion=False,
                            citas=[{"id": "v1", "inicio": "2026-10-20T15:00:00-03:00", "direccion": "Julieta 1750",
                                    "titulo": "x"}])


def test_no_duplica_si_ya_tiene_cita(monkeypatch):
    llamado = []
    monkeypatch.setattr(cerebro.ghl, "agendar", lambda *a, **k: llamado.append(a))
    res = cerebro.Resultado()
    out = asyncio.run(cerebro._ejecutar("agendar", {"inicio": "2026-10-21T10:00:00-03:00", "direccion": "x 1",
                                                    "resumen": "r"}, _ctx_con_cita(), res, set()))
    assert "ya tiene cita" in out and not llamado and not res.acciones[0]["agendar_ok"]


def test_reemplaza_borra_la_anterior(monkeypatch):
    borradas = []

    async def libre(_):
        return True, []

    async def agendar(*a):
        return {"id": "nueva"}

    async def borrar(i):
        borradas.append(i)

    async def mover(*a):
        return None
    monkeypatch.setattr(cerebro.ghl, "revisar_hora", libre)
    monkeypatch.setattr(cerebro.ghl, "agendar", agendar)
    monkeypatch.setattr(cerebro.ghl, "borrar_cita", borrar)
    monkeypatch.setattr(cerebro.ghl, "mover_oportunidad", mover)
    res = cerebro.Resultado()
    out = asyncio.run(cerebro._ejecutar("agendar", {"inicio": "2026-10-21T10:00:00-03:00", "direccion": "x 1",
                                                    "resumen": "r", "reemplaza_cita": True},
                                        _ctx_con_cita(), res, set()))
    assert "Cita creada" in out and borradas == ["v1"]


def test_reemplaza_misma_hora_no_crea_nada():
    res = cerebro.Resultado()
    out = asyncio.run(cerebro._ejecutar("agendar", {"inicio": "2026-10-20T15:00:00-03:00", "direccion": "x 1",
                                                    "resumen": "r", "reemplaza_cita": True},
                                        _ctx_con_cita(), res, set()))
    assert "ya es su cita actual" in out
