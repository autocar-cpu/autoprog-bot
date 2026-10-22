import asyncio
import datetime as dt

import ghl


def test_horarios_desde_mira_la_semana_pedida(monkeypatch):
    llamadas = []

    async def req(metodo, ruta, version="", **kw):
        llamadas.append(kw["params"])
        return {}

    monkeypatch.setattr(ghl, "_req", req)
    destino = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    asyncio.run(ghl.horarios_libres(dias=5, desde=destino))
    ini = dt.datetime.fromtimestamp(llamadas[0]["startDate"] / 1000, dt.timezone.utc)
    assert ini.date() >= dt.date.today() + dt.timedelta(days=6)
    assert llamadas[0]["endDate"] - llamadas[0]["startDate"] == 5 * 86400 * 1000
