# ChakraShield — what it is, what's in it, and how to run it

A companion to `README.md`, written for someone who just cloned the repo and needs to (a) get it
running on Windows and (b) be able to explain it out loud.

---

## 1. The one-sentence version

ChakraShield sits between **"Place order"** and the OMS. In a single synchronous call (budget 25 ms) it
decides whether this particular cash-on-delivery order should get **frictionless COD**, a **refundable ₹49
UPI deposit**, or a **prepaid-only mandate** — and it makes that choice by minimising *rupees*, not by
thresholding a probability.

The problem it attacks: COD is ~65 % of Indian e-commerce orders and roughly a quarter of them come back
(RTO). Each return burns forward + reverse logistics, packaging, restocking and locked-up capital. But
blocking a *good* buyer burns margin plus the CAC you paid to acquire them. Those two costs are different
sizes for every order, so a single global cut-off is provably the wrong tool.

---

## 2. Feature inventory

### 2.1 The decision core — why this isn't "another RTO classifier"

| Feature | What it actually does |
|---|---|
| **Instance-dependent threshold τ\*(x)** | `C_FN = logistics + holding + λ·V` vs `C_FP = M·V + κ·CAC`. The Bayes-optimal rule is `block iff p > τ* = C_FP/(C_FN+C_FP)`. τ\* is *computed per order from the merchant's own economics*, never tuned. It's returned on every API response. |
| **Soft-action threshold τ_soft(x)** | A ₹49 deposit is not a block. It loses only a fraction δ_s of good buyers and collapses residual RTO to ρ·p. Solving `E[cost\|ALLOW] = E[cost\|STEP_UP]` in closed form gives τ_soft < τ\* — the deposit becomes worth asking for long before a hard block would. |
| **Three-action expected-cost resolver** | `ALLOW_COD` / `STEP_UP_DEPOSIT` / `FORCE_PREPAID`, argmin over expected rupee cost. [`policy/resolver.py`](src/chakrashield/policy/resolver.py) |
| **Mondrian (class-conditional) conformal prediction** | Distribution-free uncertainty. Four outcomes, all actionable: `{0}` certified deliverable → ALLOW only; `{1}` certified RTO → ALLOW inadmissible; `{0,1}` ambiguous → hand to the rupee argmin; `{}` novel → neither label conforms, escalate rather than guess. Class-conditional because RTO is the minority class and a marginal guarantee can be met by covering deliverables well and RTOs badly (the repo measures exactly that failure: marginal sets cover RTOs at only 64.2 % and lose ₹34,508). |
| **Friction budget as a Lagrangian** | A merchant who says "friction at most 30 % of orders" gets a shadow price λ_f charged to every non-ALLOW action; τ\* becomes `(C_FP + λ_f)/(C_FN + C_FP)`. λ_f is chosen on *validation* and applied unchanged on test. |
| **Break-even elasticity, closed form** | `cost_stepup(α) = p·ρ_eff·C_FN + f + α·(1−p)·(C_FP − f)` is linear in abandonment α, so the critical α\*_crit where the deposit stops being cheapest is available in closed form on every response. The console gives an evaluator a 5–60 % slider to drag. |
| **Address attribution: a deposit buys commitment, not deliverability** | `ρ_eff = ρ + (1−ρ)·a(x)` with `a(x) = defect²`. A junk address resolves to *prepaid* even at moderate p, because no deposit makes a parcel findable. |

### 2.2 Model layer

- **LightGBM** RTO scorer over **36 point-in-time features** (velocity windows, PIN priors, address defects, ring stats, `entity_shared`, payment-fallback signals). Test AUC 0.794, PR-AUC 0.595.
- **Isotonic recalibration** — ECE 0.020 → **0.014**.
- **ONNX export** with a parity check: max |Δp| = 2.7 × 10⁻⁷ over 2,000 rows. ONNX Runtime, 1 intra-op thread, is what serves.
- **Cost-sensitive booster sweep** (γ ∈ {0, 0.5, 1} on `w = cost^γ`) — trained, measured, and **deliberately not served**. γ = 0 won on *validation* P&L. An honest negative result: rupee weighting buys a precision the resolver can't use, because instance costs already enter at decision time through τ\*(x).
- **Exact TreeSHAP reason codes**, computed on the native booster — and deferred: in `explain=auto` mode SHAP runs only when the decision applies friction. An ALLOW needs no defence; a step-up must carry one. That's −26 % CPU per request.

