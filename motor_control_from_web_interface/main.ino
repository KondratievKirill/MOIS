#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>

// ================= НАСТРОЙКИ WI-FI =================
const char* ssid = "S25_FE_user";
const char* password = "nmcw44ecvdcizs5";

ESP8266WebServer server(80);

// Переменные скорости (в формате u: 0=макс, 255=стоп)
int leftSpeed = 255;   // По умолчанию стоп
int rightSpeed = 255;

// Телеметрия от Arduino
String telemetryData = "No data";

// ================= КОНСТАНТЫ СКОРОСТИ =================
const int SPEED_STOP = 255;    // Остановка
const int SPEED_SLOW = 180;    // Медленно
const int SPEED_MEDIUM = 100;  // Средняя
const int SPEED_FAST = 50;     // Быстро
const int SPEED_MAX = 20;      // Максимальная (но не 0, чтобы был запас)

// ================= HTML ИНТЕРФЕЙС С WASD =================
const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WASD Управление роботом</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            color: white;
            user-select: none;
        }
        
        .container {
            background: rgba(255,255,255,0.1);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 500px;
            width: 100%;
            text-align: center;
            backdrop-filter: blur(10px);
        }
        
        h1 {
            margin-bottom: 10px;
            font-size: 24px;
        }
        
        .subtitle {
            color: #aaa;
            font-size: 14px;
            margin-bottom: 30px;
        }
        
        /* WASD контроллер */
        .wasd-container {
            display: grid;
            grid-template-columns: repeat(3, 70px);
            grid-template-rows: repeat(3, 70px);
            gap: 10px;
            justify-content: center;
            margin: 0 auto 30px;
        }
        
        .wasd-btn {
            background: linear-gradient(145deg, #3a3a5a, #2a2a4a);
            border: 2px solid #5a5a8a;
            border-radius: 12px;
            color: white;
            font-size: 20px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.1s;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 10px rgba(0,0,0,0.3);
        }
        
        .wasd-btn:active, .wasd-btn.active {
            background: linear-gradient(145deg, #5a5a8a, #4a4a7a);
            transform: scale(0.95);
            box-shadow: 0 2px 5px rgba(0,0,0,0.3);
            border-color: #7a7aaa;
        }
        
        .wasd-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        /* Позиции кнопок */
        .btn-w { grid-column: 2; grid-row: 1; }
        .btn-a { grid-column: 1; grid-row: 2; }
        .btn-s { grid-column: 2; grid-row: 2; }
        .btn-d { grid-column: 3; grid-row: 2; }
        
        /* Индикатор скорости */
        .speed-indicator {
            background: rgba(0,0,0,0.3);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 20px;
        }
        
        .speed-label {
            font-size: 12px;
            color: #aaa;
            margin-bottom: 5px;
        }
        
        .speed-values {
            display: flex;
            justify-content: space-around;
            font-family: monospace;
            font-size: 16px;
        }
        
        .speed-value {
            padding: 5px 15px;
            background: rgba(255,255,255,0.1);
            border-radius: 5px;
        }
        
        /* Телеметрия */
        .telemetry {
            background: rgba(0,0,0,0.3);
            padding: 15px;
            border-radius: 10px;
            text-align: left;
            font-family: monospace;
            font-size: 13px;
            min-height: 80px;
        }
        
        .telemetry-title {
            font-weight: bold;
            margin-bottom: 8px;
            color: #7a7aaa;
        }
        
        /* Подсказки */
        .hints {
            margin-top: 20px;
            font-size: 12px;
            color: #888;
            line-height: 1.6;
        }
        
        .key {
            display: inline-block;
            background: rgba(255,255,255,0.2);
            padding: 2px 8px;
            border-radius: 4px;
            margin: 0 2px;
            font-weight: bold;
        }
        
        /* Статус подключения */
        .connection {
            margin-top: 15px;
            font-size: 12px;
            color: #7a7aaa;
        }
        
        .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #4ade80;
            margin-right: 5px;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        /* Адаптив для мобильных */
        @media (max-width: 400px) {
            .wasd-container {
                grid-template-columns: repeat(3, 55px);
                grid-template-rows: repeat(3, 55px);
            }
            .wasd-btn {
                font-size: 16px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 WASD Control</h1>
        <div class="subtitle">Управление с клавиатуры</div>
        
        <!-- WASD контроллер -->
        <div class="wasd-container">
            <button class="wasd-btn btn-w" id="btnW">W</button>
            <button class="wasd-btn btn-a" id="btnA">A</button>
            <button class="wasd-btn btn-s" id="btnS">S</button>
            <button class="wasd-btn btn-d" id="btnD">D</button>
        </div>
        
        <!-- Индикатор текущих скоростей -->
        <div class="speed-indicator">
            <div class="speed-label">Текущее управление (0=макс, 255=стоп):</div>
            <div class="speed-values">
                <div class="speed-value">L: <span id="leftVal">255</span></div>
                <div class="speed-value">R: <span id="rightVal">255</span></div>
            </div>
        </div>
        
        <!-- Телеметрия -->
        <div class="telemetry">
            <div class="telemetry-title">📊 Телеметрия:</div>
            <div id="telemetry">Загрузка...</div>
        </div>
        
        <!-- Подсказки -->
        <div class="hints">
            <div><span class="key">W</span> Вперёд</div>
            <div><span class="key">S</span> Стоп</div>
            <div><span class="key">A</span> Поворот влево</div>
            <div><span class="key">D</span> Поворот вправо</div>
            <div style="margin-top: 8px;">
                <span class="key">Shift+W</span> Макс. скорость
            </div>
        </div>
        
        <!-- Статус -->
        <div class="connection">
            <span class="status-dot"></span>
            <span id="connectionText">Подключено</span>
        </div>
    </div>
    
    <script>
        // Элементы управления
        const btnW = document.getElementById('btnW');
        const btnA = document.getElementById('btnA');
        const btnS = document.getElementById('btnS');
        const btnD = document.getElementById('btnD');
        const leftVal = document.getElementById('leftVal');
        const rightVal = document.getElementById('rightVal');
        const telemetryDiv = document.getElementById('telemetry');
        
        // Текущее состояние
        let currentL = 255;
        let currentR = 255;
        let activeKeys = new Set();
        
        // Отправка команды на сервер
        function sendCommand(L, R) {
            currentL = L;
            currentR = R;
            leftVal.textContent = L;
            rightVal.textContent = R;
            
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/set?L=' + L + '&R=' + R, true);
            xhr.send();
        }
        
        // Расчет управления по нажатым клавишам
        function updateControl() {
            let L = 255;  // По умолчанию стоп
            let R = 255;
            
            const fast = activeKeys.has('Shift');
            const baseSpeed = fast ? SPEED_MAX : SPEED_MEDIUM;
            
            // W = вперёд
            if (activeKeys.has('w') || activeKeys.has('W')) {
                L = baseSpeed;
                R = baseSpeed;
            }
            
            // A = поворот влево (левый медленнее, правый быстрее)
            if (activeKeys.has('a') || activeKeys.has('A')) {
                L = SPEED_SLOW;   // Левый тормозит
                R = baseSpeed;    // Правый тянет
            }
            
            // D = поворот вправо (правый медленнее, левый быстрее)
            if (activeKeys.has('d') || activeKeys.has('D')) {
                L = baseSpeed;    // Левый тянет
                R = SPEED_SLOW;   // Правый тормозит
            }
            
            // Если нажаты одновременно A и D — стоп
            if ((activeKeys.has('a') || activeKeys.has('A')) && 
                (activeKeys.has('d') || activeKeys.has('D'))) {
                L = 255;
                R = 255;
            }
            
            // S = стоп (приоритет)
            if (activeKeys.has('s') || activeKeys.has('S')) {
                L = 255;
                R = 255;
            }
            
            sendCommand(L, R);
            
            // Визуальная подсветка кнопок
            btnW.classList.toggle('active', activeKeys.has('w') || activeKeys.has('W'));
            btnA.classList.toggle('active', activeKeys.has('a') || activeKeys.has('A'));
            btnS.classList.toggle('active', activeKeys.has('s') || activeKeys.has('S'));
            btnD.classList.toggle('active', activeKeys.has('d') || activeKeys.has('D'));
        }
        
        // Обработчики клавиатуры
        document.addEventListener('keydown', (e) => {
            // Игнорируем повторные события при удержании
            if (e.repeat) return;
            
            // Разрешенные клавиши
            if ('wasdWASDShift'.includes(e.key)) {
                e.preventDefault();
                activeKeys.add(e.key);
                updateControl();
            }
        });
        
        document.addEventListener('keyup', (e) => {
            if ('wasdWASDShift'.includes(e.key)) {
                e.preventDefault();
                activeKeys.delete(e.key);
                updateControl();
            }
        });
        
        // Обработчики для кнопок на экране (тач/мышь)
        function btnPress(key) {
            activeKeys.add(key);
            updateControl();
        }
        
        function btnRelease(key) {
            activeKeys.delete(key);
            updateControl();
        }
        
        // Мышь
        btnW.addEventListener('mousedown', () => btnPress('W'));
        btnA.addEventListener('mousedown', () => btnPress('A'));
        btnS.addEventListener('mousedown', () => btnPress('S'));
        btnD.addEventListener('mousedown', () => btnPress('D'));
        
        document.addEventListener('mouseup', () => {
            activeKeys.clear();
            updateControl();
        });
        
        // Тач
        btnW.addEventListener('touchstart', (e) => { e.preventDefault(); btnPress('W'); });
        btnA.addEventListener('touchstart', (e) => { e.preventDefault(); btnPress('A'); });
        btnS.addEventListener('touchstart', (e) => { e.preventDefault(); btnPress('S'); });
        btnD.addEventListener('touchstart', (e) => { e.preventDefault(); btnPress('D'); });
        
        document.addEventListener('touchend', () => {
            activeKeys.clear();
            updateControl();
        });
        
        // Обновление телеметрии
        function updateTelemetry() {
            fetch('/telemetry')
                .then(r => r.text())
                .then(data => {
                    // Форматируем телеметрию для отображения
                    if (data.startsWith('X:')) {
                        const parts = data.split(' ');
                        if (parts[0]) {
                            const vals = parts[0].substring(2).split(',');
                            let formatted = `vL: ${parseFloat(vals[0]).toFixed(2)} м/с | vR: ${parseFloat(vals[1]).toFixed(2)} м/s\n`;
                            formatted += `x: ${parseFloat(vals[2]).toFixed(3)} м | θ: ${(parseFloat(vals[3])*180/Math.PI).toFixed(1)}°`;
                            if (parts[1] && parts[1].startsWith('U:')) {
                                const u = parts[1].substring(2).split(',');
                                formatted += `\nU: ${u[0]} | ${u[1]}`;
                            }
                            telemetryDiv.textContent = formatted;
                        }
                    } else {
                        telemetryDiv.textContent = data;
                    }
                })
                .catch(() => {
                    telemetryDiv.textContent = '❌ Ошибка соединения';
                });
        }
        
        // Фокус на страницу для захвата клавиатуры
        window.addEventListener('click', () => {
            document.body.focus();
        });
        
        // Запуск
        setInterval(updateTelemetry, 200);
        updateTelemetry();
        
        // Начальное состояние - стоп
        sendCommand(255, 255);
        
        console.log('WASD Control Ready. Click anywhere to focus.');
    </script>
</body>
</html>
)rawliteral";

// ================= ОБРАБОТЧИКИ ЗАПРОСОВ =================

void handleRoot() {
    server.send(200, "text/html", INDEX_HTML);
}

void handleSetSpeed() {
    if (server.hasArg("L") && server.hasArg("R")) {
        leftSpeed = server.arg("L").toInt();
        rightSpeed = server.arg("R").toInt();
        
        // Ограничения
        leftSpeed = constrain(leftSpeed, 0, 255);
        rightSpeed = constrain(rightSpeed, 0, 255);
        
        // Отправляем команду на Arduino
        String cmd = "L:" + String(leftSpeed) + " R:" + String(rightSpeed);
        Serial.println(cmd);
        
        server.send(200, "text/plain", "OK");
    } else {
        server.send(400, "text/plain", "Bad Request");
    }
}

void handleTelemetry() {
    server.send(200, "text/plain", telemetryData);
}

void handleNotFound() {
    server.send(404, "text/plain", "Not Found");
}

// ================= SETUP =================
void setup() {
    Serial.begin(115200);
    delay(10);
    
    Serial.println();
    Serial.println("========================================");
    Serial.println("ESP-01 WASD Web Server");
    Serial.println("========================================");
    
    WiFi.begin(ssid, password);
    Serial.print("Connecting to WiFi");
    
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    
    Serial.println();
    Serial.println("✅ WiFi connected!");
    Serial.print("📡 IP: ");
    Serial.println(WiFi.localIP());
    Serial.println("========================================");
    
    server.on("/", handleRoot);
    server.on("/set", handleSetSpeed);
    server.on("/telemetry", handleTelemetry);
    server.onNotFound(handleNotFound);
    
    server.begin();
    Serial.println("🌐 Server started!");
    Serial.println("Open: http://" + WiFi.localIP().toString());
    Serial.println("Controls: W=forward, S=stop, A=left, D=right");
    Serial.println("Hold Shift+W for max speed");
    Serial.println("========================================");
}

// ================= LOOP =================
void loop() {
    server.handleClient();
    
    if (Serial.available()) {
        String data = Serial.readStringUntil('\n');
        data.trim();
        if (data.length() > 0) {
            telemetryData = data;
        }
    }
}