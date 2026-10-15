"""Cotizador determinista de Autoprog.

La IA nunca calcula precios: solo extrae datos (modelo, año, lugar, situación)
y llama a `cotizar()`. Todo precio que el bot envía tiene que salir de aquí.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import get_close_matches
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "data" / "decodificador.json").read_text())


def _norm(texto: str) -> str:
    """Mayúsculas, sin tildes ni símbolos: 'Ñuñoa' -> 'NUNOA'."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", t.upper())


# ---------------------------------------------------------------- modelos
DECO = {f'{r["MODELO"]}_{int(r["ANIO"])}': r for r in DATA["DECODIFICADOR"]}
MODELOS = sorted({r["MODELO"] for r in DATA["DECODIFICADOR"]}, key=len, reverse=True)
MODELOS_NORM = {_norm(m): m for m in MODELOS}
POR_SERIE: dict[tuple[str, int], list[dict]] = {}
for r in DATA["DECODIFICADOR"]:
    POR_SERIE.setdefault((str(r["SERIE"]).upper(), int(r["ANIO"])), []).append(r)


def normalizar_modelo(texto: str) -> str | None:
    """'320d xDrive' -> '320', 'X5 xDrive40i' -> 'X5', 'Mini Cooper S' -> 'MINI COOPER S'."""
    if not texto:
        return None
    t = _norm(texto)
    if "MINI" in t or any(k in t for k in ("COOPER", "COUNTRYMAN", "CLUBMAN", "PACEMAN", "ACEMAN", "JCW")):
        # primero las variantes con nombre propio (Countryman, Clubman, JCW...), después Cooper/One
        for m in MODELOS:
            if m.startswith("MINI") and "COOPER" not in m and m != "MINI ONE" and _norm(m[5:]) in t:
                return m
        for m in ("MINI COOPER S", "MINI COOPER E", "MINI COOPER", "MINI ONE"):
            if _norm(m[5:]) in t:
                return m
        return None
    crudo = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().upper()
    patrones = [
        r"(?<![A-Z0-9])(IX3|IX|I[3-8])(?![0-9])",
        r"(?<![A-Z0-9])(M\d{3})",
        r"(?<![A-Z0-9])(1M)(?![0-9])",
        r"(?<![A-Z0-9])(M[2-8])(?![0-9])",
        r"(?<![A-Z0-9])(X[1-7])(?![0-9])",
        r"(?<![A-Z0-9])(Z4)(?![0-9])",
        r"(?<![A-Z0-9])(128TI)",
        r"(?<![A-Z0-9])(\d{3})(?![0-9])",
    ]
    for p in patrones:
        m = re.search(p, crudo.replace(" ", " "))
        if m and m.group(1) in MODELOS:
            return m.group(1)
    return MODELOS_NORM.get(t)


def serie_desde_texto(texto: str) -> str | None:
    """'Serie 3' -> '3'. Sirve cuando el cliente no da el modelo exacto."""
    m = re.search(r"SERIE\s*([1-8])", unicodedata.normalize("NFKD", texto).upper())
    return m.group(1) if m else None


