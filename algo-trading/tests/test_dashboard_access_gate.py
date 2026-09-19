from types import SimpleNamespace

from dashboard.app import render_dashboard_access_gate


class _SessionState(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __setattr__(self, name, value):
        self[name] = value


class _FakeForm:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSt:
    def __init__(self, entered_password: str = "", submitted: bool = False):
        self.session_state = _SessionState()
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.reruns = 0
        self._entered_password = entered_password
        self._submitted = submitted

    def warning(self, message, **kwargs):
        self.warnings.append(message)

    def error(self, message, **kwargs):
        self.errors.append(message)

    def markdown(self, *args, **kwargs):
        pass

    def title(self, *args, **kwargs):
        pass

    def form(self, *args, **kwargs):
        return _FakeForm()

    def text_input(self, *args, **kwargs):
        return self._entered_password

    def form_submit_button(self, *args, **kwargs):
        return self._submitted

    def rerun(self):
        self.reruns += 1


def settings_with_password(password: str):
    return SimpleNamespace(dashboard_password=SimpleNamespace(get_secret_value=lambda: password))


def test_gate_is_open_and_warns_when_no_password_is_configured():
    fake_st = FakeSt()

    unlocked = render_dashboard_access_gate(fake_st, settings_with_password(""))

    assert unlocked is True
    assert fake_st.warnings


def test_gate_stays_open_once_session_is_already_unlocked():
    fake_st = FakeSt()
    fake_st.session_state.dashboard_unlocked = True

    unlocked = render_dashboard_access_gate(fake_st, settings_with_password("secret123"))

    assert unlocked is True


def test_gate_blocks_until_the_form_is_submitted():
    fake_st = FakeSt(submitted=False)

    unlocked = render_dashboard_access_gate(fake_st, settings_with_password("secret123"))

    assert unlocked is False
    assert not fake_st.session_state.get("dashboard_unlocked")
    assert fake_st.reruns == 0


def test_gate_unlocks_on_the_correct_password():
    fake_st = FakeSt(entered_password="secret123", submitted=True)

    render_dashboard_access_gate(fake_st, settings_with_password("secret123"))

    assert fake_st.session_state.dashboard_unlocked is True
    assert fake_st.reruns == 1
    assert not fake_st.errors


def test_gate_rejects_the_wrong_password():
    fake_st = FakeSt(entered_password="wrong", submitted=True)

    unlocked = render_dashboard_access_gate(fake_st, settings_with_password("secret123"))

    assert unlocked is False
    assert not fake_st.session_state.get("dashboard_unlocked")
    assert fake_st.errors
    assert fake_st.reruns == 0
