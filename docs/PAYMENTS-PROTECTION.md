# Payment protection: charges, refunds and credit restoration

Payments are in **test mode** and are switched off on the live site (`/health`: `"payments": {"enabled": false}`). Nothing here activates real payments. Turning on real money needs the owner's decision, production merchant contracts, an offer/terms (see LEGAL-CHECKLIST.md) and the checklist at the end.

## How a paid check is protected today (all covered by `test_payments.py`, `test_report_history.py`)

| Situation | What happens | Code |
|---|---|---|
| Balance too low | Check refused before anything runs; nothing charged | `payments.charge` (atomic: `BEGIN IMMEDIATE`) |
| Check raises an error (file unreadable, engine down, PlagIQ error) | Full price refunded at once | `app._run_check` |
| Search failed / nothing comparable, no percentage | Plagiarism part refunded; `charged_uzs` shows 0 | `unused_check_price` |
| AI detector did not run (Uzbek, too long, provider error) | AI part refunded | `unused_ai_price` |
| Server restarted during a check | Check marked "interrupted", charge refunded at start-up, once | `reports_store.interrupted`, `recover_interrupted` |
| Incomplete coverage ("at least X%") | Charged: sources were compared; shown on page, PDF, certificate | `checkstatus` |
| Refund called twice or overlapping | **Capped at the charge** (new): repeated refunds give 0, overlapping ones only the remainder; another user's charge cannot be refunded | `payments.refund` |
| Top-up confirmed twice by a provider | Credited once (provider transaction id is unique) | Payme/Click/Uzum handlers |
| Provider cancels or reverses a top-up | Reversed only while unspent; spent top-ups refused with the provider's error code | `_reverse`, Payme `CancelTransaction`, Uzum `reverse` |
| Payment still in progress | Account deletion refused, so a provider never confirms money for a deleted account | `payments.in_progress` |

Every movement is a ledger row (`topup`, `charge`, `refund`, `reversal`) with a reference, so a balance is always the sum of its ledger.

## Not built yet (proposed; needs the owner's decision)

1. **Owner credit restoration.** A form on `/admin` to give a user credit with a mandatory reason (for example a complaint upheld by hand), written to the ledger as `kind='credit'` with the owner's ID. Today this needs a database edit.
2. **Daily reconciliation.** Compare provider statements (Payme `GetStatement`, Click and Uzum reports) with the ledger, and list differences on `/admin`.
3. **Refund to card.** Today refunds restore the PlagIQ balance only. Returning money to a card is done in the provider's cabinet and recorded here by hand. Automating it needs each provider's refund API and contract.
4. **User-visible statement.** Users see their recent ledger in the account menu; a downloadable statement (CSV/PDF) is not built.
5. **Monitoring.** `/admin` shows charged amounts; it does not yet alert on refund spikes or failed provider callbacks.

## Before real payments (checklist)

- [ ] Offer/terms of service approved by counsel; refund policy text published.
- [ ] Production merchant IDs and keys from Payme, Click and Uzum; fiscal (IKPU/package) codes; test in their production sandbox.
- [ ] `PAYMENTS_MODE` changed from `test` only after the above; one real small payment and refund done by the owner.
- [ ] Backups of the accounts database on a schedule (payments live there).
- [ ] Decide on items 1–2 above.
