"""Smoke tests for caller_extractor + note_parser using real BH note shapes."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from lib.caller_extractor import extract_caller, extract_caller_from_notes
from lib.note_parser import find_urls, extract_recordings_from_note, extract_all_recordings


def test_jason_garcia_chip():
    html = '<a href="/users/details/14245674" class="cui5-user-chip">@Jason Garcia</a>&nbsp;I want you to ask him...'
    # @-mention alone is not a caller-attribution; we should fall through to default.
    assert extract_caller(html, default=None) is None


def test_regina_attribution():
    html = "Regina spoke with the seller's daughter, will follow up Friday"
    assert extract_caller(html, default=None) == "Regina"


def test_shyrine_chip_with_action():
    html = 'Hi @Shyrine Asuncion, Andre called and said...'
    # Andre isn't in roster, but Shyrine is mentioned, no action verb attached -> None
    assert extract_caller(html, default=None) is None


def test_shyrine_action():
    html = "Shyrine called the owner this morning, no answer."
    assert extract_caller(html, default=None) == "Shyrine"


def test_mike_kent_addressee_not_caller():
    html = '@Mike Kent Number I dialed: (718) 450-7371. I spoke with Mr. Williams...'
    # @Mike Kent is the ADDRESSEE; the writer is the caller. We can't ID writer
    # from chip alone, so return None (caller will fall back to default).
    assert extract_caller(html, default=None) is None


def test_mike_kent_action_not_addressee():
    html = "Mike called the seller this morning, no answer."
    assert extract_caller(html, default=None) == "Mike Kent"


def test_neighbor_john_disambiguation():
    html = "his neighbor John asked to wait until he comes back"
    # "John" is in roster but qualified by "neighbor" -> not a caller
    assert extract_caller(html, default=None) is None


def test_aircall_recording_extraction():
    # Real-shape note: chip is addressee, writer is unknown -> agent="unknown".
    html = ('<a href="/users/details/14245652">@Mike Kent</a> Number I dialed: (718) 450-7371. '
            'I spoke with Mr. Williams. https://assets.aircall.io/calls/3435922742/recording')
    note = {"content": html, "add_time": "2026-01-19 17:51:36"}
    recs = extract_recordings_from_note(note)
    assert len(recs) == 1
    assert "aircall.io" in recs[0]["url"]
    # No clear caller attribution -> "unknown"
    assert recs[0]["agent"] == "unknown"


def test_recording_in_anchor():
    html = ("Hey @Mike Kent, got the owner. Recording: "
            '<a href="https://assets.aircall.io/calls/3719179017/recording">link</a>')
    note = {"content": html, "add_time": "2026-02-01 10:00:00"}
    recs = extract_recordings_from_note(note)
    assert len(recs) == 1
    assert recs[0]["url"] == "https://assets.aircall.io/calls/3719179017/recording"


def test_extract_all_dedups():
    note = {
        "content": ('Recording: https://assets.aircall.io/calls/1/recording '
                    '<a href="https://assets.aircall.io/calls/1/recording">again</a>'),
        "add_time": "2026-03-01 12:00:00",
    }
    recs = extract_all_recordings([note])
    assert len(recs) == 1


def test_caller_from_notes_newest_first():
    notes = [
        {"content": "Regina called early", "add_time": "2026-01-01 10:00:00"},
        {"content": "Shyrine called later", "add_time": "2026-02-01 10:00:00"},
    ]
    caller, t = extract_caller_from_notes(notes, default=None)
    assert caller == "Shyrine"
    assert t == "2026-02-01 10:00:00"


def test_mostafa_khirat_never_returned_from_content():
    # Even when "Mostafa" appears with an action verb, never attribute to him.
    html = "Mostafa called the seller and left a VM."
    assert extract_caller(html, default=None) is None


def test_jason_garcia_alias_excluded_from_content():
    # Jason Garcia is Mostafa Khirat's Pipedrive alias -- excluded.
    html = "Jason called the owner this morning."
    assert extract_caller(html, default=None) is None


def test_user_resolver_skips_mostafa_falls_back_to_older_note():
    # Newest note has only Mostafa as author (no content attribution); we must
    # walk back to the next-most-recent note rather than returning Mostafa.
    notes = [
        {"content": "Following up tomorrow.", "add_time": "2026-03-01 10:00:00",
         "user_id": 14245674},  # Mostafa Khirat / Jason Garcia
        {"content": "Spoke with the seller, motivated.", "add_time": "2026-02-01 10:00:00",
         "user_id": 99999},  # Regina
    ]
    def resolver(uid):
        return {14245674: "Mostafa Khirat", 99999: "Regina"}.get(uid)
    caller, t = extract_caller_from_notes(notes, default="BH Team", user_resolver=resolver)
    assert caller == "Regina", f"Expected Regina (fallback), got {caller!r}"
    assert t == "2026-02-01 10:00:00"


def test_user_resolver_all_excluded_returns_default():
    # If every note's user_id resolves to an excluded name and content has no
    # attribution, fall back to the configured default ("BH Team").
    notes = [
        {"content": "Update.", "add_time": "2026-03-01 10:00:00", "user_id": 14245674},
        {"content": "Update.", "add_time": "2026-02-01 10:00:00", "user_id": 14245674},
    ]
    def resolver(uid):
        return "Mostafa Khirat" if uid == 14245674 else None
    caller, t = extract_caller_from_notes(notes, default="BH Team", user_resolver=resolver)
    assert caller == "BH Team"
    # Latest add_time still surfaced for recency stamp.
    assert t == "2026-03-01 10:00:00"


if __name__ == "__main__":
    # Cheap inline runner so this works without pytest installed.
    import inspect
    here = sys.modules[__name__]
    n = 0; passed = 0
    for nm, fn in inspect.getmembers(here, inspect.isfunction):
        if not nm.startswith("test_"): continue
        n += 1
        try:
            fn(); passed += 1
            print(f"OK   {nm}")
        except AssertionError as e:
            print(f"FAIL {nm}: {e}")
        except Exception as e:
            print(f"ERR  {nm}: {e!r}")
    print(f"\n{passed}/{n} passed")
    sys.exit(0 if passed == n else 1)
