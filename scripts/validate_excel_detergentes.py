from __future__ import annotations

from io import BytesIO

import pandas as pd

from models.order import NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress
from services.excel_service import generate_excel


def build_order_demo() -> NormalizedOrder:
    return NormalizedOrder(
        id="demo-1",
        source=OrderSource.MERCADOLIBRE,
        status=OrderStatus.PROCESSING,
        shipping=ShippingAddress(
            first_name="Ada",
            last_name="Lovelace",
            address_1="Calle Falsa 123",
            city="Santiago",
        ),
        items=[
            OrderItem(sku="203193", name="Detergente 35", quantity=2, price=1000),
            OrderItem(sku="203195", name="Detergente 70", quantity=1, price=2000),
            OrderItem(sku="203196", name="Detergente 105", quantity=1, price=3000),
            OrderItem(sku="203192", name="Detergente 60", quantity=3, price=4000),
            OrderItem(sku="203194", name="Detergente 120", quantity=1, price=5000),
            OrderItem(sku="203198", name="Detergente 180", quantity=2, price=6000),
            OrderItem(sku="203197", name="Detergente 95", quantity=5, price=7000),
        ],
        total=0,
    )


def build_order_fallback() -> NormalizedOrder:
    return NormalizedOrder(
        id="demo-2",
        source=OrderSource.WOOCOMMERCE,
        status=OrderStatus.PROCESSING,
        shipping=ShippingAddress(
            first_name="Grace",
            last_name="Hopper",
            address_1="Av Siempreviva 742",
            city="Ñuñoa",
        ),
        items=[
            OrderItem(sku="NO-SKU-DET", name="Detergente genérico", quantity=4, price=1500),
        ],
        total=0,
    )


def main() -> None:
    order_demo = build_order_demo()
    order_fallback = build_order_fallback()

    excel_bytes = generate_excel([order_demo, order_fallback])
    df = pd.read_excel(BytesIO(excel_bytes), engine="openpyxl")

    expected_columns = [
        "Página",
        "Cliente",
        "Dirección",
        "Comuna",
        "Factura",
        "N° Pedido",
        "Valor",
        "Seguimiento",
        "Despacho",
        "Cobertor",
        "Detergente 60",
        "Detergente 35",
        "Chocolate",
        "Cafe",
        "Fecha_Etiqueta",
    ]
    assert list(df.columns) == expected_columns, f"Columnas inválidas: {list(df.columns)}"

    row_demo = df.loc[df["N° Pedido"] == "demo-1"].iloc[0]
    expected_det35 = 2 * 1 + 1 * 2 + 1 * 4 + 5 * 1  # 13
    expected_det60 = 3 * 1 + 1 * 2 + 2 * 3 + 5 * 1  # 16
    assert int(row_demo["Detergente 35"]) == expected_det35, row_demo
    assert int(row_demo["Detergente 60"]) == expected_det60, row_demo

    row_fallback = df.loc[df["N° Pedido"] == "demo-2"].iloc[0]
    assert int(row_fallback["Detergente 35"]) == 0, row_fallback
    assert int(row_fallback["Detergente 60"]) == 4, row_fallback

    print("OK: validación de exportación Excel detergentes correcta")


if __name__ == "__main__":
    main()
