#!/usr/bin/env python3
import os
import threading
import time
import sqlite3
import json
import tempfile
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from weasyprint import HTML
import instaloader

# ----------------- Setup -----------------
BASE = Path(__file__).parent
DB_PATH = BASE / 'instadump.db'
SESSION_FILE = BASE / '.instaloader-session'

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.getenv('FLASK_SECRET', 'change_this_to_a_random_secret')
app.config['PERMANENT_SESSION_LIFETIME'] = 60*60*24*30  # 30 days

# ----------------- Database -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT,
        status TEXT,
        result_json TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def create_default_admin():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    ('admin', generate_password_hash('admin123')))
        conn.commit()
    conn.close()

init_db()
create_default_admin()

# ----------------- Worker -----------------
def worker_loop():
    L = instaloader.Instaloader(download_pictures=False, save_metadata=False)
    if SESSION_FILE.exists():
        try:
            L.load_session_from_file(str(SESSION_FILE))
        except Exception:
            pass

    while True:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, target FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row:
            job_id, target = row
            cur.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            conn.commit()
            conn.close()
            try:
                result = process_target(L, target)
                rjson = json.dumps(result, ensure_ascii=False)
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute("UPDATE jobs SET status='done', result_json=? WHERE id=?", (rjson, job_id))
                conn2.commit()
                conn2.close()
            except Exception as e:
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute("UPDATE jobs SET status='error', result_json=? WHERE id=?", 
                             (json.dumps({'error': str(e)}), job_id))
                conn2.commit()
                conn2.close()
        else:
            conn.close()
            time.sleep(2)

threading.Thread(target=worker_loop, daemon=True).start()

# ----------------- Instaloader -----------------
def process_target(L, username):
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        data = {
            'exists': True,
            'username': profile.username,
            'full_name': profile.full_name,
            'is_private': profile.is_private,
            'media_count': profile.mediacount,
            'followers': profile.followers,
            'followees': profile.followees,
            'biography': profile.biography,
            'profile_pic_url': getattr(profile, 'profile_pic_url', None)
        }
        if not profile.is_private and profile.mediacount:
            posts = []
            for i, post in enumerate(profile.get_posts()):
                if i >= 5: break
                posts.append({'date': post.date_utc.isoformat(), 'caption': post.caption})
            data['latest_posts'] = posts
        return data
    except instaloader.exceptions.ProfileNotExistsException:
        return {'exists': False}

# ----------------- Routes -----------------
@app.route('/')
def home():
    if 'user' not in session:
        return redirect(url_for('disclaimer'))
    return render_template('index.html')

@app.route('/disclaimer', methods=['GET', 'POST'])
def disclaimer():
    if request.method == 'POST':
        session['disclaimer'] = True
        return redirect(url_for('login'))
    return render_template('disclaimer.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember = 'remember' in request.form
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT id, password_hash FROM users WHERE username=?', (username,))
        row = cur.fetchone()
        conn.close()
        if row and check_password_hash(row[1], password):
            session['user'] = username
            if remember:
                session.permanent = True
            return redirect(url_for('home'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/start_job', methods=['POST'])
def start_job():
    if 'user' not in session:
        return jsonify({'error': 'auth'}), 401
    data = request.get_json() or {}
    target = data.get('username')
    if not target:
        return jsonify({'error': 'missing'}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT INTO jobs (target, status) VALUES (?, ?)', (target, 'queued'))
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': job_id})

@app.route('/job_status')
def job_status():
    jid = request.args.get('id')
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, status, result_json FROM jobs WHERE id=?', (jid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'notfound'}), 404
    jid, status, rjson = row
    try:
        result = json.loads(rjson) if rjson else None
    except Exception:
        result = None
    return jsonify({'id': jid, 'status': status, 'result': result})

@app.route('/download_report')
def download_report():
    jid = request.args.get('id')
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT result_json FROM jobs WHERE id=? LIMIT 1', (jid,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return 'No report', 404
    data = json.loads(row[0])
    html = render_template('report.html', data=data)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_pdf:
        HTML(string=html).write_pdf(tmp_pdf.name)
        return send_file(tmp_pdf.name, as_attachment=True, download_name=f'report_{jid}.pdf')

# ----------------- Run -----------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
