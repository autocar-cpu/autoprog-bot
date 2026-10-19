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
