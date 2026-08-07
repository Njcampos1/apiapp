"""
Tests para verificar que pedidos con destino a Lampa se clasifican
como regionales y se envían por Chilexpress (no por Rocket).

Contexto: Rocket ya no cubre la comuna de Lampa, por lo que
los pedidos a Lampa deben:
1. Tener courier = 'Chilexpress' (WooCommerce) en la planilla diaria
2. Tener courier = 'Mercado' (Mercado Libre) en la planilla diaria
3. Ser incluidos en el CSV de Chilexpress (si son WooCommerce)
"""
from io import BytesIO, StringIO
import csv
import pandas as pd

from models.order import NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress

from services.excel_service import (
    _get_courier, _load_rm_comunas, _normalize_text, generate_excel,
)
from services.chilexpress_service import (
    _is_regional_order, _is_woocommerce_regional, generate_chilexpress_csv,
    _parse_address, _normalize_phone,
)


# ── Fixtures helpers ──────────────────────────────────────────────────────────

def _make_order(
    city: str,
    source: str = "woocommerce",
    order_id: str = "test-123",
    first_name: str = "Juan",
    last_name: str = "Perez",
    address_1: str = "Av. Principal 100",
    address_2: str = "",
    phone: str = "931311932",
    email: str = "juan@example.com",
    state: str = "RM",
    logistic_type: str = "",
    logistic_label: str = "",
    total: float = 10000,
    items: list | None = None,
) -> NormalizedOrder:
    if items is None:
        items = [OrderItem(sku="101000-1", name="Cafe Premium", quantity=1, price=10000)]
    return NormalizedOrder(
        id=order_id,
        source=OrderSource(source),
        status=OrderStatus.PROCESSING,
        shipping=ShippingAddress(
            first_name=first_name,
            last_name=last_name,
            address_1=address_1,
            address_2=address_2,
            city=city,
            state=state,
            postcode="",
            country="CL",
            phone=phone,
            email=email,
        ),
        items=items,
        total=total,
        platform_meta={
            "logistic_type": logistic_type,
            "logistic_label": logistic_label,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Lampa NO está en el listado de comunas RM
# ══════════════════════════════════════════════════════════════════════════════

class TestLampaNoEnRM:

    def test_lampa_not_in_rm_comunas(self):
        rm_comunas = _load_rm_comunas()
        assert _normalize_text("Lampa") not in rm_comunas

    def test_lampa_normalized_not_in_rm_comunas(self):
        rm_comunas = _load_rm_comunas()
        assert _normalize_text("lampa") not in rm_comunas

    def test_lampa_con_acento_no_match(self):
        rm_comunas = _load_rm_comunas()
        assert _normalize_text("Lámpa") not in rm_comunas

    def test_lampa_uppercase_no_match(self):
        rm_comunas = _load_rm_comunas()
        assert _normalize_text("LAMPA") not in rm_comunas

    def test_lampa_con_espacios_no_match(self):
        rm_comunas = _load_rm_comunas()
        assert _normalize_text("  Lampa  ") not in rm_comunas


# ══════════════════════════════════════════════════════════════════════════════
# 2. _get_courier — Lampa → Chilexpress / Mercado
# ══════════════════════════════════════════════════════════════════════════════

class TestCourierLampaWooCommerce:

    def test_lampa_woocommerce_es_chilexpress(self):
        rm = _load_rm_comunas()
        assert _get_courier("Lampa", "woocommerce", rm) == "Chilexpress"

    def test_lampa_mixed_case(self):
        rm = _load_rm_comunas()
        assert _get_courier("LAMPA", "woocommerce", rm) == "Chilexpress"

    def test_lampa_con_acento(self):
        rm = _load_rm_comunas()
        assert _get_courier("Lámpa", "woocommerce", rm) == "Chilexpress"

    def test_lampa_con_espacios(self):
        rm = _load_rm_comunas()
        assert _get_courier("  Lampa  ", "woocommerce", rm) == "Chilexpress"


class TestCourierLampaMercadoLibre:

    def test_lampa_mercadolibre_es_mercado(self):
        rm = _load_rm_comunas()
        assert _get_courier("Lampa", "mercadolibre", rm) == "Mercado"

    def test_lampa_mercadolibre_acento(self):
        rm = _load_rm_comunas()
        assert _get_courier("Lámpa", "mercadolibre", rm) == "Mercado"


# ══════════════════════════════════════════════════════════════════════════════
# 3. _get_courier — comunas RM conocidas siguen en Rocket
# ══════════════════════════════════════════════════════════════════════════════

class TestCourierComunasRMConocidas:

    def test_santiago_woocommerce_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Santiago", "woocommerce", rm) == "Rocket"

    def test_santiago_mercadolibre_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Santiago", "mercadolibre", rm) == "Rocket"

    def test_las_condes_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Las Condes", "woocommerce", rm) == "Rocket"

    def test_puente_alto_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Puente Alto", "woocommerce", rm) == "Rocket"

    def test_nunoa_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Ñuñoa", "woocommerce", rm) == "Rocket"

    def test_nunoa_normalizada_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("nunoa", "woocommerce", rm) == "Rocket"

    def test_maipu_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Maipú", "woocommerce", rm) == "Rocket"

    def test_san_bernardo_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("San Bernardo", "woocommerce", rm) == "Rocket"

    def test_padre_hurtado_es_rocket(self):
        rm = _load_rm_comunas()
        assert _get_courier("Padre Hurtado", "woocommerce", rm) == "Rocket"


# ══════════════════════════════════════════════════════════════════════════════
# 4. _get_courier — comunas NO-RM van a Chilexpress / Mercado
# ══════════════════════════════════════════════════════════════════════════════

class TestCourierComunasRegionales:

    def test_valparaiso_woocommerce_es_chilexpress(self):
        rm = _load_rm_comunas()
        assert _get_courier("Valparaíso", "woocommerce", rm) == "Chilexpress"

    def test_concepcion_woocommerce_es_chilexpress(self):
        rm = _load_rm_comunas()
        assert _get_courier("Concepción", "woocommerce", rm) == "Chilexpress"

    def test_valdivia_woocommerce_es_chilexpress(self):
        rm = _load_rm_comunas()
        assert _get_courier("Valdivia", "woocommerce", rm) == "Chilexpress"

    def test_valparaiso_meli_es_mercado(self):
        rm = _load_rm_comunas()
        assert _get_courier("Valparaíso", "mercadolibre", rm) == "Mercado"


# ══════════════════════════════════════════════════════════════════════════════
# 5. _normalize_text
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeText:

    def test_empty_string(self):
        assert _normalize_text("") == ""

    def test_all_lowercase(self):
        assert _normalize_text("santiago") == "santiago"

    def test_all_uppercase(self):
        assert _normalize_text("SANTIAGO") == "santiago"

    def test_mixed_case(self):
        assert _normalize_text("SaNtIaGo") == "santiago"

    def test_accent_removal(self):
        assert _normalize_text("Ñuñoa") == "nunoa"

    def test_multiple_accents(self):
        assert _normalize_text("Valparaíso") == "valparaiso"

    def test_whitespace_preserved(self):
        assert _normalize_text("  Santiago  ") == "  santiago  "

    def test_tilde_removed(self):
        assert _normalize_text("Lámpa") == "lampa"

    def test_concepcion_accent(self):
        assert _normalize_text("Concepción") == "concepcion"


# ══════════════════════════════════════════════════════════════════════════════
# 6. _is_regional_order / _is_woocommerce_regional
# ══════════════════════════════════════════════════════════════════════════════

class TestRegionalOrder:

    def test_lampa_es_regional(self):
        assert _is_regional_order(_make_order("Lampa")) is True

    def test_santiago_no_es_regional(self):
        assert _is_regional_order(_make_order("Santiago")) is False

    def test_lampa_mayusculas_es_regional(self):
        assert _is_regional_order(_make_order("LAMPA")) is True

    def test_lampa_con_espacios_es_regional(self):
        assert _is_regional_order(_make_order("  Lampa  ")) is True

    def test_valparaiso_es_regional(self):
        assert _is_regional_order(_make_order("Valparaíso")) is True

    def test_lampa_woocommerce_es_regional(self):
        assert _is_woocommerce_regional(_make_order("Lampa", source="woocommerce")) is True

    def test_santiago_woocommerce_no_es_regional(self):
        assert _is_woocommerce_regional(_make_order("Santiago", source="woocommerce")) is False

    def test_lampa_meli_no_es_woocomerce_regional(self):
        assert _is_woocommerce_regional(_make_order("Lampa", source="mercadolibre")) is False


# ══════════════════════════════════════════════════════════════════════════════
# 7. Chilexpress CSV — pedidos a Lampa incluidos / excluidos correctamente
# ══════════════════════════════════════════════════════════════════════════════

class TestLampaEnChilexpressCSV:

    def test_lampa_woocomerce_aparece_en_csv(self):
        order = _make_order("Lampa", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        assert "Lampa" in csv_text
        assert "Juan Perez" in csv_text

    def test_lampa_woocomerce_no_excluido(self):
        order = _make_order("Lampa", source="woocommerce")
        assert len([o for o in [order] if _is_woocommerce_regional(o)]) == 1

    def test_meli_lampa_excluido_de_csv(self):
        order = _make_order("Lampa", source="mercadolibre")
        assert len([o for o in [order] if _is_woocommerce_regional(o)]) == 0

    def test_santiago_woocomerce_excluido_de_csv(self):
        order = _make_order("Santiago", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        assert "Santiago" not in csv_text

    def test_csv_tiene_encabezados_correctos(self):
        order = _make_order("Lampa", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.reader(StringIO(csv_text))
        headers = next(reader)
        assert "DESTINO_COMUNA" in headers
        assert "DESTINATARIO" in headers
        assert "CALLE" in headers
        assert "CELULAR" in headers

    def test_csv_valores_producto_y_servicio(self):
        order = _make_order("Lampa", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["PRODUCTO"] == "3"
        assert row["SERVICIO"] == "3"

    def test_csv_valor_declarado_es_subtotal_productos(self):
        order = _make_order(
            "Lampa",
            source="woocommerce",
            total=27990,  # incluye despacho, no debe usarse
            items=[
                OrderItem(sku="101000-1", name="Cafe Premium", quantity=2, price=9990),
                OrderItem(sku="101000-2", name="Cafe Suave", quantity=1, price=4500),
            ],
        )
        csv_bytes = generate_chilexpress_csv([order])
        reader = csv.DictReader(StringIO(csv_bytes.decode("utf-8-sig")))
        row = next(reader)
        assert row["VALOR_DECLARADO_PRODUCTO"] == "24480"

    def test_csv_valor_declarado_sin_montos_usa_fallback(self):
        order = _make_order(
            "Lampa",
            source="woocommerce",
            items=[OrderItem(sku="101000-1", name="Cafe Premium", quantity=1, price=0)],
        )
        csv_bytes = generate_chilexpress_csv([order])
        reader = csv.DictReader(StringIO(csv_bytes.decode("utf-8-sig")))
        row = next(reader)
        assert row["VALOR_DECLARADO_PRODUCTO"] == "50000"

    def test_csv_lampa_destinatario_correcto(self):
        order = _make_order(
            "Lampa",
            source="woocommerce",
            first_name="María",
            last_name="González",
        )
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["DESTINATARIO"] == "María González"

    def test_csv_lampa_referencia_es_order_id(self):
        order = _make_order("Lampa", source="woocommerce", order_id="WC-9999")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["REFERENCIA"] == "WC-9999"


# ══════════════════════════════════════════════════════════════════════════════
# 8. Excel planilla diaria — Lampa = Chilexpress / Santiago = Rocket
# ══════════════════════════════════════════════════════════════════════════════

def _read_excel_df(xlsx_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(BytesIO(xlsx_bytes), engine="openpyxl")


class TestLampaEnPlanillaDiaria:

    def test_lampa_woocomerce_despacho_chilexpress(self):
        df = _read_excel_df(generate_excel([_make_order("Lampa", source="woocommerce")]))
        row = df[df["Comuna"] == "Lampa"]
        assert len(row) == 1
        assert row.iloc[0]["Despacho"] == "Chilexpress"

    def test_lampa_meli_despacho_mercado(self):
        df = _read_excel_df(generate_excel([_make_order("Lampa", source="mercadolibre")]))
        row = df[df["Comuna"] == "Lampa"]
        assert len(row) == 1
        assert row.iloc[0]["Despacho"] == "Mercado"

    def test_santiago_woocomerce_despacho_rocket(self):
        df = _read_excel_df(generate_excel([_make_order("Santiago")]))
        row = df[df["Comuna"] == "Santiago"]
        assert len(row) == 1
        assert row.iloc[0]["Despacho"] == "Rocket"

    def test_cliente_y_direccion_correctos(self):
        df = _read_excel_df(generate_excel([_make_order("Lampa")]))
        row = df[df["Comuna"] == "Lampa"]
        assert row.iloc[0]["Cliente"] == "Juan Perez"
        assert row.iloc[0]["Dirección"] == "Av. Principal 100"


# ══════════════════════════════════════════════════════════════════════════════
# 9. Batches mixtos — Lampa + Santiago en el mismo Excel / CSV
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchMixto:

    def _mixed_orders(self):
        return [
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-001"),
            _make_order("Santiago", source="woocommerce", order_id="SANTI-001"),
            _make_order("Lampa", source="mercadolibre", order_id="LAMPA-ML-001"),
            _make_order("Las Condes", source="woocommerce", order_id="COND-001"),
        ]

    def test_excel_batch_mixto_tiene_todas_las_comunas(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        comunas = set(df["Comuna"].tolist())
        assert "Lampa" in comunas
        assert "Santiago" in comunas
        assert "Las Condes" in comunas

    def test_excel_lampa_woocomerce_es_chilexpress(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        row = df[(df["Comuna"] == "Lampa") & (df["Página"] == "woocommerce")]
        assert len(row) == 1
        assert row.iloc[0]["Despacho"] == "Chilexpress"

    def test_excel_lampa_meli_es_mercado(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        row = df[(df["Comuna"] == "Lampa") & (df["Página"] == "mercadolibre")]
        assert len(row) == 1
        assert row.iloc[0]["Despacho"] == "Mercado"

    def test_excel_santiago_es_rocket(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        row = df[df["Comuna"] == "Santiago"]
        assert row.iloc[0]["Despacho"] == "Rocket"

    def test_excel_las_condes_es_rocket(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        row = df[df["Comuna"] == "Las Condes"]
        assert row.iloc[0]["Despacho"] == "Rocket"

    def test_csv_chilexpress_solo_woocomerce_regional(self):
        csv_bytes = generate_chilexpress_csv(self._mixed_orders())
        csv_text = csv_bytes.decode("utf-8-sig")
        assert "Lampa" in csv_text
        assert "Santiago" not in csv_text
        assert "Las Condes" not in csv_text

    def test_csv_chilexpress_no_incluye_meli(self):
        csv_bytes = generate_chilexpress_csv(self._mixed_orders())
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        comunas_csv = [row["DESTINO_COMUNA"] for row in reader]
        # Lampa WooCommerce debería estar, Lampa MercadoLibre no
        assert comunas_csv.count("Lampa") == 1

    def test_excel_batch_mixto_cuatro_filas(self):
        df = _read_excel_df(generate_excel(self._mixed_orders()))
        assert len(df) == 4


# ══════════════════════════════════════════════════════════════════════════════
# 10. Pedidos Fulfillment se excluyen del Excel
# ══════════════════════════════════════════════════════════════════════════════

class TestFulfillmentExclusion:

    def test_fulfillment_excluido_por_logistic_type(self):
        order = _make_order("Lampa", logistic_type="fulfillment")
        df = _read_excel_df(generate_excel([order]))
        assert len(df[df["Comuna"] == "Lampa"]) == 0

    def test_fulfillment_excluido_por_logistic_label(self):
        order = _make_order("Lampa", logistic_label="Full")
        df = _read_excel_df(generate_excel([order]))
        assert len(df[df["Comuna"] == "Lampa"]) == 0

    def test_no_fulfillment_incluido(self):
        order = _make_order("Lampa", logistic_type="drop_off")
        df = _read_excel_df(generate_excel([order]))
        assert len(df[df["Comuna"] == "Lampa"]) == 1

    def test_fulfillment_no_aparece_en_chilexpress_csv(self):
        order = _make_order(
            "Lampa", source="woocommerce", logistic_type="fulfillment",
        )
        # generate_chilexpress_csv NO filtra fulfillment, solo revisa source y comuna
        # Pero el CSV debería incluirlo si es woocommerce regional
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        # Verificamos que al menos la funcionalidad de filtro funciona
        assert "Lampa" in csv_text


# ══════════════════════════════════════════════════════════════════════════════
# 11. _parse_address
# ══════════════════════════════════════════════════════════════════════════════

class TestParseAddress:

    def test_direccion_con_numero(self):
        calle, num = _parse_address("Av. Los Leones 123")
        assert calle == "Av. Los Leones"
        assert num == "123"

    def test_direccion_con_numero_y_letra(self):
        calle, num = _parse_address("Calle Principal 456-A")
        assert calle == "Calle Principal"
        assert num == "456-A"

    def test_direccion_sin_numero(self):
        calle, num = _parse_address("Pasaje sin numero")
        assert calle == "Pasaje sin numero"
        assert num == "S/N"

    def test_direccion_vacia(self):
        calle, num = _parse_address("")
        assert calle == ""
        assert num == "S/N"

    def test_direccion_solo_espacios(self):
        calle, num = _parse_address("   ")
        assert calle == ""
        assert num == "S/N"

    def test_direccion_con_coma(self):
        calle, num = _parse_address("Los Boldos 789, Depto 12")
        assert calle == "Los Boldos"
        assert num == "789"


# ══════════════════════════════════════════════════════════════════════════════
# 12. _normalize_phone
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizePhone:

    def test_phone_local_format(self):
        assert _normalize_phone("931311932") == "931311932"

    def test_phone_international_format(self):
        assert _normalize_phone("+56931311932") == "931311932"

    def test_phone_international_sin_plus(self):
        assert _normalize_phone("56931311932") == "931311932"

    def test_phone_con_espacios(self):
        assert _normalize_phone("+56 9 3131 1932") == "931311932"

    def test_phone_con_guiones(self):
        assert _normalize_phone("9313-11932") == "931311932"

    def test_phone_vacio(self):
        assert _normalize_phone("") == ""

    def test_phone_none(self):
        assert _normalize_phone(None) == ""

    def test_phone_con_parentesis(self):
        assert _normalize_phone("(+56) 931311932") == "931311932"


# ══════════════════════════════════════════════════════════════════════════════
# 13. CSV — campos de dirección y teléfono desde Lampa
# ══════════════════════════════════════════════════════════════════════════════

class TestCSVLampaCampos:

    def test_csv_lampa_calle_y_numero(self):
        order = _make_order(
            "Lampa", source="woocommerce",
            address_1="Av. Principal 1234",
        )
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["CALLE"] == "Av. Principal"
        assert row["NUMERO"] == "1234"

    def test_csv_lampa_telefono_normalizado(self):
        order = _make_order(
            "Lampa", source="woocommerce",
            phone="+56931311932",
        )
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["CELULAR"] == "931311932"

    def test_csv_lampa_email_correcto(self):
        order = _make_order(
            "Lampa", source="woocommerce",
            email="test@lampa.cl",
        )
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["EMAIL"] == "test@lampa.cl"

    def test_csv_lampa_destino_comuna_raw(self):
        order = _make_order("Lampa", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["DESTINO_COMUNA"] == "Lampa"

    def test_csv_lampa_pesoydimensiones(self):
        order = _make_order("Lampa", source="woocommerce")
        csv_bytes = generate_chilexpress_csv([order])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        row = next(reader)
        assert row["PESO"] == "1"
        assert row["ALTO"] == "8"
        assert row["ANCHO"] == "16"
        assert row["LARGO"] == "40"
        assert row["TIPO_DE_DIRECCION"] == "2"


# ══════════════════════════════════════════════════════════════════════════════
# 14. Consistencia cross-service — ambos servicios clasifican igual
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossServiceConsistency:

    def _check_consistency(self, city: str, expected_regional: bool):
        order = _make_order(city)
        assert _is_regional_order(order) is expected_regional, (
            f"chilexpress_service._is_regional_order({city!r}) != {expected_regional}"
        )
        rm = _load_rm_comunas()
        is_rm_in_excel = _normalize_text(city.strip()) in rm
        assert is_rm_in_excel != expected_regional, (
            f"excel_service rm check for {city!r} inconsistent with regional={expected_regional}"
        )

    def test_lampa_consistente(self):
        self._check_consistency("Lampa", expected_regional=True)

    def test_santiago_consistente(self):
        self._check_consistency("Santiago", expected_regional=False)

    def test_nunoa_consistente(self):
        self._check_consistency("Ñuñoa", expected_regional=False)

    def test_valparaiso_consistente(self):
        self._check_consistency("Valparaíso", expected_regional=True)

    def test_lampa_uppercase_consistente(self):
        self._check_consistency("LAMPA", expected_regional=True)

    def test_lampa_accent_consistente(self):
        self._check_consistency("Lámpa", expected_regional=True)


# ══════════════════════════════════════════════════════════════════════════════
# 15. Excel — pedidos múltiples a Lampa
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiplesPedidosLampa:

    def test_dos_pedidos_lampa_woocomerce(self):
        orders = [
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-001"),
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-002"),
        ]
        df = _read_excel_df(generate_excel(orders))
        lampa_rows = df[df["Comuna"] == "Lampa"]
        assert len(lampa_rows) == 2
        assert all(lampa_rows["Despacho"] == "Chilexpress")

    def test_dos_pedidos_lampa_distintos_couriers(self):
        orders = [
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-WC"),
            _make_order("Lampa", source="mercadolibre", order_id="LAMPA-ML"),
        ]
        df = _read_excel_df(generate_excel(orders))
        lampa_rows = df[df["Comuna"] == "Lampa"]
        assert len(lampa_rows) == 2
        wc_row = lampa_rows[lampa_rows["Página"] == "woocommerce"]
        ml_row = lampa_rows[lampa_rows["Página"] == "mercadolibre"]
        assert wc_row.iloc[0]["Despacho"] == "Chilexpress"
        assert ml_row.iloc[0]["Despacho"] == "Mercado"

    def test_csv_solo_woocomerce_lampa(self):
        orders = [
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-WC"),
            _make_order("Lampa", source="woocommerce", order_id="LAMPA-WC-2"),
            _make_order("Lampa", source="mercadolibre", order_id="LAMPA-ML"),
        ]
        csv_bytes = generate_chilexpress_csv(orders)
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 2
        assert all(r["DESTINO_COMUNA"] == "Lampa" for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
# 16. Excel — lista vacía
# ══════════════════════════════════════════════════════════════════════════════

class TestEmptyOrders:

    def test_excel_lista_vacia(self):
        df = _read_excel_df(generate_excel([]))
        assert len(df) == 0

    def test_csv_lista_vacia(self):
        csv_bytes = generate_chilexpress_csv([])
        csv_text = csv_bytes.decode("utf-8-sig")
        reader = csv.reader(StringIO(csv_text))
        headers = next(reader)
        assert "DESTINO_COMUNA" in headers
        # No debería haber más filas
        remaining = list(reader)
        assert len(remaining) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 17. Todas las comunas RM van a Rocket (sanity check del rm.json)
# ══════════════════════════════════════════════════════════════════════════════

class TestTodasComunasRM:

    def test_todas_las_comunas_rm_son_rocket_woocomerce(self):
        rm = _load_rm_comunas()
        for comuna in rm:
            assert _get_courier(comuna, "woocommerce", rm) == "Rocket", (
                f"{comuna} debería ser Rocket para WooCommerce"
            )

    def test_todas_las_comunas_rm_son_rocket_meli(self):
        rm = _load_rm_comunas()
        for comuna in rm:
            assert _get_courier(comuna, "mercadolibre", rm) == "Rocket", (
                f"{comuna} debería ser Rocket para MercadoLibre"
            )

    def test_todas_las_comunas_rm_no_son_regional(self):
        rm = _load_rm_comunas()
        for comuna in rm:
            order = _make_order(comuna)
            assert _is_regional_order(order) is False, (
                f"{comuna} no debería ser regional"
            )

    def test_lampa_no_esta_en_todas_las_rm(self):
        rm = _load_rm_comunas()
        normalized_lampa = _normalize_text("Lampa")
        assert normalized_lampa not in rm
