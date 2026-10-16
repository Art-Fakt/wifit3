from wifit3.models.jobs import JobAction, JobState, ToolStatus
from wifit3.ui.vault.job_pane import (
    JobActionButton, JobPauseButton, JobRow, JobSkipButton,
)


def _job(status=ToolStatus.RUNNING, actions=JobAction.NONE, progress_msg="Running..."):
    return JobState(
        job_id="hashcat-test",
        tool_name="hashcat",
        capture_path="test.hc22000",
        status=status,
        progress_msg=progress_msg,
        actions=actions,
    )


def test_running_job_shows_active_wordlist_in_detail_column():
    job = _job(progress_msg="Running (42.00%) · two.txt (2/2)")

    assert JobRow(job)._detail_markup(job) == "[dim]two.txt (2/2)[/dim]"


def test_running_job_without_tool_status_shows_starting_badge():
    job = _job()
    row = JobRow(job)

    assert "STARTING" in row._status_markup(job)


def test_running_job_with_progress_shows_running_badge():
    job = _job(progress_msg="Running (0.00%)")

    badge = JobRow(job)._status_markup(job)

    assert "RUNNING" in badge
    assert "STARTING" not in badge


def test_stopped_job_has_terminal_badge_and_detail():
    job = _job(status=ToolStatus.STOPPED, actions=JobAction.CLEAR,
               progress_msg="Stopped by user")
    row = JobRow(job)

    assert "STOPPED" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]Stopped by user[/dim]"


def test_exhausted_job_reads_as_not_found():
    job = _job(status=ToolStatus.FAILURE, actions=JobAction.CLEAR, progress_msg="3 wordlists")
    row = JobRow(job)

    assert "NOT FOUND" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]3 wordlists[/dim]"


def test_skip_button_follows_the_jobs_offered_actions():
    skippable = _job(actions=JobAction.STOP | JobAction.SKIP)
    button = JobSkipButton(skippable)
    assert button.display is True

    button.sync(_job(actions=JobAction.STOP))
    assert button.display is False

    button.sync(_job(status=ToolStatus.QUEUED, actions=JobAction.CLEAR))
    assert button.display is False


def test_pause_button_offers_resume_to_a_paused_job_and_nothing_to_an_unreachable_one():
    button = JobPauseButton(_job(actions=JobAction.STOP | JobAction.PAUSE))
    assert button.display is True
    assert str(button.label) == "Pause"

    button.sync(_job(actions=JobAction.STOP | JobAction.RESUME))
    assert button.resumes is True
    assert str(button.label) == "Resume"

    button.sync(_job(actions=JobAction.STOP))
    assert button.display is False


def test_action_button_stops_a_running_job_and_clears_a_finished_one():
    running = _job(actions=JobAction.STOP)
    button = JobActionButton(running)
    assert button.stops is True
    assert str(button.label) == "Stop"

    button.sync(_job(status=ToolStatus.SUCCESS, actions=JobAction.CLEAR))
    assert button.stops is False
    assert str(button.label) == "Clear"


def test_action_button_is_hidden_while_a_job_offers_nothing():
    button = JobActionButton(_job(actions=JobAction.NONE))

    assert button.display is False
