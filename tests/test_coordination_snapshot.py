from app.coordination_snapshot import SAMPLE_SNAPSHOT, validate_snapshot


def test_sample_valid():
    normalized, errors = validate_snapshot(SAMPLE_SNAPSHOT)
    assert errors == []
    assert normalized is not None
    assert len(normalized["turns"]) == 1


def test_thread_id_mismatch_on_turn():
    snap = {
        **SAMPLE_SNAPSHOT,
        "turns": [
            {
                **SAMPLE_SNAPSHOT["turns"][0],
                "thread_id": "11111111-1111-4111-8111-111111111111",
            }
        ],
    }
    _, errors = validate_snapshot(snap)
    assert any(e["reason"] == "thread_id_mismatch" for e in errors)


def test_duplicate_turn_numbers():
    turn = SAMPLE_SNAPSHOT["turns"][0]
    snap = {**SAMPLE_SNAPSHOT, "turns": [turn, {**turn, "speaker": "other"}]}
    _, errors = validate_snapshot(snap)
    assert any(e["reason"] == "duplicate_turn_number" for e in errors)
