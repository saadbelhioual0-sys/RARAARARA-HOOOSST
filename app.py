from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import docker
import random
import asyncio
import threading
import json
import secrets
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///jagwar.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Docker client
docker_client = docker.from_env()

# ==================== DATABASE MODELS ====================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    max_servers = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Server(db.Model):
    __tablename__ = 'servers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    language = db.Column(db.String(20), default='python')
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    container_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='stopped')
    port = db.Column(db.Integer, unique=True, nullable=True)
    cpu_usage = db.Column(db.String(10), default='0%')
    ram_usage = db.Column(db.String(10), default='0MB')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    owner = db.relationship('User', backref=db.backref('servers', lazy=True))

class FailedLogin(db.Model):
    __tablename__ = 'failed_logins'
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(45), nullable=False)
    username_attempted = db.Column(db.String(80))
    user_agent = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class LoginLog(db.Model):
    __tablename__ = 'login_logs'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80))
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    success = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# ==================== HELPER FUNCTIONS ====================

def get_user_folder(user_id):
    folder = f"user_servers/user_{user_id}"
    os.makedirs(folder, exist_ok=True)
    return folder

def get_server_folder(user_id, server_id):
    folder = f"user_servers/user_{user_id}/server_{server_id}"
    os.makedirs(folder, exist_ok=True)
    return folder

def create_user_container(server_id, port, server_dir):
    try:
        container = docker_client.containers.run(
            image="python:3.11-slim",
            command="sleep infinity",
            name=f"jagwar_server_{server_id}",
            detach=True,
            ports={f"8000/tcp": port},
            volumes={os.path.abspath(server_dir): {"bind": "/app", "mode": "rw"}},
            mem_limit="512m",
            cpu_quota=50000
        )
        return container.id
    except Exception as e:
        print(f"Docker error: {e}")
        return None

def stop_container(container_id):
    try:
        container = docker_client.containers.get(container_id)
        container.stop()
        container.remove()
        return True
    except:
        return False

def get_container_stats(container_id):
    try:
        container = docker_client.containers.get(container_id)
        stats = container.stats(stream=False)
        cpu_usage = stats["cpu_stats"]["cpu_usage"]["total_usage"]
        system_cpu = stats["cpu_stats"]["system_cpu_usage"]
        cpu_percent = round((cpu_usage / system_cpu) * 100, 2) if system_cpu else 0
        mem_usage = stats["memory_stats"]["usage"]
        return {
            "cpu": f"{cpu_percent}%",
            "ram": f"{round(mem_usage / 1024 / 1024, 2)}MB"
        }
    except:
        return {"cpu": "0%", "ram": "0MB"}

def execute_command_in_container(container_id, command):
    try:
        container = docker_client.containers.get(container_id)
        exec_result = container.exec_run(command, workdir="/app")
        return exec_result.output.decode() if exec_result.output else ""
    except Exception as e:
        return f"Error: {str(e)}"

def start_python_server_async(container_id, server_dir, server_id):
    """تشغيل السيرفر في خلفية منفصلة"""
    def run():
        try:
            container = docker_client.containers.get(container_id)
            # تثبيت المكتبات
            container.exec_run("cd /app && if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi", workdir="/app")
            # تشغيل main.py
            container.exec_run("cd /app && python main.py", workdir="/app", detach=True)
            # تحديث الحالة في قاعدة البيانات
            with app.app_context():
                server = Server.query.get(server_id)
                if server:
                    server.status = "running"
                    db.session.commit()
        except Exception as e:
            print(f"Error starting server: {e}")
    
    thread = threading.Thread(target=run)
    thread.start()

# ==================== LOGIN MANAGER ====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==================== ROUTES ====================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        ip = request.remote_addr
        user_agent = request.headers.get('User-Agent', '')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            # تسجيل الدخول الناجح
            log = LoginLog(username=username, ip=ip, user_agent=user_agent, success=True)
            db.session.add(log)
            db.session.commit()
            
            if username == 'JAGWARGG':
                return redirect(url_for('admin_panel'))
            return redirect(url_for('dashboard'))
        else:
            # تسجيل فاشل
            failed = FailedLogin(ip=ip, username_attempted=username, user_agent=user_agent)
            log = LoginLog(username=username, ip=ip, user_agent=user_agent, success=False)
            db.session.add(failed)
            db.session.add(log)
            db.session.commit()
            return render_template('login.html', error='Invalid username or password')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    servers = Server.query.filter_by(owner_id=current_user.id).all()
    for server in servers:
        if server.container_id:
            stats = get_container_stats(server.container_id)
            server.cpu_usage = stats['cpu']
            server.ram_usage = stats['ram']
            db.session.commit()
    return render_template('dashboard.html', user=current_user, servers=servers)