### 2.3 Subgraph abuse sentinel

- Typed entity graph over phone / device / address / VPA / card / IP, built asynchronously off the hot path.
- **The shared-entity guard is the interesting part.** Plain union-find folds one hostel, one office NAT or one dynamic IP into a single giant "syndicate" and every resident inherits its RTO rate. So edges are typed: hard IDs (phone, device, VPA, card) merge transitively; **IP never merges** (bipartite property only); an address merges only until 6 distinct phones have used it, then stops bridging, and is flagged PUBLIC/SHARED at 25 — a feature the model learns.
- Measured effect (naive → guarded): legit phones condemned **96 → 60**, hostel residents condemned **25 → 7 of 581**, ring recall **97.2 % → 98.6 %**, largest component **253 → 106**. The ceiling was swept, not guessed.
- Ring status needs corroboration: a phone/device ratio, or an address ratio *plus* a high observed RTO rate. A hostel with a normal return rate is never a ring.

### 2.4 Production failure modes it actually addresses

| Failure mode | The mechanism in this repo |
|---|---|
| **Censored labels / survivorship bias** — frictioned orders never ship, so they never earn a label | ε-greedy **control band**: a deterministic 5 % of *flagged* orders ship frictionless anyway, tagged `is_control_cohort`, logged with the served action's propensity, re-weighted 1/ε (Horvitz–Thompson, cap 20) at retraining. The replay shows survivor-only retraining collapsing in cycle 3 (train RTO 28 % → 10.5 %, ECE 0.173); the band holds worst-cycle ECE at **0.060**. |
| **Assumed buyer behaviour** — δ_s, δ_bad, ρ, δ_p are the numbers a merchant knows least | **Per-segment learner** using calibrated p as the instrument: `E[abandon\|p] = (1−p)·δ_s + p·δ_bad`, closed-form ridge, shrunk segment → global → prior. With a prior wrong by 2× (the new-merchant case) it recovers **62 %** of the gap to an oracle. |
| **Drift without labels** | Every scored order lands in one conformal set; under exchangeability the mix is stationary and known from calibration. Sliding 5-min windows raise `MODEL_EPISTEMIC_DRIFT`, `MODEL_RISK_MIX_SHIFT`, `MODEL_SCORE_PSI`. In the festival-shift experiment the monitor fires **within 300 orders** — days before any delivery label exists. |
| **Real merchant data** | CSV adapter + mapping file. A public Amazon India export (30,081 orders) ran end to end in 19 s with zero code changes. |

### 2.5 Serving, ops, and the rest

- **FastAPI gateway**: in-process p50 **1.90 ms** / p99 2.97 ms; **0 of 3,000** calls breached the 25 ms budget. Every response self-reports whether it stayed in budget.
- **HTTP load test** against a real uvicorn worker: **300 req/s at 4 clients**, zero errors. It found two genuine serving defects — concurrent LightGBM `pred_contrib` corrupting the native heap (`0xC0000374`, now serialised behind a lock) and a synchronous pipeline inside an `async def` handler (now a threadpool endpoint: 182 → 293 req/s at 16 clients).
- **Per-merchant config, API keys, shadow mode**: the same order gets τ\* = 0.668 for the demo merchant and 0.754 for a beauty D2C with a 25 % friction budget. Shadow mode runs the whole engine and returns what it *would* have done while serving every order frictionless — so a trial doubles as an unbiased calibration set at propensity 1.
- **Visa CE 3.0 dispute compiler**: deterministic 120–365-day window, two-element hash match, SHA-256 packet, printable HTML view. **No LLM anywhere near it** — the rule set is mechanical. The Mastercard 4837 difference is documented rather than glossed over.
- **Console** (vanilla JS, hand-rolled SVG): Checkout Simulator, Merchant Console, Ring Visualizer, CE3.0 Disputes.
- **Docker Compose + Redis**, **GitHub Actions CI**, **92 passing tests**.

