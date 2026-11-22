"""After a restart: a scrape cut off today is carried on once, before 22:00, never alongside another."""

from __future__ import annotations

import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from scraper import category_tree, checkpoint, lock, resume_after_boot as rab

DAY = datetime.now().date().isoformat()


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "data"))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "logs").mkdir()
    return tmp_path


class Runs:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=self.code)


def _at(hour):
    return lambda: datetime.now().replace(hour=hour, minute=5)


def _go(root, runs, hour=11, argv=(), env_file=None):
    return rab.main(list(argv), root=root, now=_at(hour), run=runs, synced=lambda: True, sleep=lambda s: None,
                    env_file=env_file or str(root / "no-env"))


def _log(root, store, text):
    (root / "logs" / rab.STORES[store][1].format(day=DAY)).write_text(text)


def _cut_off_checkpoint(store="woolworths"):
    checkpoint.save_section(store, "browse", "sig", {"done": 1})


def _snapshot(root, store):
    folder = root / "data" / rab.SNAPSHOT_SUBDIRS[store]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{DAY}.json").write_text("[]")


def test_progress_saved_today_starts_the_stores_wrapper_script_again(root, capsys):
    _cut_off_checkpoint()
    _log(root, "woolworths", "Step 1...\n[3/9] browsing\n")
    runs = Runs()
    assert _go(root, runs) == 0
    assert [c[0] for c in runs.calls] == [["bash", str(root / "run_woolworths.sh")]]
    log = (root / "logs" / f"woolworths-{DAY}.log").read_text()
    assert "Started again after a restart" in log and "progress was saved earlier today" in log
    out = capsys.readouterr().out
    assert "woolworths: starting (start the full wrapper again), attempt 1 of 3" in out and "finished with exit code 0" in out


@pytest.mark.parametrize("store,script", [("paknsave", "run_weekly_combined.sh"), ("newworld", "run_newworld.sh")])
def test_each_store_uses_its_own_wrapper(root, store, script):
    checkpoint.save(store, {"signature": "x", "done": 1})
    runs = Runs()
    _go(root, runs)
    assert runs.calls[0][0] == ["bash", str(root / script)]


def test_a_finished_run_is_left_alone(root, capsys):
    _log(root, "woolworths", "stuff\nRESULT: SUCCESS - 3 products saved\n")
    runs = Runs()
    assert _go(root, runs) == 0 and runs.calls == []
    assert "Nothing to do" in capsys.readouterr().out


def test_a_failed_run_with_no_saved_progress_is_left_alone(root):
    _log(root, "woolworths", "stuff\nRESULT: FAILED - Traceback\n")
    runs = Runs()
    _go(root, runs)
    assert runs.calls == []


def test_a_log_that_stops_with_no_result_and_nothing_saved_starts_again(root):
    _log(root, "newworld", "STEP 0\nsomething\n")
    runs = Runs()
    _go(root, runs)
    assert runs.calls[0][0] == ["bash", str(root / "run_newworld.sh")]
    assert "nothing was saved - starting again" in (root / "logs" / f"newworld-{DAY}.log").read_text()


def test_a_skipped_line_from_a_second_run_does_not_hide_a_cut_off(root):
    _log(root, "newworld", "STEP 0\nsomething\nRESULT: SKIPPED - another scrape was already running\n")
    runs = Runs()
    _go(root, runs)
    assert len(runs.calls) == 1


def test_no_log_today_means_nothing_was_cut_off(root):
    runs = Runs()
    _go(root, runs)
    assert runs.calls == []


def test_a_cut_during_posting_posts_the_saved_snapshot_and_does_not_scrape(root, monkeypatch):
    _log(root, "paknsave", "Snapshot written to x\nPrice history updated\nposting part 1\n")
    _snapshot(root, "paknsave")
    (root / "env").write_text("# secrets\nMAILBOX_URL=https://example.test\nexport MAILBOX_TOKEN='abc123'\n")
    runs = Runs()
    assert _go(root, runs, env_file=str(root / "env")) == 0
    command, kwargs = runs.calls[0]
    assert command[1:] == ["-m", "scraper.post_run", "--site", "paknsave", "--date", DAY]
    assert kwargs["env"]["MAILBOX_URL"] == "https://example.test" and kwargs["env"]["MAILBOX_TOKEN"] == "abc123"
    log = (root / "logs" / f"weekly-combined-{DAY}.log").read_text()
    assert "only posting was cut" in log and log.strip().endswith("RESULT: SUCCESS - saved snapshot posted after a restart")
    assert "abc123" not in log
    # next boot: the log now ends with a result, so nothing more happens
    again = Runs()
    _go(root, again)
    assert again.calls == []


