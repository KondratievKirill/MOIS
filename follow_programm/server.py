from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import socket
import json
import threading
import time
import math
import logging
import re
import uuid
import ast
import importlib
from pathlib import Path
from datetime import datetime

# ========== НАСТРОЙКА ЛОГГЕРОВ ==========

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

json_logger = logging.getLogger('JSON_MESSAGES')
json_handler = logging.StreamHandler()
json_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
json_logger.addHandler(json_handler)
json_logger.setLevel(logging.INFO)
json_logger.propagate = False

raw_logger = logging.getLogger('RAW_MESSAGES')
raw_handler = logging.StreamHandler()
raw_handler.setFormatter(logging.Formatter('%(asctime)s - RAW: %(message)s', datefmt='%H:%M:%S'))
raw_logger.addHandler(raw_handler)
raw_logger.setLevel(logging.INFO)
raw_logger.propagate = False

# ========== FLASK ==========

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / 'scripts'
SCRIPTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'wifi-car-secret'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
esp_client = None
esp_connected = False
esp_address = None
registered_commands = []
recv_buffer = ''

pending_commands = {}
pending_lock = threading.Lock()

# Текущая логика стопа. По умолчанию соответствует имеющейся прошивке: PWM=0 это stop.
# Если в UI включить инвертированный PWM, аварийный stop будет отправляться как PWM=255.
MOTOR_PWM_INVERTED = True

# ========== SCRIPT RUNNER ==========

