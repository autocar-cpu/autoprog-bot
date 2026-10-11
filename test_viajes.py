"""Pipeline VIAJES REGIONES: cada cliente de regiones queda en la columna de su ruta."""
import asyncio

import ghl

P = {"id": "PV", "etapas": {"ruta o'higgins": "S1", "ruta maule ñuble concepción": "S3",
                            "fecha confirmada": "SF", "ganado": "SG"}}


def _correr(monkeypatch, op):
    llamadas = []

    async def req(metodo, ruta, version="2021-07-28", **kw):
        llamadas.append((metodo, ruta, kw.get("json")))
        return {}

    async def pv():
        return P

    async def oa(cid):
        return op
    monkeypatch.setattr(ghl, "_req", req)
    monkeypatch.setattr(ghl, "pipeline_viajes", pv)
    monkeypatch.setattr(ghl, "oportunidad_abierta", oa)
    ok = asyncio.run(ghl.mover_a_viajes("c", "Coke", "Ruta O'Higgins", 160000))
    return ok, llamadas


def test_desde_bmw_mini_pasa_a_columna_de_ruta(monkeypatch):
    ok, ll = _correr(monkeypatch, {"id": "O1", "pipelineId": "BMW", "pipelineStageId": "X"})
    assert ok and ll == [("PUT", "/opportunities/O1", {"pipelineId": "PV", "pipelineStageId": "S1",
                                                         "monetaryValue": 160000})]


def test_fecha_confirmada_no_se_devuelve_a_ruta(monkeypatch):
    ok, ll = _correr(monkeypatch, {"id": "O1", "pipelineId": "PV", "pipelineStageId": "SF"})
    assert ok and ll == [("PUT", "/opportunities/O1", {"monetaryValue": 160000})]
