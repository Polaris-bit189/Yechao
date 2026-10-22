"""PWA 服务端契约：manifest / sw.js 的 MIME 与缓存头。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：Service Worker 的更新检查默认会受 HTTP 缓存影响；而
`manifest.webmanifest` 的 MIME 若不是 `application/manifest+json`，
部分浏览器会忽略它。两者都不是「跑起来才知道」的事 —— 可以在一台
没有网络的测试机上验。

**判据**：

· `/sw.js` 必须 `Cache-Control: no-cache` —— 否则浏览器可能缓存旧 SW，
  新版本永远装不上。
· `Service-Worker-Allowed: /` 让 SW 的 scope 覆盖根路径（`base: './'` 下
  注册时 scope 是 `./`，这个头是显式的兜底）。
· `/` 与 `/index.html` 必须 `no-cache` —— 否则发版后页面仍指向旧哈希资源，
  SW 的更新提示也就无从谈起。
· 缺 `dist` 时仍然回 501，不回落成 404 或空页。

**怎么验证**：用一个临时 `web/dist` 挂上真实的路由，再用 TestClient 读
响应头。不依赖仓库里是否已经构建。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yechao.interfaces import api as api_module


def _client(tmp_path, *, build: bool = True) -> TestClient:
    dist = tmp_path / "web" / "dist"
    if build:
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>夜抄</title>", encoding="utf-8")
        (dist / "manifest.webmanifest").write_text('{"name":"夜抄"}', encoding="utf-8")
        (dist / "sw.js").write_text("// service worker", encoding="utf-8")

    app = FastAPI()
    fake = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path / "data"))
    api_module._mount_web(app, fake)
    return TestClient(app)


def test_manifest_has_mime_and_no_cache(tmp_path):
    r = _client(tmp_path).get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert r.headers["cache-control"] == "no-cache"


def test_service_worker_has_mime_scope_and_no_cache(tmp_path):
    r = _client(tmp_path).get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["service-worker-allowed"] == "/"


def test_index_is_not_http_cached(tmp_path):
    c = _client(tmp_path)
    for path in ("/", "/index.html"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path


def test_missing_dist_still_returns_explicit_501(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_package_web_dist", lambda: None)
    r = _client(tmp_path, build=False).get("/")
    assert r.status_code == 501
    assert "网页还没构建" in r.text
