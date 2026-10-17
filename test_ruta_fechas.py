import datetime as dt
import regiones

H = dt.date(2026, 10, 17)


def _f(lugar, hoy=H):
    g = regiones.clasificar(lugar)
    return regiones.fecha_ruta(g["ciudad"], g["bloque"], hoy)


def test_dias_por_comuna():
    for c in ("Curicó", "Talca", "Linares", "Parral", "San Carlos", "Chillán", "Molina", "Bulnes"):
        assert _f(c)["dia"] == "jueves 22 de octubre", c
    for c in ("Concepción", "San Pedro de la Paz", "Talcahuano", "Chiguayante", "Hualpén", "Penco", "Coronel"):
        r = _f(c)
        assert r["dia"] == "viernes 23 de octubre" and "sábado 24" in r["alternativa"], c
    for c in ("Tomé", "Lota"):
        r = _f(c)
        assert r["dia"] == "sábado 24 de octubre" and r["cupos_limitados"], c


def test_vence_y_dias_pasados():
    assert _f("Talca", dt.date(2026, 10, 25)) is None
    assert _f("Concepción", dt.date(2026, 10, 25)) is None
    assert _f("Talca", dt.date(2026, 10, 23)) is None          # el jueves ya pasó
    assert _f("Concepción", dt.date(2026, 10, 24))["dia"] == "sábado 24 de octubre"


def test_otras_rutas_sin_fecha():
    assert _f("Rancagua") is None and _f("Temuco") is None
