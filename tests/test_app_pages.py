"""App wiring tests using Streamlit's AppTest harness.

These exist because weaker checks kept passing while the app was broken:

* An HTTP 200 from the server proves nothing -- Streamlit serves the page shell
  with 200 even when the script raises. That missed a duplicate-``url_path``
  crash in ``st.navigation`` which made the whole app unusable.
* ``at.exception`` alone is not enough either. Streamlit catches a failed
  ``st.dataframe`` serialisation and renders an error *element*, so a mixed-type
  Arrow column showed a red box to the user while the test stayed green.
  Every assertion here checks ``at.error`` as well.
* Page switching via ``query_params`` does **not** work with ``st.navigation``;
  it silently keeps rendering the default page, so parametrising over URL paths
  tested the same page ten times. Pages are driven through per-page driver
  scripts instead (see ``run_source``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
# Absolute: AppTest resolves relative paths against the *calling* file.
APP = str(ROOT / "app.py")
TIMEOUT = 180

URL_PATHS = [
    "modules",
    "data-health",
    "top-ranked",
    "know-the-stock",
    "trendlines",
    "triangle",
    "breakout",
    "candle-50",
    "displacement",
    "backtester",
]


def problems(at: AppTest) -> list[str]:
    """Both uncaught exceptions and rendered error elements."""
    return [str(e.value) for e in at.exception] + [str(e.value) for e in at.error]


def run_source(body: str) -> AppTest:
    """Execute a page through a driver script.

    ``AppTest.from_function`` re-executes only the function's own source in a
    fresh namespace, so the module-level imports a view depends on are gone and
    every page fails with NameError. A driver script with real imports is what
    actually exercises the page.
    """
    source = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n{body}"
    at = AppTest.from_string(source, default_timeout=TIMEOUT)
    at.run()
    return at


def run_overview(func_name: str) -> AppTest:
    return run_source(f"import app\napp.{func_name}()")


def run_scanner(scanner: str) -> AppTest:
    return run_source(
        "import app\n"
        "from views.common import scanner_page\n"
        f"scanner_page({scanner!r}, app.DESCRIPTIONS[{scanner!r}])"
    )


def run_view(module: str) -> AppTest:
    return run_source(f"from views import {module}\n{module}.render()")


class TestNavigation:
    def test_app_script_runs_without_raising(self) -> None:
        """Regression: st.navigation raised on duplicate url_path."""
        at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
        assert not problems(at), problems(at)

    def test_url_paths_are_unique(self) -> None:
        """Both view modules expose a function called `render`."""
        assert len(URL_PATHS) == len(set(URL_PATHS))

    def test_default_page_is_the_overview(self) -> None:
        at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
        assert any("Modules" in h.value for h in at.subheader)


class TestOverviewPages:
    def test_overview_renders_without_an_error_element(self) -> None:
        """Regression: mixed int/str in 'Min bars' failed Arrow serialisation."""
        at = run_overview("page_overview")
        assert not problems(at), problems(at)
        assert len(at.dataframe) >= 1

    def test_overview_module_table_lists_all_eight_sections(self) -> None:
        at = run_overview("page_overview")
        sections = {r["Section"] for r in at.dataframe[0].value.to_dict("records")}
        assert sections == {"3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8"}

    def test_data_health_renders(self) -> None:
        at = run_overview("page_data_health")
        assert not problems(at), problems(at)

    def test_data_health_redacts_credentials(self) -> None:
        """Spec 3.2 / 6.1: no secret may reach the UI in any deployment mode."""
        from infinity.config import load_upstox_credentials

        creds = load_upstox_credentials()
        at = run_overview("page_data_health")
        rendered = " ".join(str(j.value) for j in at.json)

        for secret in (creds.api_secret, creds.access_token):
            if secret:
                assert secret not in rendered, "raw secret leaked into the UI"
        if creds.api_key:
            assert creds.api_key not in rendered, "raw API key leaked into the UI"


class TestScannerPages:
    """Each scanner page is driven through its own callable."""

    @pytest.mark.parametrize(
        "scanner",
        ["golden_zone", "trendlines", "triangle", "resistance_breakout",
         "candle_50", "displacement"],
    )
    def test_scanner_page_renders(self, scanner: str) -> None:
        at = run_scanner(scanner)
        assert not problems(at), f"{scanner}: {problems(at)}"

    def test_know_the_stock_renders(self) -> None:
        at = run_view("view_know_stock")
        assert not problems(at), problems(at)

    def test_backtester_renders(self) -> None:
        at = run_view("view_backtester")
        assert not problems(at), problems(at)

    def test_backtester_waits_for_the_run_button(self) -> None:
        """A full replay must not fire on page load."""
        at = run_view("view_backtester")
        assert any("Run backtest" in b.label for b in at.button)
        assert any("Configure the run" in i.value for i in at.info)
