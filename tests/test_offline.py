#!/usr/bin/env python3
"""Uji offline bot.py tanpa jaringan dan tanpa SDK asli.

Kenapa ada file ini: kita ingin membuktikan bahwa logika baru benar-benar
memasang order beli (dan jual) serta tidak menyentuh API di luar bentuk yang
didukung SDK. Modul `polyester` diganti stub, client diganti fake.

Jalankan:
    python tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --- stub SDK sebelum bot.py diimpor ----------------------------------------
PRICE_DIVISOR = Decimal(10) ** 9


class FakePrice:
    __slots__ = ("ticks", "symbol")

    def __init__(self, ticks: int, symbol: str | None = None) -> None:
        self.ticks = int(ticks)
        self.symbol = symbol

    @classmethod
    def from_ticks(cls, ticks: int, *, symbol: str | None = None) -> "FakePrice":
        return cls(ticks, symbol)

    def __repr__(self) -> str:
        return f"Price({Decimal(self.ticks) / PRICE_DIVISOR})"


class FakeQty:
    def __init__(self, value: Decimal) -> None:
        self.value = value

    def as_decimal(self, scale: int | None = None) -> Decimal:
        return self.value


class FakeApiError(Exception):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.violations: list = []


class FakeError(Exception):
    pass


class FakeAuthError(FakeError):
    pass


class FakeOrderKey:
    def __init__(self, value: str) -> None:
        self.value = value


def _install_stubs() -> None:
    poly = types.ModuleType("polyester")
    poly.__path__ = []
    poly.POLYESTER_TESTNET_ENVIRONMENT = object()
    poly.AsyncPolyester = object
    poly.PolyesterApiError = FakeApiError
    poly.PolyesterError = FakeError

    errors = types.ModuleType("polyester.errors")
    errors.PolyesterAuthError = FakeAuthError

    models = types.ModuleType("polyester.models")
    models.ClientOrderId = FakeOrderKey

    types_pkg = types.ModuleType("polyester.types")
    types_pkg.__path__ = []
    money = types.ModuleType("polyester.types.money")
    money.Price = FakePrice

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None

    for name, mod in {
        "polyester": poly,
        "polyester.errors": errors,
        "polyester.models": models,
        "polyester.types": types_pkg,
        "polyester.types.money": money,
        "dotenv": dotenv,
    }.items():
        sys.modules[name] = mod


_install_stubs()

os.environ.update({
    "BOT_CYCLE_DELAY": "0",
    "BOT_ORDER_DELAY": "0",
    "BOT_MAX_CYCLES": "3",
    "BOT_TAKER_AFTER": "0",
    "BOT_MIN_SPREAD_BPS": "0",
    "BOT_MAX_DRAWDOWN_PCT": "99",
    "BOT_ORDER_TTL": "600",
})

import bot  # noqa: E402  (harus setelah stub)

PRICE_SCALE = Decimal(10) ** 9


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------
PAIRS = [
    # symbol, mid, tick, step, min_qty, min_notional, base_scale, base_balance
    ("BTC-USDT", Decimal("76000"), Decimal("0.01"), Decimal("0.00000001"), Decimal("0.00001"), Decimal("5"), 8, Decimal("0.01")),
    ("ETH-USDT", Decimal("2239"), Decimal("0.01"), Decimal("0.000001"), Decimal("0.0001"), Decimal("5"), 6, Decimal("0.5")),
    ("SOL-USDT", Decimal("90"), Decimal("0.01"), Decimal("0.000001"), Decimal("0.01"), Decimal("5"), 6, Decimal("3")),
    ("ARB-USDT", Decimal("0.169"), Decimal("0.0001"), Decimal("0.1"), Decimal("1"), Decimal("5"), 6, Decimal("50")),
]
QUOTE_BALANCE = Decimal("10000")


def _scaled(value: Decimal) -> str:
    return str(int(value * Decimal(10) ** 18))


class FakeCatalogs:
    def __init__(self) -> None:
        self.spot_config = {"pairs": [
            {
                "symbol": sym,
                "symbol_id": i + 1,
                "base_asset": sym.split("-")[0],
                "quote_asset": "USDT",
                "tick_size": str(tick),
                "step_size": str(step),
                "min_qty_base": str(min_qty),
                "min_notional_quote": str(min_notional),
                "base_quantity_scale": scale,
                "status": "ENABLED",
            }
            for i, (sym, _mid, tick, step, min_qty, min_notional, scale, _bal) in enumerate(PAIRS)
        ]}
        self._ids = {"USDT": 1, "USDC": 2, "BTC": 3, "ETH": 4, "SOL": 5, "ARB": 6}
        self._scales = {sym: scale for sym, _m, _t, _s, _mq, _mn, scale, _b in PAIRS}

    def ledger_id_for_asset(self, asset: str):
        return self._ids.get(asset)

    def base_quantity_scale_for_symbol(self, symbol: str):
        return self._scales.get(symbol)

    def symbol_for_symbol_id(self, symbol_id: int):
        rows = self.spot_config["pairs"]
        return rows[symbol_id - 1]["symbol"] if 0 < symbol_id <= len(rows) else None

    def order_limits_for_symbol(self, symbol: str):
        return None


class FakeClient:
    def __init__(self) -> None:
        self.catalogs = FakeCatalogs()
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self._open: dict[str, SimpleNamespace] = {}
        self._filled: dict[str, SimpleNamespace] = {}
        self._cross_once = True
        self.cycle = 0
        self._bals = {"USDT": QUOTE_BALANCE}
        for sym, _mid, _t, _s, _mq, _mn, _sc, bal in PAIRS:
            self._bals[sym.split("-")[0]] = bal

    async def wait_for_catalogs(self) -> None:
        return None

    # --- market data -------------------------------------------------------
    async def _book(self, symbol: str):
        row = next(p for p in PAIRS if p[0] == symbol)
        mid = row[1]
        tick = row[2]
        bid = (mid * Decimal("0.9997") / tick).to_integral_value(rounding="ROUND_FLOOR") * tick
        ask = (mid * Decimal("1.0003") / tick).to_integral_value(rounding="ROUND_CEILING") * tick
        from decimal import ROUND_CEILING, ROUND_FLOOR
        bid = (mid * Decimal("0.9997") / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        ask = (mid * Decimal("1.0003") / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        return bid, ask

    class _Book:
        def __init__(self, bid: Decimal, ask: Decimal) -> None:
            self.bids = [SimpleNamespace(price=FakePrice(int(bid * PRICE_SCALE)))]
            self.asks = [SimpleNamespace(price=FakePrice(int(ask * PRICE_SCALE)))]

    async def orderbook_get(self, *, symbol: str, depth: int = 1):
        bid, ask = await self._book(symbol)
        return self._Book(bid, ask)

    async def market_overview_list(self, limit: int = 100):
        return SimpleNamespace(markets=[
            SimpleNamespace(symbol=sym, last_price=FakePrice(int(mid * PRICE_SCALE)),
                            index_price=FakePrice(int(mid * PRICE_SCALE)))
            for sym, mid, *_ in PAIRS
        ])

    # --- akun --------------------------------------------------------------
    async def balances_list(self):
        rows = []
        ids = self.catalogs._ids
        for asset, amount in self._bals.items():
            rows.append(SimpleNamespace(
                asset_id=ids[asset],
                trading=_scaled(amount),
                funding="0",
                reserved="0",
                available=_scaled(amount),
            ))
        return SimpleNamespace(balances=rows)

    async def fees_get_spot_fee_rates(self):
        return SimpleNamespace(fee_rates=[
            SimpleNamespace(symbol=p[0], maker_fee_rate_percent="0.02", taker_fee_rate_percent="0.05")
            for p in PAIRS
        ])

    async def orders_list_open(self, *, limit: int = 100, page_token=None):
        return SimpleNamespace(orders=list(self._open.values()), next_page_token="")

    async def orders_get(self, *, key, include_execution_history=None):
        coid = key.value
        order = self._filled.get(coid) or self._open.get(coid)
        return SimpleNamespace(order=order)

    async def orders_cancel(self, *, key, symbol=None):
        self.cancelled.append(key.value)
        self._open.pop(key.value, None)
        return SimpleNamespace(status="cancelled")

    async def orders_create(self, **kw):
        if kw.get("post_only") and self._cross_once and kw["side"] == "buy":
            self._cross_once = False
            raise FakeApiError("post only would cross", code="ERROR_CODE_POST_ONLY_CROSS")
        qty = Decimal(str(kw["qty"]))
        assert qty > 0, "qty harus > 0"
        price = kw["price"]
        assert isinstance(price, FakePrice), "price harus Price"
        px = Decimal(price.ticks) / PRICE_SCALE
        row = next(p for p in PAIRS if p[0] == kw["symbol"])
        notional = qty * px
        assert notional >= row[5], f"notional {notional} < min {row[5]} di {kw['symbol']}"
        assert qty >= row[4], f"qty {qty} < min qty {row[4]} di {kw['symbol']}"
        coid = kw["client_order_id"]
        self.created.append({"symbol": kw["symbol"], "side": kw["side"], "qty": qty,
                             "px": px, "post_only": kw.get("post_only", False),
                             "tif": kw.get("tif")})
        order = SimpleNamespace(
            client_order_id=coid,
            symbol_id=[p[0] for p in PAIRS].index(kw["symbol"]) + 1,
            status="accepted",
            cum_qty=FakeQty(qty),
            avg_px=FakePrice(price.ticks),
        )
        self._open[coid] = order
        return SimpleNamespace(status="accepted", order_id=coid, client_order_id=coid)

    # helper: simulasikan sebagian order kena fill
    def fill_oldest(self) -> None:
        if not self._open:
            return
        coid = next(iter(self._open))
        order = self._open.pop(coid)
        order.status = "filled"
        self._filled[coid] = order


def _wire(fake: "FakeClient") -> "FakeClient":
    """Lampirkan service client pada fake (bentuknya sama seperti AsyncPolyester)."""
    fake.orders = SimpleNamespace(
        create=fake.orders_create,
        cancel=fake.orders_cancel,
        get=fake.orders_get,
        list_open=fake.orders_list_open,
    )
    fake.balances = SimpleNamespace(list=fake.balances_list)
    fake.fees = SimpleNamespace(get_spot_fee_rates=fake.fees_get_spot_fee_rates)
    fake.orderbook = SimpleNamespace(get=fake.orderbook_get)
    fake.market_overview = SimpleNamespace(list=fake.market_overview_list)
    return fake


async def _run_bot() -> FakeClient:
    fake = _wire(FakeClient())

    orig_cycle_delay = bot.CYCLE_DELAY
    bot.CYCLE_DELAY = 0
    # jalankan siklus, menyisipkan fill di antara siklus supaya reconcile teruji
    for i in range(3):
        fake.cycle = i
        await bot.run_account(fake, "test")
        fake.fill_oldest()
    bot.CYCLE_DELAY = orig_cycle_delay
    return fake


# ---------------------------------------------------------------------------
# Tes
# ---------------------------------------------------------------------------
def test_bot_places_orders() -> None:
    fake = asyncio.run(_run_bot())
    buys = [o for o in fake.created if o["side"] == "buy"]
    sells = [o for o in fake.created if o["side"] == "sell"]
    assert buys, "bot tidak memasang order beli sama sekali"
    assert sells, "bot tidak memasang order jual sama sekali"
    assert all(o["post_only"] for o in fake.created if o["tif"] == "gtc")
    print(f"[ok] order dipasang: {len(fake.created)} "
          f"(beli={len(buys)}, jual={len(sells)}), dibatalkan={len(fake.cancelled)}")


def test_plan_quotes_inside_spread() -> None:
    pair = bot.Pair({"symbol": "BTC-USDT", "tick_size": "0.01", "step_size": "0.00000001",
                     "min_qty_base": "0.00001", "min_notional_quote": "5"})
    bid = FakePrice(int(Decimal("76000") * PRICE_SCALE))
    ask = FakePrice(int(Decimal("76002") * PRICE_SCALE))
    buy_t, sell_t, why = bot.plan_quotes(pair, bid, ask)
    assert buy_t is not None and sell_t is not None, why
    assert buy_t == bid.ticks, "buy harus join best bid"
    assert sell_t == ask.ticks, "sell harus join best ask"
    assert pair.tick_ticks == int(Decimal("0.01") * PRICE_SCALE)
    print(f"[ok] plan_quotes: buy={buy_t} sell={sell_t} (spread 2 tick)")


def test_plan_quotes_spread_1_tick() -> None:
    pair = bot.Pair({"symbol": "BTC-USDT", "tick_size": "0.01", "step_size": "0.00000001"})
    bid = FakePrice(1000)
    ask = FakePrice(1001)
    buy_t, sell_t, _ = bot.plan_quotes(pair, bid, ask)
    assert (buy_t, sell_t) == (1000, 1001)
    print("[ok] plan_quotes: spread 1 tick tetap menghasilkan dua sisi")


def test_size_respects_min_notional() -> None:
    pair = bot.Pair({"symbol": "BTC-USDT", "tick_size": "0.01", "step_size": "0.00000001",
                     "min_qty_base": "0.00001", "min_notional_quote": "5"})
    px = Decimal("76000")
    # budget 0.5 USDT < min notional 5 -> harus dibulatkan naik ke 5 USDT
    qty = pair.size(Decimal("0.5"), px, Decimal("1"))
    assert qty > 0 and qty * px >= 5, f"qty={qty} notional={qty * px}"
    # tanpa saldo -> nol
    assert pair.size(Decimal("10"), px, Decimal("0")) == 0
    print(f"[ok] size: budget 0.5 USDT -> qty {qty} (notional {qty * px})")


def test_old_strategy_would_not_quote() -> None:
    """Regresi: rumus lama butuh spread > 2*(fee+edge) = 40 bps."""
    fee, edge = Decimal("2"), Decimal("10")
    half = (fee + edge) / 10000
    quoted = 0
    for sym, mid, *_ in PAIRS:
        bid = mid * Decimal("0.9997")
        ask = mid * Decimal("1.0003")
        ref = (bid + ask) / 2
        want_bid = ref * (1 - half)
        want_ask = ref * (1 + half)
        if want_bid > bid and want_ask < ask:
            quoted += 1
    assert quoted == 0
    print("[ok] rumus lama: 0 pair bisa dikuotasi (sesuai temuan audit)")


def test_live_catalog_parsing() -> None:
    """Kalau snapshot katalog testnet tersedia, pastikan parsing Pair benar."""
    cfg_path = Path(__file__).resolve().parent / "fixtures" / "spot_config.json"
    if not cfg_path.exists():
        print("[skip] snapshot katalog testnet tidak ada")
        return
    cfg = json.loads(cfg_path.read_text())
    rows = cfg["pairs"]
    # tiru hydrate_spot_config SDK: tambahkan kunci snake_case
    for row in rows:
        for snake, camel in (("tick_size", "tickSize"), ("step_size", "stepSize"),
                             ("min_qty_base", "minQtyBase"), ("min_notional_quote", "minNotionalQuote")):
            row[snake] = row.get(camel)
        row["base_quantity_scale"] = row.get("baseQuantityScale")
    pairs = [bot.Pair(r) for r in rows]
    assert all(p.tick_ticks >= 1 for p in pairs)
    assert all(p.step > 0 for p in pairs), [p.symbol for p in pairs if p.step <= 0]
    assert all(p.min_notional > 0 for p in pairs)
    btc = next(p for p in pairs if p.symbol == "BTC-USDT")
    assert btc.tick_ticks == 10_000_000, btc.tick_ticks  # 0.01 * 1e9
    print(f"[ok] katalog live: {len(pairs)} pair ter-parse, BTC tick_ticks={btc.tick_ticks}")


def test_new_strategy_quotes_live_books() -> None:
    """Dengan book testnet yang tersimpan, strategi baru harus mengkuotasi semua pair."""
    base = Path(__file__).resolve().parent / "fixtures"
    cfg_path, ov_path = base / "spot_config.json", base / "overview.json"
    if not (cfg_path.exists() and ov_path.exists()):
        print("[skip] fixture book testnet tidak ada")
        return
    rows = json.loads(cfg_path.read_text())["pairs"]
    for row in rows:
        for snake, camel in (("tick_size", "tickSize"), ("step_size", "stepSize"),
                             ("min_qty_base", "minQtyBase"),
                             ("min_notional_quote", "minNotionalQuote")):
            row[snake] = row.get(camel)
        row["base_quantity_scale"] = row.get("baseQuantityScale")
    pairs = {p.symbol: p for p in (bot.Pair(r) for r in rows)}
    markets = {m["symbol"]: m for m in json.loads(ov_path.read_text())["markets"]}

    viable = total = 0
    for sym, m in markets.items():
        pair = pairs.get(sym)
        if pair is None or not sym.endswith(("-USDT", "-USDC")):
            continue
        bid_s, ask_s = m.get("bestBid"), m.get("bestAsk")
        if not bid_s or not ask_s or Decimal(bid_s) <= 0:
            continue
        total += 1
        bid = FakePrice(int(Decimal(bid_s) / pair.tick) * pair.tick_ticks)
        ask = FakePrice(int(Decimal(ask_s) / pair.tick) * pair.tick_ticks)
        buy_t, sell_t, why = bot.plan_quotes(pair, bid, ask)
        if buy_t is not None:
            viable += 1
            assert buy_t == bid.ticks and sell_t == ask.ticks, (sym, buy_t, sell_t)
    assert total >= 18, f"fixture hanya punya {total} pair berbook"
    assert viable == total, f"hanya {viable}/{total} pair yang bisa dikuotasi"
    print(f"[ok] strategi baru mengkuotasi {viable}/{total} pair pada book testnet nyata")


def test_multi_account_runs_in_parallel() -> None:
    """Dua akun di .env -> dua client terpisah, label log berbeda, dua-duanya trading."""
    keys = [(f"POLYESTER_API_KEY_ID_{i}", f"key{i}") for i in (1, 2)]
    keys += [(f"POLYESTER_API_PRIVATE_KEY_{i}", f"priv{i}") for i in (1, 2)]
    keys += [(f"POLYESTER_ACCOUNT_ID_{i}", f"acc{i}") for i in (1, 2)]
    # akun 3 sengaja setengah terisi: harus dilewati, bukan menghentikan loop
    keys += [("POLYESTER_API_KEY_ID_3", "key3"), ("POLYESTER_ACCOUNT_ID_3", "acc3")]
    for name, value in keys:
        os.environ[name] = value

    created: list[FakeClient] = []

    class Ctx:
        def __init__(self, client: FakeClient) -> None:
            self.client = client

        async def __aenter__(self) -> FakeClient:
            self.client.acct_label = bot.ACCT.get()
            return self.client

        async def __aexit__(self, *exc) -> bool:
            return False

    def factory(**kwargs):
        fake = _wire(FakeClient())
        fake.kw = kwargs
        created.append(fake)
        return Ctx(fake)

    orig_polyester = bot.AsyncPolyester
    orig_setup = bot.setup_logging
    bot.AsyncPolyester = factory
    bot.setup_logging = lambda: None
    try:
        asyncio.run(bot.main())
    finally:
        bot.AsyncPolyester = orig_polyester
        bot.setup_logging = orig_setup
        for name, _ in keys:
            os.environ.pop(name, None)

    assert len(created) == 2, f"harus 2 akun jalan, dapat {len(created)}"
    assert {c.kw["api_key_id"] for c in created} == {"key1", "key2"}
    assert {c.acct_label for c in created} == {"akun1", "akun2"}, "label log per akun tertukar"
    assert all(c.created for c in created), "ada akun yang tidak memasang order"
    assert all(any(o["side"] == "buy" for o in c.created) for c in created), "ada akun tanpa order beli"
    print(f"[ok] multi akun: {len(created)} client paralel, "
          f"order masing-masing = {[len(c.created) for c in created]}")


TESTS = [
    test_plan_quotes_inside_spread,
    test_plan_quotes_spread_1_tick,
    test_size_respects_min_notional,
    test_old_strategy_would_not_quote,
    test_live_catalog_parsing,
    test_new_strategy_quotes_live_books,
    test_bot_places_orders,
    test_multi_account_runs_in_parallel,
]


if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"[ERR ] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tes lulus")
    sys.exit(1 if failed else 0)
