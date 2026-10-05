"""Helpers that turn a few lines of intent into a full World.

A scenario reads like its catalog row:

    b = WorldBuilder(seats=4)
    inv = b.charge(at=RENEWAL)
    b.session(b.member("Marcus Lee"), at=RENEWAL + hours(5), actions=("login", "export"))
    world = b.build()
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

from world.schema import (
    AppEvent,
    CreditGrant,
    Dispute,
    Interval,
    Invoice,
    InvoiceLine,
    Member,
    PlanPeriod,
    Refund,
    Role,
    SessionEvent,
    Subscription,
    Ticket,
    TicketMessage,
    Tier,
    Workspace,
    World,
)

# Placeholder price list, in cents per Seat. Annual is ten months' worth.
MONTHLY_PRICE = {Tier.STARTER: 1000, Tier.TEAM: 2000, Tier.BUSINESS: 4000}
ANNUAL_MONTHS = 10


def T(text: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' as UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


def days(n: float) -> timedelta:
    return timedelta(days=n)


def add_months(dt: datetime, n: int) -> datetime:
    month = dt.month - 1 + n
    return dt.replace(year=dt.year + month // 12, month=month % 12 + 1)


def list_price(tier: Tier, interval: Interval, seats: int) -> int:
    months = ANNUAL_MONTHS if interval is Interval.YEAR else 1
    return MONTHLY_PRICE[tier] * months * seats


class WorldBuilder:
    def __init__(
        self,
        name: str = "Acme Design",
        tier: Tier = Tier.TEAM,
        interval: Interval = Interval.MONTH,
        seats: int = 4,
        created: datetime = T("2025-11-08 14:00"),
        owner: str = "Dana Okafor",
        tax_rate: float = 0.0,
        discount_pct: int = 0,
    ):
        self.tier, self.interval, self.seats = tier, interval, seats
        self.tax_rate, self.discount_pct = tax_rate, discount_pct
        self.ws = Workspace(
            id="ws_001",
            name=name,
            created_at=created,
            status="active",
            stripe_customer_id="cus_001",
            plan_history=[PlanPeriod(tier=tier, interval=interval, seats=seats, effective_at=created)],
        )
        self.sub = Subscription(
            id="sub_001", customer="cus_001", status="active",
            current_period_start=created, current_period_end=created,
        )
        self.world = World(workspaces=[self.ws], subscriptions=[self.sub])
        self._ids = {}
        self.owner = self.member(owner, Role.OWNER, joined=created)

    def _id(self, prefix: str) -> str:
        counter = self._ids.setdefault(prefix, count(1))
        return f"{prefix}_{next(counter):03d}"

    # --- App database ---

    def member(self, name: str, role: Role = Role.MEMBER, joined: datetime | None = None,
               removed: datetime | None = None) -> str:
        user_id = "usr_" + name.split()[0].lower()
        email = f"{name.split()[0].lower()}@{self.ws.name.split()[0].lower()}.example"
        self.world.members.append(Member(
            id=user_id, workspace_id=self.ws.id, name=name, email=email, role=role,
            joined_at=joined or self.ws.created_at + days(1), removed_at=removed,
        ))
        return user_id

    def plan_change(self, at: datetime, tier: Tier | None = None, interval: Interval | None = None,
                    seats: int | None = None, actor: str | None = None) -> None:
        self.tier, self.interval, self.seats = tier or self.tier, interval or self.interval, seats or self.seats
        self.ws.plan_history.append(PlanPeriod(
            tier=self.tier, interval=self.interval, seats=self.seats, effective_at=at,
        ))
        self.event("seats_changed" if tier is None and interval is None else "plan_changed", at, actor,
                   tier=self.tier.value, interval=self.interval.value, seats=self.seats)

    def suspend(self, at: datetime, reason: str) -> None:
        self.ws.status, self.ws.suspension_reason = "suspended", reason
        self.event("workspace_suspended", at, reason=reason)

    # --- Stripe ---

    def charge(
        self,
        at: datetime,
        reason: str = "subscription_cycle",
        seats: int | None = None,
        tier: Tier | None = None,
        interval: Interval | None = None,
        discount_pct: int | None = None,
        credit_applied: int = 0,
        status: str = "paid",
        period_start: datetime | None = None,
    ) -> str:
        """Add an invoice and return its id. Defaults to the workspace's current plan."""
        tier, interval, seats = tier or self.tier, interval or self.interval, seats or self.seats
        subtotal = list_price(tier, interval, seats)
        discount = subtotal * (self.discount_pct if discount_pct is None else discount_pct) // 100
        net = subtotal - discount - credit_applied
        tax = int(net * self.tax_rate)
        paid = status == "paid"
        start = period_start or at
        end = add_months(start, 12 if interval is Interval.YEAR else 1)
        invoice = Invoice(
            id=self._id("in"), customer=self.ws.stripe_customer_id, billing_reason=reason, created=at,
            period_start=start, period_end=end,
            lines=[InvoiceLine(
                description=f"Quillstack {tier.value.title()} ({interval.value}ly) x {seats}",
                tier=tier, interval=interval, seats=seats, amount=subtotal,
            )],
            subtotal=subtotal, discount=discount, credit_applied=credit_applied, tax=tax if paid else 0,
            amount_paid=net + tax if paid else 0, status=status,
            charge_id=self._id("ch") if paid else None, payment_method="pm_001",
        )
        self.world.invoices.append(invoice)
        if paid and end > self.sub.current_period_end:
            self.sub.current_period_start, self.sub.current_period_end = start, end
        return invoice.id

    def monthly_history(self, last: datetime, months: int, failed: tuple[int, ...] = ()) -> str:
        """Add `months` monthly renewals ending with one at `last`; return that last invoice's id.

        `failed` lists how many months before `last` a payment failed (1 = the month before).
        """
        invoice_id = ""
        for back in range(months - 1, -1, -1):
            at = add_months(last, -back)
            reason = "subscription_create" if back == months - 1 else "subscription_cycle"
            invoice_id = self.charge(at, reason=reason, status="uncollectible" if back in failed else "paid")
        return invoice_id

    def invoice(self, invoice_id: str) -> Invoice:
        return next(i for i in self.world.invoices if i.id == invoice_id)

    def refund(self, invoice_id: str, at: datetime, amount: int | None = None, basis: str | None = None,
               reason: str = "requested_by_customer", ticket: str | None = None) -> None:
        """Add a refund. `ticket` records that it was issued to honor a promise made in that ticket."""
        inv = self.invoice(invoice_id)
        metadata = {"quillstack_basis": basis} if basis else {}
        if ticket:
            metadata["quillstack_ticket"] = ticket
        self.world.refunds.append(Refund(
            id=self._id("re"), charge_id=inv.charge_id, amount=amount or inv.amount_paid, created=at,
            reason=reason, metadata=metadata,
        ))

    def credit(self, at: datetime, amount: int, description: str) -> None:
        self.world.credits.append(CreditGrant(
            id=self._id("cbtxn"), customer=self.ws.stripe_customer_id, amount=-amount, created=at,
            description=description,
        ))

    def dispute(self, invoice_id: str, at: datetime, status: str, reason: str = "product_not_received") -> None:
        inv = self.invoice(invoice_id)
        self.world.disputes.append(Dispute(
            id=self._id("dp"), charge_id=inv.charge_id, amount=inv.amount_paid, created=at,
            reason=reason, status=status,
        ))

    def stripe_cancel(self, at: datetime) -> None:
        self.sub.status, self.sub.canceled_at = "canceled", at

    # --- Event pipeline ---

    def session(self, user_id: str, at: datetime, actions: tuple[str, ...] = ("login", "edit")) -> None:
        session_id = self._id("sess")
        for i, action in enumerate(actions):
            self.world.session_events.append(SessionEvent(
                id=self._id("se"), workspace_id=self.ws.id, user_id=user_id, session_id=session_id,
                at=at + timedelta(minutes=4 * i), action=action,
            ))

    def usage_days(self, user_id: str, start: datetime, n: int, every: int = 1) -> None:
        """One Active Session on each of `n` days, `every` days apart, starting at `start`."""
        for i in range(n):
            self.session(user_id, start + days(i * every), ("login", "view", "edit"))

    def event(self, type: str, at: datetime, actor: str | None = None, **data) -> None:
        self.world.app_events.append(AppEvent(
            id=self._id("ev"), workspace_id=self.ws.id, at=at, type=type, actor_user_id=actor, data=data,
        ))

    # --- Helpdesk ---

    def ticket(self, subject: str, opened_by: str, at: datetime, messages: list[tuple], status: str = "solved") -> str:
        """`messages` are (author, body) or (author, body, truth); author is a user id or 'support:<Name>'."""
        names = {m.id: m.name for m in self.world.members}
        built = []
        for i, (author, body, *rest) in enumerate(messages):
            is_support = author.startswith("support:")
            built.append(TicketMessage(
                author_type="support_agent" if is_support else "customer",
                author_id="agt_" + author.split(":")[1].split()[0].lower() if is_support else author,
                author_name=author.split(":")[1] if is_support else names[author],
                at=at + hours(3 * i), body=body, truth=rest[0] if rest else {},
            ))
        ticket = Ticket(id=self._id("tkt"), workspace_id=self.ws.id, subject=subject, opened_by=opened_by,
                        opened_at=at, status=status, messages=built)
        self.world.tickets.append(ticket)
        return ticket.id

    def build(self) -> World:
        w = self.world
        for records in (w.invoices, w.refunds, w.credits, w.disputes):
            records.sort(key=lambda r: r.created)
        w.session_events.sort(key=lambda r: r.at)
        w.app_events.sort(key=lambda r: r.at)
        w.tickets.sort(key=lambda r: r.opened_at)
        return w
