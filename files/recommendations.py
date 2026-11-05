from flask import request, session, redirect, url_for, render_template_string, flash
from files.ansible import app
from files.config import get_db, get_cursor


def init_recommendations_table():
    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lab_recommendations (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(150) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    lab_name VARCHAR(200) NOT NULL,
                    technology VARCHAR(150),
                    suggestion TEXT NOT NULL,
                    status VARCHAR(30) DEFAULT 'Pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
    finally:
        conn.close()


SUGGESTION_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Suggest a Lab - Learning Labs</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 35px 15px;
            font-family: Arial, sans-serif;
            background: #f1f5f9;
            color: #17243a;
        }
        .container {
            max-width: 650px;
            margin: 30px auto;
            padding: 30px;
            background: white;
            border-radius: 12px;
            box-shadow: 0 5px 25px rgba(0,0,0,0.07);
        }
        h1 { margin-top: 0; font-size: 25px; }
        .description { color: #64748b; line-height: 1.6; }
        label {
            display: block;
            margin: 20px 0 7px;
            font-weight: 600;
            font-size: 14px;
        }
        input, textarea {
            width: 100%;
            padding: 12px;
            border: 1px solid #cbd5e1;
            border-radius: 7px;
            font: inherit;
        }
        textarea { min-height: 130px; resize: vertical; }
        button {
            margin-top: 22px;
            padding: 12px 22px;
            border: none;
            border-radius: 7px;
            background: #0f766e;
            color: white;
            font-weight: 700;
            cursor: pointer;
        }
        button:hover { background: #115e59; }
        .back {
            display: inline-block;
            margin-top: 20px;
            color: #0f766e;
            text-decoration: none;
        }
        .message {
            padding: 12px;
            margin-bottom: 15px;
            border-radius: 6px;
            background: #ecfdf5;
            color: #166534;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Suggest a Lab</h1>
        <p class="description">
            Have an idea for another hands-on learning environment?
            Recommend a technology or lab that you would like to see
            on this platform.
        </p>

        {% with messages = get_flashed_messages() %}
            {% for message in messages %}
                <div class="message">{{ message }}</div>
            {% endfor %}
        {% endwith %}

        <form method="POST">
            <label for="lab_name">Lab name *</label>
            <input
                type="text"
                id="lab_name"
                name="lab_name"
                maxlength="200"
                placeholder="Example: Kubernetes Lab"
                required
            >

            <label for="technology">Technology</label>
            <input
                type="text"
                id="technology"
                name="technology"
                maxlength="150"
                placeholder="Example: Kubernetes, Docker, Terraform"
            >

            <label for="suggestion">Why should we add this lab? *</label>
            <textarea
                id="suggestion"
                name="suggestion"
                maxlength="5000"
                placeholder="Describe the lab and what users should be able to practise..."
                required
            ></textarea>

            <button type="submit">Submit Recommendation</button>
        </form>

        <a class="back" href="/">← Back to Learning Labs</a>
    </div>
</body>
</html>
"""


ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lab Recommendations</title>
    <style>
        body {
            margin: 0;
            padding: 30px;
            font-family: Arial, sans-serif;
            background: #f1f5f9;
            color: #17243a;
        }
        .container {
            max-width: 1100px;
            margin: auto;
            background: white;
            padding: 25px;
            border-radius: 10px;
        }
        h1 { margin-top: 0; }
        .table-wrapper { overflow-x: auto; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 13px;
            border-bottom: 1px solid #e2e8f0;
            text-align: left;
            vertical-align: top;
        }
        th { background: #f8fafc; }
        .status {
            color: #a16207;
            font-weight: 700;
        }
        .approved {
            color: #166534;
            font-weight: 700;
        }
        .back {
            display: inline-block;
            margin-top: 20px;
            color: #0f766e;
            text-decoration: none;
        }
        .actions {
            display: flex;
            gap: 8px;
        }
        .actions form { margin: 0; }
        .actions button {
            padding: 8px 12px;
            border: none;
            border-radius: 5px;
            color: white;
            font-weight: 600;
            cursor: pointer;
        }
        .approve-btn { background: #15803d; }
        .approve-btn:hover { background: #166534; }
        .remove-btn { background: #dc2626; }
        .remove-btn:hover { background: #b91c1c; }
        .message {
            padding: 12px;
            margin-bottom: 15px;
            border-radius: 6px;
            background: #ecfdf5;
            color: #166534;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Lab Recommendations</h1>
        <p>Total recommendations: {{ recommendations|length }}</p>

        {% with messages = get_flashed_messages() %}
            {% for message in messages %}
                <div class="message">{{ message }}</div>
            {% endfor %}
        {% endwith %}

        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Lab</th>
                        <th>Technology</th>
                        <th>Suggestion</th>
                        <th>Submitted By</th>
                        <th>Email</th>
                        <th>Status</th>
                        <th>Date</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in recommendations %}
                    <tr>
                        <td>{{ item.id }}</td>
                        <td>{{ item.lab_name }}</td>
                        <td>{{ item.technology or '-' }}</td>
                        <td>{{ item.suggestion }}</td>
                        <td>{{ item.username }}</td>
                        <td>{{ item.email }}</td>
                        <td class="{{ 'approved' if item.status == 'Approved' else 'status' }}">
                            {{ item.status }}
                        </td>
                        <td>{{ item.created_at }}</td>
                        <td>
                            <div class="actions">
                                {% if item.status != 'Approved' %}
                                <form
                                    method="POST"
                                    action="{{ url_for('approve_recommendation', recommendation_id=item.id) }}"
                                >
                                    <button class="approve-btn" type="submit">
                                        Approve
                                    </button>
                                </form>
                                {% endif %}

                                <form
                                    method="POST"
                                    action="{{ url_for('remove_recommendation', recommendation_id=item.id) }}"
                                    onsubmit="return confirm('Are you sure you want to remove this recommendation?');"
                                >
                                    <button class="remove-btn" type="submit">
                                        Remove
                                    </button>
                                </form>
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="9">No recommendations submitted yet.</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <a class="back" href="/">← Back to Learning Labs</a>
    </div>
</body>
</html>
"""


@app.route("/suggest-lab", methods=["GET", "POST"])
def suggest_lab():
    username = session.get("username") or session.get("user")
    email = session.get("email")

    if not username or not email:
        return redirect(url_for("login", next="/suggest-lab"))

    if request.method == "POST":
        lab_name = request.form.get("lab_name", "").strip()
        technology = request.form.get("technology", "").strip()
        suggestion = request.form.get("suggestion", "").strip()

        if not lab_name or not suggestion:
            flash("Lab name and suggestion are required.")
            return redirect(url_for("suggest_lab"))

        if len(lab_name) > 200 or len(technology) > 150 or len(suggestion) > 5000:
            flash("One or more fields exceed the allowed length.")
            return redirect(url_for("suggest_lab"))

        init_recommendations_table()

        conn = get_db()

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lab_recommendations
                    (username, email, lab_name, technology, suggestion)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    username,
                    email,
                    lab_name,
                    technology or None,
                    suggestion
                ))

            conn.commit()
            flash("Your lab recommendation has been submitted successfully.")

        except Exception:
            conn.rollback()
            app.logger.exception("Failed to save lab recommendation")
            flash("Unable to submit your recommendation. Please try again.")

        finally:
            conn.close()

        return redirect(url_for("suggest_lab"))

    return render_template_string(SUGGESTION_TEMPLATE)


@app.route("/admin/recommendations")
def view_recommendations():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    init_recommendations_table()

    conn = get_db()

    try:
        with get_cursor(conn) as cur:
            cur.execute("""
                SELECT id, username, email, lab_name, technology,
                       suggestion, status, created_at
                FROM lab_recommendations
                ORDER BY created_at DESC
            """)
            recommendations = cur.fetchall()
    finally:
        conn.close()

    return render_template_string(
        ADMIN_TEMPLATE,
        recommendations=recommendations
    )


@app.route("/admin/recommendations/<int:recommendation_id>/approve", methods=["POST"])
def approve_recommendation(recommendation_id):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE lab_recommendations
                SET status = 'Approved'
                WHERE id = %s
            """, (recommendation_id,))

        conn.commit()
        flash("Recommendation approved successfully.")

    except Exception:
        conn.rollback()
        app.logger.exception("Failed to approve lab recommendation")
        flash("Unable to approve the recommendation.")

    finally:
        conn.close()

    return redirect(url_for("view_recommendations"))


@app.route("/admin/recommendations/<int:recommendation_id>/remove", methods=["POST"])
def remove_recommendation(recommendation_id):
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM lab_recommendations
                WHERE id = %s
            """, (recommendation_id,))

        conn.commit()
        flash("Recommendation removed successfully.")

    except Exception:
        conn.rollback()
        app.logger.exception("Failed to remove lab recommendation")
        flash("Unable to remove the recommendation.")

    finally:
        conn.close()

    return redirect(url_for("view_recommendations"))
