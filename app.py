from flask import Flask, render_template, request, redirect, url_for, make_response
from engine.db_models import db, TemplateModel
from engine.template_engine import TemplateEngine
from io import BytesIO
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from datetime import datetime, date
import os
import requests
from flask_sqlalchemy import SQLAlchemy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load variables from a local .env file (OPENAI_API_KEY, OPENAI_MODEL, etc.) if present.
# We point load_dotenv() directly at the .env file next to app.py, rather than letting
# it auto-search, so this always works the same way regardless of how/where the app
# is launched from (terminal, VS Code "Run", VS Code debugger, etc.).
DOTENV_PATH = os.path.join(BASE_DIR, ".env")

try:
    from dotenv import load_dotenv
    dotenv_loaded = load_dotenv(dotenv_path=DOTENV_PATH)
    if not dotenv_loaded:
        print(f"[startup] WARNING: no .env file found at {DOTENV_PATH}")
except ImportError:
    print("[startup] WARNING: python-dotenv is not installed - run 'pip install python-dotenv'. "
          "Falling back to whatever environment variables are already set.")


app = Flask(__name__)

# --- Secret key (session/cookie signing) ---
# Reads FLASK_SECRET_KEY from .env. Falls back to a dev-only key if not set,
# so the app still runs, but you should always set a real one in .env.
app.config['SECRET_KEY'] = os.environ.get(
    "FLASK_SECRET_KEY", "dev-only-insecure-key-change-me"
)

# --- Database location ---
# Reads DATABASE_URL from .env if present; otherwise falls back to the
# original hardcoded instance/app.db path so existing setups keep working.
db_dir = os.path.join(BASE_DIR, 'instance')
os.makedirs(db_dir, exist_ok=True)
default_db_path = os.path.join(db_dir, 'app.db')

database_url = os.environ.get("DATABASE_URL")

