# Verdict Agent & Labeling — Summary (Plain English)

This covers the third agent added to the autonomous pipeline, why it exists, how it was built, and
— separately, since it's a genuinely different topic — what the "labels" it produces are for and
why the Federated Learning (FL) system needs them at all.

No prior context needed beyond: two agents already existed before this — a Structuring Agent
(quick "is this worth investigating?" judgment) and a Money-Trail Agent (traces whether a flagged
deposit connects to others across branches, now fully deterministic and reliable). This adds a
third: the Verdict Agent.

---

## 1. What is the Verdict Agent?

It's the piece that turns *"we found a real connection"* into *"this is actually fraud."* Those are
two different statements. The Money-Trail Agent proves money genuinely moved from three branches
into one account, in a suspicious pattern — that part is now a hard, reliable fact, not a guess.
The Verdict Agent looks at that confirmed fact and asks the more human question: *does this really
prove someone did this on purpose, or could there be an innocent explanation?*

It only ever runs on cases the Money-Trail Agent has already confirmed as a real connection —
there's nothing to render a verdict on for a dead end. Its answer is simple: **GUILTY or
NOT_GUILTY, a confidence number, and a plain-English reason.**

---

## 2. Why did we build it this way?

The original plan (from before this project's build started) was for three agents to argue it out
— one arguing "guilty," one arguing "innocent," and a third weighing both sides, like a courtroom.
That's a legitimate, more thorough approach, and it's not off the table forever.

But we chose to build **one simpler agent first**, for a reason that turned out to be obvious once
we looked at the actual numbers: by this point, the system's precision was already excellent —
every real test run correctly cleared 100% of false positives, with zero innocent transactions ever
wrongly marked guilty. The three-agent courtroom exists specifically to *catch* wrongly-guilty
calls. If that isn't happening anyway, building the full courtroom right now would be real extra
work solving a problem we didn't currently have. A single, simpler agent that carefully weighs the
real evidence gets the same outcome for far less effort — and it's not a guess or a shortcut, it's
literally the simpler "baseline" version this project's own design already planned for, sitting
right next to the fuller version as a documented option to build later if it turns out to matter.

---

## 3. How did we implement it, in plain terms?

Picture the full chain now: a transaction gets flagged → the Structuring Agent gives it a quick
look → the Money-Trail Agent does the real detective work and (sometimes) confirms a real
connection → **only when it does**, the Verdict Agent gets called.

What the Verdict Agent actually sees: the real evidence (the actual amounts, dates, and accounts
involved — never anything made up), plus the earlier quick judgment as background context. It
makes one decision and explains its reasoning in a sentence or two.

One detail worth knowing: this doesn't just judge the *one* transaction that happened to trigger
it. If three deposits from three branches all connect to the same case, the Verdict Agent's single
judgment covers the *whole* case at once — all three deposits get the same verdict together, since
they're really one case, not three separate ones.

We tested this against real data: one confirmed case correctly produced a GUILTY verdict with 96%
confidence, with a sensible plain-English explanation — all without anyone triggering it by hand.

---

## 4. The labeling part — a genuinely different topic

### What is a "label," and why are we creating them?

A label is the simplest possible thing a machine-learning model needs to learn: *"here is one
example transaction, and here is the correct answer — fraud, or not."* Every model needs a pile of
these to learn from.

**Here's the real-world problem this solves:** at an actual bank, a transaction doesn't get a
trustworthy "yes, this was fraud" label until a formal investigation is fully closed — and that
can take **weeks or months**. So real banks are stuck: they want their fraud models to learn from
confirmed real cases, but confirmed real cases arrive far too slowly to keep the model current.

This is the whole point of building agents that can investigate and reach a confident verdict in
**minutes**: the moment the Verdict Agent says GUILTY with high confidence, that's a trustworthy
label — created almost instantly, instead of months later. A small, separate piece of plain code
(not another AI call — once the verdict already exists, deciding whether to act on it is
bookkeeping, not judgment) writes that label down for the specific branch involved.

### How does this actually help FL?

Quick reminder of what FL (built earlier in this project) does: every so often, each of the 3
branches nudges its own copy of the shared fraud model a little smarter using whatever training
data it currently has, then shares *only that small nudge* — never the raw data — so the 3
branches' improvements blend into one better shared model.

**The problem, until now:** every one of those nudges was based on made-up practice data. That
proved the *machinery* works — branches really can share improvements without sharing raw data —
but it wasn't teaching the model anything real, because there was no real data behind it.

**What changed:** the moment a branch has real, agent-confirmed labels sitting in its buffer, the
next FL round now automatically mixes them in alongside the practice data before training. So
instead of the model only ever practicing on invented examples, it occasionally gets to learn from
an actual real, confirmed case from its own investigations. We deliberately mix real labels *in
with* the practice data rather than replacing it — on any given round there are usually only a
handful of real confirmed cases, far fewer than a full practice batch, so blending them in gives
real signal without letting a tiny handful of examples dominate or overfit the round.

### Why does FL need this at all?

Because without it, FL is a working pipe with nothing real flowing through it. The mechanism —
practice locally, share the improvement, blend it centrally — was fully built and proven to work in
an earlier session. But a mechanism for sharing improvements is only useful if there's something
worth sharing. Made-up practice data can only ever teach the model made-up patterns. **Real,
agent-verified labels are the actual point of building all of this** — they're what let the shared
model genuinely get better at recognizing the fraud patterns actually happening across the 3
branches, instead of just exercising a mechanism that has nothing real to learn from.

---

## 5. How it all connects, end to end

```
Transaction flagged
        |
        v
[Structuring Agent] --always--> [Money-Trail Agent]
                                       |
                          convergence confirmed (real, deterministic)
                                       |
                                       v
                              [Verdict Agent]
                     "does this prove deliberate fraud?"
                                       |
                         GUILTY + confident enough
                                       |
                                       v
                          [Label Generator] (plain code)
                     writes a real label for every account
                        involved, into its own branch's
                              retrain buffer
                                       |
                                       v
                     next real FL round automatically
                    mixes these real labels in with its
                       practice data before training
```

Every step above happens with zero manual triggering — the only manual step left anywhere in this
chain is starting an FL round itself, which was always meant to be its own separate, deliberately
manual process.

---

## 6. Bottom line

- The Verdict Agent answers a genuinely different question than the Money-Trail Agent — not "did
  this connect," but "does this proven connection actually show intent."
- It was built as the simpler, single-agent version on purpose — not a shortcut, but the
  documented "baseline" this project's own design already planned for, chosen because the fuller
  three-agent debate had little left to catch right now.
- Labels exist to solve a real problem: real, trustworthy fraud confirmations normally take months
  to arrive. Agent-verified labels create the same trustworthy signal in minutes.
- FL needed this because a mechanism for sharing improvements is worthless without something real
  to share — this is the piece that gives the shared model something genuine to actually learn.
