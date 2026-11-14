# Privacy notice and contact details: checklist for the operator and legal counsel

PlagIQ never invents legal details. Until the fields below are set, `/privacy` shows "to be completed by the operator" in their place, with a short "this notice is being finalised" banner. `/admin` lists what is still missing.

## Fields (Railway → PlagIQ service → Variables)

| Variable | What to enter | Who decides |
|---|---|---|
| `OPERATOR_NAME` | Legal name of the person or organisation operating PlagIQ | Operator |
| `OPERATOR_ADDRESS` | Postal address for privacy requests | Operator |
| `SUPPORT_EMAIL` | Address for support and data-protection requests (also shown in the site footer) | Operator |
| `SUPPORT_TELEGRAM` | Telegram username for support, e.g. `@plagiq_support` (optional) | Operator |
| `SUPPORT_HOURS` | e.g. "Mon–Fri 9:00–18:00 (Tashkent)" (optional) | Operator |
| `PRIVACY_UPDATED` | Date the notice was last reviewed | Operator |
| `PRIVACY_DATA_LOCATION_UZ`, `_RU`, `_EN` | Where personal data is stored, in each language | **Counsel** |
| `PRIVACY_REVIEWED=1` | Set only after counsel has reviewed the whole notice; removes "awaiting confirmation" remarks and the draft banner | **Counsel** |

## Questions for counsel (moved here from the public page)

1. **Data localisation.** The website and its databases (accounts, payments, check history, reports kept for 24 hours, monitoring figures, university templates) currently run on Railway outside Uzbekistan. Law ZRU-547 ("On personal data") requires personal data of citizens of Uzbekistan to be stored in databases located in Uzbekistan. Does this apply to PlagIQ, and what must change (local hosting of the accounts database, for example) before paid launch? The approved wording goes into `PRIVACY_DATA_LOCATION_*`.
2. **Legal basis.** The notice states consent (separate for the check, web search, the AI detector and the archive) and providing the service. Please confirm or correct.
3. **Retention.** Reports are kept for 24 hours, monitoring figures 90 days (no text, no identity), accounts and check log until the user deletes them, payment records until account deletion. Please confirm these against accounting and tax obligations for paid use; deleting payment records with the account was a deliberate owner decision.
4. **Third parties.** OpenAlex (search excerpts), open-access publishers (downloads), Yandex/Brave (web search, when enabled, with consent), Pangram (AI detector, when enabled, with consent; US company), Telegram (sign-in), Payme/Click/Uzum (payments, test mode). Please confirm the descriptions in the notice.
5. **Terms of service and offer.** There are no terms of service or public offer (оферта) yet; paid use in Uzbekistan normally needs one. This is outside what PlagIQ can draft.
