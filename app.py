from flask import Flask, render_template, request, jsonify, send_file
import json
import re
import io
import sqlite3
import os
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
import requests as req_lib

app = Flask(__name__)

# Option 1: Set environment variable GROQ_API_KEY before running
# Option 2: Paste your key directly here between the quotes
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

DB_PATH = os.path.join(os.path.dirname(__file__), "reports.db")


# ─── DATABASE ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                saved_at TEXT NOT NULL,
                date_of_exam TEXT,
                patient_count INTEGER,
                patients_json TEXT NOT NULL
            )
        """)
        conn.commit()


init_db()


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/history_page")
def history_page():
    return render_template("history.html")


@app.route("/parse", methods=["POST"])
def parse_report():
    data = request.json
    raw_text = data.get("text", "")

    prompt = f"""You are a medical report parser and corrector for a radiology/x-ray clinic.

The input may contain ONE or MULTIPLE patients.

The input has two sections:
1. A HEADER LIST at the top: lines like "16632 Lastname,Firstname Age/Sex Examination Reason"
2. REPORT BLOCKS below: each block starts with a (possibly misspelled) last name, followed by findings and impression.

IMPORTANT RULES:
- Match each report block to the correct patient in the header list by fuzzy-matching the last name (e.g. "Saavedra" matches "Sevandra", "Casaliay" matches "Casaljay", "Agsaoy" matches "Agsaoay", "Manase" matches "Menase")
- ALWAYS use the name from the HEADER LIST (not the misspelled name in the report block)
- Use the date from the header if present (e.g. "3/9/2026"), formatted as "MM/DD/YYYY" (e.g. "03/09/2026")
- If no date is in the header, use today's date in MM/DD/YYYY format

Common abbreviation corrections:
- "lf" = "lung field"
- "Hne" = "Heart is not enlarged"
- "Dsi" = "Diaphragm and sinuses are intact"
- "Cxr" or "CXR" = "CHEST PA VIEW"
- "CXR PA/LAT" or "Cxr pa/lat" = "CHEST PA/LAT VIEW"
- "rt" or "rtupper" = "right upper"
- "PTB" = "Pulmonary Tuberculosis"
- "DOB" = "difficulty of breathing"
- "Normal chest" or "normal chest" = expand to EXACTLY:
    report: "Both lungs are clear.\\nHeart is not enlarged.\\nDiaphragm and sinuses are intact."
    impression: "Essentially normal chest."
- Fix ALL spelling errors in medical terms
- Each finding should be a complete, properly punctuated sentence

Raw input:
{raw_text}

Return ONLY a JSON array (even for a single patient). Each element must have these exact fields:
{{
  "case_no": "case number from header",
  "name": "LASTNAME, FIRSTNAME in uppercase from the HEADER LIST",
  "age": "age as number only, e.g. 55",
  "sex": "M or F",
  "date_of_exam": "date in MM/DD/YYYY format",
  "examination": "type of examination in uppercase",
  "report": "corrected report text with each finding on its own line separated by \\n",
  "impression": "corrected impression text with each diagnosis on its own line separated by \\n. Empty string if none."
}}

