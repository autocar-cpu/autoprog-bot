from motos import cotizar_moto, es_moto


def test_cotiza_gs_keyless_rm():
    r = cotizar_moto("R 1250 GS Adventure", 2021, "Las Condes", "boton", "duplicado")
    assert r["estado"] == "cotizar" and r["precio"] == 240000
    assert cotizar_moto("GS 1200", 2016, "Maipú", "boton")["precio"] == 240000


def test_escala():
    r = cotizar_moto("R 1250 GS", 2021, "Las Condes", "llave")
    assert r["estado"] == "lista_espera" and r["etiqueta"] == "espera_moto_bmw" and "precio" not in r
    r = cotizar_moto("R 1250 GS", 2021, "Las Condes", "boton", "akl")
    assert r["estado"] == "lista_espera" and "precio" not in r
    assert cotizar_moto("F 850 GS", 2021, "Las Condes", "boton")["estado"] == "escalar"
    assert cotizar_moto("R 1200 GS", 2012, "Las Condes", "boton")["estado"] == "escalar"
    assert cotizar_moto("R 1250 GS", 2024, "Las Condes", "boton")["estado"] == "escalar"
    r = cotizar_moto("R 1250 GS", 2021, "Concepción", "boton")
    assert r["estado"] == "escalar" and "precio" not in r


def test_falta_dato_da_precio():
    r = cotizar_moto("", None, "", "")
    assert r["estado"] == "falta_dato" and "precio" not in r          # no sabe si es botón o chapa
    r = cotizar_moto("R 1250 GS", None, "", "boton")
    assert r["estado"] == "falta_dato" and r["precio"] == 240000


def test_detecta_moto():
    assert es_moto(["*Headline:* Duplicado de llave moto BMW GS"]) and es_moto(["tengo una R1250"])
    assert not es_moto(["BMW X3 2015 en Talca"])


def test_revisor_hora():
    from revisor import revisar
    assert any("hora" in p for p in revisar(["te gustaria la visita? a qué hora te acomoda"], set()))
    assert not revisar(["te gustaria la visita? me dices cuando te viene bien"], set())
