// ================================================================
// ПРОШИВКА ДЛЯ ARDUINO UNO
// ESP подключена к аппаратному Serial (пины 0 и 1)
// ================================================================

// ================= ПИНЫ МОТОРОВ =================
const int LEFT_CTRL = 2;   // Направление левого мотора
const int LEFT_PWM = 5;    // Скорость левого мотора
const int RIGHT_CTRL = 4;  // Направление правого мотора
const int RIGHT_PWM = 6;   // Скорость правого мотора

// ================= ТЕЛЕМЕТРИЯ =================
unsigned long lastTelemetryTime = 0;

void setup() {
    // Аппаратный Serial для связи с ESP (пины 0 и 1)
    Serial.begin(115200);
    delay(1000);  // Даем время на инициализацию
    
    // Настройка пинов моторов
    pinMode(LEFT_CTRL, OUTPUT);
    pinMode(LEFT_PWM, OUTPUT);
    pinMode(RIGHT_CTRL, OUTPUT);
    pinMode(RIGHT_PWM, OUTPUT);
    
    stopMotors();
}

void loop() {
    // Обработка команд от ESP через аппаратный Serial
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        parseCommand(cmd);
    }
    
    // Отправка телеметрии каждые 500мс
    if (millis() - lastTelemetryTime > 500) {
        lastTelemetryTime = millis();
        
        // Чтение датчиков
        int sensor1 = analogRead(A0);
        int sensor2 = analogRead(A1);
        
        // Отправка телеметрии на ESP
        String telemetry = "A0:" + String(sensor1) + " A1:" + String(sensor2);
        Serial.println(telemetry);
    }
}

// Парсинг команды "L:150 R:100"
void parseCommand(String cmd) {
    int leftSpeed = 0;
    int rightSpeed = 0;
    
    int lIndex = cmd.indexOf("L:");
    int rIndex = cmd.indexOf("R:");
    
    if (lIndex >= 0 && rIndex >= 0) {
        String leftStr = cmd.substring(lIndex + 2, rIndex);
        String rightStr = cmd.substring(rIndex + 2);
        
        leftStr.trim();
        rightStr.trim();
        
        leftSpeed = leftStr.toInt();
        rightSpeed = rightStr.toInt();
        
        leftSpeed = constrain(leftSpeed, -255, 255);
        rightSpeed = constrain(rightSpeed, -255, 255);
        
        setMotor(LEFT_PWM, LEFT_CTRL, leftSpeed);
        setMotor(RIGHT_PWM, RIGHT_CTRL, rightSpeed);
    }
}

// Управление мотором
void setMotor(int pwmPin, int ctrlPin, int speed) {
    if (speed > 0) {
        digitalWrite(ctrlPin, HIGH);
        analogWrite(pwmPin, speed);
    } else if (speed < 0) {
        digitalWrite(ctrlPin, LOW);
        analogWrite(pwmPin, abs(speed));
    } else {
        digitalWrite(ctrlPin, LOW);
        analogWrite(pwmPin, 0);
    }
}

void stopMotors() {
    setMotor(LEFT_PWM, LEFT_CTRL, 0);
    setMotor(RIGHT_PWM, RIGHT_CTRL, 0);
}
