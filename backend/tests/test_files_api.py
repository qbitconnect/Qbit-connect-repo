"""File API tests: upload/list/download/delete/stats + traversal security (Brief §22, §24)."""

from __future__ import annotations


async def test_upload_list_download_roundtrip(client, admin_headers):
    content = b"QBIT export payload \xf0\x9f\x98\x80 binary-safe"
    up = await client.post(
        "/api/v1/files",
        headers=admin_headers,
        params={"category": "EXPORT"},
        files={"upload": ("report-2026.csv", content, "text/csv")},
    )
    assert up.status_code == 201, up.text
    meta = up.json()["data"]
    assert meta["category"] == "EXPORT"
    assert meta["size"] == len(content)
    assert meta["checksum_sha256"]
    assert "path" not in meta  # never leak storage keys to clients
    file_id = meta["id"]

    listing = await client.get("/api/v1/files", headers=admin_headers)
    assert listing.status_code == 200
    assert any(f["id"] == file_id for f in listing.json()["data"])

    dl = await client.get(f"/api/v1/files/{file_id}/download", headers=admin_headers)
    assert dl.status_code == 200
    assert dl.content == content
    assert dl.headers["content-disposition"].startswith("attachment")


async def test_download_streams_from_file_id_not_path(client, admin_headers):
    """Requesting by raw path is impossible — only UUID ids are accepted (Brief §22)."""
    resp = await client.get(
        "/api/v1/files/../../etc/passwd/download", headers=admin_headers
    )
    assert resp.status_code in (403, 404)


async def test_traversal_in_upload_filename_is_sanitized(client, admin_headers):
    up = await client.post(
        "/api/v1/files",
        headers=admin_headers,
        files={"upload": ("../../evil.txt", b"x", "text/plain")},
    )
    assert up.status_code == 201
    name = up.json()["data"]["name"]
    assert ".." not in name
    assert "/" not in name


async def test_upload_category_validation(client, admin_headers):
    resp = await client.post(
        "/api/v1/files",
        headers=admin_headers,
        params={"category": "NOT_A_CATEGORY"},
        files={"upload": ("a.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_viewer_cannot_upload_but_can_list(client, admin_headers, viewer_headers):
    denied = await client.post(
        "/api/v1/files", headers=viewer_headers, files={"upload": ("b.txt", b"y", "text/plain")}
    )
    assert denied.status_code == 403

    admin_up = await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("c.txt", b"z", "text/plain")}
    )
    assert admin_up.status_code == 201
    listing = await client.get("/api/v1/files", headers=viewer_headers)
    assert listing.status_code == 200
    assert any(f["name"] == "c.txt" for f in listing.json()["data"])


async def test_viewer_cannot_download(client, admin_headers, viewer_headers):
    up = await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("d.txt", b"secret", "text/plain")}
    )
    file_id = up.json()["data"]["id"]
    denied = await client.get(f"/api/v1/files/{file_id}/download", headers=viewer_headers)
    assert denied.status_code == 403


async def test_delete_file_flow(client, admin_headers):
    up = await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("doomed.txt", b"bye", "text/plain")}
    )
    file_id = up.json()["data"]["id"]
    deleted = await client.delete(f"/api/v1/files/{file_id}", headers=admin_headers)
    assert deleted.status_code == 204

    gone = await client.get(f"/api/v1/files/{file_id}/download", headers=admin_headers)
    assert gone.status_code == 404

    again = await client.delete(f"/api/v1/files/{file_id}", headers=admin_headers)
    assert again.status_code == 404


async def test_storage_stats_endpoint(client, admin_headers):
    await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("s.txt", b"stats", "text/plain")}
    )
    resp = await client.get("/api/v1/files/stats", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_files"] >= 1
    assert "per_directory" in data
    # Exposes app directories only — never the whole server filesystem
    assert "root" in data and "/etc" not in str(data)


async def test_download_unknown_id_404(client, admin_headers):
    resp = await client.get(
        "/api/v1/files/00000000-0000-0000-0000-000000000000/download", headers=admin_headers
    )
    assert resp.status_code == 404