if database_url and database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
    # It's a RELATIVE sqlite path (e.g. "sqlite:///instance/app.db"). Resolve it
    # against the project folder itself instead of the current working directory -
    # otherwise it silently breaks depending on where the app happens to be launched from.
    relative_path = database_url.replace("sqlite:///", "", 1)
    absolute_path = os.path.join(BASE_DIR, relative_path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    # sqlite URIs always use forward slashes, even on Windows
    database_url = f"sqlite:///{absolute_path.replace(chr(92), '/')}"

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or f'sqlite:///{default_db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- OpenAI configuration (Feature 3) ---
app.config['OPENAI_API_KEY'] = os.environ.get("OPENAI_API_KEY", "")
app.config['OPENAI_MODEL'] = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

if app.config['OPENAI_API_KEY']:
    masked = app.config['OPENAI_API_KEY'][:7] + "..." + app.config['OPENAI_API_KEY'][-4:]
    print(f"[startup] OPENAI_API_KEY loaded ({masked}), model = {app.config['OPENAI_MODEL']}")
else:
    print("[startup] WARNING: OPENAI_API_KEY was NOT found. "
          f"Checked for a .env file at: {DOTENV_PATH}. "
          "AI subject correction will be skipped until this is fixed.")

db.init_app(app)

engine = TemplateEngine()

with app.app_context():
    db.create_all()


@app.route("/")
def index():
    templates = engine.list_templates()
    return render_template("index.html", templates=templates)


@app.route("/editor/<template_key>", methods=["GET", "POST"])
def editor(template_key):
    template = engine.get_template(template_key)
    if not template:
        return "Template not found", 404

    placeholders = engine.get_placeholders(template_key)
    error = None

    if request.method == "POST":

        data = {field: request.form.get(field, "") for field in placeholders}

        # --- Feature 2: reject any date field that is set in the past ---
        # The auto "date" field (today's date) is filled by the engine itself and is
        # already excluded from `placeholders`, so this only validates user-entered
        # date fields like start_date / end_date / exam_date etc.
        today = date.today()
        for field in placeholders:
            if "date" in field.lower() and data.get(field):
                try:
                    entered_date = datetime.strptime(data[field], "%Y-%m-%d").date()
                    if entered_date < today:
                        error = (
                            f"'{field.replace('_', ' ').title()}' cannot be a past date. "
                            "Please choose today's date or a future date."
                        )
                        break
                except ValueError:
                    # Not a plain YYYY-MM-DD value; skip validation for it rather than crash
                    pass

        if error:
            return render_template(
                "editor.html",
                template=template,
                placeholders=placeholders,
                error=error,
                form_data=data,
            )

        # --- Feature 3: automatically correct/polish the Subject field via OpenAI ---
        # This runs every time "Generate Letter" is clicked - no separate button to
        # remember to press. If OpenAI can't be reached for any reason, we fall back
        # to the user's original subject text and just let them know why.
        ai_notice = None
        if "subject" in data and data["subject"].strip():
            corrected_subject, ai_error = correct_subject_with_ai(data["subject"])
            if corrected_subject:
                data["subject"] = corrected_subject
            elif ai_error:
                ai_notice = (
                    "Note: your subject was used exactly as typed - "
                    f"AI correction was unavailable ({ai_error})."
                )

        letter = engine.generate_letter(template_key, data)
        return render_template("result.html", letter=letter, ai_notice=ai_notice)

    return render_template("editor.html", template=template, placeholders=placeholders, error=None, form_data={})


def correct_subject_with_ai(raw_subject: str):
    """
    Feature 3: Sends the Subject text the user typed to OpenAI, with instructions to
    fix spelling/grammar mistakes and phrase it as a proper formal subject line.

    Returns a tuple (corrected_text, error_message):
      - On success: (corrected_text, None)
      - On any failure (no key, network issue, bad response): (None, "reason")
    Callers should fall back to the user's original text whenever error_message is set,
    so a temporary OpenAI outage never blocks someone from generating their letter.
    """
    raw_subject = (raw_subject or "").strip()
    if not raw_subject:
        return None, "empty subject"

    api_key = app.config.get("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY is not set on the server"

    system_prompt = (
        "You clean up short, informal notes into a single, polished, formal subject "
        "line suitable for an official letter (e.g. to a college department, warden, "
        "registrar, or company). You MUST fix every spelling mistake, typo, and grammar "
        "error in the user's text - never leave a misspelled or garbled word as-is, and "
        "never invent new information that wasn't implied by the original note. "
        "Rules: respond with ONLY the corrected, improved subject line, no quotation "
        "marks, no preamble, no explanation, no trailing punctuation other than a period "
        "if natural, and keep it under 15 words."
    )

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": app.config.get("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": raw_subject},
                ],
                "temperature": 0.3,
                "max_tokens": 60,
            },
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        corrected = result["choices"][0]["message"]["content"].strip().strip('"')

        if not corrected:
            return None, "OpenAI returned an empty response"

        return corrected, None

    except requests.exceptions.RequestException as e:
        return None, f"could not reach OpenAI API ({e})"
    except (KeyError, IndexError):
        return None, "unexpected response format from OpenAI API"
    except Exception as e:
        # Belt-and-braces: whatever goes wrong with the AI call, never let it
        # crash the whole "Generate Letter" request - always fall back safely.
        return None, f"unexpected error ({e})"


@app.route("/add_template", methods=["GET", "POST"])
def add_template():
    if request.method == "POST":
        name = request.form.get("name")
        title = request.form.get("title")
        content = request.form.get("content")
        if name and title and content:
            new_template = TemplateModel(name=name, title=title, content=content)
            db.session.add(new_template)
            db.session.commit()
            return redirect(url_for("index"))
    return render_template("add_template.html")


@app.route("/download_pdf", methods=["POST"])
def download_pdf():
    letter_text = request.form.get("letter_text", "")

    buffer = BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=40, leftMargin=40,
                            topMargin=60, bottomMargin=40)

    styles = getSampleStyleSheet()
    story = []

   
    paragraphs = letter_text.split("\n\n")
    for para in paragraphs:
  
        para = para.replace("\n", "<br/>")
        story.append(Paragraph(para, styles['Normal']))
        story.append(Spacer(1, 12))  

    pdf.build(story)

    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "attachment; filename=letter.pdf"
    return response



if __name__ == "__main__":
    app.run(debug=True)