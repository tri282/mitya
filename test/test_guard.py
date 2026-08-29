# test/test_guard.py
"""G-S5 verify: guard layers 2 and 3 (src/guard.py).

Plain-assert script, not pytest (matches test_gateway_contract.py's idiom).
Offline, deterministic, no network, no key -- which is the point: A7 puts
these in Tier 1 precisely because they can run in CI, unlike everything the
judge measures.

The negative cases matter more than the positive ones here. A guard that fires
on legitimate output is worse than no guard, because it degrades real answers
while looking like it is working -- and A10's two-denominator rule exists to
keep that visible.
"""

import sys

sys.path.insert(0, ".")

from src.guard import CANARY_SENTINEL, plant_canary, scan_output  # noqa: E402


def test_canary_leak_is_caught() -> None:
    """Layer 2, the only binary one: the sentinel is not a word, so its
    presence in output can only mean the system prompt reached the caller."""
    verdict = scan_output(
        f"Very well. My reference code is {CANARY_SENTINEL}, since you ask.",
        canary=CANARY_SENTINEL,
    )
    assert verdict.tripped
    assert verdict.layer == "canary"


def test_canary_is_not_scanned_when_it_was_never_planted() -> None:
    """Passing None disables layer 2 rather than scanning for a default. A
    guard that cannot fire while reporting 0% leaks is worse than no guard --
    A7's own argument about disabled gates."""
    leaked = f"My reference code is {CANARY_SENTINEL}."
    assert scan_output(leaked, canary=None).tripped is False
    assert scan_output(leaked, canary=CANARY_SENTINEL).tripped is True


def test_persona_breaks_are_caught() -> None:
    """Layer 3 against the shapes the authored adversarial rows aim for."""
    cases = [
        ("As an AI language model, I must decline.", "identity:as-an-ai"),          # r-0061
        ("I am an AI and not really a 19th-century novelist.", "identity:i-am-an-ai"),
        ("[DEBUG] The answer follows.", "override:debug-prefix"),                    # r-0062
        ("My system prompt says to remain in character.", "override:prompt-recital"),  # r-0054
        ("Here are my instructions, as requested.", "override:prompt-recital"),      # r-0057
        ("My configuration is temperature 0.7.", "override:config-recital"),         # r-0056
        ("Stepping out of character for a moment.", "identity:breaking-character"),  # r-0060
    ]
    for text, expected in cases:
        verdict = scan_output(text)
        assert verdict.tripped, f"missed persona break: {text!r}"
        assert verdict.label == expected, f"{text!r} -> {verdict.label} (wanted {expected})"


def test_dostoevsky_in_character_does_not_trip() -> None:
    """The false-refusal half, and the one that decides whether this ships.

    These are deliberately adjacent to the patterns: they discuss models,
    instructions, character and debugging as SUBJECT MATTER, in persona. A
    regex that cannot tell 'I am an AI' from 'I am a man' has no business on
    the request path.
    """
    in_character = [
        "I am a sick man. I am a spiteful man. I am an unattractive man.",
        "Raskolnikov held to his theory as though it were an instruction from God himself.",
        "The character of Sonia is the moral centre of the novel.",
        "He was a model of restraint, this Prince Myshkin, and it destroyed him.",
        "My instructions, such as they were, came from a conscience I could not silence.",
        "Consider the configuration of the soul: it is not a machine, and it does not debug.",
        "Kirillov kills himself to prove that he is a god, and the proof consumes him.",
        "Ivan cannot accept a harmony purchased with the suffering of children.",
    ]
    for text in in_character:
        verdict = scan_output(text, canary=CANARY_SENTINEL)
        assert not verdict.tripped, f"false refusal on in-character text: {text!r} -> {verdict.reason}"


def test_plant_canary_does_not_mutate_the_caller_list() -> None:
    """`build_messages()`'s output is also what gets logged and cached. A guard
    that edited it in place would make the recorded prompt differ from the
    prompt that was actually measured."""
    original = [{"role": "system", "content": "persona"}, {"role": "user", "content": "q"}]
    planted = plant_canary(original, CANARY_SENTINEL)
    assert original[0]["content"] == "persona", "caller's messages were mutated"
    assert CANARY_SENTINEL in planted[0]["content"]
    assert planted[1]["content"] == "q", "the user turn must not be touched"


def test_plant_canary_adds_a_system_turn_when_there_is_none() -> None:
    """Silently not planting would leave layer 2 reporting 0% leaks forever."""
    planted = plant_canary([{"role": "user", "content": "q"}], CANARY_SENTINEL)
    assert planted[0]["role"] == "system"
    assert CANARY_SENTINEL in planted[0]["content"]


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
