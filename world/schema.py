"""Quillstack's platform data: the records an agent or a labeler can look up.

Each model mirrors a system a real SaaS company runs: Stripe for billing, an
app database for workspaces and members, an event pipeline for product usage
and lifecycle events, and a helpdesk for tickets. The agent never sees policy
concepts like "Usage" or "Confirmed Cancellation" here; it has to derive them
from these raw records, which is the hard part of the task.

Conventions, matching Stripe: money is integer cents in USD, and every
timestamp is timezone-aware UTC.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel


class Tier(StrEnum):
    STARTER = "starter"
    TEAM = "team"
    BUSINESS = "business"
    PRO = "pro"  # retired 2025-07-01 (policy §13); only appears in old history


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


class Role(StrEnum):
    OWNER = "owner"
    BILLING_ADMIN = "billing_admin"
    MEMBER = "member"


# --- App database ---------------------------------------------------------


class Member(BaseModel):
    id: str  # usr_...
    workspace_id: str
    name: str
    email: str
    role: Role
    joined_at: AwareDatetime
    removed_at: AwareDatetime | None = None


class PlanPeriod(BaseModel):
    """One row of a workspace's plan history; the latest row is the current plan."""

    tier: Tier
    interval: Interval
    seats: int
    effective_at: AwareDatetime
    migrated_from: Tier | None = None  # shown as "Team (migrated from Pro)"


class Workspace(BaseModel):
    id: str  # ws_...
    name: str  # customer-controlled, so an injection surface
    created_at: AwareDatetime
    status: Literal["trialing", "active", "canceled", "suspended"]
    suspension_reason: Literal["tos_violation", "nonpayment"] | None = None
    stripe_customer_id: str
    plan_history: list[PlanPeriod]


# --- Stripe ---------------------------------------------------------------


class InvoiceLine(BaseModel):
    description: str
    tier: Tier
    interval: Interval
    seats: int
    amount: int  # cents, before discounts and tax
    proration: bool = False  # true on mid-period upgrade charges


class Invoice(BaseModel):
    id: str  # in_...
    customer: str  # Stripe customer id
    billing_reason: Literal[
        "subscription_create",  # first charge, including trial conversions
        "subscription_cycle",  # a renewal; `created` is the Renewal Timestamp
        "subscription_update",  # mid-period upgrade
        "manual",
    ]
    created: AwareDatetime
    period_start: AwareDatetime
    period_end: AwareDatetime
    lines: list[InvoiceLine]
    subtotal: int
    discount: int = 0
    credit_applied: int = 0  # customer balance used on this invoice
    tax: int = 0
    amount_paid: int  # includes tax, as Stripe reports it; the policy's Amount Paid excludes it
    status: Literal["paid", "open", "void", "uncollectible"]
    charge_id: str | None  # ch_...; None if nothing was charged
    payment_method: str  # pm_...


class Subscription(BaseModel):
    id: str  # sub_...
    customer: str
    status: Literal["trialing", "active", "canceled"]
    current_period_start: AwareDatetime
    current_period_end: AwareDatetime
    canceled_at: AwareDatetime | None = None  # Stripe's own record of when the cancellation took effect


class Refund(BaseModel):
    id: str  # re_...
    charge_id: str
    amount: int
    created: AwareDatetime
    reason: Literal["duplicate", "requested_by_customer", "fraudulent"] | None = None
    metadata: dict[str, str] = {}  # e.g. {"quillstack_basis": "goodwill"}; §9 looks for past goodwill


class CreditGrant(BaseModel):
    """A Stripe customer balance transaction. Negative amounts are credit owed to the customer."""

    id: str  # cbtxn_...
    customer: str
    amount: int
    created: AwareDatetime
    description: str
    metadata: dict[str, str] = {}


class Dispute(BaseModel):
    id: str  # dp_...
    charge_id: str
    amount: int
    created: AwareDatetime
    reason: str
    status: Literal["warning_needs_response", "needs_response", "under_review", "won", "lost"]


# --- Event pipeline -------------------------------------------------------


class SessionEvent(BaseModel):
    """Raw product activity. Usage (§2) is a session with a login and at least one create, edit or export."""

    id: str
    workspace_id: str
    user_id: str
    session_id: str
    at: AwareDatetime
    action: Literal["login", "view", "create", "edit", "export"]


class AppEvent(BaseModel):
    id: str
    workspace_id: str
    at: AwareDatetime
    type: Literal[
        "trial_started",
        "trial_reminder_sent",
        "trial_converted",
        "cancellation_requested",
        "cancellation_completed",
        "cancellation_failed",
        "plan_changed",
        "seats_changed",
        "member_role_changed",
        "workspace_suspended",
    ]
    actor_user_id: str | None = None  # None for system events
    data: dict[str, str | int] = {}


# --- Helpdesk -------------------------------------------------------------


class TicketMessage(BaseModel):
    author_type: Literal["customer", "support_agent", "system"]
    author_id: str
    author_name: str
    at: AwareDatetime
    body: str  # free text; may contain a written refund promise (§4 case 5)
    # Ground truth for facts that live only in the text, e.g. {"promise_cents": 4000} or
    # {"cancellation_request": True}. Stripped before an agent or labeler sees the record.
    truth: dict[str, int | bool] = {}


class Ticket(BaseModel):
    id: str  # tkt_...
    workspace_id: str
    subject: str
    opened_by: str  # user id
    opened_at: AwareDatetime
    status: Literal["open", "pending", "solved", "closed"]
    messages: list[TicketMessage]


# --- The whole world ------------------------------------------------------


class World(BaseModel):
    """Every record for one scenario. Tools filter it the way the real APIs would."""

    workspaces: list[Workspace] = []
    members: list[Member] = []
    subscriptions: list[Subscription] = []
    invoices: list[Invoice] = []
    refunds: list[Refund] = []
    credits: list[CreditGrant] = []
    disputes: list[Dispute] = []
    session_events: list[SessionEvent] = []
    app_events: list[AppEvent] = []
    tickets: list[Ticket] = []
