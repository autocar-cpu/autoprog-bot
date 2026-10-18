"""Duplicado de llave keyless para motos BMW GS (pauta Meta desde 16-10-2026, definida por Jesús).
Solo R 1200 GS / GS Adventure refrigerada por agua (2014-2018) y R 1250 GS / GS Adventure (2019-2023),
con arranque por botón (keyless), cliente con una llave que funcione, en la Región Metropolitana."""
from __future__ import annotations

import re

from cotizador import resolver_lugar

PRECIO_RM = 240000   # Jesús 17-10-2026 (antes $250.000)
RANGOS = {"R 1200 GS": (2014, 2018), "R 1250 GS": (2019, 2023)}

_ES_MOTO = re.compile(r"\bmotos?\b|\bmotocicleta|\bgs\b|\bgsa\b|r\s?12[05]0|\badventure\b|\bkeyless\b", re.I)


def es_moto(textos: list[str]) -> bool:
    return any(_ES_MOTO.search(t or "") for t in textos)


def modelo_gs(texto: str) -> str | None:
    t = (texto or "").lower().replace(" ", "")
    if "1250" in t:
        return "R 1250 GS"
    if "1200" in t:
        return "R 1200 GS"
    return None


def cotizar_moto(modelo: str = "", anio: int | None = None, lugar: str = "", arranque: str = "",
                 situacion: str = "duplicado") -> dict:
    """arranque: 'boton' | 'llave' | 'no_sabe'. situacion: 'duplicado' | 'akl' | 'no_sabe'."""
    falta = []
    m = modelo_gs(modelo)
    if not modelo:
        falta.append("modelo")
    if not anio:
        falta.append("anio")
    if not lugar:
        falta.append("comuna")
    if arranque not in ("boton", "llave"):
        falta.append("arranque")
    if situacion not in ("duplicado", "akl"):
        falta.append("situacion")
    if modelo and not m:
        return {"estado": "escalar", "motivo": "Otro modelo de moto BMW (no es R 1200 GS ni R 1250 GS)."}
    if arranque == "llave":
        return {"estado": "lista_espera", "etiqueta": "espera_moto_bmw",
                "motivo": "La moto arranca girando la llave en la chapa. No se cotiza: dile que estamos esperando esas "
                          "llaves y que lo dejas anotado para avisarle apenas lleguen. No des precio."}
    if situacion == "akl":
        return {"estado": "lista_espera", "etiqueta": "espera_moto_bmw",
                "motivo": "Perdió todas las llaves. Dile que por ahora no hacemos pérdida total en motos y que lo dejas "
                          "anotado para avisarle cuando lo hagamos. No des precio ni menciones el del duplicado."}
    if m and anio and not (RANGOS[m][0] <= int(anio) <= RANGOS[m][1]):
        return {"estado": "escalar", "motivo": f"{m} {anio}: año fuera de lo que se ofrece hoy."}
    if lugar:
        l = resolver_lugar(lugar)
        if l.tipo == "region":
            return {"estado": "escalar", "motivo": f"Moto fuera de Santiago ({l.nombre}): no dar precio, Jesús lo agenda en ruta."}
        if l.tipo != "rm":
            falta.append("comuna")
    if falta:
        if arranque != "boton":   # Jesús 17-10: si no se sabe si es de botón o de chapa, primero se pregunta
            return {"estado": "falta_dato", "falta": sorted(set(falta)),
                    "instruccion": "Antes de dar precio pregunta: ¿arranca con botón (llave en el bolsillo) o girando "
                                   "la llave en la chapa?"}
        return {"estado": "falta_dato", "falta": sorted(set(falta)), "precio": PRECIO_RM,
                "instruccion": "Puedes dar el precio y en la misma frase pedir lo que falta. Si después resulta "
                               "fuera de Santiago, que gira llave, que perdió todas o es otro modelo/año, escala."}
    return {"estado": "cotizar", "precio": PRECIO_RM, "modelo": f"{m} {anio}",
            "nota_cliente": ("Duplicado de llave keyless con llave y programación incluidas, a domicilio el mismo día, "
                             "queda lista en menos de 1 hora. La llave de emergencia se copia de su llave original.")}
