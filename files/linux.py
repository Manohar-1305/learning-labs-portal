
import json
import os
import tempfile
import threading
from pathlib import Path

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
    session,
)

import files.ansible as ansible


linux_bp = Blueprint("linux_lab", __name__)

linux_DOCUMENTS_DIR = (
    Path(__file__).resolve().parent.parent / "documents" / "linux"
)

linux_PROGRESS_DIR = (
    Path(__file__).resolve().parent.parent / "instance" / "progress"
)

linux_PROGRESS_LOCK = threading.Lock()

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".html",
    ".htm",
    ".md",
    ".txt",
}


def linux_creation_script(ctx):
    header = (
        f"NETWORK={ctx.network}\n"
        f"NAME={ctx.name('linux-server')}\n"
    )

    return header + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

if ! docker image inspect ubuntu:24.04 >/dev/null 2>&1; then
    docker pull ubuntu:24.04 >/dev/null
fi

if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    docker run -d \
        --name "$NAME" \
        --hostname linux-server \
        --network "$NETWORK" \
        --network-alias linux-server \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" "$NAME" >/dev/null 2>&1 || true
fi

docker start "$NAME" >/dev/null

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)

[ "$running" = true ] || {
    echo "linux container did not start." >&2
    exit 1
}

echo "linux container is running."
"""


def linux_post_setup(ctx):
    header = f"NAME={ctx.name('linux-server')}\n"

    script = header + r"""set -Eeuo pipefail

docker exec -d "$NAME" bash -lc '
    export DEBIAN_FRONTEND=noninteractive

    if command -v jq >/dev/null 2>&1 &&
       command -v vim >/dev/null 2>&1 &&
       command -v ping >/dev/null 2>&1; then
        exit 0
    fi

    apt-get update -qq

    apt-get install -y -qq \
        coreutils \
        findutils \
        grep \
        sed \
        gawk \
        curl \
        wget \
        vim \
        nano \
        less \
        procps \
        iproute2 \
        iputils-ping \
        dnsutils \
        net-tools \
        tar \
        gzip \
        unzip \
        jq
'