### 2.6 Headline result

On the chronological test split (3,875 COD orders, ₹54.1 L GMV). **These are the numbers `GET /v1/report`
returns live**, i.e. what the Merchant Console actually renders — use these on camera:

| Policy | P&L | Δ vs no engine | RTOs shipped | Good buyers lost |
|---|---:|---:|---:|---:|
| `ALLOW_ALL` | ₹4,40,482 | — | 841 | 0 |
| `BASE@0.5` (the accuracy model) | ₹5,52,777 | +₹1,12,295 | 539 | 47 |
| `BASE@F1` | ₹5,64,269 | +₹1,23,787 | 488 | 74 |
| `BASE@GLOBAL_COST` (best tuned cut-off) | ₹5,74,768 | +₹1,34,286 | 302 | 274 |
| `BASE@TAU*(x)` | ₹5,62,207 | +₹1,21,726 | 506 | 70 |
| **`CHAKRA_FULL`** | **₹6,43,852** | **+₹2,03,370 (+46 %)** | **192** | **223** |
| `CHAKRA_FULL@F≤30%` | ₹5,95,145 | +₹1,54,664 | 406 | 97 |
| `ORACLE` | ₹7,79,688 | +₹3,39,206 | 50 | 0 |

`BASE` and `CHAKRA` share the same booster — `BASE@TAU*(x)` and `CHAKRA@TAU*(x)` are byte-identical rows
in the report — so the entire gap up to `CHAKRA_FULL` is the **decision layer**, not the model. Fewer
RTOs shipped *and* fewer good buyers lost than the best binary policy: both directions at once.

> ⚠️ **The README's results table is one run stale.** Its `BASE@0.5` (₹5,55,523 / 527 / 54),
> `BASE@F1` (₹5,64,531) and `BASE@TAU*(x)` (₹5,59,040) don't match the committed
> `artifacts/reports/evaluation.json` that the gateway serves. The headline figures — `ALLOW_ALL`,
> `BASE@GLOBAL_COST`, **`CHAKRA_FULL` +₹2,03,370**, `CHAKRA_FULL@F≤30%` and `ORACLE` — are identical in
> both, so the claim you'd actually make on camera is safe. Re-run `scripts/03_evaluate.py` to
> regenerate the report and the README's `<!-- RESULTS -->` block together if you want them consistent.

Model, live from the same endpoint: booster γ = 0 selected on validation P&L, 133 trees, test AUC
**0.7937**, PR-AUC **0.5948**, ECE 0.0202 → **0.0142** after isotonic, conformal coverage
0.9179 / 0.9179 at α = 0.10, ONNX parity max |Δp| **2.7 × 10⁻⁷** over 2,000 rows, empty-set rate 0 %.
Top features by gain: `device_distinct_phones` (by a factor of 2.4), `addr_defect_score`, `ring_is_ring`,
`addr_rto_rate`, `channel_risk`, `pay_switch_from_failed`.

---

## 3. Running it on this machine

### 3.1 Two gotchas that will stop you before anything runs

**(a) `python` is the Microsoft Store stub on this machine**, not a real interpreter:

```
C:\Users\Hp\AppData\Local\Microsoft\WindowsApps\python.exe     ← stub, errors out
C:\Users\Hp\AppData\Local\Programs\Python\Python313\python.exe ← the real 3.13
```

So `.\run.ps1` fails out of the box (it calls `python`). Use `py -3` to bootstrap, then work inside a
venv where `python` resolves correctly.

**(b) Git mangles the committed LightGBM models on a Windows clone.** This one is subtle and fatal.
The boosters ship as LightGBM's line-based **text** format (`artifacts/models/*.txt`). Windows Git
defaults to `core.autocrlf=true`, which rewrites every LF to CRLF on checkout — and LightGBM's parser
cannot read CRLF. The repo's `.gitattributes` protects `*.sh` and `Dockerfile` but **not** `*.txt`, so a
fresh clone here produces:

