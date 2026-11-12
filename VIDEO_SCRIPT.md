# ChakraShield + PayPal — 3-minute demo script

Target **2:45** (hard limit 3:00; judges need not watch past it). ~390 words of narration at a calm 145 wpm.
Everything on screen is real: the gateway, the PayPal sandbox and the numbers come from the running project.
No logos, no music (or royalty-free only), nothing third-party on screen except the PayPal sandbox pages themselves.

## Before you record (10 min)
1. `.env` filled with the **US** sandbox app credentials. Start: `python scripts/serve.py --port 8080`.
   `http://127.0.0.1:8080/v1/paypal/status` must say `"mode": "sandbox"`.
2. Two windows side by side: **browser** (console at `http://127.0.0.1:8080`) and **PowerShell** (project folder, large font, 120 cols).
3. Second browser tab: PayPal developer dashboard → Testing tools → Sandbox accounts, logged in; the **US Personal** account's
   credentials at hand for the approval page. Hide bookmarks. Close Slack/email. Hide the folder name in the prompt
   (`cd` into a short path, or set `function prompt { "> " }`) so no old project names show.
4. Recorder: **Win+G** (Game Bar) or OBS, 1920×1080. Record each section as its own take; cut together.
5. Don't show: your `.env`, the client secret, personal email.

## Script

### 0:00 – 0:20 · The problem *(browser: console checkout simulator)*
> "In India, a big share of online orders are cash on delivery, and about one in four of those comes back undelivered.
> The merchant pays shipping twice and loses the sale. ChakraShield decides, in a few milliseconds, the cheapest way to
> protect each order, and now PayPal moves the money."

### 0:20 – 0:55 · The decision *(browser: click a scenario chip, show the result)*
> "At checkout, one call scores the order. A gradient-boosted model gives the chance it returns, and conformal prediction
> says how sure we are. Here the set is ambiguous, so the engine compares costs in rupees: allow cash on delivery, ask
> for a refundable forty-nine rupee deposit, or require prepayment. The cheapest option here is the deposit, and it shows
> the reason codes."

### 0:55 – 1:50 · Real PayPal money *(PowerShell, then PayPal sandbox page)*
Run: `python scripts/16_paypal_demo.py` (it opens the PayPal approval page).
> "Now the payment. The gateway creates a real PayPal order in the sandbox: forty-nine rupees, charged as fifty-eight US cents."
*(Show the approval page. Log in with the sandbox **Personal** account. Click Pay. Show the "Thank you" page.)*
> "The buyer approves. The gateway captures the payment. Every call carries a request id, so a retry can never charge twice."
*(Back to PowerShell, press Enter. Show `status CAPTURED`.)*

### 1:50 – 2:15 · Refund on delivery *(PowerShell output: step 5 and 6)*
> "When the courier reports delivery, the deposit is refunded automatically. If the parcel had come back, the deposit would
> be kept instead. The history shows created, captured, refunded."
*(Optional 5 s: run `python scripts/16_paypal_demo.py --rto` and show `deposit_retained`. Or show the refund in the sandbox dashboard.)*

### 2:15 – 2:40 · Why it is more than a demo *(browser: Merchant Console P&L tab)*
> "On our held-out test orders, this policy adds forty-six percent net margin and prevents seventy-seven percent of returns,
> while fraud rings are found with a graph and chargebacks get a deterministic evidence packet. It is merchant-ready:
> per-merchant settings, a shadow mode, and a hundred and eleven passing tests."

### 2:40 – 2:45 · Close *(title card)*
> "ChakraShield: cut cash-on-delivery losses without turning away good customers."

## Claims you may make on camera (all verified)
- Real sandbox: order created, buyer approval, capture, automatic refund. Idempotent via `PayPal-Request-Id`.
- +46 % net margin and 77 % of RTOs prevented, measured on the chronologically last 3,875 COD orders (README table).
- 111 tests pass, 1 skipped.

## Claims you must NOT make
- Webhooks and the Disputes API against real PayPal: they are implemented and tested on the offline mock only.
- "Production ready", or any INR charge: PayPal's sandbox here charges USD.

## After recording
1. Trim to **under 3:00**. 2. Upload to YouTube, visibility **Public**. 3. Paste the link into the Devpost form.
