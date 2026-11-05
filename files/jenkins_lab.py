import shlex
from pathlib import Path

from flask import Blueprint, jsonify, render_template

import files.ansible as ansible

jenkins_bp = Blueprint("jenkins_lab", __name__)


def jenkins_creation_script(ctx):
    header = (
        f"NETWORK={shlex.quote(ctx.network)}\n"
        f"NAME={shlex.quote(ctx.name('jenkins-server'))}\n"
        f"PORT={shlex.quote(str(ctx.web_port))}\n"
    )

    return header + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

IMAGE=jenkins/jenkins:lts

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

docker image inspect "$IMAGE" >/dev/null 2>&1 || \
    docker pull "$IMAGE" >/dev/null

if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    docker run -d \
        --name "$NAME" \
        --hostname jenkins-server \
        --network "$NETWORK" \
        --network-alias jenkins-server \
        --restart unless-stopped \
        -p "$PORT":8080 \
        "$IMAGE" >/dev/null
else
    docker network connect "$NETWORK" "$NAME" >/dev/null 2>&1 || true
fi

docker start "$NAME" >/dev/null

echo "Configuring root access inside Jenkins container..."

docker exec -u root "$NAME" bash -lc '
    apt-get update -qq
    apt-get install -y -qq sudo

    echo "jenkins ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/jenkins
    chmod 440 /etc/sudoers.d/jenkins
'

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)

[ "$running" = true ] || {
    echo "Jenkins container did not start." >&2
    exit 1
}

echo "Jenkins server is running."
echo "Root access is configured."
"""


ansible.register_lab("jenkins", jenkins_creation_script)


@jenkins_bp.route("/jenkins")
def jenkins_page():
    return ansible.render_lab_template("jenkins.html", "jenkins")


@jenkins_bp.route("/jenkins/document")
def jenkins_document_page():
    return render_template(
        "document.html",
        host_ip=ansible.VM_HOST_IP,
        labs=ansible.LABS,
        LABS=ansible.LABS,
        networks=ansible.NETWORKS,
        current_lab="jenkins",
        slug="jenkins",
    )


@jenkins_bp.get("/api/jenkins/documents")
def jenkins_documents():
    documents_dir = (
        Path(__file__).resolve().parent.parent
        / "documents"
        / "jenkins"
    )

    supported_extensions = {
        ".md",
        ".html",
        ".htm",
        ".txt",
        ".yaml",
        ".yml",
        ".sh",
        ".py",
        ".groovy",
        ".json",
        ".xml",
    }

    documents = []

    if documents_dir.is_dir():
        for file_path in sorted(documents_dir.rglob("*")):
            if not file_path.is_file():
                continue

            if file_path.suffix.lower() not in supported_extensions:
                continue

            try:
                relative_name = file_path.relative_to(documents_dir).as_posix()

                documents.append({
                    "name": relative_name,
                    "title": file_path.stem.replace("_", " ").replace("-", " ").title(),
                    "content": file_path.read_text(encoding="utf-8"),
                })

            except (OSError, UnicodeDecodeError):
                continue

    return jsonify({
        "success": True,
        "documents": documents,
    })


@jenkins_bp.get("/api/jenkins/status")
def jenkins_status():
    try:
        return jsonify(ansible.lab_status("jenkins"))
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@jenkins_bp.post("/api/jenkins/create")
def jenkins_create():
    return ansible.start_lab_creation("jenkins")


@jenkins_bp.post("/api/jenkins/stop")
def jenkins_stop():
    return ansible.stop_lab("jenkins")


@jenkins_bp.post("/api/jenkins/destroy")
def jenkins_destroy():
    return ansible.destroy_lab_route_for("jenkins")


@jenkins_bp.post("/api/jenkins/containers/<container>/start")
def jenkins_container_start(container):
    return ansible.container_action("jenkins", container, "start")


@jenkins_bp.post("/api/jenkins/containers/<container>/stop")
def jenkins_container_stop(container):
    return ansible.container_action("jenkins", container, "stop")


@jenkins_bp.delete("/api/jenkins/containers/<container>")
def jenkins_container_delete(container):
    return ansible.container_action("jenkins", container, "delete")


@jenkins_bp.post("/api/jenkins/terminal/open")
def jenkins_terminal_open():
    return ansible.terminal_open_for_lab("jenkins")


ansible.app.register_blueprint(jenkins_bp)
