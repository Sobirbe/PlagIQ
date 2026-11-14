# Uzbek loanwords across scripts and spellings

**Problem.** Russian loanwords are written differently in Uzbek Latin and Cyrillic:
- the official Latin spelling drops letters: "sentabr" for сентябрь, "obyekt" for объект, "sivilizatsiya" for цивилизация;
- writers also use other variants: "sentyabr", "rejissyor", "ob'yekt", "tsivilizatsiya".

The script folding (`textfold.fold_with_map`) already handled ц and е by position, ъ as the tutuq belgisi, and oʻ/gʻ.

**Rules added** (`textfold._loanwords`). They are applied to the *folded* text of both scripts, so they make spellings equal for matching. Neither script's official spelling is "corrected".

| Rule | Example |
|---|---|
| consonant + y + a/o/u/e/oʻ → the y is dropped | сентябрь = sentabr = sentyabr; актёр = aktyor = aktor |
| consonant + ʼ + y → the ʼ is dropped | объект = obyekt = obʼyekt |
| ts at the start of a word → s | цивилизация = sivilizatsiya = tsivilizatsiya |
| щ → shch (was sh) | щёт = shchyot |

"y" itself is not treated as a consonant, so tayyor keeps both y's. The tutuq belgisi after a vowel (taʼlim, maʼno, sanʼat) is untouched (tested).

## Measured

**Official spelling pairs** (`test_uz_loanwords.py` plus the earlier list): 42 of 49 matched before; all 56 match after, including 8 native-word controls that still match and still differ where they should (yer ≠ er, taʼlim ≠ talim, tayyor ≠ tayor).

**Effect on Uzbek vocabulary** (the 152 Uzbek texts of the evaluation library: 49,875 distinct words, 298,659 occurrences):
- 1,197 words (3,641 occurrences, 1.2%) fold differently than before.
- 68 folded forms now join words that used to be different:
  - **mostly the intended variants:** rejissor/rejissyor, sentabr/sentyabr, relef/relyef, pesa/pyesa, obyekt/obʼyekt, byuro/buro;
  - **a few foreign names:** orol/Oryol, soy/Tsoy, ila/Ilya, tan/Tyan.

  A false match still needs seven consecutive words to coincide, so a merged name pair alone cannot create one.

**Passages with the other spelling** (`evaluation/loanword_eval.py`): 80 real 40-word Uzbek passages containing at least two such loanwords, re-spelled (sentyabr → sentabr, obʼyekt → obyekt, …), placed in real held-out text and searched in the library.

| | Before | After |
|---|---|---|
| Unchanged passage | 100% | 100% |
| Loanwords re-spelled | 94.0% | **100%** |

**No regression elsewhere** (`evaluation/real_eval.py`, same library and held-out articles): every row is identical except Russian "one word in 4 replaced", which rose from 83% to 89%. That gain comes from the edited-copy candidate rule below. False marks and the held-out articles are unchanged.

**Edited-copy candidates.** Re-fingerprinting changes which three-word phrases are sampled. Measuring this showed that a short, heavily edited passage can share just one sampled phrase with its source, while two were required. Now a text is also a candidate when it shares one sampled phrase that **no other library text holds** (at most 8 such texts per search). Every candidate is still verified in full, so false marks cannot rise, and they did not. Search time was unchanged (sandbox, 20 Uzbek documents): 0.16–0.20 s median both before and after.

**Re-fingerprinting the library.** The engine re-fingerprints Uzbek texts in the background, 50 per batch ("Uzbek loanword fingerprints" in its logs): 152 texts took 12.6 s in the sandbox. A full rebuild at start-up is avoided because of the 60-second health check.
