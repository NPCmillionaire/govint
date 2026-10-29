from pathlib import Path

import httpx
import pytest
import respx

from govint import watch
from govint.config import Settings
from govint.http import Http
from govint.sources import csl
from govint.sources.base import Ctx


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    s = Settings(contact="t@example.com", data_gov_key="DEMO_KEY", sam_key=None, data_dir=tmp_path)
    return Ctx(settings=s, http=Http(s.user_agent, None))


def _csl(ctx, records):
    import json
    csl.path(ctx).write_text(json.dumps(records))
    csl._load.cache_clear()


def test_keyword_watch_baselines_then_alerts_only_on_new(ctx):
    _csl(ctx, [{"id": "1", "source": "SDN - Treasury", "name": "Old Shipping Co", "programs": ["IRAN"]}])
    watch.add(ctx, "keyword", "iran")
    events, errors = watch.run(ctx)
    assert events == [] and errors == []  # first run is the silent baseline
    _csl(ctx, [{"id": "1", "source": "SDN - Treasury", "name": "Old Shipping Co", "programs": ["IRAN"]},
               {"id": "2", "source": "SDN - Treasury", "name": "New Tanker LLC", "programs": ["IRAN"]},
               {"id": "3", "source": "SDN - Treasury", "name": "Unrelated", "programs": ["CUBA"]}])
    events, _ = watch.run(ctx)
    assert [e.key for e in events] == ["csl:2"]
    assert watch.run(ctx)[0] == []  # already seen


def test_add_validates_kind_and_remove(ctx):
    with pytest.raises(ValueError):
        watch.add(ctx, "satellite", "x")
    it = watch.add(ctx, "ticker", "lmt")
    assert it.value == "LMT"
    assert watch.remove(ctx, it.id) and watch.items(ctx) == []


@respx.mock
def test_ntfy_posts_title_and_click(ctx):
    route = respx.post("https://ntfy.sh/topic123").mock(return_value=httpx.Response(200))
    it = watch.Item(1, "company", "Acme", "Acme", True, {})
    watch.notify_ntfy(ctx, "topic123", watch.Event(it, "SEC", "k", "Acme: 8-K", "https://sec.gov/x"))
    req = route.calls[0].request
    assert req.content == b"Acme: 8-K" and req.headers["Click"] == "https://sec.gov/x"
