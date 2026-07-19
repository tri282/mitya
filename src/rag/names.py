# src/rag/names.py
"""Name matching across translation conventions (phase 3, step 4).

The corpus is Constance Garnett only, but readers know these novels through
whichever translator they happened to read. Someone who came to Dostoevsky via
Pevear & Volokhonsky types "Dunya", "Razumikhin", "Sonya"; the index was built
from "Dounia", "Razumihin", "Sonia". Without a fold the entity never resolves,
EntityMap skips expansion, and the BM25 leg goes looking for tokens that occur
nowhere in the corpus. (The dense leg absorbs some of this on its own -- it is
the sparse/exact-match leg that needs help.)

Two folds, both applied SYMMETRICALLY to the stored index and the incoming
query, both derived match keys only:

    normalize()  mechanical variance: case, diacritics, ligatures, spacing
    fold()       normalize() + transliteration-system variance

Neither ever touches the `canonical` field itself. Canonical keeps the spelling
the translation actually uses, because that is what gets shown in Sources and
fed to the LLM, and it is what query expansion appends -- expansion must append
real corpus text, never a fold key. Fold keys are not text: fold("Count") is
"kunt". That is harmless precisely because both sides are folded and the key
never surfaces.

Division of labour with entities.json: anything a rule here can derive does NOT
belong in the file. Only irregulars do -- Muishkin/Myshkin, Shigalov/Shigalyov,
Ferdishenko/Ferdyshchenko.

Design: docs/RAG.md section 4. Collision measurements: plans/mitya-rag/04-plan/
phase-3-artifacts.md step 4.
"""

import re
import unicodedata

# Expanded before NFKD, which decomposes accents but leaves these alone.
LIGATURES = {
    "æ": "ae",  # ae
    "œ": "oe",  # oe
    "ß": "ss",  # sharp s
    "ĳ": "ij",  # ij
}

_SEPARATORS = re.compile(r"[\s\-'’.,]+")

# Ordered. Order is load-bearing in three ways, all of them load-bearing enough
# to break matching if disturbed:
#   - longest-first inside a family, so `tsch` is not eaten by `tch`;
#   - rules that MANUFACTURE input for later rules must run first
#     (`ja -> ia` before `zh -> j`, or `zh` would supply a spurious `j`);
#   - rules that CONSUME input a later rule needs must run last
#     (`ff\b -> v` before the doubled-consonant collapse, which would
#      otherwise turn `ff` into `f` and strand Eropegoff/Eropegov).
TRANSLIT_RULES: list[tuple[str, str]] = [
    # -- shch: Garnett spells the same Russian letter four ways
    (r"tsch", "ch"),
    (r"stch", "shch"),
    (r"shch", "ch"),
    (r"tch", "ch"),
    # -- consonant clusters
    (r"kh", "h"),      # Razumikhin / Razumihin
    (r"ph", "f"),      # Philipovna / Filippovna
    # -- German-tradition vowels. MUST precede zh->j: zh manufactures a j, and
    #    ja/ju would then fire on it and corrupt every zh name.
    (r"ja", "ia"),     # Sonja
    (r"ju", "iu"),     # Djuna
    (r"zh", "j"),      # Rogozhin / Rogojin
    (r"w", "v"),       # Raskolnikow, Lippewechsel
    (r"x", "ks"),      # Aleksei (Katz, Avsey)
    # Garnett renders some names through French. (?!h) protects the ch digraph,
    # which the shch family above has already normalised everything into.
    (r"c(?!h)", "k"),  # Colia / Kolya, Nicolai / Nikolai
    (r"tz", "ts"),     # Ordintzeff / Ordyntsev
    # MUST precede the doubling collapse below.
    (r"ff\b", "v"),    # Eropegoff / Eropegov
    (r"ou", "u"),      # Dounia / Dunia
    # -- yod and the vowels behind it
    (r"\bye", "e"),    # Yepanchin / Epanchin
    (r"yo", "e"),      # yo: Zamyotov / Zametov. MUST precede y->i.
    (r"ya", "ia"),     # Dunya / Dunia
    (r"yu", "iu"),
    (r"y", "i"),       # Dmitry / Dmitri
    # After ya/yu have become ia/iu. Deliberately does not touch "ui", which is
    # why Muishkin stays distinct from Myshkin (handled as a curated alias) and,
    # more importantly, why the German "Luise" does not collapse into "Lise".
    (r"iu", "u"),      # yu: Ilyusha / Ilusha
    # Terminal -ie before the internal rule, which would otherwise eat it.
    (r"ie\b", "i"),    # Evgenie / Yevgeny
    (r"ie", "e"),
    (r"ei", "e"),
    (r"ee", "e"),      # Matveyevitch / Matveevich
    # Last: every rule above may have produced a new doubled pair.
    (r"([bcdfghjklmnprstvz])\1", r"\1"),
]

_COMPILED_RULES = [(re.compile(pat), rep) for pat, rep in TRANSLIT_RULES]


def normalize(name: str) -> str:
    """Mechanical fold: casefold, strip diacritics, expand ligatures, collapse separators.

    Derived match key only -- never write this back over a canonical name.
    """
    text = name.casefold()
    for ligature, expansion in LIGATURES.items():
        text = text.replace(ligature, expansion)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SEPARATORS.sub(" ", text).strip()


def fold(name: str) -> str:
    """normalize() plus transliteration-system folding.

    Collapses Garnett's conventions and the ones other translators use onto a
    shared key, so a P&V-spelled query resolves against a Garnett-built index.
    Output is a match key, not readable text -- see module docstring.
    """
    text = normalize(name)
    for pattern, replacement in _COMPILED_RULES:
        text = pattern.sub(replacement, text)
    return text


def fold_tokens(name: str) -> tuple[str, ...]:
    """Folded name as a token tuple, for boundary-respecting matching.

    Matching on raw folded strings is wrong: `liza` is a substring of
    `lizaveta`, so a substring scan resolves "Lizaveta Prokofyevna" to "Liza".
    Callers should compare token sequences, not characters.
    """
    return tuple(fold(name).split())
