#!/usr/bin/env python3
"""
Cobertura-Rechner

Berechnet:
- Zielpreis anhand einer Prozentbewegung
- Gewinn der vollständig geschlossenen profitablen Position
- maximal reduzierbare Coinmenge der verlustreichen Position
- verbleibende Coinmenge und Positionsgröße
- realisierten und unrealisierten PnL

Annahme:
- Positionsgrößen werden als Coin-Menge eingegeben.
- Linear Futures / USDT-Kontrakte.
- Gebühren sind fest hinterlegt.
"""

from dataclasses import dataclass


@dataclass
class Position:
    side: str
    entry_price: float
    qty: float

    def pnl(self, exit_price: float, qty: float | None = None) -> float:
        close_qty = self.qty if qty is None else qty

        if self.side == "long":
            return close_qty * (exit_price - self.entry_price)

        if self.side == "short":
            return close_qty * (self.entry_price - exit_price)

        raise ValueError(f"Unbekannte Seite: {self.side}")


def positive_float(prompt: str) -> float:
    while True:
        try:
            value = float(input(prompt).strip().replace(",", "."))
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            print("Bitte eine positive Zahl eingeben.")


def select_option(prompt: str, options: set[str]) -> str:
    while True:
        value = input(prompt).strip().lower()
        if value in options:
            return value
        print(f"Erlaubte Eingaben: {', '.join(sorted(options))}")


def calculate_close_fee(
    qty: float,
    price: float,
    fee_rate_percent: float,
) -> float:
    return qty * price * fee_rate_percent / 100.0


def main() -> None:
    print("\n=== Cobertura-Rechner ===\n")

    long_qty = positive_float("Long-Menge in Coins: ")
    long_entry = positive_float("Long-Entry-Preis: ")

    short_qty = positive_float("Short-Menge in Coins: ")
    short_entry = positive_float("Short-Entry-Preis: ")

    long_pos = Position(
        side="long",
        entry_price=long_entry,
        qty=long_qty,
    )
    short_pos = Position(
        side="short",
        entry_price=short_entry,
        qty=short_qty,
    )

    print("\nWelche profitable Position soll vollständig geschlossen werden?")
    print("  short = Zielpreis liegt unter dem Short-Entry")
    print("  long  = Zielpreis liegt über dem Long-Entry")

    close_side = select_option(
        "Position schließen [short/long]: ",
        {"short", "long"},
    )

    move_percent = positive_float(
        "Abstand vom Entry dieser Position in Prozent: "
    )
    fee_percent = 0.055

    if close_side == "short":
        target_price = short_entry * (1 - move_percent / 100)
        winner = short_pos
        loser = long_pos
    else:
        target_price = long_entry * (1 + move_percent / 100)
        winner = long_pos
        loser = short_pos

    winner_gross_pnl = winner.pnl(target_price)

    winner_close_fee = calculate_close_fee(
        qty=winner.qty,
        price=target_price,
        fee_rate_percent=fee_percent,
    )

    available_profit = winner_gross_pnl - winner_close_fee

    # Verlust pro Coin der Gegenposition am Zielpreis
    loss_per_coin = -loser.pnl(target_price, qty=1.0)

    print("\n" + "=" * 60)
    print("ERGEBNIS")
    print("=" * 60)

    print(f"\nZielpreis: {target_price:.10f}")

    if winner_gross_pnl <= 0:
        print(
            "\nWARNUNG: Die ausgewählte Position ist am Zielpreis "
            "nicht im Gewinn."
        )
        return

    if available_profit <= 0:
        print(
            "\nNach Gebühren bleibt kein Gewinn zur Reduzierung "
            "der Gegenposition übrig."
        )
        return

    if loss_per_coin <= 0:
        print(
            "\nDie Gegenposition befindet sich am Zielpreis nicht im Verlust. "
            "Eine verlustfinanzierte Reduzierung ist nicht notwendig."
        )
        return

    # Gebühren der Gegenpositions-Schließung müssen ebenfalls vom
    # verfügbaren Gewinn getragen werden.
    #
    # available_profit =
    # qty * loss_per_coin + qty * target_price * fee_rate
    close_cost_per_coin = (
        loss_per_coin
        + target_price * fee_percent / 100.0
    )

    loser_close_qty = min(
        available_profit / close_cost_per_coin,
        loser.qty,
    )

    remaining_qty = loser.qty - loser_close_qty
    remaining_percent = (
        remaining_qty / loser.qty * 100
        if loser.qty > 0
        else 0
    )
    remaining_unrealized_pnl = loser.pnl(
        target_price,
        qty=remaining_qty,
    )

    print(
        f"Zu schließende Coins: {loser_close_qty:.10f} "
        f"({loser_close_qty * loser.entry_price:.2f} USDT)"
    )
    print(
        f"Verbleibende Coins:   {remaining_qty:.10f} "
        f"({remaining_qty * loser.entry_price:.2f} USDT)"
    )
    print(f"Verbleibender Anteil: {remaining_percent:.2f} %")
    print(
        f"Unrealisierter PnL:   "
        f"{remaining_unrealized_pnl:.6f} USDT"
    )


if __name__ == "__main__":
    main()
