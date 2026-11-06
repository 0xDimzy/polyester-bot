#!/usr/bin/env python3
"""Validasi encoding order memakai kode SDK asli (tanpa kredensial, tanpa kirim).

Membuktikan bahwa request yang dibentuk bot benar-benar bisa di-encode oleh
`polyester-sdk`: post_only, tif, harga (ticks), qty (scaled), symbol_id.

Butuh SDK terpasang (requirements.txt). Jalankan:
    python tests/validate_sdk_encoding.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polyester.catalogs import CatalogManager  # noqa: E402
from polyester.codecs.orders import (  # noqa: E402
    create_order_to_proto,
    normalize_create_order_request,
    resolve_quantity_scale,
)
from polyester.types.money import Price  # noqa: E402

SPOT_CONFIG_URL = "https://api.testnet.polyester.com/v1/spot/config"
OVERVIEW_URL = "https://api.testnet.polyester.com/v1/spot/markets/overview?limit=100"
PRICE_SCALE = Decimal(10) ** 9
NOTIONAL = Decimal("150")


def main() -> int:
    raw = json.load(urllib.request.urlopen(SPOT_CONFIG_URL, timeout=30))
    overview = {m["symbol"]: m for m in
                json.load(urllib.request.urlopen(OVERVIEW_URL, timeout=30))["markets"]}

    cat = CatalogManager()
    cat.hydrate_spot_config(raw)

    ok = failed = skipped = 0
    for row in raw["pairs"]:
        sym = row["symbol"]
        if not sym.endswith(("-USDT", "-USDC")):
            continue
        last = Decimal(overview.get(sym, {}).get("lastPrice") or "0")
        if last <= 0:
            skipped += 1
            continue
        step = Decimal(row["stepSize"])
        tick = Decimal(row["tickSize"])
        qty = (NOTIONAL / last / step).to_integral_value(rounding=ROUND_DOWN) * step
        # join harga di grid tick
        px = (last / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        ticks = int(px * PRICE_SCALE)
        try:
            req = normalize_create_order_request(
                symbol=sym, symbol_id=cat.symbol_id_for_symbol(sym),
                side="buy", order_type="limit", tif="gtc", qty=format(qty, "f"),
                price=Price.from_ticks(ticks, symbol=sym), post_only=True,
                client_order_id="bot-validate",
            )
            scale = resolve_quantity_scale(cat, sym, req.qty)
            order = create_order_to_proto(req, quantity_scale=scale,
                                          quote_quantity_scale=None).order
            assert order.WhichOneof("execution") == "limit_gtc"
            assert order.limit_gtc.post_only is True
            assert order.limit_gtc.price_ticks == ticks
            ok += 1
            print(f"{sym:12} ok  qty={qty:<14} ticks={ticks:<16} scale={scale}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"{sym:12} FAIL {type(e).__name__}: {e} code={getattr(e, 'code', None)}")

    print(f"\n{ok} pair ter-encode, {failed} gagal, {skipped} tanpa harga")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
