"""Browser fallback controller (PyAutoGUI / Playwright) with a mandatory >$50
confirmation gate.

Used when the native GUI cannot run (e.g. a browser-only deployment or API
geoblocks). Any order with notional value greater than ``$50.00`` triggers a
blocking modal confirmation before execution.
"""

from __future__ import annotations

from dataclasses import dataclass

CONFIRMATION_THRESHOLD_USD = 50.0


@dataclass(slots=True)
class PendingOrder:
    side: str
    action: str
    price: float
    size: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


class ConfirmationGate:
    """Blocks orders exceeding the notional threshold until explicitly confirmed.

    ``confirm_callback`` is invoked to present a blocking modal (QMessageBox in
    the native GUI, or a Playwright/JS ``confirm`` dialog in the browser).
    """

    def __init__(self, threshold_usd: float = CONFIRMATION_THRESHOLD_USD) -> None:
        self.threshold_usd = threshold_usd
        self.confirmations_required: list[PendingOrder] = []

    def requires_confirmation(self, order: PendingOrder) -> bool:
        return order.notional_usd > self.threshold_usd

    def submit(self, order: PendingOrder, user_confirmed: bool) -> bool:
        """Return True if the order is allowed to execute."""
        if self.requires_confirmation(order) and not user_confirmed:
            self.confirmations_required.append(order)
            return False
        return True

    def clear(self) -> None:
        self.confirmations_required.clear()


class BrowserFallbackController:
    """Coordinates browser-based execution via Playwright when available."""

    def __init__(self, dashboard_url: str = "http://localhost:8000") -> None:
        self.dashboard_url = dashboard_url
        self.gate = ConfirmationGate()

    def _confirm_modal(self, order: PendingOrder) -> bool:
        """Present a blocking confirm dialog and return the user's decision."""
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(self.dashboard_url)
                confirmed = page.evaluate(
                    f"() => window.confirm('Execute order? Notional: ${round(order.notional_usd, 2)}')"
                )
                browser.close()
                return bool(confirmed)
        except Exception:  # noqa: BLE001 - fall back to native input when Playwright absent
            return _native_confirm(order)

    def execute(self, order: PendingOrder) -> bool:
        if self.gate.requires_confirmation(order):
            return self.gate.submit(order, self._confirm_modal(order))
        return True


def _native_confirm(order: PendingOrder) -> bool:
    """Native blocking confirmation using tkinter (stdlib) as a last resort."""
    import tkinter.messagebox as mb

    return bool(mb.askyesno(
        "Confirm Order",
        f"Execute order for ${order.notional_usd:.2f} notional value?",
    ))
