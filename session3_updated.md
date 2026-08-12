# FedShieldV2 — Session 3 Updated: From Heuristic Bootstrap to a Genuinely Trained Model

This document covers the changes made *after* the original Session 3 summary — moving the
fraud-scoring model from a handwritten-rule bootstrap to an actually trained, honestly evaluated
Logistic Regression. It's written the same way as the other session docs: what changed, why, and
how, in plain English with the technical detail underneath.

---

## Part 1 — What Changed, in One Sentence

**Before:** each branch trained its own model on 2,000 made-up examples built from a rule a human
wrote ("cash + near-$10k + late-night + new-account = fraud"). It had never seen the simulator's
actual data.

**After:** one model is trained offline, once, on thousands of genuinely generated and genuinely
labeled transactions — spanning 25 different fraud scenarios (different people, hop-chain lengths,
placement amounts) and 35 different background traffic batches — with a real train/test split, and
every reported number is measured on transactions the model never trained on.

---

## Part 2 — Why This Changed

The original bootstrap model could never be trusted as evidence of genuine detection, for two
reasons raised directly in conversation:

1. **It was circular.** The model was handed almost the same rule the fraud scenario was built
   from. Catching it proved the *pipeline* worked, not that the model could find fraud it wasn't
   told about in advance.
2. **The goal explicitly asked for something better:** "let the model detect fraud genuinely,"
   using a real supervised approach (Logistic Regression), evaluated for how well it actually
   catches fraud — not just replaying a hardcoded scenario.

---

## Part 3 — How the New Model Was Built

### The training script: `branch_node/train_model.py`

This is a new, offline, host-run script (branches never train anything themselves — they just
load its output). It:

1. **Generates 25 independent fraud scenarios** — different people, different account numbers,
   different hop-chain lengths (2 through 8), and (after a fix described below) different
   placement amount ranges — plus **10 pure-background batches** of normal traffic. None of these
   are the fixed scenario used in demos.
2. **Labels every transaction from the simulator's own ground truth** — the one and only place in
   the whole system where the "answer key" file is allowed to be read, because this is an offline
   training script, not the live detection pipeline.
3. **Splits into training data and test data before looking at a single metric** (75%/25%,
   stratified so both sets have a realistic fraud rate) — every number reported below was measured
   on scenarios the model never got to learn from.
4. **Trains a real Logistic Regression** — implemented as a single PyTorch linear layer + sigmoid
   (this specific form was chosen because it's the same shape Session 5's federated learning step
   will need to average across branches later — no rework required when that lands).

### Two real data leaks were found and fixed along the way

Getting to an honest number wasn't a single clean run — two separate leaks were caught and
removed, and each is worth understanding because each is a classic, easy-to-miss ML mistake:

#### Leak #1 — the "brand new account" leak

**What happened:** every fraud-scenario account is freshly minted by the simulator (never seen
before), while every legitimate transaction uses one of 300 pre-existing customers. Feature
`account_age_days` therefore came out as **exactly 0 for 100% of fraud rows** and **≥31 for 100%
of legit rows** — a perfect, meaningless shortcut. The model learned "is this account brand new?"
instead of anything about the transaction itself.

**Why it mattered:** in production this would (a) unfairly flag every real customer who just
opened an account and made one legitimate large transfer, and (b) completely miss a smarter
launderer using an older or dormant account.

**The fix:** `account_age_days` was **removed from the model's feature set entirely** (it's still
computed and stored for evidence/reporting purposes — just never fed into the score).

#### Leak #2 — the "what time did I happen to run this script" leak

**What happened:** the fraud scenario generator anchors its fabricated timeline to `datetime.utcnow()`
— the real-world clock at the moment the script runs. Since all 25 training scenarios were
generated back-to-back in one sitting, **every single fraud transaction landed in the same narrow
real-world hour band** (roughly 6am–1pm), while legitimate traffic (generated differently, spread
over a genuine multi-day random window) was evenly spread across all 24 hours. The model latched
onto "did this happen between 6am and 1pm" as a near-perfect fraud signal — pure coincidence of
when a script happened to run, not a real pattern.

