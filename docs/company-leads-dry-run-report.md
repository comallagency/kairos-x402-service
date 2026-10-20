# POST /company-leads — dry-run report (2026-10-20)

Referenced from `app/config.py`, `app/company_leads_worker.py` and
`tests/test_company_leads.py` — consolidated here so those comments point
at something real, not a dangling filename.

## Actor selection

First candidate tested: `coladeu/apollo-organizations-scraper` (company-level
data). Rejected after a real test run on our own Apify account showed:
- No `size` or `email` field at all in its output — structurally a company
  directory, not individual contacts.
- 33.7% FAILED + 5.4% TIMED-OUT over the last 30 days of real runs
  (confirmed via the Apify API's own `stats.publicActorRunStats30Days`).

Chosen instead: `b2bsearch/b2b-leads-finder` (API id
`b2bsearch~b2b-leads-finder`), person-level leads (a lead = a contact, with
their current company's name/domain/size attached — the usual meaning of
"lead" in B2B sales). Verified 99.8% success (2991/2996 runs, 30 days).

## Cost (measured directly on our own Apify account, FREE tier)

Pricing is tiered by the caller's own Apify plan (`eventTieredPricingUsd`):
FREE tier is $0.003 per lead delivered with a live email, plus a one-time
$0.0002 actor-start fee per run. Leads without a reachable email are never
charged (confirmed: a run with an invalid filter combination that delivered
zero leads cost exactly the $0.0002 start fee, nothing more).

25 leads: 25 × $0.003 + $0.0002 = $0.0752. Sale price set at $0.30 flat per
job (~4x that cost).

## Data provenance

Per the actor's own public README (read via the Apify API, not guessed
from marketing copy): "a rolling-update proprietary B2B database;
employer-to-email matching is performed by comparing company name and
email domain." Emails are looked up from an existing database and matched
to the right current employer — never pattern-guessed
(no `firstname.lastname@domain` generation). The vendor does not publish
exact sourcing; this is disclosed as uncertain rather than overclaimed.

## Personal vs. work email ratio (quality control)

A real 20-lead test run (`countries: ["fr"], industries: ["Software
Development"], cities: ["Paris"], emailType: "any"`) returned:

| emailType | count | % |
|---|---|---|
| work | 11 | 55% |
| personal | 9 | 45% |

45% personal-mailbox addresses (gmail.com, yahoo.fr, hotmail.com, free.fr,
sfr.fr) is not usable as a professional sales lead — a quality problem the
buyer would be paying for.

**Fix**: the actor's own `emailType` input accepts `"work"` as a filter
value (current-employer address only, "fewer leads" per its own
description). A real follow-up run with `emailType: "work"` (10 leads
requested) returned **10/10 work-domain emails, 0 personal** — confirmed
again on a final 3-lead run through the full route/worker pipeline
(Diduenjoy, Syspertec Group, Shippeo — all three emails matched their
company's own domain). `app/company_leads_worker.py`'s
`build_actor_input()` now forces `emailType: "work"` unconditionally.

## "Malformed Diduenjoy line"

A real captured record (Diduenjoy / Alexandre Lacreuse) had `jobTitle: "VP
of Sales 🇪🇺🇨🇭🇺🇸"` — a real regional-indicator emoji flag sequence the
person added to their own LinkedIn headline. This is real, unaltered
self-reported data, not corruption. It looked garbled only when echoed
through a Windows-console/WSL pipe during manual investigation (terminal
encoding), not because of any bug in `shape_lead()` or the JSON response —
confirmed by `test_shape_lead_preserves_emoji_in_job_title` in
`tests/test_company_leads.py`, which round-trips the exact string through
real JSON serialization and asserts it comes back unchanged.

## Known limitation (shared with the existing `/jobs` route)

Payment is captured at job creation, not on success — matching `/jobs`'s
existing precedent. If the rare (~0.2%) Apify run failure happens after
payment, the buyer has already paid. Not a regression introduced by this
route; a pre-existing trade-off in the job-polling pattern this route
reuses.
