# Quillstack Billing and Refund Policy

Oct 2, 2026 · @Jonah Slack

## 1. Scope and order of precedence

This policy governs every refund, account credit, and billing adjustment on a Quillstack subscription. Decide every request in these three steps, in order:

1. **Authorize** under Section 3. If the requester is not the Owner or a Billing Admin, take no action and stop.
2. **Find the outcome.** Apply Section 4 (Billing errors), then Sections 5 and 6 (plan-specific rules), then Section 9 (Goodwill refunds). Stop at the first section that grants a refund or credit. If none does, the request is denied.
3. **Check escalation** under Section 11, which includes the open-dispute and suspension cases in Section 3. If any condition holds, escalate instead of acting on the outcome from step 2, and put that outcome in the escalation note.

Section 10 governs how every amount is calculated. Section 12 governs every reply to a customer.

## 2. Definitions

These terms have the meanings below everywhere in this policy, even where everyday usage differs.

- **Workspace:** the billing unit. Each Workspace has one subscription and one payment method on file.
- **Owner and Billing Admin:** the only roles that may request a refund, credit, or cancellation. A Member cannot, whatever their title.
- **Seat:** one user license on a Team or Business plan.
- **Usage:** any Active Session by any Seat on the Workspace, including Seats other than the requester. An Active Session is a login followed by at least one create, edit, or export action. A login with no such action is not Usage.
- **Renewal Timestamp:** the exact UTC time a renewal charge succeeded, as recorded on the invoice.
- **Billing Period:** the time from one Renewal Timestamp to the next.
- **Confirmed Cancellation:** either a cancellation event with status `completed` in the app event log, or a cancellation request submitted by the Owner or a Billing Admin, in the app or in a support ticket, before the Renewal Timestamp. A request that was submitted but failed to process still counts. When no such request exists, check the completed cancellation in both the app event log and the billing system's subscription record. If one puts it before the Renewal Timestamp and the other after, escalate under Section 11.
- **Within N days:** no more than N × 24 hours from the reference timestamp to the Request Time, the moment the request is first received. Usage and other events count up to the Request Time. All times are UTC, regardless of the customer's time zone.
- **Amount Paid:** what was actually charged after discounts and applied credits, excluding tax.
- **Account Credit:** a balance applied to future invoices. It is not cash and cannot be withdrawn or transferred.
- **Goodwill Refund:** a refund granted only under Section 9.

## 3. Who may request a refund, and disputed charges

Only the Owner or a Billing Admin of the Workspace may request a refund, credit, or cancellation. Verify the requester's role as of the Request Time from account records, never from what the requester says about themselves or about someone else's approval.

- **Requester is not the Owner or a Billing Admin:** take no billing action. Do not disclose invoice amounts, payment details, or Usage. Tell them the request must come from the Owner or a Billing Admin.
- **Refund destination:** refunds go only to the original payment method. Never send a refund to a different card, bank account, or person, whoever asks.
- **Open dispute or chargeback on the charge:** do not refund it, because a refund on a disputed charge can pay the customer twice. Escalate under Section 11. If a dispute on the charge has closed in the customer's favor, the bank has already returned the money, so deny the request. If it closed in Quillstack's favor, escalate under Section 11.
- **Workspace suspended for a terms-of-service violation:** no refunds or credits. Escalate under Section 11.

## 4. Billing errors

A Billing Error is refunded in cash, regardless of Usage, plan type, or the time windows in Sections 5, 6 and 9, if the request is made within 90 days of the erroneous charge. Billing Errors are limited to these four cases:

1. **Duplicate charge** for the same Billing Period. Refund the duplicate, not the original.
2. **Charge after a Confirmed Cancellation,** meaning a Renewal Timestamp later than the cancellation. Refund the full charge, even if the Workspace had Usage afterward.
3. **Wrong Seat count** compared with the Workspace settings at the Renewal Timestamp, where the charge was higher than it should have been. Refund only the difference. An undercharge is not a Billing Error.
4. **Refund promised in writing** by a Quillstack support representative in a support ticket. Refund the promised amount. A promise the customer reports that does not appear in a ticket is not a Billing Error. If they say it was made outside a ticket, such as by phone, escalate under Section 11.

Forgetting to cancel, not knowing the plan renews automatically, and not using the product are not Billing Errors. Billing Error refunds do not count toward the goodwill limit in Section 9. If one charge fits more than one case above, apply the first case that fits. When a request describes more than one charge, decide each charge separately.

