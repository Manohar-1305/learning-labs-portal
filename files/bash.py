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


bash_bp = Blueprint("bash_lab", __name__)

BASH_DOCUMENTS_DIR = (
    Path(__file__).resolve().parent.parent / "documents" / "bash"
)

BASH_PROGRESS_DIR = (
    Path(__file__).resolve().parent.parent / "instance" / "progress"
)

BASH_PROGRESS_LOCK = threading.Lock()

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".html",
    ".htm",
    ".md",
    ".txt",
}


def bash_creation_script(ctx):
    header = (
        f"NETWORK={ctx.network}\n"
        f"NAME={ctx.name('bash-server')}\n"
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
        --hostname bash-server \
        --network "$NETWORK" \
        --network-alias bash-server \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" "$NAME" >/dev/null 2>&1 || true
fi

docker start "$NAME" >/dev/null

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)

[ "$running" = true ] || {
    echo "Bash container did not start." >&2
    exit 1
}

echo "Bash container is running."
"""


def bash_post_setup(ctx):
    header = f"NAME={ctx.name('bash-server')}\n"

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
        bash \
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

echo "Bash container is ready. Package installation is running in the background."
"""

    return ansible.run_remote_script(script, timeout=30)


def get_bash_documents():
    documents = []

    if not BASH_DOCUMENTS_DIR.is_dir():
        return documents

    for file_path in sorted(
        BASH_DOCUMENTS_DIR.iterdir(),
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


def get_bash_user():
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


def get_bash_progress_file(username):
    safe_username = str(username)

    filename = f"{safe_username.encode('utf-8').hex()}.json"

    return BASH_PROGRESS_DIR / filename


def read_bash_completed_documents(username):
    progress_file = get_bash_progress_file(username)

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


def save_bash_completed_documents(username, completed):
    BASH_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)

    progress_file = get_bash_progress_file(username)

    payload = {
        "completed": completed,
    }

    temporary_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=BASH_PROGRESS_DIR,
            delete=False,
            suffix=".tmp",
        ) as file:
            json.dump(payload, file, indent=2)
            temporary_file = file.name

        os.replace(temporary_file, progress_file)

    finally:
        if temporary_file and os.path.exists(temporary_file):
            os.remove(temporary_file)


def build_bash_progress(username):
    documents = get_bash_documents()

    with BASH_PROGRESS_LOCK:
        completed_names = read_bash_completed_documents(username)

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


ansible.register_lab(
    "bash",
    bash_creation_script,
    post_setup=bash_post_setup,
)


@bash_bp.route("/bash")
def bash_page():
    return ansible.render_lab_template("bash.html", "bash")


@bash_bp.route("/bash/document")
def bash_document_page():
    return render_template(
        "document.html",
        host_ip=ansible.VM_HOST_IP,
        labs=ansible.LABS,
        LABS=ansible.LABS,
        networks=ansible.NETWORKS,
        current_lab="bash",
        slug="bash",
    )


@bash_bp.get("/api/bash/documents")
def bash_documents():
    try:
        return jsonify({
            "success": True,
            "documents": get_bash_documents(),
        })
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@bash_bp.get("/api/bash/progress")
def bash_progress():
    username = get_bash_user()

    if not username:
        return jsonify({
            "success": False,
            "error": "User session not found. Please log in again.",
        }), 401

    try:
        return jsonify(build_bash_progress(username))
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@bash_bp.post("/api/bash/progress/complete")
def bash_progress_complete():
    username = get_bash_user()

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

        documents = get_bash_documents()
        document_names = [doc["name"] for doc in documents]

        if requested_name not in document_names:
            return jsonify({
                "success": False,
                "error": "Invalid document.",
            }), 404

        with BASH_PROGRESS_LOCK:
            completed = read_bash_completed_documents(username)

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
                return jsonify(build_bash_progress(username))

            if requested_name != current_document:
                return jsonify({
                    "success": False,
                    "error": "Complete the current document before proceeding.",
                }), 403

            completed.append(requested_name)

            save_bash_completed_documents(username, completed)

        return jsonify(build_bash_progress(username))

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@bash_bp.post("/api/bash/progress/reset")
def bash_progress_reset():
    username = get_bash_user()

    if not username:
        return jsonify({
            "success": False,
            "error": "User session not found. Please log in again.",
        }), 401

    try:
        with BASH_PROGRESS_LOCK:
            save_bash_completed_documents(username, [])

        return jsonify(build_bash_progress(username))

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@bash_bp.get("/api/bash/status")
def bash_status():
    try:
        return jsonify(ansible.lab_status("bash"))
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@bash_bp.post("/api/bash/create")
def bash_create():
    return ansible.start_lab_creation("bash")


@bash_bp.post("/api/bash/stop")
def bash_stop():
    return ansible.stop_lab("bash")


@bash_bp.post("/api/bash/destroy")
def bash_destroy():
    return ansible.destroy_lab_route_for("bash")


@bash_bp.post("/api/bash/containers/<container>/start")
def bash_container_start(container):
    if container != "bash-server":
        return jsonify({
            "success": False,
            "error": "Invalid Bash host.",
        }), 404

    return ansible.container_action("bash", container, "start")


@bash_bp.post("/api/bash/containers/<container>/stop")
def bash_container_stop(container):
    if container != "bash-server":
        return jsonify({
            "success": False,
            "error": "Invalid Bash host.",
        }), 404

    return ansible.container_action("bash", container, "stop")


@bash_bp.delete("/api/bash/containers/<container>")
def bash_container_delete(container):
    if container != "bash-server":
        return jsonify({
            "success": False,
            "error": "Invalid Bash host.",
        }), 404

    return ansible.container_action("bash", container, "delete")


@bash_bp.post("/api/bash/terminal/open")
def bash_terminal_open():
    return ansible.terminal_open_for_lab("bash")


ansible.app.register_blueprint(bash_bp)