**Why it mattered:** this is worse than Leak #1 — it's not even a deliberate (if flawed) assumption
like "fraud happens at night." It's random noise from script-execution timing that got permanently
baked into the trained weights.

**The fix:** `simulator/layering_scenario.py`'s `generate_layering_scenario()` now accepts an
explicit `scenario_start` parameter. The training script passes a genuinely randomized start time
(random hour, random day) for every one of the 25 scenarios, so fraud timing is spread across the
clock exactly like legitimate traffic is.

### A third improvement made proactively: varied placement amounts

Previously every fraud scenario, in every training batch, used the same fixed $8,000–$9,800
placement range. To test whether the model generalizes to "structuring near *a* threshold" rather
than "fraud looks like exactly $8k–9.8k," six different amount ranges were introduced — from
$9,000–9,800 (textbook structuring) down to $500–3,000 (far looser, blending into ordinary
traffic) — and the final evaluation reports recall broken down by range.

---

## Part 4 — The Honest, Final Result

After both leaks were removed, here is what 6 genuinely meaningful features (amount-to-threshold
ratio, is-cash, hour, day, 10-minute velocity, transfer direction) can actually do, measured on
1,423 held-out test transactions the model never trained on:

| Metric | Value |
|---|---|
| ROC-AUC (threshold-independent) | **0.935** |
| Recall at threshold 0.5 | 87.3% |
| Precision at threshold 0.5 | 39.7% |
| Recall pushed to ~100% (threshold 0.10) | comes with a **75%+ false-positive rate** |
| **Chosen operating threshold** | **0.30** — recall 96.4%, false-positive rate 26.4% |

**Recall broken down by how close the fraud amount was to $10,000** (at threshold 0.3):
- $9,000–$9,800 (textbook structuring): **100%**
- $8,000–$9,800: **100%**
- $6,000–$8,500, $4,000–$7,000: **100%**
- $500–$3,000 (blends into ordinary traffic): **75%** — this is genuinely where detection weakens

This is a real, non-circular finding: **the model learned that amounts clustering near the $10,000
reporting threshold are suspicious, and genuinely struggles once fraud amounts move into a range
where ordinary transactions also commonly live.** That is exactly the behavior a legitimate
single-transaction structuring detector should show — not perfect, and it shouldn't be.

### Live confirmation

The trained model was deployed to all 3 branch containers and run against the actual demo
scenario. Result: **all 13 fraud accounts were flagged — 100% of the crime caught, including all
12 layering hops** the old bootstrap model completely missed — at a live false-positive rate of
~27% on background traffic, matching the offline test almost exactly. That consistency between the
lab test and live traffic is itself a good sign: the model isn't overfitting to the test set.

---

## Part 5 — The Honest Limitation That Remains

**There is no threshold that gets close to zero false negatives without an unusably high
false-positive rate**, and that's not a bug to be tuned away — it's the real ceiling of what a
single transaction's amount, timing, and direction can tell you, without connecting it to any
other transaction. Closing that gap is explicitly Session 4's job (the shared graph that traces
whether several separately-flagged transactions ultimately converge on one account), not a job for
a better-trained single-transaction score.

---

## Part 6 — Files Changed

| File | Change |
|---|---|
| `branch_node/model.py` | Rewritten: real Logistic Regression (PyTorch linear + sigmoid) loaded from trained weights, no more live bootstrap training. `account_age_days` dropped from the feature set. |
| `branch_node/train_model.py` | **New.** The offline training script described above. |
| `branch_node/consumer.py` | Updated to load the trained model (`load_trained_model()`) and use its tuned threshold, instead of bootstrap-training a heuristic model at startup. |
| `simulator/layering_scenario.py` | Added an optional `scenario_start` parameter, so training data generation can randomize fraud timing instead of always anchoring to "now." |
| `requirements.txt` | Added `scikit-learn`, `matplotlib`, `seaborn`. |
| `shared/lr_model.json` | **New.** The trained model artifact — weights, feature standardization stats, chosen threshold, and the full evaluation metrics. |
| `evaluation/visualize_model.py` | **New.** Generates all the standard evaluation plots (feature distributions, ROC curve, confusion matrix, metrics table, threshold trade-off) from this model. |
