from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Expense:
    date: str
    category: str
    amount: float
    note: str


def load_expenses(path: str | Path) -> list[Expense]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return [
            Expense(row["date"], row["category"], float(row["amount"]), row["note"])
            for row in csv.DictReader(handle)
        ]


def total_spending(expenses: list[Expense]) -> float:
    return round(sum(abs(expense.amount) for expense in expenses), 2)


def category_totals(expenses: list[Expense]) -> dict[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for expense in expenses:
        totals[expense.category] += expense.amount
    return {category: round(amount, 2) for category, amount in totals.items()}


if __name__ == "__main__":
    expenses = load_expenses("sample.csv")
    print(f"Total spending: {total_spending(expenses):.2f}")
    print("By category:")
    for category, amount in sorted(category_totals(expenses).items()):
        print(f"  {category}: {amount:.2f}")
