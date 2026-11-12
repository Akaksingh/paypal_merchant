# ChakraShield — 5-minute demo video script

Target: **5:00**. Narration below is ~760 words ≈ 5 min at a normal 150 wpm. Timings are cumulative.
Left column = what's on screen, right = what you say. Everything quoted is a real value from the
running gateway — nothing here is aspirational.

---

## Before you hit record — 10-minute prep

1. **Gateway running and warm.** `python scripts/serve.py --port 8080`, then click through all four
   tabs once so every report is cached. A cold Merchant Console tab has a visible load hitch.
2. **Pre-click the scenario chips once** so the browser has them; then reload for a clean start.
3. **Browser at 100 % zoom, 1920×1080, window maximised, bookmarks bar hidden.** The console's SVG
   charts are hand-rolled and look best at full width.
4. **Dark room, one take per section.** Record sections separately and cut — don't try for one take.
5. **Have `README.md` open in a second tab** in case you want a numbers cutaway.
6. **Mute notifications.** A Slack toast over the P&L waterfall is the one thing you can't fix in post.

Recording order that works: record §3 (the demo) first while you're sharp, then the talking-head
bookends, then the B-roll tabs. Assemble last.

---

## §1 — The problem (0:00 → 0:30)

**On screen:** Title card, then the checkout page of the console.

> "In India, about two-thirds of e-commerce orders are cash on delivery — and roughly a quarter of
> them never get paid for. The parcel goes out, the buyer refuses it, and it comes back. The merchant
> eats shipping both ways, packaging, restocking, and the capital that sat in that box for ten days.
>
> The obvious fix is to build a model that predicts which orders will come back, and block those. That
> is what almost everyone builds. It's the wrong tool, and I want to show you why."

---

## §2 — The thesis (0:30 → 1:05)

**On screen:** The `Risk vs. the merchant's own indifference points` panel, or a slide with the two cost
formulas.

> "Here's the problem with a classifier. A misclassification isn't a unit error — it's a rupee amount,
> and it's a *different* rupee amount for every order.
>
> Get it wrong one way — you allowed COD and the parcel came back — you lose logistics both ways plus
> locked-up capital. Get it wrong the other way — you frictioned a buyer who would have paid — you lose
> your margin *plus* the acquisition cost you already spent on that customer.
>
> Those two numbers are different for every single order. So the break-even threshold is different for
> every single order. It's `C_FP` over `C_FN + C_FP` — we call it tau-star, and it's not a tuned
> hyperparameter. It's the merchant's own indifference point, computed per order, and we return it on
> every API response.
>
> And a block isn't the only move. A *refundable* ₹49 UPI deposit is a third option with its own, lower
> threshold. Let me show you what that does."

---

## §3 — The live demo (1:05 → 2:45) ← the most important 100 seconds

**On screen:** Checkout Simulator. Click the scenario chips in this order. Let each decision render
fully before speaking over it.

**Scenario 0 — "Metro, complete address, returning buyer"** *(~15s)*

> "Returning buyer, complete Bengaluru address. Risk is six percent, the conformal set is a singleton —
> the model is *certified* that this delivers — so: frictionless COD. Note the latency: under two
> milliseconds. This is an inline call between 'Place order' and the OMS, on a 25-millisecond budget."

**Scenario 1 — "Tier-4 PIN, thin address, Meta ad, evening"** *(~25s)*

> "Same engine, different order. Tier-4 pincode, house number and a landmark but no street, came in
> from a Meta ad at 8 pm. Risk is thirty-one percent.
>
> Now — a naive cut-off at 0.5 ships this. Even an F1-optimised cut-off at 0.32 ships this. But look at
> the prediction set: it's `{0,1}`. The model is telling you honestly that it does *not* know. So the
> decision goes to the rupee argmin, and the cheapest admissible action is the ₹49 deposit — ninety-five
> rupees expected versus ninety-seven on allow. Not a block. A deposit."

**[The money moment]** Change **CAC** in the form from `540` to `900`, resubmit. *(~25s)*

> "And here's the part a threshold can't do. Same order. Same risk score — thirty-one percent. I'm
> only telling the engine this buyer cost ₹900 to acquire instead of ₹540.
>
> Tau-star moves from 0.668 to 0.727, and the decision flips to frictionless COD. Nothing about the
> *prediction* changed. What changed is that insulting this buyer now costs more than the parcel does."

**Scenario 5 — "Tier-4, landmark-only address, 11 pm"** *(~25s)*

> "Last one. Same phone, same pincode as before — but now the address is landmark-only, no house
> number. Risk is sixty-five percent, conformal set is `{1}` — certified to come back.
>
> And the action is *prepaid*, not a deposit. The rationale on screen says why, in words: 'a deposit
> buys commitment, not deliverability. Forty-two percent of this order's risk is attributed to the
> address, so step-up residual RTO is forty-nine percent.' No amount of money makes a parcel findable —
> so a bad address escalates to prepaid on its own."

