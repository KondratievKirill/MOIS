from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import socket
import json
import threading
import time
import logging
import re
from datetime import datetime

# ========== НАСТРОЙКА ЛОГГЕРОВ ==========

# Основной логгер
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Логгер для JSON (с цветами)
json_logger = logging.getLogger('JSON_MESSAGES')
json_handler = logging.StreamHandler()
json_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(message)s',
    datefmt='%H:%M:%S'
))
json_logger.addHandler(json_handler)
json_logger.setLevel(logging.INFO)
json_logger.propagate = False  # ← Не дублировать!

# Логгер для сырых сообщений (отладка)
raw_logger = logging.getLogger('RAW_MESSAGES')
raw_handler = logging.StreamHandler()
raw_handler.setFormatter(logging.Formatter(
    '%(asctime)s - RAW: %(message)s',
    datefmt='%H:%M:%S'
))
raw_logger.addHandler(raw_handler)
raw_logger.setLevel(logging.INFO)
raw_logger.propagate = False

# ========== FLASK ==========

app = Flask(__name__)
app.config['SECRET_KEY'] = 'wifi-car-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
esp_client = None
esp_connected = False
esp_address = None
registered_commands = []

# ========== ИСПРАВЛЕНИЕ JSON ==========

def fix_unescaped_quotes_in_stdout(json_str):
    """
    Исправляет неэкранированные кавычки ТОЛЬКО внутри поля "stdout"
    Было: {"stdout":"{"key":"value"}"}
    Стало: {"stdout":"{\"key\":\"value\"}"}
    """
    # Ищем "stdout":" и обрабатываем только эту часть
    pattern = r'("stdout"\s*:\s*")(.+?)(")(?=\s*[,}])'
    
    def replace_stdout(match):
        prefix = match.group(1)  # "stdout":"
        value = match.group(2)    # содержимое
        suffix = match.group(3)   # закрывающая "
        
        # Экранируем кавычки внутри значения
        fixed = value.replace('\\', '\\\\').replace('"', '\\"')
        return prefix + fixed + suffix
    
    # Применяем замену
    fixed = re.sub(pattern, replace_stdout, json_str)
    return fixed

def safe_parse_json(line):
    """
    Безопасный парсинг: сначала как есть, потом с авто-исправлением
    """
    # Пробуем распарсить как есть
    try:
        return json.loads(line), None
    except json.JSONDecodeError:
        pass  # Пробуем исправить
    
    # Пробуем исправить неэкранированные кавычки в stdout
    try:
        fixed = fix_unescaped_quotes_in_stdout(line)
        if fixed != line:
            raw_logger.info(f"Fixed: {line[:80]}...")
        return json.loads(fixed), "Fixed stdout quotes"
    except Exception:
        return None, "Cannot parse"

# ========== ЛОГГИРОВАНИЕ ==========

def log_json_message(direction, message, source="ESP"):
    """Красивый вывод JSON с цветами"""
    arrow = "→" if direction == "OUT" else "←"
    color = "\033[92m" if direction == "OUT" else "\033[94m"
    reset = "\033[0m"
    
    try:
        # Пытаемся отформатировать для красоты
        data = json.loads(message) if isinstance(message, str) else message
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
        json_logger.info(f"{color}{arrow} [{source}] {reset}\n{formatted}")
    except:
        # Если не JSON — выводим как есть
        json_logger.info(f"{color}{arrow} [{source}] {reset}{message}")

# ========== TCP SERVER ==========

def start_tcp_server():
    global esp_client, esp_connected, esp_address, registered_commands
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.settimeout(2)
    server_socket.bind(('0.0.0.0', 5001))
    server_socket.listen(1)
    
    logger.info("=" * 60)
    logger.info("📡 TCP Server started on port 5001")
    logger.info("=" * 60)
    
    while True:
        try:
            esp_client, esp_address = server_socket.accept()
            esp_client.setblocking(0)
            esp_connected = True
            registered_commands = []
            
            logger.info("=" * 60)
            logger.info(f"✅ ESP CONNECTED from {esp_address[0]}:{esp_address[1]}")
            logger.info("=" * 60)
            
            socketio.emit('esp_status', {'connected': True, 'address': str(esp_address)})
            
            while esp_connected:
                try:
                    data = esp_client.recv(4096)
                    if data:
                        try:
                            decoded = data.decode('utf-8')
                            lines = decoded.strip().split('\n')
                            
                            for line in lines:
                                if line.strip():
                                    raw_logger.info(f"← [ESP] {line}")
                                    handle_arduino_message(line.strip())
                        except UnicodeDecodeError as e:
                            logger.error(f"❌ Decode error: {e}")
                    else:
                        logger.warning("⚠️ ESP disconnected")
                        break
                except socket.error:
                    time.sleep(0.05)
                    continue
                except Exception as e:
                    logger.error(f"❌ Receive error: {e}")
                    break
            
            # Отключение
            esp_connected = False
            if esp_client:
                try: esp_client.close()
                except: pass
            esp_client = None
            
            logger.info("=" * 60)
            logger.info("❌ ESP DISCONNECTED")
            logger.info("=" * 60)
            
            socketio.emit('esp_status', {'connected': False})
            registered_commands = []
            
        except socket.timeout:
            continue
        except Exception as e:
            logger.error(f"❌ TCP error: {e}")
            time.sleep(1)

