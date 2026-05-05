from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
import threading
import time
import json
import os
import uuid
from collections import deque
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import gc
from datetime import datetime
import secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
socketio = SocketIO(app, cors_allowed_origins="*")

# File paths
SESSIONS_FILE = "sessions_registry.json"
LOGS_DIR = "session_logs"
MAX_LOGS = 30

os.makedirs(LOGS_DIR, exist_ok=True)

class Session:
    __slots__ = ['id', 'running', 'count', 'logs', 'idx', 'driver', 'start_time', 'post_id', 'cookies', 'comments_list', 'prefix', 'delay']
    def __init__(self, sid):
        self.id = sid
        self.running = False
        self.count = 0
        self.logs = deque(maxlen=MAX_LOGS)
        self.idx = 0
        self.driver = None
        self.start_time = None
        self.post_id = None
        self.cookies = None
        self.comments_list = []
        self.prefix = ""
        self.delay = 30
    
    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        log_entry = f"[{ts}] {msg}"
        self.logs.append(log_entry)
        try:
            with open(f"{LOGS_DIR}/{self.id}.log", "a") as f:
                f.write(log_entry + "\n")
        except:
            pass

class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
        self._load_registry()
    
    def _load_registry(self):
        if os.path.exists(SESSIONS_FILE):
            try:
                with open(SESSIONS_FILE, 'r') as f:
                    data = json.load(f)
                    for sid, info in data.items():
                        if sid not in self.sessions:
                            s = Session(sid)
                            s.count = info.get('count', 0)
                            s.running = False
                            s.start_time = info.get('start_time')
                            self.sessions[sid] = s
            except:
                pass
    
    def _save_registry(self):
        try:
            data = {}
            for sid, s in self.sessions.items():
                data[sid] = {
                    'count': s.count,
                    'running': s.running,
                    'start_time': s.start_time
                }
            with open(SESSIONS_FILE, 'w') as f:
                json.dump(data, f)
        except:
            pass
    
    def create_session(self):
        with self.lock:
            sid = uuid.uuid4().hex[:8].upper()
            s = Session(sid)
            self.sessions[sid] = s
            self._save_registry()
            return s
    
    def get_session(self, sid):
        return self.sessions.get(sid)
    
    def get_all_sessions(self):
        return list(self.sessions.values())
    
    def get_active_sessions(self):
        return [s for s in self.sessions.values() if s.running]
    
    def stop_session(self, sid):
        s = self.sessions.get(sid)
        if s:
            s.running = False
            if s.driver:
                try:
                    s.driver.quit()
                except:
                    pass
                s.driver = None
            self._save_registry()
            return True
        return False
    
    def get_logs(self, sid, limit=30):
        log_file = f"{LOGS_DIR}/{sid}.log"
        if os.path.exists(log_file):
            try:
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    return lines[-limit:]
            except:
                pass
        s = self.sessions.get(sid)
        if s:
            return list(s.logs)[-limit:]
        return []
    
    def update_count(self, sid, count):
        s = self.sessions.get(sid)
        if s:
            s.count = count
            self._save_registry()
    
    def cleanup_stopped(self):
        with self.lock:
            to_remove = [sid for sid, s in self.sessions.items() if not s.running and s.count == 0]
            for sid in to_remove:
                del self.sessions[sid]
                try:
                    os.remove(f"{LOGS_DIR}/{sid}.log")
                except:
                    pass
            self._save_registry()

manager = SessionManager()

def setup_browser(session):
    session.log('Setting up Chrome browser...')
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-setuid-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--disable-extensions')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    
    # Find Chrome binary
    chrome_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable']
    for path in chrome_paths:
        if os.path.exists(path):
            opts.binary_location = path
            session.log(f'Found Chrome: {path}')
            break
    
    try:
        driver = webdriver.Chrome(options=opts)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        session.log('Browser setup complete!')
        return driver
    except Exception as e:
        session.log(f'Browser setup failed: {str(e)[:50]}')
        raise

def find_comment_input(driver, session):
    session.log('Searching for comment input...')
    time.sleep(5)
    
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)
    except:
        pass
    
    selectors = [
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        'div[aria-label*="comment" i][contenteditable="true"]',
        'div[aria-label*="Comment" i][contenteditable="true"]'
    ]
    
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                try:
                    if element.is_displayed() and element.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                        time.sleep(0.5)
                        return element
                except:
                    continue
        except:
            continue
    
    return None

