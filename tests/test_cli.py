import os

from shelly_monitor import cli


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nexport SM_A="quoted value"\nSM_B=plain\nSM_C=keep\n\nbroken line\n')
    monkeypatch.setenv("SM_C", "existing")
    monkeypatch.delenv("SM_A", raising=False)
    monkeypatch.delenv("SM_B", raising=False)
    cli.load_dotenv(str(env))
    assert os.environ["SM_A"] == "quoted value"
    assert os.environ["SM_B"] == "plain"
    assert os.environ["SM_C"] == "existing"
    cli.load_dotenv(str(tmp_path / "missing.env"))  # no error


def test_devices_rename_and_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "c.db"))
    env = str(tmp_path / "none.env")
    assert cli.main(["--env-file", env, "devices", "--name", "abc", "Flat B"]) == 0
    assert cli.main(["--env-file", env, "devices", "--name", "abc:2", "Lamp 2"]) == 0
    assert cli.main(["--env-file", env, "devices", "--name", "abc_3", "Lamp 3"]) == 0
    assert cli.main(["--env-file", env, "devices"]) == 0
    out = capsys.readouterr().out
    assert "home\tabc:0\tFlat B" in out and "abc:2\tLamp 2" in out and "abc:3\tLamp 3" in out
    assert cli.main(["--env-file", env, "report", "--from", "2026-10-01", "--to", "2026-10-31", "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("period,")
    assert cli.main(["--env-file", env, "export"]) == 0
    assert capsys.readouterr().out.startswith("ts,iso_time")


def test_poll_skips_cycle_on_cloud_error(tmp_path, monkeypatch):
    from shelly_monitor.cloud import CloudError
    from shelly_monitor.store import Store

    class Failing:
        def discover_home(self):
            raise CloudError("rate limited (HTTP 429)")

        def all_status(self):
            raise CloudError("rate limited (HTTP 429)")

    with Store(str(tmp_path / "p.db")) as st:
        from shelly_monitor.config import Home
        assert cli.poll_once([cli.Target(Home("home", "k", "s"), Failing())], st) is False
        assert list(st.samples()) == []


def test_dotenv_inline_comments_and_export_whitespace(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "SM_D=val # comment\n"
        "export\tSM_E=tabbed\n"
        "export   SM_F=spaces  # c\n"
        'SM_G="has # hash" # c\n'
        "SM_H=a#b\n"
        "exporter=x\n"
    )
    for k in ("SM_D", "SM_E", "SM_F", "SM_G", "SM_H", "exporter"):
        monkeypatch.delenv(k, raising=False)
    cli.load_dotenv(str(env))
    assert os.environ["SM_D"] == "val"
    assert os.environ["SM_E"] == "tabbed"
    assert os.environ["SM_F"] == "spaces"
    assert os.environ["SM_G"] == "has # hash"
    assert os.environ["SM_H"] == "a#b"
    assert os.environ["exporter"] == "x"
    assert "export\tSM_E" not in os.environ


def test_invalid_interval(tmp_path, monkeypatch, capsys):
    import pytest

    monkeypatch.setenv("SHELLY_INTERVAL", "abc")
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "i.db"))
    cli.main(["--env-file", str(tmp_path / "none"), "devices"])  # create DB
    with pytest.raises(SystemExit) as e:
        cli.main(["--env-file", str(tmp_path / "none"), "report"])
    assert e.value.code == 2
    assert "SHELLY_INTERVAL" in capsys.readouterr().err


def test_report_export_missing_db(tmp_path, monkeypatch, capsys):
    import pytest

    for db in (tmp_path / "missing.db", tmp_path / "nodir" / "x.db"):
        monkeypatch.setenv("SHELLY_DB", str(db))
        for cmd in ("report", "export"):
            with pytest.raises(SystemExit) as e:
                cli.main(["--env-file", str(tmp_path / "none"), cmd])
            assert e.value.code == 1
            assert "database not found" in capsys.readouterr().err
        assert not db.exists()


def test_devices_bad_db_dir_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "nodir" / "x.db"))
    assert cli.main(["--env-file", str(tmp_path / "none"), "devices"]) == 1
    assert "cannot use database" in capsys.readouterr().err


def test_parse_device_ref():
    import pytest
    assert cli.parse_device_ref("aabbcc000001") == (None, "aabbcc000001", 0)
    assert cli.parse_device_ref("aabbcc000001:3") == (None, "aabbcc000001", 3)
    assert cli.parse_device_ref("aabbcc000001_3") == (None, "aabbcc000001", 3)
    assert cli.parse_device_ref("Casa mare/aabbcc000001:2") == ("Casa mare", "aabbcc000001", 2)
    with pytest.raises(SystemExit):
        cli.parse_device_ref("/abc")
    with pytest.raises(SystemExit):
        cli.parse_device_ref("abc:x")


def test_report_zero_channels_cli(tmp_path, monkeypatch, capsys):
    from datetime import datetime
    from shelly_monitor.cloud import DeviceInfo, Sample
    from shelly_monitor.store import Store

    db = tmp_path / "z.db"
    base = int(datetime(2026, 10, 1, 12).timestamp())
    with Store(str(db)) as st:
        st.save_poll([DeviceInfo("on", "M", "G2+"), DeviceInfo("off", "M", "G2+")],
                     [Sample(base, "on", 0, True, None, True), Sample(base, "off", 0, False, None, True)], base)
        st.save_poll([DeviceInfo("on", "M", "G2+"), DeviceInfo("off", "M", "G2+")],
                     [Sample(base + 60, "on", 0, False, None, True), Sample(base + 60, "off", 0, False, None, True)],
                     base + 60)
    monkeypatch.setenv("SHELLY_DB", str(db))
    env = ["--env-file", str(tmp_path / "none")]

    def devices_in(args):
        assert cli.main(env + ["report", "--format", "csv"] + args) == 0
        return [line.split(",")[2] for line in capsys.readouterr().out.splitlines()[1:]]

    assert devices_in(["--group", "total"]) == ["off", "on"]
    assert devices_in(["--group", "day"]) == ["on"]
    assert devices_in(["--group", "day", "--all"]) == ["off", "on"]
