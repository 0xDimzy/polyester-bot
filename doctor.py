#!/usr/bin/env python3
"""Doctor: jawab pertanyaan "kenapa order beli tidak jalan?" dengan bukti.

Menjalankan pemeriksaan berurutan dan mencetak kode error apa adanya:

  1. koneksi + katalog (pair, tick, step, min notional)
  2. saldo (funding vs trading vs available)
  3. fee maker/taker
  4. top-of-book tiap pair + spread vs syarat
  5. `preview_order` untuk order beli kecil (admissible? kalau tidak, kodenya apa)
  6. opsional `--place`: benar-benar kirim 1 order beli post-only ukuran minimum,
     laporkan status + reject_reason, lalu batalkan.

Pemakaian:
    python doctor.py                # semua cek, tanpa mengirim order
    python doctor.py --place        # ikut mengirim 1 order beli kecil
    python doctor.py --symbol ETH-USDT --place
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import ROUND_DOWN, Decimal

from dotenv import load_dotenv

from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester, PolyesterApiError, PolyesterError
from polyester.models import ClientOrderId
from polyester.types.money import Price

load_dotenv()

PRICE_DIVISOR = Decimal(10) ** 9
LEDGER_SCALE = Decimal(10) ** 18
PREFIX = "doctor-"


def real(price_obj) -> Decimal:
    return Decimal(int(price_obj.ticks)) / PRICE_DIVISOR


def line(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def load_account() -> dict | None:
    for suffix in ("_1", ""):
        key_id = os.getenv(f"POLYESTER_API_KEY_ID{suffix}")
        private = os.getenv(f"POLYESTER_API_PRIVATE_KEY{suffix}")
        account_id = os.getenv(f"POLYESTER_ACCOUNT_ID{suffix}")
        if all([key_id, private, account_id]):
            return {"api_key_id": key_id, "api_private_key": private, "account_id": account_id}
    return None


async def run(symbol_filter: str, do_place: bool) -> None:
    acc = load_account()
    if not acc:
        print("Tidak menemukan kredensial. Isi POLYESTER_API_KEY_ID_1 / "
              "POLYESTER_API_PRIVATE_KEY_1 / POLYESTER_ACCOUNT_ID_1 di .env")
        return

    kwargs = {
        "environment": POLYESTER_TESTNET_ENVIRONMENT,
        "api_key_id": acc["api_key_id"],
        "api_private_key": acc["api_private_key"],
        "default_account_id": acc["account_id"],
    }
    if os.getenv("POLYESTER_API_URL"):
        kwargs["api_url"] = os.environ["POLYESTER_API_URL"]

    async with AsyncPolyester(**kwargs) as client:
        line("Koneksi & katalog")
        print("api_url :", client.api_url)
        print("account :", acc["account_id"])
        await client.wait_for_catalogs()
        pairs = {p["symbol"]: p for p in (client.catalogs.spot_config or {}).get("pairs", [])
                 if isinstance(p, dict)}
        print(f"pair    : {len(pairs)}")

        line("Batasan order per pair (dari katalog resmi)")
        print(f"{'symbol':14}{'tick':>12}{'step':>14}{'minQty':>12}{'minNotional':>13}{'status':>10}")
        for sym, p in sorted(pairs.items()):
            print(f"{sym:14}{p.get('tick_size', '-'):>12}{p.get('step_size', '-'):>14}"
                  f"{p.get('min_qty_base', '-'):>12}{p.get('min_notional_quote', '-'):>13}"
                  f"{str(p.get('status', '-')):>10}")

        line("Saldo")
        res = await client.balances.list()
        by_id = {b.asset_id: b for b in res.balances}
        rows = []
        for sym in sorted(pairs):
            for asset in (pairs[sym].get("base_asset") or sym.split("-")[0], "USDT", "USDC"):
                ledger_id = client.catalogs.ledger_id_for_asset(asset)
                b = by_id.get(ledger_id) if ledger_id is not None else None
                trading = Decimal(b.trading) / LEDGER_SCALE if b else Decimal(0)
                funding = Decimal(b.funding) / LEDGER_SCALE if b else Decimal(0)
                avail = Decimal(b.available) / LEDGER_SCALE if b else Decimal(0)
                if trading or funding or avail:
                    rows.append((asset, trading, funding, avail))
        seen = set()
        print(f"{'asset':10}{'trading':>18}{'funding':>18}{'available':>18}")
        for asset, trading, funding, avail in rows:
            if asset in seen:
                continue
            seen.add(asset)
            print(f"{asset:10}{trading:>18.8f}{funding:>18.8f}{avail:>18.8f}")
        if not rows:
            print("(semua nol — akun belum punya dana sama sekali)")
        funding_only = [r for r in rows if r[3] == 0 and r[2] > 0]
        if funding_only:
            print("\n!! Ada dana di bucket FUNDING tapi available=0.")
            print("   Order spot hanya memakai saldo trading. Transfer funding -> trading dulu.")

        line("Fee")
        try:
            fees = await client.fees.get_spot_fee_rates()
            for f in fees.fee_rates[:5]:
                print(f"{f.symbol or f.symbol_id}: maker={f.maker_fee_rate_percent}% "
                      f"taker={f.taker_fee_rate_percent}%")
        except PolyesterError as e:
            print("gagal baca fee:", e, getattr(e, "code", ""))

        line("Top of book & spread")
        mkts = await client.market_overview.list(limit=100)
        for m in sorted(mkts.markets, key=lambda x: x.symbol):
            if not m.symbol.endswith(("-USDT", "-USDC")):
                continue
            try:
                book = await client.orderbook.get(symbol=m.symbol, depth=1)
            except PolyesterError as e:
                print(f"{m.symbol:14} orderbook error: {e}")
                continue
            if not book.bids or not book.asks:
                print(f"{m.symbol:14} book satu sisi")
                continue
            bid, ask = real(book.bids[0].price), real(book.asks[0].price)
            mid = (bid + ask) / 2
            sp = (ask - bid) / mid * 10000 if mid else Decimal(0)
            print(f"{m.symbol:14} bid={bid:<12} ask={ask:<12} spread={sp:6.2f} bps")

        line("Uji order beli (preview)")
        sym = symbol_filter
        if not sym:
            sym = "BTC-USDT" if "BTC-USDT" in pairs else sorted(pairs)[0]
        p = pairs[sym]
        tick = Decimal(str(p.get("tick_size") or "0.01"))
        step = Decimal(str(p.get("step_size") or "0"))
        min_notional = Decimal(str(p.get("min_notional_quote") or "0"))
        book = await client.orderbook.get(symbol=sym, depth=1)
        if not book.bids or not book.asks:
            print(f"{sym}: book kosong, tidak bisa uji.")
            return
        bid, ask = real(book.bids[0].price), real(book.asks[0].price)
        # harga beli: join best bid (pasti pasif, aman untuk post-only)
        buy_ticks = int(book.bids[0].price.ticks)
        px = Decimal(buy_ticks) / PRICE_DIVISOR
        notional = max(min_notional * Decimal("1.1"), Decimal(6))
        qty = (notional / px)
        if step > 0:
            qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty <= 0:
            qty = step
        print(f"{sym}: bid={bid} ask={ask} -> coba BELI {qty} @ {px} post_only "
              f"(notional ~{qty * px:.2f})")
        try:
            preview = await client.orders.preview_order(
                symbol=sym, side="buy", order_type="limit", tif="gtc",
                qty=format(qty, "f"), price=Price.from_ticks(buy_ticks, symbol=sym), post_only=True,
            )
            print("admissible:", preview.admissible)
            if preview.rejection:
                print("  code      :", preview.rejection.code)
                for v in preview.rejection.violations:
                    print("  violation :", v.field_path, v.rule_id, v.message)
            if preview.resolved_base_qty is not None:
                print("  resolved  :", preview.resolved_base_qty)
        except PolyesterError as e:
            print("preview gagal:", type(e).__name__, e, "code=", getattr(e, "code", ""))
            for v in getattr(e, "violations", []) or []:
                print("  violation :", v.field_path, v.rule_id, v.message)

        if not do_place:
            print("\n(tidak mengirim order; pakai --place untuk benar-benar mencoba)")
            return

        line("Kirim order beli sungguhan (post-only, ukuran minimum)")
        coid = f"{PREFIX}{os.urandom(6).hex()}"
        try:
            r = await client.orders.create(
                symbol=sym, side="buy", order_type="limit", tif="gtc",
                qty=format(qty, "f"), price=Price.from_ticks(buy_ticks, symbol=sym),
                post_only=True, client_order_id=coid,
            )
            print("OK status:", r.status, "order_id:", r.order_id)
            got = await client.orders.get(key=ClientOrderId(coid), include_execution_history=False)
            o = got.order
            if o is not None:
                print("   status:", o.status, "reject_reason:", o.reject_reason,
                      "cancel_reason:", o.cancel_reason)
            await asyncio.sleep(1)
            await client.orders.cancel(key=ClientOrderId(coid), symbol=sym)
            print("   order dibatalkan (bersih-bersih).")
        except PolyesterApiError as e:
            print("DITOLAK:", type(e).__name__, "code=", getattr(e, "code", ""))
            print("  pesan:", e)
            for v in getattr(e, "violations", []) or []:
                print("  violation:", v.field_path, v.rule_id, v.message)
            if getattr(e, "existing_order_id", None):
                print("  existing_order_id:", e.existing_order_id)
        except PolyesterError as e:
            print("GAGAL:", type(e).__name__, e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=os.getenv("BOT_PAIRS", "").split(",")[0].strip())
    ap.add_argument("--place", action="store_true", help="kirim 1 order beli kecil lalu batalkan")
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.place))


if __name__ == "__main__":
    main()