def handle_arduino_message(line):
    global registered_commands
    
    # Безопасный парсинг
    data, fix_msg = safe_parse_json(line)
    
    if data is None:
        logger.error(f"❌ Invalid JSON: {line[:100]}")
        return
    
    if fix_msg:
        logger.info(f"✅ Auto-fixed: {fix_msg}")
    
    try:
        msg_type = data.get('type', 'unknown')
        logger.info(f"📥 Processing: {msg_type}")
        
        if msg_type == 'capabilities':
            registered_commands = data.get('commands', [])
            logger.info(f"✅ Registered {len(registered_commands)} commands")
            socketio.emit('capabilities', {'commands': registered_commands})
            
        elif msg_type == 'telemetry':
            socketio.emit('telemetry', data.get('data', {}))
            
        elif msg_type == 'command_result':
            logger.info(f"✅ Result: {data.get('command')} - exit_code={data.get('exit_code')}")
            socketio.emit('command_result', data)
            
        elif msg_type == 'error':
            logger.error(f"❌ Arduino error: {data.get('message')}")
            socketio.emit('arduino_error', data)
            
        elif msg_type == 'system':
            logger.info(f"💬 Arduino: {data.get('message')}")
            
        else:
            socketio.emit('arduino_raw', data)
            
    except Exception as e:
        logger.error(f"❌ Handle error: {e}")

def send_to_esp(message):
    global esp_client, esp_connected
    
    if esp_client and esp_connected:
        try:
            log_json_message("OUT", message, "SERVER")
            esp_client.sendall((message + '\n').encode('utf-8'))
            logger.info(f"✅ Sent to ESP ({len(message)} bytes)")
            return True
        except Exception as e:
            logger.error(f"❌ Send error: {e}")
            esp_connected = False
            return False
    else:
        logger.warning("⚠️ ESP not connected")
        return False

# ========== WEBSOCKET ==========

@socketio.on('connect')
def handle_connect():
    logger.info(f"🌐 Web client connected: {request.sid}")
    emit('esp_status', {'connected': esp_connected, 'address': str(esp_address) if esp_address else None})
    if registered_commands:
        emit('capabilities', {'commands': registered_commands})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"🌐 Web client disconnected: {request.sid}")

@socketio.on('send_command')
def handle_send_command(data):
    command = data.get('command')
    params = data.get('params', {})
    command_id = data.get('command_id', f'cmd_{int(time.time()*1000)}')
    
    cmd_json = {'cmd': command, 'command_id': command_id, **params}
    json_str = json.dumps(cmd_json)
    
    logger.info(f"🎯 Command from web: {command}")
    
    success = send_to_esp(json_str)
    if not success:
        emit('command_error', {'command_id': command_id, 'error': 'ESP not connected'})

@socketio.on('request_capabilities')
def handle_request_capabilities():
    global esp_connected
    
    logger.info("=" * 60)
    logger.info("🔄 CAPABILITIES REQUESTED FROM WEB")
    logger.info("=" * 60)
    
    if esp_connected:
        request_json = '{"cmd":"get_capabilities"}'
        success = send_to_esp(request_json)
        if success:
            logger.info("✅ Capabilities request sent to Arduino")
        else:
            emit('command_error', {'error': 'Failed to send request'})
    else:
        logger.warning("⚠️ ESP not connected")
        emit('command_error', {'error': 'ESP not connected'})

# ========== HTTP ==========

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/commands')
def get_commands():
    return jsonify(registered_commands)

@app.route('/api/status')
def get_status():
    return jsonify({
        'esp_connected': esp_connected,
        'esp_address': str(esp_address) if esp_address else None,
        'commands_count': len(registered_commands)
    })

# ========== ЗАПУСК ==========

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("🚗 WIFI CAR CONTROL SERVER")
    logger.info("Web: http://localhost:5000")
    logger.info("ESP TCP: port 5001")
    logger.info("=" * 60)
    
    tcp_thread = threading.Thread(target=start_tcp_server, daemon=True)
    tcp_thread.start()
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)