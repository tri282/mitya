# Principal characters

Hand-written reference for the five novels in the corpus. Spellings follow the
**translations in `data/rag/*.txt`**, which vary (this text writes *Ilusha*, not
*Ilyusha*; *Lizabetha*, not *Lizaveta Prokofievna*).

**Why this exists.** The design II.4 review gate has to catch *under*-extraction —
a real character the teacher missed — and no generated report can flag an absence.
`entities.review.md` ranks what *is* there; this is the list to check it against.
It is also the ground truth for P7 eval queries.

Not exhaustive: principals and the recurring supporting cast, not every named
servant. Aliases listed are the ones the text actually uses.

---

## Crime and Punishment

| Character | Known as | Role |
|---|---|---|
| Rodion Romanovich Raskolnikov | Rodya, Rodka | Ex-student; murders the pawnbroker |
| Sofya Semyonovna Marmeladova | Sonia | Prostituted to keep her family; his confessor |
| Avdotya Romanovna Raskolnikova | Dounia | His sister |
| Pulcheria Alexandrovna Raskolnikova | — | His mother |
| Dmitri Prokofitch Razumihin | — | His friend |
| Porfiry Petrovitch | — | Examining magistrate |
| Arkady Ivanovitch Svidrigaïlov | — | Dounia's former employer |
| Pyotr Petrovitch Luzhin | — | Dounia's suitor |
| Semyon Zaharovitch Marmeladov | — | Drunken clerk; Sonia's father |
| Katerina Ivanovna Marmeladova | — | His consumptive wife |
| Alyona Ivanovna | — | Pawnbroker; the murder victim |
| Lizaveta Ivanovna | — | Her half-sister; killed as a witness |

## The Brothers Karamazov

| Character | Known as | Role |
|---|---|---|
| Fyodor Pavlovitch Karamazov | — | The father; murdered |
| Dmitri Fyodorovitch Karamazov | Mitya | Eldest son; accused of the murder |
| Ivan Fyodorovitch Karamazov | — | Middle son; the intellectual |
| Alexey Fyodorovitch Karamazov | Alyosha | Youngest son; novice |
| Pavel Fyodorovitch Smerdyakov | — | Servant; Fyodor's presumed bastard |
| Father Zossima | the Elder | Alyosha's monastic elder |
| Agrafena Alexandrovna Svetlova | Grushenka, Grusha | Contested between father and son |
| Katerina Ivanovna Verkhovtseva | Katya | Dmitri's betrothed |
| Ilyusha Snegiryov | Ilusha | The boy; his death closes the novel |
| Kolya Krassotkin | — | Schoolboy who befriends him |
| Ippolit Kirillovitch | — | Prosecutor |

> `Katerina Ivanovna` and `Lizaveta` also name *different* people in other novels.
> This is why the entity map keys on `(name, book)` — see `RAG.md` section 4.

## The Idiot

| Character | Known as | Role |
|---|---|---|
| Prince Lev Nikolayevich Myshkin | Muishkin, the Prince | Protagonist; epileptic, guileless |
| Nastasia Philipovna Barashkova | — | Kept woman; the novel's centre of gravity |
| Parfyon Semyonovitch Rogojin | — | Merchant's son; obsessed with her |
| Aglaya Ivanovna Epanchina | — | Youngest Epanchin daughter |
| Ivan Fyodorovitch Epanchin | the General | Her father |
| Elizabetha Prokofievna Epanchina | Lizabetha Prokofievna | Her mother; Myshkin's distant relation |
| Gavrila Ardalionovitch Ivolgin | Gania | Seeks Nastasia's hand for money |
| Ardalion Alexandrovitch Ivolgin | the General | Gania's father; fabulist |
| Lukian Timofeyovitch Lebedeff | — | Clerk, schemer |
| Hippolyte Terentieff | — | Consumptive youth |

## The Possessed

| Character | Known as | Role |
|---|---|---|
| Nikolay Vsyevolodovitch Stavrogin | Nicolas | Protagonist |
| Pyotr Stepanovitch Verhovensky | Petrusha | Revolutionary organiser |
| Stepan Trofimovitch Verhovensky | — | His father; liberal of the 1840s |
| Varvara Petrovna Stavrogina | — | Stavrogin's mother; Stepan's patroness |
| Ivan Pavlovitch Shatov | — | Ex-radical turned believer; murdered |
| Alexey Nilitch Kirillov | — | Engineer; theorises suicide |
| Marya Timofyevna Lebyadkina | — | Stavrogin's secret, disabled wife |
| Lizaveta Nikolaevna Drozdova | Liza | Loves Stavrogin |
| Andrey Antonovitch von Lembke | — | Provincial governor |

## Notes from the Underground

| Character | Known as | Role |
|---|---|---|
| The Underground Man | — | Unnamed narrator; retired collegiate assessor |
| Liza | — | Prostitute he alternately saves and humiliates |
| Zverkov | — | Former schoolfellow, now an officer |
| Simonov, Ferfitchkin, Trudolyubov | — | The dinner party |
| Apollon | — | His servant |

> Part I has no characters at all — it is the narrator alone. Retrieval over
> `Notes|1|*` returns argument, not scene.
