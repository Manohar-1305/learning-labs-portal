
import http.client
import shlex
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    session,
)

import files.ansible as ansible

jenkins_bp = Blueprint("jenkins_lab", __name__)


def jenkins_creation_script(ctx):
    uid = ctx.uid
    prefix = f"/jenkins/u{uid}"

    header = (
        f"NETWORK={shlex.quote(ctx.network)}\n"
        f"NAME={shlex.quote(ctx.name('jenkins-server'))}\n"
        f"PORT={shlex.quote(str(ctx.web_port))}\n"
        f"PREFIX={shlex.quote(prefix)}\n"
    )

    return header + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

IMAGE=jenkins-systemd:lts

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

echo "Building Jenkins image with systemd support..."

BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

cat > "$BUILD_DIR/Dockerfile" <<'DOCKERFILE'
FROM jenkins/jenkins:lts

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends systemd systemd-sysv dbus sudo curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN echo "jenkins ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/jenkins && \
    chmod 440 /etc/sudoers.d/jenkins

STOPSIGNAL SIGRTMIN+3

ENTRYPOINT ["/sbin/init"]
DOCKERFILE

docker build -t "$IMAGE" "$BUILD_DIR"

if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "Removing existing Jenkins container..."
    docker rm -f "$NAME" >/dev/null
fi

echo "Creating Jenkins container with systemd..."

docker run -d \
    --name "$NAME" \
    --hostname jenkins-server \
    --network "$NETWORK" \
    --network-alias jenkins-server \
    --restart unless-stopped \
    --privileged \
    --cgroupns=host \
    --tmpfs /run \
    --tmpfs /run/lock \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    -p "$PORT":8080 \
    -e JENKINS_OPTS="--prefix=$PREFIX" \
    "$IMAGE" >/dev/null

echo "Waiting for systemd..."

systemd_ready=false

for i in $(seq 1 60); do
    running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)

    if [ "$running" != "true" ]; then
        echo "Jenkins container stopped before systemd became ready." >&2
        docker logs "$NAME" 2>&1 || true
        exit 1
    fi

    if docker exec "$NAME" systemctl show-environment >/dev/null 2>&1; then
        systemd_ready=true
        break
    fi

    sleep 2
done

if [ "$systemd_ready" != "true" ]; then
    echo "systemd did not become ready inside the Jenkins container." >&2
    docker logs "$NAME" 2>&1 || true
    exit 1
fi

echo "Configuring Jenkins systemd service..."

docker exec -i "$NAME" bash -s <<'JENKINS_SETUP'
set -Eeuo pipefail

systemctl unmask jenkins >/dev/null 2>&1 || true
rm -f /etc/systemd/system/jenkins.service
rm -f /etc/systemd/system/multi-user.target.wants/jenkins.service

if [ ! -f /usr/local/bin/jenkins.sh ]; then
    echo "Jenkins startup script /usr/local/bin/jenkins.sh was not found." >&2
    exit 1
fi

JAVA_BIN="$(command -v java || true)"

if [ -z "$JAVA_BIN" ]; then
    echo "Java is not available inside the Jenkins image." >&2
    exit 1
fi

JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$JAVA_BIN")")")"
JENKINS_PATH="$JAVA_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

cat > /etc/systemd/system/jenkins.service <<EOF
[Unit]
Description=Jenkins Automation Server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=jenkins
Group=jenkins
WorkingDirectory=/var/jenkins_home
Environment=JENKINS_HOME=/var/jenkins_home
Environment=JAVA_HOME=$JAVA_HOME
Environment=PATH=$JENKINS_PATH
ExecStart=/usr/bin/bash /usr/local/bin/jenkins.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=300
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

chown root:root /etc/systemd/system/jenkins.service
chmod 644 /etc/systemd/system/jenkins.service

systemctl daemon-reload
systemctl enable jenkins
systemctl restart jenkins
JENKINS_SETUP

echo "Waiting for Jenkins service..."

jenkins_ready=false

for i in $(seq 1 150); do
    running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)

    if [ "$running" != "true" ]; then
        echo "Jenkins container stopped while Jenkins was starting." >&2
        docker logs "$NAME" 2>&1 || true
        exit 1
    fi

    if docker exec "$NAME" systemctl is-active --quiet jenkins; then
        jenkins_ready=true
        break
    fi

    sleep 2
done

if [ "$jenkins_ready" != "true" ]; then
    echo "Jenkins service failed to start." >&2
    docker exec "$NAME" systemctl status jenkins --no-pager -l || true
    docker exec "$NAME" journalctl -u jenkins -n 100 --no-pager || true
    docker logs "$NAME" 2>&1 || true
    exit 1
fi

echo "Jenkins server is running."
echo "Systemd is running as PID 1."
echo "systemctl is available inside the container."
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
                relative_name = file_path.relative_to(
                    documents_dir
                ).as_posix()

                documents.append({
                    "name": relative_name,
                    "title": file_path.stem.replace(
                        "_", " "
                    ).replace("-", " ").title(),
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
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


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


def proxy_jenkins(uid, path=""):
    current_uid = session.get("user_id")

    if current_uid is None:
        abort(401)

    try:
        current_uid = int(current_uid)
    except (TypeError, ValueError):
        abort(401)

    if current_uid != uid:
        abort(403)

    connection = None

    try:
        ctx = ansible.LabContext("jenkins", uid)

        host = ansible.VM_HOST_IP
        port = ctx.web_port

        if not host or not port:
            return "Jenkins backend is not configured.", 502

        connection = http.client.HTTPConnection(
            host,
            port,
            timeout=60,
        )

        prefix = f"/jenkins/u{uid}"

        upstream_path = prefix + "/"

        if path:
            upstream_path += path

        if request.query_string:
            upstream_path += "?" + request.query_string.decode("latin-1")

        excluded_request_headers = {
            "host",
            "connection",
            "content-length",
            "accept-encoding",
        }

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in excluded_request_headers
        }

        headers.update({
            "Host": request.host,
            "X-Forwarded-Host": request.host,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Port": "443",
            "X-Forwarded-Prefix": prefix,
            "X-Forwarded-For": request.remote_addr or "",
        })

        connection.request(
            method=request.method,
            url=upstream_path,
            body=request.get_data(),
            headers=headers,
        )

        upstream = connection.getresponse()
        body = upstream.read()

        response_headers = []

        for key, value in upstream.getheaders():
            lower_key = key.lower()

            if lower_key in {
                "connection",
                "transfer-encoding",
                "content-length",
                "keep-alive",
            }:
                continue

            if lower_key == "location":
                backend_origin = f"http://{host}:{port}"

                if value.startswith(backend_origin):
                    value = value[len(backend_origin):]

                if value.startswith("/") and not (
                    value == prefix
                    or value.startswith(prefix + "/")
                ):
                    value = prefix + value

            response_headers.append((key, value))

        return Response(
            body,
            status=upstream.status,
            headers=response_headers,
        )

    except (OSError, http.client.HTTPException, ValueError):
        return (
            "Jenkins backend is unavailable for this user.",
            502,
        )

    finally:
        if connection:
            connection.close()


@jenkins_bp.route(
    "/jenkins/u<uid>",
    defaults={"path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@jenkins_bp.route(
    "/jenkins/u<uid>/",
    defaults={"path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@jenkins_bp.route(
    "/jenkins/u<uid>/<path:path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def jenkins_user_dashboard(uid, path):
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        abort(404)

    return proxy_jenkins(uid, path)


ansible.app.register_blueprint(jenkins_bp)