echo "linux container is ready. Package installation is running in the background."
"""

    return ansible.run_remote_script(script, timeout=30)


def get_linux_documents():
    documents = []

    if not linux_DOCUMENTS_DIR.is_dir():
        return documents

    for file_path in sorted(
        linux_DOCUMENTS_DIR.iterdir(),
        key=lambda path: path.name.lower(),
    ):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        documents.append({
            "name": file_path.name,
            "title": file_path.stem.replace("_", " ").replace("-", " ").title(),
            "content": content,
        })

    return documents


def get_linux_user():
    username = session.get("username") or session.get("user")

    if isinstance(username, dict):
        username = (
            username.get("username")
            or username.get("email")
            or username.get("id")
        )

    if username is None:
        return None

    username = str(username).strip()

    return username or None


def get_linux_progress_file(username):
    safe_username = str(username)
    filename = f"{safe_username.encode('utf-8').hex()}.json"
    return linux_PROGRESS_DIR / filename


def read_linux_completed_documents(username):
    progress_file = get_linux_progress_file(username)

    if not progress_file.is_file():
        return []

    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        completed = data.get("completed", [])

        if not isinstance(completed, list):
            return []

        return completed

    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def save_linux_completed_documents(username, completed):
    linux_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)

    progress_file = get_linux_progress_file(username)

    payload = {
        "completed": completed,
    }

    temporary_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=linux_PROGRESS_DIR,
            delete=False,
            suffix=".tmp",
        ) as file:
            json.dump(payload, file, indent=2)
            temporary_file = file.name

        os.replace(temporary_file, progress_file)

    finally:
        if temporary_file and os.path.exists(temporary_file):
            os.remove(temporary_file)


def build_linux_progress(username):
    documents = get_linux_documents()

    with linux_PROGRESS_LOCK:
        completed_names = read_linux_completed_documents(username)

    document_names = [doc["name"] for doc in documents]

    completed_names = [
        name for name in completed_names
        if name in document_names
    ]

    completed_set = set(completed_names)

    current_document = None

    for doc in documents:
        if doc["name"] not in completed_set:
            current_document = doc["name"]
            break

    progress_documents = []

    for doc in documents:
        if doc["name"] in completed_set:
            status = "completed"
        elif doc["name"] == current_document:
            status = "current"
        else:
            status = "locked"

        progress_documents.append({
            "name": doc["name"],
            "title": doc["title"],
            "status": status,
            "has_check": False,
        })

    total = len(documents)
    completed_count = len(completed_set)

    percent = (
        round((completed_count / total) * 100)
        if total
        else 0
    )

    return {
        "success": True,
        "documents": progress_documents,
        "current": current_document,
        "completed": completed_count,
        "total": total,
        "percent": percent,
        "complete": total > 0 and completed_count == total,
    }


# Ensure Linux is available to the shared lab helpers and dashboard.
ansible.LABS.setdefault(
    "linux",
    {
        "title": "Linux Server",
        "description": "Linux hands-on practice environment",
    },
)

ansible.register_lab(
    "linux",
    linux_creation_script,
    post_setup=linux_post_setup,
)


@linux_bp.route("/linux")
def linux_page():
    return render_template(
        "linux.html",
        host_ip=ansible.VM_HOST_IP,
        labs=ansible.LABS,
        LABS=ansible.LABS,
        networks=ansible.NETWORKS,
        current_lab="linux",
        slug="linux",
        lab=ansible.LABS["linux"],
    )


@linux_bp.get("/api/linux/documents")
def linux_documents():
    try:
        return jsonify({
            "success": True,
            "documents": get_linux_documents(),
        })
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@linux_bp.get("/api/linux/progress")
def linux_progress():
    username = get_linux_user()

    if not username:
        return jsonify({
            "success": False,
            "error": "User session not found. Please log in again.",
        }), 401

    try:
        return jsonify(build_linux_progress(username))
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@linux_bp.post("/api/linux/progress/complete")
def linux_progress_complete():
    username = get_linux_user()

    if not username:
        return jsonify({
            "success": False,
            "error": "User session not found. Please log in again.",
        }), 401

    try:
        payload = request.get_json(silent=True) or {}
        requested_name = payload.get("name")

        if not requested_name:
            return jsonify({
                "success": False,
                "error": "Document name is required.",
            }), 400

        documents = get_linux_documents()
        document_names = [doc["name"] for doc in documents]

        if requested_name not in document_names:
            return jsonify({
                "success": False,
                "error": "Invalid document.",
            }), 404

        with linux_PROGRESS_LOCK:
            completed = read_linux_completed_documents(username)

            completed = [
                name for name in completed
                if name in document_names
            ]

            current_document = next(
                (
                    name for name in document_names
                    if name not in completed
                ),
                None,
            )

            if current_document is None:
                already_complete = True
            elif requested_name != current_document:
                return jsonify({
                    "success": False,
                    "error": "Complete the current document before proceeding.",
                }), 403
            else:
                already_complete = False
                completed.append(requested_name)
                save_linux_completed_documents(username, completed)

        return jsonify(build_linux_progress(username))

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@linux_bp.post("/api/linux/progress/reset")
def linux_progress_reset():
    username = get_linux_user()

    if not username:
        return jsonify({
            "success": False,
            "error": "User session not found. Please log in again.",
        }), 401

    try:
        with linux_PROGRESS_LOCK:
            save_linux_completed_documents(username, [])

        return jsonify(build_linux_progress(username))

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@linux_bp.get("/api/linux/status")
def linux_status():
    try:
        return jsonify(ansible.lab_status("linux"))
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@linux_bp.post("/api/linux/create")
def linux_create():
    return ansible.start_lab_creation("linux")


@linux_bp.post("/api/linux/stop")
def linux_stop():
    return ansible.stop_lab("linux")


@linux_bp.post("/api/linux/destroy")
def linux_destroy():
    return ansible.destroy_lab_route_for("linux")


@linux_bp.post("/api/linux/containers/<container>/start")
def linux_container_start(container):
    if container != "linux-server":
        return jsonify({
            "success": False,
            "error": "Invalid linux host.",
        }), 404

    return ansible.container_action("linux", container, "start")


@linux_bp.post("/api/linux/containers/<container>/stop")
def linux_container_stop(container):
    if container != "linux-server":
        return jsonify({
            "success": False,
            "error": "Invalid linux host.",
        }), 404

    return ansible.container_action("linux", container, "stop")


@linux_bp.delete("/api/linux/containers/<container>")
def linux_container_delete(container):
    if container != "linux-server":
        return jsonify({
            "success": False,
            "error": "Invalid linux host.",
        }), 404

    return ansible.container_action("linux", container, "delete")


@linux_bp.post("/api/linux/terminal/open")
def linux_terminal_open():
    return ansible.terminal_open_for_lab("linux")


ansible.app.register_blueprint(linux_bp)
