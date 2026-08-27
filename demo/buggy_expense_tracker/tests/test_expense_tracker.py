from expense_tracker import Expense, category_totals, total_spending


def test_refund_reduces_total_spending() -> None:
    expenses = [
        Expense("2026-08-01", "food", 100.0, "dinner"),
        Expense("2026-08-02", "food", -20.0, "refund"),
    ]
    assert total_spending(expenses) == 80.0


def test_category_names_are_normalized_before_grouping() -> None:
    expenses = [
        Expense("2026-08-01", "Food", 12.5, "lunch"),
        Expense("2026-08-02", " food ", 7.5, "snack"),
    ]
    assert category_totals(expenses) == {"food": 20.0}
