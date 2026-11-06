#!/usr/bin/env python3
"""Bot market-making / volume untuk Polyester Spot (testnet).

Versi hasil audit. Perbedaan utama dari skrip lama:

1. Kuotasi sekarang **di dalam spread** (join best bid / best ask, opsional
   improve 1 tick). Skrip lama memaksa harga minimal `fee + 10 bps` dari mid,
   artinya butuh spread > 2 x 20 bps = 40 bps. Spread nyata di testnet cuma
   1-12 bps, jadi `plan_quotes` selalu mengembalikan None dan TIDAK ADA order
   yang pernah dipasang (termasuk order beli). Itu akar masalahnya.
2. Ukuran order tidak lagi bisa jatuh di bawah `minNotionalQuote` (5 USDT di
   semua pair). Skrip lama memakai 1.5% equity apa adanya sehingga akun kecil
   diam-diam tidak pernah memasang order.
3. Equity dihitung dari `trading + funding`, dan bot memperingatkan kalau dana
   masih nyangkut di bucket funding (order spot hanya memakai `trading` /
   `available`).
4. Order yang ditolak `POST_ONLY_CROSS` dicoba ulang satu tick menjauh.
5. Ada ringkasan alasan "kenapa tidak ada order" tiap siklus, dan opsi
   `BOT_TAKER_AFTER` supaya volume tetap jalan saat maker tidak kena.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from uuid import uuid4

from dotenv import load_dotenv

from polyester import (
    POLYESTER_TESTNET_ENVIRONMENT,
    AsyncPolyester,
    PolyesterApiError,
    PolyesterError,
)
from polyester.errors import PolyesterAuthError
from polyester.models import ClientOrderId
from polyester.types.money import Price

load_dotenv()


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _dec(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


API_URL = os.getenv("POLYESTER_API_URL", "").strip()
WS_URL = os.getenv("POLYESTER_WS_URL", "").strip()

QUOTES = [q.strip().upper() for q in os.getenv("BOT_QUOTES", "USDT,USDC").split(",") if q.strip()]
PAIRS_FILTER = [p.strip() for p in os.getenv("BOT_PAIRS", "").split(",") if p.strip()]
DRY_RUN = _env_bool("BOT_DRY_RUN", "0")
CANCEL_ON_START = _env_bool("BOT_CANCEL_ON_START", "1")
CYCLE_DELAY = float(os.getenv("BOT_CYCLE_DELAY", "15"))
ORDER_TTL = float(os.getenv("BOT_ORDER_TTL", "120"))
REPRICE_BPS = _dec("BOT_REPRICE_BPS", "5")
MIN_ORDER_AGE = float(os.getenv("BOT_MIN_ORDER_AGE", "15"))
ORDER_DELAY = float(os.getenv("BOT_ORDER_DELAY", "0.3"))
ORDER_PCT = _dec("BOT_ORDER_PCT", "1.5")

# --- inti perbaikan strategi -------------------------------------------------
# Butuh spread minimum berapa (bps) sebelum boleh kuotasi. 0 = selalu kuotasi
# asal ada book. Naikkan kalau hanya mau pair yang benar-benar menguntungkan.
MIN_SPREAD_BPS = _dec("BOT_MIN_SPREAD_BPS", "0")
# Kalau 1: syarat spread = 2*fee + MIN_EDGE_BPS (mode cari profit, bukan volume).
PROFITABLE_ONLY = _env_bool("BOT_PROFITABLE_ONLY", "0")
MIN_EDGE_BPS = _dec("BOT_MIN_EDGE_BPS", "1")
DEFAULT_MAKER_BPS = _dec("BOT_DEFAULT_MAKER_BPS", "2")
# 0 = join best bid/ask, 1 = maju satu tick ke tengah spread.
IMPROVE_TICKS = int(os.getenv("BOT_IMPROVE_TICKS", "0"))
# Kalau maker tidak kena selama N detik, ambil likuiditas (IOC di harga touch)
# supaya volume tetap jalan. 0 = matikan.
TAKER_AFTER = float(os.getenv("BOT_TAKER_AFTER", "300"))

INV_BAND = _dec("BOT_INV_BAND", "0.5")
QUOTE_MIN_PCT = _dec("BOT_QUOTE_MIN_PCT", "8")
QUOTE_TARGET_PCT = _dec("BOT_QUOTE_TARGET_PCT", "20")
REFILL_MAX_FRAC = _dec("BOT_REFILL_MAX_FRAC", "0.3")
REFILL_MAX_ORDERS = int(os.getenv("BOT_REFILL_MAX_ORDERS", "4"))
MAX_TAKER_SPREAD_BPS = _dec("BOT_MAX_TAKER_SPREAD_BPS", "60")
SLIPPAGE_BPS = int(os.getenv("BOT_SLIPPAGE_BPS", "100"))
MAX_DRAWDOWN_PCT = _dec("BOT_MAX_DRAWDOWN_PCT", "3")
MAX_ERRORS = int(os.getenv("BOT_MAX_ERRORS", "10"))
MAX_CYCLE_FAILS = int(os.getenv("BOT_MAX_CYCLE_FAILS", "8"))
MAX_CYCLES = int(os.getenv("BOT_MAX_CYCLES", "0"))

PREFIX = "bot-"
LEDGER_SCALE = Decimal(10) ** 18
PRICE_DIVISOR = Decimal(10) ** 9

log = logging.getLogger("bot")


# ----------------------------------------------------------------------------
# Util
# ----------------------------------------------------------------------------
ACCT = contextvars.ContextVar("acct", default="-")
SKIP = contextvars.ContextVar("skip_level", default=logging.INFO)


class AcctFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.acct = ACCT.get()
        return True


def setup_logging() -> None:
    handlers = [logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
    for h in handlers:
        h.addFilter(AcctFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(acct)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def fmt(value: Decimal, places: int = 6) -> str:
    s = format(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def real(price_obj) -> Decimal:
    return Decimal(int(price_obj.ticks)) / PRICE_DIVISOR


def err_text(e: Exception) -> str:
    code = getattr(e, "code", None)
    return f"{type(e).__name__}: {e}" + (f" [{code}]" if code else "")


def load_accounts() -> list[dict]:
    """Baca akun dari .env dengan suffix _1, _2, _3, ...

    Penomoran harus mulai dari 1 tanpa lompatan di awal. Entri yang cuma terisi
    sebagian dilewati (dengan peringatan) supaya salah ketik di satu akun tidak
    mematikan akun lain. `account_id` ganda dibuang: dua worker pada akun yang
    sama akan saling membatalkan order karena keduanya memakai prefix `bot-`.
    """
    accounts: list[dict] = []
    i = 1
    while True:
        key_id = os.getenv(f"POLYESTER_API_KEY_ID_{i}")
        private = os.getenv(f"POLYESTER_API_PRIVATE_KEY_{i}")
        account_id = os.getenv(f"POLYESTER_ACCOUNT_ID_{i}")
        if not any([key_id, private, account_id]):
            break
        if not all([key_id, private, account_id]):
            log.warning("Akun %d dilewati: kredensial tidak lengkap "
                        "(isi POLYESTER_API_KEY_ID_%d, POLYESTER_API_PRIVATE_KEY_%d, "
                        "POLYESTER_ACCOUNT_ID_%d).", i, i, i, i)
            i += 1
            continue
        accounts.append({
            "name": f"akun{i}",
            "api_key_id": key_id,
            "api_private_key": private,
            "account_id": account_id,
        })
        i += 1

    if not accounts:
        key_id = os.getenv("POLYESTER_API_KEY_ID")
        private = os.getenv("POLYESTER_API_PRIVATE_KEY")
        account_id = os.getenv("POLYESTER_ACCOUNT_ID")
        if all([key_id, private, account_id]):
            accounts.append({
                "name": "akun1",
                "api_key_id": key_id,
                "api_private_key": private,
                "account_id": account_id,
            })

    unique: list[dict] = []
    seen: dict[str, str] = {}
    for acc in accounts:
        first = seen.get(acc["account_id"])
        if first is not None:
            log.warning("Akun %s dilewati: account_id sama dengan %s (dua worker di satu "
                        "akun akan saling membatalkan order).", acc["name"], first)
            continue
        seen[acc["account_id"]] = acc["name"]
        unique.append(acc)
    return unique


class Pair:
    def __init__(self, row: dict):
        self.symbol: str = row["symbol"]
        self.base, _, self.quote = self.symbol.partition("-")
        self.status: str = str(row.get("status") or "ENABLED").upper()
        self.tick = Decimal(str(row.get("tick_size") or "0"))
        self.tick_ticks = max(int(self.tick * PRICE_DIVISOR), 1)
        self.step = Decimal(str(row.get("step_size") or "0"))
        if self.step <= 0:
            scale = row.get("base_quantity_scale")
            self.step = Decimal(1).scaleb(-int(scale)) if scale is not None else Decimal("0.00000001")
        self.min_qty = Decimal(str(row.get("min_qty_base") or "0"))
        self.min_notional = Decimal(str(row.get("min_notional_quote") or "0"))
        self.max_qty = Decimal(str(row.get("max_limit_qty_base") or "0"))
        self.max_notional = Decimal(str(row.get("max_order_notional_quote") or "0"))

    def ok_size(self, qty: Decimal, price: Decimal) -> bool:
        return qty > 0 and qty >= self.min_qty and qty * price >= self.min_notional

    def size(self, notional: Decimal, price: Decimal, cap_qty: Decimal) -> Decimal:
        """Ukuran order yang valid: menghormati step, min qty, dan min notional."""
        if price <= 0 or cap_qty <= 0:
            return Decimal(0)
        cap_notional = cap_qty * price
        budget = min(notional, cap_notional)
        if self.max_notional > 0:
            budget = min(budget, self.max_notional)
        qty = floor_step(budget / price, self.step)
        if self.min_qty > 0 and qty < self.min_qty:
            qty = ceil_step(self.min_qty, self.step)
        if self.min_notional > 0 and qty * price < self.min_notional:
            qty = ceil_step(self.min_notional / price, self.step)
        if self.max_qty > 0 and qty > self.max_qty:
            qty = floor_step(self.max_qty, self.step)
        if qty > cap_qty or not self.ok_size(qty, price):
            return Decimal(0)
        if self.max_notional > 0 and qty * price > self.max_notional:
            return Decimal(0)
        return qty


@dataclass
class Live:
    """Order maker milik bot yang sedang hidup."""
    coid: str
    symbol: str
    side: str
    ticks: int
    qty: Decimal
    placed_at: float   # time.monotonic()


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    placed: int = 0
    failed: int = 0
    fills: int = 0
    volume: Decimal = Decimal(0)
    refills: int = 0
    skipped: int = 0
    takers: int = 0
    live: dict = field(default_factory=dict)
    last_fill: dict = field(default_factory=dict)
    last_taker: dict = field(default_factory=dict)
    explained: set = field(default_factory=set)
    reasons: Counter = field(default_factory=Counter)

    def line(self) -> str:
        mins = (time.time() - self.started) / 60
        return (
            f"{mins:.1f} mnt | order dipasang={self.placed} aktif={len(self.live)} gagal={self.failed} "
            f"terisi={self.fills} volume={fmt(self.volume, 2)} taker={self.takers} "
            f"pendanaan={self.refills} dilewati={self.skipped}"
        )


# ----------------------------------------------------------------------------
# Data: saldo, harga acuan, valuasi
# ----------------------------------------------------------------------------
async def fetch_balances(client, assets: set[str]) -> dict[str, dict[str, Decimal]]:
    """Saldo per aset.

    ``available`` = yang bisa dipakai order (bucket trading dikurangi reservasi).
    ``trading``+``funding`` = total kepemilikan; deposit baru masuk ke funding
    dan TIDAK bisa dipakai trading sampai ditransfer internal.
    """
    result = await client.balances.list()
    by_id = {b.asset_id: b for b in result.balances}
    out: dict[str, dict[str, Decimal]] = {}
    for asset in sorted(assets):
        ledger_id = client.catalogs.ledger_id_for_asset(asset)
        bal = by_id.get(ledger_id) if ledger_id is not None else None
        if bal is None:
            out[asset] = {"available": Decimal(0), "total": Decimal(0), "funding": Decimal(0)}
        else:
            trading = Decimal(bal.trading) / LEDGER_SCALE
            funding = Decimal(bal.funding) / LEDGER_SCALE
            out[asset] = {
                "available": Decimal(bal.available) / LEDGER_SCALE,
                "total": trading + funding,
                "funding": funding,
            }
    return out


async def fetch_refs(client) -> dict[str, tuple[Decimal, bool]]:
    res = await client.market_overview.list(limit=100)
    refs: dict[str, tuple[Decimal, bool]] = {}
    for m in res.markets:
        sym = getattr(m, "symbol", "")
        if not sym:
            continue
        last = real(m.last_price) if m.last_price and m.last_price.ticks else Decimal(0)
        idx = real(m.index_price) if m.index_price and m.index_price.ticks else Decimal(0)
        ref = last or idx
        if ref > 0:
            refs[sym] = (ref, True)
    return refs


def price_in_usdt(asset: str, refs: dict) -> Decimal | None:
    if asset == "USDT":
        return Decimal(1)
    usdc = refs.get("USDC-USDT", (Decimal(1), True))[0]
    if asset == "USDC":
        return usdc
    if f"{asset}-USDT" in refs:
        return refs[f"{asset}-USDT"][0]
    if f"{asset}-USDC" in refs:
        return refs[f"{asset}-USDC"][0] * usdc
    if f"{asset}-BTC" in refs and "BTC-USDT" in refs:
        return refs[f"{asset}-BTC"][0] * refs["BTC-USDT"][0]
    return None


def portfolio_value(bal: dict, refs: dict, key: str = "total") -> tuple[Decimal, dict[str, Decimal]]:
    per_asset: dict[str, Decimal] = {}
    for asset, b in bal.items():
        px = price_in_usdt(asset, refs)
        if px is not None:
            per_asset[asset] = b[key] * px
    return sum(per_asset.values(), Decimal(0)), per_asset


# ----------------------------------------------------------------------------
# Order
# ----------------------------------------------------------------------------
async def record_fill(client, stats: Stats, lv: Live) -> None:
    """Ambil status akhir order dan catat berapa yang terisi (qty, harga rata-rata, volume)."""
    try:
        res = await client.orders.get(key=ClientOrderId(lv.coid), include_execution_history=False)
    except PolyesterError as e:
        log.debug("get %s gagal: %s", lv.coid, err_text(e))
        return
    o = res.order
    if o is None:
        return
    scale = client.catalogs.base_quantity_scale_for_symbol(lv.symbol)
    cum = Decimal(0)
    if o.cum_qty is not None and scale is not None:
        cum = o.cum_qty.as_decimal(scale)
    if cum > 0:
        avg = real(o.avg_px) if o.avg_px is not None else Decimal(lv.ticks) / PRICE_DIVISOR
        stats.fills += 1
        stats.volume += cum * avg
        stats.last_fill[lv.symbol] = time.monotonic()
        log.info("TERISI %s %s %s/%s @ %s (%s)", lv.symbol, lv.side.upper(), fmt(cum), fmt(lv.qty),
                 fmt(avg), o.status)
    else:
        log.info("Order %s %s selesai tanpa fill (%s)", lv.symbol, lv.side.upper(), o.status)


async def cancel_live(client, stats: Stats, bal: dict | None, coid: str) -> bool:
    """Batalkan satu order bot. Dana yang terkunci dikembalikan ke saldo lokal (bal)."""
    lv = stats.live.get(coid)
    if lv is None:
        return False
    if DRY_RUN:
        stats.live.pop(coid, None)
        return True
    try:
        await client.orders.cancel(key=ClientOrderId(coid), symbol=lv.symbol)
    except PolyesterApiError as e:
        # Biasanya sudah terisi/hilang; reconcile siklus berikutnya yang menyimpulkan.
        log.debug("cancel %s: %s", coid, err_text(e))
        return False
    stats.live.pop(coid, None)
    await record_fill(client, stats, lv)   # kalau sempat terisi sebagian, tetap tercatat
    if bal is not None:
        base, _, quote = lv.symbol.partition("-")
        if lv.side == "buy" and quote in bal:
            bal[quote]["available"] += lv.qty * Decimal(lv.ticks) / PRICE_DIVISOR
        elif lv.side == "sell" and base in bal:
            bal[base]["available"] += lv.qty
    return True


async def reconcile(client, stats: Stats, first_cycle: bool = False) -> None:
    """Order yang sudah tidak terbuka padahal bukan kita batalkan = terisi."""
    own, _ = await list_own_open(client)
    open_ids = {o.client_order_id for o in own}
    for coid in [c for c in stats.live if c not in open_ids]:
        lv = stats.live.pop(coid)
        await record_fill(client, stats, lv)
    orphans = [o for o in own if o.client_order_id not in stats.live]
    if orphans and first_cycle and CANCEL_ON_START and not DRY_RUN:
        n = await cancel_own(client, orphans)
        log.info("Membatalkan %d order bot yang tidak dikenal (sisa sesi lama).", n)


async def sweep_expired(client, stats: Stats) -> int:
    """Batalkan order yang sudah hidup >= BOT_ORDER_TTL detik (nanti dipasang ulang di harga baru)."""
    now, n = time.monotonic(), 0
    for coid, lv in list(stats.live.items()):
        if now - lv.placed_at >= ORDER_TTL:
            n += await cancel_live(client, stats, None, coid)
    if n:
        log.info("%d order kedaluwarsa (>= %ss) diganti.", n, fmt(Decimal(str(ORDER_TTL)), 0))
    return n


async def list_own_open(client) -> tuple[list, int]:
    """Order terbuka milik bot (berawalan 'bot-') dan jumlah order lain di akun."""
    own, foreign, token = [], 0, None
    while True:
        page = await client.orders.list_open(limit=100, page_token=token)
        for o in page.orders:
            if (o.client_order_id or "").startswith(PREFIX):
                own.append(o)
            else:
                foreign += 1
        token = page.next_page_token
        if not token:
            break
    return own, foreign


async def cancel_own(client, own: list) -> int:
    n = 0
    for o in own:
        sym = client.catalogs.symbol_for_symbol_id(o.symbol_id)
        try:
            await client.orders.cancel(key=ClientOrderId(o.client_order_id), symbol=sym)
            n += 1
        except PolyesterApiError as e:
            log.debug("cancel %s dilewati: %s", o.client_order_id, err_text(e))
    return n


async def place_limit(client, stats: Stats, pair: Pair, side: str, qty: Decimal, ticks: int) -> bool:
    coid = f"{PREFIX}{uuid4().hex[:20]}"
    price = Price.from_ticks(ticks, symbol=pair.symbol)
    label = f"{pair.symbol} {side.upper():4s} qty={fmt(qty)} @ {fmt(real(price))}"
    if DRY_RUN:
        stats.placed += 1
        stats.live[coid] = Live(coid, pair.symbol, side, ticks, qty, time.monotonic())
        log.info("[DRY] %s", label)
        return True
    try:
        res = await client.orders.create(
            symbol=pair.symbol, side=side, order_type="limit", tif="gtc",
            qty=format(qty, "f"), price=price, post_only=True, client_order_id=coid,
        )
        stats.placed += 1
        stats.live[coid] = Live(coid, pair.symbol, side, ticks, qty, time.monotonic())
        log.info("OK   %s (%s)", label, res.status)
        return True
    except PolyesterApiError as e:
        code = str(getattr(e, "code", "") or "")
        if "POST_ONLY_CROSS" in code:
            # Harga book bergerak di antara baca dan kirim: mundur satu tick lalu coba sekali lagi.
            back = ticks - pair.tick_ticks if side == "buy" else ticks + pair.tick_ticks
            if back > 0:
                log.info("%s kena POST_ONLY_CROSS, coba ulang di %s", label,
                         fmt(Decimal(back) / PRICE_DIVISOR))
                return await place_limit(client, stats, pair, side, qty, back)
        stats.failed += 1
        stats.reasons[f"gagal {side}: {code or 'error'}"] += 1
        log.warning("GAGAL %s -> %s", label, err_text(e))
        return False
    finally:
        await asyncio.sleep(ORDER_DELAY)


async def place_ioc(client, stats: Stats, pair: Pair, side: str, qty: Decimal, ticks: int) -> bool:
    """Ambil likuiditas sekarang juga: limit IOC di harga touch (bukan market buta)."""
    coid = f"{PREFIX}{uuid4().hex[:20]}"
    price = Price.from_ticks(ticks, symbol=pair.symbol)
    label = f"{pair.symbol} {side.upper():4s} qty={fmt(qty)} @ {fmt(real(price))} IOC"
    if DRY_RUN:
        log.info("[DRY] %s", label)
        return True
    try:
        res = await client.orders.create(
            symbol=pair.symbol, side=side, order_type="limit", tif="ioc",
            qty=format(qty, "f"), price=price, client_order_id=coid,
        )
        stats.takers += 1
        log.info("OK   %s (%s)", label, res.status)
        await record_fill(client, stats, Live(coid, pair.symbol, side, ticks, qty, time.monotonic()))
        stats.last_taker[pair.symbol] = time.monotonic()
        return True
    except PolyesterApiError as e:
        stats.failed += 1
        log.warning("GAGAL %s -> %s", label, err_text(e))
        return False
    finally:
        await asyncio.sleep(ORDER_DELAY)


async def place_market_sell(client, stats: Stats, pair: Pair, qty: Decimal) -> bool:
    label = f"{pair.symbol} SELL qty={fmt(qty)} MARKET"
    if DRY_RUN:
        log.info("[DRY] %s", label)
        return True
    try:
        res = await client.orders.create(
            symbol=pair.symbol, side="sell", order_type="market", qty=format(qty, "f"),
            max_slippage_bps=SLIPPAGE_BPS, client_order_id=f"{PREFIX}{uuid4().hex[:20]}",
        )
        stats.refills += 1
        log.info("OK   %s (%s)", label, res.status)
        return True
    except PolyesterApiError as e:
        stats.failed += 1
        log.warning("GAGAL %s -> %s", label, err_text(e))
        return False
    finally:
        await asyncio.sleep(ORDER_DELAY)


async def top_of_book(client, symbol: str):
    book = await client.orderbook.get(symbol=symbol, depth=1)
    bid = book.bids[0].price if book.bids else None
    ask = book.asks[0].price if book.asks else None
    return bid, ask


# ----------------------------------------------------------------------------
# Strategi
# ----------------------------------------------------------------------------
def plan_quotes(pair: Pair, bid, ask) -> tuple[int | None, int | None, str]:
    """Pasang harga di dalam spread: join touch atau maju `IMPROVE_TICKS` tick."""
    t = pair.tick_ticks
    bid_t, ask_t = int(bid.ticks), int(ask.ticks)
    if ask_t <= bid_t:
        return None, None, "book menyilang / kosong"
    step = max(0, IMPROVE_TICKS) * t
    buy_t, sell_t = bid_t + step, ask_t - step
    if buy_t >= sell_t or buy_t >= ask_t or sell_t <= bid_t:
        buy_t, sell_t = bid_t, ask_t
    return buy_t, sell_t, ""


def spread_bps(bid_d: Decimal, ask_d: Decimal) -> Decimal:
    mid = (bid_d + ask_d) / 2
    return (ask_d - bid_d) / mid * 10000 if mid > 0 else Decimal(0)


def explain(stats: Stats, key: str, msg: str, *args) -> None:
    """Alasan dilewati: INFO sekali per pair, DEBUG sisanya."""
    if key not in stats.explained:
        stats.explained.add(key)
        log.info(msg, *args)
    else:
        slog("[%s] " + msg, key, *args)


def slog(msg: str, *args) -> None:
    log.log(SKIP.get(), msg, *args)


async def report_pairs(client, pairs, maker_bps) -> None:
    """Tabel kelayakan saat start: spread vs syarat minimum."""
    log.info("Pemindaian pair (spread book vs syarat kuotasi):")
    viable = 0
    for pair in pairs:
        try:
            bid, ask = await top_of_book(client, pair.symbol)
        except PolyesterError as e:
            log.info("  %-12s error: %s", pair.symbol, err_text(e))
            continue
        if bid is None or ask is None:
            log.info("  %-12s orderbook satu sisi/kosong", pair.symbol)
            continue
        bid_d, ask_d = real(bid), real(ask)
        sp = spread_bps(bid_d, ask_d)
        fee = maker_bps.get(pair.symbol, DEFAULT_MAKER_BPS)
        need = 2 * fee + MIN_EDGE_BPS if PROFITABLE_ONLY else MIN_SPREAD_BPS
        ok = sp >= need and ask_d > bid_d
        viable += ok
        log.info("  %-12s bid=%-12s ask=%-12s spread=%6.2f bps | fee maker=%s bps | syarat=%s bps | %s",
                 pair.symbol, fmt(bid_d), fmt(ask_d), sp, fmt(fee, 2), fmt(need, 1),
                 "LAYAK" if ok else "DILEWATI")
    log.info("%d/%d pair akan dikuotasi (mode %s).", viable, len(pairs),
             "cari profit" if PROFITABLE_ONLY else "volume")


async def quote_pair(client, stats, pair, bal, refs, equity, targets, per_asset, maker_bps) -> None:
    try:
        bid, ask = await top_of_book(client, pair.symbol)
    except PolyesterError as e:
        stats.skipped += 1
        stats.reasons["orderbook error"] += 1
        slog("%s: orderbook error %s", pair.symbol, err_text(e))
        return
    if bid is None or ask is None:
        stats.skipped += 1
        stats.reasons["book satu sisi"] += 1
        explain(stats, f"{pair.symbol}:book", "%s: orderbook satu sisi/kosong, lewati", pair.symbol)
        return

    bid_d, ask_d = real(bid), real(ask)
    sp = spread_bps(bid_d, ask_d)
    fee = maker_bps.get(pair.symbol, DEFAULT_MAKER_BPS)
    need = 2 * fee + MIN_EDGE_BPS if PROFITABLE_ONLY else MIN_SPREAD_BPS
    if sp < need:
        stats.skipped += 1
        stats.reasons["spread < syarat"] += 1
        explain(stats, f"{pair.symbol}:spread",
                "%s: spread %.2f bps < syarat %.2f bps, lewati", pair.symbol, sp, need)
        return

    buy_t, sell_t, why = plan_quotes(pair, bid, ask)
    if buy_t is None:
        stats.skipped += 1
        stats.reasons[why] += 1
        explain(stats, f"{pair.symbol}:plan", "%s: %s", pair.symbol, why)
        return

    mid = (bid_d + ask_d) / 2
    notional = max(equity * ORDER_PCT / 100, pair.min_notional * Decimal("1.05"))

    # --- pertahankan order yang masih wajar, batalkan yang sudah tidak pas ---
    keep: set[str] = set()
    now = time.monotonic()
    for lv in [v for v in stats.live.values() if v.symbol == pair.symbol]:
        px = Decimal(lv.ticks) / PRICE_DIVISOR
        want_t = buy_t if lv.side == "buy" else sell_t
        want = Decimal(want_t) / PRICE_DIVISOR
        drift = abs(px - want) / mid * 10000 if mid > 0 else Decimal(0)
        age = now - lv.placed_at
        passive = px < ask_d if lv.side == "buy" else px > bid_d
        if passive and age < ORDER_TTL and (drift <= REPRICE_BPS or age < MIN_ORDER_AGE):
            keep.add(lv.side)
        else:
            log.info("%s %s @ %s diganti (target %s, bergeser %.1f bps, umur %.0fs)", pair.symbol,
                     lv.side.upper(), fmt(px), fmt(want), drift, age)
            await cancel_live(client, stats, bal, lv.coid)

    # --- inventory ---
    target = targets.get(pair.base, Decimal(0))
    cur = per_asset.get(pair.base, Decimal(0))
    target = max(target, notional * 3)
    ratio = cur / target if target > 0 else Decimal(1)
    allow_buy = ratio < 1 + INV_BAND
    allow_sell = ratio > 1 - INV_BAND

    quote_bal = bal[pair.quote]
    base_bal = bal[pair.base]
    placed_any = False

    if "buy" not in keep and allow_buy:
        px = Decimal(buy_t) / PRICE_DIVISOR
        qty = pair.size(notional, px, quote_bal["available"] * Decimal("0.98") / px)
        if qty > 0:
            if await place_limit(client, stats, pair, "buy", qty, buy_t):
                quote_bal["available"] -= qty * px
                placed_any = True
        else:
            stats.reasons["saldo quote kurang / min notional"] += 1
            explain(stats, f"{pair.symbol}:buyfund",
                    "%s: tidak bisa beli (quote available=%s, min notional=%s, min qty=%s)",
                    pair.symbol, fmt(quote_bal["available"], 2), fmt(pair.min_notional, 2),
                    fmt(pair.min_qty))

    if "sell" not in keep and allow_sell:
        px = Decimal(sell_t) / PRICE_DIVISOR
        qty = pair.size(notional, px, base_bal["available"] * Decimal("0.995"))
        if qty > 0:
            if await place_limit(client, stats, pair, "sell", qty, sell_t):
                base_bal["available"] -= qty
                placed_any = True
        elif base_bal["available"] > 0:
            stats.reasons["saldo base kurang / min qty"] += 1

    # --- fallback volume: kalau maker tidak kena, ambil likuiditas ---
    if TAKER_AFTER > 0:
        last_fill = stats.last_fill.get(pair.symbol, stats.started_mono)
        last_taker = stats.last_taker.get(pair.symbol, 0.0)
        if now - last_fill >= TAKER_AFTER and now - last_taker >= TAKER_AFTER:
            await taker_hop(client, stats, pair, bid, ask, bid_d, ask_d, notional, ratio,
                            quote_bal, base_bal)

    if not placed_any and not keep and TAKER_AFTER <= 0:
        slog("%s: tidak ada order (inventory %.0f%% target, saldo quote %s, base %s)",
             pair.symbol, ratio * 100, fmt(quote_bal["available"], 2), fmt(base_bal["available"]))
        stats.skipped += 1


async def taker_hop(client, stats, pair, bid, ask, bid_d, ask_d, notional, ratio,
                    quote_bal, base_bal) -> None:
    """Ambil likuiditas seukuran `notional` untuk memastikan ada transaksi."""
    sp = spread_bps(bid_d, ask_d)
    if sp > MAX_TAKER_SPREAD_BPS:
        stats.reasons["spread terlalu lebar untuk taker"] += 1
        return
    buy_side = ratio < Decimal(1)
    if buy_side:
        px = ask_d
        qty = pair.size(notional, px, quote_bal["available"] * Decimal("0.98") / px)
        if qty <= 0:
            return
        if await place_ioc(client, stats, pair, "buy", qty, int(ask.ticks)):
            log.info("%s: maker tidak kena %.0fs, ambil likuiditas (BELI).",
                     pair.symbol, TAKER_AFTER)
    else:
        px = bid_d
        qty = pair.size(notional, px, base_bal["available"] * Decimal("0.995"))
        if qty <= 0:
            return
        if await place_ioc(client, stats, pair, "sell", qty, int(bid.ticks)):
            log.info("%s: maker tidak kena %.0fs, ambil likuiditas (JUAL).",
                     pair.symbol, TAKER_AFTER)


async def refill_quote(client, stats, pairs, bal, refs, equity, per_asset, targets) -> None:
    quote_avail = sum((bal[q]["available"] * price_in_usdt(q, refs) for q in QUOTES if q in bal
                       and price_in_usdt(q, refs) is not None), Decimal(0))
    if quote_avail >= equity * QUOTE_MIN_PCT / 100:
        return
    need = equity * QUOTE_TARGET_PCT / 100 - quote_avail
    if need <= 0:
        return
    log.info("Modal quote tipis (%s USDT = %.1f%% equity). Perlu menambah ~%s USDT.",
             fmt(quote_avail, 2), quote_avail / equity * 100 if equity else 0, fmt(need, 2))

    by_symbol = {p.symbol: p for p in pairs}
    cands = []
    for asset, b in bal.items():
        if asset in QUOTES:
            continue
        px = price_in_usdt(asset, refs)
        if px is None or b["available"] <= 0:
            continue
        sym = next((f"{asset}-{q}" for q in QUOTES if f"{asset}-{q}" in by_symbol), None)
        if sym is None:
            continue
        cands.append((b["available"] * px, asset, by_symbol[sym]))
    cands.sort(key=lambda c: c[0], reverse=True)

    done = 0
    for value, asset, pair in cands:
        if need <= 1 or done >= REFILL_MAX_ORDERS:
            break
        bid, ask = await top_of_book(client, pair.symbol)
        if bid is None or ask is None:
            continue
        bid_d, ask_d = real(bid), real(ask)
        sp = spread_bps(bid_d, ask_d)
        if sp > MAX_TAKER_SPREAD_BPS:
            log.info("%s: spread %.0f bps terlalu lebar untuk jual cepat, coba aset lain", pair.symbol, sp)
            continue
        sell_value = min(need, value * REFILL_MAX_FRAC)
        qty = pair.size(sell_value, bid_d, bal[asset]["available"])
        if qty <= 0:
            continue
        if await place_market_sell(client, stats, pair, qty):
            proceeds = qty * bid_d
            need -= proceeds
            bal[asset]["available"] -= qty
            bal[pair.quote]["available"] += proceeds * Decimal("0.998")
            done += 1


# ----------------------------------------------------------------------------
# Loop utama per akun
# ----------------------------------------------------------------------------
def load_pairs(client) -> list[Pair]:
    rows = (client.catalogs.spot_config or {}).get("pairs") or []
    pairs = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        p = Pair(r)
        if p.status not in ("ENABLED", "TRADING", "ACTIVE"):
            continue
        pairs.append(p)
    if PAIRS_FILTER:
        pairs = [p for p in pairs if p.symbol in set(PAIRS_FILTER)]
    return pairs


async def load_maker_fees(client) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    try:
        res = await client.fees.get_spot_fee_rates()
        for r in res.fee_rates:
            if r.symbol and r.maker_fee_rate_percent:
                out[r.symbol] = Decimal(r.maker_fee_rate_percent) * 100
    except Exception as e:
        log.warning("Tarif fee tidak terbaca (%s), pakai default %s bps", err_text(e), DEFAULT_MAKER_BPS)
    return out


async def run_account(client, account_name: str) -> None:
    """Jalankan bot untuk satu akun."""
    ACCT.set(account_name)
    stats = Stats()
    await client.wait_for_catalogs()

    all_pairs = load_pairs(client)
    pairs = [p for p in all_pairs if p.quote in QUOTES]
    if not pairs:
        log.error("Tidak ada pair berquote %s.", "/".join(QUOTES))
        return
    assets = {a for p in pairs for a in (p.base, p.quote)}
    maker_bps = await load_maker_fees(client)
    if maker_bps:
        vals = list(maker_bps.values())
        log.info("Fee maker terbaca untuk %d pair: %s - %s bps", len(vals), fmt(min(vals), 2),
                 fmt(max(vals), 2))
    else:
        log.warning("Fee maker tidak terbaca, memakai default %s bps.", DEFAULT_MAKER_BPS)
    log.info("Mulai. dry_run=%s | %d pair | order=%s%% equity | mode=%s | spread min=%s bps",
             DRY_RUN, len(pairs), ORDER_PCT, "profit" if PROFITABLE_ONLY else "volume",
             fmt(MIN_SPREAD_BPS, 1))

    own, foreign = await list_own_open(client)
    if own:
        log.info("Ada %d order bot lama terbuka.%s", len(own), "" if CANCEL_ON_START else " (tidak dibatalkan)")
        if CANCEL_ON_START and not DRY_RUN:
            n = await cancel_own(client, own)
            log.info("Dibatalkan: %d order.", n)
            await asyncio.sleep(1)
    if foreign:
        log.info("Ada %d order lain di akun (bukan milik bot) -> tidak disentuh.", foreign)

    refs = await fetch_refs(client)
    bal = await fetch_balances(client, assets)
    equity0, per_asset0 = portfolio_value(bal, refs)
    if equity0 <= 0:
        log.error("Equity 0 / harga acuan tidak terbaca. Cek saldo dan catalog. Berhenti.")
        return
    funding_idle = {a: b["funding"] for a, b in bal.items() if b["funding"] > 0}
    if funding_idle:
        log.warning("Dana di bucket FUNDING (tidak bisa dipakai order spot): %s. "
                    "Pindahkan funding -> trading (internal transfer) di aplikasi.",
                    ", ".join(f"{a}={fmt(v, 2)}" for a, v in sorted(funding_idle.items())))
    unvalued = [a for a in assets if a not in per_asset0]
    if unvalued:
        log.warning("Aset tanpa harga acuan (tidak dihitung): %s", ", ".join(sorted(unvalued)))
    targets = {a: v for a, v in per_asset0.items() if a not in QUOTES}
    log.info("Equity awal: %s USDT. Target inventory: %s", fmt(equity0, 2),
             ", ".join(f"{a}={fmt(v, 1)}" for a, v in sorted(targets.items(), key=lambda kv: -kv[1])[:8])
             or "-")
    tradable = sum((bal[q]["available"] * price_in_usdt(q, refs) for q in QUOTES if q in bal
                    and price_in_usdt(q, refs) is not None), Decimal(0))
    log.info("Saldo quote siap pakai: %s USDT.", fmt(tradable, 2))

    await report_pairs(client, pairs, maker_bps)

    cycle = bad_cycles = fails = 0
    try:
        while MAX_CYCLES == 0 or cycle < MAX_CYCLES:
            cycle += 1
            SKIP.set(logging.INFO if cycle == 1 or cycle % 10 == 0 else logging.DEBUG)
            stats.reasons.clear()
            log.info("=== Siklus %d ===", cycle)
            try:
                placed0, failed0 = stats.placed, stats.failed

                await reconcile(client, stats, first_cycle=(cycle == 1))
                if await sweep_expired(client, stats):
                    await asyncio.sleep(0.5)

                refs = await fetch_refs(client)
                bal = await fetch_balances(client, assets)
                equity, per_asset = portfolio_value(bal, refs)
                pnl = equity - equity0
                pnl_pct = pnl / equity0 * 100
                quote_total = sum((per_asset.get(q, Decimal(0)) for q in QUOTES), Decimal(0))
                log.info("Equity %s USDT | PnL %+.2f (%+.2f%%) | quote %s (%.0f%%)",
                         fmt(equity, 2), pnl, pnl_pct, fmt(quote_total, 2),
                         quote_total / equity * 100 if equity else 0)
                if pnl_pct <= -MAX_DRAWDOWN_PCT:
                    log.error("Batas rugi %s%% tercapai (%+.2f%%). Bot berhenti.", MAX_DRAWDOWN_PCT, pnl_pct)
                    break

                try:
                    await refill_quote(client, stats, pairs, bal, refs, equity, per_asset, targets)
                except PolyesterError as e:
                    log.warning("Pendanaan gagal: %s", err_text(e))

                for pair in pairs:
                    try:
                        await quote_pair(client, stats, pair, bal, refs, equity, targets,
                                         per_asset, maker_bps)
                    except PolyesterError as e:
                        stats.failed += 1
                        log.warning("%s: error -> %s", pair.symbol, err_text(e))
                    except Exception as e:  # bug/edge case: jangan matikan seluruh akun
                        stats.failed += 1
                        stats.reasons["error internal"] += 1
                        log.warning("%s: error tak terduga -> %s", pair.symbol, err_text(e))

                if stats.reasons:
                    top = ", ".join(f"{k}={v}" for k, v in stats.reasons.most_common(4))
                    log.info("Alasan dilewati: %s", top)

                if stats.failed > failed0 and stats.placed == placed0:
                    bad_cycles += 1
                    if MAX_ERRORS > 0 and bad_cycles >= MAX_ERRORS:
                        log.error("%d siklus beruntun semua order gagal. Bot berhenti.", bad_cycles)
                        break
                else:
                    bad_cycles = 0
                log.info("Ringkasan: %s", stats.line())
                fails = 0
            except asyncio.CancelledError:
                raise
            except PolyesterAuthError as e:
                log.error("Autentikasi ditolak: %s. Periksa API key/private key. Bot berhenti.", err_text(e))
                break
            except PolyesterError as e:
                fails += 1
                wait = getattr(e, "retry_after", None)
                wait = min(max(float(wait), 1.0), 120.0) if wait else min(60.0, 5.0 * 2 ** (fails - 1))
                log.warning("Siklus %d terganggu: %s | order yang ada dibiarkan, coba lagi %.0f detik "
                            "(gangguan beruntun %d%s)",
                            cycle, err_text(e), wait, fails, f"/{MAX_CYCLE_FAILS}" if MAX_CYCLE_FAILS else "")
                if MAX_CYCLE_FAILS > 0 and fails >= MAX_CYCLE_FAILS:
                    log.error("Gangguan beruntun mencapai batas. Bot berhenti.")
                    break
                await asyncio.sleep(wait)
                continue
            await asyncio.sleep(CYCLE_DELAY)
    finally:
        if not DRY_RUN:
            try:
                for coid in list(stats.live):
                    await asyncio.shield(cancel_live(client, stats, None, coid))
                own, _ = await asyncio.shield(list_own_open(client))
                n = await asyncio.shield(cancel_own(client, own))
                log.info("Cleanup: %d order bot dibatalkan.", n)
            except Exception as e:
                log.warning("Cleanup gagal: %s", err_text(e))
        try:
            refs = await asyncio.shield(fetch_refs(client))
            bal = await asyncio.shield(fetch_balances(client, assets))
            equity, _ = portfolio_value(bal, refs)
            log.info("SELESAI. Equity akhir %s USDT (awal %s) = %+.2f%% | %s",
                     fmt(equity, 2), fmt(equity0, 2), (equity - equity0) / equity0 * 100, stats.line())
        except Exception:
            pass
    return stats


async def main() -> None:
    setup_logging()
    accounts = load_accounts()
    if not accounts:
        log.error("Tidak ada akun ditemukan. Isi POLYESTER_API_KEY_ID_1, "
                  "POLYESTER_API_PRIVATE_KEY_1, POLYESTER_ACCOUNT_ID_1 di .env")
        return

    log.info("Menjalankan %d akun secara paralel...", len(accounts))

    async def run_one(acc: dict):
        ACCT.set(acc["name"])
        try:
            kwargs = {
                "environment": POLYESTER_TESTNET_ENVIRONMENT,
                "api_key_id": acc["api_key_id"],
                "api_private_key": acc["api_private_key"],
                "default_account_id": acc["account_id"],
            }
            if API_URL:
                kwargs["api_url"] = API_URL
            if WS_URL:
                kwargs["ws_url"] = WS_URL
            async with AsyncPolyester(**kwargs) as client:
                await run_account(client, acc["name"])
        except Exception as e:
            log.error("Akun %s error: %s", acc["name"], err_text(e))

    await asyncio.gather(*(run_one(acc) for acc in accounts))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDihentikan oleh user.")