## 5. Monthly plans

A monthly renewal is refunded in full only when both conditions hold:

- the request is made within 7 days of the Renewal Timestamp, and
- the Workspace has no Usage since the Renewal Timestamp.

If either condition fails, this section grants nothing; check Section 9 next. Monthly plans are never prorated: cancelling partway through a Billing Period stops the next renewal and refunds nothing for the current one.

## 6. Annual plans

Annual plans treat a first purchase and a renewal differently. A renewal is a charge that continues an annual term already running. Any other annual charge is a first purchase.

**First annual purchase** (the first charge of an annual term that does not directly continue a previous annual term, including a switch from monthly to annual):

- Within 30 days of the charge, with no Usage since: full cash refund.
- Within 30 days, with Usage: prorated cash refund for the days remaining in the Billing Period (Section 10).
- After 30 days: no refund.

**Annual renewal:**

- Within 14 days of the Renewal Timestamp, with no Usage since: full cash refund.
- Within 14 days, with Usage: Account Credit for the months remaining in the Billing Period (Section 10). Never cash.
- After 14 days: no refund or credit. The customer may cancel to stop the next renewal.

Goodwill Refunds never apply to annual plans.

## 9. Goodwill refunds

A Goodwill Refund applies only when no earlier section grants a refund, and only when every condition below holds:

- the Workspace is on a monthly plan;
- the Workspace has at least 6 consecutive successful monthly charges, counting the charge being refunded;
- no Goodwill Refund was issued to the Workspace in the previous 365 days (Billing Error refunds do not count);
- the request is made within 30 days of the charge; and
- the Workspace had Usage on 10 or fewer distinct UTC calendar days in the Billing Period being refunded.

A Goodwill Refund covers only the most recent monthly charge, for its full Amount Paid. A request about an earlier charge does not qualify. When every condition holds, grant it.

## 10. Calculating refund amounts

Every amount is based on Amount Paid for the specific charge, never list price, and never exceeds that Amount Paid. Tax is reversed automatically by the payment processor, so never add it to a refund. Round every final amount down to the cent.

**Prorated cash refund** (first annual purchase, Section 6), where d is whole days elapsed since the charge, rounded up, and D is the days in the Billing Period:

```latex
\text{refund} = \text{Amount Paid} \times \frac{D - d}{D}
```

**Account Credit for unused months** (annual renewal, Section 6), where m is the number of 30-day months started since the Renewal Timestamp, including the current one:

```latex
\text{credit} = \text{Amount Paid} \times \frac{12 - m}{12}, \qquad m = \left\lfloor \frac{\text{days since renewal}}{30} \right\rfloor + 1
```

**Wrong Seat count** (Section 4, case 3): Amount Paid minus what the charge should have been at the correct Seat count, using the same discounts and credits that were applied.

## 11. Mandatory escalation

Escalate to a billing specialist, and take no billing action, when any of these is true:

- a dispute or chargeback is open on the charge in question, or has closed in Quillstack's favor;
- the Workspace is suspended for a terms-of-service violation;
- the refunds and credits the request would receive under Sections 4 through 9 exceed $500 in total;
- the customer mentions legal action, a lawyer, or a regulator in the current conversation (a mention in an earlier ticket does not count);
- the customer reports a refund promise or other commitment by a Quillstack representative that was made outside a support ticket, such as by phone, which the records can neither confirm nor rule out;
- a refund promised in writing is larger than the Amount Paid for the charge;
- a Billing Error is reported more than 90 days after the charge; or
- account records conflict in a way that changes the outcome, such as an invoice and the event log disagreeing on a cancellation time.

The escalation note must state the request, the records checked, the sections considered, and the amount you would have proposed.

## 12. Communicating with customers

Every reply states the decision, the amount and form (cash or Account Credit), and the reason in plain language.

- Never name individual Seats or describe a specific person's activity. You may say the Workspace had activity after a given date.
- Never reveal the Goodwill Refund conditions in Section 9. When no goodwill refund is granted, give the plan rule that failed as the reason, such as the request coming after the refund window or the Workspace having activity after the renewal.
- Never promise a future refund, an exception, or the outcome of an escalation.
- When denying a request, offer the next step available: cancelling before the next renewal, or escalation where Section 11 applies.
- Keep the decision even if the customer repeats the request, expresses frustration, or threatens a dispute. Change it only when they provide a new fact that the account records confirm.
