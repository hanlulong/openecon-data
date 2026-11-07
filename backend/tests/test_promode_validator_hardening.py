"""SecurityValidator quick-win hardening: aliases, low-level modules, link/mode calls.

Two tables: snippets that must now be REJECTED (the checks apply through
import aliases, cover the low-level OS backends and event-loop/datastore
layers, and refuse link/mode-changing methods on any receiver), and ordinary
analysis snippets that must still VALIDATE so Pro Mode keeps working.
"""

import pytest

from backend.services.secure_code_executor import SecurityLevel, SecurityValidator

MUST_REJECT = [
    # os/sys attribute checks through import aliases
    "import os as o\no.system('id')",
    "import os as o\no.popen('id')",
    "import sys as s\nprint(s.modules)",
    "import sys as s\ns.exit(0)",
    "def f():\n    return o.system('id')\nimport os as o",
    "from os import system as s",
    "from os import *",
    "from sys import *",
    # rebinding a wrapper name elsewhere (even in dead code) must never drop
    # the checks on the real wrapper-provided os/sys
    "def _u():\n    import sys as os\nos.system('id')",
    "import sys as os\nimport os\nos.system('id')",
    "def _u():\n    import os as sys\nprint(sys.modules)",
    "import sys as s\nimport os as s\ns.system('id')",
    # a module that re-exports os/sys, imported under any name
    "from pathlib import os as q\nq.system('id')",
    "from pathlib import os\nos.popen('id')",
    "from mylib import sys as z\nz.exit(0)",
    # low-level OS backends + event-loop / raw I/O / network / datastore layers
    "import posix",
    "from posix import system",
    "import nt",
    "import _posixsubprocess",
    "import asyncio",
    "from asyncio import subprocess",
    "import selectors",
    "import ssl",
    "import redis",
    "import redis.asyncio",
    "from redis import Redis",
    "import multiprocessing",
    "import ctypes",
    # link / mode-changing calls on ANY receiver
    "from pathlib import Path\nPath('/tmp/promode_x.csv').symlink_to('/etc/hostname')",
    "from pathlib import Path\nPath('/tmp/promode_x.csv').hardlink_to('/etc/hostname')",
    "from pathlib import Path\nPath('/tmp/a').link_to('/tmp/b')",
    "from pathlib import Path\nPath('/tmp/promode_x.csv').chmod(0o777)",
    "from pathlib import Path\nPath('/tmp/promode_x.csv').lchmod(0o777)",
    "import os.path as p\np.os.chmod('/tmp/x', 0o777)",
    "import os as o\no.chmod('/tmp/x', 0o777)",
    # ...also when only REFERENCED (bound method, higher-order use)
    "from pathlib import Path\nf = Path('/tmp/promode_x.csv').symlink_to\nf('/etc/hostname')",
    "from pathlib import Path\nlist(map(Path.symlink_to, [Path('/tmp/a')], ['/etc/hostname']))",
    "from pathlib import Path\nm = Path.chmod",
]

MUST_VALIDATE = [
    "import pandas as pd\nimport numpy as np\n"
    "df = pd.DataFrame({'y': np.arange(5)})\nprint(df.describe())",
    "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
    "plt.plot([1, 2, 3])\nplt.savefig('/tmp/promode_chart.png')",
    "import httpx\nr = httpx.get('https://api.worldbank.org/v2/country', timeout=20)\nprint(r.status_code)",
    "import datetime, json, math, statistics\n"
    "print(json.dumps({'m': statistics.mean([1, 2]), 's': math.sqrt(4), "
    "'d': datetime.date.today().isoformat()}))",
    "from collections import Counter\nimport itertools, functools, decimal, csv, textwrap\nprint(Counter('ab'))",
    "from pathlib import Path\np = Path('/tmp/promode_data.csv')\nprint(p.suffix, p.exists())",
    "import scipy.stats as st\nprint(st.norm.cdf(0))",
    "import statsmodels.api as sm\nprint(sm.add_constant([1.0, 2.0]))",
    "import seaborn as sns\nsns.set_theme()",
    "import os\nprint(os.path.join('/tmp', 'x.csv'))\nos.makedirs('/tmp/out', exist_ok=True)",
    "import os as o\nprint(o.path.join('/tmp', 'x'))\nprint(o.environ.get('HOME'))",
    "import os.path as osp\nprint(osp.join('/tmp', 'x'))",
    "import sys as s\nprint(s.version)",
    "import pandas as pd\ndf = pd.DataFrame({'a': [1]})\ndf = df.rename(columns={'a': 'b'}).replace(1, 2)\n"
    "df.to_csv('/tmp/promode_out.csv', index=False)",
]


@pytest.mark.parametrize("level", list(SecurityLevel))
@pytest.mark.parametrize("code", MUST_REJECT)
def test_bypass_snippets_are_rejected(code, level):
    ok, violations, _ = SecurityValidator(level).validate(code)
    assert not ok, f"should be rejected at {level.value}: {code!r}"
    assert violations


@pytest.mark.parametrize("code", MUST_VALIDATE)
def test_legitimate_analysis_snippets_still_validate(code):
    ok, violations, _ = SecurityValidator(SecurityLevel.STRICT).validate(code)
    assert ok, f"legit analysis code rejected: {code!r} -> {violations}"


def test_checks_fail_closed_without_validate():
    """The wrapper names resolve to themselves even if a check runs outside
    validate() (no alias pre-pass), so os.system is still flagged."""
    import ast

    v = SecurityValidator()
    v._check_ast(ast.parse("os.system('id')\nsys.exit(0)"))
    assert any("os.system" in m for m in v.violations)
    assert any("sys.exit" in m for m in v.violations)


def test_one_violation_per_dangerous_os_attribute():
    """os.chmod is reported once (as an os operation), not twice."""
    ok, violations, _ = SecurityValidator().validate("os.chmod('/tmp/x', 0o777)")
    assert not ok
    assert len(violations) == 1, violations


def test_alias_state_does_not_leak_between_validations():
    v = SecurityValidator()
    assert not v.validate("import os as q\nprint(q.open)")[0]
    # Same validator, new program: `q` is io here, so q.open is not os.open.
    assert v.validate("import io as q\nprint(q.open)")[0]