```
[LightGBM] [Fatal] Model format error, expect a tree here. met ...
```

…thousands of times, and the gateway dies during startup (exit 127) before it ever binds a port.
The `.onnx` graph is unaffected (Git treats it as binary) and the JSON sidecars are fine — it is only
the five `.txt` boosters, and the one the SHAP explainer needs is among them.

Fix it once, after cloning:

```powershell
# normalise the committed boosters back to LF
.\.venv\Scripts\python.exe -c "import pathlib; [p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n')) for p in pathlib.Path('artifacts/models').glob('*.txt')]"
```

Verify with `lgb.Booster(model_file='artifacts/models/chakra_rto.txt').num_trees()` → should print **133**.

To avoid it on the *next* clone, either clone with the conversion off:

```powershell
git -c core.autocrlf=false clone https://github.com/Akaksingh/paypal_merchant.git
```

or fix it at the source by adding one line to `.gitattributes`:

```
artifacts/models/*.txt -text
```

> Note: after normalising, `git status` will show the five `.txt` files as modified. That is expected —
> the working tree now holds LF where Git's Windows checkout filter wants CRLF. Don't commit them
> unless you're also adding the `.gitattributes` rule above.

### 3.2 Fastest path to a running demo (~3 min)

The trained models and reports are **committed** under `artifacts/`. Only the replay pickles are
gitignored, so you do **not** need to retrain — just regenerate the world.

```powershell
cd "D:\D Drive\razorpay"

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # ~6 min, ~700 MB of wheels

# fix the CRLF-mangled boosters (see 3.1b) -- once, right after cloning
.\.venv\Scripts\python.exe -c "import pathlib; [p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n')) for p in pathlib.Path('artifacts/models').glob('*.txt')]"

$env:PYTHONUTF8 = "1"          # the reports print ₹ and Δ; Windows consoles default to cp1252
.\.venv\Scripts\python.exe scripts\01_generate_data.py          # ~65 s, seed 42, 60k orders
.\.venv\Scripts\python.exe scripts\serve.py --port 8080         # ~30 s to load the world, then binds
```

Then open **http://127.0.0.1:8080**.

**Verified on this machine (Python 3.13.15, 2026-09-14):** world regenerates bit-identically to the
committed `artifacts/data/world.json` (91,166 graph nodes, 38,092 components, 60 rings, 1,168 ring
phones, largest component 106); the booster loads with 133 trees; all six demo scenarios score correctly
through the live gateway; `pytest` gives **91 passed, 1 skipped** — the skip is `test_embeddings.py`,
which needs `torch` (deliberately not in `requirements.txt`). Install torch and it's 92/92.

> Calling `.\.venv\Scripts\python.exe` directly avoids PowerShell's execution-policy prompt on
> `Activate.ps1`. If you'd rather activate:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` then `.\.venv\Scripts\Activate.ps1`,
> after which plain `python` and `.\run.ps1` work.

**Why `01_generate_data.py` is not optional:** the console's order picker, the ring visualizer and the
dispute candidates all read `artifacts/data/orders.pkl`, and the gateway warm-starts its feature store
from `store.pkl`. Both are produced by that script. Use the default arguments — seed 42, 60k orders — so
the world matches the committed model; a smaller world trains/serves a weaker one.

### 3.3 Full pipeline

With the venv activated:

```powershell
.\run.ps1          # install -> generate -> train -> evaluate -> bench -> pytest -> serve on :8080
```

Or step by step:

| Command | What it produces | Rough time |
|---|---|---|
| `python scripts/01_generate_data.py` | synthetic world + chronological replay → `artifacts/data/` | ~90 s |
| `python scripts/02_train.py` | γ candidates, validation-P&L selection, isotonic, conformal, ONNX + parity | a few min |
| `python scripts/03_evaluate.py` | policy P&L, friction frontier, sensitivity, break-even, calibration | ~1 min |
| `python scripts/04_bench_latency.py` | in-process + HTTP p50/p95/p99 by stage | ~1 min |
| `python -m pytest tests` | 92 tests | ~1 min |
| `python scripts/serve.py --port 8080` | the gateway + console | — |

