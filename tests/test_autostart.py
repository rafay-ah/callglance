import pytest

from callglance import autostart


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "system"))
    monkeypatch.delenv("APPIMAGE", raising=False)
    return tmp_path


def system_entry(xdg):
    path = xdg / "system" / "autostart" / autostart.FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nType=Application\nName=CallGlance\nExec=callglance\n")
    return path


def test_without_a_system_entry(xdg):
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    assert autostart.is_enabled()
    text = autostart.user_path().read_text()
    assert "--background" in text and "X-GNOME-Autostart-enabled=true" in text
    autostart.set_enabled(False)
    assert not autostart.is_enabled() and not autostart.user_path().exists()


def test_system_entry_is_on_by_default_and_can_be_hidden_per_user(xdg):
    system_entry(xdg)
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert "Hidden=true" in autostart.user_path().read_text()
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    assert not autostart.user_path().exists() and autostart.is_enabled()


def test_appimage_path_is_used(xdg, monkeypatch):
    monkeypatch.setenv("APPIMAGE", "/home/me/Apps/CallGlance x86_64.AppImage")
    autostart.set_enabled(True)
    assert "Exec='/home/me/Apps/CallGlance x86_64.AppImage' --background" in \
        autostart.user_path().read_text()
