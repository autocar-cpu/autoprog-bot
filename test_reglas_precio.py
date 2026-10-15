import asyncio, json
import cerebro


def _run(args, previas):
    ctx = cerebro.Contexto("x", "Ana", [], simulacion=True, cotizaciones_previas=previas)
    res = cerebro.Resultado()
    montos = set()
    out = json.loads(asyncio.run(cerebro._ejecutar("cotizar", args, ctx, res, montos)))
    return out, montos


def test_no_baja_al_cambiar_comuna():
    a = {"modelo": "X6", "anio": 2026, "lugar": "Puente Alto", "situacion": "duplicado"}
    out, _ = _run(a, [])
    p3 = out["precio"]
    b = dict(a, lugar="Providencia")
    out2, montos = _run(b, [{"clave": cerebro.clave_cotizacion(a), "precio": p3}])
    assert out2["precio"] == p3 and p3 in montos


def test_otra_llave_si_puede_cambiar():
    a = {"modelo": "X6", "anio": 2026, "lugar": "Las Condes", "situacion": "duplicado", "tipo_llave": "pantalla"}
    out, _ = _run(a, [])
    b = dict(a, tipo_llave="")
    out2, _ = _run(b, [{"clave": cerebro.clave_cotizacion(a), "precio": out["precio"]}])
    assert out2["precio"] < out["precio"]


def test_tope_de_autos():
    prev = [{"clave": ["X1", 2015, "", "duplicado"], "precio": 1}, {"clave": ["X3", 2016, "", "duplicado"], "precio": 1}]
    out, _ = _run({"modelo": "X5", "anio": 2018, "lugar": "Ñuñoa", "situacion": "duplicado"}, prev)
    assert out["estado"] == "escalar"