### 3.4 The experiments (each writes a JSON report to `artifacts/reports/`)

```powershell
python scripts/07_graph_guard.py 6 12 25   # naive vs guarded graph, merge-ceiling sweep
python scripts/08_festival_shift.py        # domain shift: label-free monitor vs what labels confirm
python scripts/09_feedback_loop.py         # survivorship bias vs epsilon control band + IPW
python scripts/10_learn_behaviour.py       # prior vs learned vs oracle buyer response
python scripts/11_gnn_ring_embeddings.py   # GraphSAGE vs union-find  (~10 min CPU, needs torch)
python scripts/12_conformal_variants.py    # conformal alpha x conditioning sweep with resolver P&L
python scripts/13_load_test.py             # real HTTP throughput and tail latency under concurrency
python scripts/14_real_data.py             # public Amazon India export through the whole pipeline
python scripts/15_export_demo_bundle.py    # curated real responses -> self-contained offline console
```

`11_gnn_ring_embeddings.py` needs **torch**, which is *not* in `requirements.txt` (it's an experiment,
and the serving image deliberately excludes it): `pip install torch` first if you want to run it.

### 3.5 Docker

```bash
docker compose up --build     # gateway on http://127.0.0.1:8080 + Redis feature store
docker compose down -v        # also discards the generated world
```

The image ships code + committed models and **never retrains**. On first start with an empty
`chakra-data` volume the entrypoint generates a 12k-order world (~1 min), then serves. Docker Desktop is
installed on this machine, so this is the zero-Python-setup route.

### 3.6 Environment variables worth knowing

| Variable | Default | Effect |
|---|---|---|
| `REDIS_URL` | unset | Use Redis for the feature store and drift windows. Unset → in-process store with identical semantics (reported on `/healthz`). |
| `CHAKRA_EPSILON` | `0.05` | Control-band size. |
| `CHAKRA_ALPHA` | `0.10` | Conformal miscoverage. |
| `CHAKRA_REQUIRE_API_KEY` | off | Enforce `X-API-Key` against `config/merchants.json`. Off so the demo and tests run header-free. |
| `PYTHONUTF8` | — | **Set to `1` on Windows** or the ₹/Δ in report output will throw. |

### 3.7 Quick API smoke test

```powershell
curl http://127.0.0.1:8080/healthz

# score the "Tier-4 PIN, thin address, Meta ad, evening" scenario
curl -X POST "http://127.0.0.1:8080/v1/risk/evaluate?commit=false" `
  -H "content-type: application/json" `
  -d '{\"customer_phone\":\"7012349876\",\"delivery_pin\":\"845401\",\"shipping_address\":\"H.No 7, Ward 4, near Hanuman Temple\",\"cart_gmv\":1799,\"items_count\":2,\"device_fingerprint_hash\":\"fp_demo_new_02\",\"payment_method\":\"COD\",\"acquisition_channel\":\"META_ADS\",\"hour_of_day\":20,\"is_new_customer\":true,\"merchant_margin\":0.18,\"cac\":540}'
```

### 3.8 What the six built-in scenarios actually return

These are live values from the running gateway, and they're the raw material for any demo:

| # | Scenario | p(RTO) | τ\* | τ_soft | Conformal set | Decision |
|---|---|---:|---:|---:|:---:|---|
| 0 | Metro, complete address, returning buyer | 0.064 | 0.492 | 0.045 | `{0}` certified-low | **ALLOW_COD** |
| 1 | Tier-4 PIN, thin address, Meta ad, 8 pm | 0.315 | 0.668 | 0.310 | `{0,1}` ambiguous | **STEP_UP_DEPOSIT** |
| 2 | Card failed → COD, coupon, affiliate, 1 am | 0.534 | 0.707 | 0.195 | `{1}` certified-high | **STEP_UP_DEPOSIT** |
| 3 | High-CAC influencer, good address, Tier-2 | 0.175 | 0.734 | 0.281 | `{0,1}` ambiguous | **ALLOW_COD** |
| 4 | Junk address ("asdf asdf"), Tier-3, 2 am | 0.854 | 0.729 | 1.000 | `{1}` certified-high | **FORCE_PREPAID** |
| 5 | Tier-4, landmark-only address, ₹2,899, 11 pm | 0.647 | 0.703 | 0.331 | `{1}` certified-high | **FORCE_PREPAID** |