# ---------------------------------------------------------------- lugares
ZONAS = {r["CLAVE"]: r for r in DATA["ZONAS_RM"]}
BLOQUES = {r["CLAVE"]: r for r in DATA["BLOQUES_REGION"]}
ALIAS = {
    # sectores que no son comuna -> comuna o ciudad de la tabla
    "CHICUREO": "COLINA", "LADEHESA": "LOBARNECHEA", "LOSDOMINICOS": "LASCONDES",
    "ELGOLF": "LASCONDES", "SANCARLOSDEAPOQUINDO": "LASCONDES", "ESTORIL": "LASCONDES",
    "SANTAMARIADEMANQUEHUE": "VITACURA", "LOCURRO": "VITACURA", "LAFLORESTA": "PUENTEALTO",
    "BELLAVISTA": "PROVIDENCIA", "BARRIOITALIA": "PROVIDENCIA", "LASTARRIA": "SANTIAGO",
    "SANTIAGOCENTRO": "SANTIAGO", "STGO": "SANTIAGO", "STGOCENTRO": "SANTIAGO", "CENTRO": "SANTIAGO",
    "CIUDADEMPRESARIAL": "HUECHURABA", "CIUDADSATELITE": "MAIPU", "VALLEGRANDE": "LAMPA",
    "PIEDRAROJA": "COLINA", "LOSTRAPENSES": "LOBARNECHEA", "ELARRAYAN": "LOBARNECHEA",
    "VINA": "VINADELMAR", "VINADELMAR": "VINADELMAR", "VALPO": "VALPARAISO", "CONCE": "CONCEPCION",
    "PTOMONTT": "PUERTOMONTT", "PTOVARAS": "PUERTOVARAS", "STGOMAIPU": "MAIPU",
}
AMBIGUOS = {"SANPEDRO": ["San Pedro (Melipilla, RM)", "San Pedro de la Paz", "San Pedro de Atacama"]}
# Prohibido ofrecer bodega u oficina (regla 17/08/2026): todo servicio es a domicilio.


@dataclass
class Lugar:
    tipo: str            # "rm" | "region" | "ambiguo" | "desconocido"
    nombre: str = ""
    zona: str = ""
    recargo: int = 0
    bloque: dict | None = None
    opciones: list[str] = field(default_factory=list)


def resolver_lugar(texto: str) -> Lugar:
    k = _norm(texto)
    if not k:
        return Lugar("desconocido")
    if k in AMBIGUOS:
        return Lugar("ambiguo", opciones=AMBIGUOS[k])
    k = ALIAS.get(k, k)
    if k in ZONAS:
        z = ZONAS[k]
        return Lugar("rm", z["COMUNA"], z["ZONA"], int(z["RECARGO"]))
    if k in BLOQUES:
        b = BLOQUES[k]
        return Lugar("region", b["CIUDAD"], b["BLOQUE"], 0, b)
    cand = get_close_matches(k, list(ZONAS) + list(BLOQUES) + list(ALIAS), n=2, cutoff=0.85)
    if len(cand) == 1 or (cand and get_close_matches(k, cand, n=1, cutoff=0.92)):
        return resolver_lugar(ALIAS.get(cand[0], cand[0]) if cand[0] in ALIAS else cand[0])
    return Lugar("desconocido", texto)


# ---------------------------------------------------------------- precios
COL_REGION = {"CAS": "P_CAS", "CAS4_FEM": "P_CAS4_FEM", "EWS": "P_EWS",
              "BDC": "P_BDC", "BDC23": "P_BDC23", "BCP": "P_BCP"}
# Llave con pantalla (LCD): precio zona 1 RM y por bloque regional (Precios BMW.md)
LCD_RM = {"con_p": 590000, "sin_p": 529000}
LCD_REGION = {  # por grupo de bloque
    "con_p": {"B9": 660000, "B2": 660000, "B3": 660000, "B4": 660000, "B1": 740000, "B5": 740000,
              "B8": 790000, "B6": 790000, "B7": 790000, "H1": 840000, "H2": 840000, "H3": 840000, "H4": 840000},
    "sin_p": {"B9": 599000, "B2": 599000, "B3": 599000, "B4": 599000, "B1": 679000, "B5": 679000,
              "B8": 729000, "B6": 729000, "B7": 729000, "H1": 779000, "H2": 779000, "H3": 779000, "H4": 779000},
}
# AKL (regla de Jesús, 10/10/2026): 2x el duplicado de ese auto en esa zona.
# Excepción: ECU bajo el múltiple de admisión (enfriada por el aire de admisión) -> 3x.
# Supuesto: motor N20/N26 (DME MEVD17.2.4), años 2011-2017. Como el cliente no sabe el motor,
# en esos modelos se cotiza rango 2x-3x y se confirma al revisar.
AKL_FACTOR, AKL_FACTOR_ECU_ADMISION = 2, 3
ECU_BAJO_ADMISION = {  # modelo: (año desde, año hasta) donde pudo venir N20/N26
    "120": (2012, 2016), "125": (2012, 2016), "220": (2014, 2016), "228": (2014, 2016),
    "320": (2012, 2016), "328": (2012, 2016), "420": (2013, 2016), "428": (2013, 2016),
    "520": (2011, 2016), "528": (2011, 2016), "X1": (2011, 2015), "X3": (2011, 2017),
    "X4": (2014, 2017), "Z4": (2011, 2016),
}
# Llave #7 (BCP, nueva generación). Aprobado por Jesús 15/10/2026: precio único en toda la RM (sin recargo
# de zona) y en regiones según P_BCP del bloque: B9/B2-B4 $550.000, B1/B5 $580.000, B6-B8 $630.000, H $680.000.
BCP_RM = 490000
AKL_NO_FACTIBLE = ("BDC3", "BCP")   # no se hace AKL (perfil de Jesús, oct 2026)