@app.route('/create_server', methods=['POST'])
@login_required
def create_server():
    name = request.form.get('name')
    description = request.form.get('description', '')
    
    servers_count = Server.query.filter_by(owner_id=current_user.id).count()
    if servers_count >= current_user.max_servers:
        return redirect(url_for('dashboard', error='Max servers limit reached'))
    
    port = random.randint(10000, 60000)
    new_server = Server(
        name=name,
        description=description,
        language='python',
        owner_id=current_user.id,
        status='stopped',
        port=port
    )
    db.session.add(new_server)
    db.session.commit()
    
    # إنشاء مجلد السيرفر
    server_dir = get_server_folder(current_user.id, new_server.id)
    
    # إنشاء ملفات افتراضية
    with open(f"{server_dir}/main.py", 'w') as f:
        f.write('print("Hello from JAGWAR HOST!")\n\n# Your Python code here\n')
    with open(f"{server_dir}/requirements.txt", 'w') as f:
        f.write('# Add your dependencies here\n# Example:\n# flask==3.0.0\n# requests==2.31.0\n')
    
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/control')
@login_required
def server_control(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return redirect(url_for('dashboard'))
    
    stats = {"cpu": "0%", "ram": "0MB"}
    if server.container_id:
        stats = get_container_stats(server.container_id)
        server.cpu_usage = stats['cpu']
        server.ram_usage = stats['ram']
        db.session.commit()
    
    return render_template('server_control.html', server=server, stats=stats)

@app.route('/start_server/<int:server_id>', methods=['POST'])
@login_required
def start_server(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    server_dir = get_server_folder(current_user.id, server.id)
    container_id = create_user_container(server.id, server.port, server_dir)
    
    if container_id:
        server.container_id = container_id
        server.status = 'starting'
        db.session.commit()
        
        # تشغيل السيرفر في الخلفية
        start_python_server_async(container_id, server_dir, server.id)
        
        return jsonify({'status': 'started', 'container_id': container_id})
    
    return jsonify({'error': 'Failed to start server'}), 500

@app.route('/stop_server/<int:server_id>', methods=['POST'])
@login_required
def stop_server(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    if server.container_id:
        stop_container(server.container_id)
        server.container_id = None
        server.status = 'stopped'
        db.session.commit()
    
    return jsonify({'status': 'stopped'})

@app.route('/restart_server/<int:server_id>', methods=['POST'])
@login_required
def restart_server(server_id):
    stop_server(server_id)
    import time
    time.sleep(2)
    return start_server(server_id)

@app.route('/server/<int:server_id>/command', methods=['POST'])
@login_required
def execute_command(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    command = request.form.get('command', '')
    if server.container_id:
        output = execute_command_in_container(server.container_id, command)
        return jsonify({'output': output})
    
    return jsonify({'output': 'Server is not running'})

@app.route('/server/<int:server_id>/files')
@login_required
def list_files(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'files': []}), 403
    
    path = request.args.get('path', '')
    server_dir = get_server_folder(current_user.id, server.id)
    full_path = os.path.join(server_dir, path)
    
    files = []
    if os.path.exists(full_path):
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            files.append({
                'name': item,
                'is_dir': os.path.isdir(item_path),
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0
            })
    
    return jsonify({'files': files})

@app.route('/server/<int:server_id>/upload', methods=['POST'])
@login_required
def upload_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    path = request.form.get('path', '')
    file = request.files.get('file')
    
    if file:
        server_dir = get_server_folder(current_user.id, server.id)
        target_path = os.path.join(server_dir, path, secure_filename(file.filename))
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        file.save(target_path)
        return jsonify({'success': True})
    
    return jsonify({'error': 'No file uploaded'}), 400

@app.route('/server/<int:server_id>/create_file', methods=['POST'])
@login_required
def create_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    file_name = request.form.get('file_name')
    path = request.form.get('path', '')
    content = request.form.get('content', '')
    
    if file_name:
        server_dir = get_server_folder(current_user.id, server.id)
        file_path = os.path.join(server_dir, path, secure_filename(file_name))
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            f.write(content)
        return jsonify({'success': True})
    
    return jsonify({'error': 'No filename provided'}), 400

@app.route('/server/<int:server_id>/create_folder', methods=['POST'])
@login_required
def create_folder(server_id):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    folder_name = request.form.get('folder_name')
    path = request.form.get('path', '')
    
    if folder_name:
        server_dir = get_server_folder(current_user.id, server.id)
        folder_path = os.path.join(server_dir, path, secure_filename(folder_name))
        os.makedirs(folder_path, exist_ok=True)
        return jsonify({'success': True})
    
    return jsonify({'error': 'No folder name provided'}), 400

@app.route('/server/<int:server_id>/file/<path:file_path>')
@login_required
def get_file_content(server_id, file_path):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    server_dir = get_server_folder(current_user.id, server.id)
    full_path = os.path.join(server_dir, file_path)
    
    if os.path.exists(full_path) and os.path.isfile(full_path):
        with open(full_path, 'r') as f:
            content = f.read()
        return jsonify({'content': content, 'path': file_path})
    
    return jsonify({'error': 'File not found'}), 404

@app.route('/server/<int:server_id>/file/<path:file_path>', methods=['POST'])
@login_required
def save_file_content(server_id, file_path):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    content = data.get('content', '')
    
    server_dir = get_server_folder(current_user.id, server.id)
    full_path = os.path.join(server_dir, file_path)
    
    with open(full_path, 'w') as f:
        f.write(content)
    
    return jsonify({'success': True})

@app.route('/server/<int:server_id>/file/<path:file_path>', methods=['DELETE'])
@login_required
def delete_file(server_id, file_path):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    server_dir = get_server_folder(current_user.id, server.id)
    full_path = os.path.join(server_dir, file_path)
    
    if os.path.exists(full_path):
        if os.path.isdir(full_path):
            import shutil
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
    
    return jsonify({'success': True})

@app.route('/server/<int:server_id>/rename/<path:file_path>', methods=['POST'])
@login_required
def rename_file(server_id, file_path):
    server = Server.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    new_name = data.get('new_name', '')
    
    server_dir = get_server_folder(current_user.id, server.id)
    old_path = os.path.join(server_dir, file_path)
    new_path = os.path.join(os.path.dirname(old_path), secure_filename(new_name))
    
    if os.path.exists(old_path):
        os.rename(old_path, new_path)
    
    return jsonify({'success': True})

@app.route('/server/<int:server_id>/console_stream')
@login_required
def console_stream(server_id):
    """Server-Sent Events for real console"""
    def generate():
        server = Server.query.get(server_id)
        if server and server.container_id:
            import time
            while True:
                try:
                    container = docker_client.containers.get(server.container_id)
                    logs = container.logs(tail=20, follow=False).decode()
                    yield f"data: {json.dumps({'log': logs})}\n\n"
                except:
                    yield f"data: {json.dumps({'log': 'Container not accessible'})}\n\n"
                time.sleep(2)
        else:
            yield f"data: {json.dumps({'log': 'Server is not running'})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

# ==================== ADMIN ROUTES ====================

@app.route('/admin')
@login_required
def admin_panel():
    if current_user.username != 'JAGWARGG':
        return redirect(url_for('dashboard'))
    
    users = User.query.all()
    servers = Server.query.all()
    failed_logs = FailedLogin.query.order_by(FailedLogin.timestamp.desc()).limit(100).all()
    login_logs = LoginLog.query.order_by(LoginLog.timestamp.desc()).limit(100).all()
    
    return render_template('admin.html', 
                         users=users, 
                         servers=servers, 
                         failed_logs=failed_logs, 
                         login_logs=login_logs)

@app.route('/admin/add_user', methods=['POST'])
@login_required
def add_user():
    if current_user.username != 'JAGWARGG':
        return redirect(url_for('dashboard'))
    
    username = request.form.get('username')
    password = request.form.get('password')
    
    if User.query.filter_by(username=username).first():
        return redirect(url_for('admin_panel', error='User exists'))
    
    new_user = User(username=username)
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()
    
    get_user_folder(new_user.id)
    
    return redirect(url_for('admin_panel'))

@app.route('/admin/make_premium/<int:user_id>', methods=['POST'])
@login_required
def make_premium(user_id):
    if current_user.username != 'JAGWARGG':
        return redirect(url_for('dashboard'))
    
    user = User.query.get_or_404(user_id)
    user.is_premium = True
    user.max_servers = 5
    db.session.commit()
    
    return redirect(url_for('admin_panel'))

# ==================== CREATE DATABASE ====================

with app.app_context():
    db.create_all()
    # إنشاء مستخدم الأدمن إذا لم يكن موجوداً
    if not User.query.filter_by(username='JAGWARGG').first():
        admin = User(username='JAGWARGG')
        admin.set_password('JAGWAR12345')
        admin.is_premium = True
        admin.max_servers = 999
        db.session.add(admin)
        db.session.commit()
        print("Admin user created: JAGWARGG / JAGWAR12345")

if __name__ == '__main__':
    os.makedirs('user_servers', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    app.run(host='0.0.0.0', port=8000, debug=True)