def run_session(session_id):
    session = manager.get_session(session_id)
    if not session:
        return
    
    retries = 0
    driver = None
    
    while session.running and retries < 3:
        try:
            if driver is None:
                driver = setup_browser(session)
                session.driver = driver
            
            session.log('Navigating to Facebook...')
            driver.get('https://www.facebook.com/')
            time.sleep(5)
            
            if session.cookies and session.cookies.strip():
                session.log('Adding cookies...')
                driver.get('https://www.facebook.com/')
                time.sleep(2)
                cookie_pairs = session.cookies.split(';')
                for cookie_pair in cookie_pairs:
                    cookie_pair = cookie_pair.strip()
                    if cookie_pair and '=' in cookie_pair:
                        try:
                            name, value = cookie_pair.split('=', 1)
                            driver.add_cookie({'name': name.strip(), 'value': value.strip(), 'domain': '.facebook.com'})
                        except:
                            pass
                session.log('Cookies added, refreshing...')
                driver.refresh()
                time.sleep(5)
            
            session.log(f'Opening post...')
            if session.post_id.startswith('http'):
                driver.get(session.post_id)
            else:
                driver.get(f'https://www.facebook.com/{session.post_id}')
            
            time.sleep(10)
            session.log('Page loaded successfully!')
            
            while session.running:
                try:
                    # Check if session is still running
                    if not session.running:
                        break
                    
                    comment_input = find_comment_input(driver, session)
                    
                    if not comment_input:
                        session.log('Comment input not found, retrying...')
                        time.sleep(5)
                        continue
                    
                    comment_text = session.comments_list[session.idx % len(session.comments_list)]
                    session.idx += 1
                    
                    final_comment = f"{session.prefix} {comment_text}" if session.prefix else comment_text
                    
                    session.log(f'Typing: {final_comment[:30]}...')
                    
                    try:
                        comment_input.click()
                        time.sleep(0.5)
                        driver.execute_script("arguments[0].focus();", comment_input)
                        driver.execute_script("arguments[0].innerText = arguments[1];", comment_input, final_comment)
                        driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles: true}));", comment_input)
                        time.sleep(1)
                    except Exception as e:
                        session.log(f'Error typing: {str(e)[:30]}')
                        continue
                    
                    session.log('Sending comment...')
                    
                    try:
                        send_js = """
                            const element = arguments[0];
                            const enterEvent = new KeyboardEvent('keydown', {
                                key: 'Enter',
                                code: 'Enter',
                                keyCode: 13,
                                which: 13,
                                bubbles: true
                            });
                            element.dispatchEvent(enterEvent);
                        """
                        driver.execute_script(send_js, comment_input)
                        time.sleep(2)
                        
                        session.count += 1
                        manager.update_count(session.id, session.count)
                        session.log(f'✅ Comment #{session.count} sent successfully!')
                        
                        socketio.emit('session_update', {
                            'session_id': session.id,
                            'count': session.count,
                            'status': 'running'
                        })
                        
                    except Exception as e:
                        session.log(f'Error sending: {str(e)[:30]}')
                        continue
                    
                    session.log(f'Waiting {session.delay} seconds...')
                    for _ in range(session.delay):
                        if not session.running:
                            break
                        time.sleep(1)
                    
                    if session.count % 5 == 0:
                        gc.collect()
                        
                except Exception as e:
                    if session.running:
                        session.log(f'Error in loop: {str(e)[:50]}')
                    time.sleep(5)
                    
        except Exception as e:
            if session.running:
                session.log(f'Fatal error: {str(e)[:50]}')
            retries += 1
            try:
                if driver:
                    driver.quit()
            except:
                pass
            driver = None
            time.sleep(5)
    
    session.running = False
    session.log('Session stopped.')
    manager._save_registry()
    if driver:
        try:
            driver.quit()
        except:
            pass
    gc.collect()
    
    socketio.emit('session_update', {
        'session_id': session.id,
        'count': session.count,
        'status': 'stopped'
    })

def start_session_thread(session_id, post_id, cookies, comments_list, prefix, delay):
    session = manager.get_session(session_id)
    if session:
        session.post_id = post_id
        session.cookies = cookies
        session.comments_list = comments_list
        session.prefix = prefix
        session.delay = delay
        session.running = True
        session.count = 0
        session.idx = 0
        session.start_time = datetime.now().strftime("%H:%M:%S")
        session.log(f'Session {session_id} started!')
        manager._save_registry()
        
        thread = threading.Thread(target=run_session, args=(session_id,))
        thread.daemon = True
        thread.start()

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sessions')
def get_sessions():
    all_sessions = manager.get_all_sessions()
    active_sessions = manager.get_active_sessions()
    total_comments = sum(s.count for s in all_sessions)
    
    sessions_data = []
    for s in all_sessions:
        sessions_data.append({
            'id': s.id,
            'running': s.running,
            'count': s.count,
            'start_time': s.start_time
        })
    
    return jsonify({
        'total_comments': total_comments,
        'active_count': len(active_sessions),
        'sessions': sessions_data
    })

@app.route('/api/session/<session_id>')
def get_session_details(session_id):
    session = manager.get_session(session_id.upper())
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    logs = manager.get_logs(session_id.upper(), 50)
    
    return jsonify({
        'id': session.id,
        'running': session.running,
        'count': session.count,
        'logs': logs,
        'start_time': session.start_time
    })

@app.route('/api/create_session', methods=['POST'])
def create_session():
    data = request.json
    post_id = data.get('post_id')
    cookies = data.get('cookies')
    comments = data.get('comments')
    prefix = data.get('prefix', '')
    delay = int(data.get('delay', 30))
    
    if not cookies:
        return jsonify({'error': 'Cookies are required!'}), 400
    if not post_id:
        return jsonify({'error': 'Post ID/URL is required!'}), 400
    if not comments:
        return jsonify({'error': 'Comments are required!'}), 400
    
    comments_list = [c.strip() for c in comments.split('\n') if c.strip()]
    if not comments_list:
        comments_list = ['Nice post!']
    
    new_session = manager.create_session()
    start_session_thread(new_session.id, post_id, cookies, comments_list, prefix, delay)
    
    return jsonify({
        'success': True,
        'session_id': new_session.id,
        'message': f'Session {new_session.id} started!'
    })

@app.route('/api/stop_session/<session_id>', methods=['POST'])
def stop_session_route(session_id):
    success = manager.stop_session(session_id.upper())
    if success:
        return jsonify({'success': True, 'message': 'Session stopped successfully'})
    else:
        return jsonify({'success': False, 'message': 'Session not found'}), 404

@socketio.on('connect')
def handle_connect():
    print('Client connected')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