def test_a_failed_post_is_written_to_the_log_as_a_failure(root):
    _log(root, "paknsave", "posting part 1\n")
    _snapshot(root, "paknsave")
    _go(root, Runs(code=1))
    assert (root / "logs" / f"weekly-combined-{DAY}.log").read_text().strip().endswith("RESULT: FAILED - posting after a restart exited with code 1")


def test_nothing_starts_at_or_after_22_00(root, capsys):
    _cut_off_checkpoint()
    runs = Runs()
    assert _go(root, runs, hour=22) == 0 and runs.calls == []
    assert "too late" in capsys.readouterr().out
    _go(root, runs, hour=21)
    assert len(runs.calls) == 1


def test_it_does_not_start_while_another_scrape_holds_the_lock(root, capsys):
    _cut_off_checkpoint()
    runs = Runs()
    with lock.scrape_lock("woolworths scrape"):
        _go(root, runs)
    assert runs.calls == []
    assert "not started - woolworths scrape" in capsys.readouterr().out


def test_a_store_is_started_at_most_three_times_a_day(root, capsys):
    _cut_off_checkpoint()
    runs = Runs()
    for _ in range(5):
        _go(root, runs)
    assert len(runs.calls) == rab.MAX_ATTEMPTS_PER_DAY == 3
    assert "already tried 3 times today" in capsys.readouterr().out


def test_attempts_start_again_the_next_day(root):
    rab.record_attempt("woolworths", "2000-01-01")
    assert rab.attempts_today(DAY) == {}


def test_dry_run_only_says_what_it_would_do(root, capsys):
    _cut_off_checkpoint()
    runs = Runs()
    assert _go(root, runs, argv=["--dry-run"]) == 0
    assert runs.calls == [] and "would start the full wrapper again" in capsys.readouterr().out
    assert rab.attempts_today(DAY) == {}
    assert not (root / "logs" / f"woolworths-{DAY}.log").exists()


def test_it_waits_for_the_clock_to_be_corrected(root):
    _cut_off_checkpoint()
    answers = iter([False, False, True])
    sleeps = []
    runs = Runs()
    rab.main([], root=root, now=_at(11), run=runs, synced=lambda: next(answers), sleep=sleeps.append, env_file="x")
    assert sleeps == [rab.CLOCK_POLL_SECONDS] * 2 and len(runs.calls) == 1


def test_it_gives_up_when_the_clock_never_syncs(root, capsys):
    _cut_off_checkpoint()
    runs = Runs()
    code = rab.main([], root=root, now=_at(11), run=runs, synced=lambda: False, sleep=lambda s: None, env_file="x")
    assert code == 1 and runs.calls == []
    assert "date cannot be trusted" in capsys.readouterr().out


def test_a_damaged_progress_file_is_not_taken_for_a_cut_off_run(root):
    path = checkpoint.checkpoint_path("woolworths")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    runs = Runs()
    _go(root, runs)
    assert runs.calls == []


def test_the_env_file_is_parsed_without_printing_values(tmp_path):
    f = tmp_path / "env"
    f.write_text("# c\n\nA=1\nexport B=\"two words\"\nbadline\nC='x=y'\n")
    assert rab.load_env_file(str(f)) == {"A": "1", "B": "two words", "C": "x=y"}
    assert rab.load_env_file(str(tmp_path / "missing")) == {}


def test_the_wrapper_scripts_record_a_skipped_run():
    from pathlib import Path

    for script in ("run_woolworths.sh", "run_newworld.sh", "run_weekly_combined.sh"):
        text = (Path(rab.REPO_ROOT) / script).read_text()
        assert 'if [ "$EXIT_CODE" -eq 75 ]' in text and "RESULT: SKIPPED" in text
