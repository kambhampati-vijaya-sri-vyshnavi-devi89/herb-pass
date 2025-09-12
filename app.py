# app.py


import os
import hashlib
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash
import qrcode

import oracledb

# --------- CONFIG - change these to match your Oracle setup ---------
from dotenv import load_dotenv
import os

load_dotenv()  # loads variables from .env file automatically

# Database credentials from .env
ORACLE_USER = os.getenv("ORACLE_USER", "herbpass")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "dbms123")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/XEPDB1")
WALLET_DIR = os.getenv("WALLET_DIR", "")

# Oracle wallet (if using cloud) — safe default for local
if WALLET_DIR:
    os.environ["TNS_ADMIN"] = WALLET_DIR

# Upload folder and allowed extensions
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(os.getcwd(), 'static', 'uploads'))
ALLOWED_PHOTO_EXT = {'png', 'jpg', 'jpeg'}
ALLOWED_DOC_EXT = {'pdf'}

# -------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = 'super-secret-for-dev'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize Oracle connection pool
pool = oracledb.create_pool(user=ORACLE_USER, password=ORACLE_PASSWORD,
dsn=ORACLE_DSN, min=1, max=4, increment=1)

# Utility functions
def allowed_file(filename, allowed):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed

def generate_batch_code():
    return 'HB-' + uuid.uuid4().hex[:10].upper()

def gen_qr(url, save_path):
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(save_path)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            h.update(chunk)
    return h.hexdigest()


# Routes
@app.route('/')
def index():
    return render_template('index.html')


# Farmer create batch
@app.route('/farmer', methods=['GET', 'POST'])
def farmer():
    if request.method == 'POST':
        herb_name = request.form['herb_name']
        farmer_name = request.form.get('farmer_name', '')
        phone = request.form.get('phone', '')
        gps_lat = request.form.get('gps_lat', '')
        gps_lng = request.form.get('gps_lng', '')
        photo = request.files.get('photo')

        batch_code = generate_batch_code()
        photo_path_db = None

        if photo and allowed_file(photo.filename, ALLOWED_PHOTO_EXT):
            filename = f"photo_{batch_code}.{photo.filename.rsplit('.',1)[1]}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            photo.save(save_path)
            photo_path_db = 'static/uploads/' + filename

        # Insert into Oracle
        conn = pool.acquire()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO FARMER_BATCH (batch_code, herb_name, farmer_name, phone, gps_lat, gps_lng, photo_path) "
            "VALUES (:1,:2,:3,:4,:5,:6,:7)",
            (batch_code, herb_name, farmer_name, phone, gps_lat, gps_lng, photo_path_db)
        )
        conn.commit()

        # get inserted id
        cur.execute("SELECT id FROM FARMER_BATCH WHERE batch_code = :1", (batch_code,))
        row = cur.fetchone()
        batch_id = row[0]

        # generate QR that points to /batch/<id>
        qr_url = request.url_root.rstrip('/') + url_for('view_batch', batch_id=batch_id)
        qr_filename = f"qr_{batch_code}.png"
        qr_save_full = os.path.join(app.config['UPLOAD_FOLDER'], qr_filename)
        gen_qr(qr_url, qr_save_full)
        qr_path_db = 'static/uploads/' + qr_filename

        # update qr path
        cur.execute("UPDATE FARMER_BATCH SET qr_path = :1 WHERE id = :2", (qr_path_db, batch_id))
        conn.commit()

        cur.close()
        pool.release(conn)

        flash(f'Batch created: {batch_code}. QR generated.')
        return redirect(url_for('farmer'))

    return render_template('farmer.html')


# Lab upload
@app.route('/lab', methods=['GET', 'POST'])
def lab():
    if request.method == 'POST':
        batch_id = request.form['batch_id']
        report = request.files.get('report')

        if not report or not allowed_file(report.filename, ALLOWED_DOC_EXT):
            flash('Please upload a PDF report')
            return redirect(url_for('lab'))

        filename = f"lab_{batch_id}_{int(datetime.utcnow().timestamp())}.pdf"
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        report.save(save_path)

        sha = sha256_file(save_path)

        conn = pool.acquire()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO LAB_REPORT (batch_id, file_path, sha256_hash) VALUES (:1,:2,:3)",
            (batch_id, 'static/uploads/' + filename, sha)
        )
        conn.commit()
        cur.close()
        pool.release(conn)

        flash('Lab report uploaded and hashed (immutable proof).')
        return redirect(url_for('lab'))

    return render_template('lab.html')


# Pharma status update
@app.route('/pharma', methods=['GET', 'POST'])
def pharma():
    if request.method == 'POST':
        batch_id = request.form['batch_id']
        status = request.form.get('status', 'Packaged')

        conn = pool.acquire()
        cur = conn.cursor()
        cur.execute("INSERT INTO PHARMA_STATUS (batch_id, status) VALUES (:1,:2)", (batch_id, status))
        conn.commit()
        cur.close()
        pool.release(conn)

        flash('Pharma status updated')
        return redirect(url_for('pharma'))

    return render_template('pharma.html')


# Consumer view
@app.route('/batch/<int:batch_id>')
def view_batch(batch_id):
    conn = pool.acquire()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, batch_code, herb_name, farmer_name, phone, gps_lat, gps_lng, photo_path, qr_path, created_at "
        "FROM FARMER_BATCH WHERE id = :1",
        (batch_id,)
    )
    batch = cur.fetchone()

    if not batch:
        pool.release(conn)
        return "Batch not found", 404

    # fetch lab reports
    cur.execute("SELECT file_path, sha256_hash, uploaded_at FROM LAB_REPORT WHERE batch_id = :1 ORDER BY uploaded_at",
                (batch_id,))
    lab_reports = cur.fetchall()

    # fetch pharma statuses
    cur.execute("SELECT status, updated_at FROM PHARMA_STATUS WHERE batch_id = :1 ORDER BY updated_at", (batch_id,))
    pharma_rows = cur.fetchall()

    cur.close()
    pool.release(conn)

    batch_dict = {
        'id': batch[0],
        'batch_code': batch[1],
        'herb_name': batch[2],
        'farmer_name': batch[3],
        'phone': batch[4],
        'gps_lat': batch[5],
        'gps_lng': batch[6],
        'photo_path': batch[7],
        'qr_path': batch[8],
        'created_at': batch[9]
    }

    return render_template('view_batch.html', batch=batch_dict, lab_reports=lab_reports, pharma_rows=pharma_rows)


# Static file serving helper (uploads already in static)
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)


