# test/test_no_rag_arm.py
"""P3 step 7 verify: the no-RAG positive-control arm actually removes grounding.

Offline. No generation, no judge, no API call -- this asserts on the MESSAGES
that would be sent, which is the only place the difference between the two arms
exists.

The failure this guards is silent and expensive. If the flag were not threaded
past `build_context()`, both arms would send identical prompts, the run would
report "no difference between grounded and ungrounded", and that reads exactly
like outcome C -- a broken gate. A run that completes cleanly and measures the
wrong thing looks identical to success, so the assertion runs BEFORE the spend.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config  # noqa: E402
from src.rag.generate import build_messages  # noqa: E402

_PASSAGES = ('[1] (Crime and Punishment, part 1, ch 7) "He took the axe from under his coat."\n'
             '[2] (Crime and Punishment, part 5, ch 4) "It was I who killed her, Sonia."')
_QUESTION = "Describe the moment Raskolnikov kills the pawnbroker."


def _text(messages: list[dict]) -> str:
    return "\n".join(m.get("content", "") or "" for m in messages)


def test_grounded_arm_carries_the_passages() -> None:
    """Non-vacuity: if this fails, the test below proves nothing."""
    body = _text(build_messages(Config.PERSONA_HEAD, _PASSAGES, _QUESTION))
    assert "took the axe from under his coat" in body
    assert "It was I who killed her" in body


def test_no_rag_arm_carries_no_passage_text() -> None:
    """The control arm sends an empty passages block. Asserted on the message
    content rather than on a log line -- a log line can be right while the
    prompt is wrong."""
    body = _text(build_messages(Config.PERSONA_HEAD, "", _QUESTION))
    assert "took the axe from under his coat" not in body
    assert "It was I who killed her" not in body
    assert "Crime and Punishment" not in body


def test_the_two_arms_differ_only_in_the_passages() -> None:
    """Everything else is held identical: same persona head, same question, same
    composition. If the arms differed anywhere else, a measured difference could
    not be attributed to grounding."""
    grounded = build_messages(Config.PERSONA_HEAD, _PASSAGES, _QUESTION)
    control = build_messages(Config.PERSONA_HEAD, "", _QUESTION)
    assert len(grounded) == len(control), "the arms differ in message COUNT, not just content"
    assert [m.get("role") for m in grounded] == [m.get("role") for m in control]

    # The question must survive into the control arm -- an arm that drops the
    # question is not measuring grounding, it is measuring an empty prompt.
    assert _QUESTION in _text(control)

    # And the control's prompt must be strictly shorter: the passages are gone.
    assert len(_text(control)) < len(_text(grounded))


def test_the_seam_asserts_rather_than_logs() -> None:
    """The eval harness must ASSERT the control arm is empty at the seam. A
    warning that scrolls past is not a guard, and this run costs real money."""
    with open(os.path.join("scripts", "eval_quality.py"), encoding="utf-8") as f:
        source = f.read()
    assert 'assert not passages_block' in source, \
        "the no-RAG seam does not assert its own postcondition"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