class ScriptRunner:
    def __init__(self):
        self.thread = None
        self.lock = threading.Lock()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self.current_script = None
        self.state = 'idle'
        self.started_at = None

    def snapshot(self):
        return {
            'state': self.state,
            'current_script': self.current_script,
            'started_at': self.started_at,
        }

    def emit_state(self):
        socketio.emit('script_state', self.snapshot())

    def log(self, message, level='info'):
        payload = {
            'message': message,
            'level': level,
            'ts': datetime.now().strftime('%H:%M:%S')
        }
        socketio.emit('script_log', payload)
        if level == 'error':
            logger.error(f'[SCRIPT] {message}')
        else:
            logger.info(f'[SCRIPT] {message}')

    def ensure_not_stopped(self):
        if self.stop_event.is_set():
            raise RuntimeError('Script stopped by user')
        while not self.pause_event.is_set():
            if self.stop_event.is_set():
                raise RuntimeError('Script stopped by user')
            time.sleep(0.05)

    def _validate_ast(self, code: str):
        # Разрешаем рабочие конструкции для пользовательских скриптов,
        # включая try/except/finally и raise. Запрещаем только опасные
        # операции, импорты и доступ к чувствительным атрибутам.
        tree = ast.parse(code, mode='exec')

        allowed_imports = {
            'time', 'math', 'random', 'statistics', 'json', 're',
            'numpy', 'numpy.linalg',
            'scipy', 'scipy.linalg', 'scipy.signal',
        }

        banned_nodes = (
            ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
            ast.ClassDef, ast.Lambda, ast.Delete, ast.Yield, ast.YieldFrom, ast.Await,
            ast.AsyncFunctionDef,
        )
        banned_calls = {
            'open', 'exec', 'eval', 'compile', '__import__', 'input',
            'globals', 'locals', 'vars', 'dir', 'getattr', 'setattr', 'delattr',
            'help', 'breakpoint',
        }
        banned_attrs = {
            '__class__', '__dict__', '__bases__', '__mro__', '__subclasses__',
            '__globals__', '__code__', '__closure__', '__func__', '__self__',
            '__getattribute__', '__getattr__', '__setattr__', '__delattr__',
            '__reduce__', '__reduce_ex__', '__loader__', '__spec__', '__builtins__',
            'system', 'popen', 'spawn', 'fork', 'execv', 'execve', 'remove', 'unlink',
            'rmdir', 'rename', 'replace', 'chmod', 'chown', 'mkdir', 'makedirs',
        }

        def import_allowed(module_name: str) -> bool:
            return any(module_name == allowed or module_name.startswith(allowed + '.') for allowed in allowed_imports)

        for node in ast.walk(tree):
            if isinstance(node, banned_nodes):
                raise ValueError(f'Запрещённая конструкция: {type(node).__name__}')

            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not import_allowed(alias.name):
                        raise ValueError(f'Импорт модуля {alias.name} запрещён')

            if isinstance(node, ast.ImportFrom):
                if node.level and node.level != 0:
                    raise ValueError('Относительные импорты запрещены')
                if node.module is None or not import_allowed(node.module):
                    raise ValueError(f'Импорт из модуля {node.module} запрещён')
                for alias in node.names:
                    if alias.name == '*':
                        raise ValueError('Импорт через * запрещён')

            if isinstance(node, ast.Attribute):
                if node.attr.startswith('__') or node.attr in banned_attrs:
                    raise ValueError(f'Доступ к атрибуту {node.attr} запрещён')
                if isinstance(node.value, ast.Name) and node.value.id.startswith('__'):
                    raise ValueError('Доступ к dunder-атрибутам запрещён')

            if isinstance(node, ast.Name) and node.id.startswith('__'):
                raise ValueError('Использование dunder-имен запрещено')

            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in banned_calls:
                        raise ValueError(f'Вызов {node.func.id} запрещён')
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr.startswith('__') or node.func.attr in banned_attrs:
                        raise ValueError(f'Вызов атрибута {node.func.attr} запрещён')

        return tree

    def _build_api(self):
        allowed_imports = {
            'time', 'math', 'random', 'statistics', 'json', 're',
            'numpy', 'numpy.linalg',
            'scipy', 'scipy.linalg', 'scipy.signal', 'isinstance'
        }

        def import_allowed(module_name: str) -> bool:
            return any(module_name == allowed or module_name.startswith(allowed + '.') for allowed in allowed_imports)

        def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level and level != 0:
                raise ImportError('Относительные импорты запрещены')
            if not import_allowed(name):
                raise ImportError(f'Импорт модуля {name} запрещён')
            if name == 'time':
                module = TimeProxy
            else:
                module = importlib.import_module(name)
            if fromlist:
                for item in fromlist:
                    if item == '*':
                        raise ImportError('Импорт через * запрещён')
                    candidate = f'{name}.{item}'
                    if import_allowed(candidate):
                        continue
                    if not hasattr(module, item):
                        raise ImportError(f'Модуль {name} не содержит {item}')
            return module

        def make_command(command_name, timeout):
            def _command(**kwargs):
                self.ensure_not_stopped()
                payload = {
                    'cmd': command_name,
                    'command_id': str(uuid.uuid4()),
                    **kwargs,
                }
                self.log(f'→ {command_name} {kwargs}', 'info')
                result = send_json_command_and_wait(payload, timeout=timeout)
                if int(result.get('exit_code', 1)) != 0:
                    raise RuntimeError(f'{command_name} failed: {result.get("stderr") or result.get("stdout") or result}')
                stdout = result.get('stdout')
                parsed = try_parse_embedded_json(stdout)
                return parsed if parsed is not None else result
            return _command

        def _print(*args, **kwargs):
            parts = [str(x) for x in args]
            if kwargs:
                parts.append(str(kwargs))
            self.log(' '.join(parts), 'info')

        class TimeProxy:
            @staticmethod
            def sleep(seconds):
                try:
                    seconds = float(seconds)
                except Exception as exc:
                    raise ValueError('time.sleep ожидает число') from exc
                if seconds < 0:
                    raise ValueError('time.sleep не принимает отрицательные значения')
                end_at = time.time() + min(seconds, 5.0)
                while time.time() < end_at:
                    self.ensure_not_stopped()
                    time.sleep(min(0.05, end_at - time.time()))

        safe_builtins = {
            '__import__': safe_import,
            'range': range,
            'len': len,
            'min': min,
            'max': max,
            'abs': abs,
            'int': int,
            'float': float,
            'str': str,
            'bool': bool,
            'sum': sum,
            'round': round,
            'enumerate': enumerate,
            'zip': zip,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
            'sorted': sorted,
            'reversed': reversed,
            'map': map,          # <-- ДОБАВЛЕНО
            'filter': filter,    # <-- ДОБАВЛЕНО (опционально, но полезно)
            'iter': iter,        # <-- ДОБАВЛЕНО (для полноты функционала)
            'isinstance': isinstance,
            'issubclass': issubclass,
            'type': type,
            'object': object,
            'Exception': Exception,
            'RuntimeError': RuntimeError,
            'ValueError': ValueError,
            'TypeError': TypeError,
            'KeyError': KeyError,
            'IndexError': IndexError,
            'AttributeError': AttributeError,
            'ImportError': ImportError,
            'TimeoutError': TimeoutError,
            'print': _print,
        }

        api = {
            'motor': make_command('motor', timeout=0.75),
            'duration': lambda: make_command('duration', timeout=1.5)(),
            'mpu6050': lambda: make_command('mpu6050', timeout=1.5)(),
            'ping': lambda: make_command('ping', timeout=1.0)(),
            'time': TimeProxy,
            'print': _print,
            'range': range,
            'len': len,
            'min': min,
            'max': max,
            'abs': abs,
            'int': int,
            'float': float,
            'str': str,
            'bool': bool,
            'sum': sum,
            'round': round,
            'enumerate': enumerate,
            'zip': zip,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
            'sorted': sorted,
            'reversed': reversed,
            'isinstance': isinstance,
            'issubclass': issubclass,
            'type': type,
            'object': object,
            'Exception': Exception,
            'RuntimeError': RuntimeError,
            'ValueError': ValueError,
            'TypeError': TypeError,
            'KeyError': KeyError,
            'IndexError': IndexError,
            'AttributeError': AttributeError,
            'ImportError': ImportError,
            'TimeoutError': TimeoutError,
            '__builtins__': safe_builtins,
        }
        return api

    def _run(self, script_name: str, code: str):
        try:
            if not esp_connected:
                raise RuntimeError('ESP не подключена')
            self._validate_ast(code)
            api = self._build_api()
            compiled = compile(code, script_name, 'exec')
            self.log(f'Старт скрипта: {script_name}', 'success')
            exec(compiled, api, api)
            self.log(f'Скрипт завершён: {script_name}', 'success')
        except Exception as exc:
            self.log(f'Ошибка выполнения: {exc}', 'error')
            try_emergency_stop('script error')
        finally:
            if self.stop_event.is_set():
                try_emergency_stop('script stopped')
            with self.lock:
                self.thread = None
                self.current_script = None
                self.state = 'idle'
                self.started_at = None
                self.stop_event.clear()
                self.pause_event.set()
            self.emit_state()

    def start(self, script_name: str, code: str):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('Уже выполняется другой скрипт')
            self.stop_event.clear()
            self.pause_event.set()
            self.current_script = script_name
            self.state = 'running'
            self.started_at = datetime.now().isoformat(timespec='seconds')
            self.thread = threading.Thread(target=self._run, args=(script_name, code), daemon=True)
            self.thread.start()
        self.emit_state()

    def pause(self):
        with self.lock:
            if self.state != 'running':
                raise RuntimeError('Пауза доступна только для running')
            self.pause_event.clear()
            self.state = 'paused'
        self.log('Скрипт поставлен на паузу', 'info')
        self.emit_state()

    def resume(self):
        with self.lock:
            if self.state != 'paused':
                raise RuntimeError('Resume доступен только для paused')
            self.pause_event.set()
            self.state = 'running'
        self.log('Скрипт продолжен', 'info')
        self.emit_state()

    def stop(self):
        with self.lock:
            if self.state not in ('running', 'paused'):
                raise RuntimeError('Нет активного скрипта для остановки')
            self.stop_event.set()
            self.pause_event.set()
            self.state = 'stopping'
        self.log('Запрошена остановка скрипта', 'info')
        self.emit_state()


