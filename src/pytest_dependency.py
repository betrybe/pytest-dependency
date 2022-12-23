"""$DOC"""

__version__ = "$VERSION"

import logging
import pytest
import inspect
from types import ModuleType
from typing import Callable, List, Type, Union

logger = logging.getLogger(__name__)

_accept_xfail = False
_automark = False
_ignore_unknown = False


class DependencyItemStatus(object):
    """Status of a test item in a dependency manager."""

    Phases = ("setup", "call", "teardown")

    def __init__(self):
        self.results = {w: None for w in self.Phases}

    def __str__(self):
        status_list = [f"{w}: {self.results[w]}" for w in self.Phases]
        return f'Status({", ".join(status_list)})'

    def _accept_xfail(self, rep):
        """Take xfail and accept_xfail into account."""
        return (
            _accept_xfail
            and (rep.when == "call")
            and (rep.outcome == "skipped")
            and (hasattr(rep, "wasxfail"))
        )

    def addResult(self, rep):
        self.results[rep.when] = (
            "passed" if self._accept_xfail(rep) else rep.outcome
        )

    def isSuccess(self):
        return list(self.results.values()) == ["passed", "passed", "passed"]


class DependencyManager(object):
    """Dependency manager, stores the results of tests."""

    ScopeCls = {
        "session": pytest.Session,
        "package": pytest.Package,
        "module": pytest.Module,
        "class": pytest.Class,
    }

    @classmethod
    def getManager(cls, item, scope):
        """Get the DependencyManager object from the node at scope level.
        Create it, if not yet present.
        """
        node = item.getparent(cls.ScopeCls[scope])
        if not node:
            return None
        if not hasattr(node, "dependencyManager"):
            node.dependencyManager = cls(scope)
        return node.dependencyManager

    def __init__(self, scope):
        self.results = {}
        self.scope = scope

    def addResult(self, item, name, rep):
        if not name:
            # Old versions of pytest used to add an extra "::()" to
            # the node ids of class methods to denote the class
            # instance.  This has been removed in pytest 4.0.0.
            nodeid = item.nodeid.replace("::()::", "::")
            if self.scope in ["session", "package"]:
                name = nodeid
            elif self.scope == "module":
                name = nodeid.split("::", 1)[1]
            elif self.scope == "class":
                name = nodeid.split("::", 2)[2]
            else:
                raise RuntimeError(
                    "Internal error: invalid scope '%s'" % self.scope
                )
        status = self.results.setdefault(name, DependencyItemStatus())
        logger.debug(
            "register %s %s %s in %s scope",
            rep.when,
            name,
            rep.outcome,
            self.scope,
        )
        status.addResult(rep)

    def checkDepend(self, depends, item):
        logger.debug(
            "check dependencies of %s in %s scope ...", item.name, self.scope
        )
        for i in depends:
            if i in self.results:
                if self.results[i].isSuccess():
                    logger.debug("... %s succeeded", i)
                    continue
                else:
                    logger.debug("... %s has not succeeded", i)
            else:
                logger.debug("... %s is unknown", i)
                if _ignore_unknown:
                    continue
            logger.info("skip %s because it depends on %s", item.name, i)
            pytest.skip(f"{item.name} depends on {i}")


def depends(request, other, scope="module"):
    """Add dependency on other test.

    Call pytest.skip() unless a successful outcome of all of the tests in
    other has been registered previously.  This has the same effect as
    the `depends` keyword argument to the :func:`pytest.mark.dependency`
    marker.  In contrast to the marker, this function may be called at
    runtime during a test.

    :param request: the value of the `request` pytest fixture related
        to the current test.
    :param other: dependencies, a list of names of tests that this
        test depends on.  The names of the dependencies must be
        adapted to the scope.
    :type other: iterable of :class:`str`
    :param scope: the scope to search for the dependencies.  Must be
        either `'session'`, `'package'`, `'module'`, or `'class'`.
    :type scope: :class:`str`

    .. versionadded:: 0.2

    .. versionchanged:: 0.5.0
        the scope parameter has been added.
    """
    item = request.node
    manager = DependencyManager.getManager(item, scope=scope)
    manager.checkDepend(other, item)


