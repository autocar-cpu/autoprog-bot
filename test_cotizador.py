from cotizador import cotizar, normalizar_modelo, resolver_lugar


def test_modelos():
    assert normalizar_modelo("320d xDrive") == "320"
    assert normalizar_modelo("BMW X5 xDrive40i") == "X5"
    assert normalizar_modelo("Mini Cooper S") == "MINI COOPER S"
    assert normalizar_modelo("cooper countryman") == "MINI COUNTRYMAN"
    assert normalizar_modelo("M340i") == "M340"
    assert normalizar_modelo("bmw 118i") == "118"
    assert normalizar_modelo("iX3") == "IX3"
    assert normalizar_modelo("Serie 3") is None


def test_lugares():
    assert resolver_lugar("Ñuñoa").zona == "Zona 1"
    assert resolver_lugar("nunoa").zona == "Zona 1"
    assert resolver_lugar("las condes").recargo == 20000
    assert resolver_lugar("Chicureo").nombre == "Colina"
    assert resolver_lugar("Viña").zona == "B2"
    assert resolver_lugar("Peñalolen").zona == "Zona 2"
    assert resolver_lugar("Concepcion").zona == "B5"
    assert resolver_lugar("San Pedro").tipo == "ambiguo"
    assert resolver_lugar("Narnia").tipo == "desconocido"


def test_caso_vina_bdc():
    # el caso que el bot de GHL cotizó mal: BDC en Viña debe ser 240.000
    c = cotizar("X1", 2014, "Viña del Mar")
    if c.interno.get("familia") == "BDC":
        assert c.precio == 240000


def test_rm_con_recargo():
    c = cotizar("320i", 2014, "Las Condes")
    assert c.estado in ("cotizar", "cotizar_desde")
    assert c.precio == int(c.precio)
    base = {"CAS": 100000, "CAS4_FEM": 150000, "EWS": 150000, "BDC": 160000, "BDC23": 280000, "BCP": 429000}
    assert c.precio == base[c.interno["familia"]] + 20000


def test_falta_datos():
    c = cotizar("320i", None, "")
    assert c.estado == "falta_dato" and "anio" in c.falta and "lugar" in c.falta


def test_akl_doble():
    dup = cotizar("X3", 2020, "Las Condes")
    akl = cotizar("X3", 2020, "Las Condes", situacion="akl")
    assert akl.precio == 2 * dup.precio and akl.precio_max is None


def test_akl_ecu_bajo_admision_rango():
    dup = cotizar("320i", 2014, "Providencia")
    akl = cotizar("320i", 2014, "Providencia", situacion="akl")
    assert (akl.precio, akl.precio_max) == (2 * dup.precio, 3 * dup.precio)
    assert akl.estado == "cotizar_desde"
    assert "duplicado" not in akl.nota_cliente.lower()


def test_akl_bdc3_no_factible():
    c = cotizar("X5", 2021, "Providencia", situacion="akl")
    assert c.estado == "escalar"


def test_b2b():
    assert cotizar("320i", 2014, "Providencia", cliente_b2b=True).estado == "escalar"


def test_pantalla():
    c = cotizar("X5", 2021, "Vitacura", tipo_llave="pantalla")
    assert c.precio == 590000 + 20000


def test_bdc_reciente_mas_caro():
    assert cotizar("X6", 2019, "Las Condes").precio == 160000 + 20000 + 60000
    assert cotizar("X1", 2020, "Providencia").precio == 220000
    assert cotizar("X1", 2016, "Providencia").precio == 160000
    assert cotizar("320i", 2015, "Providencia").precio == 150000


def test_pantalla_no_compatible_en_auto_antiguo():
    """Caso Jd 11-10-2026: 120 del 2005 (CAS) llegó por el anuncio de llave con pantalla; se le cotizó $549.000."""
    c = cotizar("120", 2005, "San Bernardo", tipo_llave="pantalla")
    assert c.interno.get("pantalla_no_compatible")
    assert "NO es compatible" in c.aviso
    assert c.precio and c.precio < 400000          # precio de su llave normal, no de la pantalla
    assert c.precio == cotizar("120", 2005, "San Bernardo").precio


def test_pantalla_no_en_serie_f():
    for m, a in (("118i", 2014), ("X5", 2016), ("520d", 2015)):
        c = cotizar(m, a, "Providencia", tipo_llave="pantalla")
        assert c.interno.get("pantalla_no_compatible") or c.estado == "falta_dato", (m, a, c.estado)


def test_pantalla_si_en_serie_g():
    c = cotizar("X5", 2021, "Providencia", tipo_llave="pantalla")
    assert c.estado == "cotizar_desde" and c.precio >= 529000 and not c.aviso


def test_pantalla_ano_de_transicion_pide_foto():
    c = cotizar("320i", 2019, "Providencia", tipo_llave="pantalla")
    assert c.estado == "falta_dato" and "foto_llave" in c.falta


def test_bcp_llave_7():
    c = cotizar("X6", 2026, "Las Condes", tipo_llave="bcp")
    assert c.precio == 490000 and c.interno["llave"] == "bcp"
    assert cotizar("X6", 2026, "Puente Alto", tipo_llave="bcp").precio == 490000
    assert cotizar("X6", 2026, "Concepción", tipo_llave="bcp").precio == 580000
    assert cotizar("X6", 2026, "Rancagua", tipo_llave="bcp").precio == 550000
    assert cotizar("X6", 2026, "Las Condes").precio == 300000   # razor sigue igual
    assert cotizar("X6", 2015, "Providencia", tipo_llave="bcp").interno.get("bcp_no_compatible")
