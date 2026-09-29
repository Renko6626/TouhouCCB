"""Run the host maintenance script against an isolated Docker command protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def setup_db():
    """These subprocess tests never need the app's shared database fixture."""
    yield


FAKE_COMMAND = r'''
import json
import os
from pathlib import Path
import signal
import sys

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
command = Path(sys.argv[0]).name
failure = os.environ.get("FAKE_FAILURE", "")

def record(event, data=None):
    with open(os.environ["FAKE_LOG"], "a") as out:
        out.write(json.dumps({"event": event, "args": args, "data": data}) + "\n")

def done(event, data=None, fail=None):
    record(event, data)
    state_path.write_text(json.dumps(state))
    if failure == "interrupt" and event == "dump":
        os.kill(os.getppid(), signal.SIGTERM)
    if failure == (fail or event):
        print("injected failure: " + event, file=sys.stderr)
        sys.exit(1)

if command == "sleep":
    sys.exit(0)
if command == "curl":
    assert args[-1] == "http://127.0.0.1:8004/api/v1/market/list", args
    assert state["running"], "health check while backend stopped"
    done("health")
    sys.exit(0)

assert command == "docker", args
if args[0] == "ps":
    if failure == "daemon_unavailable":
        print("Docker daemon unavailable", file=sys.stderr)
        sys.exit(1)
    if args[:3] == ["ps", "--all", "--quiet"]:
        assert args[3] == "--filter", args
        if len(args) == 5:
            names = [name for name in state["containers"] if args[4] == "name=^/" + name + "$"]
        else:
            assert args[5] == "--filter", args
            names = [name for name, container in state["containers"].items()
                     if args[4] == "id=" + name
                     and args[6] == "label=thccb.season-reset.owner=" + container["owner"]]
    else:
        assert args[:3] == ["ps", "--quiet", "--filter"], args
        names = [name for name, container in state["containers"].items()
                 if args[3] == "id=" + name and container["running"]]
    if names:
        record("find_owned", names)
        print("\n".join(names))
    sys.exit(0)
if args[0] == "stop":
    assert args[1:3] == ["--time", "30"], args
    name = args[3]
    assert state["containers"][name]["owner"] != "foreign-owner"
    if failure != "container_stop":
        state["containers"][name]["running"] = False
    done("stop_container", name, fail="container_stop")
    sys.exit(0)
if args[0] == "wait":
    name = args[1]
    assert not state["containers"][name]["running"], "wait did not stop active maintenance container"
    del state["containers"][name]
    done("wait_container", name)
    print("143")
    sys.exit(0)

assert args[0] == "compose", args
cmd = args[1:]
if cmd[0] == "-f":
    assert cmd[1].endswith("docker-compose.yml") and cmd[2] == "-f", cmd
    override = Path(cmd[3])
    config = json.loads(override.read_text())
    assert config["services"]["backend"]["init"] is True
    assert config["services"]["backend"]["pull_policy"] == "never"
    state["overrides"].append(str(override))
    cmd = cmd[4:]
if cmd[0] == "run":
    assert cmd[1:6] == ["--rm", "--no-deps", "-T", "--pull", "never"], cmd
    assert cmd[6] == "--name" and cmd[8] == "--label", cmd
    name, label = cmd[7], cmd[9]
    assert name.startswith("thccb-season-") and label.startswith("thccb.season-reset.owner="), cmd
    owner = label.split("=", 1)[1]
    state["containers"][name] = {"owner": owner, "running": True}
    cmd = ["run", "--rm", "--no-deps", "-T"] + cmd[10:]
    if "--dry-run" in cmd or cmd[-1] == "scripts/audit_verify.py":
        del state["containers"][name]
if cmd == ["ps", "--status", "running", "-q", "backend"]:
    done("status")
    if state["running"]:
        print("backend-container")
elif cmd == ["stop", "backend"]:
    state["running"] = False
    done("stop")
elif cmd == ["start", "backend"]:
    assert not any(c["running"] and c["owner"] != "foreign-owner" for c in state["containers"].values()), "backend restarted before maintenance exited"
    if failure != "start":
        state["running"] = True
    done("start")
elif cmd[:8] == ["run", "--rm", "--no-deps", "-T", "backend", "python", "scripts/season_reset.py", "--dry-run"]:
    assert cmd[8:] == ["--expected-ruleset", "2026-09-27"], cmd
    done("preview", fail="ruleset")
elif cmd[:7] == ["run", "--rm", "--no-deps", "-T", "backend", "python", "scripts/season_reset.py"]:
    assert cmd[7:] == ["--expected-ruleset", "2026-09-27"], cmd
    stdin = sys.stdin.read()
    assert stdin == "RESET\n", stdin
    assert not state["running"], "reset while writer running"
    assert state["restored"], "reset before verified backup restore"
    assert not state["databases"], "verification database leaked into execution"
    state["reset_count"] += 1
    if failure in ("reset_interrupt", "reset_client_exit", "container_stop", "foreign_container", "daemon_unavailable"):
        if failure == "foreign_container":
            state["containers"][name]["owner"] = "foreign-owner"
        done("reset", stdin)
        if failure == "reset_interrupt":
            os.kill(os.getppid(), signal.SIGTERM)
            signal.pause()
        sys.exit(1)
    del state["containers"][name]
    done("reset", stdin)
elif cmd == ["run", "--rm", "--no-deps", "-T", "backend", "python", "scripts/audit_verify.py"]:
    state["audit_count"] += 1
    done("audit")
elif cmd[:4] == ["exec", "-T", "postgres", "pg_dump"]:
    assert cmd[4:] == ["-Fc", "-U", "thccb", "thccb"], cmd
    assert not state["running"], "backup while writer running"
    done("dump")
    sys.stdout.buffer.write(b"PGDMP\x00complete-production-backup")
elif cmd[:4] == ["exec", "-T", "postgres", "pg_restore"]:
    data = sys.stdin.buffer.read()
    assert data == b"PGDMP\x00complete-production-backup", data
    if cmd[4:] == ["--list"]:
        done("toc")
        print("1; 1259 123 TABLE public user thccb")
    else:
        assert cmd[4:7] == ["--exit-on-error", "-U", "thccb"], cmd
        assert cmd[7] == "-d" and cmd[8] in state["databases"], cmd
        assert cmd[8] != "thccb", "restore targets production"
        if failure != "restore":
            state["restored"].append(cmd[8])
        done("restore", cmd[8])
elif cmd[:4] == ["exec", "-T", "postgres", "createdb"]:
    assert cmd[4:7] == ["-U", "thccb", "--template=template0"], cmd
    name = cmd[7]
    assert name.startswith("thccb_season_verify_") and name != "thccb", name
    assert name not in state["databases"], "validation database reused"
    if failure != "createdb":
        state["databases"].append(name)
    done("createdb", name)
elif cmd[:4] == ["exec", "-T", "postgres", "dropdb"]:
    assert cmd[4:6] == ["-U", "thccb"], cmd
    name = cmd[6]
    assert name != "thccb" and name in state["databases"], "dropping unowned database"
    if failure != "dropdb":
        state["databases"].remove(name)
    done("dropdb", name)
else:
    raise AssertionError("unexpected Docker invocation: " + repr(cmd))
'''