*(Read that rationale off the screen — it's generated, not written, and evaluators notice.)*

---

## §4 — Does it actually make money? (2:45 → 3:25)

**On screen:** Merchant Console tab → the profit waterfall.

> "So that's one order. Here's 3,875 of them — the chronologically last COD orders in the test split,
> fifty-four lakh of GMV, never touched during training or tuning.
>
> No engine at all: four lakh forty. A standard accuracy model at 0.5: plus one lakh twelve. The best
> globally-tuned cost cut-off — and I mean the *best*, tuned with hindsight: plus one-thirty-four.
>
> The full engine: **plus two lakh three thousand, a forty-six percent lift.** Sixty-nine thousand
> above the best binary policy.
>
> And the important bit isn't the number, it's the shape of it: we ship *fewer* RTOs — 192 against
> 302 — while losing *fewer* good buyers — 223 against 274. Both directions at once. That's not a better
> model. Both policies use the identical booster. That entire gap is the decision layer."

---

## §5 — The graph (3:25 → 3:55)

**On screen:** Ring Visualizer, click into the largest ring.

> "Asynchronously, every order folds into a typed entity graph — phones, devices, addresses, VPAs, IPs.
> Fifty burner phones on seven handsets, ninety percent return rate: that's a syndicate.
>
> But naive union-find has a failure mode that will get you fired. One college hostel, one office NAT,
> and it merges a thousand strangers into one giant 'ring' — and every innocent resident inherits its
> risk score.
>
> So edges are typed. Phones and devices merge. **IP never merges.** An address merges only until six
> distinct phones have used it, then it stops bridging and gets flagged as shared — which becomes a
> feature the model learns. The result: legitimate residents wrongly condemned drop from twenty-five to
> seven out of 581, and ring recall goes *up*, not down — because real rings still share handsets."

---

## §6 — Why this is production code (3:55 → 4:35)

**On screen:** Model Health panel, then flash the load-test table from the README.

> "Three quick things that separate this from a notebook.
>
> **One: censored labels.** Every order you friction never ships, so it never gets a label. Retrain on
> survivors and your model forgets what a bad order looks like — we reproduced that collapse, cycle
> three, calibration error hits 0.17. The fix is a five percent control band: a deterministic slice of
> *flagged* orders ships anyway, logged with its propensity, inverse-weighted at retraining.
>
> **Two: drift without labels.** Delivery labels take ten days. Conformal sets don't — every order lands
> in one, and the mix is stationary under exchangeability. In our festival-shift experiment the monitor
> fires within three hundred orders.
>
> **Three: we load-tested it.** Three hundred requests a second on one worker, zero errors — and that
> test found two real bugs. Concurrent TreeSHAP was corrupting LightGBM's native heap, and a synchronous
> pipeline was sitting inside an async handler. Both fixed. Both are in the README, because a benchmark
> that only reports the good run isn't a benchmark."

---

## §7 — Close (4:35 → 5:00)

**On screen:** CE3.0 Disputes tab, compile a packet, then back to the title card.

> "One last piece: a Visa CE 3.0 compiler that assembles chargeback evidence packets — deterministic
> hash matching, SHA-256 content addressing, and deliberately no LLM anywhere near it, because the rule
> set is mechanical and an LLM can only make it less auditable.
>
> ChakraShield: ninety-two tests, Dockerised, ONNX in the hot path, under two milliseconds at p50.
>
> It doesn't predict returns better than anyone else. AUC is 0.79 and I'll be the first to say that's
> ordinary. It just decides *what to do about them* in rupees — and that's worth two lakh on four
> thousand orders. Thank you."

---

## Numbers you can say on camera

Every figure below was read off the **live gateway** while writing this, so it will match what's on
screen. The README's results table is one run stale for three of the baseline policies — the headline
numbers are unaffected, but quote from this list, not from the README:

| Claim | Value | Where it shows |
|---|---|---|
| Full engine vs no engine | **+₹2,03,370, +46 %** | Merchant Console waterfall |
| vs best tuned global cut-off | **+₹69,084** | same |
| RTOs shipped: engine vs best binary | **192 vs 302** | same |
| Good buyers lost: engine vs best binary | **223 vs 274** | same |
| Test split | 3,875 COD orders, ₹54.1 L GMV, 21.7 % RTO | — |
| Model | AUC 0.794, ECE 0.020 → **0.014** after isotonic | Model-health panel |
| Conformal coverage at α = 0.10 | 91.8 % / 91.8 %, class-conditional | Conformal panel |
| Latency | p50 **1.9 ms**, p99 2.97 ms, 0 / 3,000 over the 25 ms budget | Latency panel |
| Throughput | **300 req/s** on one worker, zero errors | README load-test table |
| Graph guard | residents wrongly condemned **25 → 7 of 581**; recall 97.2 → **98.6 %** | Ring Visualizer |
| Tests | 91 passed, 1 skipped (the skip needs `torch`) | terminal |

Avoid claiming "92/92 passing" on camera unless you `pip install torch` first — as shipped, the
GraphSAGE test skips.

## Timing safety valve

If you run long, cut in this order:

1. **§7's CE3.0 sentence** → save 12s (just say "there's also a Visa CE3.0 dispute compiler").
2. **§6's third point** (load test) → save 18s.
3. **Scenario 0 in §3** → save 15s (open on scenario 1 instead).

Do **not** cut the CAC flip in §3 or the "fewer RTOs *and* fewer good buyers" line in §4. Those two
moments are the entire differentiator; everything else is supporting evidence.

## Lines worth memorising

These are the ones that land, so don't read them off a script:

- "A misclassification isn't a unit error, it's a rupee amount — and it's a different amount for every order."
- "Same order, same risk score. I only told it this buyer cost ₹900 to acquire."
- "A deposit buys commitment, not deliverability."
- "Fewer RTOs shipped *and* fewer good buyers lost. Both directions at once."
- "It doesn't predict returns better. It decides what to do about them in rupees."