Do not include any explanation. Return ONLY the JSON array."""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 4000
    }

    try:
        response = req_lib.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=60
        )
        if not response.ok:
            return jsonify({"success": False, "error": f"API error {response.status_code}: {response.text}"})

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        content = re.sub(r"```json\s*|\s*```", "", content).strip()
        parsed = json.loads(content)

        if isinstance(parsed, dict):
            parsed = [parsed]

        return jsonify({"success": True, "data": parsed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/save_session", methods=["POST"])
def save_session():
    data = request.json
    patients = data.get("patients", [])
    if not patients:
        return jsonify({"success": False, "error": "No patients to save"})

    date_of_exam = patients[0].get("date_of_exam", "")
    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (saved_at, date_of_exam, patient_count, patients_json) VALUES (?,?,?,?)",
            (saved_at, date_of_exam, len(patients), json.dumps(patients))
        )
        session_id = cursor.lastrowid
        conn.commit()

    return jsonify({"success": True, "session_id": session_id})


@app.route("/history", methods=["GET"])
def get_history():
    search = request.args.get("search", "").strip().lower()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, saved_at, date_of_exam, patient_count, patients_json FROM sessions ORDER BY id DESC"
        ).fetchall()

    results = []
    for row in rows:
        patients = json.loads(row["patients_json"])
        names = [p.get("name", "") for p in patients]

        # Filter by search
        if search:
            match = any(
                search in n.lower() or
                search in (p.get("case_no", "") or "").lower() or
                search in (row["date_of_exam"] or "").lower()
                for n, p in zip(names, patients)
            )
            if not match:
                continue

        results.append({
            "id": row["id"],
            "saved_at": row["saved_at"],
            "date_of_exam": row["date_of_exam"],
            "patient_count": row["patient_count"],
            "names": names,
            "patients": patients
        })

    return jsonify({"success": True, "sessions": results})


@app.route("/delete_session/<int:session_id>", methods=["DELETE"])
def delete_session(session_id):
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
    return jsonify({"success": True})


# ─── PDF ─────────────────────────────────────────────────────────────────────

def make_styles():
    base = getSampleStyleSheet()
    return {
        "clinic_name": ParagraphStyle("ClinicName", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=15,
            textColor=colors.HexColor("#1a3a5c"),
            alignment=TA_CENTER, spaceAfter=3),
        "clinic_sub": ParagraphStyle("ClinicSub", parent=base["Normal"],
            fontName="Helvetica", fontSize=9,
            textColor=colors.HexColor("#555555"),
            alignment=TA_CENTER, spaceAfter=2),
        "header_label": ParagraphStyle("HeaderLabel", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=9,
            textColor=colors.HexColor("#333333")),
        "header_value": ParagraphStyle("HeaderValue", parent=base["Normal"],
            fontName="Helvetica", fontSize=10,
            textColor=colors.HexColor("#000000")),
        "section_title": ParagraphStyle("SectionTitle", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=10,
            textColor=colors.HexColor("#1a3a5c"),
            spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("Body", parent=base["Normal"],
            fontName="Helvetica", fontSize=10,
            leading=16, textColor=colors.HexColor("#111111"), spaceAfter=3),
        "impression": ParagraphStyle("Impression", parent=base["Normal"],
            fontName="Helvetica-BoldOblique", fontSize=10,
            leading=16, textColor=colors.HexColor("#1a3a5c"), spaceAfter=3),
        "sig": ParagraphStyle("Sig", parent=base["Normal"],
            fontSize=8, textColor=colors.HexColor("#666666")),
    }


def build_patient_story(patient, s):
    story = []
    story.append(Paragraph("CITY CLINIC", s["clinic_name"]))
    story.append(Paragraph("Radiology &amp; Diagnostic Imaging", s["clinic_sub"]))
    story.append(Paragraph("X-Ray Report", s["clinic_sub"]))
    story.append(Spacer(1, 5))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a3a5c")))
    story.append(Spacer(1, 8))

    info_data = [
        [Paragraph("Patient Name:", s["header_label"]), Paragraph(str(patient.get("name", "")), s["header_value"])],
        [Paragraph("Age / Sex:", s["header_label"]), Paragraph(f"{patient.get('age','')} / {patient.get('sex','')}", s["header_value"])],
        [Paragraph("Case No.:", s["header_label"]), Paragraph(str(patient.get("case_no", "")), s["header_value"])],
        [Paragraph("Date of Exam:", s["header_label"]), Paragraph(str(patient.get("date_of_exam", "")), s["header_value"])],
        [Paragraph("Examination:", s["header_label"]), Paragraph(str(patient.get("examination", "")), s["header_value"])],
    ]
    tbl = Table(info_data, colWidths=[1.4 * inch, 5.6 * inch])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#aaaaaa")))

    story.append(Paragraph("REPORT:", s["section_title"]))
    for line in patient.get("report", "").strip().split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(line, s["body"]))

    impression_text = patient.get("impression", "").strip()
    if impression_text:
        story.append(Spacer(1, 4))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#aaaaaa")))
        story.append(Paragraph("IMPRESSION:", s["section_title"]))
        for line in impression_text.split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(line, s["impression"]))

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="40%", thickness=0.5, color=colors.HexColor("#333333")))
    story.append(Paragraph("Radiologist Signature", s["sig"]))
    return story


def generate_pdf_buffer(patients):
    buffer = io.BytesIO()
    s = make_styles()
    all_story = []
    for i, patient in enumerate(patients):
        if i > 0:
            all_story.append(PageBreak())
        all_story.extend(build_patient_story(patient, s))
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        rightMargin=0.75 * inch, leftMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch
    )
    doc.build(all_story)
    buffer.seek(0)
    return buffer


@app.route("/generate_pdf", methods=["POST"])
def generate_pdf():
    data = request.json
    patients = data.get("patients", [])
    buffer = generate_pdf_buffer(patients)
    date_str = patients[0].get("date_of_exam", "report").replace(" ", "_").replace(",", "") if patients else "report"
    return send_file(buffer, as_attachment=True,
                     download_name=f"reports_{date_str}.pdf",
                     mimetype="application/pdf")


# ─── OVERLAY PDF (for pre-printed template paper) ────────────────────────────
#
# Coordinates measured from the helper PDF pipe markers (| chars = box edges).
# Page: A4 (595.2 x 841.8 pts). ReportLab origin = bottom-left.
# All x,y values are the CENTER of each box.
#
# Box edges found:
#   NAME:    left=60.0   right=261.5
#   AGE:     left=261.5  right=354.4
#   SEX:     left=354.4  right=390.5
#   CASE_NO: left=497.7  right=541.0
#   DATE:    left=485.7  right=541.0
#   EXAM:    left=111.0  right=326.9
#   REPORT:  left=46.0   right=487.1  (rows at y=309,323,337,351,365...)
#   IMP:     left=30.2   right=531.4  (rows at y=588,601,615,629,643...)
#
# To fine-tune: increase x → RIGHT, decrease x → LEFT
#               increase y → UP,    decrease y → DOWN

PAGE_W = 595.2
PAGE_H = 841.8
FONT_NAME = "Helvetica"
FONT_SIZE = 10

OVERLAY_NAME_CX    = 160.8;  OVERLAY_ROW1_CY  = 661.2
#OVERLAY_AGE_CX     = 307.9
OVERLAY_AGE_CX     = 315
OVERLAY_SEX_CX     = 372.4
OVERLAY_CASENO_CX  = 519.4
OVERLAY_DATE_CX    = 513.4;  OVERLAY_DATE_CY  = 643.0
OVERLAY_EXAM_CX    = 230;  OVERLAY_EXAM_CY  = 624.9

OVERLAY_REPORT_CX  = 266.6;  OVERLAY_REPORT_CY = 527.5;  OVERLAY_REPORT_LH = 14.0
OVERLAY_IMP_CX     = 280.8;  OVERLAY_IMP_CY    = 249.0;  OVERLAY_IMP_LH    = 13.9


def generate_overlay_buffer(patients):
    """Generate a text-only PDF for pre-printed A4 template paper.
    All text is centered within its box, except date (right) and exam (left)."""
    from reportlab.pdfgen import canvas as rl_canvas

    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))

    # Box right edges (for right-aligned fields)
    DATE_RIGHT_EDGE  = 541.0
    EXAM_LEFT_EDGE   = 111.0

    for i, p in enumerate(patients):
        if i > 0:
            c.showPage()

        c.setFillColorRGB(0, 0, 0)
        c.setFont(FONT_NAME, FONT_SIZE)

        def centered(text, cx, cy):
            """Draw text centered horizontally at cx, vertically at cy."""
            text = str(text)
            w = c.stringWidth(text, FONT_NAME, FONT_SIZE)
            c.drawString(cx - w / 2, cy - FONT_SIZE * 0.3, text)

        def right_aligned(text, right_x, cy):
            """Draw text so its right edge aligns to right_x."""
            text = str(text)
            w = c.stringWidth(text, FONT_NAME, FONT_SIZE)
            c.drawString(right_x - w, cy - FONT_SIZE * 0.3, text)

        def left_aligned(text, left_x, cy):
            """Draw text left-aligned from left_x."""
            c.drawString(left_x, cy - FONT_SIZE * 0.3, str(text))

        # Age: append " Y/O"
        age_text = f"{p.get('age', '')} Y/O"

        centered(p.get("name", ""),         OVERLAY_NAME_CX,   OVERLAY_ROW1_CY)
        centered(age_text,                   OVERLAY_AGE_CX,    OVERLAY_ROW1_CY)
        centered(p.get("sex", ""),           OVERLAY_SEX_CX,    OVERLAY_ROW1_CY)
        centered(p.get("case_no", ""),       OVERLAY_CASENO_CX, OVERLAY_ROW1_CY)

        # Date: right-aligned within its box
        right_aligned(p.get("date_of_exam", ""), DATE_RIGHT_EDGE, OVERLAY_DATE_CY)

        # Exam: left-aligned from left edge of its box
        left_aligned(p.get("examination", ""), EXAM_LEFT_EDGE, OVERLAY_EXAM_CY)

        for idx, line in enumerate([l.strip() for l in p.get("report", "").strip().split("\n") if l.strip()][:14]):
            centered(line, OVERLAY_REPORT_CX, OVERLAY_REPORT_CY - idx * OVERLAY_REPORT_LH)

        for idx, line in enumerate([l.strip() for l in p.get("impression", "").strip().split("\n") if l.strip()][:5]):
            centered(line, OVERLAY_IMP_CX, OVERLAY_IMP_CY - idx * OVERLAY_IMP_LH)

    c.save()
    buffer.seek(0)
    return buffer


@app.route("/generate_overlay_pdf", methods=["POST"])
def generate_overlay_pdf():
    data = request.json
    patients = data.get("patients", [])
    buffer = generate_overlay_buffer(patients)
    date_str = patients[0].get("date_of_exam", "template").replace(" ", "_").replace(",", "") if patients else "template"
    return send_file(buffer, as_attachment=True,
                     download_name=f"overlay_{date_str}.pdf",
                     mimetype="application/pdf")


@app.route("/reprint_overlay_pdf/<int:session_id>", methods=["GET"])
def reprint_overlay_pdf(session_id):
    with get_db() as conn:
        row = conn.execute("SELECT patients_json, date_of_exam FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return "Session not found", 404
    patients = json.loads(row["patients_json"])
    buffer = generate_overlay_buffer(patients)
    date_str = (row["date_of_exam"] or "template").replace(" ", "_").replace(",", "")
    return send_file(buffer, as_attachment=True,
                     download_name=f"overlay_{date_str}.pdf",
                     mimetype="application/pdf")


@app.route("/reprint_pdf/<int:session_id>", methods=["GET"])
def reprint_pdf(session_id):
    with get_db() as conn:
        row = conn.execute("SELECT patients_json, date_of_exam FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return "Session not found", 404
    patients = json.loads(row["patients_json"])
    buffer = generate_pdf_buffer(patients)
    date_str = (row["date_of_exam"] or "report").replace(" ", "_").replace(",", "")
    return send_file(buffer, as_attachment=True,
                     download_name=f"reports_{date_str}.pdf",
                     mimetype="application/pdf")


if __name__ == "__main__":
    import threading, webbrowser
    def open_browser():
        import time; time.sleep(1.2)
        webbrowser.open("http://localhost:5000")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000)