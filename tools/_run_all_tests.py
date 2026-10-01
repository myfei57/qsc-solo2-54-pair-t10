"""通用测试运行器（环境无 pytest）：支持 platform/clock/tmp_path 三个夹具。"""

from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile
import traceback

from hydrorefuel.app import build_platform
from hydrorefuel.clock import ManualClock
from hydrorefuel.config import default_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


class _Raises:
    def __init__(self, expected):
        self._expected = expected
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        if exc_type is None:
            raise AssertionError(f"期望抛出 {self._expected.__name__}，但没有异常")
        if not issubclass(exc_type, self._expected):
            return False
        self.value = exc
        return True


class _PytestShim:
    """最小 pytest 替身：只实现测试里用到的 raises / fixture。"""

    raises = staticmethod(_Raises)

    @staticmethod
    def fixture(*_args, **_kwargs):
        def decorator(func):
            return func

        if _args and callable(_args[0]):
            return _args[0]
        return decorator


sys.modules.setdefault("pytest", _PytestShim)



def run() -> int:
    failures = 0
    total = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = importlib.import_module(f"tests.{path.stem}")
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            func = getattr(module, name)
            total += 1
            args = func.__code__.co_varnames[: func.__code__.co_argcount]
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = pathlib.Path(tmp)
                kwargs = {}
                if "clock" in args:
                    kwargs["clock"] = ManualClock()
                if "tmp_path" in args:
                    kwargs["tmp_path"] = tmp_path
                if "platform" in args:
                    kwargs["platform"] = build_platform(
                        default_config(),
                        clock=kwargs.get("clock", ManualClock()),
                        storage_root=str(tmp_path / "state"),
                    )
                try:
                    func(**kwargs)
                except Exception:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {path.stem}::{name}")
                    traceback.print_exc()
                else:
                    print(f"PASS {path.stem}::{name}")
    print(f"\n{total - failures}/{total} passed, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