def pytest_addoption(parser):
    parser.addini(
        "automark_dependency",
        "Add the dependency marker to all tests automatically",
        type="bool",
        default=False,
    )
    parser.addini(
        "accept_xfail",
        "Consider xfailing dependencies as succesful dependencies.",
        type="bool",
        default=False,
    )
    parser.addoption(
        "--ignore-unknown-dependency",
        action="store_true",
        default=False,
        help="ignore dependencies whose outcome is not known",
    )


def pytest_configure(config):
    global _accept_xfail, _automark, _ignore_unknown
    _accept_xfail = config.getini("accept_xfail")
    _automark = config.getini("automark_dependency")
    _ignore_unknown = config.getoption("--ignore-unknown-dependency")
    config.addinivalue_line(
        "markers",
        "dependency(name=None, depends=[]): "
        "mark a test to be used as a dependency for "
        "other tests or to depend on other tests.",
    )


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store the test outcome if this item is marked "dependency"."""
    outcome = yield
    marker = item.get_closest_marker("dependency")
    if marker is not None or _automark:
        rep = outcome.get_result()
        name = marker.kwargs.get("name") if marker is not None else None
        for scope in DependencyManager.ScopeCls:
            if manager := DependencyManager.getManager(item, scope=scope):
                manager.addResult(item, name, rep)


def pytest_runtest_setup(item):
    """Check dependencies if this item is marked "dependency".
    Skip if any of the dependencies has not been run successfully.
    """
    marker = item.get_closest_marker("dependency")
    if marker is not None:
        if depends := marker.kwargs.get("depends"):
            scope = marker.kwargs.get("scope", "module")
            manager = DependencyManager.getManager(item, scope=scope)
            manager.checkDepend(depends, item)


def mark_dependency(mocked, dependent_tests):
    return pytest.param(
        mocked,
        marks=[pytest.mark.dependency(depends=dependent_tests)],
    )


def mark_xfail(mocked, expected: Type[BaseException] = AssertionError):
    """
    Sets up parametrization with a mocked implementation expected to fail.

    Parameters
    ----------
    mocked : function
        the mocked implementation to try out.
    expected : Exception, optional
        An expected Exception, by default AssertionError

    Returns
    -------
    pytest.param
        Configured param for pytest fixture parametrization.
    """
    return pytest.param(
        mocked,
        marks=[
            pytest.mark.xfail(
                raises=expected,
                reason=mocked.__doc__ or "Should fail",
                strict=True,
            ),
            pytest.mark.dependency(),
        ],
    )


def build_mocked_assets(
    mocks_module: ModuleType,
    asset_to_mock: Union[Callable, Type],
    test_function: Callable,
    **expected_raise: Type[BaseException],
) -> List:
    """
    Sets up parameters with mocked implementations expected to fail.

    Parameters
    ----------
    `mocks_module` : module
        the module that contains the mocking assets (paremeters)
    `asset_to_mock` : function or class
        the asset (function or class) intended to be mocked
    `test_function` : function
        the test function which will be parametrized
    keyword arguments:
        replace default xfail exception for a given mocking asset
        Example: `build_mocked_assets(..., _TestSomeClass=TypeError)`

    Returns
    -------
    `list`
        Configured mocked params for pytest fixture parametrization.
    """
    asset_map = _build_asset_map(mocks_module)
    _mocked_tests = [
        f"{test_function.__name__}[{asset_name}]" for asset_name in asset_map
    ]

    _mocking_config = _build_mocking_config(
        asset_to_mock, expected_raise, asset_map, _mocked_tests
    )
    return _mocking_config


def _build_mocking_config(
    asset_to_mock, expected_raise, asset_map, _mocked_tests
):
    _mocking_config = [
        mark_xfail(asset)
        for asset_name, asset in asset_map.items()
        if asset_name not in expected_raise
    ]
    for asset_name, expected in expected_raise.items():
        _mocking_config.append(mark_xfail(asset_map[asset_name], expected))

    _mocking_config.append(mark_dependency(asset_to_mock, _mocked_tests))
    return _mocking_config


def _build_asset_map(mocks_module):
    return {
        asset_name: asset
        for asset_name, asset in inspect.getmembers(mocks_module)
        if (
            (inspect.isclass(asset) or inspect.isfunction(asset))
            and asset_name.lower().startswith("_test")
            and inspect.getmodule(asset) is mocks_module
        )
    }
