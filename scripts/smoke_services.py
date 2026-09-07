"""Exercise two actual HTTP processes with temporary databases and credentials.

Run from the platform root. Optional --browser checks the built Vue UI with Edge.
Neither project imports the other's source. No real model calls are made.
"""

import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import httpx


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-root", type=Path, default=Path(__file__).resolve().parents[2] / "Opsark_knowledge")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    admin = Path(__file__).resolve().parents[1]
    knowledge = args.knowledge_root.resolve()
    py = knowledge / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        raise SystemExit("Install the knowledge project in its own .venv first.")
    processes = []
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with tempfile.TemporaryDirectory(prefix="opsark-smoke-") as tmp:
        a_port, k_port = port(), port()
        service_key, password = secrets.token_urlsafe(40), secrets.token_urlsafe(24)
        common = {
            **os.environ,
            "KNOWLEDGE_SERVICE_TOKEN": service_key,
            "EMBEDDING_BASE_URL": "",
            "EMBEDDING_API_KEY": "",
            "EMBEDDING_MODEL": "",
        }
        ae = {
            **common,
            "DATABASE_URL": "sqlite:///" + str(Path(tmp) / "platform.db"),
            "KNOWLEDGE_URL": f"http://127.0.0.1:{k_port}",
            "KNOWLEDGE_PUBLIC_URL": f"http://127.0.0.1:{k_port}/api/v1",
            "SMOKE_PASSWORD": password,
            "ALLOWED_ORIGINS": f"http://127.0.0.1:{a_port}",
            "COOKIE_SECURE": "false",
        }
        ke = {**common, "DATABASE_URL": "sqlite:///" + str(Path(tmp) / "knowledge.db")}

        def run(command, cwd, env):
            subprocess.run(command, cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL, **flags)

        def start(command, cwd, env):
            p = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **flags
            )
            processes.append(p)

        try:
            run([sys.executable, "-m", "alembic", "upgrade", "head"], admin, ae)
            run([str(py), "-m", "alembic", "upgrade", "head"], knowledge, ke)
            run(
                [
                    sys.executable,
                    "-c",
                    "import os; from app.db import SessionLocal; from app.models import Admin; from argon2 import PasswordHasher; s=SessionLocal(); s.add(Admin(username='smoke',password_hash=PasswordHasher().hash(os.environ['SMOKE_PASSWORD']))); s.commit(); s.close()",
                ],
                admin,
                ae,
            )
            start(
                [str(py), "-m", "uvicorn", "knowledge.main:app", "--port", str(k_port), "--host", "127.0.0.1"],
                knowledge,
                ke,
            )
            start(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(a_port), "--host", "127.0.0.1"],
                admin,
                ae,
            )
            start([str(py), "-m", "knowledge.processing"], knowledge, ke)
            with httpx.Client(timeout=3, trust_env=False) as client:
                for endpoint in (f"http://127.0.0.1:{a_port}", f"http://127.0.0.1:{k_port}"):
                    for _ in range(60):
                        try:
                            if client.get(endpoint + "/health/live").status_code == 200:
                                break
                        except httpx.RequestError:
                            pass
                        time.sleep(0.2)
                    else:
                        failed = [p for p in processes if p.poll() is not None]
                        details = "\n".join(p.stderr.read()[-3000:] for p in failed)
                        raise RuntimeError("Service did not start: " + endpoint + "\n" + details)
                ap = f"http://127.0.0.1:{a_port}/api/admin/v1"
                kp = f"http://127.0.0.1:{k_port}/api/v1"

                def checked(response):
                    response.raise_for_status()
                    return response.json()

                login = checked(client.post(ap + "/session", json={"username": "smoke", "password": password}))
                csrf = {"X-CSRF-Token": login["csrf"]}
                kb = checked(client.post(ap + "/knowledge/knowledge-bases", headers=csrf, json={"name": "联调知识库"}))
                key = checked(
                    client.post(
                        ap + "/knowledge/knowledge-keys",
                        headers=csrf,
                        json={
                            "name": "smoke",
                            "installation_id": "smoke-device",
                            "knowledge_base_ids": [kb["id"]],
                            "scopes": ["records:write", "records:read", "knowledge:read"],
                        },
                    )
                )
                auth = {"Authorization": "Bearer " + key["api_key"]}
                record = {
                    "schema_version": "1.0",
                    "source_record_id": "smoke-1",
                    "source_revision": 1,
                    "knowledge_base_id": kb["id"],
                    "record_type": "task_result",
                    "title": "Nginx 配置检查",
                    "occurred_at": "2026-09-07T00:00:00Z",
                    "problem": "Nginx 语法检查",
                    "steps": [],
                    "outcome": {"status": "partial", "summary": "配置通过，尚未重载"},
                    "redaction": {"client_applied": True, "ruleset_version": "v1"},
                }
                checked(client.post(kp + "/records", headers={**auth, "Idempotency-Key": "smoke-1"}, json=record))
                for _ in range(50):
                    docs = checked(client.get(ap + "/knowledge/documents"))
                    if docs:
                        break
                    time.sleep(0.2)
                assert docs, "Worker did not produce a draft"
                d = docs[0]
                checked(
                    client.post(
                        ap + f"/knowledge/documents/{d['id']}/publish", headers=csrf, json={"revision": d["revision"]}
                    )
                )
                for _ in range(50):
                    result = checked(
                        client.post(
                            kp + "/knowledge/search",
                            headers=auth,
                            json={"query": "Nginx", "knowledge_base_ids": [kb["id"]]},
                        )
                    )
                    if result["hits"]:
                        break
                    time.sleep(0.2)
                assert result["hits"], "Published knowledge not found"
                if args.browser:
                    run(
                        ["node", "scripts/smoke-browser.mjs"],
                        admin / "web",
                        {**ae, "SMOKE_URL": f"http://127.0.0.1:{a_port}"},
                    )
                checked(client.post(ap + f"/knowledge/documents/{d['id']}/unpublish", headers=csrf))
                assert not checked(
                    client.post(
                        kp + "/knowledge/search",
                        headers=auth,
                        json={"query": "Nginx", "knowledge_base_ids": [kb["id"]]},
                    )
                )["hits"]
                print(
                    "PASS: independent HTTP services, auth, upload, worker, publish, search, unpublish"
                    + (", browser" if args.browser else "")
                )
        finally:
            for p in reversed(processes):
                if os.name == "nt" and p.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
                elif p.poll() is None:
                    p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                if p.stderr:
                    p.stderr.close()
            time.sleep(0.3)


if __name__ == "__main__":
    main()
