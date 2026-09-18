from app.coordination_thread import SAMPLE_TURN, validate_turn


def test_sample_valid():
    normalized, errors = validate_turn(SAMPLE_TURN)
    assert errors == []
    assert normalized is not None
    assert normalized["turn"] == 1


def test_reply_must_be_earlier_turn():
    turn = {**SAMPLE_TURN, "turn": 1, "in_reply_to": 1}
    _, errors = validate_turn(turn)
    assert any(e["reason"] == "reply_not_before_turn" for e in errors)


def test_invalid_thread_id():
    turn = {**SAMPLE_TURN, "thread_id": "not-a-uuid"}
    _, errors = validate_turn(turn)
    assert any(e["path"] == "thread_id" for e in errors)
