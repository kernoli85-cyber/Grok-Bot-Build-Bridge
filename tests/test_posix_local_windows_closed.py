import os

import pytest

from grok_bot_build_bridge.runner import posix_local as PL


@pytest.mark.skipif(os.name == "posix", reason="Windows fail-closed check")
def test_local_runner_refuses_on_windows():
    for fn in (lambda: PL.validate_executable("C:/grok.exe"), PL.make_private_cwd, PL.require_posix_nonroot):
        with pytest.raises(PL.RunnerError) as e:
            fn()
        assert e.value.code == "posix_only"
