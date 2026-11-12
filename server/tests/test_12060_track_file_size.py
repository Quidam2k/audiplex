"""#12060: track JSON carries file_size, so the phone can key its stream cache on it."""
from audiplex.models import Track


def test_track_json_carries_file_size(client, sample_track):
    body = client.get(f"/api/music/tracks/{sample_track.id}").json()
    assert body["file_size"] == 5_000_000


def test_a_swapped_file_changes_file_size(client, db_session, sample_track):
    db_session.query(Track).filter(Track.id == sample_track.id).update({Track.file_size: 3_890_739})
    db_session.commit()
    body = client.get(f"/api/music/tracks/{sample_track.id}").json()
    assert body["file_size"] == 3_890_739
