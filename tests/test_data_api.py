from fastapi import FastAPI
from fastapi.testclient import TestClient
import data_api
import run_store


def test_data_download_export_and_retention_validation(tmp_path):
    run_store.init_db()
    run_id = run_store.create_run("keep this active", "p", str(tmp_path), "test")
    app = FastAPI()
    app.include_router(data_api.router)
    with TestClient(app) as client:
        archive = client.post("/api/data/backup")
        assert archive.status_code == 200
        download = client.get(archive.json()["download"])
        assert download.status_code == 200 and download.content[:2] == b"PK"
        exported = client.post("/api/data/export").json()
        assert run_id in client.get(exported["download"]).text
        assert client.get("/api/data/files/runtime.pid").status_code == 404
        assert client.post("/api/data/retention", json={"days": 0}).status_code == 422
        preview = client.post("/api/data/retention", json={"days": 1}).json()
        assert preview["deleted"] == 0 and "backup" not in preview
        applied = client.post("/api/data/retention", json={"days": 1, "apply": True}).json()
        assert applied["deleted"] == 0 and applied["backup"]
        assert run_store.get_run(run_id)["status"] == "queued"