# BDC reciente o de gama alta (regla de Jesús, 10/10/2026): no puede costar lo mismo que un auto antiguo.
# Valor propuesto por Claude: +$60.000 sobre la tabla (RM $160k -> $220k; regiones $240k -> $300k).
BDC_PREMIUM_DESDE = 2018
BDC_PREMIUM_MODELOS = {"X5", "X6", "630", "640", "840", "M8", "M850", "I8"}
BDC_PREMIUM_EXTRA = 60000


def _extra_gama(fam: str, modelo: str | None, anio: int) -> int:
    if fam == "BDC" and (anio >= BDC_PREMIUM_DESDE or (modelo or "") in BDC_PREMIUM_MODELOS):
        return BDC_PREMIUM_EXTRA
    return 0


@dataclass
class Cotizacion:
    estado: str                 # cotizar | cotizar_desde | escalar | falta_dato
    precio: int | None = None
    precio_max: int | None = None   # solo cuando hay rango (ej. AKL con posible ECU bajo admisión)
    modelo: str = ""
    anio: int | None = None
    lugar: str = ""
    zona: str = ""
    servicio: str = "duplicado"
    falta: list[str] = field(default_factory=list)
    motivo: str = ""
    nota_cliente: str = ""      # texto que el bot puede usar tal cual
    aviso: str = ""             # instrucción obligatoria para el bot (ej. llave con pantalla no compatible)
    imagen_sugerida: str = ""   # lámina del catálogo que corresponde a este auto
    interno: dict = field(default_factory=dict)  # sistema, familia, confianza (no se muestra al cliente)

    def dict(self):
        return asdict(self)


def _fila(modelo: str | None, serie: str | None, anio: int) -> tuple[dict | None, str]:
    if modelo:
        return DECO.get(f"{modelo}_{anio}"), modelo
    if serie:
        filas = POR_SERIE.get((serie, anio), [])
        familias = {(f["FAMILIA_PRECIO"], f["ACCION"]) for f in filas}
        if len(familias) == 1:
            return filas[0], f"Serie {serie}"
    return None, modelo or (f"Serie {serie}" if serie else "")


def pantalla_compatible(familia: str, sistema: str) -> str:
    """'si' | 'no' | 'verificar' (año de transición: puede ser Serie F o G)."""
    if familia not in ("BDC23", "BCP"):
        return "no"
    s = sistema.upper()
    if any(x in s for x in ("FEM", "CAS", "EWS", "O BDC ", "CONECTAR")) or s.endswith("O BDC"):
        return "verificar"
    return "si"