@pytest.fixture
def runner(tmp_path):
    root = tmp_path / "project"
    (root / "deploy").mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "deploy" / "season_reset.sh"
    if source.exists():
        shutil.copyfile(source, root / "deploy" / "season_reset.sh")
    compose_source = source.parent.parent / "docker-compose.yml"
    compose_bytes = compose_source.read_bytes()
    (root / "docker-compose.yml").write_bytes(compose_bytes)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("docker", "curl", "sleep"):
        path = commands / name
        path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        path.chmod(0o755)

    def run(action="preview", *, confirmation=None, failure="", running=True):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "running": running, "databases": [], "restored": [],
            "reset_count": 0, "audit_count": 0, "containers": {}, "overrides": [],
        }))
        log_path = tmp_path / "commands.jsonl"
        if log_path.exists():
            log_path.unlink()
        env = {
            **os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
            "FAKE_STATE": str(state_path), "FAKE_LOG": str(log_path),
            "FAKE_FAILURE": failure,
        }
        env.pop("SEASON_CONFIRM", None)
        if confirmation is not None:
            env["SEASON_CONFIRM"] = confirmation
        result = subprocess.run(
            ["bash", "deploy/season_reset.sh", action], cwd=root,
            env=env, capture_output=True, text=True, timeout=15,
        )
        events = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
        state = json.loads(state_path.read_text())
        assert (root / "docker-compose.yml").read_bytes() == compose_bytes
        assert all(not Path(path).exists() for path in state["overrides"]), "temporary compose override leaked"
        return result, state, events, root

    return run


def event_names(events):
    return [event["event"] for event in events]


def test_preview_is_read_only_and_requires_new_ruleset(runner):
    result, state, events, root = runner()
    assert result.returncode == 0, result.stderr
    assert event_names(events) == ["preview"]
    assert state["running"] is True and state["reset_count"] == 0
    assert not (root / "backups").exists()


@pytest.mark.parametrize("confirmation", [None, "reset", " RESET", "RESET\n"])
def test_execute_rejects_wrong_confirmation_before_any_docker_call(runner, confirmation):
    result, state, events, root = runner("execute", confirmation=confirmation)
    assert result.returncode != 0
    assert events == []
    assert state["running"] is True and state["reset_count"] == 0
    assert not (root / "backups").exists()


def test_old_image_is_rejected_before_stopping_backend(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="ruleset")
    assert result.returncode != 0
    assert event_names(events) == ["preview"]
    assert state["running"] is True and state["reset_count"] == 0