def try_emergency_stop(reason=''):
    try:
        if esp_connected:
            payload = {
                'cmd': 'motor',
                'command_id': str(uuid.uuid4()),
                'left_pwm': 255 if MOTOR_PWM_INVERTED else 0,
                'right_pwm': 255 if MOTOR_PWM_INVERTED else 0,
                'left_dir': 'forward',
                'right_dir': 'forward',
            }
            send_to_esp(json.dumps(payload, ensure_ascii=False))
            suffix = f": {reason}" if reason else ''
            logger.warning(f'🛑 Emergency stop sent{suffix}')
    except Exception as exc:
        logger.error(f'Failed to send emergency stop: {exc}')

script_runner = ScriptRunner()

# ========== ИСПРАВЛЕНИЕ JSON ==========

def fix_unescaped_quotes_in_stdout(json_str):
    pattern = r'("stdout"\s*:\s*")(.+?)(")(?=\s*[,}])'

    def replace_stdout(match):
        prefix = match.group(1)
        value = match.group(2)
        suffix = match.group(3)
        fixed = value.replace('\\', '\\\\').replace('"', '\\"')
        return prefix + fixed + suffix

    return re.sub(pattern, replace_stdout, json_str)


def safe_parse_json(line):
    try:
        return json.loads(line), None
    except json.JSONDecodeError:
        pass

    try:
        fixed = fix_unescaped_quotes_in_stdout(line)
        if fixed != line:
            raw_logger.info(f'Fixed: {line[:80]}...')
        return json.loads(fixed), 'Fixed stdout quotes'
    except Exception:
        return None, 'Cannot parse'