Three things worth noticing, because they're the whole argument in miniature:

- **#2 is certified-RTO at p = 0.534 and still only gets a deposit.** A hard block would cost ₹151;
  the deposit costs ₹63 expected. Certified-high forbids ALLOW — it does not mandate a block.
- **#3 has 2.7× the risk of #0 and still ships frictionless.** τ\* = 0.734 because that influencer
  buyer cost ₹800 to acquire. A global cut-off cannot express this.
- **#1 and #5 are the same phone and the same PIN.** Degrade the address from "H.No 7, Ward 4, near
  Hanuman Temple" to "Near Hanuman Temple, Ward 4" and the action escalates from deposit to prepaid —
  because a deposit buys commitment, not deliverability (`ρ_eff = ρ + (1−ρ)·defect²`).

### 3.9 The same order priced three ways

Also verified live, on scenario #1 — this is the most compelling thing you can show in 20 seconds:

| Change | τ\* | Decision |
|---|---:|---|
| demo defaults (M = 0.18, CAC = ₹540) | 0.668 | STEP_UP_DEPOSIT |
| CAC ₹540 → ₹900 | 0.727 | **ALLOW_COD** |
| margin 0.18 → 0.35 | 0.751 | **ALLOW_COD** |
| friction budget ≤ 30 % (λ = ₹40) | 0.712 | **ALLOW_COD** |

And by merchant (drop `merchant_margin`/`cac` from the request, pass `merchant_id`):

| `merchant_id` | τ\* | policy_action | served `decision` |
|---|---:|---|---|
| `demo_merchant` | 0.643 | STEP_UP_DEPOSIT | STEP_UP_DEPOSIT |
| `nykaa_style_d2c` (λ = ₹60, budget 25 %) | 0.815 | ALLOW_COD | ALLOW_COD |
| `trial_merchant` (**shadow**) | 0.534 | STEP_UP_DEPOSIT | **ALLOW_COD** ← shadow mode |

That last row is shadow mode doing its job: the engine computes and reports the intervention it *would*
have applied, while every order ships frictionless at propensity 1.

Other routes: `POST /v1/risk/outcome/{order_id}` · `GET /v1/behaviour` · `GET /v1/ledger/stats` ·
`GET /v1/monitor/drift` · `POST /v1/dispute/ce3-compile` · `GET /v1/dispute/candidates` ·
`GET /v1/graph/rings` · `GET /v1/graph/subgraph?seed=` · `GET /v1/report` · `GET /v1/merchants` ·
`GET /v1/scenarios` · `GET /healthz` · `GET /` (console).

---

## 4. Where to look in the code

```
src/chakrashield/
  config.py                 unit economics, alpha, latency budget, epsilon, paths
  policy/economics.py       tau*, tau_soft, lambda_f, elasticity        <- the thesis lives here
  policy/resolver.py        conformal set x economics -> action          <- and here
  models/conformal.py       Mondrian split conformal
  graph/syndicate.py        typed union-find + shared-entity guard
  learning/exploration.py   epsilon control band, propensity, IPW weights
  learning/response.py      per-segment buyer-response learner
  monitoring/drift.py       label-free conformal drift alarms
  runtime/scorer.py         ONNX Runtime + deferred TreeSHAP
  dispute/ce3.py            Visa CE3.0 compiler (+ the Mastercard difference)
  serving/app.py            FastAPI gateway; evaluate_pipeline() is the whole hot path in one function
  serving/static/           the console
```

If you read only two files, read [`policy/economics.py`](src/chakrashield/policy/economics.py) and
[`policy/resolver.py`](src/chakrashield/policy/resolver.py). Everything else is in service of those.
