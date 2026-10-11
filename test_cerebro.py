"""Prueba del ciclo del cerebro con un Claude falso (sin gastar API)."""
import asyncio
from types import SimpleNamespace as NS

import cerebro


def _uso(id_, nombre, entrada):
    return NS(type="tool_use", id=id_, name=nombre, input=entrada)


class FalsoClaude:
    def __init__(self, guion):
        self.guion, self.llamadas = list(guion), []
        self.messages = self

    async def create(self, **kw):
        self.llamadas.append(kw)
        return NS(content=self.guion.pop(0))


def correr(guion):
    falso = FalsoClaude(guion)
    cerebro._cliente = falso
    ctx = cerebro.Contexto("x", "Felipe", [{"rol": "cliente", "texto": "Hola, X6 2019 en Las Condes, necesito copia"}])
    return asyncio.run(cerebro.pensar(ctx)), falso


def test_precio_inventado_se_bloquea_y_corrige():
    r, f = correr([
        [_uso("1", "cotizar", {"modelo": "X6", "anio": 2019, "lugar": "Las Condes", "situacion": "duplicado"})],
        [_uso("2", "enviar_respuesta", {"mensajes": ["Te sale $180.000 a domicilio"]})],   # inventado
        [_uso("3", "enviar_respuesta", {"mensajes": ["Para tu X6 2019 en Las Condes queda en $240.000 a domicilio."]})],
    ])
    assert r.mensajes == ["Para tu X6 2019 en Las Condes queda en $240.000 a domicilio."]
    assert f.llamadas[-1]["model"] == cerebro.config.MODELO_DIFICIL


def test_sin_cotizar_no_sale_precio():
    r, _ = correr([[_uso("1", "enviar_respuesta", {"mensajes": ["Sale $100.000"]})]] * 3)
    assert r.mensajes == [] and r.escalado


def test_emoji_rechazado():
    r, _ = correr([
        [_uso("1", "enviar_respuesta", {"mensajes": ["Hola 👋 ¿en qué comuna estás?"]})],
        [_uso("2", "enviar_respuesta", {"mensajes": ["Hola, ¿en qué comuna estás?"]})],
    ])
    assert r.mensajes == ["Hola, ¿en qué comuna estás?"]


def test_anio_no_es_precio():
    from revisor import revisar
    assert revisar(["Tu X6 del 2019, a las 10:00 del 12-10"], set()) == []


def test_no_menciona_nombre_de_jesus():
    from revisor import revisar
    assert revisar(["Hola, te escribe Jesús, ingeniero especialista"], set())
    assert revisar(["Soy Jesus Perdomo"], set())
    assert revisar(["Hola, te escribo de Autoprog, programación de llaves BMW a domicilio"], set()) == []
