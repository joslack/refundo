---
marp: true
title: Refund agent for Quillstack billing support
paginate: true
style: |
  section {
    background: #F7F6F2;
    color: #17212B;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    font-size: 30px;
    line-height: 1.45;
    padding: 70px 90px;
  }
  h1 { font-size: 64px; line-height: 1.1; font-weight: 650; color: #17212B; }
  h2 { font-size: 44px; font-weight: 650; color: #17212B; margin-bottom: 0.6em; }
  strong { color: #2F5BD9; }
  code { background: #E8E6DF; color: #17212B; border-radius: 4px; }
  table { font-size: 24px; border-collapse: collapse; }
  th { background: #17212B; color: #F7F6F2; text-align: left; }
  td, th { border: 1px solid #D6D3CA; padding: 8px 14px; }
  th:empty { display: none; }
  tr:nth-child(even) td { background: #EFEDE6; }
  blockquote { border-left: 6px solid #2F5BD9; color: #3F4B57; padding-left: 24px; }
  section::after { color: #7A858F; font-size: 18px; }
  section.statement { background: #17212B; color: #F4F2EC; justify-content: center; }
  section.statement h1 { color: #F4F2EC; font-size: 68px; }
  section.statement strong { color: #8FB0FF; }
  section.statement p { color: #C9D2DA; }
  section.todo { background: #FFF6DB; }
  .small { font-size: 22px; color: #5F6B76; }
---

<!-- _class: statement -->
<!-- _paginate: false -->

# Refund agent for Quillstack billing support

Design, evaluation, and path to production

Jonah Slack

<!--
Thanks for the time. I'm going to walk you through a refund agent I built for Quillstack: the problem, the design, how I measured it, and what it would take to put it in production. By the end you should have what you need to decide whether to keep going.
-->

---

## Why I chose refunds

- Requests arrive as free text from customers. The input is unstructured and can contain prompt injection.
- Deciding a request means searching the customer's records and applying a written policy.
- Refunds move money, so a human approves each one, at least to start.

<!--
I'm building a refund agent for a software-as-a-service business. I chose it because it exercises a lot of the interesting problems in agents.

There's a real person making a request in their own words, which is unstructured and also an injection surface. The model has to decide whether that request is valid under a refund policy, and to do that it has to search customer data and reason over what it finds.

And there's real money involved, so you'd expect a human to approve the release of funds, with the long-term aim of letting the agent handle the easy cases on its own and escalate when it has to.
-->

---

<!-- _class: statement -->

# My working assumption: the hard part is finding the right facts and determining which parts of the policy apply.

<!--
The first thing I need is a world that's hard enough to be worth solving. My guess is the hard part is finding the right facts across messy records, and working out which parts of the policy apply to them.
-->

---

## The test world

| | |
|---|---|
| **Policy** | 10 sections, grounded in published policies from Zoho, Atlassian, Slack and Microsoft, plus internal rules |
| **Records** | Members, invoices, refunds, disputes, sessions, app events, support tickets |
| **Scenarios** | 80 cases covering the policy's decision rules and their boundaries. The rules for how replies are written are not scored yet |
| **Oracle** | Code that reads the records and computes the correct answer |

Claude Code drafted the policy and wrote the scenarios and the oracle. I chose the problem, set the scoring and made the rulings. I have not read all of the world's code; I checked it by labeling cases by hand.

<!--
I built that world with Claude Code. I had it research real billing policies from companies like Zoho, Atlassian and Slack, and draft a policy grounded in them. We added some internal rules too, like a goodwill refund whose conditions you wouldn't want to show customers.

Then I had it write about eighty scenarios that exercise the policy, and an oracle that computes the correct answer for each one.

I want to be clear about this. I didn't write the policy and I didn't write the scenarios, and I haven't read all of that code. My part was choosing the problem, deciding how to score it, and ruling on the places where the policy was ambiguous.
-->

---

<!-- _class: statement -->

# The policy, the scenarios and the oracle have the same author.

They agree with each other. That shows they are consistent. It does not show they are correct.

<!--
That leaves a problem. The policy, the scenarios and the oracle all come from the same author, so of course they agree with each other. That means they're consistent. It doesn't mean they're right, and it doesn't mean I understand the policy.

That's a situation I'd expect on a real engagement. I'd arrive at a customer with a policy someone else wrote, and I wouldn't know what it implies. The way to find out is to take cases and decide them myself.
-->

---

## Labeling the cases by hand

- I see the **request** and the **records**. I don't see the oracle's answer.
- I record my **decision**: action, amount, governing section.
- I record my **evidence**: the records it rests on, and the sources I had to check.

Each disagreement with the oracle is my mistake, an oracle bug, or a gap in the policy.

<span class="small">[screenshot or short clip of the labeling UI]</span>

<!--
So that's what I'm doing. For each case I see the customer's request and the records. I record my decision and the evidence it rests on, and I don't see the oracle's answer.

Where I disagree with it, either I made a mistake, there's a bug in the oracle, or there's a hole in the policy. I want to find as many of those as I can, so this environment is realistic and reviewed by a human, without my having built every piece by hand.
-->

---

## What labeling found

| | |
|---|---|
| Cases labeled | 41 of 80: 32 blind, then 9 chosen so each kind of outcome was checked |
| First pass, same action and amount as the answer key | 30 of 41 |
| First pass, sections also complete | 23 of 41 |
| My misses on the outcome | 7 |
| Gaps in the policy's wording | 4 |
| Incomplete citations, or the form's wording | 7 |
| Oracle bugs found by labeling | 1 |
| Median time per case | 2 minutes 45 seconds |

After the rulings and 18 corrected labels, all 41 agree. The first-pass rows are measured against the answer key as it is now.

<!--
I labeled 41 of the 80 cases. The first 32 were blind. The last nine I chose so that each kind of outcome the oracle can produce had been checked by a person. Of 31 kinds, 26 have a hand-labeled case. Four were settled by a ruling, and one case was written after I stopped labeling.

Measured against the answer key as it is now, 30 of my first-pass labels had the right action and amount, and 23 also named every section the outcome rests on. I corrected 18. Seven were my own misses on the outcome, all on hard cases. Four were places where the policy's wording was ambiguous or silent. Seven were citations: mostly a monthly denial where I named one of the two sections it rests on. Separately, labeling found one bug in the oracle, where my label was right.

All 41 agree now, but that is weaker evidence, because the labels and the oracle were adjusted to each other.

The time per case matters later: it's what a human reviewer costs, and that decides which cases are worth automating.
-->

---

## Three examples from labeling

| | |
|---|---|
| My miss | The customer had eight invoices, so I counted eight months and granted a goodwill refund. One payment had failed, so only four charges in a row succeeded. |
| A gap in the policy | It said to prorate "for unused days". I read that as days without usage. It now says "days remaining in the Billing Period". |
| An oracle bug | A customer was charged twice after cancelling. The oracle refunded one charge. Both were billing errors. |

<span class="small">[screenshot of one of these in the labeling UI]</span>

<!--
The first one is the reason I think this world is hard. I know the policy, I was being careful, and I counted rows without reading their status. I'd expect an agent to do the same.

Two more misses worth mentioning if asked: a request that arrived 14 days and one hour after an annual renewal, and a dispute marked "lost", which in the payment processor's terms means the customer won.
-->

---

<!-- _class: todo -->

## What a second review found

After labeling, I had the policy, the oracle and the labels reviewed again from the policy text alone.

| | |
|---|---|
| Oracle bugs that 243 passing tests and 41 hand labels had not caught | 6 |
| Policy sections the oracle did not implement | 3 |
| Labels counted as agreeing without naming every section the outcome rests on | 7 |

Each bug is now a test written from the policy text. The policy and the oracle cover the same rules. Section scoring is strict.

<span class="small">TODO: say who or what did the review.</span>

<!--
The same-author problem came back. The tests compared the oracle with the scenarios, and both had one author, so they passed. My labels covered half the cases, and none of those cases contained these situations.

The six bugs: an action before the login counted as usage; cancellation authority was read from a person's role today, not when they asked; a refund on a different charge counted as paying a written promise; a promise in a ticket hid a phone promise that should have escalated; the seat overcharge was wrong when a fixed credit was applied; and goodwill was granted after a switch to annual.

Trials, seat changes and legacy plans were in the policy but not in the oracle. I cut them from the policy instead of building them. The world is already complicated enough, and the policy and the answer key now cover the same rules.

What I take from it: agreement between things that share an author is weak evidence, and a second reader working from the policy text finds what the first one cannot.
-->

---

## The agent

**Intake → Authorize → Investigate → Decide → Draft → Approve → Execute**

- **Authorize:** if the requester isn't the Owner or a Billing Admin, take no action and disclose nothing.
- **Investigate:** look up records with tools, based on what the customer claims.
- **Approve:** a human reviews the proposal before any money moves.
- **Execute:** the refund step can be retried without refunding twice.

<span class="small">Draft: the agent isn't built yet. Update once it is.</span>

<!--
Walk the flow left to right. Stress that approval and execution are separate steps, so a restart after approval can't issue a second refund.
-->

---

## Human approval

- The autonomy threshold starts at **$0**, so a human reviews every proposal.
- It is raised for one range of refund amounts at a time, when this holds for that range:

> error rate × average loss per error **<** reviewer cost per case

- Never automated: escalations, unauthorized requesters, suspected injection.
- The policy's $500 escalation cap stays fixed. It is a business rule, separate from the autonomy threshold.

<!--
Two different thresholds. The $500 cap is a business rule and doesn't move with the model. The autonomy threshold is an operating setting that starts at zero and rises only on production evidence: how often reviewers override the agent in that band.
-->

---

## Four designs compared

| | The model does | Code does |
|---|---|---|
| **1. Simple tools** | Search, policy reasoning, arithmetic | Returns raw records |
| **2. Plus code** | Search, policy reasoning | Arithmetic |
| **3. Plus structured tools** | Policy reasoning | Aggregation: usage since a date, hours between events |
| **4. Plus a rules engine** | Turns messy records into facts | Applies the policy |

Same harness, same model, same approval step. Only the tools change.

<!--
This is the experiment. Each rung moves one responsibility from the model to code, so the results show what each step buys.

An oracle baseline sits beside the ladder: the rules engine given perfect facts. The gap between rung 4 and that baseline is exactly what extraction errors cost.
-->

---

## Trade-offs

| Decision | Chose | Over | Why |
|---|---|---|---|
| Orchestration | [LangGraph workflow with an agent step] | A single Deep Agent | Each step can be evaluated on its own |
| Test world | Synthetic, grounded in real policies | One real policy | Real policies are vague on purpose; no single right answer |
| Ground truth | Hand labels checked against an oracle | Oracle labels alone | The oracle shares its author's blind spots |
| Labeling tool | A small custom UI | LangSmith annotation queues | Queues label traces; this labels world state against policy |
| Escalation cap | Fixed at $500 | Scaling with agent quality | Moving it would rewrite the labels |

<span class="small">First row is a draft until the agent is built.</span>

<!--
The labeling-tool row is also a friction log item.
-->

---

## Primary metric: dollars lost

| Outcome | Cost |
|---|---|
| Correct refund, denial or escalation | $0 |
| Wrongful denial | Refund value + $15 dispute fee |
| Wrongful refund | Refund value |
| Wrong amount | The difference |
| Unneeded escalation | [ESCALATION_COST] |

Reported separately, never in dollars: **unsafe actions** (acting on an injection, or for an unauthorized requester).

<!--
Dollars lost is the number a stakeholder can act on. Always refunding loses money; always denying breaks the policy and invites disputes. The escalation cost is what stops "escalate everything" from scoring perfectly.

Second metric: good faith. Did the agent cite the right section, and how often did it deny someone who deserved a refund?
-->

---

## The dataset

- **80 scenarios**: 11 easy, 23 medium, 46 hard.
- **Minimal pairs:** two cases identical except for one fact, with different answers. For example, a request one minute inside a deadline and one minute outside it.
- **Claims with evidence:** each case lists the facts a correct decision depends on and the records that support them.

This lets me score an agent on its answer and on whether it looked at the right records.

<!--
Give one pair out loud: a teammate who logged in and only viewed, versus one who exported once. Same world otherwise. One is a full refund, the other a denial.

With 80 cases, one case is 1.25 points. Differences of two or three cases are noise.
-->

---

<!-- _class: todo -->

## Results

| Design | Model | Dollars lost | Correct decisions | Unsafe actions |
|---|---|---|---|---|
| 1. Simple tools | [__] | [__] | [__] | [__] |
| 2. Plus code | [__] | [__] | [__] | [__] |
| 3. Plus structured tools | [__] | [__] | [__] | [__] |
| 4. Plus a rules engine | [__] | [__] | [__] | [__] |
| Oracle baseline | none | [__] | [__] | [__] |

<span class="small">TODO after the LangSmith experiments. Add results by difficulty tier and pass^k.</span>

<!--
Show the experiments live in LangSmith here. Then say what the numbers mean for the decision: which rung is good enough to pilot, and what the remaining errors are.
-->

---

## Monitoring after launch

- Each reviewer approval, edit or rejection is recorded as a label on the agent's proposal.
- The override rate for each range of refund amounts decides whether the autonomy threshold goes up or back down.
- A random sample of automated cases still goes to a human, so those ranges keep being measured.
- Unsafe actions raise an alert, whatever the dollar amount.

<!--
Offline evaluation justifies the launch at a zero threshold. Only production evidence justifies raising it.
-->

---

<!-- _class: statement -->

# Demo

<!--
1. One easy case end to end: request, investigation, proposal, approval, refund.
2. One hard case: a teammate's usage the requester didn't mention.
3. An injection attempt.
4. The experiment in LangSmith.
-->

---

## Path to production

| | |
|---|---|
| **State and memory** | Conversation state per thread. Anything remembered across threads is scoped to one customer. |
| **Auth and tenancy** | The requester's identity comes from the platform, never the message. Each tenant sees only its own threads and records. |
| **Failure recovery** | Approval and execution are separate steps. Before refunding, check for an existing refund tagged with the decision. |
| **Guardrails** | Records and messages are untrusted input. Escalations, unauthorized requesters and suspected injection are never automated. |

<!--
More detail: Stripe idempotency keys expire after about a day, so a reviewer approving the next morning can't rely on the key alone. And refunds should be issued as credit notes on the invoice so tax is reversed with them.

Out of scope and worth a mention: defending chargebacks.
-->

---

<!-- _class: todo -->

## Friction log

- [Top rough edge in LangGraph or LangChain]
- [Top rough edge in LangSmith]
- [One thing I'd ask the product team for]

<span class="small">TODO from the running friction log.</span>

<!--
Already known: annotation queues hold traces, not cases, so labeling ground truth needed a custom UI.
-->

---

<!-- _class: statement -->

# Recommendation

[Recommendation, in one sentence, once the results are in.]

<!--
State the recommendation, the evidence for it, the biggest remaining risk, and what the next four weeks would deliver.
-->