def try_parse_embedded_json(value):
    """Parse JSON even when firmware/bridge wrapped it with extra text."""
    if isinstance(value, dict) or isinstance(value, list):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    candidates = [text]

    obj_start = text.find('{')
    obj_end = text.rfind('}')
    if obj_start >= 0 and obj_end > obj_start:
        candidates.append(text[obj_start:obj_end + 1])

    arr_start = text.find('[')
    arr_end = text.rfind(']')
    if arr_start >= 0 and arr_end > arr_start:
        candidates.append(text[arr_start:arr_end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


def parse_distance_cm_from_stdout(stdout):
    parsed = try_parse_embedded_json(stdout)
    if isinstance(parsed, dict):
        for key in ('distance_cm', 'distanceCm', 'cm'):
            if key in parsed:
                return float(parsed[key])
        if 'duration_us' in parsed:
            return float(parsed['duration_us']) * 0.0343 / 2.0

    text = '' if stdout is None else str(stdout)

    match = re.search(r'(?:distance|dist|range)[^0-9+\-]*([+\-]?\d+(?:[.,]\d+)?)\s*(cm|сm|см|m|м)?', text, re.I)
    if not match:
        match = re.search(r'([+\-]?\d+(?:[.,]\d+)?)\s*(cm|сm|см|m|м)\b', text, re.I)

    if match:
        value = float(match.group(1).replace(',', '.'))
        unit = (match.group(2) or 'cm').lower()
        return value * 100.0 if unit in ('m', 'м') else value

    match = re.search(r'(?:duration|echo|us|мкс)[^0-9+\-]*([+\-]?\d+(?:[.,]\d+)?)', text, re.I)
    if match:
        return float(match.group(1).replace(',', '.')) * 0.0343 / 2.0

    raise ValueError(f'cannot parse distance from stdout: {text[:160]}')


def parse_gyro_z_from_stdout(stdout):
    parsed = try_parse_embedded_json(stdout)
    if isinstance(parsed, dict):
        for key in ('gyro_z', 'gz', 'gyroZ', 'z'):
            if key in parsed:
                return float(parsed[key])

    text = '' if stdout is None else str(stdout)
    match = re.search(r'(?:gyro[_\s-]*z|gz|z)[^0-9+\-]*([+\-]?\d+(?:[.,]\d+)?)', text, re.I)
    if match:
        return float(match.group(1).replace(',', '.'))

    raise ValueError(f'cannot parse gyro_z from stdout: {text[:160]}')

# ========== ЛОГГИРОВАНИЕ ==========

def log_json_message(direction, message, source='ESP'):
    arrow = '→' if direction == 'OUT' else '←'
    color = '\033[92m' if direction == 'OUT' else '\033[94m'
    reset = '\033[0m'
    try:
        data = json.loads(message) if isinstance(message, str) else message
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
        json_logger.info(f'{color}{arrow} [{source}] {reset}\n{formatted}')
    except Exception:
        json_logger.info(f'{color}{arrow} [{source}] {reset}{message}')

# ========== PENDING COMMANDS ==========

def register_pending_command(command_id):
    event = threading.Event()
    with pending_lock:
        pending_commands[command_id] = {
            'event': event,
            'result': None,
            'created_at': time.time(),
        }
    return event


def resolve_pending_command(command_id, result):
    if not command_id:
        return
    with pending_lock:
        item = pending_commands.get(command_id)
        if item:
            item['result'] = result
            item['event'].set()


def pop_pending_result(command_id):
    with pending_lock:
        item = pending_commands.pop(command_id, None)
    return item['result'] if item else None


def cleanup_stale_pending(max_age=10.0):
    now = time.time()
    stale = []
    with pending_lock:
        for command_id, item in pending_commands.items():
            if now - item['created_at'] > max_age:
                stale.append(command_id)
        for command_id in stale:
            pending_commands.pop(command_id, None)
    if stale:
        logger.warning(f'🧹 Removed stale pending commands: {len(stale)}')


def send_json_command_and_wait(payload, timeout=1.0):
    command_id = payload.get('command_id')
    if not command_id:
        raise ValueError('payload must contain command_id')

    event = register_pending_command(command_id)
    ok = send_to_esp(json.dumps(payload, ensure_ascii=False))
    if not ok:
        with pending_lock:
            pending_commands.pop(command_id, None)
        raise RuntimeError('ESP not connected or send failed')

    if not event.wait(timeout):
        with pending_lock:
            pending_commands.pop(command_id, None)
        raise TimeoutError(f'Timeout waiting for command_id={command_id}')

    result = pop_pending_result(command_id)
    if result is None:
        raise RuntimeError(f'No result for command_id={command_id}')
    return result

# ========== TCP SERVER ==========

def start_tcp_server():
    global esp_client, esp_connected, esp_address, registered_commands, recv_buffer

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.settimeout(2)
    server_socket.bind(('0.0.0.0', 5001))
    server_socket.listen(1)

    logger.info('=' * 60)
    logger.info('📡 TCP Server started on port 5001')
    logger.info('=' * 60)

    while True:
        try:
            esp_client, esp_address = server_socket.accept()
            esp_client.setblocking(False)
            esp_connected = True
            recv_buffer = ''
            registered_commands = []
            cleanup_stale_pending()

            logger.info('=' * 60)
            logger.info(f'✅ ESP CONNECTED from {esp_address[0]}:{esp_address[1]}')
            logger.info('=' * 60)

            socketio.emit('esp_status', {'connected': True, 'address': str(esp_address)})

            while esp_connected:
                try:
                    data = esp_client.recv(4096)
                    if not data:
                        logger.warning('⚠️ ESP disconnected')
                        break

                    recv_buffer += data.decode('utf-8', errors='ignore')

                    while '\n' in recv_buffer:
                        line, recv_buffer = recv_buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            raw_logger.info(f'← [ESP] {line}')
                            handle_arduino_message(line)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                except socket.error:
                    time.sleep(0.01)
                    continue
                except Exception as e:
                    logger.error(f'❌ Receive error: {e}')
                    break

            esp_connected = False
            if esp_client:
                try:
                    esp_client.close()
                except Exception:
                    pass
            esp_client = None

            logger.info('=' * 60)
            logger.info('❌ ESP DISCONNECTED')
            logger.info('=' * 60)

            socketio.emit('esp_status', {'connected': False})
            registered_commands = []

        except socket.timeout:
            continue
        except Exception as e:
            logger.error(f'❌ TCP error: {e}')
            time.sleep(1)


def handle_arduino_message(line):
    global registered_commands
    data, fix_msg = safe_parse_json(line)

    if data is None:
        logger.error(f'❌ Invalid JSON: {line[:160]}')
        return

    if fix_msg:
        logger.info(f'✅ Auto-fixed: {fix_msg}')

    try:
        msg_type = data.get('type', 'unknown')
        logger.info(f'📥 Processing: {msg_type}')

        if msg_type == 'capabilities':
            registered_commands = data.get('commands', [])
            logger.info(f'✅ Registered {len(registered_commands)} commands')
            socketio.emit('capabilities', {'commands': registered_commands})

        elif msg_type == 'telemetry':
            socketio.emit('telemetry', data.get('data', {}))

        elif msg_type == 'command_result':
            logger.info(f"✅ Result: {data.get('command')} - exit_code={data.get('exit_code')}")
            resolve_pending_command(data.get('command_id', ''), data)
            socketio.emit('command_result', data)

        elif msg_type == 'error':
            logger.error(f"❌ Arduino error: {data.get('message')}")
            socketio.emit('arduino_error', data)

        elif msg_type == 'system':
            logger.info(f"💬 Arduino: {data.get('message')}")
            socketio.emit('arduino_raw', data)

        else:
            socketio.emit('arduino_raw', data)

    except Exception as e:
        logger.error(f'❌ Handle error: {e}')


def send_to_esp(message):
    global esp_client, esp_connected
    if esp_client and esp_connected:
        try:
            log_json_message('OUT', message, 'SERVER')
            esp_client.sendall((message + '\n').encode('utf-8'))
            logger.info(f'✅ Sent to ESP ({len(message)} bytes)')
            return True
        except Exception as e:
            logger.error(f'❌ Send error: {e}')
            esp_connected = False
            return False
    logger.warning('⚠️ ESP not connected')
    return False

# ========== REAL-TIME FOLLOW CONTROLLER ==========

class FollowController:
    DEFAULTS = {
        'target_distance_m': 1.0, 'dt': 0.10, 'K': 1.0, 'tau': 0.2, 'W': 0.13,
        'kp_d': 0.85, 'kd_d': 0.28, 'k_z': 1.3, 'k_theta': 1.8, 'k_omega': 0.18,
        'vf_alpha': 0.25, 'dist_alpha': 0.35, 'gyro_alpha': 0.30, 'deriv_alpha': 0.75,
        'theta_leak': 0.997, 'max_u': 0.75, 'max_turn': 0.22, 'max_du': 0.05,
        'max_pwm_step': 14, 'min_valid_distance_m': 0.03, 'max_valid_distance_m': 3.0,
        'invert_pwm': True, 'gyro_bias_samples': 15, 'z_leak': 0.97, 'u_w_deadband': 0.006, 'max_du_turn': 0.12,
    }

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.running = False
        self.params = dict(self.DEFAULTS)
        self.reset_state()

    def reset_state(self):
        self.v_l = 0.0; self.v_r = 0.0; self.theta = 0.0; self.z = 0.0
        self.d_f = None; self.prev_d = None; self.d_dot_f = 0.0; self.vf_hat = 0.0
        self.gyro_z_f = 0.0; self.gyro_bias = 0.0; self.u_v_prev = 0.0; self.u_w_prev = 0.0
        self.pwm_l_prev = self.stop_pwm(); self.pwm_r_prev = self.stop_pwm(); self.step = 0

    def snapshot(self):
        with self.lock:
            return {'running': self.running, 'params': dict(self.params)}

    def update_params(self, params):
        global MOTOR_PWM_INVERTED
        with self.lock:
            # Аппаратная логика фиксированная: 0 = максимум, 255 = остановка.
            self.params['invert_pwm'] = True
            MOTOR_PWM_INVERTED = True
            for key, value in (params or {}).items():
                if key not in self.params or key == 'invert_pwm':
                    continue
                try:
                    self.params[key] = float(value)
                except Exception:
                    pass
        socketio.emit('follow_state', self.snapshot())

    @staticmethod
    def clamp(x, lo, hi):
        return max(lo, min(hi, x))

    @staticmethod
    def slew(current, target, max_step):
        return current + FollowController.clamp(target - current, -max_step, max_step)

    def stop_pwm(self):
        # Аппаратная логика: 255 = стоп, 0 = максимальная скорость.
        return 255

    def norm_to_pwm(self, u):
        # Нормированное управление u: 0 = стоп, 1 = максимум.
        # Реальный PWM инверсный: 255 = стоп, 0 = максимум.
        u = self.clamp(float(u), 0.0, 1.0)
        return int(round(255.0 * (1.0 - u)))

    def ramp_pwm(self, current, target, max_step):
        return int(round(self.slew(float(current), float(target), float(max_step))))

    def send_motor(self, pwm_l, pwm_r):
        return send_json_command_and_wait({
            'cmd': 'motor', 'command_id': str(uuid.uuid4()),
            'left_pwm': int(self.clamp(pwm_l, 0, 255)), 'right_pwm': int(self.clamp(pwm_r, 0, 255)),
            'left_dir': 'forward', 'right_dir': 'forward',
        }, timeout=0.75)

    def stop_motors(self, reason=''):
        try:
            pwm = self.stop_pwm()
            self.send_motor(pwm, pwm)
            logger.warning(f'[FOLLOW] stop motors {reason}'.strip())
        except Exception as exc:
            logger.error(f'[FOLLOW] stop failed: {exc}')

    def read_distance_m(self):
        result = send_json_command_and_wait({'cmd': 'duration', 'command_id': str(uuid.uuid4())}, timeout=1.2)
        if int(result.get('exit_code', 1)) != 0:
            raise RuntimeError(result.get('stderr') or result.get('stdout') or 'duration failed')
        try:
            return parse_distance_cm_from_stdout(result.get('stdout')) / 100.0
        except Exception as exc:
            raise RuntimeError(f'duration parse failed: {exc}') from exc

    def read_gyro_z(self):
        result = send_json_command_and_wait({'cmd': 'mpu6050', 'command_id': str(uuid.uuid4())}, timeout=1.2)
        if int(result.get('exit_code', 1)) != 0:
            raise RuntimeError(result.get('stderr') or result.get('stdout') or 'mpu6050 failed')
        try:
            return parse_gyro_z_from_stdout(result.get('stdout'))
        except Exception as exc:
            raise RuntimeError(f'mpu6050 parse failed: {exc}') from exc

    def calibrate_gyro(self):
        with self.lock:
            samples = int(self.params.get('gyro_bias_samples', 15)); dt = float(self.params.get('dt', 0.10))
        acc = 0.0; ok = 0
        for _ in range(max(1, samples)):
            if self.stop_event.is_set(): break
            try:
                acc += self.read_gyro_z(); ok += 1
            except Exception as exc:
                logger.warning(f'[FOLLOW] gyro calibration read failed: {exc}')
            time.sleep(min(0.04, dt))
        return acc / max(ok, 1)

    def start(self, params=None):
        if params: self.update_params(params)
        with self.lock:
            if self.thread and self.thread.is_alive(): raise RuntimeError('Follow controller already running')
            if not esp_connected: raise RuntimeError('ESP не подключена')
            self.reset_state(); self.stop_event.clear(); self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()
        socketio.emit('follow_state', self.snapshot())

    def stop(self):
        with self.lock:
            if not self.running: return
            self.stop_event.set()
        self.stop_motors('requested')
        socketio.emit('follow_state', self.snapshot())

    def _run(self):
        bad_distance = 0
        try:
            logger.info('[FOLLOW] controller starting')
            self.gyro_bias = self.calibrate_gyro()
            logger.info(f'[FOLLOW] gyro_bias={self.gyro_bias:.6f} rad/s')
            while not self.stop_event.is_set():
                t0 = time.time()
                with self.lock: p = dict(self.params)
                dt = max(0.03, float(p['dt'])); K = float(p['K']); tau = max(0.02, float(p['tau'])); target_d = float(p['target_distance_m'])
                d_raw = self.read_distance_m(); wz_raw = self.read_gyro_z() - self.gyro_bias
                if not (p['min_valid_distance_m'] <= d_raw <= p['max_valid_distance_m']):
                    bad_distance += 1
                    if bad_distance >= 5: raise RuntimeError(f'too many invalid distance reads: {d_raw:.3f} m')
                    d_raw = self.d_f if self.d_f is not None else target_d
                else:
                    bad_distance = 0
                if self.d_f is None:
                    self.d_f = d_raw; self.prev_d = d_raw
                else:
                    a = float(p['dist_alpha']); self.d_f = a * d_raw + (1.0 - a) * self.d_f
                ga = float(p['gyro_alpha']); self.gyro_z_f = ga * wz_raw + (1.0 - ga) * self.gyro_z_f
                d_dot_raw = (self.d_f - self.prev_d) / dt
                da = float(p['deriv_alpha']); self.d_dot_f = da * self.d_dot_f + (1.0 - da) * d_dot_raw
                self.prev_d = self.d_f
                v = 0.5 * (self.v_l + self.v_r)
                self.theta = float(p['theta_leak']) * (self.theta + self.gyro_z_f * dt)
                self.z = float(p.get('z_leak', 0.97)) * self.z + v * math.sin(self.theta) * dt
                vf_raw = self.clamp(self.d_dot_f + v, 0.0, K)
                va = float(p['vf_alpha']); self.vf_hat = va * vf_raw + (1.0 - va) * self.vf_hat
                e_d = self.d_f - target_d
                target_u_v = self.vf_hat / max(K, 1e-6) + float(p['kp_d']) * e_d + float(p['kd_d']) * self.d_dot_f
                target_u_v = self.clamp(target_u_v, 0.0, float(p['max_u']))
                target_u_w = -float(p['k_z']) * self.z - float(p['k_theta']) * self.theta - float(p['k_omega']) * self.gyro_z_f
                target_u_w = self.clamp(target_u_w, -float(p['max_turn']), float(p['max_turn']))
                if abs(target_u_w) < float(p.get('u_w_deadband', 0.006)):
                    target_u_w = 0.0
                self.u_v_prev = self.slew(self.u_v_prev, target_u_v, float(p['max_du']))
                self.u_w_prev = self.slew(self.u_w_prev, target_u_w, float(p.get('max_du_turn', p['max_du'])))
                u_l = self.clamp(self.u_v_prev - self.u_w_prev, 0.0, 1.0)
                u_r = self.clamp(self.u_v_prev + self.u_w_prev, 0.0, 1.0)
                target_pwm_l = self.norm_to_pwm(u_l); target_pwm_r = self.norm_to_pwm(u_r)
                self.pwm_l_prev = self.ramp_pwm(self.pwm_l_prev, target_pwm_l, float(p['max_pwm_step']))
                self.pwm_r_prev = self.ramp_pwm(self.pwm_r_prev, target_pwm_r, float(p['max_pwm_step']))
                self.send_motor(self.pwm_l_prev, self.pwm_r_prev)
                self.v_l += dt * ((K * u_l - self.v_l) / tau); self.v_r += dt * ((K * u_r - self.v_r) / tau)
                self.step += 1
                telemetry = {
                    'step': self.step, 'time': time.time(), 'd_raw': d_raw, 'd': self.d_f, 'd_target': target_d,
                    'e_d': e_d, 'd_dot': self.d_dot_f, 'v_l': self.v_l, 'v_r': self.v_r,
                    'v': 0.5 * (self.v_l + self.v_r), 'vf_hat': self.vf_hat,
                    'theta': self.theta, 'z': self.z, 'omega_z': self.gyro_z_f, 'gyro_bias': self.gyro_bias,
                    'u_l': u_l, 'u_r': u_r, 'u_v': self.u_v_prev, 'u_w': self.u_w_prev,
                    'pwm_l': self.pwm_l_prev, 'pwm_r': self.pwm_r_prev,
                }
                socketio.emit('follow_telemetry', telemetry)
                if self.step % max(1, int(round(1.0 / dt))) == 0:
                    msg = (
                        f"[FOLLOW][X] x=[vL={self.v_l:.4f}, vR={self.v_r:.4f}, "
                        f"theta={self.theta:.5f}, z={self.z:.5f}, d={self.d_f:.3f}] "
                        f"u=[uL={u_l:.4f}, uR={u_r:.4f}, uV={self.u_v_prev:.4f}, uW={self.u_w_prev:.4f}] "
                        f"pwm=[L={self.pwm_l_prev}, R={self.pwm_r_prev}] "
                        f"gyro=[wz={self.gyro_z_f:.5f}, bias={self.gyro_bias:.5f}]"
                    )
                    logger.info(msg)
                    socketio.emit('follow_log', {'level': 'info', 'message': msg})
                time.sleep(max(0.0, dt - (time.time() - t0)))
        except Exception as exc:
            logger.error(f'[FOLLOW] controller error: {exc}')
            socketio.emit('follow_error', {'error': str(exc)})
            self.stop_motors('error')
        finally:
            with self.lock:
                self.running = False; self.stop_event.clear()
            socketio.emit('follow_state', self.snapshot())
            logger.info('[FOLLOW] controller stopped')

follow_controller = FollowController()

# ========== SCRIPT STORAGE ==========

def sanitize_script_name(name: str) -> str:
    name = (name or '').strip()
    if not name:
        raise ValueError('Имя скрипта пустое')
    if not re.fullmatch(r'[A-Za-z0-9_\-\. ]{1,80}', name):
        raise ValueError('Имя скрипта содержит недопустимые символы')
    if not name.endswith('.py'):
        name += '.py'
    return name


def list_scripts():
    items = []
    for path in sorted(SCRIPTS_DIR.glob('*.py')):
        stat = path.stat()
        items.append({
            'name': path.name,
            'size': stat.st_size,
            'modified_at': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        })
    return items

# ========== WEBSOCKET ==========

@socketio.on('connect')
def handle_connect():
    logger.info(f'🌐 Web client connected: {request.sid}')
    emit('esp_status', {'connected': esp_connected, 'address': str(esp_address) if esp_address else None})
    if registered_commands:
        emit('capabilities', {'commands': registered_commands})
    emit('script_state', script_runner.snapshot())
    emit('scripts_list', {'scripts': list_scripts()})


@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f'🌐 Web client disconnected: {request.sid}')


@socketio.on('send_command')
def handle_send_command(data):
    command = data.get('command')
    params = data.get('params', {})
    command_id = data.get('command_id', f'cmd_{int(time.time()*1000)}')
    payload = {'cmd': command, 'command_id': command_id, **params}

    logger.info(f'🎯 Command from web: {command}')
    try:
        result = send_json_command_and_wait(payload, timeout=1.5)
        emit('command_result', result)
    except Exception as exc:
        emit('command_error', {'command_id': command_id, 'error': str(exc)})


@socketio.on('request_capabilities')
def handle_request_capabilities():
    logger.info('=' * 60)
    logger.info('🔄 CAPABILITIES REQUESTED FROM WEB')
    logger.info('=' * 60)

    if esp_connected:
        request_json = '{"cmd":"get_capabilities","command_id":"capabilities_request"}'
        success = send_to_esp(request_json)
        if success:
            logger.info('✅ Capabilities request sent to Arduino')
        else:
            emit('command_error', {'error': 'Failed to send request'})
    else:
        logger.warning('⚠️ ESP not connected')
        emit('command_error', {'error': 'ESP not connected'})


@socketio.on('save_script')
def handle_save_script(data):
    try:
        name = sanitize_script_name(data.get('name', 'script.py'))
        code = data.get('code', '')
        (SCRIPTS_DIR / name).write_text(code, encoding='utf-8')
        emit('script_saved', {'name': name})
        socketio.emit('scripts_list', {'scripts': list_scripts()})
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('load_script')
def handle_load_script(data):
    try:
        name = sanitize_script_name(data.get('name', ''))
        code = (SCRIPTS_DIR / name).read_text(encoding='utf-8')
        emit('script_loaded', {'name': name, 'code': code})
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('delete_script')
def handle_delete_script(data):
    try:
        name = sanitize_script_name(data.get('name', ''))
        path = SCRIPTS_DIR / name
        if path.exists():
            path.unlink()
        emit('script_deleted', {'name': name})
        socketio.emit('scripts_list', {'scripts': list_scripts()})
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('run_script')
def handle_run_script(data):
    try:
        name = sanitize_script_name(data.get('name', 'untitled.py'))
        code = data.get('code', '')
        script_runner.start(name, code)
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('pause_script')
def handle_pause_script():
    try:
        script_runner.pause()
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('resume_script')
def handle_resume_script():
    try:
        script_runner.resume()
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('stop_script')
def handle_stop_script():
    try:
        script_runner.stop()
    except Exception as exc:
        emit('script_error', {'error': str(exc)})


@socketio.on('follow_start')
def handle_follow_start(data):
    try:
        follow_controller.start((data or {}).get('params', {}))
    except Exception as exc:
        emit('follow_error', {'error': str(exc)})

@socketio.on('follow_stop')
def handle_follow_stop():
    try:
        follow_controller.stop()
    except Exception as exc:
        emit('follow_error', {'error': str(exc)})

@socketio.on('follow_update_params')
def handle_follow_update_params(data):
    try:
        follow_controller.update_params((data or {}).get('params', {}))
    except Exception as exc:
        emit('follow_error', {'error': str(exc)})

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
        'commands_count': len(registered_commands),
        'script': script_runner.snapshot(),
        'follow': follow_controller.snapshot(),
    })


@app.route('/api/scripts')
def get_scripts():
    return jsonify({'scripts': list_scripts()})

# ========== ЗАПУСК ==========

if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info('🚗 WIFI CAR CONTROL SERVER')
    logger.info('Web: http://localhost:5000')
    logger.info('ESP TCP: port 5001')
    logger.info('=' * 60)

    tcp_thread = threading.Thread(target=start_tcp_server, daemon=True)
    tcp_thread.start()

    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