@pytest.mark.parametrize("failure", ["dump", "toc", "createdb", "restore", "dropdb"])
@pytest.mark.parametrize("running", [True, False])
def test_backup_failure_never_resets_and_restores_original_service_state(runner, failure, running):
    result, state, events, root = runner("execute", confirmation="RESET", failure=failure, running=running)
    assert result.returncode != 0
    assert state["running"] is running and state["reset_count"] == 0
    assert not list((root / "backups").glob("*.verified"))
    names = event_names(events)
    if failure == "createdb":
        assert "dropdb" not in names
    if failure == "restore":
        assert "dropdb" in names and state["databases"] == []
    if failure == "dropdb":
        assert "manual cleanup required" in result.stderr
    assert ("start" in names) is running


@pytest.mark.parametrize("running", [True, False])
def test_backup_creates_private_verified_artifacts_without_reset(runner, running):
    result, state, events, root = runner("backup", running=running)
    assert result.returncode == 0, result.stderr
    assert state["running"] is running and state["reset_count"] == 0
    assert state["databases"] == [] and len(state["restored"]) == 1
    dump, = (root / "backups").glob("*.dump")
    assert dump.read_bytes() == b"PGDMP\x00complete-production-backup"
    assert dump.stat().st_mode & 0o777 == 0o600
    for suffix in (".sha256", ".toc", ".verified"):
        artifact = Path(str(dump) + suffix)
        assert artifact.is_file() and artifact.stat().st_mode & 0o777 == 0o600
    checked = subprocess.run(["sha256sum", "--check", str(dump) + ".sha256"], capture_output=True)
    assert checked.returncode == 0
    assert "reset" not in event_names(events) and "audit" not in event_names(events)
    assert "verified" in result.stdout.lower()


@pytest.mark.parametrize("running", [True, False])
def test_execute_restores_backup_before_reset_and_preserves_original_service_state(runner, running):
    result, state, events, root = runner("execute", confirmation="RESET", running=running)
    assert result.returncode == 0, result.stderr
    assert state["running"] is running
    assert state["reset_count"] == 1 and state["audit_count"] == 1
    names = event_names(events)
    assert names.index("dump") < names.index("restore") < names.index("dropdb") < names.index("reset") < names.index("audit")
    if running:
        assert names.index("stop") < names.index("dump")
        assert names.index("reset") < names.index("start") < names.index("health") < names.index("audit")
    else:
        assert "stop" not in names and "start" not in names and "health" not in names
    assert list((root / "backups").glob("*.verified"))


@pytest.mark.parametrize("failure", ["reset", "health", "audit"])
def test_execute_failure_returns_error_and_restores_running_backend(runner, failure):
    result, state, _, root = runner("execute", confirmation="RESET", failure=failure)
    assert result.returncode != 0
    assert state["running"] is True and state["databases"] == []
    assert state["reset_count"] == 1
    assert list((root / "backups").glob("*.verified"))


def test_failure_to_restart_is_not_reported_as_success(runner):
    result, state, events, _ = runner("backup", failure="start")
    assert result.returncode != 0
    assert state["running"] is False
    assert event_names(events).count("start") >= 2
    assert "manual recovery required" in result.stderr


def test_termination_restores_running_backend_without_reset(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="interrupt")
    assert result.returncode != 0
    assert state["running"] is True and state["reset_count"] == 0
    assert "start" in event_names(events)


def test_partial_stop_failure_still_restores_running_backend(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="stop")
    assert result.returncode != 0
    assert state["running"] is True and state["reset_count"] == 0
    assert "start" in event_names(events) and "dump" not in event_names(events)


def test_unknown_action_is_rejected_before_any_service_change(runner):
    result, state, events, _ = runner("destroy")
    assert result.returncode != 0 and events == []
    assert state["running"] is True


@pytest.mark.parametrize("failure", ["reset_interrupt", "reset_client_exit"])
def test_reset_interruption_stops_and_waits_owned_container_before_backend_restart(runner, failure):
    result, state, events, _ = runner("execute", confirmation="RESET", failure=failure)
    assert result.returncode != 0
    assert state["running"] is True and state["containers"] == {}
    names = event_names(events)
    assert names.index("reset") < names.index("stop_container") < names.index("wait_container") < names.index("start")


def test_cannot_stop_maintenance_container_keeps_backend_stopped(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="container_stop")
    assert result.returncode != 0 and state["running"] is False
    assert "start" not in event_names(events)
    assert "manual recovery required" in result.stderr


def test_container_with_foreign_owner_label_is_never_stopped(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="foreign_container")
    assert result.returncode != 0 and state["running"] is False
    assert "start" not in event_names(events)
    assert "stop_container" not in event_names(events) and "wait_container" not in event_names(events)
    assert all(container["owner"] == "foreign-owner" for container in state["containers"].values())


def test_docker_api_failure_is_not_treated_as_container_disappearance(runner):
    result, state, events, _ = runner("execute", confirmation="RESET", failure="daemon_unavailable")
    assert result.returncode != 0 and state["running"] is False
    assert "start" not in event_names(events)
    assert "manual recovery required" in result.stderr
