from vibe_agent import models as m


def _catalog():
    def info(i, p, c, tools=True, img=False, created=0):
        return m.ModelInfo(i, p, c, tools, img, created)

    return {
        "a/old": info("a/old", 1, 2, created=1),
        "a/new": info("a/new", 0.1, 0.5, img=True, created=9),
        "a/new:batch": info("a/new:batch", 0.05, 0.25, created=9),
        "~a/latest": info("~a/latest", 1, 1, created=10),
        "a/notools": info("a/notools", 1, 1, tools=False, created=8),
        "a/free:free": info("a/free:free", 0, 0, created=5),
        "a/unpriced": m.ModelInfo("a/unpriced", None, None, True, None, 7),
    }


def test_browseable_filters_and_sorts_newest_first():
    assert [x.id for x in m.browseable_models(_catalog())] == ["a/new", "a/free:free", "a/old"]


def test_describe_and_expensive():
    cat = _catalog()
    assert m.describe(cat["a/new"]) == "a/new  ·  $0.10 / $0.50  ·  🖼️"
    assert "무료" in m.describe(cat["a/free:free"])
    assert not m.is_expensive(cat["a/new"]) and m.is_expensive(m.ModelInfo("x", 1, 10, True))
    assert not m.is_expensive(None)


def test_parse_catalog_reads_created_and_images():
    payload = {"data": [{"id": "x/y", "created": 123, "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                         "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text", "image"]}}]}
    info = m.parse_catalog(payload)["x/y"]
    assert (info.created, info.supports_images, info.supports_tools) == (123, True, True)
    assert round(info.prompt_per_m, 6) == 1.0
