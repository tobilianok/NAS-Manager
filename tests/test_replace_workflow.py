from app import replace_workflow


def test_state_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / "disk_replacement.json"
    monkeypatch.setattr(replace_workflow, "STATE_FILE", state_file)

    assert replace_workflow.load_state() is None

    state = replace_workflow.start_replacement("tank", "/dev/sdc", "SERIAL123", "ModelX")
    assert state.step == replace_workflow.STEP_OFFLINED
    assert state_file.exists()

    loaded = replace_workflow.load_state()
    assert loaded is not None
    assert loaded.pool == "tank"
    assert loaded.old_disk == "/dev/sdc"
    assert loaded.old_disk_serial == "SERIAL123"
    assert loaded.step == replace_workflow.STEP_OFFLINED


def test_state_step_transitions(tmp_path, monkeypatch):
    state_file = tmp_path / "disk_replacement.json"
    monkeypatch.setattr(replace_workflow, "STATE_FILE", state_file)

    state = replace_workflow.start_replacement("tank", "/dev/sdc", None, None)

    replace_workflow.advance_to_disk_selection(state)
    assert replace_workflow.load_state().step == replace_workflow.STEP_AWAITING_NEW_DISK

    replace_workflow.advance_to_resilvering(state, "/dev/sdz")
    reloaded = replace_workflow.load_state()
    assert reloaded.step == replace_workflow.STEP_RESILVERING
    assert reloaded.new_disk == "/dev/sdz"

    replace_workflow.mark_done(state)
    assert replace_workflow.load_state().step == replace_workflow.STEP_DONE


def test_clear_state_removes_file(tmp_path, monkeypatch):
    state_file = tmp_path / "disk_replacement.json"
    monkeypatch.setattr(replace_workflow, "STATE_FILE", state_file)

    replace_workflow.start_replacement("tank", "/dev/sdc", None, None)
    assert state_file.exists()

    replace_workflow.clear_state()
    assert not state_file.exists()
    assert replace_workflow.load_state() is None


def test_clear_state_is_safe_when_no_file(tmp_path, monkeypatch):
    state_file = tmp_path / "disk_replacement.json"
    monkeypatch.setattr(replace_workflow, "STATE_FILE", state_file)
    replace_workflow.clear_state()  # ne doit pas lever d'exception


def test_load_state_survives_corrupt_json(tmp_path, monkeypatch):
    state_file = tmp_path / "disk_replacement.json"
    state_file.write_text("{not valid json")
    monkeypatch.setattr(replace_workflow, "STATE_FILE", state_file)

    assert replace_workflow.load_state() is None