def cotizar(modelo_texto: str = "", anio: int | None = None, lugar_texto: str = "",
            situacion: str = "duplicado", tipo_llave: str = "", cliente_b2b: bool = False) -> Cotizacion:
    """situacion: 'duplicado' (tiene una llave que funciona) | 'akl' (perdió todas) | 'no_sabe'.
    tipo_llave: '' | 'pantalla' (llave con display) | 'razor' | 'bcp'."""
    c = Cotizacion("falta_dato", servicio=situacion)
    modelo = normalizar_modelo(modelo_texto)
    serie = None if modelo else serie_desde_texto(modelo_texto or "")
    if not modelo and not serie:
        c.falta.append("modelo")
    if not anio:
        c.falta.append("anio")
    lugar = resolver_lugar(lugar_texto) if lugar_texto else Lugar("desconocido")
    if lugar.tipo == "ambiguo":
        c.falta.append("lugar_ambiguo")
        c.interno["opciones_lugar"] = lugar.opciones
    elif lugar.tipo == "desconocido":
        c.falta.append("lugar")
    if situacion == "no_sabe":
        c.falta.append("situacion")
    if cliente_b2b:
        c.estado, c.motivo = "escalar", "Cliente B2B (taller, cerrajero, automotora o flota): precio socio lo da Jesús."
        return c
    if "modelo" in c.falta or "anio" in c.falta:
        return c

    fila, nombre = _fila(modelo, serie, int(anio))
    c.modelo, c.anio = nombre, int(anio)
    if not fila:
        if serie and not modelo:
            c.falta.append("modelo_exacto")
            c.motivo = "En esa serie y año hay más de un sistema; falta el modelo exacto (ej. 320i, 330e)."
            return c
        c.estado, c.motivo = "escalar", f"Modelo/año fuera del decodificador: {modelo_texto} {anio}."
        return c

    fam, accion, sistema = fila["FAMILIA_PRECIO"], fila["ACCION"], fila["SISTEMA"]
    c.imagen_sugerida = ("selector_mini" if str(nombre).startswith("MINI") else
                         {"BDC23": "selector_serie_g", "BCP": "selector_serie_g", "BDC": "selector_llave_5",
                          "CAS4_FEM": "selector_llave_4"}.get(fam, "catalogo_general"))
    c.interno = {"sistema": sistema, "familia": fam, "confianza": fila["CONFIANZA"],
                 "chasis": fila["CHASIS"], "accion_tabla": accion}

    if c.falta:            # tenemos el auto pero no el lugar o la situación
        return c
    c.lugar, c.zona = lugar.nombre, lugar.zona

    if accion == "ESCALAR":
        c.estado, c.motivo = "escalar", f"El decodificador marca {nombre} {anio} ({sistema}) para revisión de Jesús."
        return c

    # --- AKL (pérdida total): se calcula sobre el duplicado, pero NUNCA se menciona el duplicado al cliente
    if situacion == "akl":
        if any(s in sistema for s in AKL_NO_FACTIBLE) and not any(s in sistema for s in ("FEM", "CAS", "EWS", "BDC2")):
            c.estado, c.motivo = "escalar", f"AKL en {sistema}: Autoprog no lo hace hoy. Jesús decide qué ofrecer."
            c.interno["akl_no_factible"] = True
            return c
        if "BDC3" in sistema or "BCP" in sistema:
            c.interno["akl_puede_ser_no_factible"] = True
        extra = _extra_gama(fam, modelo, int(anio))
        dup = ((int(fila["PRECIO_RM_Z1"]) + lugar.recargo) if lugar.tipo == "rm" else int(lugar.bloque[COL_REGION[fam]])) + extra
        c.precio = dup * AKL_FACTOR
        rango = ECU_BAJO_ADMISION.get(modelo or "")
        if rango and rango[0] <= int(anio) <= rango[1]:
            c.precio_max = dup * AKL_FACTOR_ECU_ADMISION
            c.estado = "cotizar_desde"
            c.nota_cliente = "Incluye llave nueva y programación desde cero."
        else:
            c.estado = "cotizar" if accion == "COTIZAR" and not c.interno.get("akl_puede_ser_no_factible") else "cotizar_desde"
            c.nota_cliente = "Incluye llave nueva y programación desde cero."
        if lugar.tipo == "region":
            c.nota_cliente += (" Estamos armando ruta a la zona; se reserva el cupo y se confirma la fecha exacta "
                               "al cerrar la programación. Sin costo adicional por traslado.")
        return c

    # --- llave con pantalla: solo Serie G / U (BDC2, BDC3, BCP). NO en FEM, CAS, EWS ni BDC de Serie F
    #     (vault: Procedimientos/LCD Display Key compatibilidad BMW).
    if tipo_llave == "pantalla":
        compat = pantalla_compatible(fam, sistema)
        if compat == "no":
            base = cotizar(modelo_texto, anio, lugar_texto, situacion, "", cliente_b2b)
            base.interno["pantalla_no_compatible"] = True
            base.aviso = ("La llave con pantalla NO es compatible con este auto (es solo para modelos nuevos). "
                          "Díselo claro en una frase y ofrécele su llave original con este precio.")
            return base
        if compat == "verificar":
            c.estado = "falta_dato"
            c.falta.append("foto_llave")
            c.motivo = ("Según el año, este modelo puede o no aceptar llave con pantalla. Pide una foto de la llave "
                        "que usa hoy, o que te diga cuál es en la lámina.")
            return c
        variante = "sin_p" if int(anio) <= 2019 else "con_p"
        if lugar.tipo == "rm":
            c.precio = LCD_RM[variante] + lugar.recargo
        else:
            c.precio = LCD_REGION[variante].get(lugar.zona)
        c.estado = "cotizar_desde"
        c.interno["lcd"] = variante
        c.nota_cliente = "Llave con pantalla original."
        return c

    # --- llave #7 (BCP): solo Serie G / U. Antes se cotizaba como razor (BDC3) y salía mucho más barata.
    if tipo_llave == "bcp":
        compat = pantalla_compatible(fam, sistema)
        if compat == "no":
            base = cotizar(modelo_texto, anio, lugar_texto, situacion, "", cliente_b2b)
            base.interno["bcp_no_compatible"] = True
            base.aviso = ("La llave #7 NO es compatible con este auto (es solo para modelos nuevos). "
                          "Díselo claro en una frase y ofrécele su llave original con este precio.")
            return base
        if compat == "verificar":
            c.estado = "falta_dato"
            c.falta.append("foto_llave")
            c.motivo = "Según el año puede o no aceptar la llave #7. Pide una foto de la llave que usa hoy."
            return c
        c.precio = BCP_RM if lugar.tipo == "rm" else int(lugar.bloque["P_BCP"])
        c.estado = "cotizar"
        c.interno["llave"] = "bcp"
        c.nota_cliente = "Llave #7 original, la de nueva generación. La programación toma unas 2 horas."
        return c

    # --- duplicado normal
    extra = _extra_gama(fam, modelo, int(anio))
    if extra:
        c.interno["extra_gama"] = extra
    if lugar.tipo == "rm":
        c.precio = int(fila["PRECIO_RM_Z1"]) + lugar.recargo + extra
    else:
        c.precio = int(lugar.bloque[COL_REGION[fam]]) + extra
        c.interno["region"] = {"bloque": lugar.zona, "costo_viaje": lugar.bloque["COSTO_VIAJE"],
                               "umbral_viaje": lugar.bloque["UMBRAL_PARA_VIAJAR"]}
        c.nota_cliente = ("Estamos armando ruta a la zona; se reserva el cupo y se confirma la fecha "
                          "exacta al cerrar la programación. Sin costo adicional por traslado.")
    c.estado = "cotizar" if accion == "COTIZAR" else "cotizar_desde"
    return c


if __name__ == "__main__":
    import sys
    print(json.dumps(cotizar(*sys.argv[1:4]).dict(), ensure_ascii=False, indent=1))
