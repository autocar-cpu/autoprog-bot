"""Clasificación geográfica de clientes: comuna → zona RM o bloque regional, región, etiqueta de ruta.
Sirve para juntar clientes por zona y planificar viajes (umbral por bloque en data/decodificador.json)."""
from __future__ import annotations

from cotizador import BLOQUES, resolver_lugar

BLOQUE_LABEL = {
    "B1": "B1 - Norte Chico", "B2": "B2 - Valpo Costa Norte", "B3": "B3 - Valpo Litoral Sur",
    "B4": "B4 - Valpo Interior", "B5": "B5 - Centro Sur", "B6": "B6 - Sur", "B7": "B7 - Sur Lagos",
    "B8": "B8 - Atacama", "B9": "B9 - O'Higgins", "H1": "H1 - Hub Antofagasta", "H2": "H2 - Hub Iquique",
    "H3": "H3 - Hub Arica", "H4": "H4 - Hub Patagonia"}
TAG_RUTA = {
    "B1": "ruta-b1-norte-chico", "B2": "ruta-b2-valpo-costa", "B3": "ruta-b3-valpo-litoral",
    "B4": "ruta-b4-valpo-interior", "B5": "ruta-b5-centro-sur", "B6": "ruta-b6-sur", "B7": "ruta-b7-sur-lagos",
    "B8": "ruta-b8-atacama", "B9": "ruta-b9-ohiggins", "H1": "ruta-h1-antofagasta", "H2": "ruta-h2-iquique",
    "H3": "ruta-h3-arica", "H4": "ruta-h4-patagonia"}
ZONA_RM = {"Zona 1": "Z1-RM Centro", "Zona 2": "Z2-RM Intermedia", "Zona 3": "Z3-RM Periferia"}

_MAULE = {"Talca", "Curicó", "Linares", "Constitución", "Cauquenes", "Molina", "San Javier", "Parral"}
_NUBLE = {"Chillán", "San Carlos", "Bulnes"}
_LOS_RIOS = {"Valdivia", "La Unión", "Panguipulli", "Río Bueno"}
_REGION_BLOQUE = {"B1": "Coquimbo", "B2": "Valparaíso", "B3": "Valparaíso", "B4": "Valparaíso", "B6": "Araucanía",
                  "B8": "Atacama", "B9": "O'Higgins", "H1": "Antofagasta", "H2": "Tarapacá", "H3": "Arica y Parinacota"}


def _region(ciudad: str, bloque: str) -> str:
    if bloque == "B5":
        return "Maule" if ciudad in _MAULE else "Ñuble" if ciudad in _NUBLE else "Biobío"
    if bloque == "B7":
        return "Los Ríos" if ciudad in _LOS_RIOS else "Los Lagos"
    if bloque == "H4":
        return "Magallanes" if ciudad in {"Punta Arenas", "Puerto Natales", "Porvenir"} else "Aysén"
    return _REGION_BLOQUE.get(bloque, "")


def clasificar(lugar_texto: str) -> dict | None:
    """{'tipo','ciudad','bloque','campos':{clave_config: valor}, 'etiqueta', 'umbral', 'costo_viaje'} o None."""
    if not lugar_texto:
        return None
    l = resolver_lugar(lugar_texto)
    if not l or l.tipo not in ("rm", "region"):
        return None
    if l.tipo == "rm":
        return {"tipo": "rm", "ciudad": l.nombre, "bloque": "RM", "etiqueta": None,
                "campos": {"comuna": l.nombre, "region": "Metropolitana", "bloque": "RM",
                           "zona_operativa": ZONA_RM.get(l.zona, "RM-Otra")}}
    b = l.zona
    fila = l.bloque or {}
    return {"tipo": "region", "ciudad": l.nombre, "bloque": b, "etiqueta": TAG_RUTA.get(b),
            "umbral": fila.get("UMBRAL_PARA_VIAJAR"), "costo_viaje": fila.get("COSTO_VIAJE"),
            "campos": {"comuna": l.nombre, "region": _region(l.nombre, b), "bloque": BLOQUE_LABEL.get(b, ""),
                       "zona_operativa": "No aplica (cliente fuera de RM)"}}
